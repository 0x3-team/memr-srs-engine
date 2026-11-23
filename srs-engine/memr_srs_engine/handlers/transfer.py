"""Native Anki transfer operations over bounded opaque file handles."""
from contextlib import contextmanager
import csv
import json
from pathlib import Path
import shutil
import tempfile

from anki.collection import Collection, CardIdsLimit, DeckIdLimit, media_paths_from_col_path
from anki import import_export_pb2 as pb

from ..errors import EngineError, public_error
from ..paths import checkpoint, export_destination, mutation, resolve_upload
from ..registry import method
from ..protocol import initialize_scheduling
from ..warmup import schedule_collection_warmup
from .package_bounds import validate_package_bounds

DELIMITERS = {"\t": pb.CsvMetadata.Delimiter.TAB, "|": pb.CsvMetadata.Delimiter.PIPE, ";": pb.CsvMetadata.Delimiter.SEMICOLON,
              ":": pb.CsvMetadata.Delimiter.COLON, ",": pb.CsvMetadata.Delimiter.COMMA, " ": pb.CsvMetadata.Delimiter.SPACE}
UPDATE_MODES = {"ifNewer": 0, "always": 1, "never": 2}
CSV_COLUMN_LIMIT = 512
CSV_FIELD_LIMIT = 65536
# A valid row can quote every character and add separators/line endings.
CSV_RECORD_LIMIT = (2 * CSV_FIELD_LIMIT + 3) * CSV_COLUMN_LIMIT + 2
CSV_METADATA_LIMIT = 2 * 1024 * 1024
CSV_SAMPLE_LIMIT = 64 * 1024 * 1024
CSV_PREVIEW_LIMIT = 8 * 1024 * 1024
CSV_NATIVE_SAMPLE_ROWS = 5  # pinned Anki's metadata preview length
SCRATCH_RESERVE_BYTES = 16 * 1024 * 1024


def _delimiter(value):
    if value not in DELIMITERS:
        raise EngineError("invalid_params", "Select a supported delimiter.")
    return DELIMITERS[value]


def _utf8_size(value, limit):
    """Count only until a byte budget is exceeded, without a whole-line copy."""
    total = 0
    for offset in range(0, len(value), 65536):
        total += len(value[offset:offset + 65536].encode("utf-8"))
        if total > limit:
            break
    return total


def _package(path, engine=None):
    """Bound ZIP allocation and native zstd expansion before backend work."""
    return validate_package_bounds(path, engine)


def _counts(col, engine=None):
    media = 0
    for path in Path(col.media.dir()).iterdir():
        if engine is not None:
            checkpoint(engine)
        media += path.is_file()
    return {"notes": col.note_count(), "cards": col.card_count(),
            "decks": len(col.decks.all_names_and_ids()), "notetypes": len(col.models.all_names_and_ids()),
            "media": media}


def _restore(col, path, engine=None):
    media, media_db = media_paths_from_col_path(col.path)
    # A reused native media index can retain checksums across immediate imports.
    # A private comparison index makes primary restore hash actual current files
    # without deleting or modifying the live derived index on native failure.
    primary = engine is not None and col is engine.col
    with tempfile.TemporaryDirectory(prefix="srs-media-index-", dir=Path(col.path).parent) as directory:
        if primary:
            media_db = str(Path(directory) / "media.db")
        warmup_state = getattr(engine, "_collection_warmup", None) if primary else None
        if warmup_state is not None:
            warmup_state.cancel()
        col.close()
        try:
            if warmup_state is not None:
                # SQLite released the collection locks, so the pass's
                # descriptors may close now — and a replaced file's old
                # inode is freed rather than pinned until process exit.
                warmup_state.close()
            if engine is not None:
                checkpoint(engine)
            col._backend.import_collection_package(col_path=col.path, backup_path=str(path),
                                                  media_folder=media, media_db=media_db)
        finally:
            col.reopen()
            col._load_scheduler()
            initialize_scheduling(col)
            if primary:
                schedule_collection_warmup(engine)


