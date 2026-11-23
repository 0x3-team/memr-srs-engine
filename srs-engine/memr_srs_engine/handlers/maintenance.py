"""Native collection maintenance and completed backups in private engine storage."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile
import threading

from ..errors import EngineError, public_error
from ..paths import MAX_UPLOAD_BYTES, checkpoint, mutation, safe_directory
from ..protocol import idle_hook, startup_hook
from ..registry import method
from .empty_scan import scan_empty_cards

BACKUP_INTERVAL_SECONDS = 1800
SUMMARY_LIMIT = 65536  # progress.get's closed operation-outcome schema


class _BackupState:
    def __init__(self, engine):
        self.lock = threading.Lock()
        self.next_backup = engine.clock() + BACKUP_INTERVAL_SECONDS


@startup_hook
def initialize(engine):
    """Initialize once at engine startup; creating the directory performs no backup."""
    if not hasattr(engine, "_maintenance_backups"):
        engine._maintenance_backups = _BackupState(engine)
        safe_directory(engine, "backups")


def _state(engine):
    initialize(engine)
    return engine._maintenance_backups


def _valid_id(value):
    if type(value) is not str:
        return False
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return (bool(value) and not value.startswith(".") and Path(value).name == value
            and value.endswith(".colpkg") and byte_length <= 255
            and not any(character in "/\\" or ord(character) < 32 or 127 <= ord(character) <= 159
                        for character in value))


def _backup_files(engine):
    directory = safe_directory(engine, "backups")
    files = []
    for path in directory.iterdir():
        if not _valid_id(path.name):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size > 0:
            files.append((path, info))
    return sorted(files, key=lambda entry: (entry[1].st_mtime_ns, entry[0].name), reverse=True)


@contextmanager
def _open_backup(path):
    """Open the inspected regular file without following a substituted symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise EngineError("not_found", "The backup is unavailable.") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise EngineError("invalid_params", "The backup must be a regular file.")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise EngineError("not_found", "The backup is unavailable.") from None
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise EngineError("invalid_params", "The backup changed while it was opened.")
        yield stream


def _copy_with_checkpoints(engine, source, destination):
    while True:
        checkpoint(engine)
        block = source.read(1024 * 1024)
        if not block:
            return
        destination.write(block)


def _verify_backup(path, engine):
    from .transfer import _package, restored_collection

    try:
        with engine.progress.using_backend(None):
            engine.progress.report(label="Verifying completed backup")
            expanded_bound = _package(path, engine)
        with restored_collection(engine, path, prefix="srs-verify-backup-", expanded_bound=expanded_bound) as col:
            if col.db.scalar("pragma quick_check") != "ok":
                raise ValueError("Invalid backup database")
            checkpoint(engine)
    except Exception as error:
        mapped = public_error(error)
        if mapped.code == "aborted":
            raise mapped from None
        raise EngineError("internal", "A completed collection backup is unavailable.") from None


def _signature(info):
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def force_backup(engine, include_media=False) -> Path:
    """Require an awaited, verified native backup before collection replacement.

    Anki may return False for force=True when the collection has not changed;
    that is safe only if its existing completed backup is still available.
    Destructive replacement additionally requires a fresh full-media recovery
    archive even when the native DB has not changed since its last backup.
    """
    state = _state(engine)
    checkpoint(engine)
    with state.lock:
        checkpoint(engine)
        before = {path.name: _signature(info) for path, info in _backup_files(engine)}
        created = engine.col.create_backup(backup_folder=str(safe_directory(engine, "backups")),
                                          force=True, wait_for_completion=True)
        checkpoint(engine)
        files = _backup_files(engine)
        if created:
            files = [(path, info) for path, info in files
                     if before.get(path.name) != _signature(info)]
        if not files:
            raise EngineError("internal", "A completed collection backup is unavailable.")
        path = files[0][0]
        _verify_backup(path, engine)
        checkpoint(engine)
        path.chmod(0o600)
        if include_media:
            from .recovery import create_recovery_backup
            path = create_recovery_backup(engine)
        state.next_backup = engine.clock() + BACKUP_INTERVAL_SECONDS
        return path


