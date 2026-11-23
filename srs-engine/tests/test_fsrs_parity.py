"""FSRS collection switches and bulk optimization use the pinned native engine."""
from tests import test_fsrs
from tests.e06_helpers import RpcCase
from unittest.mock import patch
from memr_srs_engine.handlers import fsrs


class FsrsParityTests(RpcCase):
    start = test_fsrs.FsrsTests.start
    finish = test_fsrs.FsrsTests.finish
    long_call = test_fsrs.FsrsTests.long_call
    history = test_fsrs.FsrsTests.history

    def test_collection_preferences_round_trip_and_undo(self):
        before = self.rpc("fsrs.getPreferences")
        desired = {key: not value for key, value in before.items()}
        result = self.rpc("fsrs.setPreferences", desired)
        self.assertTrue(result["changes"]["config"])
        self.assertEqual(self.rpc("fsrs.getPreferences"), desired)
        self.assertEqual(self.col.load_balancer_enabled, desired["loadBalancerEnabled"])
        self.assertEqual(self.col.fsrs_short_term_with_steps_enabled, desired["shortTermWithStepsEnabled"])
        self.col.undo()
        self.assertEqual(self.rpc("fsrs.getPreferences"), before)
        self.col.redo()
        self.assertEqual(self.rpc("fsrs.getPreferences"), desired)
        unchanged = self.rpc("fsrs.setPreferences", desired)
        self.assertFalse(any(unchanged["changes"].values()))
        self.error("fsrs.setPreferences", {**desired, "loadBalancerEnabled": "yes"}, "invalid_params")

    def test_stale_preference_update_preserves_newer_switches(self):
        before = self.rpc("fsrs.getPreferences")
        desired = {key: not value for key, value in before.items()}
        self.rpc("fsrs.setPreferences", desired)
        self.error("fsrs.setPreferences", {**before, "expected": before}, "conflict")
        self.assertEqual(self.rpc("fsrs.getPreferences"), desired)

    def test_optimize_all_reports_each_preset_and_preserves_assignment(self):
        self.history(count=40, reviews=20)
        other = self.rpc("deckOptions.presetAdd", {"name": "Empty preset"})["presetId"]
        options = self.rpc("deckOptions.get", {"deckId": 1})
        self.long_call("deckOptions.update", {**{key: options[key] for key in ("deckId", "config", "deckLimits", "todayLimits")},
            "applyToChildren": False, "fsrsEnabled": True})
        before = self.col.decks.get_config(1)
        result = self.long_call("fsrs.optimizeAll", {"deckId": 1, "reschedule": False})
        ids = [value["presetId"] for value in result["fsrsOptimizations"] + result["fsrsOptimizationFailures"]]
        self.assertCountEqual(ids, [1, other])
        self.assertTrue(result["fsrsOptimizations"])
        self.assertEqual(self.rpc("deckOptions.get", {"deckId": 1})["config"]["id"], 1)
        self.assertEqual(self.col.get_config("lastFsrsOptimize"), self.col.sched.today)
        self.col.undo()
        self.assertEqual(self.col.decks.get_config(1), before)

    def test_optimize_all_rejects_disabled_fsrs_without_writes(self):
        before = self.col.decks.get_config(1)
        outcome = self.finish(self.start("fsrs.optimizeAll", {"deckId": 1}))
        self.assertEqual((outcome["status"], outcome["error"]["code"]), ("failed", "invalid_params"))
        self.assertEqual(self.col.decks.get_config(1), before)

    def test_bulk_failure_is_reported_and_all_failed_does_not_write(self):
        options = self.rpc("deckOptions.get", {"deckId": 1})
        options["config"]["fsrs"]["parameterSearch"] = "("
        self.long_call("deckOptions.update", {**{key: options[key] for key in ("deckId", "config", "deckLimits", "todayLimits")},
            "applyToChildren": False, "fsrsEnabled": True})
        before, undo = self.col.decks.get_config(1), self.col.undo_status()
        result = self.long_call("fsrs.optimizeAll", {"deckId": 1})
        self.assertEqual(result["fsrsOptimizations"], [])
        self.assertEqual([item["presetId"] for item in result["fsrsOptimizationFailures"]], [1])
        self.assertFalse(any(result["changes"].values()))
        self.assertEqual(self.col.decks.get_config(1), before)
        self.assertEqual(self.col.undo_status(), undo)
        self.assertIsNone(self.col.get_config("lastFsrsOptimize"))

    def test_bulk_abort_after_native_training_does_not_save_partial_parameters(self):
        self.history(count=20, reviews=20)
        options = self.rpc("deckOptions.get", {"deckId": 1})
        self.long_call("deckOptions.update", {**{key: options[key] for key in ("deckId", "config", "deckLimits", "todayLimits")},
            "applyToChildren": False, "fsrsEnabled": True})
        before, undo = self.col.decks.get_config(1), self.col.undo_status()
        original = fsrs._compute

        def interrupt_after_training(engine, *args):
            result = original(engine, *args)
            engine.progress.abort(engine.progress.current)
            return result
        with patch.object(fsrs, "_compute", interrupt_after_training):
            result = self.finish(self.start("fsrs.optimizeAll", {"deckId": 1}))
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(self.col.decks.get_config(1), before)
        self.assertEqual(self.col.undo_status(), undo)

    def test_retention_workload_and_history_coverage_are_native_reads(self):
        self.history(count=12, reviews=20)
        before, undo = self.col.db.all("select * from cards"), self.col.undo_status()
        count = self.rpc("fsrs.historyCount", {"search": "", "ignoreReviewsBeforeDate": ""})
        self.assertGreater(count["included"], 0)
        self.assertEqual(count["included"], count["total"])
        curve = self.long_call("fsrs.retentionWorkload", {"search": "", "parameters": []})["fsrsRetentionWorkload"]
        self.assertGreater(len(curve), 1)
        self.assertTrue(all(item["cost"] >= 0 for item in curve))
        workload = self.long_call("fsrs.simulateWorkload", {"deckId": 1, "parameters": [], "desiredRetention": .9,
            "days": 7, "newCardsPerDay": 20, "reviewLimit": 200, "maximumIntervalDays": 36500, "easyDays": []})["fsrsWorkload"]
        self.assertGreater(len(workload["points"]), 1)
        self.assertTrue(all(item["reviews"] >= 0 and item["costSeconds"] >= 0 for item in workload["points"]))
        self.assertEqual(self.col.db.all("select * from cards"), before)
        self.assertEqual(self.col.undo_status(), undo)
