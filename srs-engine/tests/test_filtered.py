"""Filtered configuration, membership and scheduling stay owned by native Anki."""
from copy import deepcopy
import threading
from unittest.mock import patch

from tests.test_search import SearchRpcCase


class FilteredTests(SearchRpcCase):
    def config(self, **changes):
        return {**self.rpc("filtered.defaults"), "name": "Generated filtered",
                "terms": [{"search": "is:new", "limit": 100, "order": "added"}], **changes}

    def create(self, **changes):
        return self.rpc("filtered.createOrUpdate", {"config": self.config(**changes)})["deckId"]

    def operation(self, method, params):
        ack = self.rpc(method, params)
        self.assertTrue(ack["operationId"])
        self.engine.progress.thread.join(timeout=5)
        self.assertFalse(self.engine.progress.thread.is_alive())
        status = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(status["status"], "completed", status)
        result = status["result"]
        self.assertEqual(result["undo"]["canUndo"], bool(self.col.undo_status().undo))
        return result

    def test_defaults_and_configuration_only_create_update_undo_redo(self):
        defaults = self.rpc("filtered.defaults")
        self.assertEqual(defaults["deckId"], 0)
        self.assertEqual(len(defaults["terms"]), 2)
        self.assertEqual((defaults["reschedule"], defaults["previewAgainSeconds"], defaults["previewHardSeconds"]), (True, 60, 600))
        note = self.note("native")
        config = self.config(stepsMinutes=[0.5, 10], previewAgainSeconds=20, reschedule=False)
        result = self.rpc("filtered.createOrUpdate", {"config": config})
        deck_id = result["deckId"]
        self.assertTrue(result["changes"]["deck"])
        self.assertEqual(self.col.get_card(note.cards()[0].id).did, 1)
        self.assertEqual(self.rpc("filtered.get", {"deckId": deck_id}), {**config, "deckId": deck_id})
        self.col.undo()
        self.assertIsNone(self.col.decks.id_for_name(config["name"]))
        self.col.redo()
        wanted = {**config, "deckId": deck_id, "previewHardSeconds": 720, "reschedule": True}
        self.rpc("filtered.createOrUpdate", {"config": wanted})
        self.assertEqual(self.rpc("filtered.get", {"deckId": deck_id}), wanted)
        self.col.undo()
        self.assertEqual(self.rpc("filtered.get", {"deckId": deck_id})["previewHardSeconds"], 600)
        self.col.redo()
        self.assertEqual(self.rpc("filtered.get", {"deckId": deck_id}), wanted)

    def test_rebuild_native_exclusions_limits_empty_and_undo_redo(self):
        notes = [self.note(str(i)) for i in range(5)]
        suspended, buried = notes[0].cards()[0], notes[1].cards()[0]
        suspended.queue, buried.queue = -1, -2
        self.col.update_cards([suspended, buried])
        deck_id = self.create(terms=[{"search": "is:new", "limit": 2, "order": "added"}])
        result = self.operation("filtered.rebuild", {"deckId": deck_id})
        self.assertEqual(result["count"], 2)
        members = self.col.find_cards(f"did:{deck_id}")
        self.assertEqual(members, [notes[2].cards()[0].id, notes[3].cards()[0].id])
        self.assertTrue(all(self.col.get_card(i).odid == 1 for i in members))
        self.col.undo()
        self.assertEqual(self.col.find_cards(f"did:{deck_id}"), [])
        self.col.redo()
        self.assertEqual(self.col.find_cards(f"did:{deck_id}"), members)
        emptied = self.operation("filtered.empty", {"deckId": deck_id})
        self.assertEqual(emptied["count"], 2)
        self.assertTrue(emptied["changes"]["card"])
        self.assertTrue(all(self.col.get_card(i).did == 1 and self.col.get_card(i).odid == 0 for i in members))
        self.col.undo()
        self.assertEqual(self.col.find_cards(f"did:{deck_id}"), members)
        self.col.redo()
        self.assertEqual(self.operation("filtered.empty", {"deckId": deck_id})["count"], 0)

    def test_two_terms_and_every_native_order_round_trip(self):
        for order in ("oldest", "random", "intervalAscending", "intervalDescending", "lapses", "added", "due",
                      "reverseAdded", "retrievabilityAscending", "retrievabilityDescending", "relativeOverdueness"):
            if order.startswith("retrievability"):
                self.col.set_config("fsrs", True, undoable=True)
                self.assertTrue(self.col.decks.get_deck_configs_for_update(1).fsrs)
            deck_id = self.create(name="Filtered " + order, terms=[{"search": "is:new", "limit": 1, "order": order},
                                                                   {"search": "is:review", "limit": 2, "order": "due"}])
            self.assertEqual(self.rpc("filtered.get", {"deckId": deck_id})["terms"][0]["order"], order)
            self.assertEqual(self.operation("filtered.rebuild", {"deckId": deck_id})["count"], 0)

    def test_retrievability_without_fsrs_fails_before_native_membership_changes(self):
        self.note("preserved")
        deck_id = self.create(terms=[{"search": "is:new", "limit": 1, "order": "retrievabilityAscending"}])
        before = self.col.undo_status()
        ack = self.rpc("filtered.rebuild", {"deckId": deck_id})
        self.engine.progress.thread.join(timeout=5)
        status = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual((status["status"], status["error"]["code"]), ("failed", "unsupported"))
        self.assertEqual(self.col.undo_status(), before)
        self.assertEqual(self.col.find_cards(f"did:{deck_id}"), [])

    def test_integral_json_numbers_are_accepted_for_configuration_and_ids(self):
        config = self.config(deckId=0.0, terms=[{"search": "is:new", "limit": 1.0, "order": "added"}],
                             previewAgainSeconds=60.0, previewHardSeconds=600.0, previewGoodSeconds=0.0)
        deck_id = self.rpc("filtered.createOrUpdate", {"config": config})["deckId"]
        self.assertEqual(self.rpc("filtered.get", {"deckId": float(deck_id)})["deckId"], deck_id)
        updated = {**config, "deckId": float(deck_id), "previewAgainSeconds": 61.0,
                   "previewHardSeconds": 601.0, "previewGoodSeconds": 1.0}
        self.rpc("filtered.createOrUpdate", {"config": updated})
        self.assertEqual(self.rpc("filtered.get", {"deckId": deck_id})["previewGoodSeconds"], 1)
        self.assertEqual(self.operation("filtered.rebuild", {"deckId": float(deck_id)})["count"], 0)
        self.assertEqual(self.operation("filtered.empty", {"deckId": float(deck_id)})["count"], 0)

    def test_zero_minute_steps_preserve_native_supported_configuration(self):
        deck_id = self.create(stepsMinutes=[0, 0.5, 10])
        self.assertEqual(self.rpc("filtered.get", {"deckId": deck_id})["stepsMinutes"], [0, 0.5, 10])

    def test_not_found_normal_deck_and_invalid_input_leave_history_unchanged(self):
        before = ({d["id"]: d for d in deepcopy(self.col.decks.all())}, self.col.undo_status())
        self.error("filtered.get", {"deckId": 9007199254740991}, "not_found")
        self.error("filtered.get", {"deckId": 1}, "invalid_params")
        invalid = [{"name": ""}, {"name": "Bad\nName"}, {"terms": []},
                   {"terms": [{"search": "is:unknown", "limit": 1, "order": "random"}]},
                   {"terms": [{"search": "is:new", "limit": 0, "order": "random"}]},
                   {"stepsMinutes": [-1]}, {"previewHardSeconds": 86401}, {"deckId": 1},
                   {"deckId": 9007199254740991}]
        for changes in invalid:
            self.error("filtered.createOrUpdate", {"config": self.config(**changes)},
                       "not_found" if changes.get("deckId") == 9007199254740991 else "invalid_params")
        self.assertEqual(({d["id"]: d for d in self.col.decks.all()}, self.col.undo_status()), before)

    def test_name_conflict_and_filtered_parent_are_atomic(self):
        deck_id = self.create()
        before = ({d["id"]: d for d in deepcopy(self.col.decks.all())}, self.col.undo_status())
        self.error("filtered.createOrUpdate", {"config": self.config()}, "conflict")
        self.error("filtered.createOrUpdate", {"config": self.config(name="Generated filtered::Child")}, "invalid_params")
        self.assertEqual(({d["id"]: d for d in self.col.decks.all()}, self.col.undo_status()), before)
        same = self.rpc("filtered.get", {"deckId": deck_id})
        self.assertFalse(any(self.rpc("filtered.createOrUpdate", {"config": same})["changes"].values()))

    def test_long_invalid_deck_errors_are_terminal_and_do_not_mutate(self):
        before = self.col.undo_status()
        for method in ("filtered.rebuild", "filtered.empty"):
            for deck_id, code in [(9007199254740991, "not_found"), (1, "invalid_params")]:
                ack = self.rpc(method, {"deckId": deck_id})
                self.engine.progress.thread.join(timeout=5)
                status = self.rpc("progress.get", {"operationId": ack["operationId"]})
                self.assertEqual((status["status"], status["error"]["code"]), ("failed", code))
                self.assertNotIn("Traceback", str(status))
        self.assertEqual(self.col.undo_status(), before)

    def test_cancel_before_native_boundary_preserves_cards_and_undo_then_allows_retry(self):
        note = self.note("cancel")
        deck_id = self.create()
        entered, release = threading.Event(), threading.Event()
        native = self.col.sched.rebuild_filtered_deck
        def blocked(identifier):
            entered.set()
            self.assertTrue(release.wait(5))
            self.engine.progress.check_abort()
            return native(identifier)
        before = self.col.undo_status()
        with patch.object(self.col.sched, "rebuild_filtered_deck", blocked):
            ack = self.rpc("filtered.rebuild", {"deckId": deck_id})
            self.assertTrue(entered.wait(5))
            self.error("search.cards", {"search": ""}, "busy")
            self.rpc("progress.abort", {"operationId": ack["operationId"]})
            release.set()
            self.engine.progress.thread.join(timeout=5)
        status = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual((status["status"], status["error"]["code"]), ("aborted", "aborted"))
        self.assertEqual(self.col.undo_status(), before)
        self.assertEqual(self.col.get_card(note.cards()[0].id).did, 1)
        self.assertEqual(self.operation("filtered.rebuild", {"deckId": deck_id})["count"], 1)

    def test_cancel_after_native_commit_reports_completed_mutation(self):
        self.note("committed")
        deck_id = self.create()
        committed, release = threading.Event(), threading.Event()
        native = self.col.sched.rebuild_filtered_deck
        def completed(identifier):
            result = native(identifier)
            committed.set()
            self.assertTrue(release.wait(5))
            return result
        with patch.object(self.col.sched, "rebuild_filtered_deck", completed):
            ack = self.rpc("filtered.rebuild", {"deckId": deck_id})
            self.assertTrue(committed.wait(5))
            self.rpc("progress.abort", {"operationId": ack["operationId"]})
            release.set()
            self.engine.progress.thread.join(timeout=5)
        status = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["result"]["count"], 1)
        self.assertTrue(status["result"]["changes"]["card"])
        self.assertEqual(len(self.col.find_cards(f"did:{deck_id}")), 1)
