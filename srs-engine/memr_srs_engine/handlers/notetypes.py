"""Native notetype schema edits retain field and template identities."""
import copy
from contextlib import nullcontext
import re
import tempfile
import unicodedata
from pathlib import Path

from ..errors import EngineError
from ..registry import method
from .decks import _result
from anki.collection import Collection
from anki.cards import Card
from anki.models import StockNotetypeKind
from anki.sound import SoundOrVideoTag
from anki.template import TemplateRenderContext
from anki.utils import from_json_bytes, to_json_bytes


STOCK_KINDS = {
    "basic": StockNotetypeKind.KIND_BASIC,
    "basicReversed": StockNotetypeKind.KIND_BASIC_AND_REVERSED,
    "basicOptionalReversed": StockNotetypeKind.KIND_BASIC_OPTIONAL_REVERSED,
    "typeIn": StockNotetypeKind.KIND_BASIC_TYPING,
    "cloze": StockNotetypeKind.KIND_CLOZE,
    "imageOcclusion": StockNotetypeKind.KIND_IMAGE_OCCLUSION,
}
FIELD_KEYS = ("name", "sticky", "rtl", "font", "size", "plainText", "collapsed", "excludeFromSearch")
TEMPLATE_KEYS = {"name": "name", "questionFormat": "qfmt", "answerFormat": "afmt", "deckId": "did",
                 "browserQuestionFormat": "bqfmt", "browserAnswerFormat": "bafmt"}
PREVIEW_CARD_COLUMNS = ("id", "nid", "did", "ord", "mod", "usn", "type", "queue", "due", "ivl",
                        "factor", "reps", "lapses", "left", "odue", "odid", "flags", "data")


def _notetype(col, notetype_id):
    model = col.models.get(int(notetype_id))
    if model is None:
        raise EngineError("not_found", "The requested note type is unavailable.")
    return model


def _kind(model):
    # OriginalStockKind differs from StockNotetypeKind by one in Anki's API.
    if model.get("originalStockKind") == 6:
        return "imageOcclusion"
    return "cloze" if model["type"] == 1 else "normal"


def _public(col, model, note_count=None):
    fields = []
    for ordinal, field in enumerate(model["flds"]):
        fields.append({"name": field["name"], "ord": ordinal, "sticky": bool(field["sticky"]),
                       "rtl": bool(field["rtl"]), "font": field["font"], "size": field["size"],
                       "plainText": bool(field.get("plainText", False)),
                       "collapsed": bool(field.get("collapsed", False)),
                       "excludeFromSearch": bool(field.get("excludeFromSearch", False))})
    templates = [{"ord": ordinal, **{key: template.get(native, None if key == "deckId" else "")
                                   for key, native in TEMPLATE_KEYS.items()}}
                 for ordinal, template in enumerate(model["tmpls"])]
    return {"id": model["id"], "name": model["name"], "kind": _kind(model),
            "fields": fields, "templates": templates, "css": model["css"], "sortField": model["sortf"],
            "latexHeader": model.get("latexPre", ""), "latexFooter": model.get("latexPost", ""),
            "latexSvg": bool(model.get("latexsvg", False)),
            "noteCount": col.models.use_count(model) if note_count is None else note_count}


def _stock(col, kind):
    return from_json_bytes(col._backend.get_stock_notetype_legacy(STOCK_KINDS[kind]))


@method("notetypes.list")
def list_notetypes(engine, params):
    return {"notetypes": [{"id": item.id, "name": item.name,
                           "kind": _kind(_notetype(engine.col, item.id)), "noteCount": item.use_count}
                          for item in engine.col.models.all_use_counts()]}


@method("notetypes.get")
def get(engine, params):
    return _public(engine.col, _notetype(engine.col, params["notetypeId"]))


@method("notetypes.stock")
def stock(engine, params):
    kinds = [params["kind"]] if "kind" in params else STOCK_KINDS
    return {"notetypes": [_public(engine.col, _stock(engine.col, kind), note_count=0) for kind in kinds]}


