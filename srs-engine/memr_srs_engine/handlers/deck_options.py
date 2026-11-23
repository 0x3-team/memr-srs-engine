"""Native preset editing without interpreting custom scheduling JavaScript."""
from datetime import date, datetime
import struct

from ..collection import mutation_result, undo_state
from anki import deck_config_pb2 as native
from ..errors import EngineError
from ..registry import method
from .decks import _deck, _result, _text

NEW_GATHER = ("deck", "ascendingPosition", "descendingPosition", "randomNotes", "randomCards", "deckThenRandomNotes")
NEW_SORT = ("templateThenPosition", "position", "templateThenRandom", "randomNoteThenTemplate", "random")
REVIEW_ORDER = ("due", "dueThenDeck", "deckThenDue", "intervalAscending", "intervalDescending", "easeAscending",
                "easeDescending", "retrievabilityAscending", "random", "added", "reverseAdded", "retrievabilityDescending", "relativeOverdueness")
MIX = ("mix", "afterReviews", "beforeReviews")
ANSWER_ACTION = ("bury", "again", "good", "hard", "showReminder")
QUESTION_ACTION = ("showAnswer", "showReminder")

# The groups are an explicit mapping of the pinned protobuf, not legacy-dict
# passthrough. Unknown protobuf/legacy fields remain on the fetched message.
FIELDS = {
    "dailyLimits": {"new": "new_per_day", "review": "reviews_per_day"},
    "newCards": {"stepsMinutes": "learn_steps", "graduatingIntervalDays": "graduating_interval_good", "easyIntervalDays": "graduating_interval_easy"},
    "lapses": {"stepsMinutes": "relearn_steps", "minimumIntervalDays": "minimum_lapse_interval", "leechThreshold": "leech_threshold"},
    "fsrs": {"desiredRetention": "desired_retention", "parameters": "fsrs_params_6", "parameters4": "fsrs_params_4", "parameters5": "fsrs_params_5",
             "historicalRetention": "historical_retention", "ignoreReviewsBeforeDate": "ignore_revlogs_before_date", "parameterSearch": "param_search"},
    "burying": {"new": "bury_new", "review": "bury_reviews", "interdayLearning": "bury_interday_learning"},
    "timer": {"maximumAnswerSeconds": "cap_answer_time_to_secs", "show": "show_timer", "stopOnAnswer": "stop_timer_on_answer"},
    "autoAdvance": {"questionSeconds": "seconds_to_show_question", "answerSeconds": "seconds_to_show_answer", "waitForAudio": "wait_for_audio"},
    "advanced": {"maximumIntervalDays": "maximum_review_interval", "startingEase": "initial_ease", "easyBonus": "easy_multiplier",
                 "intervalModifier": "interval_multiplier", "hardInterval": "hard_multiplier", "newInterval": "lapse_multiplier"},
}
ENUMS = {
    ("newCards", "insertionOrder"): ("new_card_insert_order", ("sequential", "random")),
    ("lapses", "leechAction"): ("leech_action", ("suspend", "tagOnly")),
    ("displayOrder", "newGather"): ("new_card_gather_priority", NEW_GATHER),
    ("displayOrder", "newSort"): ("new_card_sort_order", NEW_SORT),
    ("displayOrder", "newReview"): ("new_mix", MIX),
    ("displayOrder", "interdayLearningReview"): ("interday_learning_mix", MIX),
    ("displayOrder", "reviewSort"): ("review_order", REVIEW_ORDER),
    ("autoAdvance", "questionAction"): ("question_action", QUESTION_ACTION),
    ("autoAdvance", "answerAction"): ("answer_action", ANSWER_ACTION),
}


def _normal_deck(col, deck_id):
    deck = _deck(col, deck_id)
    if deck["dyn"]:
        raise EngineError("unsupported", "Filtered decks do not have deck-options presets.")
    return deck