@contextmanager
def restored_collection(engine, path, prefix="srs-inspect-", expanded_bound=None):
    """Prove native restorability in disposable storage with scoped controls."""
    with engine.progress.using_backend(None):
        checkpoint(engine)
        if expanded_bound is None:
            expanded_bound = _package(path, engine)
        # Reserve room for extraction and a SQLite journal. This is an
        # admission check, not a filesystem quota against concurrent writers.
        root = Path(engine.col.path).parent
        if shutil.disk_usage(root).free < 2 * expanded_bound + SCRATCH_RESERVE_BYTES:
            raise EngineError("internal", "There is not enough free storage to verify the package safely.")
        with tempfile.TemporaryDirectory(prefix=prefix, dir=root) as directory:
            col = Collection(str(Path(directory) / "collection.anki2"))
            try:
                with engine.progress.using_backend(col):
                    _restore(col, path, engine)
                    checkpoint(engine)
                    yield col
            finally:
                col.close()


def import_collection(engine, path):
    from .maintenance import force_backup
    checkpoint(engine)
    with engine.progress.using_backend(None):
        engine.progress.report(label="Checking collection package")
        expanded_bound = _package(path, engine)
    # Native collection restore writes media before publishing its database.
    # Refuse malformed/unrestorable inputs before any current media is touched.
    with restored_collection(engine, path, prefix="srs-validate-", expanded_bound=expanded_bound):
        checkpoint(engine)
    recovery = force_backup(engine, include_media=True)
    try:
        checkpoint(engine)
        _restore(engine.col, path, engine)
    except Exception as error:
        mapped = public_error(error)
        # A native failure may follow media publication. The verified retained
        # archive is addressable through Backups; never expose a private path.
        raise EngineError(mapped.code, f"Collection restore stopped. Original data is available in Backups: {recovery.name}") from None
    # Replacement invalidates every collection projection and clears undo.
    # A successful native return has committed; a later abort cannot undo it.
    result = mutation(engine.col, summary="Collection restored.", search="", imported={**_counts(engine.col), "updated": 0, "skipped": 0})
    result["changes"] = {key: True for key in result["changes"]}
    return result


def _import_summary(col, response, before, csv_duplicate_mode=None):
    after = _counts(col)
    log = response.log
    # Native CSV logs first-field updates in first_field_match, carrying the
    # previous fields, rather than log.updated (which covers GUID updates).
    matched_updates = log.first_field_match if csv_duplicate_mode == "update" else ()
    updated = len(log.updated) + len(matched_updates)
    skipped = sum(len(getattr(log, key)) for key in
                  ("duplicate", "conflicting", "missing_notetype", "missing_deck", "empty_first_field"))
    if csv_duplicate_mode != "update":
        skipped += len(log.first_field_match)
    imported = {key: max(0, after[key] - before[key]) for key in before}
    imported.update(updated=updated, skipped=skipped)
    return mutation(col, response.changes, imported=imported, **_import_targets(log, matched_updates),
                    summary=f"Imported {len(log.new)} notes; updated {updated}; skipped {skipped}.")


def _import_targets(log, matched_updates=()):
    """Expose exact changed identities without truncating a successful import."""
    ids = set()
    for group in (log.new, log.updated, matched_updates):
        for note in group:
            ids.add(int(note.id.nid))
            if len(ids) > 100000:
                return {"warnings": ["More than 100,000 notes changed. The complete import succeeded, but its exact note selection exceeds the display limit. Use Browse to review the collection."]}
    result = {"affectedIds": sorted(ids)}
    search = "nid:" + (",".join(map(str, result["affectedIds"])) if ids else "0")
    if len(search) <= 65536:
        result["search"] = search
    return result


