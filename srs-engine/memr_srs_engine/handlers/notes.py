"""Note editing delegates field checks, card generation and undo to Anki."""
import copy
import re
import tempfile
import unicodedata
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import quote
from ..provenance_links import first_source, parse_source_uri

from ..errors import EngineError
from ..collection import Collection, mutation_result, undo_state
from ..registry import method
from .decks import _deck, _result
from anki.notes import NoteFieldsCheckResult
from anki.collection import AddNoteRequest, StripHtmlMode
from anki.config import Config
from anki.search_pb2 import SearchNode
from anki.utils import to_json_bytes

ADDING_DEFAULTS = "canonnotesAddingDefaults"
FIELD_STATES = {NoteFieldsCheckResult.NORMAL: "ok", NoteFieldsCheckResult.EMPTY: "empty",
                NoteFieldsCheckResult.DUPLICATE: "duplicate", NoteFieldsCheckResult.MISSING_CLOZE: "missingCloze",
                NoteFieldsCheckResult.NOTETYPE_NOT_CLOZE: "notetypeNotCloze",
                NoteFieldsCheckResult.FIELD_NOT_CLOZE: "fieldNotCloze"}


def _text(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise EngineError("invalid_params", "Text must contain valid Unicode characters.") from None
    return value


def _model(col, model_id):
    model = col.models.get(int(model_id)) if model_id else None
    if model is None:
        raise EngineError("not_found", "The requested note type is unavailable.")
    return model


def _note(col, note_id):
    # Note(id=0) creates a new note instead of fetching one in Anki's Python API.
    if not note_id:
        raise EngineError("not_found", "The requested note is unavailable.")
    return col.get_note(int(note_id))


def _selection(col, note_ids, progress=None):
    if not note_ids:
        raise EngineError("invalid_params", "Select at least one note.")
    selected = list(dict.fromkeys(map(int, note_ids)))
    notes = []
    for index, note_id in enumerate(selected):
        if progress is not None:
            progress.check_abort()
        notes.append(_note(col, note_id))
        if progress is not None:
            progress.report(index + 1, len(selected), "Checking selected notes")
    return notes


def _selected_ids(engine, note_ids):
    selected = list(dict.fromkeys(map(int, note_ids)))
    if not selected:
        raise EngineError("invalid_params", "Select at least one note.")
    for offset in range(0, len(selected), 512):
        engine.progress.check_abort()
        batch = selected[offset:offset + 512]
        placeholders = ",".join("?" for _ in batch)
        if engine.col.db.scalar(f"select count(*) from notes where id in ({placeholders})", *batch) != len(batch):
            raise EngineError("not_found", "A selected note is unavailable.")
        engine.progress.report(min(offset + 512, len(selected)), len(selected), "Checking selected notes")
    return selected


def _fields(model, fields):
    if len(fields) != len(model["flds"]):
        raise EngineError("invalid_params", "The field count must match the note type.")
    for field in fields:
        _text(field)
        if len(field) > 1048576:
            raise EngineError("invalid_params", "A field exceeds the supported text size.")
        if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in field):
            raise EngineError("invalid_params", "Fields must not contain unsupported control characters.")
    # Anki parses u16 ordinals but clamps generated cards above c500 to c500.
    # Reject that silent alias while retaining comma lists and nested clozes.
    if model["type"] == 1 and model.get("originalStockKind") != 6:
        for field in fields:
            for match in re.finditer(r"\{\{c([0-9,]+)::", field):
                if any(not number or len(number) > 3 or not 1 <= int(number) <= 500
                       for number in match[1].split(",")):
                    raise EngineError("invalid_params", "Cloze numbers must be between 1 and 500.")
    return list(fields)


def _tag_name(value, allow_empty=False):
    _text(value)
    value = unicodedata.normalize("NFC", value)
    if allow_empty and value == "":
        return value
    if (not value or len(value) > 1024 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or any(not part for part in value.split("::")) or value.count("::") > 100):
        raise EngineError("invalid_params", "Tags must have nonempty components without spaces or control characters.")
    return value


def _tags(values):
    result = list(dict.fromkeys(_tag_name(value) for value in values))
    if len(result) > 1000:
        raise EngineError("invalid_params", "A note supports at most 1000 tags.")
    return result