def _preset(col, preset_id):
    config = col._backend.get_deck_config(preset_id)
    # The native getter intentionally falls back to Default for missing IDs.
    if config.id != preset_id:
        raise EngineError("not_found", "The requested preset is unavailable.")
    return config


def _view(config, state):
    source = config.config
    result = {"id": config.id, "name": config.name, "displayOrder": {}, "audio": {"autoplay": not source.disable_autoplay,
              "replayQuestionOnAnswer": not source.skip_question_when_replaying_answer}, "easyDays": list(source.easy_days_percentages)}
    for group, fields in FIELDS.items():
        result[group] = {key: list(getattr(source, field)) if source.DESCRIPTOR.fields_by_name[field].is_repeated
                         else getattr(source, field) for key, field in fields.items()}
    for (group, key), (field, names) in ENUMS.items():
        value = getattr(source, field)
        if not 0 <= value < len(names):
            raise EngineError("unsupported", "A preset uses an unsupported scheduling option.")
        result[group][key] = names[value]
    result["dailyLimits"].update(newIgnoresReviewLimit=state.new_cards_ignore_review_limit, limitsStartFromTop=state.apply_all_parent_limits)
    result["advanced"]["customScheduling"] = state.card_state_customizer
    return result


def _overrides(limits):
    deck = {key: getattr(limits, field) if limits.HasField(field) else None
            for key, field in (("new", "new"), ("review", "review"), ("desiredRetention", "desired_retention"))}
    today = {"new": limits.new_today if limits.HasField("new_today") else None,
             "review": limits.review_today if limits.HasField("review_today") else None,
             "newActive": limits.new_today_active, "reviewActive": limits.review_today_active}
    return (deck if any(value is not None for value in deck.values()) else None,
            today if today["new"] is not None or today["review"] is not None else None)


def _name(col, value, except_id=None):
    _text(value)
    name = value.strip()
    if not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise EngineError("invalid_params", "Preset names must be nonempty and contain no control characters.")
    for config in col.decks.all_config():
        if config["id"] != except_id and config["name"].casefold() == name.casefold():
            raise EngineError("conflict", "A preset with that name already exists.")
    return name


def _validate_options(col, value):
    _name(col, value["name"], value["id"])
    for steps in (value["newCards"]["stepsMinutes"], value["lapses"]["stepsMinutes"]):
        # Match native f32 minute -> f32 seconds conversion, including the
        # representable upper boundary of the native UI's signed revlog range.
        for step in steps:
            minutes = struct.unpack("f", struct.pack("f", step))[0]
            seconds = struct.unpack("f", struct.pack("f", minutes * 60))[0]
            if not 1 <= seconds <= 2 ** 31:
                raise EngineError("invalid_params", "Learning steps must convert to one through 2^31 seconds.")
    if len(value["easyDays"]) not in (0, 7):
        raise EngineError("invalid_params", "Easy days require either defaults or seven values.")
    from .fsrs import _parameters
    for key in ("parameters", "parameters4", "parameters5"):
        values = value["fsrs"][key]
        # Anki's update view promotes legacy 17/19-value parameters into its
        # current field without expansion. Its own get/save path accepts them.
        _parameters(values)
    cutoff = value["fsrs"]["ignoreReviewsBeforeDate"]
    if cutoff:
        try:
            parsed = date.fromisoformat(cutoff)
            scheduling_day = datetime.fromtimestamp(col.sched.day_cutoff - 86400).date()
            if parsed.isoformat() != cutoff or parsed > scheduling_day:
                raise ValueError()
        except ValueError:
            raise EngineError("invalid_params", "The review cutoff must be a valid nonfuture YYYY-MM-DD date.") from None
    _text(value["fsrs"]["parameterSearch"])
    _text(value["advanced"]["customScheduling"])


