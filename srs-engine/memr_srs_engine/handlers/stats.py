"""Typed native statistics and lossless chart data; no HTML or protobuf leaks."""
from ..collection import mutation_result, undo_state
from ..errors import EngineError
from ..registry import method
from .decks import _deck, _result, _text
from .fsrs import _snapshot

WINDOWS = {"month": "one_month", "quarter": "three_months", "year": "one_year", "all": "all_time"}
PERIODS = {"month": 30, "quarter": 90, "year": 365, "all": 0}
KINDS = ("learning", "review", "relearning", "filtered", "manual", "rescheduled")
MAX_BUCKETS = 10000
MAX_REVIEWS = 100000


def _scope_search(col, scope):
    search = scope.get("search") or ""
    _text(search)
    deck_id = scope.get("deckId")
    if deck_id is None:
        return search
    deck = _deck(col, deck_id)
    selected = [deck_id]
    if scope.get("includeChildren", True):
        selected += [item.id for item in col.decks.all_names_and_ids() if item.name.startswith(deck["name"] + "::")]
    term = "did:" + ",".join(str(value) for value in selected)
    return f"({term}) ({search})" if search.strip() else term


def _bounded(values, maximum=MAX_BUCKETS):
    if len(values) > maximum:
        raise EngineError("unsupported", "The native statistics exceed the bounded response; narrow the scope or period.")
    return values


def _histogram(values, key):
    _bounded(values)
    return [{key: value, "count": values[value]} for value in sorted(values)]


def _counts(value):
    return {"new": value.newCards, "learning": value.learn, "relearning": value.relearn, "young": value.young,
            "mature": value.mature, "suspended": value.suspended, "buried": value.buried}


def _retention(value):
    return {"youngPassed": value.young_passed, "youngFailed": value.young_failed,
            "maturePassed": value.mature_passed, "matureFailed": value.mature_failed}


def _review_buckets(values):
    _bounded(values)
    return [{"dayOffset": day, "learning": value.learn, "relearning": value.relearn,
             "young": value.young, "mature": value.mature, "filtered": value.filtered}
            for day, value in sorted(values.items())]


def _data(value):
    current = value.today
    return {"rolloverHour": value.rollover_hour, "fsrsEnabled": value.fsrs,
            "todayNative": {"answerCount": current.answer_count, "answerMilliseconds": current.answer_millis,
                "correctCount": current.correct_count, "matureCorrect": current.mature_correct, "matureCount": current.mature_count,
                "learningCount": current.learn_count, "reviewCount": current.review_count,
                "relearningCount": current.relearn_count, "earlyReviewCount": current.early_review_count},
            "futureDue": {"buckets": _histogram(value.future_due.future_due, "dayOffset"),
                "haveBacklog": value.future_due.have_backlog, "dailyLoad": value.future_due.daily_load},
            "added": _histogram(value.added.added, "dayOffset"), "intervals": _histogram(value.intervals.intervals, "days"),
            "stability": _histogram(value.stability.intervals, "days"),
            "ease": {"buckets": _histogram(value.eases.eases, "percent"), "medianPercent": value.eases.average},
            "difficulty": {"buckets": _histogram(value.difficulty.eases, "percent"), "medianPercent": value.difficulty.average},
            "retrievability": {"buckets": _histogram(value.retrievability.retrievability, "percent"),
                "averagePercent": value.retrievability.average, "sumByCard": value.retrievability.sum_by_card,
                "sumByNote": value.retrievability.sum_by_note},
            "reviews": {"counts": _review_buckets(value.reviews.count), "timeMilliseconds": _review_buckets(value.reviews.time)},
            "cardCounts": {"includingInactive": _counts(value.card_counts.including_inactive),
                           "separateInactive": _counts(value.card_counts.excluding_inactive)},
            "hours": {key: [{"hour": hour, "total": item.total, "correct": item.correct}
                            for hour, item in enumerate(_bounded(getattr(value.hours, window), 24))] for key, window in WINDOWS.items()},
            "answerButtons": {key: {kind: list(getattr(getattr(value.buttons, window), kind)) for kind in ("learning", "young", "mature")}
                              for key, window in WINDOWS.items()},
            "trueRetention": {key: _retention(getattr(value.true_retention, field)) for key, field in
                              (("today", "today"), ("yesterday", "yesterday"), ("week", "week"), ("month", "month"), ("year", "year"), ("all", "all_time"))}}


