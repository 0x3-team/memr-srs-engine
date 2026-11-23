"""Review sessions delegate scheduling and undo exclusively to Anki's core."""
import hashlib
import json
import time

from ..collection import mutation_result, undo_state
from ..errors import EngineError
from ..registry import method
from ..render import compare_typed_answer, render_card
from .decks import _deck, _result, _text, today_summary
from anki import errors as anki_errors
from anki.cards import Card
from anki.consts import (
    QUEUE_TYPE_MANUALLY_BURIED,
    QUEUE_TYPE_SIBLING_BURIED,
    QUEUE_TYPE_SUSPENDED,
)
from anki.scheduler.base import CustomStudyRequest, UnburyDeck
from anki.scheduler.v3 import CardAnswer


def _select(engine, deck_id):
    # JSON Schema integers include integral JSON numbers such as 1.0, while
    # Anki's protobuf-backed calls require Python int values.
    deck_id = int(deck_id)
    deck = _deck(engine.col, deck_id)
    if engine.col.decks.get_current_id() != deck_id:
        engine.col.decks.set_current(deck_id)
        engine._study = None
    return deck


def _counts(queued):
    return {"new": queued.new_count, "learning": queued.learning_count,
            "review": queued.review_count}


def _scope(col, deck_id):
    # IDs come from Anki, never from interpolated client input.
    return ",".join(str(value) for value in col.decks.deck_and_child_ids(int(deck_id)))


def _inactive_counts(col, deck_id):
    return col.db.first(
        f"select coalesce(sum(queue in (-2,-3)),0), coalesce(sum(queue=-1),0) "
        f"from cards where did in ({_scope(col, deck_id)})")


def _limits(info, counts):
    # Congratulations reports uncapped availability; a limit is reached when
    # those cards cannot be admitted to the current queue.
    return {"newLimitReached": bool(info.new_remaining and not counts["new"]),
            "reviewLimitReached": bool(info.review_remaining and not counts["review"])}