def _text(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise EngineError("invalid_params", "Text must contain valid Unicode characters.") from None
    return value


def _name(value, field=False):
    value = unicodedata.normalize("NFC", _text(value))
    if (not value.strip() or len(value) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in value) or '"' in value):
        raise EngineError("invalid_params", "Names must contain text without control or quote characters.")
    if field and (any(char in value for char in ":{}") or value != value.strip() or value[0] in "#/^"):
        raise EngineError("invalid_params", "The field name contains reserved template syntax.")
    return value


def _unique_name(col, name, notetype_id=0):
    other = col.models.id_for_name(name)
    if other is not None and other != notetype_id:
        raise EngineError("conflict", "A note type with that name already exists.")


def _items(col, values, originals, field, updating):
    """Retained ordinals carry native IDs, tags and unexposed configuration."""
    result, seen_ordinals, seen_names = [], set(), {}
    for value in values:
        ordinal = int(value["ord"]) if value["ord"] is not None else None
        if ordinal is not None:
            if ordinal in seen_ordinals or updating and ordinal >= len(originals):
                raise EngineError("invalid_params", "An original ordinal must identify one existing item.")
            seen_ordinals.add(ordinal)
        name = _name(value["name"], field=field)
        # Python casefold also groups names Anki keeps distinct, such as ffi
        # and the ligature. Let the pinned native collation decide equality.
        candidates = seen_names.setdefault(name.casefold(), [])
        if any(col.db.scalar("select ? = ? collate unicase", name, other) for other in candidates):
            raise EngineError("invalid_params", "Field and template names must be unique within their lists.")
        candidates.append(name)
        if ordinal is not None and ordinal < len(originals):
            native = copy.deepcopy(originals[ordinal])
        else:
            native = col.models.new_field(name) if field else col.models.new_template(name)
        native["ord"] = ordinal if updating else None
        if field:
            native.update({key: _text(value[key]) if type(value[key]) is str else value[key] for key in FIELD_KEYS})
            native["name"] = name
            native["size"] = int(value["size"])
        else:
            for key, target in TEMPLATE_KEYS.items():
                native[target] = _text(value[key]) if type(value[key]) is str else value[key]
            native["name"] = name
            deck_id = value["deckId"]
            native["did"] = int(deck_id) if deck_id is not None else None
            if deck_id is not None and col.decks.get(int(deck_id), default=False) is None:
                raise EngineError("invalid_params", "A template's deck override is unavailable.")
        result.append(native)
    if field and updating and any(item.get("preventDeletion") and index not in seen_ordinals
                                  for index, item in enumerate(originals)):
        raise EngineError("invalid_params", "A required native field cannot be removed.")
    return result


def _definition(col, value, original=None):
    if not value["fields"] or not value["templates"]:
        raise EngineError("invalid_params", "A note type needs at least one field and template.")
    if value["sortField"] >= len(value["fields"]):
        raise EngineError("invalid_params", "The sort field must identify a field in the resulting order.")
    if original is not None and value["kind"] != _kind(original):
        raise EngineError("invalid_params", "Use a different note type to change the native kind.")
    if value["kind"] != "normal" and len(value["templates"]) != 1:
        raise EngineError("invalid_params", "Cloze and image-occlusion note types use one template.")
    base_kind = {"normal": "basic", "cloze": "cloze", "imageOcclusion": "imageOcclusion"}[value["kind"]]
    model = copy.deepcopy(original) if original is not None else _stock(col, base_kind)
    model["id"] = original["id"] if original is not None else 0
    model["name"] = _name(value["name"])
    model["flds"] = _items(col, value["fields"], model["flds"], True, original is not None)
    model["tmpls"] = _items(col, value["templates"], model["tmpls"], False, original is not None)
    model["css"] = _text(value["css"])
    model["latexPre"], model["latexPost"] = _text(value["latexHeader"]), _text(value["latexFooter"])
    model["latexsvg"] = value["latexSvg"]
    # Anki repositions retained ordinals, otherwise clamps this index. A new
    # final field can always be selected with an ordinal outside the schema.
    sort_field = int(value["sortField"])
    selected = model["flds"][sort_field]["ord"]
    if selected is not None:
        model["sortf"] = selected
    elif original is not None and sort_field == len(model["flds"]) - 1:
        model["sortf"] = max(len(original["flds"]), len(model["flds"]))
    else:
        model["sortf"] = sort_field
    return model


