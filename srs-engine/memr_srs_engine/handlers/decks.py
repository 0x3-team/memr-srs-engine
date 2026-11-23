"""Deck operations use Anki's native hierarchy, scheduler counts and undo."""
import re
import unicodedata

# Collection must load first: importing anki.decks alone creates a circular
# import in the pinned wheel's hook initialization.
from ..collection import mutation_result, undo_state
from anki.decks import DeckCollapseScope
from anki.stats_pb2 import RevlogEntry
from ..errors import EngineError
from ..registry import method


def _result(col, changes, **payload):
    result = mutation_result(undo_state(col))
    aliases = {"deckConfig": "deck_config", "browserTable": "browser_table",
               "noteText": "note_text", "studyQueues": "study_queues"}
    result["changes"] = {key: bool(getattr(changes, aliases.get(key, key)))
                         for key in result["changes"]}
    return {**result, **payload}


def _deck(col, deck_id):
    # DeckManager.get() otherwise silently returns Default for unknown ids.
    deck = col.decks.get(deck_id, default=False)
    if deck is None:
        raise EngineError("not_found", "The requested deck is unavailable.")
    return deck


def _selection(col, deck_ids):
    if not deck_ids:
        raise EngineError("invalid_params", "Select at least one deck.")
    selected = list(dict.fromkeys(deck_ids))
    for deck_id in selected:
        _deck(col, deck_id)
    return selected


def _text(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise EngineError("invalid_params", "Text must contain valid Unicode characters.") from None
    return value


def _name(col, value):
    _text(value)
    parts = value.split("::")
    if any(not part.strip() for part in parts) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise EngineError("invalid_params", "Deck names must have nonempty components without control characters.")
    # Match Ankitects' 26.09.3 decks/name.rs (AGPL-3.0): NFC, trim mixed
    # whitespace/colon edges, then substitute blank. Controls are rejected above.
    parts = [re.sub(r"^[\s:]+|[\s:]+$", "", unicodedata.normalize("NFC", part)) or "blank"
             for part in parts]
    name = "::".join(parts)
    # Anki retains an existing ancestor's spelling. Unicode-equivalent
    # casing can have a different length, so bound the name it will store.
    for depth in range(len(parts) - 1, 0, -1):
        ancestor_id = col.decks.id_for_name("::".join(parts[:depth]))
        if ancestor_id is not None:
            name = _deck(col, ancestor_id)["name"] + "::" + "::".join(parts[depth:])
            break
    if len(name) > 1024:
        raise EngineError("invalid_params", "The normalized deck name is too long.")
    return name


def _bounded_subtree(col, names, deck_id, new_name):
    """Preflight native prefix moves and '+' collisions without writing data."""
    occupied = {}
    for item_id, name in names.items():
        occupied.setdefault(name.casefold(), []).append((item_id, name))
    # Python casefold narrows candidates, but Anki's collation determines
    # uniqueness (for example, it does not fold the 'ﬃ' ligature to 'ffi').
    while any(item_id != deck_id and col.db.scalar("select ? = ? collate unicase", new_name, name)
              for item_id, name in occupied.get(new_name.casefold(), [])):
        new_name += "+"
    old_name = names[deck_id]
    updates = {}
    for item_id, name in names.items():
        if item_id == deck_id or name.startswith(old_name + "::"):
            renamed = new_name + name[len(old_name):]
            if len(renamed) > 1024:
                raise EngineError("invalid_params", "The resulting deck hierarchy has a name that is too long.")
            updates[item_id] = renamed
    names.update(updates)


def _normal_parent(col, parent_id):
    parent = _deck(col, parent_id)
    if parent["dyn"]:
        raise EngineError("invalid_params", "A filtered deck cannot contain child decks.")
    return parent


def _nodes(root):
    pending = [root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.children))


def _view(deck, node, by_name):
    parent_name = deck["name"].rsplit("::", 1)[0] if "::" in deck["name"] else None
    return {
        "id": deck["id"], "parentId": by_name.get(parent_name), "name": deck["name"],
        "description": deck.get("desc", ""), "descriptionMarkdown": bool(deck.get("md", False)),
        "collapsed": bool(deck["collapsed"]), "browserCollapsed": bool(deck["browserCollapsed"]),
        "filtered": bool(deck["dyn"]), "presetId": None if deck["dyn"] else deck["conf"],
        "counts": {"new": node.new_count if node else 0, "learning": node.learn_count if node else 0,
                   "review": node.review_count if node else 0},
        "totalCards": node.total_including_children if node else 0,
    }