def _tag_equal(col, left, right):
    if left == right:
        return True
    # casefold is only a candidate filter. Anki's native collation keeps, for
    # example, the ligature 'ﬃ' distinct from 'ffi'.
    return left.casefold() == right.casefold() and bool(col.db.scalar("select ? = ? collate unicase", left, right))


def _source(note):
    return first_source(note)


def _set_source(note, source):
    if "Source" not in note:
        if source is not None:
            raise EngineError("unsupported", "This note type has no Source field.")
        return
    if source is None:
        note["Source"] = ""
        note.remove_tag("canonnotes::from-note")
        return
    for value in (source["pageId"], source["blockId"], source["revision"], source["uri"]):
        if value is not None:
            _text(value)
    if not source["pageId"] or source["blockId"] == "" or (source["revision"] is not None
            and not re.fullmatch(r"[a-fA-F0-9]{64}", source["revision"])):
        raise EngineError("invalid_params", "The source link is invalid.")
    uri = "canonnotes:page/" + quote(source["pageId"], safe="")
    query = [key + "=" + quote(value, safe="")
             for key, value in (("block", source["blockId"]), ("rev", source["revision"])) if value is not None]
    if query:
        uri += "?" + "&".join(query)
    if uri != source["uri"] or len(uri) > 2048:
        raise EngineError("invalid_params", "The source URI must match its page, block and revision.")
    note["Source"] = uri
    if not note.has_tag("canonnotes::from-note"):
        note.add_tag("canonnotes::from-note")


def _serialize(note):
    return {"id": int(note.id), "guid": note.guid, "notetypeId": int(note.mid),
            "fields": list(note.fields), "tags": list(note.tags), "cardIds": list(note.card_ids()),
            "modifiedAt": int(note.mod), "source": _source(note)}


def _check(col, note):
    status = FIELD_STATES[note.fields_check()]
    duplicates = []
    if status == "duplicate":
        first = note.fields[0]
        if col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT):
            first = unicodedata.normalize("NFC", first)
        first = col._backend.strip_html(text=first, mode=StripHtmlMode.PRESERVE_MEDIA_FILENAMES)
        checksum = int.from_bytes(hashlib.sha1(first.encode()).digest()[:4], "big")
        # The search-string representation collapses whitespace in complex
        # HTML. Use Anki's indexed checksum plus its native stripped equality.
        candidates = col.db.list("select id from notes where mid=? and csum=? order by id", note.mid, checksum)
        duplicates = [int(nid) for nid in candidates if nid != note.id and
                      col._backend.strip_html(text=col.get_note(nid).fields[0],
                                             mode=StripHtmlMode.PRESERVE_MEDIA_FILENAMES) == first]
    return {"status": status, "duplicateNoteIds": duplicates}


def _canonical_sources(value):
    if len(value) > 1048576:
        raise EngineError("invalid_params", "The Source field exceeds its supported size.")
    links = []
    for line in value.splitlines():
        if not line.strip():
            continue
        parsed = parse_source_uri(line.strip())
        if parsed is None:
            raise EngineError("invalid_params", "Every Source line must be a valid Memr source link.")
        uri = "canonnotes:page/" + quote(parsed["pageId"], safe="")
        query = [(key, value) for key, value in (("block", parsed["blockId"]),
                 ("rev", parsed["revision"].lower() if parsed["revision"] else None)) if value is not None]
        if query:
            uri += "?" + "&".join(key + "=" + quote(value, safe="") for key, value in query)
        if len(uri) > 2048:
            raise EngineError("invalid_params", "A canonical source link exceeds its supported size.")
        links.append(uri)
        if len(links) > 1000:
            raise EngineError("invalid_params", "A note supports at most 1000 source links.")
    if not links:
        raise EngineError("invalid_params", "Source de-duplication requires a source link.")
    canonical = "\n".join(sorted(set(links)))
    if len(canonical) > 1048576:
        raise EngineError("invalid_params", "The canonical Source field exceeds its supported size.")
    return canonical


