"""Small fixtures shared by the engine area's unittest modules."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

from anki.collection import Collection

ENGINE_ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ENGINE_ROOT / "launch.py"


@contextmanager
def temp_collection():
    with tempfile.TemporaryDirectory(prefix="memr-srs-test-") as directory:
        col = Collection(str(Path(directory) / "collection.anki2"))
        try:
            yield col
        finally:
            col.close()


def build_collection(col, deck_count=1, notes_per_deck=1):
    """Return generated deck/note/card ids using Anki's public creation APIs."""
    model = col.models.by_name("Basic")
    result = {"deckIds": [], "noteIds": [], "cardIds": []}
    for deck_index in range(deck_count):
        deck_id = col.decks.id(f"Generated deck {deck_index + 1}")
        result["deckIds"].append(int(deck_id))
        for note_index in range(notes_per_deck):
            note = col.new_note(model)
            note["Front"] = f"Question {deck_index + 1}/{note_index + 1}"
            note["Back"] = "Generated answer"
            col.add_note(note, deck_id)
            result["noteIds"].append(int(note.id))
            result["cardIds"].extend(int(card.id) for card in note.cards())
    return result


class FakeClock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class EnginePipe:
    """A subprocess with bounded read deadlines and cleanup owned by the test."""
    def __init__(self, directory, code=None, collection_path=None, log=True):
        self.collection_path = Path(collection_path or Path(directory) / "collection.anki2")
        self.log_path = Path(directory) / "engine.log"
        args = [sys.executable, "-I", "-B"]
        if code is None:
            args += [str(LAUNCH)]
        else:
            args += ["-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); " + code, str(ENGINE_ROOT)]
        args += ["--collection", str(self.collection_path)]
        if log:
            args += ["--log", str(self.log_path)]
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR") if key in os.environ}
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=directory, env=env)
        self.inbox = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.sequence = 0

    def _read(self):
        for line in self.process.stdout:
            self.inbox.put(line)
        self.inbox.put(None)

    def send(self, method, params=None):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": {} if params is None else params}
        self.raw(json.dumps(request).encode() + b"\n")
        return self.sequence

    def raw(self, frame):
        self.process.stdin.write(frame)
        self.process.stdin.flush()

    def receive(self, timeout=5):
        line = self.inbox.get(timeout=timeout)
        if line is None:
            raise EOFError("Engine stdout ended")
        return json.loads(line)

    def response(self, request_id, timeout=5):
        deadline = time.monotonic() + timeout
        events = []
        while True:
            frame = self.receive(max(0.01, deadline - time.monotonic()))
            if frame.get("event"):
                events.append(frame)
            else:
                if frame["id"] != request_id:
                    raise AssertionError("Unexpected response id")
                return frame, events

    def call(self, method, params=None):
        return self.response(self.send(method, params))[0]

    def close(self):
        try:
            if self.process.poll() is None and not self.process.stdin.closed:
                self.send("engine.shutdown")
            self.process.wait(timeout=5)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


# Test-only replacement exercises the real process and collection without the
# area's production handler. Abort reaches Anki's backend and the synthetic wait.
LONG_OPERATION_CODE = """
import threading
from anki import errors
from memr_srs_engine import handlers
from memr_srs_engine.registry import HANDLERS, method
from memr_srs_engine.collection import mutation_result, undo_state
cancelled = threading.Event()
HANDLERS.pop('maintenance.checkDatabase', None)
@method('maintenance.checkDatabase')
def check_database(engine, params):
    original = engine.col.set_wants_abort
    def abort():
        original()
        cancelled.set()
    engine.col.set_wants_abort = abort
    try:
        if cancelled.wait(1.1):
            raise errors.Interrupted('fixture aborted', None, None, None)
        summary, ok = engine.col.fix_integrity()
        if not ok:
            raise RuntimeError('Generated collection integrity failed')
        return {**mutation_result(undo_state(engine.col)), 'summary': summary}
    finally:
        engine.col.set_wants_abort = original
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
