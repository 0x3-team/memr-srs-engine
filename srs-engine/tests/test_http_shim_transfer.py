"""Shim collection transfers: POST /upload and GET /export/<token>.

The hosted EngineHost pushes an upload into the container and pulls an export
back over the hosted engine host's transfer channel. These tests exercise the two
shim endpoints against a real engine and against a scripted one, covering the
gateway parity rules the adapter relies on: bounded streaming, the
declared-vs-actual byte check, one-claim export tokens, exact bytes, and no
path traversal through a token or an engine-supplied filename.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote

from anki.collection import Collection
from anki import import_export_pb2 as pb

from tests.test_http_shim import _http_request, _rpc, running_shim

import http_shim

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+g6wAAAAASUVORK5CYII="
)

# The scripted engine answers progress.get with whatever file metadata the
# caller asks for, so registration can be tested without a real export.
FAKE_TRANSFER_ENGINE = r"""
import json, sys

for raw in sys.stdin:
    req = json.loads(raw)
    rid, method, params = req["id"], req["method"], req.get("params", {})
    if method == "engine.status":
        reply = {"id": rid, "result": {"state": "ready", "engineVersion": "fake",
                 "contractVersion": 1, "collectionOpen": True, "mediaPrefix": None, "message": None}}
    elif method == "engine.shutdown":
        sys.stdout.write(json.dumps({"id": rid, "result": {}}) + "\n")
        sys.stdout.flush()
        sys.exit(0)
    elif method == "progress.get":
        metadata = {"downloadToken": params.get("token", "A" * 43),
                    "filename": params.get("filename", "notes.csv"),
                    "bytes": params.get("bytes", 3),
                    "expiresAt": params.get("expiresAt", 1)}
        reply = {"id": rid, "result": {"operationId": "op-1", "kind": "export", "status": "completed",
                 "done": 1, "total": 1, "label": "Export completed", "abortable": False,
                 "result": {"summary": "Exported 1 notes.", "file": metadata}, "error": None}}
    else:
        reply = {"id": rid, "error": {"code": "unsupported", "message": "Unknown fake method."}}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
"""


def _build_package(directory: Path) -> Path:
    """A .apkg holding one note whose back references one media file."""
    directory.mkdir(parents=True, exist_ok=True)
    collection = Collection(str(directory / "collection.anki2"))
    try:
        deck = collection.decks.id("Packaged deck")
        note = collection.new_note(collection.models.by_name("Basic"))
        note["Front"] = "Packaged question"
        note["Back"] = '<img src="pack.png">'
        collection.add_note(note, deck)
        collection.media.write_data("pack.png", PNG)
        package = directory / "pack.apkg"
        collection.export_anki_package(
            out_path=str(package),
            options=pb.ExportAnkiPackageOptions(
                with_scheduling=False,
                with_media=True,
                with_deck_configs=True,
                legacy=False,
            ),
            limit=None,
        )
    finally:
        collection.close()
    return package


def _upload(port: int, filename: str | None, body: bytes, **headers: str):
    query = "" if filename is None else "?filename=" + quote(filename)
    merged = {"Content-Type": "application/octet-stream"}
    merged.update(headers)
    return _http_request(port, "POST", "/upload" + query, body=body, headers=merged)


def _raw_exchange(port: int, request: bytes, *, timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
    """Send a hand-built request so framing (length, chunking) is exact."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    parsed = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        parsed[key.strip().lower()] = value.strip()
    return status, parsed, body


