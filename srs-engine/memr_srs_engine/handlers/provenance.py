"""Native note-type setup and read-only source lookups; canonical pages stay outside Anki."""
from copy import deepcopy
import json
import re

from ..collection import mutation_result, undo_state
from ..errors import EngineError
from ..provenance_links import MAX_SOURCE_BYTES, source_links, valid_identity
from ..registry import method
from .decks import _result
from .notetypes import _preflight, _stock

OWNER_KEY = "canonnotesProvenance"
INDEX_KEY = "canonnotes.provenanceNotetypes"
SCHEMA_VERSION = 1
# Leave half of the protocol's 32 MiB frame budget for its envelope and other
# transport metadata; reject oversized collections before building that frame.
MAX_CHECK_SOURCES_BYTES = 16 * 1024 * 1024
NOTETYPES = (
    ("basic", "Memr Basic"),
    ("basicReversed", "Memr Basic (and reversed)"),
    ("cloze", "Memr Cloze"),
    ("typeIn", "Memr Type-in"),
)
DEFAULT_CSS = """.card {
  font-family: Inter, Arial, sans-serif;
  font-size: 22px;
  line-height: 1.5;
  text-align: center;
  text-wrap: balance;
  overflow-wrap: anywhere;
  max-width: 64ch;
  margin: 0 auto;
  color: inherit;
  background: transparent;
}
"""


def _owner(model):
    value = model.get(OWNER_KEY)
    return value if type(value) is dict and value.get("owner") == "canonnotes" else None


def _prepared(col, kind, name, original=None):
    model = deepcopy(original) if original is not None else _stock(col, kind)
    if original is None:
        model["id"], model["name"], model["css"] = 0, name, DEFAULT_CSS
    source = [field for field in model["flds"] if field["name"].casefold() == "source"]
    if len(source) > 1 or source and source[0]["name"] != "Source":
        raise EngineError("conflict", "An owned note type has an ambiguous Source field.")
    # A user's template edits remain theirs. Setup must never silently remove
    # a template expression or change generated cards to hide this field.
    if any(re.search(r"{{[^{}]*\bSource\b[^{}]*}}", template.get(key, ""), re.I)
           for template in model["tmpls"] for key in ("qfmt", "afmt", "bqfmt", "bafmt")):
        raise EngineError("conflict", "A Memr template renders Source. Hide it before running setup.")
    if not source:
        if len(model["flds"]) >= 512:
            raise EngineError("unsupported", "This note type has no space for a Source field.")
        field = col.models.new_field("Source")
        field.update(plainText=True, collapsed=True)
        model["flds"].append(field)
    elif model["flds"][-1] is not source[0]:
        model["flds"].remove(source[0])
        model["flds"].append(source[0])
    marker = _owner(model)
    if marker and marker.get("schemaVersion") == 0 and model["css"] == marker.get("managedCss"):
        model["css"] = DEFAULT_CSS
    model[OWNER_KEY] = {"owner": "canonnotes", "kind": kind, "schemaVersion": SCHEMA_VERSION,
                        "managedCss": DEFAULT_CSS}
    return model


@method("provenance.ensureNotetypes")
def ensure_notetypes(engine, params):
    col = engine.col
    models = [col.models.get(item.id) for item in col.models.all_names_and_ids()]
    index = col.get_config(INDEX_KEY)
    if index is not None and (type(index) is not dict or set(index) != {"version", "ids"}
            or type(index["version"]) is not int or index["version"] != SCHEMA_VERSION
            or type(index["ids"]) is not dict or set(index["ids"]) - {kind for kind, _ in NOTETYPES}
            or any(type(value) is not int or not 0 < value <= 9007199254740991 for value in index["ids"].values())):
        raise EngineError("conflict", "The Memr note-type ownership index is invalid.")
    owned_ids = index["ids"] if index is not None else {}
    prepared = []
    for kind, name in NOTETYPES:
        candidates = [model for model in models if (_owner(model) or {}).get("kind") == kind]
        if kind in owned_ids:
            original = next((model for model in models if model["id"] == owned_ids[kind]), None)
            if original is not None and (_owner(original) or {}).get("kind") != kind:
                raise EngineError("conflict", "An indexed Memr note type has different ownership.")
            # A deleted original does not transfer its identity to a user clone
            # that copied the native note-type metadata.
        else:
            if len(candidates) > 1:
                raise EngineError("conflict", "More than one owned note type has the same role. Choose one before setup.")
            original = candidates[0] if candidates else None
        if original is None and col.models.id_for_name(name) is not None:
            raise EngineError("conflict", "A user note type uses a Memr name. Rename it before setup.")
        if original is not None:
            version = _owner(original).get("schemaVersion")
            if type(version) is not int or version not in (0, SCHEMA_VERSION):
                raise EngineError("unsupported", "This Memr note type uses an unsupported schema version.")
            if original["type"] != (1 if kind == "cloze" else 0):
                raise EngineError("conflict", "An owned note type has an incompatible native kind.")
        candidate = _prepared(col, kind, name, original)
        if candidate != original:
            _preflight(candidate, original)
        prepared.append((original, candidate))
    definitions_unchanged = all(original == candidate for original, candidate in prepared)
    expected_index = {"version": SCHEMA_VERSION, "ids": {
        kind: original["id"] for (kind, _), (original, _) in zip(NOTETYPES, prepared) if original is not None}}
    if definitions_unchanged and index == expected_index:
        return {**mutation_result(undo_state(col)), "notetypeIds": [model["id"] for model, _ in prepared]}

    target = col.add_custom_undo_entry("Set up Memr note types")
    ids = []
    try:
        for original, candidate in prepared:
            if original is None:
                ids.append(col.models.add_dict(candidate).id)
            else:
                if candidate != original:
                    col.models.update_dict(candidate)
                ids.append(original["id"])
        col.set_config(INDEX_KEY, {"version": SCHEMA_VERSION,
                                  "ids": {kind: int(value) for (kind, _), value in zip(NOTETYPES, ids)}}, undoable=True)
    finally:
        # Ownership is saved in the same native operation as each definition.
        # If a later native write fails, a retry resumes the already-owned
        # definitions and the successful steps remain one native undo action.
        changes = col.merge_undo_entries(target)
    return _result(col, changes, notetypeIds=list(map(int, ids)))


