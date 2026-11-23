"""Cancellable native empty-card scans over a read-only SQLite snapshot.

The pinned native scan has no progress/abort checkpoints. Its disposable
process owns only the snapshot, so cancellation never kills a collection
writer or substitutes a different card-generation predicate.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

from ..errors import EngineError
from ..paths import checkpoint
from ..protocol import MAX_RESPONSE_BYTES
from ..validate import validate_value

POLL_SECONDS = 0.05
TERMINATE_SECONDS = 0.5
KILL_SECONDS = 1
SNAPSHOT_RESERVE_BYTES = 16 * 1024 * 1024
ENGINE_ROOT = Path(__file__).resolve().parents[2]

WORKER_CODE = r"""
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from memr_srs_engine.errors import EngineError, public_error
from anki.collection import Collection
source, output, started = map(pathlib.Path, sys.argv[2:5])
limit = int(sys.argv[5])
col = None
try:
    col = Collection(str(source))
    started.touch(mode=0o600)
    report = col.get_empty_cards()
    if len(report.notes) > 100000:
        raise EngineError('internal', 'The empty-card report exceeds the result limit.')
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as file:
        size = 0
        def write(value):
            global size
            size += len(value)
            if size > limit:
                raise EngineError('internal', 'The empty-card report exceeds the result limit.')
            file.write(value)
        write(b'{"result":{"notes":[')
        for index, note in enumerate(report.notes):
            if len(note.card_ids) > 100000:
                raise EngineError('internal', 'The empty-card report exceeds the result limit.')
            if index:
                write(b',')
            write(json.dumps({'noteId': int(note.note_id), 'cardIds': list(note.card_ids),
                              'allCardsEmpty': bool(note.will_delete_note)},
                             separators=(',', ':'), allow_nan=False).encode('ascii'))
        write(b']}}')
except Exception as error:
    payload = json.dumps({'error': public_error(error).as_dict()},
                         separators=(',', ':'), allow_nan=False).encode('ascii')
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as file:
        file.write(payload)
finally:
    if col is not None:
        col.close()
"""


def _snapshot(engine, destination):
    source = target = None
    try:
        source = sqlite3.connect(Path(engine.col.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
        page_count = source.execute("pragma page_count").fetchone()[0]
        page_size = source.execute("pragma page_size").fetchone()[0]
        if shutil.disk_usage(destination.parent).free < page_count * page_size + SNAPSHOT_RESERVE_BYTES:
            raise EngineError("internal", "There is not enough free storage for an empty-card scan.")
        target = sqlite3.connect(str(destination))
        destination.chmod(0o600)

        def progress(_status, remaining, total):
            checkpoint(engine)
            if total >= 0 and remaining >= 0:
                engine.progress.report(done=total - remaining, total=total, label="Preparing empty-card scan")

        source.backup(target, pages=128, progress=progress, sleep=POLL_SECONDS)
        checkpoint(engine)
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()


def _reap(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=KILL_SECONDS)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate scan result key")
        value[key] = item
    return value


def _nonfinite(_value):
    raise ValueError("Nonfinite scan result")


def _result(engine, path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_RESPONSE_BYTES:
        raise EngineError("internal", "The empty-card worker returned an invalid result.")
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as file:
        data = file.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise EngineError("internal", "The empty-card worker returned an oversized result.")
    payload = json.loads(data, object_pairs_hook=_object, parse_constant=_nonfinite)
    if type(payload) is not dict:
        raise EngineError("internal", "The empty-card worker returned an invalid result.")
    progress = engine.registry.catalogue["progress.get"]["result"]["properties"]
    if set(payload) == {"error"} and not validate_value(progress["error"], payload["error"]):
        if type(payload["error"]) is dict:
            raise EngineError(**payload["error"])
    if set(payload) != {"result"} or validate_value(progress["result"]["properties"]["emptyCards"], payload["result"]):
        raise EngineError("internal", "The empty-card worker returned an invalid result.")
    return payload["result"]


def scan_empty_cards(engine):
    """Return Anki's native report; abort/shutdown reap the scratch-only child."""
    checkpoint(engine)
    root = Path(engine.col.path).parent
    try:
        with engine.progress.using_backend(None), tempfile.TemporaryDirectory(prefix="srs-empty-", dir=root) as directory:
            directory = Path(directory)
            snapshot, result, started = (directory / name for name in ("collection.anki2", "result.json", "started"))
            engine.progress.report(label="Preparing empty-card scan")
            _snapshot(engine, snapshot)
            checkpoint(engine)
            env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR") if key in os.environ}
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", WORKER_CODE, str(ENGINE_ROOT),
                                        str(snapshot), str(result), str(started), str(MAX_RESPONSE_BYTES)],
                                       cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                native_started = False
                while process.poll() is None:
                    checkpoint(engine)
                    if not native_started and started.exists():
                        native_started = True
                        engine.progress.report(label="Scanning empty cards")
                    try:
                        process.wait(timeout=POLL_SECONDS)
                    except subprocess.TimeoutExpired:
                        pass
                checkpoint(engine)
                if process.returncode != 0:
                    raise EngineError("internal", "The empty-card worker could not complete the scan.")
                report = _result(engine, result)
                checkpoint(engine)
                return report
            finally:
                _reap(process)
    except EngineError:
        raise
    except Exception:
        raise EngineError("internal", "The empty-card scan could not complete.") from None
