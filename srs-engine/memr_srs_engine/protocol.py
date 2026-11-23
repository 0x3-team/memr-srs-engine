"""Bounded JSON lines on stdin/stdout, with a responsive control lane."""
import importlib.metadata
import json
import queue
import threading
import time
import uuid

from .errors import EngineError, public_error
from .progress import LongOperations, POLL_SECONDS
from .registry import METHOD_NAME
from .validate import MAX_SAFE_INTEGER, validate_value

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
CONTROL_METHODS = {"progress.get", "progress.abort", "sync.abort"}
EOF = object()
STARTUP_HOOKS = []
IDLE_HOOKS = []


def startup_hook(callback):
    STARTUP_HOOKS.append(callback)
    return callback


def idle_hook(interval_seconds):
    def register(callback):
        IDLE_HOOKS.append((interval_seconds, callback))
        return callback
    return register


def initialize_scheduling(col):
    """Initialize native day defaults and refresh its derived timezone offset.

    This preserves the chosen rollover and keeps first-request reads from
    becoming the collection's scheduling initializer.
    """
    return col.sched.today


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("Nonfinite JSON number")


def parse_request(frame):
    try:
        value = json.loads(frame, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise EngineError("invalid_params", "Invalid JSON request.") from None
    if type(value) is not dict:
        raise EngineError("invalid_params", "The request must be an object.")
    request_id = value.get("id")
    valid_id = type(request_id) in (int, float) and 0 < request_id <= MAX_SAFE_INTEGER and request_id == int(request_id)
    if not valid_id or set(value) - {"id", "method", "params"}:
        raise EngineError("invalid_params", "Invalid request envelope.")
    return value


class FrameWriter:
    def __init__(self, stream, max_bytes=MAX_RESPONSE_BYTES):
        self.stream = stream
        self.max_bytes = max_bytes

    def encode(self, frame):
        try:
            data = json.dumps(frame, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise EngineError("internal", "The engine returned an invalid JSON result.") from None
        if len(data) > self.max_bytes:
            raise EngineError("internal", "The engine response exceeds 32 MiB.")
        return data + b"\n"

    def write(self, frame):
        data = self.encode(frame)
        remaining = memoryview(data)
        while remaining:
            count = self.stream.write(remaining)
            if not count:
                raise BrokenPipeError("The protocol output stream closed")
            remaining = remaining[count:]
        self.stream.flush()


def read_frames(stream, inbox, stop):
    """Bound memory even for unbounded lines and producers faster than handlers."""
    def put(value):
        while not stop.is_set():
            try:
                inbox.put(value, timeout=POLL_SECONDS)
                return
            except queue.Full:
                pass

    try:
        parts = bytearray()
        oversized = False
        while not stop.is_set():
            chunk = stream.read(64 * 1024)
            if not chunk:
                if parts:
                    put(EngineError("invalid_params", "The request frame is truncated."))
                break
            offset = 0
            while offset < len(chunk) and not stop.is_set():
                newline = chunk.find(b"\n", offset)
                end = len(chunk) if newline < 0 else newline
                if not oversized:
                    if len(parts) + end - offset > MAX_REQUEST_BYTES:
                        put(EngineError("invalid_params", "The request exceeds 4 MiB."))
                        parts.clear()
                        oversized = True
                    else:
                        parts.extend(chunk[offset:end])
                if newline < 0:
                    break
                if not oversized:
                    put(bytes(parts))
                parts.clear()
                oversized = False
                offset = newline + 1
    except (OSError, ValueError):
        put(EngineError("engine_unavailable", "The input stream is unavailable."))
    finally:
        put(EOF)


class Engine:
    def __init__(self, col, registry, writer, logger, clock=time.monotonic):
        self.col = col
        self.registry = registry
        self.writer = writer
        self.logger = logger
        self.clock = clock
        # Cursor readers must notice same-millisecond and skip-undo mutations.
        # This process-local generation also invalidates conservatively failed
        # admitted writes; collection modification timestamps are insufficient.
        self.collection_generation = 0
        self._undo_session = uuid.uuid4().hex
        self._undo_revision = 0
        self.stop = threading.Event()
        self.version = importlib.metadata.version("anki")
        self.progress = LongOperations(self)
        self._idle_hooks = [(interval, callback, self.clock() + interval)
                            for interval, callback in IDLE_HOOKS]
        initialize_scheduling(self.col)
        for callback in STARTUP_HOOKS:
            self._run_hook(callback)

    def undo_revision(self):
        # Native steps can be merged, and their counter restarts after reopen.
        # The session and successful-write counter keep targeted Undo distinct.
        return f"{self._undo_session}:{self._undo_revision}:{self.col.undo_status().last_step}"

    def _run_hook(self, callback):
        try:
            callback(self)
        except Exception:
            # Hooks have no response channel; avoid logging backend diagnostics
            # that could include collection text or filesystem paths.
            self.logger.error("Engine lifecycle hook failed")

    def run_idle_hooks(self):
        if self.progress.running or self.stop.is_set():
            return
        now = self.clock()
        for index, (interval, callback, due) in enumerate(self._idle_hooks):
            if now >= due:
                self._idle_hooks[index] = (interval, callback, now + interval)
                self._run_hook(callback)

    def dispatch(self, frame):
        request_id = None
        try:
            if isinstance(frame, EngineError):
                raise frame
            request = parse_request(frame)
            request_id = request["id"]
            name, params = request.get("method"), request.get("params", {})
            if type(name) is not str or not METHOD_NAME.fullmatch(name):
                raise EngineError("invalid_params", "Invalid method name.")
            # shutdown is a private process-lifecycle command, not a public RPC.
            if name == "engine.shutdown":
                if params != {} or type(params) is not dict:
                    raise EngineError("invalid_params", "Shutdown takes no parameters.")
                self.progress.close()
                warmup_state = getattr(self, "_collection_warmup", None)
                if warmup_state is not None:
                    warmup_state.cancel()
                self.col.close()
                if warmup_state is not None:
                    # SQLite has released its record locks; the pass's
                    # descriptors may close without stripping them.
                    warmup_state.close()
                self.writer.write({"id": request_id, "result": {}})
                self.stop.set()
                return
            if self.progress.running and name not in CONTROL_METHODS:
                raise EngineError("busy", "The engine is running a long operation.")
            entry = self.registry.catalogue.get(name)
            if entry is None:
                raise EngineError("unsupported", "Unknown engine method.")
            issues = validate_value(entry["params"], params)
            if issues:
                raise EngineError("invalid_params", "Invalid method parameters.", {"field": issues[0][:1024]})
            handler = self.registry.handlers.get(name)
            if handler is None:
                raise EngineError("unsupported", "The method is not implemented by this engine.")
            if entry["mutating"]:
                self.collection_generation += 1
                if entry["longOperation"]:
                    # Reserve the admission revision before a fast worker can
                    # finish and bind its terminal receipt to that revision.
                    self._undo_revision += 1
            result = self.progress.start(request_id, name, handler, params) if entry["longOperation"] else handler(self, params)
            if entry["mutating"] and not entry["longOperation"]:
                self._undo_revision += 1
            if name == "study.undoStatus":
                result = {**result, "revision": self.undo_revision()}
            elif entry["mutating"] and not entry["longOperation"] and name not in CONTROL_METHODS:
                # Control calls can run alongside the native worker; they must
                # not inspect its collection. A long-operation worker captures
                # its terminal token while it still owns the collection.
                result = {**result, "undo": {**result["undo"], "revision": self.undo_revision()}}
            if validate_value(entry["result"], result):
                raise EngineError("internal", "The handler returned an invalid result.")
            self.writer.write({"id": request_id, "result": result})
        except Exception as exception:
            # Backend exception messages/tracebacks can embed card fields or
            # credentials. Keep only the app-authored event in diagnostics.
            self.logger.error("Engine request failed")
            self.writer.write({"id": request_id, "error": public_error(exception).as_dict()})

    def run(self, stream):
        inbox = queue.Queue(maxsize=4)
        reader = threading.Thread(target=read_frames, args=(stream, inbox, self.stop),
                                  name="srs-input", daemon=True)
        reader.start()
        try:
            while not self.stop.is_set():
                try:
                    frame = inbox.get(timeout=self.progress.wait_seconds())
                except queue.Empty:
                    frame = None
                if frame is EOF:
                    break
                if frame is not None:
                    self.dispatch(frame)
                self.progress.tick()
                self.run_idle_hooks()
        finally:
            self.stop.set()
            self.progress.close()