def _wait_for_operation(port: int, operation_id: str, *, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status, reply = _rpc(port, {"id": 90, "method": "progress.get", "params": {"operationId": operation_id}})
        assert status == 200, reply
        result = reply["result"]
        if result["status"] in ("completed", "failed", "aborted"):
            return result
        assert time.monotonic() < deadline, f"operation {operation_id} did not finish: {result}"
        time.sleep(0.02)


class UploadTests(unittest.TestCase):
    def test_upload_package_round_trip_imports_notes_and_media(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-package-") as source, \
                tempfile.TemporaryDirectory(prefix="memr-shim-upload-") as target:
            package = _build_package(Path(source))
            body = package.read_bytes()
            with running_shim(target) as (_shim, port):
                status, headers, raw = _upload(port, "review.apkg", body)
                self.assertEqual(status, 200, raw)
                self.assertEqual(headers.get("content-type"), "application/json")
                payload = json.loads(raw)
                self.assertRegex(
                    payload["uploadId"],
                    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.apkg$",
                )
                self.assertEqual(payload["bytes"], len(body))

                staged = Path(target) / "incoming" / payload["uploadId"]
                self.assertTrue(staged.is_file())
                self.assertEqual(staged.read_bytes(), body)
                self.assertEqual(list((Path(target) / "incoming").glob("*.upload")), [])

                status, reply = _rpc(port, {"id": 1, "method": "transfer.importPackage", "params": {
                    "uploadId": payload["uploadId"],
                    "mergeNotetypes": True,
                    "updateNotes": "ifNewer",
                    "updateNotetypes": "ifNewer",
                    "includeScheduling": False,
                    "includeDeckConfigs": True,
                }})
                self.assertEqual(status, 200, reply)
                outcome = _wait_for_operation(port, reply["result"]["operationId"])
                self.assertEqual(outcome["status"], "completed", outcome)

                status, reply = _rpc(port, {"id": 2, "method": "search.notes", "params": {"search": ""}})
                self.assertEqual(status, 200, reply)
                self.assertEqual(len(reply["result"]["noteIds"]), 1)

                # The media archive inside the package lands in the media store.
                media = Path(target) / "collection.media" / "pack.png"
                self.assertTrue(media.is_file(), list((Path(target) / "collection.media").iterdir()))
                self.assertEqual(media.read_bytes(), PNG)

    def test_upload_refuses_declared_byte_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-short-") as target:
            with running_shim(target) as (_shim, port):
                body = b"not a real package"
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10.0)
                try:
                    conn.putrequest("POST", "/upload?filename=short.apkg")
                    conn.putheader("Content-Type", "application/octet-stream")
                    conn.putheader("Content-Length", str(len(body) + 8))
                    conn.endheaders()
                    conn.send(body)
                    # Closing the write side makes the shim see EOF mid-body.
                    conn.sock.shutdown(socket.SHUT_WR)
                    response = conn.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 400)
                    self.assertEqual(payload["error"]["code"], "invalid_params")
                finally:
                    conn.close()

                incoming = Path(target) / "incoming"
                leftovers = list(incoming.iterdir()) if incoming.is_dir() else []
                self.assertEqual(leftovers, [], "a refused upload must not be published or staged")

    def test_upload_guards_and_chunked_streaming(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-guards-") as target:
            with running_shim(target) as (_shim, port):
                # Wrong content type / content encoding.
                status, _headers, raw = _http_request(
                    port, "POST", "/upload?filename=notes.csv", body=b"x",
                    headers={"Content-Type": "text/csv"},
                )
                self.assertEqual(status, 415)
                self.assertEqual(json.loads(raw)["error"]["code"], "invalid_params")
                status, _headers, raw = _http_request(
                    port, "POST", "/upload?filename=notes.csv", body=b"x",
                    headers={"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"},
                )
                self.assertEqual(status, 415)

                # Hostile or absent filenames.
                for name in ("../escape.csv", "a/b.csv", ".hidden", "bad\u0000name.csv", "CON.csv", "trail."):
                    with self.subTest(name=name):
                        status, _headers, raw = _http_request(
                            port, "POST", "/upload?filename=" + quote(name), body=b"x",
                            headers={"Content-Type": "application/octet-stream"},
                        )
                        self.assertEqual(status, 400, raw)
                        self.assertEqual(json.loads(raw)["error"]["code"], "invalid_params")
                status, _headers, raw = _upload(port, None, b"x")
                self.assertEqual(status, 400)

                # Missing framing.
                status, _headers, _body = _raw_exchange(
                    port,
                    b"POST /upload?filename=notes.csv HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/octet-stream\r\nConnection: close\r\n\r\n",
                )
                self.assertEqual(status, 411)

                # Declared length above the cap is refused before reading.
                status, _headers, _body = _raw_exchange(
                    port,
                    b"POST /upload?filename=notes.csv HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/octet-stream\r\n"
                    + f"Content-Length: {http_shim.MAX_UPLOAD_BYTES + 1}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n",
                )
                self.assertEqual(status, 413)

                # X-Upload-Name is the adapter-free metadata form; the
                # extension of a media archive is preserved on the handle.
                archive = b"PK\x03\x04 fake media archive"
                status, _headers, raw = _upload(
                    port, None, archive, **{"X-Upload-Name": "media.zip"},
                )
                self.assertEqual(status, 200, raw)
                payload = json.loads(raw)
                self.assertRegex(payload["uploadId"], r"\.zip\Z")
                self.assertEqual(payload["bytes"], len(archive))
                self.assertEqual((Path(target) / "incoming" / payload["uploadId"]).read_bytes(), archive)

                # Chunked bodies stream without a Content-Length.
                chunk = b"front,back\nQuestion,Answer\n"
                status, _headers, body = _raw_exchange(
                    port,
                    b"POST /upload?filename=chunked.csv HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/octet-stream\r\n"
                    b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                    + f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n0\r\n\r\n",
                )
                self.assertEqual(status, 200, body)
                chunked = json.loads(body)
                self.assertEqual(chunked["bytes"], len(chunk))
                self.assertTrue(chunked["uploadId"].endswith(".csv"))

                # A lowered cap cuts an over-limit chunked body mid-stream.
                previous = os.environ.get("MEMR_SRS_UPLOAD_MAX_BYTES")
                os.environ["MEMR_SRS_UPLOAD_MAX_BYTES"] = "16"
                try:
                    oversized = b"x" * 32
                    status, _headers, body = _raw_exchange(
                        port,
                        b"POST /upload?filename=big.csv HTTP/1.1\r\nHost: localhost\r\n"
                        b"Content-Type: application/octet-stream\r\n"
                        b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                        + f"{len(oversized):x}\r\n".encode("ascii") + oversized + b"\r\n0\r\n\r\n",
                    )
                    self.assertEqual(status, 413, body)
                finally:
                    if previous is None:
                        os.environ.pop("MEMR_SRS_UPLOAD_MAX_BYTES", None)
                    else:
                        os.environ["MEMR_SRS_UPLOAD_MAX_BYTES"] = previous


class ExportTests(unittest.TestCase):
    def test_export_token_serves_exact_bytes_once(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-export-") as target:
            with running_shim(target) as (_shim, port):
                status, reply = _rpc(port, {"id": 1, "method": "transfer.exportCsv", "params": {
                    "scope": {},
                    "mode": "notes",
                    "delimiter": ",",
                    "includeHtml": True,
                    "includeTags": True,
                    "includeGuid": False,
                    "includeDeck": False,
                    "includeNotetype": False,
                }})
                self.assertEqual(status, 200, reply)
                outcome = _wait_for_operation(port, reply["result"]["operationId"])
                self.assertEqual(outcome["status"], "completed", outcome)
                metadata = outcome["result"]["file"]
                token = metadata["downloadToken"]
                self.assertRegex(token, r"^[A-Za-z0-9_-]{32,128}$")

                published = Path(target) / "exports" / token / metadata["filename"]
                expected = published.read_bytes()
                self.assertEqual(metadata["bytes"], len(expected))
                self.assertEqual(metadata["filename"], "notes.csv")

                status, headers, body = _http_request(port, "GET", f"/export/{token}")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "application/octet-stream")
                self.assertEqual(headers.get("content-length"), str(len(expected)))
                self.assertEqual(body, expected)

                # One claim per token: the second read is gone.
                status, _headers, raw = _http_request(port, "GET", f"/export/{token}")
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(raw)["error"]["code"], "not_found")

                # Unknown tokens answer the same 404.
                status, _headers, raw = _http_request(port, "GET", "/export/" + "a" * 43)
                self.assertEqual(status, 404)

    def test_export_token_refuses_traversal_and_wrong_methods(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-traversal-") as target:
            with running_shim(target) as (_shim, port):
                for path in (
                    "/export/../../collection.anki2",
                    "/export/..%2f..%2fcollection.anki2",
                    "/export/%2e%2e%2f%2e%2e%2fcollection.anki2",
                    "/export/",
                    "/export/" + "a" * 31,
                    "/export/" + "a" * 129,
                    "/export/" + "a" * 43 + "/../../collection.anki2",
                ):
                    with self.subTest(path=path):
                        status, _headers, raw = _http_request(port, "GET", path)
                        self.assertEqual(status, 404, raw)
                        self.assertEqual(json.loads(raw)["error"]["code"], "not_found")

                # HEAD must never consume a token, and /export is read-only.
                status, headers, _body = _http_request(port, "HEAD", "/export/" + "a" * 43)
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("allow"), "GET")
                status, headers, _body = _http_request(
                    port, "POST", "/export/" + "a" * 43, body=b"", headers={},
                )
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("allow"), "GET")