def _token(payload):
    """Optional SHA-256 token of compact JSON with these keys in this order.

    UTF-8 JSON uses unescaped Unicode and the listed key order, including each
    nextStates item's rating, label and secondsUntilDue order. Clients can
    derive it without a new render key; tokenless answers remain supported.
    """
    projection = {key: payload[key] for key in ("cardId", "noteId", "ord", "deckId", "queue", "nextStates")}
    return hashlib.sha256(json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _without_learning_clock(states):
    """Compare scheduling choices without Anki's advancing intraday clock."""
    snapshot = type(states)()
    snapshot.CopyFrom(states)
    for name in ("current", "again", "hard", "good", "easy"):
        state = getattr(snapshot, name)
        if state.WhichOneof("kind") == "normal":
            normal = state.normal
        elif state.filtered.WhichOneof("kind") == "rescheduling":
            normal = state.filtered.rescheduling.original_state
        else:
            continue
        kind = normal.WhichOneof("kind")
        if kind == "learning":
            normal.learning.ClearField("elapsed_secs")
        elif kind == "relearning":
            normal.relearning.learning.ClearField("elapsed_secs")
    return snapshot


@method("study.overview")
def overview(engine, params):
    col = engine.col
    deck = _select(engine, params["deckId"])
    counts = _counts(col.sched.get_queued_cards())
    info = col.sched.congratulations_info()
    buried, suspended = _inactive_counts(col, deck["id"])
    # The native overview reads the deck directly; congratulations_info does
    # not supply its description while cards remain. Match aqt.overview._desc.
    description = deck.get("desc", "")
    if deck.get("md", False):
        description = col.render_markdown(description)
    return {"deckId": deck["id"], "name": deck["name"], "description": description,
            "counts": counts, "buried": buried, "suspended": suspended, **_limits(info, counts)}


@method("study.next")
def next_card(engine, params):
    col = engine.col
    if col.get_config("cardStateCustomizer", ""):
        # Validate the identifier without changing selection or undo state on
        # a request whose scheduling configuration cannot be supported.
        _deck(col, int(params["deckId"]))
        raise EngineError("unsupported", "JavaScript scheduling customizers are not supported.")
    deck = _select(engine, params["deckId"])
    queued = col.sched.get_queued_cards()
    if not queued.cards:
        engine._study = None
        return None
    top = queued.cards[0]
    card = Card(col, backend_card=top.card)
    # Match aqt/reviewer.py V3CardInfo: preserve the shown card's custom data.
    top.states.current.custom_data = top.card.custom_data
    card.start_timer()
    payload = render_card(col, card, states=top.states, counts=_counts(queued))
    # The queued card can contain synthesized FSRS memory/decay fields. Keep
    # the persisted card separately so later edits compare like with like.
    persisted = col.get_card(card.id)._to_backend_card()
    engine._study = (deck["id"], card, top.states, _token(payload), persisted)
    return payload


@method("study.answer")
def answer(engine, params):
    col = engine.col
    card_id = int(params["cardId"])
    current_card = col.get_card(card_id)
    if col.get_config("cardStateCustomizer", ""):
        raise EngineError("unsupported", "JavaScript scheduling customizers are not supported.")
    shown = getattr(engine, "_study", None)
    if shown is None or shown[1].id != card_id:
        raise EngineError("conflict", "Fetch the current study card before answering.")
    deck_id, card, states, token, persisted = shown
    # A move can preserve scheduling states while changing the timer preset.
    if (current_card.did, current_card.odid) != (card.did, card.odid):
        raise EngineError("conflict", "The study card's deck has changed; fetch it again.")
    # A due-date edit can change only elapsed_secs in the computed states.
    # Check persisted data before excluding that clock from the comparison.
    if current_card._to_backend_card() != persisted:
        raise EngineError("conflict", "The study card has changed; fetch it again.")
    if params.get("stateToken", token) != token or col.decks.get_current_id() != deck_id:
        raise EngineError("conflict", "The study card has changed; fetch it again.")
    queued = col.sched.get_queued_cards()
    if not queued.cards or queued.cards[0].card.id != card.id:
        raise EngineError("conflict", "The study queue has changed; fetch it again.")
    current_states = queued.cards[0].states
    current_states.current.custom_data = queued.cards[0].card.custom_data
    if _without_learning_clock(current_states) != _without_learning_clock(states):
        raise EngineError("conflict", "The scheduling state has changed; fetch it again.")
    ratings = (CardAnswer.AGAIN, CardAnswer.HARD, CardAnswer.GOOD, CardAnswer.EASY)
    built = col.sched.build_answer(card=card, states=states, rating=ratings[int(params["rating"]) - 1])
    # Clamp first: a finite JSON duration can overflow when multiplied by 1000.
    elapsed_seconds = min(params["elapsedSeconds"], card.time_limit() / 1000)
    built.milliseconds_taken = int(elapsed_seconds * 1000)
    leeched = col.sched.state_is_leech(built.new_state)
    changes = col.sched.answer_card(built)
    engine._study = None
    leech = None
    if leeched:
        updated = col.get_card(card.id)
        leech = {"tagged": updated.note().has_tag("leech"), "suspended": updated.queue == -1}
    return _result(col, changes, counts=_counts(col.sched.get_queued_cards()), leech=leech)


_REPLAY_RATINGS = (CardAnswer.AGAIN, CardAnswer.HARD, CardAnswer.GOOD, CardAnswer.EASY)
_REPLAY_BURIED_QUEUES = (QUEUE_TYPE_SIBLING_BURIED, QUEUE_TYPE_MANUALLY_BURIED)


def _replay_card_answer(col, card, rating, elapsed_seconds, answered_at):
    states = col._backend.get_scheduling_states(card.id)
    new_state = (states.again, states.hard, states.good, states.easy)[rating - 1]
    return CardAnswer(
        card_id=card.id,
        current_state=states.current,
        new_state=new_state,
        rating=_REPLAY_RATINGS[rating - 1],
        answered_at_millis=int(answered_at * 1000),
        milliseconds_taken=int(min(elapsed_seconds, card.time_limit() / 1000) * 1000))


@method("study.replayAnswers")
def replay_answers(engine, params):
    """Apply journaled answers to cards from their current state, in order.

    Each entry is an independent commit: one failing answer never stops or
    reverts the others. The host deduplicates answer ids in its journal, so
    the engine does not store them; skipped entries carry only a reason.
    """
    col = engine.col
    applied = []
    skipped = []
    merged = {key: False for key in mutation_result()["changes"]}
    aliases = {"deckConfig": "deck_config", "browserTable": "browser_table",
               "noteText": "note_text", "studyQueues": "study_queues"}
    for entry in params["answers"]:
        answer_id = entry["id"]
        card_id = int(entry["cardId"])
        try:
            card = col.get_card(card_id)
        except (anki_errors.NotFoundError, anki_errors.DeletedError):
            card = None
        if card is None:
            skipped.append({"id": answer_id, "reason": "missing"})
            continue
        if card.queue == QUEUE_TYPE_SUSPENDED:
            skipped.append({"id": answer_id, "reason": "suspended"})
            continue
        if card.queue in _REPLAY_BURIED_QUEUES:
            skipped.append({"id": answer_id, "reason": "buried"})
            continue
        rating = int(entry["rating"])
        elapsed = entry["elapsedSeconds"]
        answered_at = entry["answeredAt"]
        # Normal answering computes scheduling states with the study queue's
        # load balancer built; rebuilding the queue here keeps replay on the
        # same interval path as a live session. The load balancer's day map is
        # per-session state, so a ±1-day easy-interval difference from another
        # session's pick is possible when load balancing is enabled; with it
        # off, scheduling is a pure function of collection state.
        try:
            col.sched.get_queued_cards()
            built = _replay_card_answer(col, card, rating, elapsed, answered_at)
        except Exception:
            built = None
        # The backend's queue-position check rejects a card that is not at the
        # head of a built queue; the failed transaction clears the queues, so a
        # retry of the identical answer commits the same state transition
        # (including the already-computed interval) without depending on queue
        # position. Any other failure skips the entry.
        changes = None
        if built is not None:
            try:
                changes = col.sched.answer_card(built)
            except Exception:
                try:
                    changes = col.sched.answer_card(built)
                except Exception:
                    changes = None
        if changes is None:
            skipped.append({"id": answer_id, "reason": "failed"})
            continue
        applied.append(answer_id)
        for key in merged:
            merged[key] = merged[key] or bool(getattr(changes, aliases.get(key, key)))
    result = mutation_result(undo_state(col))
    result["changes"] = merged
    return {**result, "applied": applied, "skipped": skipped}


def _undo_redo(engine, redo, expected_revision=None):
    col = engine.col
    if expected_revision is not None and expected_revision != engine.undo_revision():
        raise EngineError("conflict", "The latest collection action has changed. Refresh before undoing or redoing.")
    changes = (col.redo() if redo else col.undo()).changes
    engine._study = None
    # A successful undo/redo must report changes even if the resulting state
    # requires a JavaScript customizer that this engine cannot execute.
    card = (None if col.get_config("cardStateCustomizer", "")
            else next_card(engine, {"deckId": col.decks.get_current_id()}))
    return _result(col, changes, card=card)


@method("study.undo")
def undo(engine, params):
    return _undo_redo(engine, False, params.get("expectedRevision"))


@method("study.redo")
def redo(engine, params):
    return _undo_redo(engine, True, params.get("expectedRevision"))


@method("study.undoStatus")
def undo_status(engine, _params):
    return undo_state(engine.col)


@method("study.compareTypedAnswer")
def compare_answer(engine, params):
    card = engine.col.get_card(int(params["cardId"]))
    return compare_typed_answer(engine.col, card, _text(params["typedAnswer"]))


@method("study.congratulations")
def congratulations(engine, params):
    col = engine.col
    _select(engine, params["deckId"])
    info = col.sched.congratulations_info()
    counts = _counts(col.sched.get_queued_cards())
    buried, suspended = _inactive_counts(col, params["deckId"])
    reviews_tomorrow = col.db.scalar(
        f"select count() from cards where did in ({_scope(col, params['deckId'])}) "
        "and queue in (2,3) and due=?", col.sched.today + 1)
    return {"today": today_summary(engine, {}),
            "nextDueAt": int(time.time()) + info.secs_until_next_learn if info.learn_remaining else None,
            "reviewsTomorrow": reviews_tomorrow, "buried": buried, "suspended": suspended,
            "canCustomStudy": not info.is_filtered_deck, **_limits(info, counts)}


@method("study.customStudyDefaults")
def custom_study_defaults(engine, params):
    deck = _deck(engine.col, int(params["deckId"]))
    if deck["dyn"]:
        raise EngineError("invalid_params", "Custom study requires a normal deck.")
    defaults = engine.col.sched.custom_study_defaults(deck["id"])
    return {"extendNew": defaults.extend_new, "extendReview": defaults.extend_review,
            "availableNew": defaults.available_new, "availableReview": defaults.available_review,
            "availableNewInChildren": defaults.available_new_in_children,
            "availableReviewInChildren": defaults.available_review_in_children,
            "tags": [{"name": tag.name, "include": tag.include, "exclude": tag.exclude}
                     for tag in defaults.tags]}


@method("study.customStudy")
def custom_study(engine, params):
    col = engine.col
    deck = _deck(col, int(params["deckId"]))
    if deck["dyn"]:
        raise EngineError("invalid_params", "Custom study requires a normal deck.")
    request = CustomStudyRequest(deck_id=deck["id"])
    kind = params["kind"]
    values = {"increaseNew": "new_limit_delta", "increaseReview": "review_limit_delta",
              "forgotten": "forgot_days", "ahead": "review_ahead_days", "previewNew": "preview_days"}
    if kind == "byTags":
        states = {"due": CustomStudyRequest.Cram.CRAM_KIND_DUE,
                  "new": CustomStudyRequest.Cram.CRAM_KIND_NEW,
                  "review": CustomStudyRequest.Cram.CRAM_KIND_REVIEW,
                  "all": CustomStudyRequest.Cram.CRAM_KIND_ALL}
        tags = [_text(tag) for tag in params.get("tags", [])]
        excluded = [_text(tag) for tag in params.get("excludeTags", [])]
        request.cram.CopyFrom(CustomStudyRequest.Cram(
            kind=states[params.get("cardState", "all")], card_limit=int(params["amount"]),
            tags_to_include=tags, tags_to_exclude=excluded))
    else:
        if any(key in params for key in ("tags", "excludeTags", "cardState")):
            raise EngineError("invalid_params", "Tag options apply only to study by tags.")
        setattr(request, values[kind], int(params["amount"]))
    changes = col.sched.custom_study(request)
    engine._study = None
    result_deck_id = deck["id"] if kind.startswith("increase") else col.decks.get_current_id()
    return _result(col, changes, deckId=result_deck_id)


@method("study.unbury")
def unbury(engine, params):
    col = engine.col
    deck_id = int(params["deckId"])
    _deck(col, deck_id)
    kind = params.get("kind", "all")
    modes = {"all": UnburyDeck.ALL, "manual": UnburyDeck.USER_ONLY, "siblings": UnburyDeck.SCHED_ONLY}
    queues = {"all": "-2,-3", "manual": "-3", "siblings": "-2"}
    count = col.db.scalar(f"select count() from cards where did in ({_scope(col, deck_id)}) "
                          f"and queue in ({queues[kind]})")
    return _result(col, col.sched.unbury_deck(deck_id, modes[kind]), count=count)
