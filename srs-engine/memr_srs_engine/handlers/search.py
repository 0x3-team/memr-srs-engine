"""Anki owns parsing, escaping and ordering; clients select named columns only."""
import re

from .decks import _text  # Load Collection before Anki's hook imports.
from anki.errors import SearchError
from anki.search_pb2 import BrowserColumns, SearchNode
from ..errors import EngineError
from ..registry import method


def native_search(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except SearchError as error:
        raise EngineError("invalid_params", str(error)[:4096]) from None


def sort_column(col, mode, key="noteFld"):
    column = col.get_browser_column(_text(key))
    if column is None or (column.sorting_notes if mode == "notes" else column.sorting_cards) == BrowserColumns.SORTING_NONE:
        raise EngineError("invalid_params", "Choose a sortable browser column for this mode.")
    return column


def find(col, mode, params):
    query = _text(params["search"])
    column = sort_column(col, mode, params.get("sort", "noteFld"))
    function = col.find_notes if mode == "notes" else col.find_cards
    return list(native_search(function, query, order=column, reverse=params.get("descending", False)))


@method("search.cards")
def cards(engine, params):
    values = find(engine.col, "cards", params)
    if len(values) > 100000:
        raise EngineError("invalid_params", "This search exceeds 100000 ids; use browser paging or narrow the search.")
    return {"cardIds": values}


@method("search.notes")
def notes(engine, params):
    values = find(engine.col, "notes", params)
    if len(values) > 100000:
        raise EngineError("invalid_params", "This search exceeds 100000 ids; use browser paging or narrow the search.")
    return {"noteIds": values}


def _integer(value, low, high):
    if not re.fullmatch(r"-?[0-9]+", value) or len(value) > 11:
        raise EngineError("invalid_params", "The filter requires an integer.")
    number = int(value)
    if not low <= number <= high:
        raise EngineError("invalid_params", "The numeric filter is outside its supported range.")
    return number


def _node(term):
    kind, value = term["kind"], _text(term["value"])
    fields = {"deck": "deck", "notetype": "note", "tag": "tag", "text": "literal_text", "raw": "parsable_text"}
    if kind in fields:
        node = SearchNode(**{fields[kind]: value})
    elif kind == "field":
        name = _text(term.get("field", ""))
        if not name.strip():
            raise EngineError("invalid_params", "A field filter requires a field name.")
        node = SearchNode(field=SearchNode.Field(field_name=name, text=value))
    elif kind == "flag":
        flag = 1 if value == "any" else _integer(value, 0, 7)
        node = SearchNode(flag=flag if value == "any" or flag == 0 else flag + 1)
    elif kind == "state":
        states = {"new": 0, "learn": 1, "review": 2, "due": 3, "suspended": 4, "buried": 5}
        if value not in states:
            raise EngineError("invalid_params", "Unknown card-state filter.")
        node = SearchNode(card_state=states[value])
    elif kind == "added":
        node = SearchNode(added_in_days=_integer(value, 0, 2147483647))
    else:
        node = SearchNode(due_in_days=_integer(value, -2147483648, 2147483647))
    return SearchNode(negated=node) if term.get("negate", False) else node


@method("search.build")
def build(engine, params):
    col = engine.col
    nodes = [_node(term) for term in params["terms"]]
    joiner = params["join"].upper()
    existing = params.get("existingSearch")
    if existing is not None:
        _text(existing)
    if not nodes and existing is None and params.get("operation") != "replace":
        return {"search": ""}
    if params.get("operation") == "replace":
        if existing is None or not nodes:
            raise EngineError("invalid_params", "Replacing filters requires an existing search and at least one term.")
        search = existing
        for node in nodes:
            search = native_search(col.replace_in_search_node, SearchNode(parsable_text=search), node)
    elif existing is not None and nodes:
        search = native_search(col.join_searches, SearchNode(parsable_text=existing), col.group_searches(*nodes, joiner=joiner), joiner)
    else:
        search = native_search(col.build_search_string, *(nodes or [existing or ""]), joiner=joiner)
    if len(search) > 65536:
        raise EngineError("invalid_params", "The combined search is too long.")
    return {"search": search}