def _source_deduplication(col, model, note):
    """Run inside the serialized add operation, before any native mutation.

    Only this opt-in path canonicalizes Source lines. Existing notes keep
    their fields, tags, cards, scheduling and native undo history verbatim.
    """
    source_fields = [index for index, field in enumerate(model["flds"]) if field["name"] == "Source"]
    if len(source_fields) != 1:
        raise EngineError("unsupported", "Source de-duplication requires one Source field.")
    ordinal = source_fields[0]
    note.fields[ordinal] = _canonical_sources(note.fields[ordinal])
    note.add_tag("canonnotes::from-note")
    if note.fields_check() not in (NoteFieldsCheckResult.NORMAL, NoteFieldsCheckResult.DUPLICATE):
        raise EngineError("invalid_params", "The note has empty content or invalid cloze fields.")
    normalize_enabled = col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT)
    normalize = (lambda value: unicodedata.normalize("NFC", value)) if normalize_enabled else (lambda value: value)
    expected = [normalize(value) for value in note.fields]
    # Bound both result count and bytes before loading potentially large fields
    # from an imported collection. No application-owned table or cache is used.
    query = "select id,length(cast(flds as blob)) from notes where mid=?"
    arguments = [int(note.mid)]
    if ordinal != 0:
        # Native first-field checksums narrow the search without requiring old
        # imported Source fields to have our canonical order or URL encoding.
        first = col._backend.strip_html(text=expected[0], mode=StripHtmlMode.PRESERVE_MEDIA_FILENAMES)
        checksum = int.from_bytes(hashlib.sha1(first.encode()).digest()[:4], "big")
        if normalize_enabled:
            # Imported first fields may have a checksum from before NFC was
            # enabled. Any non-NFC field contains non-ASCII code points, so
            # include those candidates instead of guessing Unicode variants.
            query += " and (csum=? or field_at_index(flds,0) glob ?)"
            arguments.extend([checksum, "*[^\x01-\x7f]*"])
        else:
            query += " and csum=?"
            arguments.append(checksum)
    candidates = col.db.all(query + " order by id limit 10001", *arguments)
    if len(candidates) > 10000:
        raise EngineError("unsupported", "Too many candidate notes to de-duplicate safely.")
    compared_bytes = 0
    for note_id, field_bytes in candidates:
        compared_bytes += field_bytes
        if compared_bytes > 16 * 1024 * 1024:
            raise EngineError("unsupported", "Source de-duplication exceeds its bounded field-read size.")
        existing = col.get_note(note_id)
        fields = list(existing.fields)
        try:
            fields[ordinal] = _canonical_sources(fields[ordinal])
        except EngineError:
            continue
        if [normalize(value) for value in fields] == expected:
            return existing
    return None


@method("notes.get")
def get(engine, params):
    return _serialize(_note(engine.col, params["noteId"]))


@method("notes.fieldsCheck")
def fields_check(engine, params):
    col = engine.col
    model = _model(col, params["notetypeId"])
    if params.get("noteId") is not None:
        note = _note(col, params["noteId"])
        if note.mid != model["id"]:
            raise EngineError("invalid_params", "The note must belong to the requested note type.")
    else:
        note = col.new_note(model)
    note.fields = _fields(model, params["fields"])
    return _check(col, note)


@method("notes.add")
def add(engine, params):
    col = engine.col
    model = _model(col, params["notetypeId"])
    if "expectedFieldNames" in params and params["expectedFieldNames"] != [field["name"] for field in model["flds"]]:
        raise EngineError("conflict", "The note type's fields changed. Reload the preview before adding.")
    deck = _deck(col, int(params["deckId"]))
    if deck["dyn"]:
        raise EngineError("invalid_params", "Notes must be added to a normal deck.")
    note = col.new_note(model)
    note.fields, note.tags = _fields(model, params["fields"]), _tags(params["tags"])
    if "source" in params:
        _set_source(note, params["source"])
    deduplicate = params.get("deduplicateBySource", False)
    if deduplicate:
        existing = _source_deduplication(col, model, note)
        if existing is not None:
            return {**mutation_result(undo_state(col)), "noteId": int(existing.id),
                    "cardIds": list(existing.card_ids()), "duplicateState": "duplicate",
                    "duplicateNoteIds": [int(existing.id)], "deduplicated": True}
    note.tags = _tags(note.tags)
    checked = _check(col, note)
    if checked["status"] == "duplicate" and not (params.get("allowDuplicate", False) or deduplicate):
        raise EngineError("conflict", "A note with the same first field already exists.")
    if checked["status"] not in ("ok", "duplicate"):
        raise EngineError("invalid_params", "The note has empty content or invalid cloze fields.")
    changes = col.add_note(note, int(params["deckId"]))
    return _result(col, changes.changes, noteId=int(note.id), cardIds=list(note.card_ids()),
                   duplicateState=checked["status"], duplicateNoteIds=checked["duplicateNoteIds"],
                   **({"deduplicated": False} if deduplicate else {}))


