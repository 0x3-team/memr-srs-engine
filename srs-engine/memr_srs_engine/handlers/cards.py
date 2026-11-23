"""Card catalogue operations backed by the pinned Anki collection and scheduler."""
import re

from anki.collection import OpChanges

from ..errors import EngineError
from ..registry import method
from .decks import _deck, _result, _text


CARD_TYPES = ("new", "learning", "review", "relearning")
REVIEW_KINDS = ("learning", "review", "relearning", "filtered", "manual", "rescheduled")
FLAG_COLORS = ("red", "orange", "green", "blue", "pink", "turquoise", "purple")
MAX_POSITION = 2**31 - 1


def _selection(col, card_ids, scope="card"):
    if not card_ids:
        raise EngineError("invalid_params", "Select at least one card.")
    cards = [col.get_card(int(card_id)) for card_id in dict.fromkeys(card_ids)]
    if scope == "note":
        note_ids = list(dict.fromkeys(card.nid for card in cards))
        for note_id in note_ids:
            col.get_note(note_id)
        cards = [col.get_card(card_id) for note_id in note_ids
                 for card_id in col.card_ids_of_note(note_id)]
    return cards


def _ids(cards):
    return [card.id for card in cards]


@method("cards.info")
def info(engine, params):
    col = engine.col
    card = col.get_card(int(params["cardId"]))
    stats = col.card_stats_data(card.id)
    # Native stats lazily persists inferred metadata, including last-review time.
    # Account for that read only if the shown card matched before it began;
    # actual edits must still fail the complete persisted-card check in answer().
    shown = getattr(engine, "_study", None)
    if shown is not None and shown[4] == card._to_backend_card():
        engine._study = (*shown[:4], col.get_card(card.id)._to_backend_card())
    memory = None
    # Anki can infer a missing memory state without computing retrievability.
    # The contract requires all three values, so expose only complete states.
    if stats.HasField("memory_state") and stats.HasField("fsrs_retrievability"):
        memory = {"stability": stats.memory_state.stability,
                  "difficulty": stats.memory_state.difficulty,
                  "retrievability": stats.fsrs_retrievability}
    # Stats converts millisecond revlog ids to seconds. Read only the original
    # identities; the backend supplies history order, kinds and unit conversion.
    review_ids = col.db.list("select id from revlog where cid=? order by id desc", card.id)
    reviews = [{"id": review_id, "reviewedAt": entry.time, "rating": entry.button_chosen,
                "intervalDays": entry.interval / 86400, "lastIntervalDays": entry.last_interval / 86400,
                "ease": entry.ease / 1000, "elapsedSeconds": entry.taken_secs,
                "kind": REVIEW_KINDS[entry.review_kind]}
               for review_id, entry in zip(review_ids, stats.revlog, strict=True)]
    return {"card": {
        "id": card.id, "noteId": card.nid, "deckId": card.did,
        "originalDeckId": card.odid or None, "ord": card.ord, "queue": int(card.queue),
        "type": CARD_TYPES[card.type], "due": card.due, "originalDue": card.odue,
        "intervalDays": stats.interval, "ease": stats.ease / 1000,
        "repetitions": stats.reviews, "lapses": stats.lapses, "flags": card.user_flag(),
        "position": stats.due_position if stats.HasField("due_position") else None,
        "modifiedAt": card.mod, "memoryState": memory,
    }, "reviews": reviews}


@method("cards.preview")
def preview(engine, params):
    card = engine.col.get_card(int(params["cardId"]))
    # Imported on demand so the shared renderer does not depend on this module.
    from ..render import render_card
    return render_card(engine.col, card, fill_empty=params.get("fillEmpty", False))


