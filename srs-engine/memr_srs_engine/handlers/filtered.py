"""Native filtered deck configuration and undoable, cancellable membership changes."""
from .decks import _deck, _name, _normal_parent, _result, _text
from .search import native_search
from anki.decks_pb2 import Deck
from ..collection import mutation_result, undo_state
from ..errors import EngineError
from ..registry import method
from ..validate import validate_value

ORDERS = ("oldest", "random", "intervalAscending", "intervalDescending", "lapses", "added", "due",
          "reverseAdded", "retrievabilityAscending", "retrievabilityDescending", "relativeOverdueness")


def _filtered(col, deck_id):
    deck = _deck(col, int(deck_id))
    if not deck["dyn"]:
        raise EngineError("invalid_params", "Select a filtered deck.")
    return deck


def _view(engine, value):
    config = value.config
    try:
        result = {"deckId": value.id, "name": value.name,
                  "terms": [{"search": t.search, "limit": t.limit, "order": ORDERS[t.order]} for t in config.search_terms],
                  "reschedule": config.reschedule, "stepsMinutes": list(config.delays),
                  "previewAgainSeconds": config.preview_again_secs, "previewHardSeconds": config.preview_hard_secs,
                  "previewGoodSeconds": config.preview_good_secs}
    except IndexError:
        raise EngineError("unsupported", "This filtered deck uses an unsupported search order.") from None
    if validate_value(engine.registry.catalogue["filtered.defaults"]["result"], result):
        raise EngineError("unsupported", "The filtered deck settings exceed the supported display limits.")
    return result


@method("filtered.defaults")
def defaults(engine, _params):
    return _view(engine, engine.col.sched.get_or_create_filtered_deck(0))


@method("filtered.get")
def get(engine, params):
    deck = _filtered(engine.col, params["deckId"])
    return _view(engine, engine.col.sched.get_or_create_filtered_deck(deck["id"]))


@method("filtered.createOrUpdate")
def create_or_update(engine, params):
    col, config = engine.col, params["config"]
    deck_id = int(config["deckId"])
    existing = _filtered(col, deck_id) if deck_id else None
    name = _name(col, config["name"])
    if not name.strip() or len(name) > 255:
        raise EngineError("invalid_params", "The normalized filtered-deck name must contain 1 to 255 characters.")
    if "::" in name:
        parent_id = col.decks.id_for_name(name.rsplit("::", 1)[0])
        if parent_id is not None:
            _normal_parent(col, parent_id)
    other_id = col.decks.id_for_name(name)
    if other_id is not None and other_id != deck_id:
        raise EngineError("conflict", "A deck with this name already exists.")
    if not config["terms"]:
        raise EngineError("invalid_params", "Use one or two search terms.")
    for term in config["terms"]:
        native_search(col.build_search_string, _text(term["search"]))
    if existing:
        if get(engine, {"deckId": deck_id}) == {**config, "name": name}:
            return {**mutation_result(undo_state(col)), "deckId": deck_id}
        existing.update(name=name, terms=[[term["search"], int(term["limit"]), ORDERS.index(term["order"])] for term in config["terms"]],
                        resched=config["reschedule"], delays=config["stepsMinutes"] or None,
                        previewAgainSecs=int(config["previewAgainSeconds"]), previewHardSecs=int(config["previewHardSeconds"]),
                        previewGoodSecs=int(config["previewGoodSeconds"]))
        return _result(col, col.decks.update_dict(existing), deckId=deck_id)
    deck = col.decks.new_deck()
    deck.name = name
    deck.filtered.CopyFrom(Deck.Filtered(
        reschedule=config["reschedule"], delays=config["stepsMinutes"],
        search_terms=[Deck.Filtered.SearchTerm(search=t["search"], limit=int(t["limit"]), order=ORDERS.index(t["order"])) for t in config["terms"]],
        preview_again_secs=int(config["previewAgainSeconds"]), preview_hard_secs=int(config["previewHardSeconds"]),
        preview_good_secs=int(config["previewGoodSeconds"])))
    result = col.decks.add_deck(deck)
    return _result(col, result.changes, deckId=result.id)


@method("filtered.rebuild")
def rebuild(engine, params):
    engine.progress.check_abort()
    deck = _filtered(engine.col, params["deckId"])
    if any(term[2] in (8, 9) for term in deck["terms"]) and not engine.col.decks.get_deck_configs_for_update(1).fsrs:
        # Pinned Anki otherwise builds an empty SQL ordering expression. Reject
        # this unsupported combination before its native mutation transaction.
        raise EngineError("unsupported", "Retrievability ordering requires FSRS to be enabled.")
    engine.progress.check_abort()
    result = native_search(engine.col.sched.rebuild_filtered_deck, deck["id"])
    # Do not turn a committed native mutation into an aborted result afterwards.
    return _result(engine.col, result.changes, count=result.count)


@method("filtered.empty")
def empty(engine, params):
    engine.progress.check_abort()
    deck = _filtered(engine.col, params["deckId"])
    count = engine.col.db.scalar("select count() from cards where did=?", deck["id"])
    engine.progress.check_abort()
    result = engine.col.sched.empty_filtered_deck(deck["id"])
    return _result(engine.col, result, count=count)
