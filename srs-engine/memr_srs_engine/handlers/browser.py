"""Native browser rows and four bounded search snapshots per engine session."""
from collections import OrderedDict
import re
import uuid

from .decks import _result, _text
from .search import find, native_search, sort_column
from anki.browser import BrowserConfig, BrowserDefaults
from anki.config_pb2 import ConfigKey as Config
from ..collection import mutation_result, undo_state
from ..errors import EngineError
from ..registry import method
from ..validate import validate_value

LAYOUT_KEY = "canonnotesBrowserLayout"
LAYOUT_DEFAULT = {"sidebarWidth": 220, "detailWidth": 440, "detailVisible": True}
COLOURS = ("default", "marked", "suspended", "flagRed", "flagOrange", "flagGreen", "flagBlue",
           "flagPink", "flagTurquoise", "flagPurple", "buried")
CURSOR = re.compile(r"([0-9a-f]{32}):([0-9]{1,16})\Z")


def _mode(col):
    return "notes" if col.get_config_bool(Config.Bool.BROWSER_TABLE_SHOW_NOTES_MODE) else "cards"


def _columns(col, columns):
    if not columns or len(columns) != len(set(columns)):
        raise EngineError("invalid_params", "Select distinct browser columns.")
    available = {column.key for column in col.all_browser_columns()}
    if any(_text(key) not in available for key in columns):
        raise EngineError("invalid_params", "Unknown browser column.")
    return columns


@method("browser.columns")
def columns(engine, params):
    col, notes = engine.col, params["mode"] == "notes"
    defaults = BrowserDefaults.NOTE_COLUMNS if notes else BrowserDefaults.CARD_COLUMNS
    selected = col.get_config(BrowserConfig.active_columns_key(notes), defaults)
    if type(selected) is not list or any(type(key) is not str for key in selected):
        raise EngineError("conflict", "The stored browser column settings are invalid.")
    try:
        _columns(col, selected)
    except EngineError:
        raise EngineError("conflict", "The stored browser column settings are invalid.") from None
    return {"available": [{"key": c.key, "label": c.notes_mode_label if notes else c.cards_mode_label,
                           "sortable": bool(c.sorting_notes if notes else c.sorting_cards),
                           "alignment": "center" if c.alignment else "start"} for c in col.all_browser_columns()],
            "selected": list(selected), "defaults": list(defaults)}


@method("browser.setColumns")
def set_columns(engine, params):
    col = engine.col
    selected = _columns(col, params["columns"])
    key = BrowserConfig.active_columns_key(params["mode"] == "notes")
    if col.get_config(key) == selected:
        return mutation_result(undo_state(col))
    return _result(col, col.set_config(key, selected, undoable=True))


def _revision(engine):
    # Browser rendering caches update SQLite total_changes(); they are reads.
    # Native undo sequence also distinguishes same-millisecond writes and undo.
    col = engine.col
    return f"{engine._browser_session}:{engine.collection_generation}:{col.mod}:{col.undo_status().last_step}:{col.sched.today}"


def _states(col, mode, ids):
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    if mode == "cards":
        rows = col.db.all(f"select c.id,c.nid,c.flags&7,n.tags,c.queue from cards c join notes n on n.id=c.nid where c.id in ({placeholders})", *ids)
        return {i: {"noteId": nid, "flags": flags, "marked": "marked" in tags.lower().split(),
                    "suspended": queue == -1, "buried": queue in (-2, -3)} for i, nid, flags, tags, queue in rows}
    rows = col.db.all(f"select n.id,n.tags,max(c.queue=-1),max(c.queue in (-2,-3)) from notes n join cards c on c.nid=n.id where n.id in ({placeholders}) group by n.id", *ids)
    # A note has no single card flag; suspended/buried indicate any child card.
    return {i: {"noteId": i, "flags": 0, "marked": "marked" in tags.lower().split(),
                "suspended": bool(suspended), "buried": bool(buried)} for i, tags, suspended, buried in rows}