@method("notes.update")
def update(engine, params):
    col = engine.col
    note = _note(col, params["noteId"])
    if "expectedModifiedAt" in params and int(params["expectedModifiedAt"]) != note.mod:
        raise EngineError("conflict", "The note changed after it was read. Reload it before saving.")
    original_fields, original_tags = list(note.fields), list(note.tags)
    note.fields, note.tags = _fields(note.note_type(), params["fields"]), _tags(params["tags"])
    if "source" in params:
        _set_source(note, params["source"])
    note.tags = _tags(note.tags)
    if note.fields == original_fields:
        if note.tags == original_tags:
            return {**mutation_result(undo_state(col)), "noteId": int(note.id)}
        result = _set_tags(engine, [note.id], note.tags, "replace")
        result.pop("count")
        return {**result, "noteId": int(note.id)}
    if col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT) and any(
            unicodedata.normalize("NFC", field) != field for field in note.fields):
        raise EngineError("unsupported", "The collection's text-normalization preference would change supplied field bytes. "
                          "Submit NFC-normalized fields or explicitly change that preference before retrying; no changes were saved.")
    return _result(col, col.update_note(note), noteId=int(note.id))


@method("notes.remove")
def remove(engine, params):
    col = engine.col
    with engine.progress.using_backend(None):
        selected = _selected_ids(engine, params["noteIds"])
    engine.progress.check_abort()
    changes = col.remove_notes(selected)
    # Anki counts removed cards here; the RPC counts removed notes.
    return _result(col, changes.changes, count=len(selected))


def _check_abort(progress):
    if progress is not None:
        progress.check_abort()


def _set_tags(engine, note_ids, tags, mode, progress=None):
    # A bounded notes.update reuses native tag-only commits without joining or
    # resetting the cancellation state of an earlier long operation.
    col = engine.col
    with progress.using_backend(None) if progress is not None else nullcontext():
        notes = _selection(col, note_ids, progress)
        before = {note.id: list(note.tags) for note in notes}
        def groups(values):
            grouped = {}
            for tag in values:
                grouped.setdefault(tag.casefold(), []).append(tag)
            return grouped

        def contains(grouped, tag):
            _check_abort(progress)
            return any(_tag_equal(col, tag, existing) for existing in grouped.get(tag.casefold(), []))

        desired = groups(tags)
        subsets, supersets, unwanted = True, True, {}
        for index, note in enumerate(notes):
            _check_abort(progress)
            current = groups(note.tags)
            if mode == "replace":
                for tag in note.tags:
                    if not contains(desired, tag):
                        subsets = False
                        unwanted[tag] = None
                if not all(contains(current, tag) for tag in tags):
                    supersets = False
                note.tags = list(tags)
            elif mode == "add":
                for tag in tags:
                    _check_abort(progress)
                    if not contains(current, tag):
                        note.tags.append(tag)
                        current.setdefault(tag.casefold(), []).append(tag)
            else:
                remaining = []
                for tag in note.tags:
                    _check_abort(progress)
                    if not contains(desired, tag):
                        remaining.append(tag)
                note.tags = remaining
            note.tags = _tags(note.tags)
            if progress is not None:
                progress.report(index + 1, len(notes), "Preparing note tags")
        selected = [note.id for note in notes]
        original = [before[note.id] for note in notes]
        if mode == "add" or mode == "replace" and subsets:
            commit, arguments = col.tags.bulk_add, (selected, " ".join(tags))
        elif mode == "remove":
            pattern = "^(?:" + "|".join(re.escape(tag) for tag in tags) + ")$"
            _preflight_tag_regex(pattern, "", progress)
            commit, arguments = col.tags.find_and_replace, (selected, pattern, "", True, False)
        elif not tags:
            commit, arguments = col.tags.find_and_replace, (selected, "(?s)^.+$", "", True, False)
        elif supersets:
            pattern = "^(?:" + "|".join(re.escape(tag) for tag in unwanted) + ")$"
            _preflight_tag_regex(pattern, "", progress)
            commit, arguments = col.tags.find_and_replace, (selected, pattern, "", True, False)
        elif len(tags) == 1 and all(original):
            replacement = tags[0].replace("$", "$$")
            _preflight_tag_regex("(?s)^.+$", replacement, progress)
            commit, arguments = col.tags.find_and_replace, (selected, "(?s)^.+$", replacement, True, False)
        else:
            _preflight_tag_updates(engine, notes, before, progress)
            commit, arguments = None, None
    _check_abort(progress)
    if commit is not None:
        changed = commit(*arguments)
        return _result(col, changed.changes, count=changed.count)
    changes = col.update_notes(notes)
    count = sum(before[note.id] != col.get_note(note.id).tags for note in notes)
    return _result(col, changes, count=count)


