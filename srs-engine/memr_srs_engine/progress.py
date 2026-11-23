"""One background collection operation; only backend progress/abort run beside it."""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import threading
import uuid

from .collection import mutation_result, undo_state
from .errors import EngineError, public_error
from .validate import MAX_SAFE_INTEGER, validate_value

POLL_SECONDS = 0.250


def idle_progress():
    return {"operationId": None, "kind": None, "status": "idle", "done": 0,
            "total": None, "label": "", "abortable": False, "result": None, "error": None}


def backend_progress(value):
    """Normalize the pinned Progress protobuf without exposing protobuf types."""
    kind = value.WhichOneof("value")
    if kind in (None, "none"):
        return {"done": 0, "total": None, "label": ""}
    detail = getattr(value, kind)
    if isinstance(detail, str):
        return {"done": 0, "total": None, "label": detail[:4096]}
    for current, total in (
        ("transferred", "total"), ("stage_current", "stage_total"),
        ("current", "total"), ("current_cards", "total_cards"),
        ("downloaded_bytes", "total_bytes"),
    ):
        if hasattr(detail, current):
            return {"done": int(getattr(detail, current)), "total": int(getattr(detail, total)),
                    "label": str(getattr(detail, "label", getattr(detail, "stage", "")))[:4096]}
    # Media/normal sync progress is already localized text, not numeric counts.
    label = " · ".join(str(getattr(detail, field)) for field in ("stage", "checked", "added", "removed")
                       if hasattr(detail, field) and getattr(detail, field))
    return {"done": 0, "total": None, "label": label[:4096]}


