#!/usr/bin/env python3
"""Minimal single-collection HTTP shim wrapping the JSON-RPC SRS engine.

Exposes:
  POST /rpc    Forward one JSON-RPC request to the engine in FIFO arrival order.
  GET /healthz Non-blocking readiness and child-liveness probe.

Collection transfers (the hosted EngineHost's container channel):
  POST /upload?filename=<name>  Stream an octet-stream body into
                                <collection>/incoming/<uploadId><ext> and
                                answer { uploadId, bytes }; the existing
                                resolve_upload machinery then reads it.
  GET  /export/<token>          Serve the one published file for a token that
                                a completed progress.get result.file named.
                                One claim per token; unknown tokens 404.

With MEMR_SRS_REQUIRE_RESTORE=1 (cloud mode) the shim also exposes:
  POST /restore     Install a verified zstd snapshot, then open the collection.
  GET /snapshot     Stream a zstd-consistent copy of the open collection.
  GET /media-index  List media files with sizes and SHA-256 digests.
  GET /media/<name> Stream one media file.
In cloud mode no engine starts and no collection may be opened until a
restore succeeds; POST /rpc, /snapshot and /media* answer 409 beforehand.

All runtime state is kept inside the configured collection directory and /tmp.
"""
from __future__ import annotations

import argparse
from collections import deque
import errno
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_SAFE_INTEGER = 9007199254740991
MAX_CHUNK_LINE_BYTES = 4096
# Snapshot bodies are bounded so a bad host cannot fill the container disk.
MAX_RESTORE_BYTES = 2 * 1024 * 1024 * 1024
MAX_COLLECTION_BYTES = 8 * 1024 * 1024 * 1024
MEDIA_MAX_NAME_BYTES = 255
# Transfers match the Memr upload gateway's limits: a 2 GiB upload cap
# (MEMR_SRS_UPLOAD_MAX_BYTES may only lower it), a 64 MiB free-space reserve
# and a download token that stays claimable for ten minutes at most.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_RESERVE_BYTES = 64 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024
DOWNLOAD_TTL_MS = 10 * 60 * 1000
DOWNLOAD_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
EXPORT_EXTENSIONS = frozenset({".apkg", ".colpkg", ".csv", ".txt"})
UPLOAD_SUFFIX_RE = re.compile(r"\.[a-z0-9]{1,16}\Z")
UPLOAD_UNSAFE_CHARS_RE = re.compile(r"[\u0000-\u001f\u007f/\\<>:\"|?*]")
EXPORT_UNSAFE_CHARS_RE = re.compile(r"[\u0000-\u001f\u007f/\\]")
RESERVED_NAME_RE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|\Z)", re.IGNORECASE)
SQLITE_HEADER = b"SQLite format 3\x00"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_STARTUP_TIMEOUT_S = 30.0
DEFAULT_RPC_TIMEOUT_S = 30.0
DEFAULT_SHUTDOWN_GRACE_S = 5.0
DEFAULT_TERMINATE_GRACE_S = 2.0
DEFAULT_INFLIGHT_SHUTDOWN_WAIT_S = 1.5
DEFAULT_MAX_PENDING = 128

ENGINE_LAUNCH = Path(__file__).resolve().with_name("launch.py")
METHOD_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*\.[a-zA-Z][a-zA-Z0-9]*\Z")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}\Z")
ALLOWED_CHILD_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR")


class ShimError(Exception):
    """Typed protocol/availability error returned as JSON over HTTP."""

    def __init__(self, code: str, message: str, *, status: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class EngineStartupError(ShimError):
    """Raised when the wrapped engine fails during startup."""


def _parse_positive_float(raw: str | None, default: float, label: str) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a positive number.") from exc
    if not (value > 0 and value != float("inf")):
        raise ValueError(f"{label} must be a positive finite number.")
    return value


def _parse_positive_int(raw: str | None, default: int, label: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip(), 10)
    except ValueError as exc:
        raise ValueError(f"{label} must be a positive integer.") from exc
    if value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _parse_port(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip(), 10)
    except ValueError as exc:
        raise ValueError("Port must be an integer between 0 and 65535.") from exc
    if not (0 <= value <= 65535):
        raise ValueError("Port must be an integer between 0 and 65535.")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property.")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ValueError("Nonfinite JSON number.")


def parse_strict_json(raw: bytes) -> object:
    """Decode UTF-8 JSON while rejecting duplicate keys and nonfinite numbers."""
    text = raw.decode("utf-8", errors="strict")
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_nonfinite)


def encode_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_rpc_payload(payload: object) -> tuple[int | None, str, dict[str, object]]:
    """Validate an incoming POST /rpc JSON object and return (client_id, method, params)."""
    if type(payload) is not dict:
        raise ShimError("invalid_params", "The request body must be a JSON object.", status=400)

    raw_id = payload.get("id")
    valid_id: int | None = None
    if "id" in payload:
        if (
            type(raw_id) in (int, float)
            and 0 < raw_id <= MAX_SAFE_INTEGER
            and raw_id == int(raw_id)
        ):
            valid_id = int(raw_id)
        else:
            raise ShimError("invalid_params", "Invalid request id.", status=400)

    extra = set(payload) - {"id", "method", "params"}
    if extra:
        raise ShimError("invalid_params", "Unexpected request fields.", status=400)

    method = payload.get("method")
    if type(method) is not str or not METHOD_NAME_RE.fullmatch(method):
        raise ShimError("invalid_params", "Invalid engine method.", status=400)

    if "params" in payload:
        params = payload["params"]
        if type(params) is not dict:
            raise ShimError("invalid_params", "Method params must be a JSON object.", status=400)
    else:
        params = {}

    return valid_id, method, params


def _extract_valid_id(payload: object) -> int | None:
    if type(payload) is not dict or "id" not in payload:
        return None
    raw_id = payload.get("id")
    if (
        type(raw_id) in (int, float)
        and 0 < raw_id <= MAX_SAFE_INTEGER
        and raw_id == int(raw_id)
    ):
        return int(raw_id)
    return None


