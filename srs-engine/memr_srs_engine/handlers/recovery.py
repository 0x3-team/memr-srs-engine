"""Verified, retained DB/media recovery before native collection replacement.

Native replacement can publish media before reporting failure. Prepare on a
private snapshot so exporting cannot clear the live collection's undo. The
archive contains every top-level media file, including unreferenced files;
native flat-file imports leave existing subfolders and media trash untouched.
"""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unicodedata
import uuid

from anki.collection import Collection

from ..errors import EngineError
from ..paths import checkpoint, safe_directory
from .empty_scan import _snapshot
from .media import _media_name

COPY_BYTES = 1024 * 1024
RESERVE_BYTES = 16 * 1024 * 1024


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _inventory(engine, directory):
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise EngineError("invalid_params", "The media directory is unsafe for recovery.")
    files = {}
    for path in directory.iterdir():
        checkpoint(engine)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue  # Native collection imports cannot address nested paths.
        if not stat.S_ISREG(info.st_mode):
            raise EngineError("invalid_params", "Recovery requires regular media files without links.")
        _media_name(path.name)
        if unicodedata.normalize("NFC", path.name) != path.name:
            raise EngineError("invalid_params", "Recovery requires normalized media filenames.")
        files[path.name] = _signature(info)
    return files


def _copy_file(engine, path, expected, destination):
    digest, size = hashlib.sha256(), 0
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as source:
        if _signature(os.fstat(source.fileno())) != expected:
            raise EngineError("conflict", "Media changed while preparing recovery.")
        with destination.open("xb") as target:
            destination.chmod(0o600)
            while True:
                checkpoint(engine)
                block = source.read(COPY_BYTES)
                if not block:
                    break
                size += len(block)
                if size > expected[3]:
                    raise EngineError("conflict", "Media changed while preparing recovery.")
                digest.update(block)
                target.write(block)
        if size != expected[3] or _signature(os.fstat(source.fileno())) != expected:
            raise EngineError("conflict", "Media changed while preparing recovery.")
    if _signature(path.lstat()) != expected:
        raise EngineError("conflict", "Media changed while preparing recovery.")
    return size, digest.digest()


def _hash_file(engine, path):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while True:
            checkpoint(engine)
            block = stream.read(COPY_BYTES)
            if not block:
                return size, digest.digest()
            size += len(block)
            digest.update(block)


def _verify(engine, package, expected):
    from .transfer import restored_collection

    with restored_collection(engine, package, prefix="srs-recovery-proof-") as col:
        if col.db.scalar("pragma quick_check") != "ok":
            raise EngineError("internal", "The recovery database could not be verified.")
        directory = Path(col.media.dir())
        actual = _inventory(engine, directory)
        if set(actual) != set(expected):
            raise EngineError("internal", "The recovery media inventory could not be verified.")
        for name in expected:
            checkpoint(engine)
            if _hash_file(engine, directory / name) != expected[name]:
                raise EngineError("internal", "The recovery media bytes could not be verified.")


def _publish(engine, package, prefix):
    directory = safe_directory(engine, "backups")
    destination = directory / (prefix + "-" + str(uuid.uuid4()) + ".colpkg")
    # Reserve only our unique name; never replace an existing retained backup.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    reserved = _signature(os.fstat(fd))
    os.close(fd)
    try:
        package.chmod(0o600)
        with package.open("rb") as stream:
            os.fsync(stream.fileno())
        checkpoint(engine)
        os.replace(package, destination)
    except BaseException:
        if destination.exists() and _signature(destination.lstat()) == reserved:
            destination.unlink()  # Only the empty reservation made by this call.
        raise
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return destination


def create_recovery_backup(engine):
    """Publish a verified full-media COLPKG, retaining it without pruning."""
    return _create_recovery_backup(engine, Path(engine.col.media.dir()), "recovery")


def create_media_trash_backup(engine):
    """Retain the database and trash bytes; restoration returns media to use.

    A separate archive preserves same-name active and trashed files without
    renaming either. Native restore leaves other active media files in place.
    """
    return _create_recovery_backup(engine, Path(engine.col.path).parent / "media.trash",
                                   "media-trash-recovery")


def _create_recovery_backup(engine, media, prefix):
    checkpoint(engine)
    root = Path(engine.col.path).parent
    with engine.progress.using_backend(None):
        engine.progress.report(label="Preparing full collection recovery")
        files = _inventory(engine, media)
        # Four concurrent copies cover snapshot, copied media, native archive
        # and restored proof, with room for SQLite overhead. This is admission,
        # not a storage quota against concurrent writers.
        database = sum(path.stat().st_size for path in (Path(engine.col.path), Path(engine.col.path + "-wal"))
                       if path.exists())
        required = 4 * (database + sum(info[3] for info in files.values())) + RESERVE_BYTES
        if shutil.disk_usage(root).free < required:
            raise EngineError("internal", "There is not enough free storage for verified collection recovery.")
        with tempfile.TemporaryDirectory(prefix="srs-recovery-", dir=root) as directory:
            directory = Path(directory)
            snapshot = directory / "collection.anki2"
            _snapshot(engine, snapshot)
            clone = Collection(str(snapshot))
            try:
                expected = {}
                for index, (name, info) in enumerate(files.items()):
                    engine.progress.report(done=index, total=len(files), label="Copying recovery media")
                    expected[name] = _copy_file(engine, media / name, info, Path(clone.media.dir()) / name)
                checkpoint(engine)
                package = directory / "recovery.colpkg"
                with engine.progress.using_backend(clone):
                    clone.export_collection_package(str(package), include_media=True, legacy=False)
            finally:
                # Export closes its collection. Reopen only the disposable clone
                # when needed so native close always releases its backend.
                if clone.db is None:
                    clone.reopen()
                clone.close()
            checkpoint(engine)
            engine.progress.report(label="Verifying full collection recovery")
            _verify(engine, package, expected)
            if _inventory(engine, media) != files:
                raise EngineError("conflict", "Media changed while preparing recovery.")
            checkpoint(engine)
            return _publish(engine, package, prefix)