def _preflight_tag_regex(pattern, replacement, progress=None):
    """A rejected native regex must not clear the active collection's undo."""
    with tempfile.TemporaryDirectory(prefix="memr-srs-tag-regex-") as directory:
        scratch = Collection(str(Path(directory) / "collection.anki2"))
        try:
            with progress.using_backend(scratch) if progress is not None else nullcontext():
                probe = scratch.new_note(scratch.models.by_name("Basic"))
                probe.fields, probe.tags = ["Native tag validation", ""], ["validation"]
                scratch.add_note(probe, 1)
                scratch.tags.find_and_replace([], pattern, replacement, True, False)
                _check_abort(progress)
        finally:
            scratch.close()


def _preflight_tag_updates(engine, notes, before, progress=None):
    """Prove a general replacement cannot normalize fields or add cards.

    Native full-note updates regenerate cards even for tag-only edits. Existing
    normal types with every template card need only a native field-normalization
    probe; other notes also need their actual required ordinals checked.
    """
    with tempfile.TemporaryDirectory(prefix="memr-srs-tag-update-") as directory:
        scratch = Collection(str(Path(directory) / "collection.anki2"))
        try:
            with progress.using_backend(scratch) if progress is not None else nullcontext():
                normalize = engine.col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT)
                scratch.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
                models = {}
                for index, note in enumerate(notes):
                    _check_abort(progress)
                    original = note.note_type()
                    existing = set(engine.col.db.list("select ord from cards where nid=?", note.id))
                    if not existing:
                        raise EngineError("unsupported", "A tag-only replacement requires an existing card. "
                                          "Use separate notes.setTags add and remove requests; those requests are not jointly atomic.")
                    complete = original["type"] == 0 and set(range(len(original["tmpls"]))).issubset(existing)
                    key = (note.mid, complete)
                    if key not in models:
                        model = copy.deepcopy(original)
                        model["id"], model["originalId"] = 0, None
                        if complete:
                            model["tmpls"] = copy.deepcopy(scratch.models.by_name("Basic")["tmpls"])
                            model["tmpls"][0].update(qfmt="Native field validation", afmt="Native field validation")
                            model["originalStockKind"] = 1
                        for template in model["tmpls"]:
                            template["did"] = None
                        model_id = scratch._backend.add_or_update_notetype(
                            json=to_json_bytes(model), preserve_usn_and_mtime=True, skip_checks=True)
                        models[key] = scratch.models.get(model_id)
                    probe = scratch.new_note(models[key])
                    probe.fields, probe.tags = list(note.fields), before[note.id]
                    step = scratch.add_custom_undo_entry("Validate tag-only replacement")
                    scratch.add_note(probe, 1)
                    # New-note creation forces ord0 when every front is empty.
                    # Retain one actual existing ordinal before the native
                    # update, which uses existing-note generation semantics.
                    cards = probe.cards()
                    if len(cards) > 1:
                        scratch.remove_cards_and_orphaned_notes([card.id for card in cards[1:]])
                    cards[0].ord = min(existing)
                    scratch.update_card(cards[0])
                    scratch.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, normalize)
                    probe.fields, probe.tags = list(note.fields), list(note.tags)
                    scratch.update_note(probe)
                    fields = scratch.get_note(probe.id).fields
                    required = set(scratch.db.list("select ord from cards where nid=?", probe.id))
                    if fields != note.fields or not complete and not required.issubset(existing):
                        raise EngineError("unsupported", "This replacement cannot be validated as preserving stored field bytes and existing cards. "
                                          "Use separate notes.setTags add and remove requests; those requests are not jointly atomic.")
                    # Group the disposable seed/update so one native undo also
                    # restores the unnormalized seed preference for the next
                    # note and bounds scratch history to the current probe.
                    scratch.merge_undo_entries(step)
                    scratch.undo()
                    _check_abort(progress)
                    if progress is not None:
                        progress.report(index + 1, len(notes), "Checking tag-only replacement")
        finally:
            scratch.close()


