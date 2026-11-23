"""Only collection-owned scheduling, reviewer and backup preferences persist."""
from ..collection import mutation_result, undo_state
from ..registry import method


@method("prefs.get")
def get(engine, _params):
    prefs = engine.col.get_preferences()
    return {"nextDayStartsAt": prefs.scheduling.rollover,
            "learnAheadSeconds": prefs.scheduling.learn_ahead_secs,
            "timeboxSeconds": prefs.reviewing.time_limit_secs,
            "showIntervals": prefs.reviewing.show_intervals_on_buttons,
            "showRemainingCounts": prefs.reviewing.show_remaining_due_counts,
            "backup": {"intervalMinutes": prefs.backups.minimum_interval_mins,
                       "daily": prefs.backups.daily, "weekly": prefs.backups.weekly,
                       "monthly": prefs.backups.monthly}}


@method("prefs.set")
def set_preferences(engine, params):
    col = engine.col
    wanted = params["preferences"]
    if wanted == get(engine, {}):
        return mutation_result(undo_state(col))
    prefs = col.get_preferences()
    # Read/modify/write preserves editing, backup and other reviewer options.
    prefs.scheduling.rollover = wanted["nextDayStartsAt"]
    prefs.scheduling.learn_ahead_secs = wanted["learnAheadSeconds"]
    prefs.reviewing.time_limit_secs = wanted["timeboxSeconds"]
    prefs.reviewing.show_intervals_on_buttons = wanted["showIntervals"]
    prefs.reviewing.show_remaining_due_counts = wanted["showRemainingCounts"]
    prefs.backups.minimum_interval_mins = wanted["backup"]["intervalMinutes"]
    prefs.backups.daily = wanted["backup"]["daily"]
    prefs.backups.weekly = wanted["backup"]["weekly"]
    prefs.backups.monthly = wanted["backup"]["monthly"]
    changes = col.set_preferences(prefs)
    result = mutation_result(undo_state(col))
    aliases = {"deckConfig": "deck_config", "browserTable": "browser_table",
               "noteText": "note_text", "studyQueues": "study_queues"}
    result["changes"] = {key: bool(getattr(changes, aliases.get(key, key))) for key in result["changes"]}
    return result
