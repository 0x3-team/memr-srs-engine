"""Sync acceptance uses disposable native collections and loopback servers only."""
from contextlib import contextmanager
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memr_srs_engine import handlers
from memr_srs_engine.errors import EngineError
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from tests.helpers import temp_collection
from tests.test_decks import RpcCase


class SyncOfflineTests(RpcCase):
    def test_all_sync_methods_are_callable(self):
        expected = {"sync.status", "sync.login", "sync.logout", "sync.run", "sync.resolveFullSync", "sync.media", "sync.abort"}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_status_is_private_passive_and_does_not_create_credentials(self):
        self.note(1)
        before = self.col.db.all("select * from notes"), self.col.db.all("select * from cards"), self.col.undo_status()
        result = self.rpc("sync.status")
        self.assertFalse(result["loggedIn"])
        self.assertIsNone(result["endpoint"])
        self.assertIsNone(result["username"])
        self.assertIsNone(result["remote"])
        self.assertIsNone(result["mediaPending"])
        self.assertEqual((result["local"]["notes"], result["local"]["cards"]), (1, 1))
        self.assertFalse((Path(self.col.path).parent / "sync-auth.json").exists())
        self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"), self.col.undo_status()), before)

    def test_idle_abort_and_repeated_logout_preserve_collection_undo(self):
        self.note(1)
        before = self.col.undo_status()
        for name in ("sync.abort", "sync.logout", "sync.logout"):
            self.assertFalse(any(self.rpc(name)["changes"].values()))
            self.assertEqual(self.col.undo_status(), before)

    def test_invalid_method_shapes_are_rejected_before_work(self):
        for name, params in [("sync.login", {}), ("sync.run", {"endpoint": "http://127.0.0.1/"}),
                             ("sync.resolveFullSync", {"direction": "guess"}), ("sync.media", {"extra": True}),
                             ("sync.abort", {"operationId": "other"})]:
            with self.subTest(name=name):
                self.error(name, params, "invalid_params")