def _write_config(config, value):
    config.name = value["name"].strip()
    for group, fields in FIELDS.items():
        for key, field in fields.items():
            if config.config.DESCRIPTOR.fields_by_name[field].is_repeated:
                getattr(config.config, field)[:] = value[group][key]
            else:
                setattr(config.config, field, value[group][key])
    for (group, key), (field, names) in ENUMS.items():
        setattr(config.config, field, names.index(value[group][key]))
    config.config.disable_autoplay = not value["audio"]["autoplay"]
    config.config.skip_question_when_replaying_answer = not value["audio"]["replayQuestionOnAnswer"]
    config.config.easy_days_percentages[:] = value["easyDays"]


def _request(state, deck_id, configs, **extra):
    request = native.UpdateDeckConfigsRequest(target_deck_id=deck_id, configs=configs, limits=state.current_deck.limits,
        card_state_customizer=state.card_state_customizer, new_cards_ignore_review_limit=state.new_cards_ignore_review_limit,
        fsrs=state.fsrs, apply_all_parent_limits=state.apply_all_parent_limits, fsrs_health_check=state.fsrs_health_check, **extra)
    # Native writes ignore the *_active booleans and activate any provided
    # optional value today. Absence preserves an already-expired native limit.
    for key in ("new", "review"):
        if not getattr(request.limits, key + "_today_active"):
            request.limits.ClearField(key + "_today")
    return request


@method("deckOptions.get")
def get(engine, params):
    col = engine.col
    _normal_deck(col, params["deckId"])
    state = col.decks.get_deck_configs_for_update(params["deckId"])
    configs = {item.config.id: item.config for item in state.all_config}
    deck, today = _overrides(state.current_deck.limits)
    return {"deckId": params["deckId"], "config": _view(configs[state.current_deck.config_id], state),
            "presets": [{"id": item.config.id, "name": item.config.name, "deckCount": item.use_count} for item in state.all_config],
            "presetConfigs": [_view(item.config, state) for item in state.all_config], "defaults": _view(state.defaults, state),
            "deckLimits": deck, "todayLimits": today, "parentPresetIds": list(state.current_deck.parent_config_ids),
            "fsrsEnabled": state.fsrs, "fsrsHealthCheck": state.fsrs_health_check, "fsrsLegacyEvaluate": state.fsrs_legacy_evaluate,
            "schemaModified": state.schema_modified, "daysSinceLastFsrsOptimize": state.days_since_last_fsrs_optimize}


@method("deckOptions.update")
def update(engine, params):
    engine.progress.check_abort()
    col, deck_id = engine.col, params["deckId"]
    _normal_deck(col, deck_id)
    config = _preset(col, params["config"]["id"])
    _validate_options(col, params["config"])
    _write_config(config, params["config"])
    state = col.decks.get_deck_configs_for_update(deck_id)
    request = _request(state, deck_id, [config], mode=native.UPDATE_DECK_CONFIGS_MODE_APPLY_TO_CHILDREN if params["applyToChildren"] else native.UPDATE_DECK_CONFIGS_MODE_NORMAL)
    request.limits.Clear()
    for key, value in (params["deckLimits"] or {}).items():
        if value is not None:
            setattr(request.limits, {"desiredRetention": "desired_retention"}.get(key, key), value)
    if params["todayLimits"] is not None:
        for key in ("new", "review"):
            value = params["todayLimits"][key]
            if value is not None and params["todayLimits"][key + "Active"]:
                setattr(request.limits, key + "_today", value)
            setattr(request.limits, key + "_today_active", params["todayLimits"][key + "Active"])
    request.card_state_customizer = params["config"]["advanced"]["customScheduling"]
    request.new_cards_ignore_review_limit = params["config"]["dailyLimits"]["newIgnoresReviewLimit"]
    request.apply_all_parent_limits = params["config"]["dailyLimits"]["limitsStartFromTop"]
    request.fsrs, request.fsrs_health_check = params["fsrsEnabled"], params.get("fsrsHealthCheck", state.fsrs_health_check)
    request.fsrs_reschedule = params.get("reschedule", False)
    if request.fsrs_reschedule and not request.fsrs:
        raise EngineError("invalid_params", "Rescheduling requires FSRS to be enabled.")
    engine.progress.report(label="Saving native deck options")
    with engine.progress.using_backend(col):
        engine.progress.check_abort()
        changes = col.decks.update_deck_configs(request)
    # The successful native transaction is the commit boundary. A subsequent
    # request to abort cannot safely roll back native undo/redo history.
    return _result(col, changes)