@method("decks.tree")
def tree(engine, params):
    col = engine.col
    top_id = params.get("topDeckId")
    if top_id is not None:
        _deck(col, top_id)
    root = col.sched.deck_due_tree(top_id)
    decks = {deck["id"]: deck for deck in col.decks.get_all_legacy()}
    by_name = {deck["name"]: deck["id"] for deck in decks.values()}
    # Anki omits an empty Default deck when other decks exist. A direct
    # subtree request still returns that existing deck with zero counts.
    views = []
    if root is not None:
        views = [_view(decks[node.deck_id], node, by_name)
                 for node in _nodes(root) if node.deck_id]
    if top_id is not None and not views:
        views = [_view(decks[top_id], None, by_name)]
    return {"decks": views, "currentDeckId": col.decks.selected()}


@method("decks.get")
def get(engine, params):
    col = engine.col
    deck = _deck(col, params["deckId"])
    node = col.sched.deck_due_tree(deck["id"])
    by_name = {item.name: item.id for item in col.decks.all_names_and_ids()}
    return _view(deck, node, by_name)


@method("decks.create")
def create(engine, params):
    col = engine.col
    name = params["name"]
    if params.get("parentId") is not None:
        name = _normal_parent(col, params["parentId"])["name"] + "::" + name
    name = _name(col, name)
    existing = col.decks.id_for_name(name)
    if existing is not None and _deck(col, existing)["dyn"]:
        raise EngineError("conflict", "A filtered deck already uses that name.")
    out = col.decks.add_normal_deck_with_name(name)
    return _result(col, out.changes, deckId=out.id)


@method("decks.rename")
def rename(engine, params):
    col = engine.col
    _deck(col, params["deckId"])
    name = _name(col, params["name"])
    names = {item.id: item.name for item in col.decks.all_names_and_ids()}
    _bounded_subtree(col, names, params["deckId"], name)
    changes = col.decks.rename(params["deckId"], name)
    return _result(col, changes)


@method("decks.reparent")
def reparent(engine, params):
    col = engine.col
    deck_ids = _selection(col, params["deckIds"])
    parent_id = params["parentId"]
    if parent_id is not None:
        _normal_parent(col, parent_id)
    names = {item.id: item.name for item in col.decks.all_names_and_ids()}
    target = names[parent_id] if parent_id is not None else None
    for deck_id in deck_ids:
        old_name = names[deck_id]
        # Match Anki's drag no-op check before constructing prospective names.
        if target is not None and target.startswith(old_name) and target.split("::")[0] == old_name.split("::")[0]:
            continue
        name = old_name.rsplit("::", 1)[-1]
        if target is not None:
            name = target + "::" + name
        _bounded_subtree(col, names, deck_id, name)
    out = col.decks.reparent(deck_ids, parent_id or 0)
    return _result(col, out.changes)


@method("decks.remove")
def remove(engine, params):
    col = engine.col
    deck_ids = _selection(col, params["deckIds"])
    if not params["deleteCards"]:
        raise EngineError("unsupported", "Removing a deck while preserving its cards is not supported.")
    before = {item.id for item in col.decks.all_names_and_ids()}
    out = col.decks.remove(deck_ids)
    after = {item.id for item in col.decks.all_names_and_ids()}
    # OpChangesWithCount.count counts deleted cards, not removed decks.
    return _result(col, out.changes, removedCount=len(before - after))


@method("decks.setCollapsed")
def set_collapsed(engine, params):
    col = engine.col
    _deck(col, params["deckId"])
    scope = DeckCollapseScope.BROWSER if params.get("browser", False) else DeckCollapseScope.REVIEWER
    changes = col.decks.set_collapsed(params["deckId"], params["collapsed"], scope)
    return _result(col, changes)


@method("decks.setCurrent")
def set_current(engine, params):
    col = engine.col
    _deck(col, params["deckId"])
    return _result(col, col.decks.set_current(params["deckId"]))


@method("decks.setDescription")
def set_description(engine, params):
    col = engine.col
    deck = _deck(col, params["deckId"])
    if deck["dyn"]:
        raise EngineError("unsupported", "Filtered decks do not have descriptions.")
    deck["desc"] = _text(params["description"])
    if "markdown" in params:
        deck["md"] = params["markdown"]
    return _result(col, col.decks.update_dict(deck))


@method("decks.todaySummary")
def today_summary(engine, _params):
    # Match Ankitects' 26.09.3 storage/revlog/studied_today.sql (AGPL-3.0),
    # including its strict cutoff. Graphs use a different boundary, and the
    # public studied_today() API returns localized text instead of numbers.
    col = engine.col
    cutoff = (col.sched.day_cutoff - 86400) * 1000
    answers, seconds, again, correct = col.db.first(
        """select count(), coalesce(sum(time) / 1000.0, 0.0),
                  coalesce(sum(ease = 1), 0), coalesce(sum(ease > 1), 0)
           from revlog where id > ? and type != ? and type != ?""",
        cutoff, RevlogEntry.MANUAL, RevlogEntry.RESCHEDULED)
    return {"cards": answers, "reviews": answers, "elapsedSeconds": seconds,
            "averageSeconds": seconds / answers if answers else 0,
            "againCount": again, "correctCount": correct}