@method("browser.page")
def page(engine, params):
    col, mode = engine.col, params["mode"]
    _text(params["search"])
    _columns(col, params["columns"])
    sort_column(col, mode, params.get("sort", "noteFld"))
    signature = (params["search"], mode, tuple(params["columns"]), params.get("sort", "noteFld"), params.get("descending", False))
    cursors = getattr(engine, "_browser_cursors", None)
    if cursors is None:
        cursors = engine._browser_cursors = OrderedDict()
        engine._browser_session = uuid.uuid4().hex
    token = params.get("cursor")
    offset = int(params.get("offset", 0))
    if token is not None:
        match = CURSOR.fullmatch(token)
        if match is None or "offset" in params:
            raise EngineError("invalid_params", "Invalid browser cursor or offset.")
        key, offset = match[1], int(match[2])
        snapshot = cursors.get(key)
        if snapshot is None:
            raise EngineError("not_found", "The browser cursor expired; start a new search.")
        if snapshot["signature"] != signature or offset > len(snapshot["ids"]):
            raise EngineError("invalid_params", "The browser cursor belongs to different page parameters.")
        if snapshot["revision"] != _revision(engine):
            raise EngineError("conflict", "The collection changed; start a new browser search.")
    if mode != _mode(col):
        raise EngineError("conflict", "Select this browser mode with browser.setLayout before reading its rows.")
    if token is None:
        key = uuid.uuid4().hex
        snapshot = {"signature": signature, "revision": _revision(engine), "ids": find(col, mode, params)}
    ids = snapshot["ids"][offset:offset + int(params["limit"])]
    states = _states(col, mode, ids)
    col._backend.set_active_browser_columns(params["columns"])
    rows = []
    for item_id in ids:
        state = states.get(item_id)
        if state is None:
            raise EngineError("conflict", "The collection changed; start a new browser search.")
        cells, colour, _font, _size = col.browser_row_for_id(item_id)
        text = [cell[0] for cell in cells]
        if any(len(cell) > 65536 for cell in text):
            raise EngineError("unsupported", "A browser cell exceeds the display limit; narrow the selected columns.")
        rows.append({"id": item_id, "cardId": item_id if mode == "cards" else None,
                     **state, "cells": text, "color": COLOURS[colour]})
    next_offset = offset + len(ids)
    cursors[key] = snapshot
    cursors.move_to_end(key)
    while len(cursors) > 4:
        cursors.popitem(last=False)
    return {"rows": rows, "total": len(snapshot["ids"]), "revision": snapshot["revision"],
            "nextCursor": f"{key}:{next_offset}" if next_offset < len(snapshot["ids"]) else None}


def _searches(engine):
    saved = engine.col.get_config("savedFilters", {})
    if type(saved) is not dict:
        raise EngineError("conflict", "The stored saved searches are invalid.")
    values = [{"name": name, "search": query} for name, query in saved.items()]
    schema = engine.registry.catalogue["browser.savedSearches"]["result"]
    if validate_value(schema, {"searches": values}):
        raise EngineError("conflict", "The stored saved searches are invalid.")
    return dict(saved)


def _search_name(value):
    _text(value)
    if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EngineError("invalid_params", "Saved searches require a nonempty name without control characters.")
    return value


@method("browser.savedSearches")
def saved_searches(engine, _params):
    return {"searches": [{"name": name, "search": query} for name, query in sorted(_searches(engine).items())]}


@method("browser.saveSearch")
def save_search(engine, params):
    name, query = _search_name(params["name"]), _text(params["search"])
    native_search(engine.col.build_search_string, query)
    saved = _searches(engine)
    if name not in saved and len(saved) >= 1000:
        raise EngineError("invalid_params", "At most 1000 searches can be saved.")
    if saved.get(name) == query:
        return mutation_result(undo_state(engine.col))
    saved[name] = query
    return _result(engine.col, engine.col.set_config("savedFilters", saved, undoable=True))


@method("browser.removeSavedSearch")
def remove_saved_search(engine, params):
    name, saved = _search_name(params["name"]), _searches(engine)
    if name not in saved:
        raise EngineError("not_found", "The saved search is unavailable.")
    del saved[name]
    return _result(engine.col, engine.col.set_config("savedFilters", saved, undoable=True))


@method("browser.getLayout")
def get_layout(engine, _params):
    stored = engine.col.get_config(LAYOUT_KEY, LAYOUT_DEFAULT)
    if type(stored) is not dict or "mode" in stored:
        raise EngineError("conflict", "The stored browser layout is invalid.")
    layout = {"mode": _mode(engine.col), **stored}
    if validate_value(engine.registry.catalogue["browser.getLayout"]["result"], layout):
        raise EngineError("conflict", "The stored browser layout is invalid.")
    return layout


@method("browser.setLayout")
def set_layout(engine, params):
    col = engine.col
    current = get_layout(engine, {})
    geometry = {k: int(v) if k.endswith("Width") else v for k, v in params.items() if k != "mode"}
    mode_changed = current["mode"] != params["mode"]
    geometry_changed = any(current[k] != v for k, v in geometry.items())
    if mode_changed and geometry_changed:
        raise EngineError("invalid_params", "Change browser mode and pane geometry with two explicit browser.setLayout requests.")
    if not mode_changed and not geometry_changed:
        return mutation_result(undo_state(col))
    # The pinned native API has no atomic batch-config command. Each accepted
    # request therefore makes exactly one undoable write, preserving failure
    # atomicity and the existing undo/redo stack without compensating mutations.
    changes = (col.set_config_bool(Config.Bool.BROWSER_TABLE_SHOW_NOTES_MODE, params["mode"] == "notes", undoable=True)
               if mode_changed else col.set_config(LAYOUT_KEY, geometry, undoable=True))
    return _result(col, changes)
