"""Deck presets and overrides are native, validated and undoable."""
from copy import deepcopy
from datetime import datetime
from unittest.mock import PropertyMock, patch

from tests.e06_helpers import RpcCase
from tests.test_fsrs import finish_operation


class DeckOptionsTests(RpcCase):
    def options(self, deck_id=1):
        return self.rpc("deckOptions.get", {"deckId": deck_id})

    def update(self, value, **overrides):
        params = {key: deepcopy(value[key]) for key in
                  ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
        frame = self.frame("deckOptions.update", {**params, "applyToChildren": False, **overrides})
        self.assertIn("result", frame, frame)
        outcome = finish_operation(self, frame["result"]["operationId"])
        self.assertEqual(outcome["status"], "completed", outcome)
        return outcome["result"]

    def error(self, method, params, code):
        if method != "deckOptions.update":
            return super().error(method, params, code)
        frame = self.frame(method, params)
        error = frame.get("error")
        if error is None:
            outcome = finish_operation(self, frame["result"]["operationId"])
            self.assertEqual(outcome["status"], "failed", outcome)
            error = outcome["error"]
        self.assertEqual(error["code"], code)
        self.assertNotIn("Traceback", str(error))

    def test_all_methods_registered(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("deckOptions.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_get_all_presets_defaults_and_native_flags(self):
        result = self.options()
        native = self.col.decks.get_deck_configs_for_update(1)
        self.assertEqual(result["config"]["id"], native.current_deck.config_id)
        self.assertEqual(result["defaults"]["newCards"]["stepsMinutes"], [1, 10])
        self.assertEqual(result["config"]["dailyLimits"]["new"], 20)
        self.assertEqual(result["config"]["dailyLimits"]["review"], 200)
        self.assertEqual(result["config"]["autoAdvance"]["answerAction"], "bury")
        self.assertEqual(result["config"]["fsrs"]["ignoreReviewsBeforeDate"], "")
        self.assertEqual(result["presets"], [{"id": 1, "name": "Default", "deckCount": 1}])
        self.assertEqual([item["id"] for item in result["presetConfigs"]], [1])
        self.assertEqual(result["fsrsEnabled"], native.fsrs)
        self.assertEqual(result["fsrsHealthCheck"], native.fsrs_health_check)
        self.assertIsNone(result["deckLimits"])
        self.assertIsNone(result["todayLimits"])

    def test_update_preserves_unknown_native_fields_and_undo_redo(self):
        legacy = self.col.decks.get_config(1)
        legacy["futureOption"] = {"kept": "verbatim"}
        self.col.decks.update_config(legacy)
        value = self.options()
        value["config"]["dailyLimits"]["new"] = 31
        value["config"]["displayOrder"]["reviewSort"] = "relativeOverdueness"
        value["config"]["advanced"]["customScheduling"] = "throw new Error('must never execute')"
        value["config"]["fsrs"]["parameterSearch"] = "deck:Default"
        before = self.col.decks.get_config(1)
        result = self.update(value)
        self.assertTrue(result["changes"]["deckConfig"])
        stored = self.options()["config"]
        self.assertEqual(stored["dailyLimits"]["new"], 31)
        self.assertEqual(stored["displayOrder"]["reviewSort"], "relativeOverdueness")
        self.assertEqual(stored["advanced"]["customScheduling"], value["config"]["advanced"]["customScheduling"])
        self.assertEqual(self.col.decks.get_config(1)["futureOption"], {"kept": "verbatim"})
        self.col.undo()
        self.assertEqual(self.col.decks.get_config(1), before)
        self.col.redo()
        self.assertEqual(self.options()["config"], stored)

    def test_long_receipt_keeps_exact_undo_revision_after_same_label_change(self):
        value = self.options()
        value["config"]["dailyLimits"]["new"] = 31
        first = self.update(value)
        first_id = self.engine.progress.current
        self.assertEqual(first["undo"], self.rpc("study.undoStatus"))
        self.assertTrue(first["undo"].get("revision"))
        value["config"]["dailyLimits"]["new"] = 32
        second = self.update(value)
        self.assertEqual(first["undo"]["undoLabel"], second["undo"]["undoLabel"])
        self.assertNotEqual(first["undo"]["revision"], second["undo"]["revision"])
        self.assertEqual(self.rpc("progress.get", {"operationId": first_id})["result"]["undo"], first["undo"])
        self.error("study.undo", {"expectedRevision": first["undo"]["revision"]}, "conflict")
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 32)
        self.rpc("study.undo", {"expectedRevision": second["undo"]["revision"]})
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 31)

    def test_fast_long_completion_uses_revision_reserved_before_worker_start(self):
        start = self.engine.progress.start

        def finish_before_ack(*args):
            receipt = start(*args)
            self.engine.progress.thread.join(timeout=5)
            self.assertFalse(self.engine.progress.thread.is_alive())
            return receipt

        value = self.options()
        value["config"]["dailyLimits"]["new"] = 33
        with patch.object(self.engine.progress, "start", side_effect=finish_before_ack):
            result = self.update(value)
        self.assertEqual(result["undo"], self.rpc("study.undoStatus"))
        self.assertTrue(result["undo"].get("revision"))
        self.rpc("study.undo", {"expectedRevision": result["undo"]["revision"]})
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 20)

    def test_independent_deck_and_today_limits_and_reset(self):
        value = self.options()
        value["deckLimits"] = {"new": 0, "review": None, "desiredRetention": 0.87}
        value["todayLimits"] = {"new": None, "review": 17, "newActive": False, "reviewActive": True}
        self.update(value)
        actual = self.options()
        self.assertEqual(actual["deckLimits"]["new"], 0)
        self.assertIsNone(actual["deckLimits"]["review"])
        self.assertAlmostEqual(actual["deckLimits"]["desiredRetention"], 0.87, places=6)
        self.assertEqual(actual["todayLimits"], value["todayLimits"])
        self.update(actual, deckLimits=None, todayLimits=None)
        self.assertIsNone(self.options()["deckLimits"])
        self.assertIsNone(self.options()["todayLimits"])

    def test_preset_lifecycle_assignment_and_undo_redo(self):
        parent = self.create("Options parent")
        child = self.create("Options parent::Child")
        created = self.rpc("deckOptions.presetAdd", {"name": "Learning"})
        preset_id = created["presetId"]
        self.assertNotEqual(preset_id, 1)
        self.assertEqual(self.options(parent)["config"]["id"], 1)
        self.rpc("deckOptions.presetAssign", {"deckIds": [parent, child], "presetId": preset_id})
        self.assertEqual([self.options(d)["config"]["id"] for d in [parent, child]], [preset_id, preset_id])
        self.col.undo()
        self.assertEqual([self.options(d)["config"]["id"] for d in [parent, child]], [1, 1])
        self.col.redo()
        self.rpc("deckOptions.presetRename", {"presetId": preset_id, "name": "Renamed"})
        clone_id = self.rpc("deckOptions.presetClone", {"presetId": preset_id, "name": "Cloned"})["presetId"]
        self.assertEqual(self.col.decks.get_config(clone_id)["name"], "Cloned")
        self.rpc("deckOptions.presetRemove", {"presetId": preset_id, "replacementPresetId": clone_id})
        self.assertNotIn(preset_id, [config["id"] for config in self.col.decks.all_config()])
        self.assertEqual([self.options(d)["config"]["id"] for d in [parent, child]], [clone_id, clone_id])
        self.col.undo()
        self.assertEqual(self.col.decks.get_config(preset_id)["name"], "Renamed")
        self.assertEqual([self.options(d)["config"]["id"] for d in [parent, child]], [preset_id, preset_id])
        self.col.redo()
        self.assertNotIn(preset_id, [config["id"] for config in self.col.decks.all_config()])

    def test_apply_to_children_uses_native_preset_assignment(self):
        parent = self.create("A")
        child = self.create("A::B")
        other = self.create("Other")
        preset_id = self.rpc("deckOptions.presetAdd", {"name": "Children"})["presetId"]
        self.rpc("deckOptions.presetAssign", {"deckIds": [parent], "presetId": preset_id})
        value = self.options(parent)
        value["config"]["dailyLimits"]["review"] = 105
        self.update(value, applyToChildren=True)
        self.assertEqual(self.options(child)["config"]["id"], preset_id)
        self.assertEqual(self.options(other)["config"]["id"], 1)
        self.col.undo()
        self.assertEqual(self.options(child)["config"]["id"], 1)

    def test_invalid_and_missing_targets_do_not_mutate(self):
        before = self.col.decks.get_deck_configs_for_update(1)
        history = self.col.undo_status()
        for method, params, code in [
            ("deckOptions.get", {"deckId": 999}, "not_found"),
            ("deckOptions.get", {"deckId": True}, "invalid_params"),
            ("deckOptions.presetAdd", {"name": " "}, "invalid_params"),
            ("deckOptions.presetAdd", {"name": "Default"}, "conflict"),
            ("deckOptions.presetClone", {"presetId": 999, "name": "Absent"}, "not_found"),
            ("deckOptions.presetRename", {"presetId": 999, "name": "Absent"}, "not_found"),
            ("deckOptions.presetRemove", {"presetId": 1, "replacementPresetId": 1}, "invalid_params"),
            ("deckOptions.presetAssign", {"deckIds": [], "presetId": 1}, "invalid_params"),
            ("deckOptions.presetAssign", {"deckIds": [1, 999], "presetId": 1}, "not_found"),
        ]:
            with self.subTest(method=method, code=code):
                self.error(method, params, code)
                self.assertEqual(self.col.decks.get_deck_configs_for_update(1), before)
                self.assertEqual(self.col.undo_status(), history)

    def test_invalid_options_rejected_before_native_clamping(self):
        value = self.options()
        history = self.col.undo_status()
        cases = [("easyDays", [1, 1]), ("fsrs", {**value["config"]["fsrs"], "ignoreReviewsBeforeDate": "2026-02-30"}),
                 ("fsrs", {**value["config"]["fsrs"], "parameters": [1]}),
                 ("newCards", {**value["config"]["newCards"], "stepsMinutes": [0]})]
        for key, invalid in cases:
            with self.subTest(key=key):
                bad = {**value, "config": {**value["config"], key: invalid}}
                params = {k: bad[k] for k in ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
                self.error("deckOptions.update", {**params, "applyToChildren": False}, "invalid_params")
                self.assertEqual(self.options(), value)
                self.assertEqual(self.col.undo_status(), history)

    def test_native_numeric_boundaries_and_multiday_steps_round_trip(self):
        for low in (True, False):
            value = self.options()
            value["config"]["newCards"]["stepsMinutes"] = [1 / 60, 1440, 10080, 35791396]
            value["config"]["lapses"]["leechThreshold"] = 1 if low else 9999
            value["config"]["timer"]["maximumAnswerSeconds"] = 1 if low else 9999
            value["config"]["fsrs"]["desiredRetention"] = .7 if low else .99
            value["config"]["fsrs"]["historicalRetention"] = .7 if low else .97
            value["config"]["advanced"].update(startingEase=1.31 if low else 5,
                easyBonus=1 if low else 5, hardInterval=.5 if low else 1.3, intervalModifier=.5 if low else 2)
            value["config"]["autoAdvance"].update(questionSeconds=3, answerSeconds=7)
            self.update(value)
            actual = self.options()["config"]
            self.assertEqual(actual["newCards"]["stepsMinutes"][1:], [1440, 10080, 35791396])
            self.assertEqual(actual["lapses"]["leechThreshold"], 1 if low else 9999)
            self.assertEqual(actual["timer"]["maximumAnswerSeconds"], 1 if low else 9999)
            self.assertAlmostEqual(actual["advanced"]["startingEase"], 1.31 if low else 5, places=6)
            self.assertAlmostEqual(actual["advanced"]["hardInterval"], .5 if low else 1.3, places=6)
            self.assertAlmostEqual(actual["fsrs"]["desiredRetention"], .7 if low else .99, places=6)
            native = self.col._backend.get_deck_config(1).config
            self.assertEqual((native.seconds_to_show_question, native.seconds_to_show_answer), (3, 7))

    def test_native_reset_ranges_are_rejected_without_state_or_history_changes(self):
        value = self.options()
        history = self.col.undo_status()
        for group, key, invalid in [
            ("advanced", "startingEase", 1.3), ("advanced", "startingEase", 5.1),
            ("advanced", "easyBonus", 0), ("advanced", "hardInterval", 0),
            ("advanced", "hardInterval", 1.4), ("advanced", "intervalModifier", .49),
            ("advanced", "intervalModifier", 2.1), ("lapses", "leechThreshold", 0),
            ("timer", "maximumAnswerSeconds", 0), ("fsrs", "historicalRetention", .98),
        ]:
            bad = deepcopy(value)
            bad["config"][group][key] = invalid
            params = {k: bad[k] for k in ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
            self.error("deckOptions.update", {**params, "applyToChildren": False}, "invalid_params")
            self.assertEqual(self.options(), value)
            self.assertEqual(self.col.undo_status(), history)
        bad = deepcopy(value)
        bad["config"]["easyDays"] = [1, 1, 1, 1, 1, .25, .5]
        params = {k: bad[k] for k in ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
        self.error("deckOptions.update", {**params, "applyToChildren": False}, "invalid_params")

    def test_expired_today_limits_are_never_reactivated_by_preset_operations(self):
        self.col.crt -= 10 * 86400
        # Startup warmed native timing. Reopen the disposable fixture so its
        # edited creation timestamp is reloaded before constructing old limits.
        self.col.close()
        self.col.reopen()
        self.assertGreater(self.col.sched.today, 0)
        parent = self.create("Expired")
        deck = self.col._backend.get_deck(parent)
        deck.normal.new_limit_today.limit, deck.normal.new_limit_today.today = 7, self.col.sched.today - 1
        deck.normal.review_limit_today.limit, deck.normal.review_limit_today.today = 13, self.col.sched.today - 1
        self.col._backend.update_deck(deck)
        before = self.options(parent)["todayLimits"]
        self.assertEqual(before, {"new": 7, "review": 13, "newActive": False, "reviewActive": False})
        created = self.rpc("deckOptions.presetAdd", {"name": "Expired source"})["presetId"]
        self.rpc("deckOptions.presetAssign", {"deckIds": [parent], "presetId": created})
        clone = self.rpc("deckOptions.presetClone", {"presetId": created, "name": "Expired clone"})["presetId"]
        self.rpc("deckOptions.presetRename", {"presetId": created, "name": "Expired renamed"})
        self.update(self.options(parent))
        self.rpc("deckOptions.presetRemove", {"presetId": created, "replacementPresetId": clone})
        limits = self.options(parent)["todayLimits"]
        self.assertEqual(limits, before)

    def test_legacy_weights_round_trip_and_preset_creation_undo(self):
        default = self.col.decks.get_deck_configs_for_update(1).defaults.config
        old = self.col.decks.get_config(1)
        old["fsrsWeights"] = list(default.fsrs_params_6)[:17]
        self.col.decks.update_config(old)
        value = self.options()
        self.assertEqual(len(value["config"]["fsrs"]["parameters"]), 17)
        self.update(value)
        self.assertEqual(self.options()["config"]["fsrs"]["parameters4"], old["fsrsWeights"])
        created = self.rpc("deckOptions.presetAdd", {"name": "Undo create"})["presetId"]
        self.col.undo()
        self.assertNotIn(created, [c["id"] for c in self.col.decks.all_config()])
        self.col.redo()
        self.assertIn(created, [c["id"] for c in self.col.decks.all_config()])

    def test_cutoff_uses_native_scheduling_date(self):
        value = self.options()
        value["config"]["fsrs"]["ignoreReviewsBeforeDate"] = "2020-01-02"
        cutoff = int(datetime(2020, 1, 2, 4).timestamp())
        params = {k: value[k] for k in ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
        with patch.object(type(self.col.sched), "day_cutoff", new_callable=PropertyMock, return_value=cutoff):
            self.error("deckOptions.update", {**params, "applyToChildren": False}, "invalid_params")

    def test_abort_before_option_save_preserves_database_and_undo_redo(self):
        self.create("Undo history")
        self.create("Redo history")
        self.col.undo()
        value = self.options()
        value["config"]["dailyLimits"]["new"] = 33
        params = {k: value[k] for k in ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
        before, history = self.col.decks.get_deck_configs_for_update(1), self.col.undo_status()
        report = self.engine.progress.report

        def cancel(*args, **kwargs):
            report(*args, **kwargs)
            self.engine.progress.abort(self.engine.progress.current)
        with patch.object(self.engine.progress, "report", cancel):
            ack = self.frame("deckOptions.update", {**params, "applyToChildren": False})
            result = finish_operation(self, ack["result"]["operationId"])
        self.assertEqual(result["status"], "aborted", result)
        self.assertEqual(self.col.decks.get_deck_configs_for_update(1), before)
        self.assertEqual(self.col.undo_status(), history)
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 20)

    def test_abort_after_native_option_commit_completes_and_keeps_valid_undo(self):
        value = self.options()
        value["config"]["dailyLimits"]["new"] = 34
        original = self.col.decks.update_deck_configs

        def after_commit(request):
            changes = original(request)
            self.engine.progress.abort(self.engine.progress.current)
            return changes
        with patch.object(self.col.decks, "update_deck_configs", after_commit):
            result = self.update(value)
        self.assertTrue(result["changes"]["deckConfig"])
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 34)
        self.col.undo()
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 20)
        self.col.redo()
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 34)

    def test_native_option_failure_rolls_back_data_and_reports_native_undo_limit(self):
        self.create("Native failure history")
        value = self.options()
        value["config"]["dailyLimits"]["new"] = 35
        before = self.col.decks.get_deck_configs_for_update(1)
        original = self.col.decks.update_deck_configs

        def native_failure(request):
            request.removed_config_ids.append(1)
            return original(request)
        params = {k: value[k] for k in ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
        with patch.object(self.col.decks, "update_deck_configs", native_failure):
            self.error("deckOptions.update", {**params, "applyToChildren": False}, "invalid_params")
        self.assertEqual(self.col.decks.get_deck_configs_for_update(1), before)
        # Native failed transactions clear their in-memory undo queues. The
        # engine exposes that native state instead of claiming restoration.
        self.assertFalse(self.col.undo_status().undo)
        self.assertEqual(self.options()["config"]["dailyLimits"]["new"], 20)