@method("notes.mark")
def mark(engine, params):
    return _set_tags(engine, params["noteIds"], ["marked"],
                     "add" if params["marked"] else "remove", engine.progress)


@method("notes.setTags")
def set_tags(engine, params):
    return _set_tags(engine, params["noteIds"], _tags(params["tags"]), params["mode"], engine.progress)


@method("notes.findDuplicates")
def find_duplicates(engine, params):
    col = engine.col
    with engine.progress.using_backend(None):
        name, search = _text(params["fieldName"]), _text(params.get("search", ""))
        if not name:
            raise EngineError("invalid_params", "Choose a field to check.")
        models = col.models.all()
        if not any(_tag_equal(col, field["name"], name) for model in models for field in model["flds"]):
            raise EngineError("not_found", "The requested field is unavailable.")
        engine.progress.check_abort()
    # Native search runs on the active backend so its abort lane remains live.
    selected = col.find_notes(col.build_search_string(search, SearchNode(field_name=name)))
    with engine.progress.using_backend(None):
        values = {}
        ordinals = {model["id"]: next((index for index, field in enumerate(model["flds"])
                    if _tag_equal(col, field["name"], name)), None) for model in models}
        for index, note_id in enumerate(selected):
            engine.progress.check_abort()
            note = col.get_note(note_id)
            ordinal = ordinals.get(note.mid)
            if ordinal is None:
                continue
            text = col._backend.strip_html(text=note.fields[ordinal], mode=StripHtmlMode.PRESERVE_MEDIA_FILENAMES)
            if text:
                values.setdefault(text, []).append(int(note_id))
            engine.progress.report(index + 1, len(selected), "Checking duplicate fields")
        groups = [{"text": text, "noteIds": sorted(ids)} for text, ids in values.items() if len(ids) > 1]
        if len(groups) > 10000 or any(len(group["text"]) > 65536 or len(group["noteIds"]) > 100000 for group in groups):
            raise EngineError("unsupported", "The duplicate result exceeds the supported response size.")
        engine.progress.check_abort()
    return {**mutation_result(undo_state(col)), "groups": groups}


@method("notes.findReplace")
def find_replace(engine, params):
    col = engine.col
    progress = engine.progress
    with progress.using_backend(None):
        selected = _selected_ids(engine, params["noteIds"])
        for key in ("search", "replacement"):
            _text(params[key])
        name = params["fieldName"]
        if name is not None:
            _text(name)
            if not name:
                raise EngineError("invalid_params", "Choose a field or all fields.")
            progress.check_abort()
            if not any(_tag_equal(col, name, field) for field in col.field_names_for_note_ids(selected)):
                raise EngineError("not_found", "The requested field is unavailable on the selected notes.")
        _fields({"flds": [None], "type": 0}, [params["replacement"]])
        _preflight_replace(engine, params, selected)
    # This is the only active write. Cancellation after its commit must not
    # replace a completed result with an aborted outcome.
    progress.check_abort()
    changes = col.find_and_replace(note_ids=selected, search=params["search"],
                                   replacement=params["replacement"], regex=params["regex"],
                                   match_case=params["matchCase"], field_name=name)
    return _result(col, changes.changes, count=changes.count)


def _preflight_replace(engine, params, selected):
    """Validate Rust regexes and possible field overflow without active writes.

    Most fields cannot reach the public 1 MiB limit even under the worst-case
    replacement expansion. Only risky fields need native scratch evaluation.
    An empty probe still validates regex syntax before active undo is touched.
    """
    # Bounded scratch batches avoid holding every selected note or risky field
    # in memory. The empty probe validates the Rust regex even with no matches.
    _probe_replace(engine, params, [("", False)])
    candidates, expansion = [], 0
    for index, note_id in enumerate(selected):
        engine.progress.check_abort()
        note = engine.col.get_note(note_id)
        model = note.note_type()
        cloze = model["type"] == 1 and model.get("originalStockKind") != 6
        for name, field in note.items():
            if params["fieldName"] is not None and not _tag_equal(engine.col, name, params["fieldName"]):
                continue
            bound = len(field) + (2 * len(field) + 1) * len(params["replacement"])
            normalized_bound = bound * 3
            if normalized_bound > 8 * 1048576:
                _probe_replace_isolated(engine, params, field, cloze)
                continue
            if normalized_bound > 1048576 or cloze:
                if candidates and expansion + normalized_bound > 8 * 1048576:
                    _probe_replace(engine, params, candidates)
                    candidates, expansion = [], 0
                candidates.append((field, cloze))
                expansion += normalized_bound
                if len(candidates) >= 64:
                    _probe_replace(engine, params, candidates)
                    candidates, expansion = [], 0
        engine.progress.report(index + 1, len(selected), "Validating replacement fields")
    if candidates:
        _probe_replace(engine, params, candidates)


