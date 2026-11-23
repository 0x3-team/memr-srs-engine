"""Unit and integration tests for the SRS engine HTTP shim."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from anki.collection import Collection

from tests.helpers import ENGINE_ROOT, LONG_OPERATION_CODE

import http_shim

HTTP_SHIM_SCRIPT = ENGINE_ROOT / "http_shim.py"

FAKE_ENGINE_CODE = r"""
import json, os, sys, time

gate_path = os.environ.get("FAKE_GATE_FILE")
order_path = os.environ.get("FAKE_ORDER_FILE")
mode = os.environ.get("FAKE_MODE", "normal")

if mode == "startup_conflict":
    sys.stdout.write(json.dumps({"id": None, "error": {"code": "conflict", "message": "collection is open"}}) + "\n")
    sys.stdout.flush()
    sys.exit(1)

for raw in sys.stdin:
    req = json.loads(raw)
    rid = req["id"]
    method = req["method"]
    params = req.get("params", {})

    if method == "engine.status":
        reply = {"id": rid, "result": {
            "state": "ready", "engineVersion": "26.9.3-fake", "contractVersion": 1,
            "collectionOpen": True, "mediaPrefix": None, "message": None
        }}
    elif method == "engine.shutdown":
        sys.stdout.write(json.dumps({"id": rid, "result": {}}) + "\n")
        sys.stdout.flush()
        sys.exit(0)
    elif method == "fake.hold":
        if gate_path:
            open(gate_path + ".entered", "w").close()
            while not os.path.exists(gate_path):
                time.sleep(0.01)
        if order_path:
            with open(order_path, "a", encoding="utf-8") as fh:
                fh.write(f"hold:{params.get('seq')}\n")
        reply = {"id": rid, "result": {"seq": params.get("seq")}}
    elif method == "fake.echo":
        if order_path:
            with open(order_path, "a", encoding="utf-8") as fh:
                fh.write(f"echo:{params.get('seq')}\n")
        reply = {"id": rid, "result": {"seq": params.get("seq")}}
    elif method == "fake.progress":
        for done in (1, 2):
            ev = {"event": "progress", "id": rid, "operationId": "op-1",
                  "progress": {"operationId": "op-1", "kind": "fake", "status": "running",
                               "done": done, "total": 2, "label": "Working", "abortable": True,
                               "result": None, "error": None}}
            sys.stdout.write(json.dumps(ev) + "\n")
            sys.stdout.flush()
        reply = {"id": rid, "result": {"operationId": "op-1"}}
    elif method == "fake.crash":
        sys.exit(2)
    elif method == "fake.badFrame":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        continue
    elif method == "fake.oversize":
        big = "x" * (33 * 1024 * 1024)
        sys.stdout.write(json.dumps({"id": rid, "result": {"big": big}}) + "\n")
        sys.stdout.flush()
        continue
    elif method == "fake.sleep":
        time.sleep(float(params.get("seconds", 1.0)))
        reply = {"id": rid, "result": {"slept": True}}
    else:
        reply = {"id": rid, "error": {"code": "unsupported", "message": "Unknown fake method."}}

    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw
    finally:
        conn.close()