def _source_notes(col):
    """Read Source fields in bounded batches without changing native search state."""
    for item in col.models.all_names_and_ids():
        model = col.models.get(item.id)
        ordinal = next((index for index, field in enumerate(model["flds"])
                        if field["name"] == "Source"), None)
        if ordinal is None:
            continue
        after = 0
        while True:
            # field_at_index is Anki's own SQLite function. Limit the returned
            # field before it crosses into Python, then fail on oversize input.
            rows = col.db.all("select id,substr(field_at_index(flds,?),1,?) from notes "
                              "where mid=? and id>? and field_at_index(flds,?) like '%canonnotes:page/%' "
                              "order by id limit 32", ordinal, MAX_SOURCE_BYTES + 1, item.id, after, ordinal)
            if not rows:
                break
            for note_id, value in rows:
                sources = source_links(value)
                if sources:
                    yield int(note_id), sources
            after = int(rows[-1][0])


@method("provenance.sourceOf")
def source_of(engine, params):
    note = engine.col.get_note(int(params["noteId"]))
    return {"sources": source_links(note["Source"]) if "Source" in note else []}


@method("provenance.cardsForSource")
def cards_for_source(engine, params):
    if not valid_identity(params["pageId"]) or "blockId" in params and not valid_identity(params["blockId"]):
        raise EngineError("invalid_params", "Source identities must be nonempty text without control characters.")
    note_ids = []
    for note_id, sources in _source_notes(engine.col):
        if any(source["pageId"] == params["pageId"] and
               ("blockId" not in params or source["blockId"] == params["blockId"]) for source in sources):
            note_ids.append(note_id)
            if len(note_ids) > 100000:
                raise EngineError("unsupported", "This source has too many linked notes for one response.")
    card_ids = []
    for offset in range(0, len(note_ids), 512):
        batch = note_ids[offset:offset + 512]
        placeholders = ",".join("?" for _ in batch)
        card_ids.extend(engine.col.db.list(f"select id from cards where nid in ({placeholders}) order by id", *batch))
        if len(card_ids) > 100000:
            raise EngineError("unsupported", "This source has too many linked cards for one response.")
    return {"cardIds": sorted(map(int, card_ids)), "noteIds": sorted(note_ids)}


@method("provenance.checkSources")
def check_sources(engine, params):
    revisions = {}
    for source in params["sources"]:
        page, block, revision = source["pageId"], source["blockId"], source["revision"]
        if (not valid_identity(page) or block is not None and not valid_identity(block)
                or revision is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", revision)):
            raise EngineError("invalid_params", "A source identity or current revision is invalid.")
        key = (page, block)
        revision = revision.lower() if revision is not None else None
        if key in revisions and revisions[key] != revision:
            raise EngineError("invalid_params", "A source has conflicting current revisions.")
        revisions[key] = revision
    if not revisions:
        return {"notes": []}
    notes = []
    result_bytes = len(b'{"notes":[]}')
    for note_id, sources in _source_notes(engine.col):
        if not any((source["pageId"], source["blockId"]) in revisions for source in sources):
            continue
        checked = []
        for source in sources:
            key, stored = (source["pageId"], source["blockId"]), source["revision"]
            current = revisions.get(key)
            if key not in revisions:
                status, changed = "unchecked", None
            elif current is None:
                status, changed = "missing", None
            elif stored is None:
                status, changed = "untracked", None
            else:
                changed = stored.lower() != current
                status = "changed" if changed else "current"
            checked.append({**source, "changed": changed, "status": status})
        entry = {"noteId": note_id, "sources": checked}
        result_bytes += len(json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + bool(notes)
        if result_bytes > MAX_CHECK_SOURCES_BYTES:
            raise EngineError("unsupported", "The source check exceeds its bounded response size.")
        notes.append(entry)
        if len(notes) > 10000:
            raise EngineError("unsupported", "Too many linked notes match this source check.")
    return {"notes": sorted(notes, key=lambda note: note["noteId"])}