def _probe_replace(engine, params, candidates):
    engine.progress.check_abort()
    with tempfile.TemporaryDirectory(prefix="memr-srs-replace-") as directory:
        scratch = Collection(str(Path(directory) / "collection.anki2"))
        try:
            with engine.progress.using_backend(scratch):
                _validate_replacement(scratch, params, candidates,
                                      engine.col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT),
                                      engine.progress.check_abort)
        finally:
            scratch.close()


def _validate_replacement(scratch, params, candidates, normalize, check_abort):
    # Seed original bytes first. Imported fields may predate the
    # collection's current normalization preference.
    scratch.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
    model = scratch.models.by_name("Basic")
    requests = []
    for field, cloze in candidates:
        check_abort()
        note = scratch.new_note(model)
        note.fields = [field, ""]
        requests.append((AddNoteRequest(note=note, deck_id=1), cloze))
    scratch.add_notes([request for request, _cloze in requests])
    check_abort()
    scratch.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, normalize)
    scratch.find_and_replace(note_ids=[request.note.id for request, _cloze in requests], search=params["search"],
                             replacement=params["replacement"], regex=params["regex"],
                             match_case=params["matchCase"], field_name="Front")
    check_abort()
    for request, cloze in requests:
        check_abort()
        fields = scratch.get_note(request.note.id).fields
        if fields != request.note.fields:
            validation_model = {"flds": model["flds"], "type": 1 if cloze else 0}
            _fields(validation_model, fields)


REPLACEMENT_PROBE = r"""
import json, pathlib, resource, sys
sys.path.insert(0, sys.argv[1])
from memr_srs_engine.handlers.notes import _validate_replacement
from memr_srs_engine.errors import public_error
from anki.collection import Collection
directory = pathlib.Path(sys.argv[2])
payload = json.loads((directory / 'input.json').read_text())
col = Collection(str(directory / 'collection.anki2'))
try:
    # Add a fixed allowance to the initialized backend's virtual address space.
    # A native allocator failure remains confined to this disposable process.
    baseline = int(pathlib.Path('/proc/self/statm').read_text().split()[0]) * resource.getpagesize()
    ceiling = baseline + 256 * 1024 * 1024
    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
    if hard != resource.RLIM_INFINITY:
        ceiling = min(ceiling, hard)
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 21))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _validate_replacement(col, payload['params'], [(payload['field'], payload['cloze'])], payload['normalize'], lambda: None)
    print(json.dumps({'ok': True}), flush=True)
except Exception as error:
    print(json.dumps({'error': public_error(error).as_dict()}), flush=True)
finally:
    col.close()
"""


def _probe_replace_isolated(engine, params, field, cloze=False):
    """Run potentially huge native expansion outside the live engine process."""
    engine.progress.check_abort()
    if not sys.platform.startswith("linux"):
        raise EngineError("unsupported", "This replacement needs isolated native validation unavailable on this platform.")
    with tempfile.TemporaryDirectory(prefix="memr-srs-replace-isolated-") as directory:
        root = Path(directory)
        payload = {"params": {key: params[key] for key in ("search", "replacement", "regex", "matchCase")},
                   "field": field, "cloze": cloze,
                   "normalize": engine.col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT)}
        (root / "input.json").write_text(json.dumps(payload), encoding="utf-8")
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR")
               if key in os.environ}
        with (root / "output.json").open("wb") as output, (root / "stderr.log").open("wb") as errors:
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", REPLACEMENT_PROBE,
                                        str(Path(__file__).resolve().parents[2]), str(root)],
                                       cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    engine.progress.check_abort()
                    if time.monotonic() - started > 30:
                        raise EngineError("unsupported", "Native replacement validation exceeded its isolated time budget.")
                    try:
                        process.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
        if process.returncode:
            raise EngineError("unsupported", "Native replacement validation exceeded its isolated resource budget.")
        try:
            with (root / "output.json").open("rb") as output:
                result = json.loads(output.read(65537))
            if result == {"ok": True}:
                return
            error = result["error"]
            if set(error) - {"code", "message", "data"} or error["code"] not in {
                    "invalid_params", "not_found", "busy", "conflict", "unsupported", "aborted", "internal"}:
                raise ValueError("Invalid isolated validation result")
            raise EngineError(error["code"], error["message"], error.get("data"))
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise EngineError("internal", "Isolated replacement validation returned an invalid result.") from None


