"""Scheduling and reviewer preferences remain native collection preferences."""
from tests.test_decks import RpcCase


class PreferenceTests(RpcCase):
    def test_both_methods_registered(self):
        self.assertTrue({"prefs.get", "prefs.set"} <= self.engine.registry.handlers.keys())

    def test_get_native_defaults(self):
        self.assertEqual(self.rpc("prefs.get"), {"nextDayStartsAt": 4, "learnAheadSeconds": 1200,
                                               "timeboxSeconds": 0, "showIntervals": True,
                                               "showRemainingCounts": True,
                                               "backup": {"intervalMinutes": 30, "daily": 12,
                                                          "weekly": 10, "monthly": 9}})

    def test_set_preserves_other_preferences_and_undo_redo(self):
        before = self.col.get_preferences()
        wanted = {"nextDayStartsAt": 7, "learnAheadSeconds": 1800, "timeboxSeconds": 600,
                  "showIntervals": False, "showRemainingCounts": False,
                  "backup": {"intervalMinutes": 60, "daily": 7, "weekly": 4, "monthly": 2}}
        result = self.rpc("prefs.set", {"preferences": wanted})
        self.assertTrue(result["changes"]["config"])
        self.assertTrue(result["changes"]["studyQueues"])
        self.assertEqual(self.rpc("prefs.get"), wanted)
        native = self.col.get_preferences()
        self.assertEqual(native.scheduling.rollover, 7)
        self.assertEqual(native.scheduling.learn_ahead_secs, 1800)
        self.assertEqual(native.reviewing.time_limit_secs, 600)
        self.assertFalse(native.reviewing.show_intervals_on_buttons)
        self.assertFalse(native.reviewing.show_remaining_due_counts)
        self.assertEqual(native.backups.minimum_interval_mins, 60)
        self.assertEqual((native.backups.daily, native.backups.weekly, native.backups.monthly), (7, 4, 2))
        self.assertEqual(native.editing, before.editing)
        self.assertEqual(native.reviewing.hide_audio_play_buttons, before.reviewing.hide_audio_play_buttons)
        self.col.undo()
        self.assertEqual(self.col.get_preferences(), before)
        self.col.redo()
        self.assertEqual(self.rpc("prefs.get"), wanted)
        self.assertIsNone(self.col.get_config("keyboardHints"))
        self.assertIsNone(self.col.get_config("reviewerFontSize"))

    def test_noop_preserves_undo_history(self):
        self.create("History")
        before = self.col.undo_status()
        result = self.rpc("prefs.set", {"preferences": self.rpc("prefs.get")})
        self.assertFalse(any(result["changes"].values()))
        self.assertEqual(self.col.undo_status(), before)

    def test_zero_and_upper_bound_preferences(self):
        for hour, seconds in [(0, 0), (23, 86400)]:
            wanted = {**self.rpc("prefs.get"), "nextDayStartsAt": hour, "learnAheadSeconds": seconds,
                      "timeboxSeconds": seconds, "showIntervals": True,
                      "showRemainingCounts": False}
            self.rpc("prefs.set", {"preferences": wanted})
            self.assertEqual(self.rpc("prefs.get"), wanted)
            self.assertEqual(self.col.get_config("collapseTime"), seconds)
            self.assertEqual(self.col.get_config("timeLim"), seconds)

    def test_backup_only_changes_zero_retention_and_boundaries(self):
        original = self.rpc("prefs.get")
        for backup in [{"intervalMinutes": 1, "daily": 0, "weekly": 0, "monthly": 0},
                       {"intervalMinutes": 1440, "daily": 999, "weekly": 999, "monthly": 999}]:
            with self.subTest(backup=backup):
                before = self.col.get_preferences()
                wanted = {**original, "backup": backup}
                result = self.rpc("prefs.set", {"preferences": wanted})
                self.assertTrue(result["changes"]["config"])
                self.assertEqual(self.rpc("prefs.get"), wanted)
                native = self.col.get_preferences()
                self.assertEqual(native.scheduling, before.scheduling)
                self.assertEqual(native.reviewing, before.reviewing)
                self.assertEqual(native.backups.minimum_interval_mins, backup["intervalMinutes"])
                self.assertEqual((native.backups.daily, native.backups.weekly, native.backups.monthly),
                                 (backup["daily"], backup["weekly"], backup["monthly"]))
                self.col.undo()
                self.assertEqual(self.col.get_preferences(), before)
                self.col.redo()
                self.assertEqual(self.rpc("prefs.get"), wanted)

    def test_invalid_backup_parameters_leave_preferences_and_history_unchanged(self):
        defaults = self.rpc("prefs.get")
        before = self.col.get_preferences()
        history = self.col.undo_status()
        backups = [None, {}, {**defaults["backup"], "enabled": True}]
        for key, value in [("intervalMinutes", 0), ("intervalMinutes", 1441),
                           ("intervalMinutes", True), ("intervalMinutes", 1.5),
                           ("daily", -1), ("daily", 1000), ("weekly", "4"), ("monthly", False)]:
            backups.append({**defaults["backup"], key: value})
        for backup in backups:
            with self.subTest(backup=backup):
                self.error("prefs.set", {"preferences": {**defaults, "backup": backup}}, "invalid_params")
                self.assertEqual(self.col.get_preferences(), before)
                self.assertEqual(self.col.undo_status(), history)

    def test_invalid_preferences_and_unknown_app_settings(self):
        defaults = self.rpc("prefs.get")
        self.error("prefs.get", {"extra": True}, "invalid_params")
        self.error("prefs.set", {}, "invalid_params")
        self.error("prefs.set", {"preferences": {}}, "invalid_params")
        for key, value in [("nextDayStartsAt", 24), ("nextDayStartsAt", -1),
                           ("learnAheadSeconds", 86401), ("timeboxSeconds", -1),
                           ("showIntervals", 1), ("showRemainingCounts", "false"),
                           ("keyboardHints", True), ("reviewerFontSize", 22), ("autoplay", True),
                           ("defaultDeckId", 1), ("defaultNotetypeId", 1)]:
            with self.subTest(key=key, value=value):
                self.error("prefs.set", {"preferences": {**defaults, key: value}}, "invalid_params")
        self.assertEqual(self.rpc("prefs.get"), defaults)
