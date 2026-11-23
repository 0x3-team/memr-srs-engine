"""Media files move between gateway uploads, the media folder and Anki's trash."""
from pathlib import Path
import os
import shutil
import stat
import tempfile

from ..errors import EngineError
from ..paths import checkpoint, mutation, resolve_upload
from ..registry import method

LIST_LIMIT = 100000  # contract maxItems for filename lists
WARNING_LIMIT = 4096  # contract maxLength for warning strings
COMPONENT_LIMIT = 255  # filesystem name limit in bytes
MAX_MEDIA_BYTES = 100 * 1024 * 1024


def _text(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise EngineError("invalid_params", "Text must contain valid Unicode characters.") from None
    return value


def _media_name(value):
    """A bare media file name: never a path, never empty or control text."""
    _text(value)
    if not value.strip() or value in (".", "..") or "/" in value or "\\" in value:
        raise EngineError("invalid_params", "Media names must be single nonempty components.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise EngineError("invalid_params", "Media names must not contain control characters.")
    return value


def _temp_component(name):
    """Stage the requested name inside one filesystem component; Anki truncates beyond it."""
    encoded = name.encode("utf-8")
    if len(encoded) <= COMPONENT_LIMIT:
        return name
    stem, dot, extension = name.rpartition(".")
    suffix = dot + extension if dot and len(extension.encode("utf-8")) <= 16 else ""
    keep = COMPONENT_LIMIT - len(suffix.encode("utf-8"))
    trimmed = (stem if suffix else name).encode("utf-8")[:keep].decode("utf-8", errors="ignore")
    return (trimmed or "media") + suffix


def _trash_entries(engine):
    """{name: bytes} for regular files inside Anki's media trash; links are refused."""
    root = Path(engine.col.path).parent
    directory = root / "media.trash"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise EngineError("invalid_params", "The media trash directory is unavailable.")
    if not directory.is_dir():
        return {}
    entries = {}
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            raise EngineError("invalid_params", "The media trash holds an unsafe entry.")
        entries[child.name] = child.stat().st_size
    return entries


@method("media.addFromUpload")
def add_from_upload(engine, params):
    col = engine.col
    source = resolve_upload(engine, params["uploadId"])
    name = _media_name(params["filename"])
    if source.lstat().st_size > MAX_MEDIA_BYTES:
        raise EngineError("invalid_params", "Media files must be at most 100 MiB.")
    # The upload's basename is its opaque id, so the requested name is staged
    # as a copy. Anki still normalizes (case, characters, dedupe, length).
    with tempfile.TemporaryDirectory(dir=source.parent) as staging:
        staged = Path(staging) / _temp_component(name)
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as read, staged.open("wb") as write:
            info = os.fstat(read.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MEDIA_BYTES:
                raise EngineError("invalid_params", "Media files must be regular files of at most 100 MiB.")
            shutil.copyfileobj(read, write)
        stored = col.media.add_file(str(staged))
    return mutation(col, filename=stored)


@method("media.check")
def check(engine, _params):
    col = engine.col
    checkpoint(engine)
    output = col.media.check()
    checkpoint(engine)
    warnings = []
    broken = len(output.missing_media_notes)
    if broken:
        warnings.append(f"Broken media references found in {broken} "
                        f"note{'s' if broken != 1 else ''}.")
    # Beyond the first counts header the localized report lists renamed,
    # subfolder, oversize and per-file details no structured field carries.
    detail = output.report.split("\n\n", 1)
    if len(detail) == 2 and detail[1].strip():
        warnings.append(detail[1].strip()[:WARNING_LIMIT])
    if len(output.missing) > LIST_LIMIT or len(output.unused) > LIST_LIMIT:
        raise EngineError("unsupported", "The media listing exceeds the result limit.")
    return mutation(col, missing=sorted(output.missing), unused=sorted(output.unused), warnings=warnings)


@method("media.trashUnused")
def trash_unused(engine, params):
    col = engine.col
    names = list(dict.fromkeys(_media_name(name) for name in params["filenames"]))
    if names:
        # Anki's own check decides which on-disk files are truly unreferenced;
        # trash_files itself would happily move files still in use.
        unused = set(col.media.check().unused)
        media_dir = Path(col.media.dir())
        for name in names:
            candidate = media_dir / name
            if candidate.is_symlink():
                raise EngineError("invalid_params", "Media names must be regular files.")
            if not candidate.is_file():
                raise EngineError("not_found", "The media file does not exist.")
            if name not in unused:
                raise EngineError("invalid_params", "The media file is still referenced.")
        col.media.trash_files(names)
    return mutation(col, count=len(names))


@method("media.trash")
def trash(engine, _params):
    entries = _trash_entries(engine)
    if len(entries) > LIST_LIMIT:
        raise EngineError("unsupported", "The media trash listing exceeds the result limit.")
    return {"files": [{"filename": name, "bytes": entries[name]}
                      for name in sorted(entries)]}


@method("media.emptyTrash")
def empty_trash(engine, _params):
    count, _ = _empty_trash_with_backup(engine)
    return mutation(engine.col, count=count)


@method("media.emptyTrashWithBackup")
def empty_trash_with_backup(engine, params):
    count, backup = _empty_trash_with_backup(engine, params["filenames"])
    summary = (f"Removed {count} trashed media files. Verified recovery is available in Backups: {backup}"
               if backup else "Media trash is already empty.")
    return mutation(engine.col, count=count, summary=summary)


def _empty_trash_with_backup(engine, expected_names=None):
    from .recovery import _inventory, create_media_trash_backup

    checkpoint(engine)
    col = engine.col
    before = _trash_entries(engine)
    if expected_names is not None and (len(expected_names) != len(set(expected_names))
                                       or set(expected_names) != set(before)):
        raise EngineError("conflict", "Media trash changed. Refresh its list and confirm the files again.")
    if not before:
        return 0, None
    directory = Path(col.path).parent / "media.trash"
    inventory = _inventory(engine, directory)
    backup = create_media_trash_backup(engine)
    if _inventory(engine, directory) != inventory:
        raise EngineError("conflict", "Media trash changed while preparing its recovery backup.")
    checkpoint(engine)
    col.media.empty_trash()
    # Native deletion has committed; a late cancellation cannot undo it.
    return len(before) - len(_trash_entries(engine)), backup.name


@method("media.restoreTrash")
def restore_trash(engine, _params):
    col = engine.col
    before = _trash_entries(engine)
    col.media.restore_trash()
    return mutation(col, count=len(before) - len(_trash_entries(engine)))