@method("transfer.inspectPackage")
def inspect_package(engine, params):
    checkpoint(engine)
    with engine.progress.using_backend(None):
        engine.progress.report(label="Checking package")
        path = resolve_upload(engine, params["uploadId"], {".apkg", ".colpkg"})
        expanded_bound = _package(path, engine)
        kind = path.suffix[1:]
        # Preserve native compatibility/counts without mutating the open collection.
        with restored_collection(engine, path, expanded_bound=expanded_bound) as col:
            scheduled = bool(col.db.scalar("select count() from cards where type != 0 or reps != 0") or
                             col.db.scalar("select count() from revlog"))
            inspection = {"kind": kind, **_counts(col, engine), "schedulingIncluded": scheduled,
                "warnings": [] if scheduled or kind == "colpkg" else ["No scheduling history is present."],
                "availableOptions": [] if kind == "colpkg" else
                ["mergeNotetypes", "updateNotes", "updateNotetypes", "includeScheduling", "includeDeckConfigs"]}
            checkpoint(engine)
    checkpoint(engine)
    return mutation(engine.col, packageInspection=inspection)


@method("transfer.importPackage")
def import_package(engine, params):
    checkpoint(engine)
    path = resolve_upload(engine, params["uploadId"], {".apkg", ".colpkg"})
    with engine.progress.using_backend(None):
        engine.progress.report(label="Checking package")
        expanded_bound = _package(path, engine)
    checkpoint(engine)
    if path.suffix == ".colpkg":
        return import_collection(engine, path)
    with restored_collection(engine, path, prefix="srs-validate-", expanded_bound=expanded_bound):
        checkpoint(engine)
    before = _counts(engine.col, engine)
    checkpoint(engine)
    response = engine.col.import_anki_package(pb.ImportAnkiPackageRequest(package_path=str(path),
        options=pb.ImportAnkiPackageOptions(merge_notetypes=params["mergeNotetypes"],
            update_notes=UPDATE_MODES[params["updateNotes"]], update_notetypes=UPDATE_MODES[params["updateNotetypes"]],
            with_scheduling=params["includeScheduling"], with_deck_configs=params["includeDeckConfigs"])))
    return _import_summary(engine.col, response, before)


def _csv_records(file, delimiter, engine=None, on_comment=None, on_line=None):
    """Skip native headers/comments, preserving quoted '#' and multiline data."""
    in_record = False
    record_chars = 0
    first_line = True

    def lines():
        nonlocal in_record, record_chars, first_line
        while True:
            line = file.readline(CSV_RECORD_LIMIT + 1)
            if not line:
                break
            if engine is not None:
                checkpoint(engine)
            if len(line) > CSV_RECORD_LIMIT:
                raise ValueError("CSV physical line exceeds the record limits")
            legacy_tags = first_line and line.startswith("tags:")
            first_line = False
            if not in_record and (line.startswith("#") or legacy_tags):
                if on_comment is not None:
                    on_comment(line)
                if on_line is not None:
                    on_line(line, True, False)
                continue
            record_chars += len(line)
            if record_chars > CSV_RECORD_LIMIT:
                raise ValueError("CSV record exceeds the column and field limits")
            if on_line is not None:
                on_line(line, False, in_record)
            in_record = True
            yield line

    for row in csv.reader(lines(), delimiter=delimiter):
        in_record = False
        record_chars = 0
        yield row