def build_child_env(collection_dir: Path, source_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal allowlisted environment confined to collection_dir and /tmp."""
    src = os.environ if source_env is None else source_env
    env: dict[str, str] = {}
    for key in ALLOWED_CHILD_ENV:
        value = src.get(key)
        if isinstance(value, str) and value:
            env[key] = value
    tmpdir = src.get("TMPDIR") or "/tmp"
    temp = src.get("TEMP") or tmpdir
    env["HOME"] = str(collection_dir)
    env["TMPDIR"] = tmpdir
    env["TEMP"] = temp
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


class _InFlightCall:
    def __init__(self, wire_id: int, *, internal: bool = False):
        self.wire_id = wire_id
        self.internal = internal
        self.done = threading.Event()
        self.reply: dict[str, object] | None = None
        self.error: ShimError | None = None


class EngineShim:
    """Owns one JSON-RPC engine child process and serializes RPC calls in FIFO order."""

    def __init__(
        self,
        collection_dir: str | Path,
        *,
        engine_cmd: list[str] | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_S,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_S,
        terminate_grace: float = DEFAULT_TERMINATE_GRACE_S,
        max_pending: int = DEFAULT_MAX_PENDING,
        stderr=None,
    ):
        self.collection_dir = Path(collection_dir).resolve()
        self.collection_path = self.collection_dir / "collection.anki2"
        self.engine_cmd = engine_cmd
        self.startup_timeout = startup_timeout
        self.rpc_timeout = rpc_timeout
        self.shutdown_grace = shutdown_grace
        self.terminate_grace = terminate_grace
        self.max_pending = max_pending
        self._stderr = stderr

        self._cond = threading.Condition()
        self._queue: deque[object] = deque()
        self._in_flight: _InFlightCall | None = None
        # An exclusive slot (e.g. a filesystem snapshot) is queue-ordered like
        # an RPC but runs no wire exchange, so it needs its own busy marker.
        self._exclusive = False
        self._next_wire_id = 1
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._ready = False
        self._stopping = False
        self._failed = False
        self._closed = False
        self._failure_reason: str | None = None
        self._engine_version: str | None = None
        self._latest_progress: dict[str, object] | None = None
        self._startup_error: ShimError | None = None

    def _build_command(self) -> list[str]:
        if self.engine_cmd is not None:
            return list(self.engine_cmd)
        return [
            sys.executable,
            "-I",
            "-B",
            str(ENGINE_LAUNCH),
            "--collection",
            str(self.collection_path),
        ]

    def start(self) -> None:
        """Spawn the engine process and verify readiness via engine.status."""
        with self._cond:
            if self._proc is not None:
                raise RuntimeError("EngineShim is already started.")
            if self._closed or self._stopping:
                raise EngineStartupError("engine_unavailable", "The review engine is closed.", status=503)

        self.collection_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cmd = self._build_command()
        env = build_child_env(self.collection_dir)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                cwd=str(self.collection_dir),
                env=env,
                shell=False,
                bufsize=0,
            )
        except OSError as exc:
            with self._cond:
                self._failed = True
                self._failure_reason = "spawn_error"
            raise EngineStartupError(
                "engine_unavailable",
                "The review engine could not start.",
                status=503,
            ) from exc

        with self._cond:
            self._proc = proc

        self._reader_thread = threading.Thread(
            target=self._read_stdout_loop,
            args=(proc,),
            name="srs-shim-stdout",
            daemon=True,
        )
        self._reader_thread.start()

        try:
            reply = self._send_internal_rpc("engine.status", {}, timeout=self.startup_timeout)
            if "error" in reply:
                err = reply["error"]
                code = err.get("code", "engine_unavailable") if isinstance(err, dict) else "engine_unavailable"
                raise EngineStartupError(
                    str(code),
                    "The review engine failed to initialize.",
                    status=409 if code == "conflict" else 503,
                )
            result = reply.get("result")
            if not isinstance(result, dict) or result.get("collectionOpen") is not True:
                raise EngineStartupError(
                    "engine_unavailable",
                    "The review engine did not open the collection.",
                    status=503,
                )
            with self._cond:
                self._engine_version = result.get("engineVersion") if isinstance(result.get("engineVersion"), str) else None
                self._ready = True
                self._cond.notify_all()
        except Exception as exc:
            with self._cond:
                self._failed = True
                self._ready = False
                self._cond.notify_all()
            self._terminate_process(proc)
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=1.0)
            if isinstance(exc, EngineStartupError):
                raise
            raise EngineStartupError(
                "engine_unavailable",
                "The review engine failed to start.",
                status=503,
            ) from exc

    def health(self) -> tuple[int, dict[str, object]]:
        """Return non-blocking HTTP status and payload for /healthz."""
        with self._cond:
            proc = self._proc
            stopping = self._stopping or self._closed
            failed = self._failed
            ready = self._ready
            version = self._engine_version

        alive = proc is not None and proc.poll() is None
        if stopping:
            return 503, {"ok": False, "status": "stopping"}
        if ready and not failed and alive:
            return 200, {
                "ok": True,
                "status": "ready",
                "collectionOpen": True,
                "engineVersion": version,
            }
        if not ready and not failed and proc is not None and alive:
            return 503, {"ok": False, "status": "starting"}
        return 503, {"ok": False, "status": "unhealthy"}

    def transfer_root(self) -> Path:
        """The collection directory an upload may stage into, while not stopping."""
        with self._cond:
            if self._closed or self._stopping:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
        self.collection_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.collection_dir

    def _send_internal_rpc(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float,
    ) -> dict[str, object]:
        with self._cond:
            if self._failed or self._closed or self._proc is None:
                if self._startup_error is not None:
                    raise self._startup_error
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            wire_id = self._next_wire_id
            self._next_wire_id += 1
            call_box = _InFlightCall(wire_id, internal=True)
            self._in_flight = call_box
            proc = self._proc

        try:
            self._write_frame(proc, {"id": wire_id, "method": method, "params": params})
            if not call_box.done.wait(timeout=timeout):
                self._fail_session("timeout")
                raise ShimError("engine_unavailable", "The review engine timed out.", status=503)
            if call_box.error is not None:
                raise call_box.error
            if call_box.reply is None:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            return call_box.reply
        finally:
            with self._cond:
                if self._in_flight is call_box:
                    self._in_flight = None
                self._cond.notify_all()

    def call(
        self,
        method: str,
        params: dict[str, object],
        *,
        client_id: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Serialize one RPC request through the engine in FIFO arrival order."""
        effective_timeout = self.rpc_timeout if timeout is None else timeout
        ticket = object()
        call_box: _InFlightCall | None = None
        proc: subprocess.Popen[bytes] | None = None

        with self._cond:
            if self._closed or self._stopping or self._failed or not self._ready:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            if len(self._queue) >= self.max_pending:
                raise ShimError("busy", "The engine request queue is full.", status=429)
            self._queue.append(ticket)
            try:
                while (
                    not self._closed
                    and not self._stopping
                    and not self._failed
                    and (
                        self._in_flight is not None
                        or self._exclusive
                        or self._queue[0] is not ticket
                    )
                ):
                    self._cond.wait()

                if self._closed or self._stopping or self._failed or not self._ready or self._proc is None:
                    raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)

                popped = self._queue.popleft()
                assert popped is ticket
                wire_id = self._next_wire_id
                self._next_wire_id += 1
                call_box = _InFlightCall(wire_id)
                self._in_flight = call_box
                proc = self._proc
            except Exception:
                try:
                    self._queue.remove(ticket)
                except ValueError:
                    pass
                self._cond.notify_all()
                raise

        try:
            self._write_frame(proc, {"id": wire_id, "method": method, "params": params})
            if not call_box.done.wait(timeout=effective_timeout):
                self._fail_session("timeout")
                raise ShimError("engine_unavailable", "The review engine request timed out.", status=503)
            if call_box.error is not None:
                raise call_box.error
            if call_box.reply is None:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            response = dict(call_box.reply)
            response["id"] = client_id if client_id is not None else wire_id
            return response
        finally:
            with self._cond:
                if self._in_flight is call_box:
                    self._in_flight = None
                self._cond.notify_all()

    def run_exclusive(self, procedure):
        """Run procedure() as one item in the same FIFO queue as RPC calls.

        Queued RPCs wait while the slot is held; the procedure must not send
        engine frames through call() (that would deadlock on its own slot).
        """
        ticket = object()
        with self._cond:
            if self._closed or self._stopping or self._failed or not self._ready:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            if len(self._queue) >= self.max_pending:
                raise ShimError("busy", "The engine request queue is full.", status=429)
            self._queue.append(ticket)
            try:
                while (
                    not self._closed
                    and not self._stopping
                    and not self._failed
                    and (
                        self._in_flight is not None
                        or self._exclusive
                        or self._queue[0] is not ticket
                    )
                ):
                    self._cond.wait()

                if self._closed or self._stopping or self._failed or not self._ready:
                    raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)

                popped = self._queue.popleft()
                assert popped is ticket
                self._exclusive = True
            except Exception:
                try:
                    self._queue.remove(ticket)
                except ValueError:
                    pass
                self._cond.notify_all()
                raise

        try:
            return procedure()
        finally:
            with self._cond:
                self._exclusive = False
                self._cond.notify_all()

    def call_in_exclusive(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Send one engine frame while this thread holds the exclusive slot.

        Only valid inside run_exclusive(): the queue position is already owned,
        so no ticket is taken and queued callers keep waiting. This lets a
        filesystem operation run as one indivisible queue item that includes
        its own engine RPC.
        """
        effective_timeout = self.rpc_timeout if timeout is None else timeout
        with self._cond:
            if (
                not self._exclusive
                or self._in_flight is not None
                or self._closed
                or self._stopping
                or self._failed
                or not self._ready
            ):
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            wire_id = self._next_wire_id
            self._next_wire_id += 1
            call_box = _InFlightCall(wire_id)
            self._in_flight = call_box
            proc = self._proc

        try:
            self._write_frame(proc, {"id": wire_id, "method": method, "params": params})
            if not call_box.done.wait(timeout=effective_timeout):
                self._fail_session("timeout")
                raise ShimError("engine_unavailable", "The review engine request timed out.", status=503)
            if call_box.error is not None:
                raise call_box.error
            if call_box.reply is None:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            return call_box.reply
        finally:
            with self._cond:
                if self._in_flight is call_box:
                    self._in_flight = None
                self._cond.notify_all()

    def _write_frame(self, proc: subprocess.Popen[bytes], payload: dict[str, object]) -> None:
        try:
            frame = encode_json_bytes(payload) + b"\n"
        except (ValueError, TypeError, RecursionError) as exc:
            raise ShimError("invalid_params", "Request payload is not JSON serializable.", status=400) from exc
        if len(frame) - 1 > MAX_REQUEST_BYTES:
            raise ShimError("invalid_params", "Engine request exceeds 4 MiB.", status=413)
        stdin = proc.stdin
        if stdin is None:
            self._fail_session("stdin_closed")
            raise ShimError("engine_unavailable", "The review engine input stream is closed.", status=503)
        try:
            view = memoryview(frame)
            offset = 0
            while offset < len(view):
                written = stdin.write(view[offset:])
                if written is None or written == 0:
                    raise BrokenPipeError("Engine stdin closed")
                offset += written
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._fail_session("stdin_error")
            raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503) from exc

    def _read_stdout_loop(self, proc: subprocess.Popen[bytes]) -> None:
        stdout = proc.stdout
        if stdout is None:
            self._fail_session("stdout_missing")
            return
        parts = bytearray()
        try:
            while True:
                chunk = stdout.read(64 * 1024)
                if not chunk:
                    if parts:
                        self._fail_session("truncated_frame")
                    else:
                        self._fail_session("stdout_closed")
                    return
                offset = 0
                while offset < len(chunk):
                    newline = chunk.find(b"\n", offset)
                    end = len(chunk) if newline < 0 else newline
                    segment_len = end - offset
                    if len(parts) + segment_len > MAX_RESPONSE_BYTES:
                        self._fail_session("frame_too_large")
                        return
                    if segment_len:
                        parts.extend(chunk[offset:end])
                    if newline < 0:
                        break
                    line = bytes(parts)
                    parts.clear()
                    self._handle_engine_line(line)
                    offset = newline + 1
        except (OSError, ValueError):
            self._fail_session("stdout_error")

    def _handle_engine_line(self, line: bytes) -> None:
        try:
            frame = parse_strict_json(line)
        except (UnicodeError, ValueError, RecursionError):
            self._fail_session("invalid_frame")
            return

        if type(frame) is not dict:
            self._fail_session("invalid_frame")
            return

        with self._cond:
            ready = self._ready
            in_flight = self._in_flight

            # Native startup diagnostic before request processing begins.
            if (
                not ready
                and frame.get("id") is None
                and set(frame) == {"id", "error"}
                and type(frame.get("error")) is dict
            ):
                err = frame["error"]
                code = err.get("code")
                msg = err.get("message")
                if isinstance(code, str) and isinstance(msg, str) and msg:
                    startup_err = EngineStartupError(
                        code,
                        "The collection is already in use." if code == "conflict" else "The review engine could not open the collection.",
                        status=409 if code == "conflict" else 503,
                    )
                    self._startup_error = startup_err
                    self._failed = True
                    self._failure_reason = "startup_error"
                    if in_flight is not None:
                        in_flight.error = startup_err
                        in_flight.done.set()
                    self._cond.notify_all()
                    return

            if frame.get("event") == "progress":
                if (
                    "result" in frame
                    or "error" in frame
                    or not isinstance(frame.get("operationId"), str)
                    or type(frame.get("progress")) is not dict
                ):
                    self._fail_session_locked("invalid_response")
                    return
                self._latest_progress = frame
                return

            if "event" in frame or (("result" in frame) == ("error" in frame)):
                self._fail_session_locked("invalid_response")
                return

            if in_flight is None or frame.get("id") != in_flight.wire_id:
                self._fail_session_locked("unexpected_id")
                return

            if "error" in frame:
                err = frame["error"]
                if type(err) is not dict or not isinstance(err.get("code"), str) or not isinstance(err.get("message"), str):
                    self._fail_session_locked("invalid_response")
                    return
                in_flight.reply = {"id": in_flight.wire_id, "error": err}
            else:
                in_flight.reply = {"id": in_flight.wire_id, "result": frame["result"]}
            in_flight.done.set()

    def _fail_session(self, reason: str) -> None:
        with self._cond:
            self._fail_session_locked(reason)

    def _fail_session_locked(self, reason: str) -> None:
        if self._failed:
            return
        self._failed = True
        self._ready = False
        if self._failure_reason is None:
            self._failure_reason = reason
        if self._in_flight is not None and not self._in_flight.done.is_set():
            if self._startup_error is not None and not self._ready:
                self._in_flight.error = self._startup_error
            else:
                self._in_flight.error = ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            self._in_flight.done.set()
        self._cond.notify_all()

    def _terminate_process(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=self.terminate_grace)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=self.terminate_grace)
                except subprocess.TimeoutExpired:
                    pass
        try:
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()
        except OSError:
            pass

    def mark_stopping(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()

    def shutdown(self) -> None:
        """Gracefully close the collection via engine.shutdown, escalating only if needed."""
        with self._cond:
            if self._closed:
                return
            self._stopping = True
            self._closed = True
            proc = self._proc
            self._cond.notify_all()

        if proc is None:
            return

        # Wait briefly if a single call is currently executing so engine.shutdown
        # runs against an idle slot and closes the collection cleanly.
        deadline = time.monotonic() + min(DEFAULT_INFLIGHT_SHUTDOWN_WAIT_S, self.shutdown_grace)
        with self._cond:
            while self._in_flight is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            can_send_shutdown = (
                self._in_flight is None
                and proc.poll() is None
                and proc.stdin is not None
                and not proc.stdin.closed
            )
            if can_send_shutdown:
                wire_id = self._next_wire_id
                self._next_wire_id += 1
                shutdown_call = _InFlightCall(wire_id, internal=True)
                self._in_flight = shutdown_call
            else:
                shutdown_call = None

        if shutdown_call is not None:
            try:
                self._write_frame(proc, {"id": shutdown_call.wire_id, "method": "engine.shutdown", "params": {}})
                shutdown_call.done.wait(timeout=self.shutdown_grace)
            except ShimError:
                pass
            finally:
                with self._cond:
                    if self._in_flight is shutdown_call:
                        self._in_flight = None
                    self._cond.notify_all()

        if proc.poll() is None:
            try:
                proc.wait(timeout=self.shutdown_grace)
            except subprocess.TimeoutExpired:
                self._terminate_process(proc)

        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()
        except OSError:
            pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        # Windows cannot open a directory for synchronization.
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _valid_media_name(name: str) -> bool:
    """Media names are single UTF-8 path components of at most 255 bytes."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if len(name.encode("utf-8")) > MEDIA_MAX_NAME_BYTES:
        return False
    for char in name:
        point = ord(char)
        if point < 32 or point == 127:
            return False
    return True


def _utf8_bytes(value: str) -> int | None:
    """UTF-8 length, or None when the text is not encodable (lone surrogates)."""
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return None


def _upload_limit() -> int:
    """Gateway uploadMaxBytes: 2 GiB unless MEMR_SRS_UPLOAD_MAX_BYTES lowers it."""
    try:
        configured = int(os.environ.get("MEMR_SRS_UPLOAD_MAX_BYTES", ""), 10)
    except ValueError:
        configured = MAX_UPLOAD_BYTES
    return min(MAX_UPLOAD_BYTES, max(1, configured))


def _upload_filename(value: object) -> str:
    """Gateway fileName(): one safe component; it only supplies the extension.

    The value never becomes a storage path: the upload lands under a generated
    uuid, exactly as the gateway does, so resolve_upload keeps working.
    """
    if type(value) is not str:
        raise ShimError("invalid_params", "The upload needs a filename.", status=400)
    size = _utf8_bytes(value)
    if (
        not value
        or size is None
        or size > 255
        or value.startswith(".")
        or ".." in value
        or UPLOAD_UNSAFE_CHARS_RE.search(value)
        or value.endswith(".")
        or value.endswith(" ")
        or RESERVED_NAME_RE.match(value)
    ):
        raise ShimError("invalid_params", "The upload filename is invalid.", status=400)
    return value


def _export_filename(value: object) -> str:
    """Gateway exportName(): the leaf name inside exports/<token>/."""
    if type(value) is not str:
        raise ShimError("invalid_params", "Invalid export name.", status=400)
    size = _utf8_bytes(value)
    if not value or size is None or size > 255 or value.startswith(".") or EXPORT_UNSAFE_CHARS_RE.search(value):
        raise ShimError("invalid_params", "Invalid export name.", status=400)
    return value


def _request_upload_filename(handler: BaseHTTPRequestHandler) -> str:
    """The declared upload name: ?filename= from the adapter, else X-Upload-Name."""
    names = parse_qs(urlsplit(handler.path).query, keep_blank_values=True).get("filename", [])
    if len(names) > 1:
        raise ShimError("invalid_params", "The upload names exactly one filename.", status=400)
    value = names[0] if names else handler.headers.get("x-upload-name")
    return _upload_filename(value)


def _require_octet_stream(handler: BaseHTTPRequestHandler) -> None:
    """Gateway contentType(): octet-stream body, identity or absent encoding."""
    media_type = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    encoding = handler.headers.get("Content-Encoding")
    if media_type != "application/octet-stream" or (
        encoding and encoding.strip().lower() not in ("", "identity")
    ):
        raise ShimError("invalid_params", "Uploads must be application/octet-stream.", status=415)


def _upload_content_length(handler: BaseHTTPRequestHandler, limit: int) -> int | None:
    """Gateway bodyLength(): absent (chunked) or a digit string within the cap."""
    values = handler.headers.get_all("Content-Length") or []
    if not values:
        return None
    if len(values) > 1 or not re.fullmatch(r"\d+", values[0].strip()):
        raise ShimError("invalid_params", "Invalid Content-Length header.", status=400)
    length = int(values[0].strip(), 10)
    if length > limit:
        raise ShimError("invalid_params", "Upload exceeds the size limit.", status=413)
    return length


def _ensure_transfer_directory(root: Path, name: str) -> Path:
    """Create <root>/<name> at 0700 without ever following a symlink."""
    directory = root / name
    if directory.is_symlink():
        raise ShimError("engine_unavailable", "The upload directory is unavailable.", status=503)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ShimError("engine_unavailable", "The upload directory is unavailable.", status=503) from exc
    if not directory.is_dir() or directory.resolve() != root.resolve() / name:
        raise ShimError("engine_unavailable", "The upload directory is unavailable.", status=503)
    return directory


def _require_upload_space(directory: Path, needed: int) -> None:
    """Gateway spaceAvailable(): refillable disk, with a reserve for the rest."""
    try:
        available = shutil.disk_usage(directory).free
    except OSError as exc:
        raise ShimError("engine_unavailable", "The upload directory is unavailable.", status=503) from exc
    if available < needed + UPLOAD_RESERVE_BYTES:
        raise ShimError("conflict", "There is not enough free storage for this upload.", status=507)


class DownloadRegistry:
    """One-claim export tokens served by GET /export/<token>.

    Mirrors the gateway's registerDownload()/download(): only a well-formed
    exportFile record registers, re-registration must describe the same file,
    the claim deadline is min(engine expiry, registration + 10 minutes), and a
    token is consumed before the first read so concurrent requests cannot both
    claim it. The shim has no user identity, so the only path it can ever
    serve is <collection>/exports/<token>/<filename> — the token is matched
    against a fixed alphabet and is never unquoted or joined as a path.
    """

    def __init__(self, exports_dir: str | Path, *, clock=time.time):
        self._exports_dir = Path(exports_dir)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, object]] = {}

    @staticmethod
    def _internal() -> ShimError:
        return ShimError("internal", "The export could not be registered.", status=500)

    def register(self, metadata: object) -> dict[str, object]:
        """Register one completed export and return it with its claim deadline."""
        if type(metadata) is not dict:
            raise self._internal()
        token = metadata.get("downloadToken")
        if type(token) is not str or not DOWNLOAD_TOKEN_RE.fullmatch(token):
            raise self._internal()
        try:
            filename = _export_filename(metadata.get("filename"))
        except ShimError:
            raise self._internal() from None
        if Path(filename).suffix.lower() not in EXPORT_EXTENSIONS:
            raise self._internal()
        size = metadata.get("bytes")
        # type() keeps bools out: a JSON true is not a byte count.
        if type(size) is not int or not 0 <= size <= MAX_SAFE_INTEGER:
            raise self._internal()
        engine_expiry = metadata.get("expiresAt")
        if type(engine_expiry) is not int or not 0 <= engine_expiry <= MAX_SAFE_INTEGER:
            raise self._internal()
        now_ms = self._clock() * 1000
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                if (
                    entry["filename"] != filename
                    or entry["bytes"] != size
                    or entry["engineExpiry"] != engine_expiry
                ):
                    raise self._internal()
            else:
                entry = {
                    "filename": filename,
                    "bytes": size,
                    "engineExpiry": engine_expiry,
                    "expiresAt": min(engine_expiry * 1000, now_ms + DOWNLOAD_TTL_MS),
                    "claimed": False,
                }
                self._entries[token] = entry
            expires_at = entry["expiresAt"]
        return {**metadata, "expiresAt": int(expires_at // 1000)}

    def claim(self, token: str) -> tuple[Path, int]:
        """Consume one token and return the exact published file, or 404."""
        not_found = ShimError("not_found", "Not found.", status=404)
        if not isinstance(token, str) or not DOWNLOAD_TOKEN_RE.fullmatch(token):
            raise not_found
        with self._lock:
            entry = self._entries.get(token)
            if entry is None or entry["claimed"] or self._clock() * 1000 >= entry["expiresAt"]:
                raise not_found
            entry["claimed"] = True
            filename = entry["filename"]
            size = entry["bytes"]
        folder = self._exports_dir / token
        path = folder / str(filename)
        try:
            folder_info = folder.lstat()
            file_info = path.lstat()
            contained = path.resolve().parent == folder.resolve()
        except OSError:
            raise not_found from None
        if (
            not stat.S_ISDIR(folder_info.st_mode)
            or not stat.S_ISREG(file_info.st_mode)
            or path.is_symlink()
            or not contained
            or file_info.st_size != size
        ):
            raise not_found
        return path, size


class CloudShim:
    """Cloud-mode shim: the engine opens only via a verified POST /restore.

    The container owns one collection directory on ephemeral storage. Until a
    snapshot body (or an empty body for a fresh collection) is installed and
    the engine is running, every engine-facing route reports a 409 conflict.
    """

    def __init__(
        self,
        collection_dir: str | Path,
        *,
        engine_cmd: list[str] | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_S,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_S,
        terminate_grace: float = DEFAULT_TERMINATE_GRACE_S,
        max_pending: int = DEFAULT_MAX_PENDING,
        stderr=None,
    ):
        self.collection_dir = Path(collection_dir).resolve()
        self.collection_path = self.collection_dir / "collection.anki2"
        self._kwargs = {
            "engine_cmd": engine_cmd,
            "startup_timeout": startup_timeout,
            "rpc_timeout": rpc_timeout,
            "shutdown_grace": shutdown_grace,
            "terminate_grace": terminate_grace,
            "max_pending": max_pending,
            "stderr": stderr,
        }
        self._cond = threading.Condition()
        self._shim: EngineShim | None = None
        self._restoring = False
        self._stopping = False
        self._closed = False

    def health(self) -> tuple[int, dict[str, object]]:
        with self._cond:
            stopping = self._stopping or self._closed
            shim = self._shim
        if stopping:
            return 503, {"ok": False, "status": "stopping"}
        if shim is None:
            return 200, {"ok": True, "status": "awaitingRestore", "collectionOpen": False}
        return shim.health()

    def _require_shim(self) -> EngineShim:
        with self._cond:
            if self._closed or self._stopping:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            shim = self._shim
        if shim is None:
            raise ShimError("conflict", "Restore a collection first.", status=409)
        return shim

    def transfer_root(self) -> Path:
        """Upload staging needs only the collection directory, not an open engine."""
        with self._cond:
            if self._closed or self._stopping:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
        self.collection_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.collection_dir

    def call(self, method: str, params: dict[str, object], *, client_id: int | None = None) -> dict[str, object]:
        return self._require_shim().call(method, params, client_id=client_id)

    def mark_stopping(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            shim = self._shim
        if shim is not None:
            shim.mark_stopping()

    def shutdown(self) -> None:
        with self._cond:
            self._stopping = True
            self._closed = True
            self._cond.notify_all()
            shim = self._shim
        if shim is not None:
            shim.shutdown()

    def restore_available(self) -> bool:
        """Cheap pre-check so POST /restore can refuse before staging a body."""
        with self._cond:
            if self._closed or self._stopping:
                return True
            return self._shim is None and not self._restoring and not self.collection_path.exists()

    def restore(self, incoming: Path, sha_header: str | None) -> dict[str, object]:
        """Install a verified snapshot body, open the engine, and report counts."""
        with self._cond:
            if self._closed or self._stopping:
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            if self._shim is not None or self._restoring or self.collection_path.exists():
                raise ShimError("conflict", "A collection is already open.", status=409)
            self._restoring = True
        try:
            return self._restore_locked(incoming, sha_header)
        finally:
            with self._cond:
                self._restoring = False

    def _restore_locked(self, incoming: Path, sha_header: str | None) -> dict[str, object]:
        self.collection_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fresh = incoming.stat().st_size == 0
        if sha_header is not None and not SHA256_RE.fullmatch(sha_header):
            raise ShimError("invalid_params", "The x-snapshot-sha256 header is missing or invalid.", status=400)
        if not fresh and sha_header is None:
            raise ShimError("invalid_params", "The x-snapshot-sha256 header is missing or invalid.", status=400)
        if sha_header is not None and _sha256_file(incoming) != sha_header.lower():
            raise ShimError("invalid_params", "The snapshot body does not match x-snapshot-sha256.", status=400)
        if not fresh:
            self._install_snapshot(incoming)
        shim = EngineShim(self.collection_dir, **self._kwargs)
        try:
            shim.start()
        except EngineStartupError:
            # A body that verified but cannot be opened is refused with no
            # partial collection left behind, so a corrected restore may run.
            try:
                self.collection_path.unlink()
            except OSError:
                pass
            if fresh:
                raise
            raise ShimError("invalid_params", "The snapshot could not be opened.", status=400) from None
        with self._cond:
            if self._closed or self._stopping:
                shim.shutdown()
                raise ShimError("engine_unavailable", "The review engine is unavailable.", status=503)
            self._shim = shim
        notes = shim.call("search.notes", {"search": ""})["result"]["noteIds"]
        cards = shim.call("search.cards", {"search": ""})["result"]["cardIds"]
        return {"ok": True, "notes": len(notes), "cards": len(cards)}

    def _install_snapshot(self, incoming: Path) -> None:
        """Decompress a verified zstd body into collection.anki2 atomically."""
        import zstandard

        partial = self.collection_dir / "collection.anki2.restore"
        total = 0
        try:
            decompressor = zstandard.ZstdDecompressor()
            with open(incoming, "rb") as source, open(partial, "wb") as target:
                with decompressor.stream_reader(source, read_across_frames=True) as reader:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_COLLECTION_BYTES:
                            raise ShimError("invalid_params", "The snapshot expands beyond the collection size limit.", status=400)
                        target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if total == 0:
                raise ShimError("invalid_params", "The snapshot body expands to nothing.", status=400)
            with open(partial, "rb") as check:
                if check.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                    raise ShimError("invalid_params", "The snapshot body is not an Anki collection.", status=400)
            os.replace(partial, self.collection_path)
            _fsync_directory(self.collection_dir)
        except zstandard.ZstdError:
            raise ShimError("invalid_params", "The snapshot body is not valid zstandard data.", status=400) from None
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass

    def _backup_signatures(self) -> dict[str, tuple[int, int, int, int]]:
        """Map backups/*.colpkg names to (ino, size, mtime_ns, ctime_ns)."""
        directory = self.collection_dir / "backups"
        result: dict[str, tuple[int, int, int, int]] = {}
        try:
            children = list(directory.iterdir())
        except OSError:
            return result
        for child in children:
            name = child.name
            if name.startswith(".") or not name.endswith(".colpkg"):
                continue
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not child.is_symlink():
                result[name] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return result

    def snapshot(self) -> tuple[Path, str, int]:
        """Queue a consistent copy of the open collection, then zstd-compress it.

        Anki's own maintenance.backupNow produces the copy inside the engine in
        the same indivisible FIFO slot as client RPCs, so the file reflects
        committed state at exactly this request's position and the collection
        stays usable throughout. The verified .colpkg is then unpacked and
        recompressed for streaming; hard-linking pins it against rotation.
        """
        shim = self._require_shim()
        backups_dir = self.collection_dir / "backups"
        staged_colpkg = self.collection_dir / "snapshot.colpkg.tmp"
        out_tmp = self.collection_dir / "snapshot.zst.tmp"
        out_path = self.collection_dir / "snapshot.zst"
        try:
            staged_colpkg.unlink(missing_ok=True)

            def procedure() -> None:
                before = self._backup_signatures()
                reply = shim.call_in_exclusive("maintenance.backupNow", {})
                if "error" in reply:
                    raise ShimError(
                        "engine_unavailable",
                        "The snapshot could not be produced.",
                        status=503,
                    )
                operation_id = reply["result"]["operationId"]
                # backupNow is a native long operation on a worker thread; poll
                # progress.get (a control method) until it reaches a terminal
                # state. The exclusive slot keeps every other RPC queued, so
                # no foreign backup can appear between it and the file pick.
                deadline = time.monotonic() + 4 * shim.rpc_timeout
                while True:
                    poll = shim.call_in_exclusive("progress.get", {"operationId": operation_id})
                    if "error" in poll:
                        raise ShimError(
                            "engine_unavailable",
                            "The snapshot could not be produced.",
                            status=503,
                        )
                    status = poll["result"]["status"]
                    if status == "completed":
                        break
                    if status in ("failed", "aborted"):
                        raise ShimError(
                            "engine_unavailable",
                            "The snapshot could not be produced.",
                            status=503,
                        )
                    if time.monotonic() > deadline:
                        raise ShimError(
                            "engine_unavailable",
                            "The snapshot timed out.",
                            status=503,
                        )
                    time.sleep(0.05)
                after = self._backup_signatures()
                created = [name for name in after if before.get(name) != after[name]]
                if created:
                    # Ours is the earliest new file; anything written later in
                    # this slot (e.g. a periodic backup) can only come after.
                    name = min(created, key=lambda item: (after[item][2], item))
                else:
                    # An unchanged collection re-verified the newest existing
                    # backup instead of writing one; that backup captures the
                    # same committed state at this request's position.
                    if not before:
                        raise ShimError(
                            "engine_unavailable",
                            "The snapshot could not be produced.",
                            status=503,
                        )
                    name = max(before, key=lambda item: (before[item][2], item))
                # Hard-link pins the inode while it is read, so backup rotation
                # cannot swap the bytes out from under the compressor.
                try:
                    os.link(backups_dir / name, staged_colpkg)
                except OSError as exc:
                    raise ShimError(
                        "engine_unavailable",
                        "The snapshot could not be produced.",
                        status=503,
                    ) from exc

            shim.run_exclusive(procedure)

            import zipfile

            with zipfile.ZipFile(staged_colpkg) as archive:
                # Anki's backup package keeps the live collection inside
                # collection.anki21b as a zstd frame; the plain .anki2 member
                # is a legacy template and must never be mistaken for data.
                try:
                    member = archive.open("collection.anki21b")
                except KeyError:
                    raise ShimError(
                        "engine_unavailable",
                        "The snapshot could not be produced.",
                        status=503,
                    ) from None
                with member:
                    first = member.read(len(ZSTD_MAGIC))
                    if first != ZSTD_MAGIC:
                        raise ShimError(
                            "engine_unavailable",
                            "The snapshot could not be produced.",
                            status=503,
                        )
                    with open(out_tmp, "wb") as packed:
                        packed.write(first)
                        while True:
                            chunk = member.read(1024 * 1024)
                            if not chunk:
                                break
                            packed.write(chunk)
                        packed.flush()
                        os.fsync(packed.fileno())
            os.replace(out_tmp, out_path)
            _fsync_directory(self.collection_dir)
            return out_path, _sha256_file(out_path), out_path.stat().st_size
        finally:
            for stale in (staged_colpkg, out_tmp):
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass

    def _media_dir(self) -> Path:
        return self.collection_dir / "collection.media"

    def media_index(self) -> dict[str, object]:
        self._require_shim()
        directory = self._media_dir()
        entries: list[dict[str, object]] = []
        if not directory.is_dir():
            return {"files": entries}
        for child in sorted(directory.iterdir(), key=lambda entry: entry.name):
            try:
                if not child.is_file() or child.is_symlink():
                    continue
                name = child.name
                name.encode("utf-8")
                entries.append({"name": name, "bytes": child.stat().st_size, "sha256": _sha256_file(child)})
            except (UnicodeEncodeError, OSError):
                # Unencodable names are unaddressable, and files may rotate
                # away while the index is being read.
                continue
        return {"files": entries}

    def media_file(self, raw_name: str) -> Path:
        self._require_shim()
        try:
            name = unquote(raw_name, errors="strict")
        except (UnicodeDecodeError, ValueError):
            raise ShimError("invalid_params", "Invalid media name.", status=400) from None
        if not _valid_media_name(name):
            raise ShimError("invalid_params", "Invalid media name.", status=400)
        path = self._media_dir() / name
        if not path.is_file() or path.is_symlink():
            raise ShimError("not_found", "Not found.", status=404)
        return path


def _read_chunked_body(rfile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        raw_line = rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
        if not raw_line or len(raw_line) > MAX_CHUNK_LINE_BYTES or not raw_line.endswith(b"\n"):
            raise ShimError("invalid_params", "Invalid chunked request framing.", status=400)
        size_token = raw_line.rstrip(b"\r\n").split(b";", 1)[0].strip()
        if not size_token:
            raise ShimError("invalid_params", "Invalid chunk size.", status=400)
        try:
            size = int(size_token, 16)
        except ValueError as exc:
            raise ShimError("invalid_params", "Invalid chunk size.", status=400) from exc
        if size < 0:
            raise ShimError("invalid_params", "Invalid chunk size.", status=400)
        if total + size > max_bytes:
            raise ShimError("invalid_params", "Request body exceeds 4 MiB.", status=413)
        if size == 0:
            # Drain optional trailer lines until blank line.
            while True:
                trailer = rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
                if not trailer or len(trailer) > MAX_CHUNK_LINE_BYTES:
                    raise ShimError("invalid_params", "Invalid chunked trailer.", status=400)
                if trailer in (b"\r\n", b"\n"):
                    break
            return b"".join(chunks)
        data = rfile.read(size)
        if len(data) != size:
            raise ShimError("invalid_params", "Truncated chunked request body.", status=400)
        crlf = rfile.read(2)
        if crlf != b"\r\n":
            raise ShimError("invalid_params", "Malformed chunk terminator.", status=400)
        chunks.append(data)
        total += size


def _read_request_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> bytes:
    encoding = handler.headers.get("Content-Encoding")
    if encoding is not None and encoding.strip().lower() not in ("", "identity"):
        raise ShimError("invalid_params", "Unsupported Content-Encoding.", status=415)

    ctype = handler.headers.get("Content-Type")
    if ctype is not None:
        media_type = ctype.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ShimError("invalid_params", "Content-Type must be application/json.", status=415)

    transfer_encodings = handler.headers.get_all("Transfer-Encoding") or []
    content_lengths = handler.headers.get_all("Content-Length") or []

    if transfer_encodings:
        if content_lengths:
            raise ShimError("invalid_params", "Conflicting Transfer-Encoding and Content-Length headers.", status=400)
        tokens = [part.strip().lower() for hdr in transfer_encodings for part in hdr.split(",") if part.strip()]
        if tokens != ["chunked"]:
            raise ShimError("invalid_params", "Unsupported Transfer-Encoding.", status=400)
        return _read_chunked_body(handler.rfile, max_bytes)

    if not content_lengths:
        raise ShimError("invalid_params", "Content-Length is required.", status=411)
    if len(content_lengths) > 1 or not re.fullmatch(r"\d+", content_lengths[0].strip()):
        raise ShimError("invalid_params", "Invalid Content-Length header.", status=400)

    length = int(content_lengths[0].strip(), 10)
    if length > max_bytes:
        raise ShimError("invalid_params", "Request body exceeds 4 MiB.", status=413)
    if length == 0:
        return b""
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ShimError("invalid_params", "Truncated request body.", status=400)
    return body


def _stream_chunked_body(rfile, sink, max_bytes: int, label: str) -> int:
    """Chunked variant of _read_chunked_body that streams into sink.write()."""
    total = 0
    while True:
        raw_line = rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
        if not raw_line or len(raw_line) > MAX_CHUNK_LINE_BYTES or not raw_line.endswith(b"\n"):
            raise ShimError("invalid_params", "Invalid chunked request framing.", status=400)
        size_token = raw_line.rstrip(b"\r\n").split(b";", 1)[0].strip()
        if not size_token:
            raise ShimError("invalid_params", "Invalid chunk size.", status=400)
        try:
            size = int(size_token, 16)
        except ValueError as exc:
            raise ShimError("invalid_params", "Invalid chunk size.", status=400) from exc
        if size < 0:
            raise ShimError("invalid_params", "Invalid chunk size.", status=400)
        if total + size > max_bytes:
            raise ShimError("invalid_params", label, status=413)
        if size == 0:
            while True:
                trailer = rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
                if not trailer or len(trailer) > MAX_CHUNK_LINE_BYTES:
                    raise ShimError("invalid_params", "Invalid chunked trailer.", status=400)
                if trailer in (b"\r\n", b"\n"):
                    break
            return total
        remaining = size
        while remaining:
            piece = rfile.read(min(remaining, 64 * 1024))
            if not piece:
                raise ShimError("invalid_params", "Truncated chunked request body.", status=400)
            sink.write(piece)
            remaining -= len(piece)
            total += len(piece)
        crlf = rfile.read(2)
        if crlf != b"\r\n":
            raise ShimError("invalid_params", "Malformed chunk terminator.", status=400)


def _stream_request_body(handler: BaseHTTPRequestHandler, sink, max_bytes: int, label: str) -> int:
    """Stream a non-JSON request body into sink.write(chunk); returns bytes."""
    encoding = handler.headers.get("Content-Encoding")
    if encoding is not None and encoding.strip().lower() not in ("", "identity"):
        raise ShimError("invalid_params", "Unsupported Content-Encoding.", status=415)

    transfer_encodings = handler.headers.get_all("Transfer-Encoding") or []
    content_lengths = handler.headers.get_all("Content-Length") or []

    if transfer_encodings:
        if content_lengths:
            raise ShimError("invalid_params", "Conflicting Transfer-Encoding and Content-Length headers.", status=400)
        tokens = [part.strip().lower() for hdr in transfer_encodings for part in hdr.split(",") if part.strip()]
        if tokens != ["chunked"]:
            raise ShimError("invalid_params", "Unsupported Transfer-Encoding.", status=400)
        return _stream_chunked_body(handler.rfile, sink, max_bytes, label)

    if not content_lengths:
        raise ShimError("invalid_params", "Content-Length is required.", status=411)
    if len(content_lengths) > 1 or not re.fullmatch(r"\d+", content_lengths[0].strip()):
        raise ShimError("invalid_params", "Invalid Content-Length header.", status=400)

    length = int(content_lengths[0].strip(), 10)
    if length > max_bytes:
        raise ShimError("invalid_params", label, status=413)
    remaining = length
    total = 0
    while remaining:
        piece = handler.rfile.read(min(remaining, 64 * 1024))
        if not piece:
            raise ShimError("invalid_params", "Truncated request body.", status=400)
        sink.write(piece)
        remaining -= len(piece)
        total += len(piece)
    return total


class ShimHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], shim: "EngineShim | CloudShim"):
        self.shim = shim
        # Export tokens are per shim process; the engine publishes each file
        # under <collection>/exports/<token>/ and this registry is the only
        # way a token becomes a path.
        self.downloads = DownloadRegistry(Path(shim.collection_dir) / "exports")
        super().__init__(server_address, _ShimRequestHandler)


class _ShimRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Never log request lines, headers, or card/collection paths.
        return

    def _send_json(
        self,
        status: int,
        payload: object,
        *,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
        close: bool = False,
    ) -> None:
        body = encode_json_bytes(payload)
        if close:
            self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if close:
                self.send_header("Connection", "close")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _stream_file(
        self,
        path: Path,
        size: int,
        *,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        try:
            handle = open(path, "rb")
        except OSError:
            # Nothing has been written yet, so the caller can still answer.
            self.close_connection = True
            return
        with handle:
            self._stream_handle(
                handle,
                size,
                extra_headers=extra_headers,
                head_only=head_only,
            )

    def _stream_handle(
        self,
        handle,
        size: int,
        *,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            if head_only:
                return
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _handle_upload(self) -> None:
        """Stream one octet-stream body into incoming/, gateway style."""
        shim = self.server.shim  # type: ignore[attr-defined]
        pending: Path | None = None
        try:
            _require_octet_stream(self)
            filename = _request_upload_filename(self)
            limit = _upload_limit()
            declared = _upload_content_length(self, limit)
            root = shim.transfer_root()
            directory = _ensure_transfer_directory(root, "incoming")
            suffix = Path(filename).suffix.lower()
            upload_id = str(uuid.uuid4()) + (suffix if UPLOAD_SUFFIX_RE.fullmatch(suffix) else "")
            pending = directory / (upload_id + ".upload")
            destination = directory / upload_id
            _require_upload_space(
                directory,
                declared if declared is not None else min(limit, UPLOAD_CHUNK_BYTES),
            )
            descriptor = os.open(
                pending,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as sink:
                written = _stream_request_body(
                    self,
                    sink,
                    limit,
                    "Upload exceeds the size limit.",
                )
                sink.flush()
                os.fsync(sink.fileno())
            # The gateway refuses a body that is shorter or longer than the
            # length it was admitted with, instead of publishing a partial file.
            if declared is not None and written != declared:
                raise ShimError(
                    "invalid_params",
                    "The upload body did not match its Content-Length.",
                    status=400,
                )
            os.replace(pending, destination)
            pending = None
            try:
                _fsync_directory(directory)
            except OSError as exc:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ShimError(
                    "engine_unavailable",
                    "The upload could not be published.",
                    status=503,
                ) from exc
            self._send_json(HTTPStatus.OK, {"uploadId": upload_id, "bytes": written})
        except ShimError as exc:
            if pending is not None:
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    pass
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                close=exc.status in (
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.LENGTH_REQUIRED,
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    HTTPStatus.INSUFFICIENT_STORAGE,
                ),
            )
        except OSError as exc:
            if pending is not None:
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    pass
            if exc.errno == errno.ENOSPC:
                self._send_json(
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    {"error": {"code": "conflict", "message": "There is not enough free storage for this upload."}},
                    close=True,
                )
            else:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": {"code": "engine_unavailable", "message": "The shim could not store the upload."}},
                    close=True,
                )

    def _handle_export(self, raw_token: str) -> None:
        """Serve the one registered file for a token; unknown means 404."""
        registry = self.server.downloads  # type: ignore[attr-defined]
        try:
            path, size = registry.claim(raw_token)
        except ShimError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        self._stream_file(path, size)

    def _register_export(self, reply: dict[str, object]) -> dict[str, object]:
        """Mirror relayProgress(): a completed progress result.file is claimable."""
        outcome = reply.get("result")
        if type(outcome) is not dict:
            return reply
        result = outcome.get("result")
        if type(result) is not dict or result.get("file") is None:
            return reply
        if outcome.get("status") != "completed":
            raise ShimError("internal", "The export could not be registered.", status=500)
        file = self.server.downloads.register(result["file"])  # type: ignore[attr-defined]
        return {**reply, "result": {**outcome, "result": {**result, "file": file}}}

    def _handle_snapshot(self) -> None:
        shim = self.server.shim  # type: ignore[attr-defined]
        snapshot_fn = getattr(shim, "snapshot", None)
        if snapshot_fn is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Not found."}},
            )
            return
        path: Path | None = None
        try:
            path, digest, size = snapshot_fn()
        except ShimError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"code": "engine_unavailable", "message": "The snapshot could not be produced."}},
            )
            return
        try:
            self._stream_file(
                path,
                size,
                extra_headers={
                    "x-snapshot-sha256": digest,
                    "x-snapshot-bytes": str(size),
                },
            )
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _handle_media_index(self) -> None:
        shim = self.server.shim  # type: ignore[attr-defined]
        index_fn = getattr(shim, "media_index", None)
        if index_fn is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Not found."}},
            )
            return
        try:
            payload = index_fn()
        except ShimError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"code": "engine_unavailable", "message": "The media index could not be read."}},
            )
            return
        self._send_json(HTTPStatus.OK, payload)

    def _handle_media_file(self, raw_name: str, *, head_only: bool = False) -> None:
        shim = self.server.shim  # type: ignore[attr-defined]
        media_fn = getattr(shim, "media_file", None)
        if media_fn is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Not found."}},
                head_only=head_only,
            )
            return
        try:
            path = media_fn(raw_name)
            size = path.stat().st_size
        except ShimError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, head_only=head_only)
            return
        except OSError:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Not found."}},
                head_only=head_only,
            )
            return
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"code": "engine_unavailable", "message": "The media file could not be read."}},
                head_only=head_only,
            )
            return
        self._stream_file(path, size, head_only=head_only)

    def _handle_healthz(self, *, head_only: bool = False) -> None:
        status, payload = self.server.shim.health()  # type: ignore[attr-defined]
        self._send_json(status, payload, head_only=head_only)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._handle_healthz(head_only=False)
            return
        if path == "/rpc" or path == "/restore" or path == "/upload":
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": f"Use POST for {path}."}},
                extra_headers={"Allow": "POST"},
            )
            return
        if path == "/snapshot":
            self._handle_snapshot()
            return
        if path == "/media-index":
            self._handle_media_index()
            return
        if path.startswith("/media/"):
            self._handle_media_file(path[len("/media/"):], head_only=False)
            return
        if path.startswith("/export/"):
            self._handle_export(path[len("/export/"):])
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": {"code": "not_found", "message": "Not found."}},
        )

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._handle_healthz(head_only=True)
            return
        if path == "/rpc" or path == "/restore" or path == "/upload":
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": f"Use POST for {path}."}},
                extra_headers={"Allow": "POST"},
                head_only=True,
            )
            return
        if path.startswith("/media/"):
            self._handle_media_file(path[len("/media/"):], head_only=True)
            return
        if path == "/export" or path.startswith("/export/"):
            # HEAD must not consume a one-claim token.
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": "Use GET for /export/<token>."}},
                extra_headers={"Allow": "GET"},
                head_only=True,
            )
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": {"code": "not_found", "message": "Not found."}},
            head_only=True,
        )

    def _handle_restore(self) -> None:
        shim = self.server.shim  # type: ignore[attr-defined]
        restore_fn = getattr(shim, "restore", None)
        if restore_fn is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Not found."}},
            )
            return
        # Refuse before staging a body that cannot possibly be installed; the
        # unread body is then dropped with the connection.
        available_fn = getattr(shim, "restore_available", None)
        if available_fn is not None and not available_fn():
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": {"code": "conflict", "message": "A collection is already open."}},
                close=True,
            )
            return
        incoming_path: Path | None = None
        try:
            shim.collection_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                dir=shim.collection_dir,
                prefix="restore-incoming-",
            )
            incoming_path = Path(name)
            with os.fdopen(descriptor, "wb") as sink:
                _stream_request_body(
                    self,
                    sink,
                    MAX_RESTORE_BYTES,
                    "Request body exceeds the snapshot size limit.",
                )
        except ShimError as exc:
            if incoming_path is not None:
                try:
                    incoming_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                close=exc.status in (400, 411, 413, 415),
            )
            return
        except OSError:
            if incoming_path is not None:
                try:
                    incoming_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"code": "engine_unavailable", "message": "The shim could not stage the snapshot body."}},
            )
            return

        try:
            result = restore_fn(
                incoming_path,
                self.headers.get("x-snapshot-sha256"),
            )
        except ShimError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
            )
            return
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"code": "engine_unavailable", "message": "The snapshot could not be installed."}},
            )
            return
        finally:
            if incoming_path is not None:
                try:
                    incoming_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._send_json(HTTPStatus.OK, result)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": "Use GET or HEAD for /healthz."}},
                extra_headers={"Allow": "GET, HEAD"},
            )
            return
        if (
            path == "/snapshot"
            or path == "/media-index"
            or path == "/media"
            or path.startswith("/media/")
            or path == "/export"
            or path.startswith("/export/")
        ):
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": f"Use GET for {path}."}},
                extra_headers={"Allow": "GET"},
            )
            return
        if path == "/restore":
            self._handle_restore()
            return
        if path == "/upload":
            self._handle_upload()
            return
        if path != "/rpc":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Not found."}},
            )
            return

        client_id: int | None = None
        try:
            raw = _read_request_body(self, MAX_REQUEST_BYTES)
        except ShimError as exc:
            self._send_json(
                exc.status,
                {"id": None, "error": {"code": exc.code, "message": exc.message}},
                close=exc.status in (400, 411, 413, 415),
            )
            return

        try:
            payload = parse_strict_json(raw)
        except (UnicodeError, ValueError, RecursionError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"id": None, "error": {"code": "invalid_params", "message": "Invalid JSON request."}},
            )
            return

        client_id = _extract_valid_id(payload)
        try:
            client_id, method, params = validate_rpc_payload(payload)
        except ShimError as exc:
            self._send_json(
                exc.status,
                {"id": client_id, "error": {"code": exc.code, "message": exc.message}},
            )
            return

        if method == "engine.shutdown":
            self._send_json(
                HTTPStatus.OK,
                {
                    "id": client_id,
                    "error": {"code": "unsupported", "message": "Unknown engine method."},
                },
            )
            return

        try:
            reply = self.server.shim.call(method, params, client_id=client_id)  # type: ignore[attr-defined]
        except ShimError as exc:
            self._send_json(
                exc.status,
                {"id": client_id, "error": {"code": exc.code, "message": exc.message}},
            )
            return

        if method == "progress.get":
            try:
                reply = self._register_export(reply)
            except ShimError as exc:
                self._send_json(
                    exc.status,
                    {"id": client_id, "error": {"code": exc.code, "message": exc.message}},
                )
                return

        self._send_json(HTTPStatus.OK, reply)

    def _method_not_allowed(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": "Method not allowed."}},
                extra_headers={"Allow": "GET, HEAD"},
            )
        elif path == "/rpc" or path == "/restore" or path == "/upload":
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": "Method not allowed."}},
                extra_headers={"Allow": "POST"},
            )
        elif (
            path == "/snapshot"
            or path == "/media-index"
            or path == "/media"
            or path.startswith("/media/")
            or path == "/export"
            or path.startswith("/export/")
        ):
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "invalid_params", "message": "Method not allowed."}},
                extra_headers={"Allow": "GET"},
            )
        else:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Not found."}},
            )

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def resolve_config(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Memr SRS engine HTTP shim")
    parser.add_argument("--collection-dir", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None)
    args = parser.parse_args(argv)

    collection_dir = (
        args.collection_dir
        if args.collection_dir is not None
        else (os.environ.get("MEMR_SRS_DIR") or os.environ.get("COLLECTION_DIR") or "")
    ).strip()
    if not collection_dir:
        raise ValueError("Collection directory must be set via MEMR_SRS_DIR or COLLECTION_DIR.")

    host = (
        args.host
        if args.host is not None
        else (os.environ.get("MEMR_SRS_HOST") or os.environ.get("HOST") or DEFAULT_HOST)
    ).strip() or DEFAULT_HOST
    raw_port = args.port if args.port is not None else (os.environ.get("MEMR_SRS_PORT") or os.environ.get("PORT"))
    port = _parse_port(raw_port, DEFAULT_PORT)

    return argparse.Namespace(
        collection_dir=Path(collection_dir),
        host=host,
        port=port,
        startup_timeout=_parse_positive_float(
            os.environ.get("MEMR_SRS_STARTUP_TIMEOUT_S"),
            DEFAULT_STARTUP_TIMEOUT_S,
            "MEMR_SRS_STARTUP_TIMEOUT_S",
        ),
        rpc_timeout=_parse_positive_float(
            os.environ.get("MEMR_SRS_RPC_TIMEOUT_S"),
            DEFAULT_RPC_TIMEOUT_S,
            "MEMR_SRS_RPC_TIMEOUT_S",
        ),
        shutdown_grace=_parse_positive_float(
            os.environ.get("MEMR_SRS_SHUTDOWN_GRACE_S"),
            DEFAULT_SHUTDOWN_GRACE_S,
            "MEMR_SRS_SHUTDOWN_GRACE_S",
        ),
        terminate_grace=_parse_positive_float(
            os.environ.get("MEMR_SRS_TERMINATE_GRACE_S"),
            DEFAULT_TERMINATE_GRACE_S,
            "MEMR_SRS_TERMINATE_GRACE_S",
        ),
        max_pending=_parse_positive_int(
            os.environ.get("MEMR_SRS_MAX_PENDING"),
            DEFAULT_MAX_PENDING,
            "MEMR_SRS_MAX_PENDING",
        ),
        require_restore=(
            os.environ.get("MEMR_SRS_REQUIRE_RESTORE") or ""
        ).strip().lower() in ("1", "true", "yes", "on"),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = resolve_config(argv)
    except ValueError as exc:
        sys.stderr.write(f"srs-http-shim configuration error: {exc}\n")
        sys.stderr.flush()
        return 2

    stop_event = threading.Event()

    def _on_signal(_signum: int, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    shim: EngineShim | CloudShim
    if cfg.require_restore:
        if (cfg.collection_dir / "collection.anki2").exists():
            # Cloud mode opens a collection only through POST /restore, so a
            # pre-existing file is never silently adopted or removed.
            sys.stderr.write("srs-http-shim startup failed: existing_collection\n")
            sys.stderr.flush()
            return 1
        shim = CloudShim(
            cfg.collection_dir,
            startup_timeout=cfg.startup_timeout,
            rpc_timeout=cfg.rpc_timeout,
            shutdown_grace=cfg.shutdown_grace,
            terminate_grace=cfg.terminate_grace,
            max_pending=cfg.max_pending,
        )
    else:
        shim = EngineShim(
            cfg.collection_dir,
            startup_timeout=cfg.startup_timeout,
            rpc_timeout=cfg.rpc_timeout,
            shutdown_grace=cfg.shutdown_grace,
            terminate_grace=cfg.terminate_grace,
            max_pending=cfg.max_pending,
        )
    server: ShimHTTPServer | None = None
    server_thread: threading.Thread | None = None
    try:
        if not cfg.require_restore:
            shim.start()
        server = ShimHTTPServer((cfg.host, cfg.port), shim)
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="srs-http-server",
            daemon=True,
        )
        server_thread.start()
        while not stop_event.wait(timeout=0.5):
            pass
        return 0
    except EngineStartupError as exc:
        sys.stderr.write(f"srs-http-shim startup failed: {exc.code}\n")
        sys.stderr.flush()
        return 1
    except OSError as exc:
        sys.stderr.write(f"srs-http-shim bind failed: {errno.errorcode.get(exc.errno, 'OS_ERROR')}\n")
        sys.stderr.flush()
        return 1
    finally:
        shim.mark_stopping()
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2.0)
        shim.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