def _create(col, name, source=None):
    name = _name(col, name)
    state = col.decks.get_deck_configs_for_update(1)
    config = _preset(col, source) if source is not None else native.DeckConfig.FromString(state.defaults.SerializeToString())
    config.id, config.name = 0, name
    before = {item.config.id for item in state.all_config}
    current = _preset(col, state.current_deck.config_id)
    changes = col.decks.update_deck_configs(_request(state, 1, [config, current]))
    added = [item["id"] for item in col.decks.all_config() if item["id"] not in before]
    if len(added) != 1:
        raise EngineError("internal", "The preset could not be created.")
    return _result(col, changes, presetId=added[0])


@method("deckOptions.presetAdd")
def preset_add(engine, params):
    return _create(engine.col, params["name"])


@method("deckOptions.presetClone")
def preset_clone(engine, params):
    _preset(engine.col, params["presetId"])
    return _create(engine.col, params["name"], params["presetId"])


@method("deckOptions.presetRename")
def preset_rename(engine, params):
    col = engine.col
    config = _preset(col, params["presetId"])
    name = _name(col, params["name"], config.id)
    if name == config.name:
        return mutation_result(undo_state(col))
    config.name = name
    state = col.decks.get_deck_configs_for_update(1)
    configs = [config] if config.id == state.current_deck.config_id else [config, _preset(col, state.current_deck.config_id)]
    return _result(col, col.decks.update_deck_configs(_request(state, 1, configs)))


def _assign(col, deck_ids, preset_id, remove=None):
    if not deck_ids and remove is None:
        raise EngineError("invalid_params", "Select at least one deck.")
    config = _preset(col, preset_id)
    requests = []
    for deck_id in dict.fromkeys(deck_ids):
        deck = _normal_deck(col, deck_id)
        if deck["conf"] != preset_id:
            state = col.decks.get_deck_configs_for_update(deck_id)
            requests.append(_request(state, deck_id, [config]))
    if remove is not None:
        state = col.decks.get_deck_configs_for_update(1)
        current = config if state.current_deck.config_id == remove else _preset(col, state.current_deck.config_id)
        # All former users are reassigned first, so native removal cannot apply
        # the target's limits to unrelated decks with independent overrides.
        requests.append(_request(state, 1, [current], removed_config_ids=[remove]))
    if not requests:
        return mutation_result(undo_state(col))
    combined = None

    def apply():
        nonlocal combined
        step = col.add_custom_undo_entry("Remove preset" if remove is not None else "Assign preset")
        for request in requests:
            col.decks.update_deck_configs(request)
        combined = col.merge_undo_entries(step)
    col.db.transact(apply)
    return _result(col, combined)


@method("deckOptions.presetAssign")
def preset_assign(engine, params):
    return _assign(engine.col, params["deckIds"], params["presetId"])


@method("deckOptions.presetRemove")
def preset_remove(engine, params):
    col = engine.col
    source, replacement = params["presetId"], params["replacementPresetId"]
    _preset(col, source)
    _preset(col, replacement)
    if source == replacement:
        raise EngineError("invalid_params", "Choose a different replacement preset.")
    if source == 1:
        raise EngineError("unsupported", "The native Default preset cannot be removed.")
    users = [deck["id"] for deck in col.decks.get_all_legacy() if not deck["dyn"] and deck["conf"] == source]
    return _assign(col, users, replacement, remove=source)
