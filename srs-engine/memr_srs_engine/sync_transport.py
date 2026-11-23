"""A native sync operation owns its transport child and cancellation watcher."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading

from .errors import EngineError
from .sync_auth import sync_endpoint


class SyncBridge:
    def __init__(self, target):
        self.target = sync_endpoint(target)
        self.process = None
        self.reader = None
        self.ready = queue.Queue(maxsize=1)
        self.faults = set()
        self.endpoint = None
        self.closed = False

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(4097)
                if not line or len(line) > 4096:
                    break
                message = json.loads(line)
                if "fault" in message:
                    self.faults.add(message["fault"])
                elif self.endpoint is None and self.ready.empty():
                    self.ready.put_nowait(message)
        except (ValueError, OSError, queue.Full):
            pass

    def __enter__(self):
        executable = shutil.which("node")
        if executable is None:
            raise EngineError("engine_unavailable", "The sync transport runtime is unavailable.")
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR") if key in os.environ}
        try:
            self.process = subprocess.Popen([executable, str(Path(__file__).with_name("sync_bridge.mjs"))],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            env=env, shell=False)
            self.reader = threading.Thread(target=self._read, name="srs-sync-transport")
            self.reader.start()
            self.process.stdin.write((json.dumps({"endpoint": self.target}) + "\n").encode())
            self.process.stdin.flush()
            message = self.ready.get(timeout=5)
            if "error" in message:
                code = message["error"] if message["error"] in ("unsupported", "invalid_params") else "internal"
                raise EngineError(code, "The sync transport could not start. A Node runtime with Zstandard streams is required.")
            value = message.get("endpoint", "")
            if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/[A-Za-z0-9_-]{43}/", value):
                raise EngineError("internal", "The sync transport returned an invalid endpoint.")
            self.endpoint = value
            return self
        except (OSError, queue.Empty):
            self.close()
            raise EngineError("engine_unavailable", "The sync transport could not start.") from None
        except Exception:
            self.close()
            raise

    def close(self):
        if self.closed or self.process is None:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=2)
        if self.reader is not None:
            self.reader.join(timeout=1)
        self.process.stdout.close()

    def __exit__(self, *_args):
        self.close()
        if "redirect_refused" in self.faults:
            raise EngineError("unsupported", "The sync server redirected the request. Choose its final endpoint explicitly.") from None
        if self.faults:
            raise EngineError("unsupported", "The sync server returned an invalid or oversized response.") from None


@contextmanager
def native_sync(engine, endpoint):
    engine.progress.check_abort()
    stop = threading.Event()
    cancellation_errors = []

    def watch():
        # Anki registers its abort handle inside the blocking native call.
        # Repetition covers an abort just before registration, and shutdown
        # after the main protocol loop has stopped polling progress.
        while not stop.wait(0.025):
            if engine.progress.abort_requested:
                try:
                    with engine.progress.backend_lock:
                        engine.col.abort_sync()
                        engine.col.abort_media_sync()
                except Exception:
                    cancellation_errors.append(True)
                    return

    watcher = threading.Thread(target=watch, name="srs-sync-cancellation")
    watcher.start()
    try:
        with SyncBridge(endpoint) as bridge:
            engine.progress.check_abort()
            yield bridge
            engine.progress.check_abort()
            if cancellation_errors:
                raise EngineError("internal", "Native sync cancellation failed.")
    except Exception:
        engine.progress.check_abort()
        raise
    finally:
        stop.set(); watcher.join()


def native_call(engine, action):
    try:
        return action()
    except EngineError:
        raise
    except Exception:
        engine.progress.check_abort()
        # Native exceptions can contain URLs or server-provided text. Replace
        # them before the protocol logger sees an exception chain.
        raise EngineError("engine_unavailable", "The sync service could not complete the request.") from None
