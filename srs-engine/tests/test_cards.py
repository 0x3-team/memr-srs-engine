"""Card RPC contracts and scheduling behavior on disposable Anki collections."""
import io
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from anki.cards_pb2 import FsrsMemoryState
from anki.collection import Collection
from anki.stats_pb2 import CardStatsResponse
from google.protobuf.descriptor import FieldDescriptor

from memr_srs_engine import handlers
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from tests.helpers import EnginePipe, temp_collection


class CardTests(unittest.TestCase):
    def setUp(self):
        self.collection = temp_collection()
        self.col = self.collection.__enter__()
        # Initialize Anki's lazy scheduler timing/config before atomicity checks.
        self.col.sched.today
        self.output = io.BytesIO()
        logger = logging.getLogger("e02-cards-tests")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger)
        self.sequence = 0

    def tearDown(self):
        self.engine.progress.close()
        self.collection.__exit__(None, None, None)

    def frame(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": method,
                                         "params": {} if params is None else params}).encode())
        return json.loads(self.output.getvalue()[offset:])

    def rpc(self, method, params=None):
        frame = self.frame(method, params)
        self.assertIn("result", frame, frame)
        result = frame["result"]
        entry = self.engine.registry.catalogue[method]
        self.assertEqual(validate_value(entry["result"], result), [])
        if entry["mutating"]:
            self.assertEqual(set(result["changes"]), {
                "card", "note", "deck", "tag", "notetype", "config", "deckConfig",
                "mtime", "browserTable", "noteText", "studyQueues"})
            status = self.col.undo_status()
            self.assertEqual(result["undo"], {"canUndo": bool(status.undo), "undoLabel": status.undo or None,
                                              "canRedo": bool(status.redo), "redoLabel": status.redo or None,
                                              "revision": self.engine.undo_revision()})
        return result

    def error(self, method, params, code):
        frame = self.frame(method, params)
        self.assertEqual(frame.get("error", {}).get("code"), code, frame)
        self.assertNotIn("Traceback", json.dumps(frame))
        self.assertNotIn("Generated question", json.dumps(frame))

    def note(self, siblings=False):
        model = self.col.models.by_name("Basic (and reversed card)" if siblings else "Basic")
        note = self.col.new_note(model)
        note["Front"], note["Back"] = "Generated question", "Generated answer"
        self.col.add_note(note, 1)
        return note

    def state(self):
        return {"cards": self.col.db.all("select * from cards order by id"),
                "notes": self.col.db.all("select * from notes order by id"),
                "revlog": self.col.db.all("select * from revlog order by id"),
                "config": self.col.all_config()}

    def roundtrip(self, method, params, read):
        before = read()
        result = self.rpc(method, params)
        self.assertTrue(result["undo"]["canUndo"])
        after = read()
        self.assertNotEqual(before, after)
        undo = self.rpc("study.undo")
        self.assertTrue(undo["undo"]["canRedo"])
        self.assertEqual(read(), before)
        self.rpc("study.redo")
        self.assertEqual(read(), after)
        return result

    def test_all_catalogue_methods_registered(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("cards.")}
        self.assertEqual(len(expected), 14)
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_info_new_and_review_metadata(self):
        card = self.note().cards()[0]
        with patch.object(self.col, "card_stats_data", wraps=self.col.card_stats_data) as stats:
            info = self.rpc("cards.info", {"cardId": card.id})
            stats.assert_called_once_with(card.id)
        self.assertEqual(info["reviews"], [])
        self.assertEqual(info["card"]["type"], "new")
        self.assertEqual(info["card"]["position"], card.due)
        self.assertIsNone(info["card"]["memoryState"])
        self.assertIsNone(info["card"]["originalDeckId"])
        card.type, card.queue, card.ivl, card.factor = 2, 2, 17, 2350
        card.reps, card.lapses, card.original_position, card.flags = 8, 3, 45, 0x80 | 6
        self.col.update_card(card)
        info = self.rpc("cards.info", {"cardId": float(card.id)})["card"]
        self.assertEqual((info["type"], info["queue"], info["intervalDays"], info["ease"]),
                         ("review", 2, 17, 2.35))
        self.assertEqual((info["repetitions"], info["lapses"], info["position"], info["flags"]), (8, 3, 45, 6))
        self.assertEqual(info["modifiedAt"], self.col.get_card(card.id).mod)

    def test_info_history_ids_kinds_and_units(self):
        card = self.note().cards()[0]
        base = (int(time.time()) - 86400) * 1000
        kinds = ("learning", "review", "relearning", "filtered", "manual", "rescheduled")
        # Anki's stats has seconds-only timestamps; two rows in the same second
        # must retain distinct revlog ids in the public projection.
        for index in range(6):
            self.col.db.execute("insert into revlog values (?,?,?,?,?,?,?,?,?)",
                                base + index, card.id, -1, 0 if index >= 4 else index + 1,
                                -600 if index == 0 else 12, -120, 2500, 1500, index)
        result = self.rpc("cards.info", {"cardId": card.id})
        reviews = result["reviews"]
        self.assertEqual([review["id"] for review in reviews], [base + index for index in reversed(range(6))])
        self.assertEqual([review["kind"] for review in reviews], list(reversed(kinds)))
        self.assertEqual({review["reviewedAt"] for review in reviews}, {base // 1000})
        self.assertEqual(reviews[-1]["intervalDays"], 600 / 86400)
        self.assertEqual(reviews[0]["intervalDays"], 12)
        self.assertEqual(reviews[0]["lastIntervalDays"], 120 / 86400)
        self.assertEqual(reviews[0]["ease"], 2.5)
        self.assertEqual(reviews[0]["elapsedSeconds"], 1.5)

    def test_filtered_new_card_preserves_signed_native_position(self):
        card = self.note().cards()[0]
        original_position = card.due
        config = self.rpc("filtered.defaults")
        config.update(name="Generated filtered card info",
                      terms=[{"search": f"cid:{card.id}", "limit": 1, "order": "added"}])
        deck_id = self.rpc("filtered.createOrUpdate", {"config": config})["deckId"]
        for method in ("filtered.rebuild", "filtered.empty"):
            self.sequence += 1
            offset = self.output.tell()
            self.engine.dispatch(json.dumps({"id": self.sequence, "method": method,
                                             "params": {"deckId": deck_id}}).encode())
            self.engine.progress.thread.join(timeout=5)
            self.assertFalse(self.engine.progress.thread.is_alive())
            # A quick operation may emit progress before dispatch returns its acknowledgement.
            frames = [json.loads(line) for line in self.output.getvalue()[offset:].splitlines()]
            ack = next(frame for frame in frames if frame.get("id") == self.sequence)["result"]
            self.assertEqual(self.rpc("progress.get", {"operationId": ack["operationId"]})["status"], "completed")
            native_card = self.col.get_card(card.id)
            native_stats = self.col.card_stats_data(card.id)
            self.assertEqual(native_stats.due_position, native_card.due)
            if method == "filtered.rebuild":
                self.assertLess(native_stats.due_position, 0)
                self.assertEqual(native_card.odue, original_position)
            else:
                self.assertEqual(native_stats.due_position, original_position)
            for reader in ("cards.info", "stats.cardStats"):
                with self.subTest(method=method, reader=reader):
                    result = self.rpc(reader, {"cardId": card.id})
                    self.assertEqual(result["card"]["position"], native_stats.due_position)
                    self.assertEqual(result["card"]["originalDeckId"], native_card.odid or None)
                    if reader == "stats.cardStats":
                        self.assertEqual(result["details"]["duePosition"], native_stats.due_position)

    def test_card_position_contract_matches_native_signed_int32(self):
        field = CardStatsResponse.DESCRIPTOR.fields_by_name["due_position"]
        self.assertEqual(field.type, FieldDescriptor.TYPE_INT32)
        for reader in ("cards.info", "stats.cardStats"):
            schema = self.engine.registry.catalogue[reader]["result"]["properties"]["card"]["properties"]["position"]
            for position in (None, -(2**31), -100000, 0, 2**31 - 1):
                with self.subTest(reader=reader, valid=position):
                    self.assertEqual(validate_value(schema, position), [])
            for position in (-(2**31) - 1, 2**31, -0.5, "-100000", True):
                with self.subTest(reader=reader, invalid=position):
                    self.assertTrue(validate_value(schema, position))

    def test_info_memory_state(self):
        card = self.note().cards()[0]
        card.type, card.queue, card.ivl = 2, 2, 10
        card.memory_state = FsrsMemoryState(stability=10, difficulty=5)
        card.last_review_time = int(time.time()) - 86400
        self.col.update_card(card)
        expected = self.col.card_stats_data(card.id)
        result = self.rpc("cards.info", {"cardId": card.id})["card"]["memoryState"]
        self.assertIsNotNone(result)
        self.assertEqual(result["stability"], expected.memory_state.stability)
        self.assertEqual(result["difficulty"], expected.memory_state.difficulty)
        self.assertEqual(result["retrievability"], expected.fsrs_retrievability)

    def test_info_learning_relearning_and_queue_dependent_due(self):
        card = self.note().cards()[0]
        for card_type, queue, due, name in ((1, 1, int(time.time()) + 120, "learning"),
                                           (3, 3, self.col.sched.today + 1, "relearning")):
            card = self.col.get_card(card.id)
            card.type, card.queue, card.due = card_type, queue, due
            self.col.update_card(card)
            info = self.rpc("cards.info", {"cardId": card.id})["card"]
            self.assertEqual((info["type"], info["queue"], info["due"]), (name, queue, due))
            self.assertIsNone(info["position"])

    def test_preview_shared_renderer_interface_and_no_scheduling(self):
        card = self.note().cards()[0]
        rendered = card.render_output()
        payload = {"cardId": card.id, "noteId": card.nid, "ord": card.ord, "deckId": card.did,
                   "queue": int(card.queue), "questionHtml": rendered.question_text,
                   "answerHtml": rendered.answer_text, "css": rendered.css, "bodyClass": "card card1",
                   "av": {"question": [], "answer": []}, "typeAnswer": None,
                   "autoplay": True, "replayQuestionOnAnswer": True, "timer": {"show": False, "limitSeconds": 60},
                   "autoAdvance": {"enabled": False, "questionSeconds": 0, "answerSeconds": 0,
                                   "questionAction": "none", "answerAction": "none", "waitForAudio": True},
                   "nextStates": [], "counts": {"new": 0, "learning": 0, "review": 0},
                   "flags": 0, "marked": False, "source": None}
        before, undo = self.state(), self.col.undo_status()
        with patch("memr_srs_engine.render.render_card", return_value=payload) as render_card:
            for fill in (False, True):
                params = {"cardId": card.id, **({"fillEmpty": True} if fill else {})}
                result = self.rpc("cards.preview", params)
                self.assertEqual(result, payload)
                args, kwargs = render_card.call_args
                self.assertIs(args[0], self.col)
                self.assertEqual(args[1].id, card.id)
                self.assertEqual(kwargs, {"fill_empty": fill})
        self.assertEqual(self.state(), before)
        self.assertEqual(self.col.undo_status(), undo)

    def test_preview_real_renderer(self):
        card = self.note().cards()[0]
        before, undo = self.state(), self.col.undo_status()
        result = self.rpc("cards.preview", {"cardId": card.id})
        self.assertIn("Generated question", result["questionHtml"])
        self.assertIn("Generated answer", result["answerHtml"])
        self.assertEqual([state["rating"] for state in result["nextStates"]], [1, 2, 3, 4])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.col.undo_status(), undo)

    def test_queue_operations_card_scope_undo_redo(self):
        selected, sibling = self.note(siblings=True).cards()
        read = lambda: [int(self.col.get_card(card.id).queue) for card in (selected, sibling)]
        for method, queue in (("bury", -3), ("unbury", 0), ("suspend", -1), ("unsuspend", 0)):
            with self.subTest(method=method):
                result = self.roundtrip("cards." + method, {"cardIds": [selected.id, selected.id]}, read)
                self.assertEqual(result["count"], 1)
                self.assertTrue(result["changes"]["card"])
                self.assertEqual(read(), [queue, 0])

    def test_queue_operations_note_scope_undo_redo(self):
        cards = self.note(siblings=True).cards()
        read = lambda: [int(self.col.get_card(card.id).queue) for card in cards]
        for method, queue in (("bury", -3), ("unbury", 0), ("suspend", -1), ("unsuspend", 0)):
            with self.subTest(method=method):
                result = self.roundtrip("cards." + method, {"cardIds": [cards[0].id], "scope": "note"}, read)
                self.assertEqual(result["count"], 2)
                self.assertEqual(read(), [queue, queue])

    def test_restore_keeps_buried_and_suspended_distinct(self):
        buried, suspended = self.note(siblings=True).cards()
        self.col.sched.bury_cards([buried.id], manual=False)
        self.col.sched.suspend_cards([suspended.id])
        result = self.rpc("cards.unbury", {"cardIds": [buried.id], "scope": "note"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_card(suspended.id).queue, -1)
        self.col.sched.bury_cards([buried.id])
        result = self.rpc("cards.unsuspend", {"cardIds": [suspended.id], "scope": "note"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_card(buried.id).queue, -3)
        self.assertEqual(self.rpc("cards.bury", {"cardIds": [suspended.id], "scope": "note"})["count"], 1)
        self.col.sched.suspend_cards([suspended.id])
        self.assertEqual(self.rpc("cards.bury", {"cardIds": [suspended.id]})["count"], 0)
        self.assertEqual(self.col.get_card(suspended.id).queue, -1)

    def test_noop_restore_and_repeated_operations_preserve_undo(self):
        card = self.note().cards()[0]
        for method in ("unbury", "unsuspend"):
            before = self.col.undo_status()
            result = self.rpc("cards." + method, {"cardIds": [card.id]})
            self.assertEqual(result["count"], 0)
            self.assertFalse(any(result["changes"].values()))
            after = self.col.undo_status()
            self.assertEqual((after.undo, after.redo), (before.undo, before.redo))
        for method in ("bury", "suspend"):
            self.rpc("cards." + method, {"cardIds": [card.id]})
            before = self.col.undo_status()
            result = self.rpc("cards." + method, {"cardIds": [card.id]})
            self.assertEqual(result["count"], 0)
            self.assertFalse(any(result["changes"].values()))
            after = self.col.undo_status()
            self.assertEqual((after.undo, after.redo), (before.undo, before.redo))

    def test_flags_preserve_other_bits_and_clear_undo_redo(self):
        card = self.note().cards()[0]
        card.flags = 0x80
        self.col.update_card(card)
        read = lambda: self.col.get_card(card.id).flags
        for value in (1, 2, 3, 4, 5, 6, 7, 0):
            result = self.roundtrip("cards.flag", {"cardIds": [card.id, card.id], "flag": value}, read)
            self.assertEqual(result["count"], 1)
            self.assertEqual(read(), 0x80 | value)
        result = self.rpc("cards.flag", {"cardIds": [card.id], "flag": 0})
        self.assertEqual(result["count"], 0)
        self.assertFalse(any(result["changes"].values()))

    def test_forget_options_defaults_and_undo_redo(self):
        card = self.note().cards()[0]
        for options in ({}, {"resetCounts": True}, {"restorePosition": True},
                        {"resetCounts": True, "restorePosition": True}):
            card = self.col.get_card(card.id)
            card.type, card.queue, card.ivl, card.factor = 2, 2, 25, 2400
            card.reps, card.lapses, card.original_position = 10, 4, 123
            card.memory_state = FsrsMemoryState(stability=20, difficulty=6)
            self.col.update_card(card)
            read = lambda: self.col.get_card(card.id)._to_backend_card().SerializeToString()
            result = self.roundtrip("cards.forget", {"cardIds": [card.id, card.id], **options}, read)
            self.assertEqual(result["count"], 1)
            forgotten = self.col.get_card(card.id)
            self.assertEqual((forgotten.type, forgotten.queue, forgotten.ivl, forgotten.factor), (0, 0, 0, 0))
            self.assertEqual((forgotten.reps, forgotten.lapses), (0, 0) if options.get("resetCounts") else (10, 4))
            self.assertIsNone(forgotten.memory_state)
            self.assertIsNone(forgotten.original_position)
            if options.get("restorePosition"):
                self.assertEqual(forgotten.due, 123)
            else:
                self.assertNotEqual(forgotten.due, 123)
            self.assertEqual(self.rpc("cards.info", {"cardId": card.id})["reviews"][0]["kind"], "manual")

    def test_set_due_date_syntax_force_reset_and_undo_redo(self):
        card = self.note().cards()[0]
        read = lambda: self.col.get_card(card.id)._to_backend_card().SerializeToString()
        result = self.roundtrip("cards.setDueDate", {"cardIds": [card.id], "days": "10"}, read)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_card(card.id).due, self.col.sched.today + 10)
        self.assertEqual(self.col.get_card(card.id).ivl, 10)
        for days, force, interval in (("2", False, 10), ("3", True, 3), ("4!", False, 4), ("5!", True, 5)):
            self.rpc("cards.setDueDate", {"cardIds": [card.id], "days": days, "forceReset": force})
            self.assertEqual(self.col.get_card(card.id).ivl, interval)
        for days in ("6-8", "8-6", "7-7!"):
            self.rpc("cards.setDueDate", {"cardIds": [card.id], "days": days})
            self.assertIn(self.col.get_card(card.id).due - self.col.sched.today, range(6, 9))
        self.rpc("cards.setDueDate", {"cardIds": [card.id], "days": "0"})
        self.assertEqual(self.col.get_card(card.id).due, self.col.sched.today)

    def test_reposition_order_step_shuffle_shift_and_undo_redo(self):
        siblings = self.note(siblings=True).cards()
        other = self.note().cards()[0]
        unselected = self.note().cards()[0]
        ids = [other.id, siblings[1].id, siblings[0].id]
        params = {"cardIds": ids, "start": 10, "step": 3, "randomize": False, "shiftExisting": False}
        read = lambda: [self.col.get_card(card_id).due for card_id in ids + [unselected.id]]
        original_unselected = self.col.get_card(unselected.id).due
        result = self.roundtrip("cards.reposition", params, read)
        self.assertEqual(result["count"], 3)
        self.assertEqual(read(), [10, 13, 13, original_unselected])
        self.col.sched.reposition_new_cards([unselected.id], 10, 1, False, False)
        result = self.roundtrip("cards.reposition", {**params, "start": 9, "step": 2, "shiftExisting": True}, read)
        self.assertEqual(result["count"], 3)
        self.assertEqual(self.col.get_card(unselected.id).due, 16)
        self.rpc("cards.reposition", {**params, "start": 30, "step": 4, "randomize": True})
        self.assertEqual({self.col.get_card(card_id).due for card_id in ids}, {30, 34})
        self.assertEqual(self.col.get_card(siblings[0].id).due, self.col.get_card(siblings[1].id).due)
        self.col.sched.set_due_date([other.id], "8")
        result = self.rpc("cards.reposition", {**params, "start": 50})
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.col.get_card(other.id).type, 2)

    def test_set_deck_restores_filtered_state_and_undo_redo(self):
        card = self.note().cards()[0]
        self.col.sched.set_due_date([card.id], "15")
        filtered = self.col.sched.get_or_create_filtered_deck(0)
        filtered.name = "Generated filtered"
        filtered.config.search_terms[0].search = "deck:Default"
        filtered_id = self.col.sched.add_or_update_filtered_deck(filtered).id
        self.col.sched.rebuild_filtered_deck(filtered_id)
        filtered_card = self.col.get_card(card.id)
        self.assertEqual((filtered_card.did, filtered_card.odid), (filtered_id, 1))
        info = self.rpc("cards.info", {"cardId": card.id})["card"]
        self.assertEqual((info["deckId"], info["originalDeckId"], info["originalDue"]),
                         (filtered_id, 1, self.col.sched.today + 15))
        target_id = self.col.decks.add_normal_deck_with_name("Generated destination").id
        read = lambda: (self.col.get_card(card.id).did, self.col.get_card(card.id).odid,
                        self.col.get_card(card.id).due, self.col.get_card(card.id).odue)
        result = self.roundtrip("cards.setDeck", {"cardIds": [card.id], "deckId": target_id}, read)
        self.assertEqual(result["count"], 1)
        self.assertEqual(read(), (target_id, 0, self.col.sched.today + 15, 0))
        before, undo = self.state(), self.col.undo_status()
        self.error("cards.setDeck", {"cardIds": [card.id], "deckId": filtered_id}, "invalid_params")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.col.undo_status(), undo)
        self.error("cards.setDeck", {"cardIds": [card.id], "deckId": 9007199254740991}, "not_found")

    def test_remove_preserves_siblings_deletes_orphans_and_undo_redo(self):
        note = self.note(siblings=True)
        first, second = note.cards()
        read = lambda: (self.col.db.all("select id from cards order by id"),
                        self.col.db.all("select id from notes order by id"))
        result = self.roundtrip("cards.remove", {"cardIds": [first.id, first.id]}, read)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_note(note.id).card_ids(), [second.id])
        result = self.roundtrip("cards.remove", {"cardIds": [second.id]}, read)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["changes"]["note"])
        self.assertEqual(read(), ([], []))

    def test_user_flag_names_config_undo_redo_and_defaults(self):
        defaults = self.rpc("cards.userFlagNames")["names"]
        self.assertEqual(defaults, [getattr(self.col.tr, "actions_flag_" + color)()
                                    for color in ("red", "orange", "green", "blue", "pink", "turquoise", "purple")])
        names = [f"Generated flag {index}" for index in range(1, 8)]
        result = self.roundtrip("cards.setUserFlagNames", {"names": names},
                               lambda: self.rpc("cards.userFlagNames")["names"])
        self.assertTrue(result["changes"]["config"])
        self.assertEqual(self.col.get_config("flagLabels"), {str(index): name for index, name in enumerate(names, 1)})
        self.rpc("cards.setUserFlagNames", {"names": [""] * 7})
        self.assertEqual(self.rpc("cards.userFlagNames")["names"], defaults)

    def test_cards_process_rpc_and_persistence(self):
        with tempfile.TemporaryDirectory(prefix="memr-cards-pipe-") as directory:
            col = Collection(str(Path(directory) / "collection.anki2"))
            try:
                note = col.new_note(col.models.by_name("Basic"))
                note["Front"], note["Back"] = "Generated pipe question", "Generated pipe answer"
                col.add_note(note, 1)
                card_id = note.card_ids()[0]
            finally:
                col.close()
            names = [f"Persisted flag {index}" for index in range(1, 8)]
            with EnginePipe(directory) as pipe:
                for method, params in (("cards.preview", {"cardId": card_id}),
                                       ("cards.flag", {"cardIds": [card_id], "flag": 7}),
                                       ("study.undo", {}), ("study.redo", {}),
                                       ("cards.info", {"cardId": card_id}),
                                       ("cards.setUserFlagNames", {"names": names})):
                    with self.subTest(method=method):
                        frame = pipe.call(method, params)
                        self.assertIn("result", frame, frame)
                        self.assertEqual(validate_value(self.engine.registry.catalogue[method]["result"], frame["result"]), [])
                        if method == "cards.info":
                            self.assertEqual(frame["result"]["card"]["flags"], 7)
            with EnginePipe(directory) as pipe:
                self.assertEqual(pipe.call("cards.userFlagNames")["result"]["names"], names)
                self.assertEqual(pipe.call("cards.info", {"cardId": card_id})["result"]["card"]["flags"], 7)

    def test_invalid_params_for_every_method(self):
        for name in self.engine.registry.catalogue:
            if name.startswith("cards."):
                with self.subTest(method=name):
                    self.error(name, {"unexpected": True}, "invalid_params")
        card = self.note().cards()[0]
        selection = {"cardIds": [card.id]}
        cases = [("cards.flag", {**selection, "flag": value}) for value in (-1, 8, True, 1.5)]
        cases += [("cards.forget", {**selection, "resetCounts": 1}),
                  ("cards.bury", {**selection, "scope": "all"}),
                  ("cards.info", {"cardId": True}), ("cards.preview", {"cardId": card.id, "fillEmpty": "yes"})]
        cases += [("cards.setDueDate", {**selection, "days": value})
                  for value in ("", "-1", "1-", " 5", "5 ", "1!!", "1.5", "tomorrow", "4294967296", "2147483648", "５")]
        cases += [("cards.setUserFlagNames", {"names": value})
                  for value in ([], ["x"] * 6, ["x"] * 8, ["x" * 256] * 7, ["\ud800"] * 7)]
        params = {**selection, "start": 1, "step": 1, "randomize": False, "shiftExisting": False}
        cases += [("cards.reposition", {**params, **value})
                  for value in ({"start": -1}, {"step": 0}, {"start": 2**31}, {"step": 2**32}, {"randomize": 0})]
        before, undo = self.state(), self.col.undo_status()
        for name, params in cases:
            with self.subTest(method=name, params=params):
                self.error(name, params, "invalid_params")
                self.assertEqual(self.state(), before)
                self.assertEqual(self.col.undo_status(), undo)

    def test_all_batches_reject_empty_and_missing_before_mutation(self):
        card = self.note().cards()[0]
        extra = {"cards.flag": {"flag": 3}, "cards.setDueDate": {"days": "2"},
                 "cards.setDeck": {"deckId": 1},
                 "cards.reposition": {"start": 10, "step": 1, "randomize": False, "shiftExisting": False}}
        methods = ("bury", "unbury", "suspend", "unsuspend", "flag", "forget", "setDueDate", "reposition", "setDeck", "remove")
        before, undo = self.state(), self.col.undo_status()
        for suffix in methods:
            name = "cards." + suffix
            for ids, code in (([], "invalid_params"), ([card.id, 9007199254740991], "not_found")):
                with self.subTest(method=name, ids=ids):
                    self.error(name, {"cardIds": ids, **extra.get(name, {})}, code)
                    self.assertEqual(self.state(), before)
                    self.assertEqual(self.col.undo_status(), undo)
        for name in ("cards.info", "cards.preview"):
            self.error(name, {"cardId": 9007199254740991}, "not_found")
        for suffix in ("bury", "unbury", "suspend", "unsuspend"):
            self.error("cards." + suffix, {"cardIds": [card.id, 9007199254740991], "scope": "note"}, "not_found")
            self.assertEqual(self.state(), before)

    def test_reposition_overflow_rejected_before_changes(self):
        first, second = self.note().cards()[0], self.note().cards()[0]
        params = {"cardIds": [first.id, second.id], "start": 2**31 - 2,
                  "step": 2, "randomize": False, "shiftExisting": False}
        before, undo = self.state(), self.col.undo_status()
        self.error("cards.reposition", params, "invalid_params")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.col.undo_status(), undo)
        second.due = 2**31 - 1
        self.col.update_card(second)
        before, undo = self.state(), self.col.undo_status()
        self.error("cards.reposition", {**params, "cardIds": [first.id], "start": 1, "step": 1,
                                         "shiftExisting": True}, "invalid_params")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.col.undo_status(), undo)