def _preflight(model, original=None, progress=None):
    """Anki clears prior undo history on a rejected schema transaction."""
    with tempfile.TemporaryDirectory(prefix="memr-srs-schema-") as directory:
        scratch = Collection(str(Path(directory) / "collection.anki2"))
        try:
            with progress.using_backend(scratch) if progress is not None else nullcontext():
                candidate = copy.deepcopy(model)
                candidate["id"], candidate["originalId"] = 0, None
                if original is None:
                    candidate_id = scratch.models.add_dict(candidate).id
                else:
                    seed = copy.deepcopy(original)
                    seed["id"], seed["originalId"] = 0, None
                    # Seed only a disposable copy of an already-stored schema.
                    # This permits repairing an imported invalid template. The
                    # candidate below and the active update always run full checks.
                    seed_id = scratch._backend.add_or_update_notetype(
                        json=to_json_bytes(seed), preserve_usn_and_mtime=True, skip_checks=True)
                    if progress is not None:
                        progress.check_abort()
                    candidate["id"] = seed_id
                    scratch.models.update_dict(candidate)
                    candidate_id = seed_id
                if progress is not None:
                    progress.check_abort()
                return scratch.models.get(candidate_id)["sortf"]
        finally:
            scratch.close()


@method("notetypes.add")
def add(engine, params):
    if ("notetype" in params) == ("stockKind" in params) or "notetype" in params and "name" in params:
        raise EngineError("invalid_params", "Provide one complete definition or a stock kind with an optional name.")
    if "stockKind" in params:
        model = _stock(engine.col, params["stockKind"])
        model["name"] = _name(params.get("name", model["name"]))
    else:
        model = _definition(engine.col, params["notetype"])
    _unique_name(engine.col, model["name"])
    _preflight(model)
    result = engine.col.models.add_dict(model)
    return _result(engine.col, result.changes, notetypeId=result.id)


@method("notetypes.clone")
def clone(engine, params):
    original = _notetype(engine.col, params["notetypeId"])
    name = _name(params["name"])
    _unique_name(engine.col, name)
    model = engine.col.models.copy(original, add=False)
    model["name"] = name
    _preflight(model)
    result = engine.col.models.add_dict(model)
    return _result(engine.col, result.changes, notetypeId=result.id)


@method("notetypes.update")
def update(engine, params):
    with engine.progress.using_backend(None):
        engine.progress.report(0, 3, "Validating note type")
        engine.progress.check_abort()
        value = params["notetype"]
        original = _notetype(engine.col, value["id"])
        model = _definition(engine.col, value, original)
        _unique_name(engine.col, model["name"], model["id"])
        engine.progress.report(1, 3, "Checking note type schema")
        engine.progress.check_abort()
        if _preflight(model, original, engine.progress) != value["sortField"]:
            raise EngineError("unsupported", "Save the field changes with the last field as the sort field, "
                              "reload the note type, then select the new sort field.")
        engine.progress.report(2, 3, "Updating note type")
        engine.progress.check_abort()
    return _result(engine.col, engine.col.models.update_dict(model))


@method("notetypes.rename")
def rename(engine, params):
    model = copy.deepcopy(_notetype(engine.col, params["notetypeId"]))
    model["name"] = _name(params["name"])
    _unique_name(engine.col, model["name"], model["id"])
    _preflight(model, _notetype(engine.col, model["id"]))
    return _result(engine.col, engine.col.models.update_dict(model))