def _today(col, native, search):
    cutoff = (col.sched.day_cutoff - 86400) * 1000
    clause = ""
    if search.strip():
        cards = _bounded(col.find_cards(search), 100000)
        clause = " and cid in (" + ",".join(str(cid) for cid in cards) + ")" if cards else " and 0"
    cards = col.db.scalar("select count(distinct cid) from revlog where id >= ? and type not in (4,5)" + clause, cutoff)
    seconds = native.answer_millis / 1000
    return {"cards": cards, "reviews": native.answer_count, "elapsedSeconds": seconds,
            "averageSeconds": seconds / native.answer_count if native.answer_count else 0,
            "againCount": native.answer_count - native.correct_count, "correctCount": native.correct_count}


def _charts(data, period):
    def points(values, xkey):
        return [{"x": value[xkey], "y": value["count"], "label": str(value[xkey])} for value in values]

    def graph(key, unit, series):
        return {"key": key, "label": key, "unit": unit, "series": [{"name": name, "points": values} for name, values in series]}
    reviews = data["reviews"]["counts"]
    day_totals = [{"x": row["dayOffset"], "y": sum(row[k] for k in ("learning", "relearning", "young", "mature", "filtered")), "label": str(row["dayOffset"])} for row in reviews]
    counts = data["cardCounts"]["separateInactive"]
    result = [graph("futureDue", "cards", [("Total", points(data["futureDue"]["buckets"], "dayOffset"))]),
              graph("calendar", "answers/day", [("Total", day_totals)]),
              graph("reviews", "answers", [(kind, [{"x": row["dayOffset"], "y": row[kind], "label": str(row["dayOffset"])} for row in reviews])
                                            for kind in ("learning", "relearning", "young", "mature", "filtered")]),
              graph("cardCounts", "cards", [("Total", [{"x": index, "y": value, "label": key} for index, (key, value) in enumerate(counts.items())])])]
    for key, source, xkey in (("intervals", data["intervals"], "days"), ("ease", data["ease"]["buckets"], "percent"),
                              ("difficulty", data["difficulty"]["buckets"], "percent"), ("stability", data["stability"], "days"),
                              ("retrievability", data["retrievability"]["buckets"], "percent")):
        result.append(graph(key, "cards", [("Total", points(source, xkey))]))
    result.append(graph("hourly", "answers", [("Total", [{"x": row["hour"], "y": row["total"], "label": str(row["hour"])} for row in data["hours"][period]])]))
    result.append(graph("answerButtons", "answers", [(key, [{"x": index + 1, "y": value, "label": ("Again", "Hard", "Good", "Easy")[index]} for index, value in enumerate(values)]) for key, values in data["answerButtons"][period].items()]))
    result.append(graph("added", "cards", [("Total", points(data["added"], "dayOffset"))]))
    return result


@method("stats.graphs")
def graphs(engine, params):
    col = engine.col
    search = _scope_search(col, params["scope"])
    native = col._backend.graphs(search=search, days=PERIODS[params["period"]])
    data = _data(native)
    source = data["trueRetention"]["all"]
    retention = []
    for label, passed, failed in (("Young", source["youngPassed"], source["youngFailed"]),
                                 ("Mature", source["maturePassed"], source["matureFailed"]),
                                 ("All", source["youngPassed"] + source["maturePassed"], source["youngFailed"] + source["matureFailed"])):
        retention.append({"label": label, "passed": passed, "failed": failed, "retention": passed / (passed + failed) if passed + failed else None})
    return {"today": _today(col, native.today, search), "graphs": _charts(data, params["period"]), "trueRetention": retention, "data": data}


@method("stats.today")
def today(engine, params):
    search = _scope_search(engine.col, params)
    native = engine.col._backend.graphs(search=search, days=1)
    return _today(engine.col, native.today, search)


@method("stats.graphPreferences")
def graph_preferences(engine, _params):
    value = engine.col._backend.get_graph_preferences()
    return {"firstDayOfWeek": value.calendar_first_day_of_week, "showBacklog": value.future_due_show_backlog,
            "separateInactive": value.card_counts_separate_inactive, "browserLinks": value.browser_links_supported}