class NativeClient:
    def __init__(self):
        self.fixture = temp_collection()
        self.col = self.fixture.__enter__()
        self.output = io.BytesIO()
        self.log = io.StringIO()
        logger = logging.Logger("native-sync-fixture")
        logger.addHandler(logging.StreamHandler(self.log))
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger)
        self.sequence = 0

    @property
    def auth_path(self):
        return Path(self.col.path).parent / "sync-auth.json"

    def request(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": method, "params": params or {}}).encode())
        frames = [json.loads(line) for line in self.output.getvalue()[offset:].splitlines()]
        frame = next(value for value in frames if not value.get("event") and value.get("id") == self.sequence)
        if "result" in frame:
            assert not validate_value(self.engine.registry.catalogue[method]["result"], frame["result"]), frame
        return frame

    def call(self, method, params=None):
        frame = self.request(method, params)
        assert "result" in frame, frame
        return frame["result"]

    def wait(self, operation, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            progress = self.engine.progress.get(operation["operationId"])
            if progress["status"] != "running":
                assert not validate_value(self.engine.registry.catalogue["progress.get"]["result"], progress), progress
                return progress
            time.sleep(0.01)
        raise AssertionError("Native sync did not reach a terminal state.")

    def long(self, method, params=None, *, error=None):
        progress = self.wait(self.call(method, params))
        if error:
            assert progress["error"] is not None, progress
            assert progress["error"]["code"] == error, progress
            assert progress["status"] == ("aborted" if error == "aborted" else "failed"), progress
        else:
            assert progress["status"] == "completed", progress
        return progress

    def add(self, front):
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"], note["Back"] = front, "Disposable answer"
        self.col.add_note(note, 1)
        return note

    def close(self):
        self.engine.progress.close()
        self.fixture.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class NativeServer:
    def __init__(self, usernames):
        self.temp = tempfile.TemporaryDirectory(prefix="memr-sync-server-test-")
        self.password = "generated-" + secrets.token_hex(16)
        salt = secrets.token_bytes(16)
        encoded = lambda value: base64.b64encode(value).decode().rstrip("=")
        hashed = "$pbkdf2-sha256$i=600000,l=32$" + encoded(salt) + "$" + encoded(hashlib.pbkdf2_hmac("sha256", self.password.encode(), salt, 600000))
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR") if key in os.environ}
        env.update(HOME=self.temp.name, SYNC_HOST="127.0.0.1", SYNC_PORT="0", SYNC_BASE=self.temp.name,
                   PASSWORDS_HASHED="1", RUST_LOG="anki::sync::http_server=info")
        env.update({f"SYNC_USER{index + 1}": f"{name}:{hashed}" for index, name in enumerate(usernames)})
        self.process = subprocess.Popen([sys.executable, "-I", "-B", "-m", "anki.syncserver"], env=env,
                                        cwd=self.temp.name, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        ready = queue.Queue(maxsize=1)

        def read():
            for line in self.process.stdout:
                match = re.search(r"listening.*127\.0\.0\.1:(\d+)", re.sub(r"\x1b\[[0-9;]*m", "", line.decode(errors="replace")))
                if match and ready.empty():
                    ready.put(int(match[1]))

        self.reader = threading.Thread(target=read)
        self.reader.start()
        try:
            self.endpoint = f"http://127.0.0.1:{ready.get(timeout=10)}/"
        except queue.Empty:
            self.close()
            raise AssertionError("Disposable native sync server did not start.") from None

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        self.process.stdout.close()
        self.temp.cleanup()


@contextmanager
def network_fixture(*, redirect=None):
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(self.path)
            entered.set()
            if redirect:
                self.send_response(redirect[0]); self.send_header("Location", redirect[1]); self.send_header("Content-Length", "0"); self.end_headers()
            else:
                release.wait(10)
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield {"endpoint": f"http://127.0.0.1:{server.server_port}/", "entered": entered, "requests": requests}
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join()


class SyncNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = NativeServer(["roundtrip", "credentials", "abort", "backup", "status", "clock-boundary", "clock-step-back", "upload-failure"])

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def login(self, client, username):
        return client.long("sync.login", {"endpoint": self.server.endpoint, "username": username, "password": self.server.password})

    def test_two_native_collections_sync_both_directions_and_media(self):
        with NativeClient() as first, NativeClient() as second:
            first.add("First local card")
            self.login(first, "roundtrip")
            first.long("sync.run", error="conflict")
            self.assertEqual(first.call("sync.status")["required"], "upload")
            first.long("sync.resolveFullSync", {"direction": "download"}, error="conflict")
            first.long("sync.resolveFullSync", {"direction": "upload"})
            self.login(second, "roundtrip")
            second.long("sync.run", error="conflict")
            self.assertEqual(second.call("sync.status")["required"], "download")
            second.long("sync.resolveFullSync", {"direction": "download"})
            self.assertEqual(second.col.db.scalar("select count(*) from notes"), 1)
            self.assertEqual(second.col.db.scalar("select flds from notes").split("\x1f")[0], "First local card")
            second.add("Second local card")
            second.long("sync.run"); first.long("sync.run")
            self.assertEqual(first.col.db.scalar("select count(*) from notes"), 2)
            first.col.media.write_data("generated-sync.txt", b"Native media round trip\n")
            first.long("sync.media"); second.long("sync.media")
            self.assertEqual((Path(second.col.media.dir()) / "generated-sync.txt").read_bytes(), b"Native media round trip\n")
            self.assertIsNotNone(first.call("sync.status")["lastSyncAt"])
            self.assertTrue(list((Path(second.col.path).parent / "backups").glob("*.colpkg")))
            # A changed native schema requires a real explicit choice, even
            # when both collections already have the same cards.
            model = first.col.models.by_name("Basic")
            first.col.models.add_field(model, first.col.models.new_field("Extra native field"))
            first.col.models.save(model)
            first.long("sync.run", error="conflict")
            status = first.call("sync.status")
            self.assertEqual(status["required"], "full")
            self.assertEqual(status["local"]["notes"], 2)
            self.assertIsNone(status["remote"])

    def test_credentials_are_private_persisted_and_never_returned_or_logged(self):
        with NativeClient() as client:
            self.assertNotIn("syncRequired", self.login(client, "credentials")["result"])
            self.assertEqual(client.call("sync.status")["required"], "normal")
            self.assertEqual(stat.S_IMODE(client.auth_path.stat().st_mode), 0o600)
            saved = json.loads(client.auth_path.read_text())
            self.assertEqual(saved["endpoint"], self.server.endpoint)
            self.assertTrue(saved["hkey"])
            status = client.call("sync.status")
            self.assertEqual(status["username"], "credentials")
            public = client.output.getvalue().decode() + client.log.getvalue()
            self.assertNotIn(saved["hkey"], public)
            self.assertNotIn(self.server.password, public)
            self.assertNotIn(self.server.password, client.auth_path.read_text())
            # Reject broad permissions and links before reading their contents.
            client.auth_path.chmod(0o644)
            self.assertEqual(client.request("sync.status")["error"]["code"], "conflict")
            client.auth_path.chmod(0o600)
            client.call("sync.logout")
            self.assertFalse(client.auth_path.exists())
            self.assertFalse(client.call("sync.status")["loggedIn"])

    def test_failed_login_preserves_previous_credentials(self):
        with NativeClient() as client:
            self.login(client, "credentials")
            before = client.auth_path.read_bytes()
            client.long("sync.login", {"endpoint": self.server.endpoint, "username": "credentials", "password": "incorrect-generated-password"}, error="engine_unavailable")
            self.assertEqual(client.auth_path.read_bytes(), before)

    def test_invalid_returned_credentials_are_not_persisted(self):
        for key in ["invalid\nkey", "invalid\x80key", "x" * 4097]:
            with self.subTest(key_length=len(key)), NativeClient() as client:
                with patch.object(client.col, "sync_login", return_value=SimpleNamespace(hkey=key)):
                    client.long("sync.login", {"endpoint": self.server.endpoint, "username": "credentials", "password": "synthetic"}, error="internal")
                self.assertFalse(client.auth_path.exists())

    def test_status_reflects_local_changes_without_network(self):
        with NativeClient() as client:
            client.add("Initial local content")
            self.login(client, "status")
            client.long("sync.run", error="conflict")
            client.long("sync.resolveFullSync", {"direction": "upload"})
            self.assertEqual(client.call("sync.status")["required"], "none")
            time.sleep(0.003)
            client.add("Unsynced local content")
            with patch.object(client.col, "sync_status", side_effect=AssertionError("Status must stay offline")):
                self.assertEqual(client.call("sync.status")["required"], "normal")
                self.assertNotIn("syncRequired", client.long("sync.media")["result"])
                self.assertEqual(client.call("sync.status")["required"], "normal")
                model = client.col.models.by_name("Basic")
                client.col.models.add_field(model, client.col.models.new_field("Extra native field"))
                client.col.models.save(model)
                self.assertEqual(client.call("sync.status")["required"], "full")

    def test_full_upload_is_clean_across_native_timestamp_boundary(self):
        with NativeClient() as client:
            client.add("Uploaded across a clock tick")
            self.login(client, "clock-boundary")
            client.long("sync.run", error="conflict")
            upload = client.col.full_upload_or_download
            uploaded = []

            def upload_across_clock_tick(**kwargs):
                upload(**kwargs)
                modified = client.col.mod
                # Inject the native clock boundary deterministically: Anki's
                # before_upload sets scm=ls, then commits mod on a later tick.
                client.col.db.execute("update col set scm=?, ls=?", modified - 1, modified - 1)
                uploaded.append(client.col.db.first("select mod, scm, usn from col"))

            with patch.object(client.col, "full_upload_or_download", side_effect=upload_across_clock_tick):
                client.long("sync.resolveFullSync", {"direction": "upload"})
            with patch.object(client.col, "sync_status", side_effect=AssertionError("Status must stay offline")):
                self.assertEqual(client.call("sync.status")["required"], "none")
                self.assertEqual(client.call("sync.status")["required"], "none")
            # Keep the uploaded revision and schema intact; only the local
            # sync watermark should advance to cover that exact revision.
            self.assertEqual(client.col.db.first("select mod, scm, usn from col"), uploaded[0])
            self.assertEqual(client.col.db.scalar("select ls from col"), uploaded[0][0])

    def test_full_upload_is_clean_when_clock_steps_back_inside_upload(self):
        with NativeClient() as client:
            client.add("Uploaded across a clock step back")
            self.login(client, "clock-step-back")
            client.long("sync.run", error="conflict")
            upload = client.col.full_upload_or_download

            def upload_across_clock_step_back(**kwargs):
                upload(**kwargs)
                modified = client.col.mod
                # before_upload read the wall clock for scm=ls, then the clock
                # stepped back before the transaction stamped mod.
                client.col.db.execute("update col set scm=?, ls=?", modified + 5, modified + 5)

            with patch.object(client.col, "full_upload_or_download", side_effect=upload_across_clock_step_back):
                client.long("sync.resolveFullSync", {"direction": "upload"})
            modified, schema, last_sync = client.col.db.first("select mod, scm, ls from col")
            self.assertGreaterEqual(last_sync, schema)
            self.assertGreaterEqual(last_sync, modified)
            self.assertEqual(client.call("sync.status")["required"], "none")

    def test_failed_full_upload_does_not_advance_sync_watermark(self):
        with NativeClient() as client:
            client.add("Unsynced after a failed upload")
            self.login(client, "upload-failure")
            client.long("sync.run", error="conflict")
            modified = client.col.mod
            client.col.db.execute("update col set scm=?, ls=?", modified - 1, modified - 1)
            before = client.col.db.first("select mod, scm, ls, usn from col")
            auth_before = client.auth_path.read_bytes()
            with patch.object(client.col, "full_upload_or_download", side_effect=RuntimeError("Synthetic upload failure")):
                client.long("sync.resolveFullSync", {"direction": "upload"}, error="engine_unavailable")
            self.assertEqual(client.col.db.first("select mod, scm, ls, usn from col"), before)
            self.assertEqual(client.auth_path.read_bytes(), auth_before)
            self.assertEqual(client.call("sync.status")["required"], "upload")

    def test_signed_out_operations_do_not_start_network_work(self):
        with NativeClient() as client:
            for method, params in [("sync.run", {}), ("sync.media", {}), ("sync.resolveFullSync", {"direction": "upload"})]:
                with self.subTest(method=method):
                    client.long(method, params, error="not_found")

    def test_forbidden_endpoints_never_reach_native_network(self):
        with NativeClient() as client, patch.object(client.col, "sync_login", side_effect=AssertionError("Forbidden native network call")):
            for url in ["https://ankiweb.net/", "https://sync.ANKIWEB.net./", "https://foo.ankiuser.net/", "http://192.168.1.2/", "file:///tmp/sync"]:
                with self.subTest(url=url):
                    client.long("sync.login", {"endpoint": url, "username": "generated", "password": "generated"}, error="unsupported")
            for url in ["", "https://user:password@example.invalid/", "https://example.invalid/?key=hidden", "https://example.invalid/#fragment", "https://example.invalid/%zz", "https://example.invalid/%0a"]:
                with self.subTest(url=url):
                    client.long("sync.login", {"endpoint": url, "username": "generated", "password": "generated"}, error="invalid_params")

    def test_redirects_do_not_reach_a_second_loopback_destination(self):
        for status in [301, 302, 303, 307, 308]:
            with self.subTest(status=status), network_fixture() as destination, network_fixture(redirect=(status, destination["endpoint"])) as redirect, NativeClient() as client:
                client.long("sync.login", {"endpoint": redirect["endpoint"], "username": "generated", "password": "generated"}, error="unsupported")
                self.assertTrue(redirect["entered"].is_set())
                self.assertEqual(destination["requests"], [])
                self.assertFalse(client.auth_path.exists())

    def test_real_native_network_is_cancelled_for_every_long_sync_method(self):
        for method in ["sync.login", "sync.run", "sync.resolveFullSync", "sync.media"]:
            with self.subTest(method=method), NativeClient() as client, network_fixture() as stalled:
                if method != "sync.login":
                    client.add("Cancellation fixture")
                    self.login(client, "abort")
                    client.long("sync.run", error="conflict")
                    saved = json.loads(client.auth_path.read_text())
                    saved["endpoint"] = stalled["endpoint"]
                    client.auth_path.write_text(json.dumps(saved))
                before = client.auth_path.read_bytes() if client.auth_path.exists() else None
                params = ({"endpoint": stalled["endpoint"], "username": "generated", "password": "generated"} if method == "sync.login"
                          else {"direction": "upload"} if method == "sync.resolveFullSync" else {})
                operation = client.call(method, params)
                self.assertTrue(stalled["entered"].wait(5), "Native operation did not reach the loopback fixture.")
                self.assertEqual(client.request("sync.status")["error"]["code"], "busy")
                started = time.monotonic()
                result = client.call("sync.abort")
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertFalse(any(result["changes"].values()))
                self.assertEqual(client.wait(operation, timeout=2)["status"], "aborted")
                self.assertEqual(client.auth_path.read_bytes() if client.auth_path.exists() else None, before)
                self.assertEqual(client.col.db.scalar("pragma quick_check"), "ok")

    def test_generic_progress_abort_cancels_native_network(self):
        with NativeClient() as client, network_fixture() as stalled:
            operation = client.call("sync.login", {"endpoint": stalled["endpoint"], "username": "generated", "password": "generated"})
            self.assertTrue(stalled["entered"].wait(5))
            client.call("progress.abort", {"operationId": operation["operationId"]})
            self.assertEqual(client.wait(operation, timeout=2)["status"], "aborted")
            self.assertFalse(client.auth_path.exists())

    def test_abort_before_native_handle_registration_is_not_lost(self):
        with NativeClient() as client, network_fixture() as stalled:
            original = client.col.sync_login
            entered, release = threading.Event(), threading.Event()

            def delayed_native(*args):
                entered.set()
                self.assertTrue(release.wait(3))
                return original(*args)

            with patch.object(client.col, "sync_login", side_effect=delayed_native):
                operation = client.call("sync.login", {"endpoint": stalled["endpoint"], "username": "generated", "password": "generated"})
                try:
                    self.assertTrue(entered.wait(3))
                    client.call("sync.abort")
                finally:
                    release.set()
                self.assertEqual(client.wait(operation, timeout=2)["status"], "aborted")
            self.assertFalse(client.auth_path.exists())

    def test_shutdown_cancels_native_network_without_protocol_ticks(self):
        with NativeClient() as client, network_fixture() as stalled:
            operation = client.call("sync.login", {"endpoint": stalled["endpoint"], "username": "generated", "password": "generated"})
            self.assertTrue(stalled["entered"].wait(5))
            started = time.monotonic()
            client.engine.progress.close()
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(client.wait(operation)["status"], "aborted")
            self.assertFalse(client.auth_path.exists())

    def test_download_cannot_replace_collection_if_verified_backup_fails(self):
        with NativeClient() as source, NativeClient() as target:
            source.add("Remote fixture")
            self.login(source, "backup")
            source.long("sync.run", error="conflict")
            source.long("sync.resolveFullSync", {"direction": "upload"})
            target.add("Local data that must survive")
            self.login(target, "backup")
            target.long("sync.run", error="conflict")
            before = target.col.db.all("select * from notes"), target.col.db.all("select * from cards")
            with patch("memr_srs_engine.handlers.maintenance.force_backup", side_effect=EngineError("internal", "Synthetic backup failure.")):
                target.long("sync.resolveFullSync", {"direction": "download"}, error="internal")
            self.assertEqual((target.col.db.all("select * from notes"), target.col.db.all("select * from cards")), before)