@method("notetypes.remove")
def remove(engine, params):
    with engine.progress.using_backend(None):
        engine.progress.report(0, 1, "Validating note type removal")
        engine.progress.check_abort()
        model = _notetype(engine.col, params["notetypeId"])
        count = engine.col.models.use_count(model)
        if count and not params["deleteNotes"]:
            raise EngineError("conflict", "This note type still has notes; confirm their deletion to continue.")
        engine.progress.report(0, 1, "Removing note type")
        engine.progress.check_abort()
    return _result(engine.col, engine.col.models.remove(model["id"]), count=count)


def _selected_notetype(col, note_ids, progress):
    selected = list(dict.fromkeys(map(int, note_ids)))
    if not selected:
        raise EngineError("invalid_params", "Select at least one note.")
    models = set()
    for offset in range(0, len(selected), 512):
        progress.check_abort()
        batch = selected[offset:offset + 512]
        placeholders = ",".join("?" for _ in batch)
        rows = col.db.all(f"select id,mid from notes where id in ({placeholders})", *batch)
        if len(rows) != len(batch):
            raise EngineError("not_found", "A selected note is unavailable.")
        models.update(row[1] for row in rows)
        progress.report(offset + len(batch), len(selected) + 1, "Validating selected notes")
    if len(models) != 1:
        raise EngineError("invalid_params", "All selected notes must use the same note type.")
    return selected, _notetype(col, next(iter(models)))


def _mapping(values, destination_count, source_count):
    used = [value for value in values if value != -1]
    if len(values) != destination_count or len(set(used)) != len(used) or any(value >= source_count for value in used):
        raise EngineError("invalid_params", "Mappings need one distinct source ordinal per destination, or -1 for a new value.")


@method("notetypes.changeNotetype")
def change_notetype(engine, params):
    with engine.progress.using_backend(None):
        engine.progress.report(0, len(params["noteIds"]) + 1, "Validating selected notes")
        engine.progress.check_abort()
        target = _notetype(engine.col, params["notetypeId"])
        selected, original = _selected_notetype(engine.col, params["noteIds"], engine.progress)
        _mapping(params["fieldMap"], len(target["flds"]), len(original["flds"]))
        cloze = original["type"] == 1 or target["type"] == 1
        if cloze:
            if params["templateMap"]:
                raise EngineError("invalid_params", "Cloze conversions do not accept template mappings.")
        else:
            _mapping(params["templateMap"], len(target["tmpls"]), len(original["tmpls"]))
        request = engine.col.models.change_notetype_info(
            old_notetype_id=original["id"], new_notetype_id=target["id"]).input
        request.note_ids.extend(selected)
        request.new_fields[:] = list(map(int, params["fieldMap"]))
        request.new_templates[:] = list(map(int, params["templateMap"]))
        engine.progress.report(len(selected), len(selected) + 1, "Converting note types")
        engine.progress.check_abort()
    return _result(engine.col, engine.col.models.change_notetype_of_notes(request), count=len(selected))


def _av(tags):
    return [{"kind": "sound", "filename": tag.filename, "text": None, "language": None,
             "voices": [], "speed": 1}
            if isinstance(tag, SoundOrVideoTag) else
            {"kind": "tts", "filename": None, "text": tag.field_text, "language": tag.lang,
             "voices": tag.voices, "speed": tag.speed} for tag in tags]