def _csv_rows(path, delimiter, engine=None, sample=None):
    rows = []
    count = 0
    preview_bytes = 0
    sample_bytes = 0
    preview_full = False
    sample_header = True
    sample_first_line = True

    def sample_line(line, comment, continuation):
        nonlocal sample_bytes, sample_header, sample_first_line
        if sample is None or count >= CSV_NATIVE_SAMPLE_ROWS:
            return
        # Native metadata stops recognizing directives at the first non-header
        # physical line, including a blank. Keep that separator so later '#'
        # comments cannot become newly active directives in the sample.
        legacy_tags = sample_first_line and line.startswith("tags:")
        sample_first_line = False
        if comment and not sample_header:
            return
        was_header = sample_header
        if not comment and not legacy_tags:
            sample_header = False
        if not continuation and line in ("\n", "\r", "\r\n") and not was_header:
            return
        size = _utf8_size(line, CSV_SAMPLE_LIMIT - sample_bytes)
        sample_bytes += size
        if sample_bytes > CSV_SAMPLE_LIMIT:
            raise EngineError("invalid_params", "The CSV metadata sample exceeds 64 MiB.")
        sample.write(line)

    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            for row in _csv_records(file, delimiter, engine, on_line=sample_line):
                if engine is not None:
                    checkpoint(engine)
                if not row:
                    continue
                if len(row) > CSV_COLUMN_LIMIT or any(len(field) > CSV_FIELD_LIMIT for field in row):
                    raise ValueError()
                count += 1
                if engine is not None and count % 256 == 0:
                    engine.progress.report(done=count, label="Scanning CSV records")
                if not preview_full and len(rows) < 100:
                    size = 2 + max(0, len(row) - 1) * 2
                    for field in row:
                        size += len(json.dumps(field, ensure_ascii=True))
                        if preview_bytes + size > CSV_PREVIEW_LIMIT:
                            preview_full = True
                            break
                    if not preview_full:
                        rows.append(row)
                        preview_bytes += size
    except (UnicodeError, csv.Error, ValueError):
        raise EngineError("invalid_params", "The CSV is invalid or exceeds the column limits.") from None
    return rows, count


def _csv_delimiter(engine, path, params):
    """Match the pinned native delimiter selection without reading the upload whole."""
    selected = params.get("delimiter")
    metadata_bytes = 0
    forced = None
    names = {"tab": "\t", "pipe": "|", "semicolon": ";", "colon": ":", "comma": ",", "space": " "}
    try:
        with path.open("rb") as file:
            bom = b"\xef\xbb\xbf" if file.read(3) == b"\xef\xbb\xbf" else b""
        with path.open(encoding="utf-8-sig", newline="") as file:
            first = True
            while True:
                checkpoint(engine)
                line = file.readline(CSV_RECORD_LIMIT + 1)
                if len(line) > CSV_RECORD_LIMIT:
                    raise ValueError()
                if line.startswith("#") or (first and line.startswith("tags:")):
                    metadata_bytes += _utf8_size(line, CSV_METADATA_LIMIT - metadata_bytes)
                    if metadata_bytes > CSV_METADATA_LIMIT:
                        raise EngineError("invalid_params", "The CSV leading metadata exceeds 2 MiB.")
                    key, separator, value = line[1:].partition(":")
                    if separator and key.strip().lower() == "separator":
                        value = value.rstrip("\r\n")
                        end = 1
                        while end < len(value) and value[end].isascii() and value[end].isalnum():
                            end += 1
                        value = value[:end]
                        candidate = names.get(value.strip().lower(), value)
                        if candidate in DELIMITERS:
                            forced = candidate
                    first = False
                    continue
                # Native scans the first 8 KiB following leading metadata in
                # enum order. ASCII separators are identical in UTF-8 bytes.
                prefix = ((bom if not metadata_bytes else b"") +
                          (line[:8192] + file.read(max(0, 8192 - len(line)))).encode("utf-8"))[:8192]
                detected = next((char for char in DELIMITERS if char.encode() in prefix), " ")
                return selected if selected is not None else forced if forced is not None else detected
    except (UnicodeError, ValueError):
        raise EngineError("invalid_params", "The CSV is invalid or exceeds the record limits.") from None


def _metadata(engine, path, params):
    # Anki's metadata API reads its entire input into a String. Scan the upload
    # cooperatively and give it only the directives and first five records it
    # actually examines; the original upload remains the native import input.
    with tempfile.TemporaryDirectory(prefix="srs-csv-", dir=Path(engine.col.path).parent) as directory:
        sample_path = Path(directory) / "metadata.csv"
        with engine.progress.using_backend(None):
            engine.progress.report(label="Scanning CSV records")
            char = _csv_delimiter(engine, path, params)
            delimiter = _delimiter(char)
            with sample_path.open("w", encoding="utf-8", newline="") as sample:
                rows, count = _csv_rows(path, char, engine, sample)
            checkpoint(engine)
        metadata = engine.col.get_csv_metadata(str(sample_path), delimiter)
        checkpoint(engine)
    if len(metadata.column_labels) > CSV_COLUMN_LIMIT or any(len(label) > 255 for label in metadata.column_labels):
        raise EngineError("invalid_params", "The CSV column headers exceed the limits.")
    if "isHtml" in params:
        metadata.is_html = params["isHtml"]
    return metadata, char, rows, count


