"""Opaque upload handles and private, atomically published export files."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time

from .collection import mutation_result, undo_state
from .errors import EngineError
from .protocol import startup_hook

UPLOAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?:\.[a-z0-9]{1,16})?\Z")
DOWNLOAD_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


def checkpoint(engine):
    engine.progress.check_abort()


def fsync_directory(path):
    """Persist a directory's entries, so a rename or new file survives sudden power loss.

    Windows cannot open a directory for synchronization, so the call is a no-op there.
    """
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mutation(col, changes=None, **payload):
    result = mutation_result(undo_state(col))
    if changes is not None:
        aliases = {"deckConfig": "deck_config", "browserTable": "browser_table",
                   "noteText": "note_text", "studyQueues": "study_queues"}
        result["changes"] = {key: bool(getattr(changes, aliases.get(key, key)))
                             for key in result["changes"]}
    return {**result, **payload}


def safe_directory(engine, name):
    root = Path(engine.col.path).parent
    directory = root / name
    if directory.is_symlink():
        raise EngineError("invalid_params", "The storage directory is unavailable.")
    directory.mkdir(mode=0o700, exist_ok=True)
    if not directory.is_dir() or directory.resolve() != root.resolve() / name:
        raise EngineError("invalid_params", "The storage directory is unavailable.")
    directory.chmod(0o700)
    return directory


def resolve_upload(engine, upload_id, extensions=None):
    if type(upload_id) is not str or not UPLOAD_ID.fullmatch(upload_id) or upload_id.endswith(".upload"):
        raise EngineError("invalid_params", "Invalid upload handle.")
    if extensions is not None and Path(upload_id).suffix not in extensions:
        raise EngineError("invalid_params", "The upload has an unsupported file type.")
    incoming = safe_directory(engine, "incoming")
    path = incoming / upload_id
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise EngineError("not_found", "The upload is unavailable.") from None
    if not stat.S_ISREG(info.st_mode) or path.resolve() != incoming.resolve() / upload_id:
        raise EngineError("invalid_params", "The upload must be a regular file.")
    try:
        limit = int(os.environ.get("MEMR_SRS_UPLOAD_MAX_BYTES", MAX_UPLOAD_BYTES))
    except ValueError:
        limit = MAX_UPLOAD_BYTES
    if info.st_size > min(MAX_UPLOAD_BYTES, max(1, limit)):
        raise EngineError("invalid_params", "The upload exceeds the size limit.")
    return path


@startup_hook
def prune_exports(engine, now=None):
    now = time.time() if now is None else now
    directory = safe_directory(engine, "exports")
    for child in directory.iterdir():
        if DOWNLOAD_TOKEN.fullmatch(child.name) and not child.is_symlink() and child.is_dir():
            if now - child.stat().st_mtime > 3600:
                shutil.rmtree(child)


@contextmanager
def export_destination(engine, filename):
    """The caller writes the partial path, then explicitly publishes completion."""
    prune_exports(engine)
    token = secrets.token_urlsafe(32)
    exports = safe_directory(engine, "exports")
    folder = exports / token
    folder.mkdir(mode=0o700)
    partial = folder / ("partial-" + filename)
    partial.touch(mode=0o600)
    completed = False

    def publish():
        nonlocal completed
        checkpoint(engine)
        partial.chmod(0o600)
        with partial.open("rb") as file:
            os.fsync(file.fileno())
        final = folder / filename
        os.replace(partial, final)
        # The file is durable only once its folder entry is, and the token folder
        # is new, so its own entry in exports/ must be written down too. A failure
        # here fails the export: the finally block below removes the whole folder.
        fsync_directory(folder)
        fsync_directory(exports)
        now = time.time()
        os.utime(folder, (now, now))
        completed = True
        return {"downloadToken": token, "filename": filename,
                "bytes": final.stat().st_size, "expiresAt": int(now) + 600}

    try:
        yield partial, publish
    finally:
        if not completed:
            shutil.rmtree(folder)