def _render(col, model, fields, ordinal, note=None, card=None, sample=False):
    note = copy.copy(note) if note is not None else col.new_note(model)
    note.mid = model["id"]
    note.fields = list(fields)
    template = copy.deepcopy(model["tmpls"][0 if model["type"] else ordinal])
    # Native uncommitted rendering finds card context by note/template identity.
    # New templates and unmatched note-only samples use native defaults.
    template["ord"] = card.ord if card is not None else None
    render_card = copy.copy(card) if card is not None else Card(col)
    render_card.ord = ordinal
    # Validate before TemplateRenderContext.render(), which turns template
    # exceptions into display text. Invalid input must remain an RPC error.
    col._backend.render_uncommitted_card_legacy(
        note=note._to_backend_note(), card_ord=ordinal, template=to_json_bytes(template),
        fill_empty=sample, partial_render=False)
    # Browser mode skips card_did_render hooks, notably the external LaTeX
    # compiler. The explicit template still uses its regular front/back.
    output = TemplateRenderContext(col, render_card, note, browser=True, notetype=model,
                                   template=template, fill_empty=sample).render()
    question = col._backend.extract_latex(text=output.question_text, svg=model["latexsvg"], expand_clozes=False).text
    answer = col._backend.extract_latex(text=output.answer_text, svg=model["latexsvg"], expand_clozes=False).text
    type_answer = None
    match = re.search(r"\[\[type:([^\]]+)\]\]", question)
    if match:
        field_name = match[1]
        cloze = field_name.startswith("cloze:")
        if cloze:
            field_name = field_name[6:]
        field_index = next((index for index, field in enumerate(model["flds"]) if field["name"] == field_name), None)
        if field_index is None:
            raise EngineError("invalid_params", "The typed-answer field is unavailable.")
        field = model["flds"][field_index]
        correct = fields[field_index]
        if cloze:
            correct = col.extract_cloze_for_typing(correct, ordinal)
        correct = col._backend.strip_html(text=correct, mode=0)
        type_answer = {"field": field_name, "correct": correct, "font": field["font"], "size": field["size"]}
        marker = re.compile(r"\[\[type:[^\]]+\]\]")
        question = marker.sub('<input id="typeans" type="text" autocomplete="off" aria-label="Type the answer">', question)
        answer = marker.sub(lambda _match: col.compare_answer(correct, correct), answer)
    return question, answer, _av(output.question_av_tags), _av(output.answer_av_tags), type_answer


def _preview_deck(source, scratch, deck_id):
    deck = source.decks.get(deck_id, default=False) if deck_id else None
    return scratch.decks.add_normal_deck_with_name(deck["name"]).id if deck is not None else 0


def _preview_card(source, scratch, card, note, model, fields):
    # Anki has no public add-card API. Only this disposable collection receives
    # a real source row, after verifying the complete pinned card-table schema.
    expected = [(name, "TEXT" if name == "data" else "INTEGER", int(name != "id"), None,
                 int(name == "id")) for name in PREVIEW_CARD_COLUMNS]
    for collection in (source, scratch):
        # DBProxy treats a leading PRAGMA as a write and clears native undo.
        schema = collection.db.all('select name,type,"notnull",dflt_value,pk from pragma_table_info(\'cards\') order by cid')
        if [tuple(row) for row in schema] != expected:
            raise EngineError("unsupported", "The card schema is unavailable for a read-only preview.")
    columns = ",".join(f'"{name}"' for name in PREVIEW_CARD_COLUMNS)
    row = source.db.first(f"select {columns} from cards where id = ?", card.id)
    if row is None:
        raise EngineError("not_found", "The requested card is unavailable.")
    scratch_note = scratch.new_note(model)
    scratch_note.fields, scratch_note.tags = list(fields), list(note.tags)
    scratch.add_note(scratch_note, 1)
    generated = scratch_note.card_ids()
    copied = list(row)
    copied[1] = scratch_note.id
    copied[2] = _preview_deck(source, scratch, card.did)
    copied[15] = _preview_deck(source, scratch, card.odid)
    scratch.db.execute(f"insert or replace into cards ({columns}) values ({','.join('?' for _ in copied)})", *copied)
    # A timestamp collision can make the copied real ID equal a generated ID.
    # Retain that row and remove only other cards generated for this scratch note.
    discarded = [card_id for card_id in generated if card_id != card.id]
    if discarded:
        scratch.remove_cards_and_orphaned_notes(discarded)
    return scratch_note, scratch.get_card(card.id)