@method("transfer.inspectCsv")
def inspect_csv(engine, params):
    checkpoint(engine)
    path = resolve_upload(engine, params["uploadId"], {".csv", ".tsv", ".txt"})
    meta, delimiter, rows, count = _metadata(engine, path, params)
    checkpoint(engine)
    width = max([len(row) for row in rows] + [len(meta.column_labels)])
    mapping = []
    for ordinal, column in enumerate(meta.global_notetype.field_columns):
        if column:
            mapping.append({"column": column - 1, "target": "field", "fieldOrdinal": ordinal})
    for target, column in (("tags", meta.tags_column), ("deck", meta.deck_column),
                           ("notetype", meta.notetype_column), ("guid", meta.guid_column)):
        if column:
            mapping.append({"column": column - 1, "target": target, "fieldOrdinal": None})
    inspection = {"delimiter": delimiter, "columns": [meta.column_labels[i] if i < len(meta.column_labels) and meta.column_labels[i] else
            f"Column {i + 1}" for i in range(width)], "rows": rows, "totalRows": count,
            "notetypeId": meta.global_notetype.id or None, "deckId": meta.deck_id or None,
            "mapping": mapping, "isHtml": meta.is_html, "previewTruncated": count > len(rows),
            "warnings": ["The preview was shortened; the row count includes the complete upload."] if count > len(rows) else []}
    return mutation(engine.col, csvInspection=inspection)


@method("transfer.importCsv")
def import_csv(engine, params):
    checkpoint(engine)
    path = resolve_upload(engine, params["uploadId"], {".csv", ".tsv", ".txt"})
    meta, _, rows, _ = _metadata(engine, path, params)
    model = engine.col.models.get(params["notetypeId"])
    deck = engine.col.decks.get(params["deckId"], default=False)
    if model is None or deck is None:
        raise EngineError("not_found", "The selected note type or deck is unavailable.")
    width = max([len(row) for row in rows] + [len(meta.column_labels)])
    columns = [0] * len(model["flds"])
    seen_columns, seen_targets = set(), set()
    special_columns = {}
    meta.deck_column = meta.notetype_column = meta.tags_column = meta.guid_column = 0
    for item in params["mapping"]:
        column, target, ordinal = item["column"], item["target"], item["fieldOrdinal"]
        key = (target, ordinal if target == "field" else None)
        if column >= width or column in seen_columns or (target != "ignore" and key in seen_targets):
            raise EngineError("invalid_params", "The column mapping is invalid.")
        seen_columns.add(column)
        seen_targets.add(key)
        if target == "field":
            if ordinal is None or ordinal >= len(columns):
                raise EngineError("invalid_params", "The field mapping is invalid.")
            columns[ordinal] = column + 1
        elif target != "ignore":
            if ordinal is not None:
                raise EngineError("invalid_params", "Only fields accept field ordinals.")
            special_columns[target] = column + 1
    if not columns[0]:
        raise EngineError("invalid_params", "Map the first note field.")
    meta.global_notetype.id = params["notetypeId"]
    meta.global_notetype.field_columns[:] = columns
    meta.deck_id = params["deckId"]
    if "notetype" in special_columns and any(column not in (0, ordinal + 1)
                                              for ordinal, column in enumerate(columns)):
        raise EngineError("unsupported", "A note type column requires fields in their original order.")
    for target, column in special_columns.items():
        setattr(meta, target + "_column", column)
    meta.global_tags[:] = params["tags"]
    meta.updated_tags[:] = []
    meta.dupe_resolution = {"update": 0, "preserve": 1, "new": 2}[params["duplicateMode"]]
    meta.match_scope = {"notetype": 0, "notetypeAndDeck": 1}[params["matchScope"]]
    checkpoint(engine)
    before = _counts(engine.col, engine)
    checkpoint(engine)
    response = engine.col.import_csv(pb.ImportCsvRequest(path=str(path), metadata=meta))
    return _import_summary(engine.col, response, before, params["duplicateMode"])