@method("cards.bury")
def bury(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"], params.get("scope", "card"))
    out = col.sched.bury_cards(_ids(cards))
    return _result(col, out.changes, count=out.count)


def _restore(engine, params, queues, operation):
    col = engine.col
    cards = _selection(col, params["cardIds"], params.get("scope", "card"))
    # Both Anki restore methods restore *all* negative queues. Keep the public
    # operations distinct, including when sibling cards have mixed states.
    card_ids = [card.id for card in cards if card.queue in queues]
    changes = operation(card_ids) if card_ids else OpChanges()
    return _result(col, changes, count=len(card_ids))


@method("cards.unbury")
def unbury(engine, params):
    return _restore(engine, params, (-3, -2), engine.col.sched.unbury_cards)


@method("cards.suspend")
def suspend(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"], params.get("scope", "card"))
    out = col.sched.suspend_cards(_ids(cards))
    return _result(col, out.changes, count=out.count)


@method("cards.unsuspend")
def unsuspend(engine, params):
    return _restore(engine, params, (-1,), engine.col.sched.unsuspend_cards)


@method("cards.flag")
def flag(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"])
    out = col.set_user_flag_for_cards(int(params["flag"]), _ids(cards))
    return _result(col, out.changes, count=out.count)


@method("cards.forget")
def forget(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"])
    changes = col.sched.schedule_cards_as_new(
        _ids(cards), reset_counts=params.get("resetCounts", False),
        restore_position=params.get("restorePosition", False))
    return _result(col, changes, count=len(cards))


@method("cards.setDueDate")
def set_due_date(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"])
    days = params["days"]
    match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?!?", days)
    maximum = max(int(value) for value in match.groups() if value is not None) if match else None
    if maximum is None or maximum > MAX_POSITION or maximum > MAX_POSITION - col.sched.today:
        raise EngineError("invalid_params", "Use a nonnegative day or day range, optionally followed by !.")
    if params.get("forceReset", False) and not days.endswith("!"):
        days += "!"
    changes = col.sched.set_due_date(_ids(cards), days)
    return _result(col, changes, count=len(cards))


@method("cards.reposition")
def reposition(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"])
    start, step = int(params["start"]), int(params["step"])
    # Anki's protobuf uses u32, but positions are stored as signed i32. Its
    # sorter includes non-new cards when assigning note positions and shifts
    # by the number of selected cards, not the number of distinct notes.
    last_position = start + (len({card.nid for card in cards}) - 1) * step
    if start > MAX_POSITION or step > MAX_POSITION or last_position > MAX_POSITION:
        raise EngineError("invalid_params", "The requested positions exceed the supported range.")
    if params["shiftExisting"]:
        shift = step * len(cards)
        highest = col.db.scalar("select max(due) from cards where type=0 and due>=?", start)
        if shift > 2**32 - 1 or (highest is not None and highest + shift > MAX_POSITION):
            raise EngineError("invalid_params", "Shifting existing cards would exceed the supported range.")
    out = col.sched.reposition_new_cards(
        _ids(cards), starting_from=start, step_size=step,
        randomize=params["randomize"], shift_existing=params["shiftExisting"])
    return _result(col, out.changes, count=out.count)


@method("cards.setDeck")
def set_deck(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"])
    deck = _deck(col, int(params["deckId"]))
    if deck["dyn"]:
        raise EngineError("invalid_params", "Cards can only be moved to a normal deck.")
    out = col.set_deck(_ids(cards), deck["id"])
    return _result(col, out.changes, count=out.count)


@method("cards.remove")
def remove(engine, params):
    col = engine.col
    cards = _selection(col, params["cardIds"])
    out = col.remove_cards_and_orphaned_notes(_ids(cards))
    return _result(col, out.changes, count=out.count)


@method("cards.userFlagNames")
def user_flag_names(engine, _params):
    col = engine.col
    labels = col.get_config("flagLabels", {})
    return {"names": [labels.get(str(index), getattr(col.tr, "actions_flag_" + color)())
                      for index, color in enumerate(FLAG_COLORS, 1)]}


@method("cards.setUserFlagNames")
def set_user_flag_names(engine, params):
    names = params["names"]
    if len(names) != 7:
        raise EngineError("invalid_params", "Provide exactly seven flag names.")
    # Empty labels restore Anki's localized default rather than hiding a flag.
    labels = {str(index): _text(name) for index, name in enumerate(names, 1) if name}
    return _result(engine.col, engine.col.set_config("flagLabels", labels, undoable=True))