@method("notetypes.previewTemplate")
def preview_template(engine, params):
    from .notes import _fields, _note, _source

    value, col = params["notetype"], engine.col
    original = _notetype(col, value["id"]) if value["id"] else None
    model = _definition(col, value, original)
    ordinal = int(params["templateOrdinal"])
    if model["type"] == 0 and ordinal >= len(model["tmpls"]):
        raise EngineError("invalid_params", "The template ordinal is unavailable.")
    card = None
    if "cardId" in params:
        if not params["cardId"]:
            raise EngineError("not_found", "The requested card is unavailable.")
        card = col.get_card(int(params["cardId"]))
    note = _note(col, params["noteId"]) if "noteId" in params else card.note() if card else None
    if card is not None and note.id != card.nid:
        raise EngineError("invalid_params", "The card and note must identify the same sample.")
    identity = ordinal if model["type"] else model["tmpls"][ordinal]["ord"]
    if card is not None and (identity is None or card.ord != identity):
        raise EngineError("invalid_params", "The sample card must match the retained template ordinal or requested cloze number.")
    if card is None and note is not None and identity is not None:
        card_id = col.db.scalar("select id from cards where nid = ? and ord = ? order by id limit 1",
                                note.id, identity)
        if card_id is not None:
            card = col.get_card(card_id)
    if "fields" in params:
        fields = _fields(model, params["fields"])
    elif note is not None:
        if original is None or original["id"] != note.mid:
            raise EngineError("invalid_params", "Supply fields when previewing a different note type.")
        fields = [note.fields[field["ord"]] if field["ord"] is not None else "" for field in model["flds"]]
    else:
        fields = [""] * len(model["flds"])
    sample = "fields" not in params and note is None
    same_schema = (original is not None and original["name"] == model["name"]
                   and [field["name"] for field in original["flds"]] == [field["name"] for field in model["flds"]])
    if same_schema:
        rendered = _render(col, model, fields, ordinal, note, card, sample)
    else:
        # The pinned rendering API reads field names from a saved notetype ID.
        # Save the unsaved schema only in scratch, retaining active undo/data.
        with tempfile.TemporaryDirectory(prefix="memr-srs-preview-") as directory:
            scratch = Collection(str(Path(directory) / "collection.anki2"))
            try:
                native = copy.deepcopy(model)
                native["id"], native["originalId"], native["sortf"] = 0, None, int(value["sortField"])
                for item in [*native["flds"], *native["tmpls"]]:
                    item["ord"] = None
                for template in native["tmpls"]:
                    if template.get("did") is not None:
                        template["did"] = _preview_deck(col, scratch, template["did"])
                existing = scratch.models.id_for_name(native["name"])
                if existing is not None:
                    scratch.models.remove(existing)
                created = scratch.models.add_dict(native)
                native = _notetype(scratch, created.id)
                if card is not None:
                    scratch_note, scratch_card = _preview_card(col, scratch, card, note, native, fields)
                else:
                    scratch_note, scratch_card = scratch.new_note(native), None
                    if note is not None:
                        scratch_note.tags = list(note.tags)
                rendered = _render(scratch, native, fields, ordinal, scratch_note, scratch_card, sample)
            finally:
                scratch.close()
    question, answer, question_av, answer_av, type_answer = rendered
    # Previews have no study queue, answer buttons, timer or automatic advances.
    return {"cardId": card.id if card else 0, "noteId": note.id if note else 0,
            "ord": ordinal, "deckId": int(card.did) if card else 1, "queue": int(card.queue) if card else 0,
            "questionHtml": question, "answerHtml": answer, "css": model["css"], "bodyClass": f"card card{ordinal + 1}",
            "av": {"question": question_av, "answer": answer_av}, "typeAnswer": type_answer,
            "autoplay": False, "replayQuestionOnAnswer": False, "timer": {"show": False, "limitSeconds": 0},
            "autoAdvance": {"enabled": False, "questionSeconds": 0, "answerSeconds": 0,
                            "questionAction": "none", "answerAction": "none", "waitForAudio": True},
            "nextStates": [], "counts": {"new": 0, "learning": 0, "review": 0},
            "flags": card.user_flag() if card else 0, "marked": note.has_tag("marked") if note else False,
            "source": _source(note) if note else None}