class ScriptedRegistrationTests(unittest.TestCase):
    def _fake_cmd(self) -> list[str]:
        return [sys.executable, "-I", "-B", "-c", FAKE_TRANSFER_ENGINE]

    def test_registration_refuses_hostile_metadata(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-register-") as target:
            with running_shim(target, engine_cmd=self._fake_cmd()) as (_shim, port):
                expiry = int(time.time()) + 600
                for filename in ("../collection.anki2", "notes.zip", ".hidden.csv", "a/../b.csv", ""):
                    with self.subTest(filename=filename):
                        status, reply = _rpc(port, {"id": 1, "method": "progress.get", "params": {
                            "token": "B" * 43, "filename": filename, "bytes": 3, "expiresAt": expiry,
                        }})
                        self.assertEqual(status, 500, reply)
                        self.assertEqual(reply["error"]["code"], "internal")
                # A bad token shape is refused too.
                status, reply = _rpc(port, {"id": 2, "method": "progress.get", "params": {
                    "token": "../" + "B" * 40, "filename": "notes.csv", "bytes": 3, "expiresAt": expiry,
                }})
                self.assertEqual(status, 500)
                self.assertEqual(reply["error"]["code"], "internal")

    def test_download_requires_the_registered_size_and_one_claim(self):
        with tempfile.TemporaryDirectory(prefix="memr-shim-size-") as target:
            content = b"front,back\nQuestion,Answer\n"
            mismatched = "C" * 43
            matched = "D" * 43
            for token in (mismatched, matched):
                folder = Path(target) / "exports" / token
                folder.mkdir(parents=True)
                (folder / "notes.csv").write_bytes(content)
            expiry = int(time.time()) + 600
            with running_shim(target, engine_cmd=self._fake_cmd()) as (_shim, port):
                # A registered size that disagrees with the published file is
                # never served, and the claim is still consumed.
                status, reply = _rpc(port, {"id": 1, "method": "progress.get", "params": {
                    "token": mismatched, "filename": "notes.csv", "bytes": len(content) + 1, "expiresAt": expiry,
                }})
                self.assertEqual(status, 200, reply)
                status, _headers, raw = _http_request(port, "GET", f"/export/{mismatched}")
                self.assertEqual(status, 404, raw)

                # Re-registration must describe the same file or fail closed.
                status, reply = _rpc(port, {"id": 2, "method": "progress.get", "params": {
                    "token": mismatched, "filename": "notes.csv", "bytes": len(content), "expiresAt": expiry,
                }})
                self.assertEqual(status, 500, reply)
                self.assertEqual(reply["error"]["code"], "internal")

                # A matching registration serves the file exactly once.
                status, reply = _rpc(port, {"id": 3, "method": "progress.get", "params": {
                    "token": matched, "filename": "notes.csv", "bytes": len(content), "expiresAt": expiry,
                }})
                self.assertEqual(status, 200, reply)
                self.assertEqual(reply["result"]["result"]["file"]["expiresAt"], expiry)
                status, headers, body = _http_request(port, "GET", f"/export/{matched}")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-length"), str(len(content)))
                self.assertEqual(body, content)
                status, _headers, raw = _http_request(port, "GET", f"/export/{matched}")
                self.assertEqual(status, 404, raw)


if __name__ == "__main__":
    unittest.main()