@method("notes.addHistory")
def add_history(engine, params):
    notes = engine.col.db.list("select id from notes order by id desc limit ?", int(params.get("limit", 100)))
    return {"notes": [_serialize(engine.col.get_note(nid)) for nid in notes]}


@method("notes.defaultsForAdding")
def defaults_for_adding(engine, params):
    col = engine.col
    native = col.defaults_for_adding(current_review_card=None)
    saved = _saved_defaults(col)
    model_id = params.get("notetypeId", saved.get("notetypeId", native.notetype_id))
    if "notetypeId" not in params and not col.models.get(model_id):
        model_id = native.notetype_id
    model = _model(col, model_id)
    deck_id = params.get("deckId", saved.get("deckId", native.deck_id))
    if "deckId" not in params and not col.decks.get(deck_id, default=False):
        deck_id = native.deck_id
    deck = _deck(col, int(deck_id))
    if deck["dyn"]:
        raise EngineError("invalid_params", "The add editor requires a normal deck.")
    stored = _saved_entry(engine, model, saved["byNotetype"].get(str(model["id"]), {}))
    fields = ["" for _ in model["flds"]]
    sticky = [index for index, field in enumerate(model["flds"]) if field["sticky"]]
    if stored.get("fieldNames") == [field["name"] for field in model["flds"]]:
        sticky = stored["stickyFields"]
        for index in sticky:
            fields[index] = stored["fields"][index]
    return {"notetypeId": int(model["id"]), "deckId": int(deck_id), "fields": fields,
            "stickyFields": sticky, "tags": stored.get("tags", [])}


@method("notes.setAddingDefaults")
def set_adding_defaults(engine, params):
    col = engine.col
    model = _model(col, params["notetypeId"])
    deck = _deck(col, int(params["deckId"]))
    if deck["dyn"]:
        raise EngineError("invalid_params", "The add editor requires a normal deck.")
    fields, tags = _fields(model, params["fields"]), _tags(params["tags"])
    sticky = list(dict.fromkeys(map(int, params["stickyFields"])))
    if any(index >= len(fields) for index in sticky):
        raise EngineError("invalid_params", "Sticky field indices must belong to the note type.")
    saved = _saved_defaults(col)
    saved.update(notetypeId=int(model["id"]), deckId=int(params["deckId"]))
    saved.setdefault("byNotetype", {})[str(model["id"])] = {
        "fieldNames": [field["name"] for field in model["flds"]], "fields": fields,
        "stickyFields": sticky, "tags": tags}
    return _result(col, col.set_config(ADDING_DEFAULTS, saved, undoable=True))


def _saved_defaults(col):
    saved = copy.deepcopy(col.get_config(ADDING_DEFAULTS, {}))
    if type(saved) is not dict:
        saved = {}
    if type(saved.get("byNotetype")) is not dict:
        saved["byNotetype"] = {}
    for key in ("notetypeId", "deckId"):
        value = saved.get(key)
        if type(value) is not int or not 1 <= value <= 9007199254740991:
            saved.pop(key, None)
    return saved


def _saved_entry(engine, model, stored):
    from ..validate import validate_value

    if type(stored) is not dict or stored.get("fieldNames") != [field["name"] for field in model["flds"]]:
        return {}
    candidate = {"deckId": 1, "notetypeId": int(model["id"]),
                 **{key: stored.get(key) for key in ("fields", "stickyFields", "tags")}}
    if validate_value(engine.registry.catalogue["notes.setAddingDefaults"]["params"], candidate):
        return {}
    try:
        _fields(model, candidate["fields"])
        _tags(candidate["tags"])
        if any(index >= len(candidate["fields"]) for index in candidate["stickyFields"]):
            return {}
    except EngineError:
        return {}
    return stored