class LongOperations:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.operations = OrderedDict()
        self.current = None
        self.thread = None
        self.request_id = None
        self.next_event = 0
        self.terminal_emitted = True
        self.undo = None
        self._abort_requested = False
        # Lifetime lock for the backend used by control calls. A handler may
        # temporarily inspect a different collection, or do Python-only work.
        self.backend_lock = threading.RLock()
        self.backend = engine.col
        self.manual_progress = None

    @property
    def abort_requested(self):
        with self.lock:
            return self._abort_requested

    def check_abort(self):
        if self.abort_requested:
            raise EngineError("aborted", "The operation was aborted.")

    @contextmanager
    def using_backend(self, backend):
        """Route progress/abort to live native work; None denotes a Python phase.

        Leave the context before closing a temporary collection. Only Anki's
        thread-safe progress and abort APIs run beside the operation thread.
        """
        previous = None
        try:
            with self.backend_lock:
                previous = self.backend, self.manual_progress
                self.backend, self.manual_progress = backend, None
                if backend is not None and self.abort_requested:
                    backend.set_wants_abort()
            self.check_abort()
            yield
        finally:
            if previous is not None:
                with self.backend_lock:
                    self.backend, self.manual_progress = previous

    def report(self, done=0, total=None, label=""):
        """Publish bounded counters for a handler's Python-only phase."""
        if (type(done) is not int or not 0 <= done <= MAX_SAFE_INTEGER
                or (total is not None and (type(total) is not int or not done <= total <= MAX_SAFE_INTEGER))
                or type(label) is not str or len(label) > 4096):
            raise EngineError("internal", "The operation returned invalid progress.")
        with self.backend_lock:
            self.manual_progress = {"done": done, "total": total, "label": label}

    @property
    def running(self):
        with self.lock:
            return self.current is not None and self.operations[self.current]["status"] == "running"

    def wait_seconds(self):
        with self.lock:
            if self.current is not None and self.operations[self.current]["status"] == "running":
                return max(0, min(POLL_SECONDS, self.next_event - self.engine.clock()))
            return POLL_SECONDS

    def start(self, request_id, name, handler, params):
        if self.running:
            raise EngineError("busy", "The engine is running a long operation.")
        if self.thread is not None:
            self.thread.join()
        # The next request can arrive after completion but before the next
        # main-loop tick. Emit its terminal frame with the previous request id.
        self.tick()
        self.undo = undo_state(self.engine.col)
        with self.backend_lock:
            self.backend, self.manual_progress = self.engine.col, None
        operation_id = uuid.uuid4().hex
        with self.lock:
            self._abort_requested = False
            self.current = operation_id
            self.request_id = request_id
            self.operations[operation_id] = {**idle_progress(), "operationId": operation_id,
                                           "kind": name, "status": "running", "abortable": True}
            while len(self.operations) > 8:
                self.operations.popitem(last=False)
            self.next_event = self.engine.clock() + POLL_SECONDS
            self.terminal_emitted = False
        self.thread = threading.Thread(target=self._run, args=(operation_id, name, handler, params),
                                       name="srs-long-operation")
        self.thread.start()
        return {"operationId": operation_id, **mutation_result(self.undo)}

    def _run(self, operation_id, name, handler, params):
        try:
            result = handler(self.engine, params)
            schema = self.engine.registry.catalogue["progress.get"]["result"]["properties"]["result"]
            if result is None or validate_value(schema, result):
                raise EngineError("internal", "The long operation returned an invalid result.")
            if self.engine.registry.catalogue[name]["mutating"]:
                # Capture this operation's identity before marking it complete.
                # Reading undoStatus later could adopt another client's write.
                result = {**result, "undo": {**result["undo"], "revision": self.engine.undo_revision()}}
            # Validate serialized size here as well: terminal polling must never
            # turn an otherwise completed operation into a transport failure.
            with self.lock:
                completed = {**self.operations[operation_id], "status": "completed", "result": result, "abortable": False}
            self.engine.writer.encode({"event": "progress", "id": MAX_SAFE_INTEGER,
                                       "operationId": operation_id, "progress": completed})
            status, error = "completed", None
        except Exception as exception:
            self.engine.logger.error("Long operation failed")
            error = public_error(exception).as_dict()
            status, result = ("aborted" if error["code"] == "aborted" else "failed"), None
        with self.lock:
            self.operations[operation_id].update(status=status, result=result, error=error, abortable=False)

    def get(self, operation_id=None):
        with self.lock:
            key = self.current if operation_id is None else operation_id
            if key is None:
                return idle_progress()
            if key not in self.operations:
                raise EngineError("not_found", "The operation is unavailable.")
            return deepcopy(self.operations[key])

    def abort(self, operation_id):
        with self.lock:
            operation = self.operations.get(operation_id)
            if operation is None:
                raise EngineError("not_found", "The operation is unavailable.")
            if operation["status"] == "running":
                self._abort_requested = True
                running = True
            else:
                running = False
        if running:
            # Do not hold the state lock over native calls. The lifetime lock
            # keeps a disposable backend attached until the call returns.
            with self.backend_lock:
                if self.backend is not None:
                    self.backend.set_wants_abort()
        return mutation_result(self.undo)

    def tick(self):
        now = self.engine.clock()
        with self.lock:
            if self.current is None:
                return
            operation = self.operations[self.current]
            running = operation["status"] == "running"
            if (running and now < self.next_event) or (not running and self.terminal_emitted):
                return
            operation_id = self.current
        detail = None
        if running:
            try:
                with self.backend_lock:
                    detail = (self.manual_progress if self.manual_progress is not None else
                              backend_progress(self.backend.latest_progress()) if self.backend is not None else
                              {"done": 0, "total": None, "label": ""})
            except Exception:
                self.engine.logger.error("Progress polling failed")
        with self.lock:
            if operation_id != self.current:
                return
            operation = self.operations[operation_id]
            if operation["status"] == "running":
                if detail is not None:
                    operation.update(detail)
                self.next_event = now + POLL_SECONDS
            else:
                self.terminal_emitted = True
            frame = {"event": "progress", "id": self.request_id, "operationId": self.current,
                     "progress": deepcopy(operation)}
        self.engine.writer.write(frame)

    def close(self):
        if self.running:
            self.abort(self.current)
        if self.thread is not None:
            self.thread.join()