def _rpc(port: int, payload: object, *, timeout: float = 10.0) -> tuple[int, dict[str, object]]:
    raw = json.dumps(payload).encode("utf-8")
    status, headers, resp_bytes = _http_request(
        port,
        "POST",
        "/rpc",
        body=raw,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    assert headers.get("content-type") == "application/json", headers
    return status, json.loads(resp_bytes)


@contextmanager
def running_shim(
    directory: str | Path,
    *,
    engine_cmd: list[str] | None = None,
    rpc_timeout: float = 10.0,
    max_pending: int = 128,
):
    shim = http_shim.EngineShim(
        directory,
        engine_cmd=engine_cmd,
        rpc_timeout=rpc_timeout,
        max_pending=max_pending,
        stderr=subprocess.DEVNULL,
    )
    shim.start()
    server = http_shim.ShimHTTPServer(("127.0.0.1", 0), shim)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield shim, port
    finally:
        shim.mark_stopping()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        shim.shutdown()


class FakeShimUnitTests(unittest.TestCase):
    def _fake_cmd(self, extra_code: str = "") -> list[str]:
        return [sys.executable, "-I", "-B", "-c", extra_code + FAKE_ENGINE_CODE]

    def test_fifo_arrival_order_serialization_and_nonblocking_healthz(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-fifo-") as tmpdir:
            gate = Path(tmpdir) / "gate"
            order_file = Path(tmpdir) / "order.txt"
            prefix = (
                f"import os; os.environ['FAKE_GATE_FILE']={str(gate)!r}; "
                f"os.environ['FAKE_ORDER_FILE']={str(order_file)!r}\n"
            )
            with running_shim(tmpdir, engine_cmd=self._fake_cmd(prefix)) as (shim, port):
                results: dict[int, tuple[int, dict[str, object]]] = {}
                errors: list[Exception] = []

                def send_req(seq: int, method: str) -> None:
                    try:
                        results[seq] = _rpc(port, {"id": seq, "method": method, "params": {"seq": seq}})
                    except Exception as exc:
                        errors.append(exc)

                t1 = threading.Thread(target=send_req, args=(1, "fake.hold"))
                t1.start()
                deadline = time.monotonic() + 5.0
                while not Path(str(gate) + ".entered").exists():
                    self.assertLess(time.monotonic(), deadline, "First request never entered fake engine.")
                    time.sleep(0.01)

                # /healthz must remain immediately responsive while req 1 blocks the RPC lane.
                h_status, _headers, h_raw = _http_request(port, "GET", "/healthz", timeout=1.0)
                self.assertEqual(h_status, 200)
                self.assertEqual(json.loads(h_raw)["ok"], True)

                # Enqueue requests 2..5 in deterministic arrival order while req 1 holds the engine.
                queued_threads: list[threading.Thread] = []
                for seq in (2, 3, 4, 5):
                    with self.subTest(enqueuing=seq):
                        before = len(shim._queue)
                        th = threading.Thread(target=send_req, args=(seq, "fake.echo"))
                        th.start()
                        queued_threads.append(th)
                        q_deadline = time.monotonic() + 5.0
                        while len(shim._queue) < before + 1:
                            self.assertLess(time.monotonic(), q_deadline)
                            time.sleep(0.005)

                # Release request 1.
                gate.write_text("go", encoding="utf-8")
                t1.join(timeout=5.0)
                for th in queued_threads:
                    th.join(timeout=5.0)

                self.assertEqual(errors, [])
                self.assertEqual(
                    order_file.read_text(encoding="utf-8").splitlines(),
                    ["hold:1", "echo:2", "echo:3", "echo:4", "echo:5"],
                )
                for seq in range(1, 6):
                    status, body = results[seq]
                    self.assertEqual(status, 200)
                    self.assertEqual(body["id"], seq)
                    self.assertEqual(body["result"], {"seq": seq})

    def test_http_validation_routes_and_limits(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-http-") as tmpdir:
            with running_shim(tmpdir, engine_cmd=self._fake_cmd()) as (_shim, port):
                # 404 & 405
                status, _, _ = _http_request(port, "GET", "/unknown")
                self.assertEqual(status, 404)
                status, headers, _ = _http_request(port, "GET", "/rpc")
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("allow"), "POST")
                status, headers, _ = _http_request(port, "POST", "/healthz")
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("allow"), "GET, HEAD")
                status, headers, raw = _http_request(port, "HEAD", "/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(raw, b"")
                self.assertGreater(int(headers["content-length"]), 0)

                # 415 wrong Content-Type / Content-Encoding
                status, _, _ = _http_request(
                    port, "POST", "/rpc", body=b"{}", headers={"Content-Type": "text/plain"}
                )
                self.assertEqual(status, 415)
                status, _, _ = _http_request(
                    port,
                    "POST",
                    "/rpc",
                    body=b"{}",
                    headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
                )
                self.assertEqual(status, 415)

                # 413 oversized Content-Length (> 4 MiB)
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
                try:
                    conn.putrequest("POST", "/rpc")
                    conn.putheader("Content-Type", "application/json")
                    conn.putheader("Content-Length", str(http_shim.MAX_REQUEST_BYTES + 1))
                    conn.endheaders()
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 413)
                    resp.read()
                finally:
                    conn.close()

                # 411 missing Content-Length without chunked encoding
                with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
                    sock.sendall(b"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
                    raw_resp = b""
                    while chunk := sock.recv(4096):
                        raw_resp += chunk
                self.assertTrue(raw_resp.startswith(b"HTTP/1.1 411 "), raw_resp)

                # Valid Transfer-Encoding: chunked succeeds, oversized chunked returns 413
                with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
                    payload = b'{"id":9,"method":"fake.echo","params":{"seq":9}}'
                    req = (
                        b"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                        b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                        + f"{len(payload):x}\r\n".encode("ascii")
                        + payload
                        + b"\r\n0\r\n\r\n"
                    )
                    sock.sendall(req)
                    raw_resp = b""
                    while chunk := sock.recv(4096):
                        raw_resp += chunk
                self.assertTrue(raw_resp.startswith(b"HTTP/1.1 200 "), raw_resp)
                self.assertEqual(json.loads(raw_resp.split(b"\r\n\r\n", 1)[1])["result"], {"seq": 9})

                with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
                    oversize_hex = f"{http_shim.MAX_REQUEST_BYTES + 1:x}\r\n".encode("ascii")
                    sock.sendall(
                        b"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                        b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                        + oversize_hex
                    )
                    raw_resp = b""
                    while chunk := sock.recv(4096):
                        raw_resp += chunk
                self.assertTrue(raw_resp.startswith(b"HTTP/1.1 413 "), raw_resp)

                # 400 invalid JSON / duplicate keys / NaN / bad id / bad method / bad params
                bad_bodies = [
                    b"not json",
                    b'{"id":1,"id":2,"method":"fake.echo"}',
                    b'{"id":1,"method":"fake.echo","params":{"x":NaN}}',
                    b"[]",
                    b'{"id":0,"method":"fake.echo"}',
                    b'{"id":-1,"method":"fake.echo"}',
                    b'{"id":true,"method":"fake.echo"}',
                    b'{"id":"1","method":"fake.echo"}',
                    b'{"id":9007199254740992,"method":"fake.echo"}',
                    b'{"id":1,"method":"invalid"}',
                    b'{"id":1,"method":"fake.echo","params":[]}',
                    b'{"id":1,"method":"fake.echo","extra":1}',
                ]
                for body in bad_bodies:
                    with self.subTest(body=body):
                        st, _, raw = _http_request(
                            port, "POST", "/rpc", body=body, headers={"Content-Type": "application/json"}
                        )
                        self.assertEqual(st, 400)
                        self.assertEqual(json.loads(raw)["error"]["code"], "invalid_params")

                # engine.shutdown is blocked externally without stopping the engine.
                st, body = _rpc(port, {"id": 10, "method": "engine.shutdown", "params": {}})
                self.assertEqual(st, 200)
                self.assertEqual(body["id"], 10)
                self.assertEqual(body["error"]["code"], "unsupported")

                # Unsolicited progress frames are drained cleanly without corrupting reply.
                st, body = _rpc(port, {"id": 11, "method": "fake.progress", "params": {}})
                self.assertEqual(st, 200)
                self.assertEqual(body, {"id": 11, "result": {"operationId": "op-1"}})

                # Subsequent valid call still succeeds.
                st, body = _rpc(port, {"id": 12, "method": "fake.echo", "params": {"seq": 12}})
                self.assertEqual(st, 200)
                self.assertEqual(body, {"id": 12, "result": {"seq": 12}})

    def test_queue_overflow_returns_429_busy(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-qfull-") as tmpdir:
            gate = Path(tmpdir) / "gate"
            prefix = f"import os; os.environ['FAKE_GATE_FILE']={str(gate)!r}\n"
            with running_shim(tmpdir, engine_cmd=self._fake_cmd(prefix), max_pending=1) as (shim, port):
                r1: list[tuple[int, dict[str, object]]] = []
                r2: list[tuple[int, dict[str, object]]] = []
                t1 = threading.Thread(target=lambda: r1.append(_rpc(port, {"id": 1, "method": "fake.hold", "params": {"seq": 1}})))
                t1.start()
                deadline = time.monotonic() + 5.0
                while not Path(str(gate) + ".entered").exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)

                t2 = threading.Thread(target=lambda: r2.append(_rpc(port, {"id": 2, "method": "fake.echo", "params": {"seq": 2}})))
                t2.start()
                while len(shim._queue) < 1:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.005)

                status, body = _rpc(port, {"id": 3, "method": "fake.echo", "params": {"seq": 3}})
                self.assertEqual(status, 429)
                self.assertEqual(body["id"], 3)
                self.assertEqual(body["error"]["code"], "busy")

                gate.write_text("go", encoding="utf-8")
                t1.join(timeout=5.0)
                t2.join(timeout=5.0)
                self.assertEqual(r1[0][0], 200)
                self.assertEqual(r2[0][0], 200)

    def test_engine_crash_and_protocol_fault_mark_healthz_unhealthy(self):
        for bad_method in ("fake.crash", "fake.badFrame", "fake.oversize"):
            with self.subTest(bad_method=bad_method), tempfile.TemporaryDirectory(prefix="memr-shim-fault-") as tmpdir:
                with running_shim(tmpdir, engine_cmd=self._fake_cmd()) as (_shim, port):
                    status, body = _rpc(port, {"id": 1, "method": bad_method, "params": {}})
                    self.assertEqual(status, 503)
                    self.assertEqual(body["error"]["code"], "engine_unavailable")

                    h_status, _, h_raw = _http_request(port, "GET", "/healthz")
                    self.assertEqual(h_status, 503)
                    self.assertEqual(json.loads(h_raw), {"ok": False, "status": "unhealthy"})

                    status2, body2 = _rpc(port, {"id": 2, "method": "fake.echo", "params": {}})
                    self.assertEqual(status2, 503)
                    self.assertEqual(body2["error"]["code"], "engine_unavailable")

    def test_timeout_faults_session(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-timeout-") as tmpdir:
            with running_shim(tmpdir, engine_cmd=self._fake_cmd(), rpc_timeout=0.15) as (_shim, port):
                status, body = _rpc(port, {"id": 1, "method": "fake.sleep", "params": {"seconds": 1.0}})
                self.assertEqual(status, 503)
                self.assertEqual(body["error"]["code"], "engine_unavailable")
                h_status, _, _ = _http_request(port, "GET", "/healthz")
                self.assertEqual(h_status, 503)

    def test_startup_conflict_is_reported(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-conflict-") as tmpdir:
            prefix = "import os; os.environ['FAKE_MODE']='startup_conflict'\n"
            shim = http_shim.EngineShim(tmpdir, engine_cmd=self._fake_cmd(prefix), stderr=subprocess.DEVNULL)
            with self.assertRaises(http_shim.EngineStartupError) as ctx:
                shim.start()
            self.assertEqual(ctx.exception.code, "conflict")
            self.assertEqual(ctx.exception.status, 409)


class RealEngineShimIntegrationTests(unittest.TestCase):
    def test_real_engine_rpc_healthz_and_long_operation(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-real-") as tmpdir:
            with running_shim(tmpdir) as (_shim, port):
                h_status, _, h_raw = _http_request(port, "GET", "/healthz")
                self.assertEqual(h_status, 200)
                health = json.loads(h_raw)
                self.assertTrue(health["ok"])
                self.assertEqual(health["status"], "ready")
                self.assertTrue(health["collectionOpen"])
                self.assertTrue(health["engineVersion"])

                # Omitted id and explicit id both work.
                st, body = _rpc(port, {"method": "engine.status"})
                self.assertEqual(st, 200)
                self.assertIsInstance(body["id"], int)
                self.assertEqual(body["result"]["state"], "ready")

                st, created = _rpc(port, {"id": 7, "method": "decks.create", "params": {"name": "Cloud Deck"}})
                self.assertEqual(st, 200)
                self.assertEqual(created["id"], 7)
                deck_id = created["result"]["deckId"]

                st, tree = _rpc(port, {"id": 8, "method": "decks.tree", "params": {}})
                self.assertEqual(st, 200)
                self.assertTrue(any(d["id"] == deck_id for d in tree["result"]["decks"]))

                st, nt_list = _rpc(port, {"id": 100, "method": "notetypes.list", "params": {}})
                self.assertEqual(st, 200)
                basic_id = next(nt["id"] for nt in nt_list["result"]["notetypes"] if nt["name"] == "Basic")

                st, added = _rpc(
                    port,
                    {
                        "id": 101,
                        "method": "notes.add",
                        "params": {
                            "notetypeId": basic_id,
                            "deckId": deck_id,
                            "fields": ["Cloud Question", "Cloud Answer"],
                            "tags": ["cloud"],
                        },
                    },
                )
                self.assertEqual(st, 200)
                self.assertIsInstance(added["result"]["noteId"], int)

                st, err = _rpc(port, {"id": 9, "method": "unknown.method", "params": {}})
                self.assertEqual(st, 200)
                self.assertEqual(err["error"]["code"], "unsupported")

                st, err = _rpc(port, {"id": 10, "method": "decks.create", "params": {"unexpected": True}})
                self.assertEqual(st, 200)
                self.assertEqual(err["error"]["code"], "invalid_params")

                # Long operation + progress polling without pipe deadlock.
                st, ack = _rpc(port, {"id": 11, "method": "maintenance.checkDatabase", "params": {}})
                self.assertEqual(st, 200)
                op_id = ack["result"]["operationId"]
                deadline = time.monotonic() + 10.0
                terminal = None
                while time.monotonic() < deadline:
                    st, prog = _rpc(port, {"id": 12, "method": "progress.get", "params": {"operationId": op_id}})
                    self.assertEqual(st, 200)
                    if prog["result"]["status"] != "running":
                        terminal = prog["result"]
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(terminal)
                self.assertEqual(terminal["status"], "completed")

    def test_subprocess_requires_collection_dir_env(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-noenv-") as tmpdir:
            env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "TEMP") if k in os.environ}
            proc = subprocess.run(
                [sys.executable, "-I", "-B", str(HTTP_SHIM_SCRIPT)],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("MEMR_SRS_DIR", proc.stderr)

    def test_subprocess_sigterm_closes_collection_cleanly_and_writes_only_under_srs_dir(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-proc-") as tmpdir:
            srs_dir = Path(tmpdir) / "srs"
            cwd_dir = Path(tmpdir) / "cwd"
            scratch_tmp = Path(tmpdir) / "tmp"
            cwd_dir.mkdir()
            scratch_tmp.mkdir()
            port = _free_port()
            env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
            env.update(
                MEMR_SRS_DIR=str(srs_dir),
                MEMR_SRS_HOST="127.0.0.1",
                MEMR_SRS_PORT=str(port),
                TMPDIR=str(scratch_tmp),
                TEMP=str(scratch_tmp),
            )
            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", str(HTTP_SHIM_SCRIPT)],
                cwd=str(cwd_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    try:
                        st, _, raw = _http_request(port, "GET", "/healthz", timeout=1.0)
                        if st == 200 and json.loads(raw).get("ok"):
                            break
                    except OSError:
                        pass
                    self.assertIsNone(proc.poll(), "http_shim exited before becoming healthy.")
                    time.sleep(0.05)
                else:
                    self.fail("http_shim did not become healthy in time.")

                st, created = _rpc(port, {"id": 1, "method": "decks.create", "params": {"name": "Persisted Deck"}})
                self.assertEqual(st, 200)
                self.assertIsInstance(created["result"]["deckId"], int)

                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=8.0), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5.0)
                proc.stdout.close()
                proc.stderr.close()

            # Nothing written to cwd; collection persisted and lock released.
            self.assertEqual(list(cwd_dir.iterdir()), [])
            self.assertTrue((srs_dir / "collection.anki2").is_file())
            col = Collection(str(srs_dir / "collection.anki2"))
            try:
                self.assertEqual(col.db.scalar("pragma quick_check"), "ok")
                self.assertIsNotNone(col.decks.id("Persisted Deck", create=False))
            finally:
                col.close()

    def test_shutdown_while_long_operation_active_joins_and_releases_collection(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-longterm-") as tmpdir:
            srs_dir = Path(tmpdir) / "srs"
            cmd = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv.pop(1)); " + LONG_OPERATION_CODE,
                str(ENGINE_ROOT),
                "--collection",
                str(srs_dir / "collection.anki2"),
            ]
            with running_shim(tmpdir, engine_cmd=cmd) as (shim, port):
                st, ack = _rpc(port, {"id": 1, "method": "maintenance.checkDatabase", "params": {}})
                self.assertEqual(st, 200)
                self.assertTrue(ack["result"]["operationId"])
                shim.shutdown()
                self.assertEqual(shim._proc.returncode, 0)

            col = Collection(str(srs_dir / "collection.anki2"))
            try:
                self.assertEqual(col.db.scalar("pragma quick_check"), "ok")
            finally:
                col.close()


if __name__ == "__main__":
    unittest.main()
