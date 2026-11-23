"""Paced page-cache warm-up for a freshly opened collection.

A collection that opens with cold operating-system caches — the first open
after a restart, or a freshly linked copy — pays full disk latency on its
first large scan: statistics reads the whole revlog. Reading the file once,
in small chunks on a daemon thread, lets the kernel pull the pages in while
requests proceed. Plain file reads take no SQLite lock or transaction, so
review writes are unaffected; the loop stops when the engine stops or the
collection is replaced.

The pass holds its read descriptors until SQLite closes the collection —
closing any descriptor earlier would strip the process's POSIX record locks
on that inode — and releases them afterwards, so a replaced collection
frees its old inode instead of keeping it pinned until process exit.
"""
import os
from pathlib import Path
import platform
import sys
import threading
import time

from .protocol import startup_hook

CHUNK_BYTES = 4 * 1024 * 1024
CHUNK_PAUSE_SECONDS = 0.025
SUFFIXES = ("", "-wal")

_IOPRIO_CLASS_IDLE = 3
# ioprio_set syscall numbers are arch-specific; other arches run nice-only.
_IOPRIO_SET_SYSCALLS = {"x86_64": 251, "aarch64": 30}


def lower_thread_priority():
    """Best-effort: demote only this thread, only where demotion is thread-scoped.

    Linux keeps nice and ioprio per thread, so a worker demoting itself
    leaves the request loop untouched. Darwin's setpriority(PRIO_PROCESS)
    is process-wide — calling it here would drop the whole engine to
    nice 19 for the session — and Windows has neither call, so other
    platforms keep normal priority; the chunked pace already bounds the
    load on every platform.
    """
    if sys.platform != "linux":
        return
    try:
        os.nice(19)  # per-thread on every Linux arch
    except (AttributeError, OSError):
        pass
    syscall = _IOPRIO_SET_SYSCALLS.get(platform.machine())
    if syscall is not None:
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            # ioprio_set(IOPRIO_WHO_PROCESS=1, pid=0=self, IOPRIO_CLASS_IDLE<<13)
            libc.syscall(syscall, 1, 0, _IOPRIO_CLASS_IDLE << 13)
        except Exception:
            pass


def collection_files(col):
    """The live database files whose pages back cold statistics reads."""
    path = Path(col.path)
    return [candidate for suffix in SUFFIXES
            if (candidate := path.with_name(path.name + suffix)).is_file()]


class CollectionWarmup:
    """One bounded read pass over the collection files.

    Cancellation has two scopes: `engine.stop` ends the pass with the whole
    engine, `cancel()` ends just this pass (a collection replace keeps the
    engine running). `close()` joins the pass and releases the descriptors;
    callers invoke it only after SQLite has closed the collection.
    """

    def __init__(self, engine, paths, pause_seconds=CHUNK_PAUSE_SECONDS,
                 chunk_bytes=CHUNK_BYTES, sleep=time.sleep):
        self.engine = engine
        self.paths = [Path(path) for path in paths]
        self.pause_seconds = pause_seconds
        self.chunk_bytes = chunk_bytes
        self.sleep = sleep
        self.bytes_read = 0
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.closed = False
        self.thread = None
        # POSIX drops every fcntl lock this process holds on an inode the
        # moment ANY descriptor of it closes (fcntl(2) "record locks" caveat).
        # Anki's SQLite connection locks the collection's lock-byte page, so
        # these descriptors must stay open while the collection is open;
        # closing one mid-session would silently strip the engine's
        # collection lock and let a second process open the same collection.
        self._fds_lock = threading.Lock()
        self.held_fds = []

    def start(self):
        if self.thread is None:
            if not self.paths:
                self.done.set()
                return
            self.thread = threading.Thread(target=self._run, name="srs-collection-warmup",
                                           daemon=True)
            self.thread.start()

    def cancel(self):
        """Ask the pass to stop; the descriptors stay held until close()."""
        self.cancelled.set()

    def close(self):
        """Join the pass and release the held descriptors.

        Call only after SQLite has closed the collection. While the engine
        holds its POSIX lock, a descriptor close here would strip that lock;
        once the collection is closed, keeping the descriptors only pins a
        possibly replaced inode for the rest of the process lifetime.
        """
        self.cancel()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=5)
        with self._fds_lock:
            self.closed = True
            fds, self.held_fds = self.held_fds, []
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def _stopped(self):
        return self.engine.stop.is_set() or self.cancelled.is_set()

    def _run(self):
        lower_thread_priority()
        try:
            for path in self.paths:
                if self._stopped():
                    break
                self._warm_path(path)
        finally:
            self.done.set()

    def _warm_path(self, path):
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        with self._fds_lock:
            if self.closed:
                os.close(fd)
                return
            self.held_fds.append(fd)
        try:
            while not self._stopped():
                data = os.read(fd, self.chunk_bytes)
                if not data:
                    return
                self.bytes_read += len(data)
                self.sleep(self.pause_seconds)
        except OSError:
            pass


@startup_hook
def schedule_collection_warmup(engine):
    """Begin the paced pass when the collection opens; stop/close ends it."""
    state = getattr(engine, "_collection_warmup", None)
    if state is not None and not state.closed:
        return
    try:
        paths = collection_files(engine.col)
    except Exception:
        paths = []
    engine._collection_warmup = CollectionWarmup(engine, paths)
    engine._collection_warmup.start()