@idle_hook(1)
def periodic_tick(engine):
    """Start Anki's background backup from the idle collection-owning thread."""
    state = _state(engine)
    if (engine.progress.running or engine.clock() < state.next_backup
            or not state.lock.acquire(blocking=False)):
        return
    try:
        if engine.progress.running or engine.clock() < state.next_backup:
            return
        # Throttle failed and no-change attempts as well as successful backups.
        state.next_backup = engine.clock() + BACKUP_INTERVAL_SECONDS
        engine.col.create_backup(backup_folder=str(safe_directory(engine, "backups")),
                                 force=False, wait_for_completion=False)
    except Exception:
        engine.logger.error("Periodic collection backup failed")
    finally:
        state.lock.release()


@method("maintenance.checkDatabase")
def check_database(engine, params):
    checkpoint(engine)
    summary, ok = engine.col.fix_integrity()
    # Native repairs have committed when this returns; report that actual result.
    warnings = []
    if len(summary) > SUMMARY_LIMIT:
        marker = "\n[Database check summary truncated.]"
        summary = summary[:SUMMARY_LIMIT - len(marker)] + marker
        warnings.append("The database check summary exceeded its display limit and was truncated.")
    result = mutation(engine.col, summary=summary)
    # Database repair returns no OpChanges and invalidates collection caches/undo.
    result["changes"] = {key: True for key in result["changes"]}
    if not ok:
        warnings.append("The database check reported problems; review its summary.")
    if warnings:
        result["warnings"] = warnings
    return result


@method("maintenance.emptyCards")
def empty_cards(engine, params):
    checkpoint(engine)
    report = scan_empty_cards(engine)
    checkpoint(engine)
    return mutation(engine.col, emptyCards=report)


@method("maintenance.removeEmptyCards")
def remove_empty_cards(engine, params):
    checkpoint(engine)
    card_ids = list(dict.fromkeys(int(value) for value in params["cardIds"]))
    if not card_ids:
        return mutation(engine.col, count=0)
    existing = set(engine.col.find_cards("cid:" + ",".join(map(str, card_ids))))
    if set(card_ids) - existing:
        raise EngineError("not_found", "An empty card is unavailable.")
    checkpoint(engine)
    empty = {card_id for note in scan_empty_cards(engine)["notes"] for card_id in note["cardIds"]}
    for card_id in card_ids:
        checkpoint(engine)
        if card_id not in empty:
            raise EngineError("invalid_params", "Only empty cards may be removed.")
    checkpoint(engine)
    removed = engine.col.remove_cards_and_orphaned_notes(card_ids)
    return mutation(engine.col, removed.changes, count=int(removed.count))


@method("maintenance.backupNow")
def backup_now(engine, params):
    force_backup(engine)
    return mutation(engine.col, summary="Collection backup completed.")


@method("maintenance.backups")
def backups(engine, params):
    return {"backups": [{"id": path.name, "createdAt": int(info.st_mtime), "bytes": info.st_size}
                        for path, info in _backup_files(engine)]}


@method("maintenance.restoreBackup")
def restore_backup(engine, params):
    from .transfer import import_collection

    checkpoint(engine)
    backup_id = params["backupId"]
    if not _valid_id(backup_id):
        raise EngineError("invalid_params", "Invalid backup identifier.")
    directory = safe_directory(engine, "backups")
    path = directory / backup_id
    # Anki names backups to the second and can overwrite the selected name
    # during the safety backup. Preserve its exact bytes before delegation.
    with _open_backup(path) as source, tempfile.TemporaryDirectory(prefix="restore-", dir=directory) as scratch:
        if os.fstat(source.fileno()).st_size > MAX_UPLOAD_BYTES:
            raise EngineError("invalid_params", "The backup exceeds the package size limit.")
        snapshot = Path(scratch) / "restore.colpkg"
        with snapshot.open("xb") as destination:
            _copy_with_checkpoints(engine, source, destination)
        snapshot.chmod(0o600)
        checkpoint(engine)
        return import_collection(engine, snapshot)
