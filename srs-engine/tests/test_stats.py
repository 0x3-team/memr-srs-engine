"""Statistics preserve native graph data, scope, units and undo boundaries."""
from anki.stats_pb2 import RevlogEntry
from anki.cards_pb2 import FsrsMemoryState
from unittest.mock import patch

from tests.e06_helpers import RpcCase


class StatisticsTests(RpcCase):
    def test_all_methods_registered(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("stats.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_empty_graphs_and_today_are_native(self):
        result = self.rpc("stats.graphs", {"scope": {}, "period": "month"})
        native = self.col._backend.graphs(search="", days=30)
        self.assertEqual(result["today"], {"cards": 0, "reviews": 0, "elapsedSeconds": 0,
                                          "averageSeconds": 0, "againCount": 0, "correctCount": 0})
        self.assertEqual({g["key"] for g in result["graphs"]}, {"futureDue", "calendar", "reviews", "cardCounts",
                         "intervals", "ease", "difficulty", "stability", "retrievability", "hourly", "answerButtons", "added"})
        self.assertEqual(result["data"]["rolloverHour"], native.rollover_hour)
        self.assertEqual(result["data"]["fsrsEnabled"], native.fsrs)
        self.assertEqual(set(result["data"]["answerButtons"]), {"month", "quarter", "year", "all"})
        self.assertEqual(set(result["data"]["trueRetention"]), {"today", "yesterday", "week", "month", "year", "all"})
        self.assertEqual(self.rpc("stats.today"), result["today"])

    def test_graph_preferences_native_persistence_and_validation(self):
        original = self.rpc("stats.graphPreferences")
        wanted = {"firstDayOfWeek": 6, "showBacklog": False, "separateInactive": False, "browserLinks": True}
        result = self.rpc("stats.setGraphPreferences", {"preferences": wanted})
        self.assertEqual(self.rpc("stats.graphPreferences"), wanted)
        self.assertTrue(result["changes"]["config"])
        native = self.col._backend.get_graph_preferences()
        self.assertEqual(native.calendar_first_day_of_week, 6)
        self.assertFalse(native.future_due_show_backlog)
        self.col.undo()
        self.assertEqual(self.rpc("stats.graphPreferences"), original)
        self.col.redo()
        self.assertEqual(self.rpc("stats.graphPreferences"), wanted)
        self.error("stats.setGraphPreferences", {"preferences": {**wanted, "firstDayOfWeek": 2}}, "invalid_params")
        self.error("stats.setGraphPreferences", {"preferences": {**wanted, "browserLinks": False}}, "unsupported")
        self.error("stats.graphPreferences", {"unknown": True}, "invalid_params")
        self.rpc("stats.setGraphPreferences", {"preferences": original})

    def test_today_scope_counts_unique_cards_and_native_millis(self):
        deck = self.create("Stats")
        other = self.create("Other")
        card = self.note(deck).cards()[0]
        other_card = self.note(other).cards()[0]
        cutoff = (self.col.sched.day_cutoff - 86400) * 1000
        entries = [(cutoff + 1000, card.id, 1, 1500, RevlogEntry.REVIEW),
                   (cutoff + 2000, card.id, 3, 2500, RevlogEntry.REVIEW),
                   (cutoff + 3000, other_card.id, 2, 5000, RevlogEntry.LEARNING),
                   (cutoff + 4000, card.id, 0, 1000, RevlogEntry.MANUAL)]
        for key, cid, ease, millis, kind in entries:
            self.col.db.execute("insert into revlog values (?,?,?,?,?,?,?,?,?)", key, cid, -1, ease, 4, 1, 2500, millis, kind)
        result = self.rpc("stats.today", {"deckId": deck, "includeChildren": False})
        self.assertEqual(result, {"cards": 1, "reviews": 2, "elapsedSeconds": 4,
                                  "averageSeconds": 2, "againCount": 1, "correctCount": 1})
        self.assertEqual(self.rpc("stats.today")["reviews"], 3)
        self.assertEqual(self.rpc("stats.graphs", {"scope": {"deckId": deck}, "period": "all"})["today"], result)

    def test_search_scope_intersection_and_missing_deck(self):
        deck = self.create("Scope")
        child = self.create("Scope::Child")
        self.note(deck)
        self.note(child)
        result = self.rpc("stats.graphs", {"scope": {"deckId": deck, "includeChildren": False}, "period": "month"})
        self.assertEqual(result["data"]["cardCounts"]["includingInactive"]["new"], 1)
        result = self.rpc("stats.graphs", {"scope": {"deckId": deck, "search": "is:new"}, "period": "year"})
        self.assertEqual(result["data"]["cardCounts"]["includingInactive"]["new"], 2)
        self.error("stats.today", {"deckId": 999}, "not_found")
        self.error("stats.graphs", {"scope": {}, "period": "week"}, "invalid_params")
        self.error("stats.graphs", {"scope": {"search": "("}, "period": "month"}, "invalid_params")

    def test_card_stats_uses_native_intervals_and_missing_results(self):
        card = self.note(1).cards()[0]
        result = self.rpc("stats.cardStats", {"cardId": card.id})
        self.assertEqual(result["card"]["id"], card.id)
        self.assertEqual(result["reviews"], [])
        self.assertIsNone(result["memoryState"])
        self.assertEqual(result["details"]["deck"], "Default")
        self.error("stats.cardStats", {"cardId": 999}, "not_found")
        self.error("stats.cardStats", {"cardId": -1}, "invalid_params")

    def test_card_statistics_keep_native_cache_writes_off_the_source(self):
        card = self.note(1).cards()[0]
        card.type, card.queue, card.ivl, card.reps = 2, 2, 3, 1
        card.memory_state = FsrsMemoryState(stability=10, difficulty=5)
        card.desired_retention, card.decay, card.last_review_time = .9, .5, None
        self.col.update_card(card)
        key = (self.col.sched.day_cutoff - 2 * 86400) * 1000 + 123
        self.col.db.execute("insert into revlog values (?,?,?,?,?,?,?,?,?)", key, card.id, -1, 3, -90, 3, 2500, 1500, 0)
        before, history = self.col.db.all("select * from cards"), self.col.undo_status()
        result = self.rpc("stats.cardStats", {"cardId": card.id})
        self.assertEqual(result["reviews"][0], {"id": key, "reviewedAt": key // 1000, "rating": 3,
            "intervalDays": 90 / 86400, "lastIntervalDays": 3, "ease": 2.5, "elapsedSeconds": 1.5, "kind": "learning"})
        self.assertEqual(result["details"]["reviewHistory"][0]["intervalSeconds"], 90)
        self.assertEqual(result["details"]["reviewHistory"][0]["lastIntervalSeconds"], 3 * 86400)
        self.assertEqual(self.col.db.all("select * from cards"), before)
        self.assertEqual(self.col.undo_status(), history)

    def test_or_search_cannot_escape_deck_scope_and_filtered_origins_match_native(self):
        deck, other = self.create("Scoped"), self.create("Outside")
        card = self.note(deck).cards()[0]
        self.note(other)
        result = self.rpc("stats.graphs", {"scope": {"deckId": deck, "search": "is:new OR deck:Outside"}, "period": "month"})
        self.assertEqual(result["data"]["cardCounts"]["includingInactive"]["new"], 1)
        filtered = self.col.decks.new_filtered("Filtered stats")
        card.odid, card.did = deck, filtered
        self.col.update_card(card)
        result = self.rpc("stats.graphs", {"scope": {"deckId": deck, "includeChildren": False}, "period": "month"})
        self.assertEqual(result["data"]["cardCounts"]["includingInactive"]["new"], 1)

    def test_native_histogram_and_history_overflow_fail_explicitly(self):
        native = self.col._backend.graphs(search="", days=30)
        for interval in range(10001):
            native.intervals.intervals[interval] = 1
        with patch.object(self.col._backend, "graphs", return_value=native):
            self.error("stats.graphs", {"scope": {}, "period": "month"}, "unsupported")
        card = self.note(1).cards()[0]
        first = (self.col.sched.day_cutoff - 86400) * 1000
        self.col.db.execute("begin")
        try:
            self.col.db.executemany("insert into revlog values (?,?,?,?,?,?,?,?,?)",
                ((first + index, card.id, -1, 3, 1, 1, 2500, 1000, 1) for index in range(100001)))
        except BaseException:
            self.col.db.execute("rollback")
            raise
        else:
            self.col.db.execute("commit")
        self.error("stats.cardStats", {"cardId": card.id}, "unsupported")