@method("stats.setGraphPreferences")
def set_graph_preferences(engine, params):
    col, value = engine.col, params["preferences"]
    if not value["browserLinks"]:
        raise EngineError("unsupported", "Browser links are a read-only native statistics capability.")
    if value == graph_preferences(engine, {}):
        return mutation_result(undo_state(col))
    changed = None

    def apply():
        nonlocal changed
        step = col.add_custom_undo_entry("Statistics preferences")
        col.set_config("firstDayOfWeek", value["firstDayOfWeek"], undoable=True)
        # These native BoolKey values are not exposed by Python's ConfigKey
        # protobuf, so use their exact native camelCase JSON keys.
        col.set_config("cardCountsSeparateInactive", value["separateInactive"], undoable=True)
        col.set_config("futureDueShowBacklog", value["showBacklog"], undoable=True)
        changed = col.merge_undo_entries(step)
    col.db.transact(apply)
    return _result(col, changed)


def _memory(value, retrieval=None):
    return {"stability": value.stability, "difficulty": value.difficulty, "retrievability": retrieval}


@method("stats.cardStats")
def card_stats(engine, params):
    col, card_id = engine.col, params["cardId"]
    card = col.get_card(card_id)
    if col.db.scalar("select count() from revlog where cid=?", card_id) > MAX_REVIEWS:
        raise EngineError("unsupported", "The card history exceeds the bounded response.")
    with _snapshot(engine, control=False) as scratch:
        value = scratch.card_stats_data(card_id)
    memory = _memory(value.memory_state, value.fsrs_retrievability if value.HasField("fsrs_retrievability") else None) if value.HasField("memory_state") else None
    details = {"deck": value.deck, "addedAt": value.added, "firstReviewAt": value.first_review if value.HasField("first_review") else None,
               "latestReviewAt": value.latest_review if value.HasField("latest_review") else None, "dueAt": value.due_date if value.HasField("due_date") else None,
               "duePosition": value.due_position if value.HasField("due_position") else None, "averageSeconds": value.average_secs,
               "totalSeconds": value.total_secs, "cardType": value.card_type, "notetype": value.notetype, "customData": value.custom_data,
               "preset": value.preset, "originalDeck": value.original_deck if value.HasField("original_deck") else None,
               "desiredRetention": value.desired_retention if value.HasField("desired_retention") else None, "parameters": list(value.fsrs_params),
               "reviewHistory": [{"reviewedAt": row.time, "kind": KINDS[row.review_kind], "rating": row.button_chosen,
                    "intervalSeconds": row.interval, "lastIntervalSeconds": row.last_interval, "easePerMill": row.ease,
                    "elapsedSeconds": row.taken_secs, "memoryState": {"stability": row.memory_state.stability, "difficulty": row.memory_state.difficulty} if row.HasField("memory_state") else None} for row in value.revlog]}
    card_view = {"id": card.id, "noteId": card.nid, "deckId": card.did, "originalDeckId": card.odid or None, "ord": card.ord,
                 "queue": card.queue, "type": ("new", "learning", "review", "relearning")[card.type], "due": card.due,
                 "originalDue": card.odue, "intervalDays": card.ivl, "ease": card.factor / 1000, "repetitions": card.reps,
                 "lapses": card.lapses, "flags": card.user_flag(), "position": card.due if card.type == 0 else None,
                 "modifiedAt": card.mod, "memoryState": memory}
    rows = col.db.all("select id,ease,ivl,lastIvl,factor,time,type from revlog where cid=? order by id desc", card_id)
    reviews = [{"id": key, "reviewedAt": key // 1000, "rating": rating,
                "intervalDays": interval if interval >= 0 else -interval / 86400,
                "lastIntervalDays": last if last >= 0 else -last / 86400,
                "ease": ease / 1000, "elapsedSeconds": milliseconds / 1000, "kind": KINDS[kind]}
               for key, rating, interval, last, ease, milliseconds, kind in rows]
    return {"card": card_view, "memoryState": memory, "reviews": reviews, "details": details}