def _limit(col, scope):
    deck_id, search = scope.get("deckId"), scope.get("search")
    if deck_id is not None and search is not None:
        raise EngineError("invalid_params", "Select a deck or a search, not both.")
    if deck_id is not None:
        deck = col.decks.get(deck_id, default=False)
        if deck is None:
            raise EngineError("not_found", "The selected deck is unavailable.")
        if scope.get("includeChildren", True):
            return DeckIdLimit(deck_id)
        return CardIdsLimit(col.db.list("select id from cards where did = ?", deck_id))
    if search is not None:
        return CardIdsLimit(col.find_cards(search))
    return None


@method("transfer.exportPackage")
def export_package(engine, params):
    checkpoint(engine)
    limit = _limit(engine.col, params["scope"])
    with export_destination(engine, "deck.apkg") as (path, publish):
        checkpoint(engine)
        count = engine.col.export_anki_package(out_path=str(path), limit=limit,
            options=pb.ExportAnkiPackageOptions(with_scheduling=params["includeScheduling"],
                with_media=params["includeMedia"], with_deck_configs=params["includeDeckConfigs"],
                legacy=params.get("legacy", False)))
        return mutation(engine.col, summary=f"Exported {count} notes.", file=publish())


@method("transfer.exportCollection")
def export_collection(engine, params):
    checkpoint(engine)
    with export_destination(engine, "collection.colpkg") as (path, publish):
        try:
            checkpoint(engine)
            engine.col.export_collection_package(str(path), params["includeMedia"], params.get("legacy", False))
        finally:
            if engine.col.db is None:
                engine.col.reopen()
                engine.col._load_scheduler()
                initialize_scheduling(engine.col)
        return mutation(engine.col, summary="Collection exported.", file=publish())


@method("transfer.exportCsv")
def export_csv(engine, params):
    checkpoint(engine)
    _delimiter(params["delimiter"])
    limit = _limit(engine.col, params["scope"])
    with export_destination(engine, "notes.csv" if params["mode"] == "notes" else "cards.csv") as (path, publish):
        checkpoint(engine)
        if params["mode"] == "notes":
            count = engine.col.export_note_csv(out_path=str(path), limit=limit, with_html=params["includeHtml"],
                with_tags=params["includeTags"], with_guid=params["includeGuid"],
                with_deck=params["includeDeck"], with_notetype=params["includeNotetype"])
        else:
            if any(params[key] for key in ("includeTags", "includeGuid", "includeDeck", "includeNotetype")):
                raise EngineError("unsupported", "Card text exports support HTML and delimiter options only.")
            count = engine.col.export_card_csv(out_path=str(path), limit=limit, with_html=params["includeHtml"])
        if params["delimiter"] != "\t":
            # Native exports use tab-separated text with importer directives.
            # Convert with a streaming CSV parser so quoted multiline fields survive.
            converted = path.with_name("converted.csv")
            with path.open(encoding="utf-8", newline="") as source, converted.open("w", encoding="utf-8", newline="") as dest:
                writer = csv.writer(dest, delimiter=params["delimiter"], lineterminator="\n", quoting=csv.QUOTE_ALL)
                first_comment = True

                def preserve_directive(line):
                    nonlocal first_comment
                    if first_comment and line.rstrip("\r\n") == "#separator:tab":
                        name = {",": "comma", ";": "semicolon", "|": "pipe", ":": "colon", " ": "space"}[params["delimiter"]]
                        line = "#separator:" + name + "\n"
                    first_comment = False
                    dest.write(line)

                # Native exports quote every data field, including values that
                # begin '#'. Keep that distinction from physical directive lines.
                for row in _csv_records(source, "\t", engine, preserve_directive):
                    checkpoint(engine)
                    writer.writerow(row)
            converted.replace(path)
        return mutation(engine.col, summary=f"Exported {count} {params['mode']}.", file=publish())
