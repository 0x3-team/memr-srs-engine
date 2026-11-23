"""Deck RPC contracts exercised against disposable, real Anki collections."""
import io
import json
import logging
import tempfile
import time
import unittest

from memr_srs_engine import handlers
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from tests.helpers import EnginePipe, temp_collection


class RpcCase(unittest.TestCase):
    def setUp(self):
        self.collection = temp_collection()
        self.col = self.collection.__enter__()
        self.output = io.BytesIO()
        logger = logging.getLogger("e01-tests")
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
        self.assertEqual(validate_value(self.engine.registry.catalogue[method]["result"], result), [])
        if self.engine.registry.catalogue[method]["mutating"]:
            self.assertEqual(set(result["changes"]), {
                "card", "note", "deck", "tag", "notetype", "config", "deckConfig",
                "mtime", "browserTable", "noteText", "studyQueues"})
            status = self.col.undo_status()
            expected = {"canUndo": bool(status.undo), "undoLabel": status.undo or None,
                        "canRedo": bool(status.redo), "redoLabel": status.redo or None}
            if not self.engine.registry.catalogue[method]["longOperation"] and method not in {"progress.abort", "sync.abort"}:
                expected["revision"] = self.engine.undo_revision()
            self.assertEqual(result["undo"], expected)
        return result

    def error(self, method, params, code):
        frame = self.frame(method, params)
        self.assertEqual(frame.get("error", {}).get("code"), code, frame)
        self.assertNotIn("Traceback", json.dumps(frame))

    def create(self, name):
        return self.rpc("decks.create", {"name": name})["deckId"]

    def note(self, deck_id):
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"], note["Back"] = "Generated question", "Generated answer"
        self.col.add_note(note, deck_id)
        return note


class DeckTests(RpcCase):
    def test_all_methods_registered(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("decks.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_create_nested_and_undo_redo(self):
        result = self.rpc("decks.create", {"name": "Language::English::Grammar"})
        deck_id = result["deckId"]
        self.assertTrue(result["changes"]["deck"])
        self.assertEqual(self.col.decks.name(deck_id), "Language::English::Grammar")
        self.col.undo()
        self.assertIsNone(self.col.decks.id_for_name("Language"))
        self.col.redo()
        self.assertEqual(self.col.decks.name(deck_id), "Language::English::Grammar")

    def test_create_parent_and_existing_name(self):
        parent = self.create("Parent")
        result = self.rpc("decks.create", {"name": "Child::Leaf", "parentId": parent})
        self.assertEqual(self.col.decks.name(result["deckId"]), "Parent::Child::Leaf")
        before = self.col.undo_status()
        again = self.rpc("decks.create", {"name": "parent::child::leaf"})
        self.assertEqual(again["deckId"], result["deckId"])
        self.assertFalse(any(again["changes"].values()))
        self.assertEqual(self.col.undo_status(), before)

    def test_tree_counts_and_get(self):
        parent = self.create("Parent")
        child = self.create("Parent::Child")
        for _ in range(3):
            self.note(child)
        card_ids = self.col.find_cards("deck:Parent::Child")
        learning, review = (self.col.get_card(card_id) for card_id in card_ids[:2])
        learning.type, learning.queue, learning.due = 1, 1, int(time.time()) - 10
        review.type, review.queue, review.due, review.ivl = 2, 2, self.col.sched.today, 10
        self.col.update_cards([learning, review])
        tree = self.rpc("decks.tree")
        decks = {deck["id"]: deck for deck in tree["decks"]}
        self.assertEqual(decks[child]["parentId"], parent)
        self.assertEqual(decks[child]["counts"], {"new": 1, "learning": 1, "review": 1})
        self.assertEqual(decks[parent]["counts"], decks[child]["counts"])
        self.assertEqual(decks[parent]["totalCards"], 3)
        self.assertEqual(self.rpc("decks.get", {"deckId": child}), decks[child])
        subtree = self.rpc("decks.tree", {"topDeckId": child})
        self.assertEqual([deck["id"] for deck in subtree["decks"]], [child])
        self.assertEqual(subtree["decks"][0]["parentId"], parent)

    def test_tree_honors_daily_limits_and_hidden_default(self):
        deck_id = self.create("Limited")
        for _ in range(25):
            self.note(deck_id)
        deck = self.rpc("decks.get", {"deckId": deck_id})
        self.assertEqual(deck["counts"]["new"], 20)
        self.assertEqual(deck["totalCards"], 25)
        self.assertNotIn(1, [deck["id"] for deck in self.rpc("decks.tree")["decks"]])
        self.assertEqual(self.rpc("decks.get", {"deckId": 1})["totalCards"], 0)
        default = self.rpc("decks.tree", {"topDeckId": 1})
        self.assertEqual([deck["id"] for deck in default["decks"]], [1])

    def test_filtered_deck_flag_and_parent_rejection(self):
        filtered = self.col.decks.new_deck_legacy(True)
        filtered["name"] = "Filtered"
        deck_id = self.col.decks.add_deck_legacy(filtered).id
        deck = self.rpc("decks.get", {"deckId": deck_id})
        self.assertTrue(deck["filtered"])
        self.assertIsNone(deck["presetId"])
        self.error("decks.create", {"name": "Filtered"}, "conflict")
        self.error("decks.create", {"name": "Child", "parentId": deck_id}, "invalid_params")
        normal = self.create("Normal")
        self.error("decks.reparent", {"deckIds": [normal], "parentId": deck_id}, "invalid_params")
        self.error("decks.setDescription", {"deckId": deck_id, "description": "text"}, "unsupported")

    def test_rename_descendants_and_undo_redo(self):
        parent = self.create("Old")
        child = self.create("Old::Child")
        self.rpc("decks.rename", {"deckId": parent, "name": "New"})
        self.assertEqual(self.col.decks.name(child), "New::Child")
        self.col.undo()
        self.assertEqual(self.col.decks.name(child), "Old::Child")
        self.col.redo()
        self.assertEqual(self.col.decks.name(child), "New::Child")

    def test_rename_collision_uses_anki_disambiguation(self):
        source, target = self.create("Source"), self.create("Target")
        self.rpc("decks.rename", {"deckId": source, "name": "target"})
        self.assertEqual(self.col.decks.name(source), "target+")
        self.assertEqual(self.col.decks.name(target), "Target")

    def test_reparent_and_top_level_undo_redo(self):
        parent, child = self.create("Parent"), self.create("Old::Child")
        leaf = self.create("Old::Child::Leaf")
        self.rpc("decks.reparent", {"deckIds": [child], "parentId": parent})
        self.assertEqual(self.col.decks.name(leaf), "Parent::Child::Leaf")
        self.col.undo()
        self.assertEqual(self.col.decks.name(leaf), "Old::Child::Leaf")
        self.col.redo()
        self.rpc("decks.reparent", {"deckIds": [child], "parentId": None})
        self.assertEqual(self.col.decks.name(leaf), "Child::Leaf")

    def test_reparent_parent_and_child_selection(self):
        parent, child, target = self.create("Source"), self.create("Source::Child"), self.create("Target")
        self.rpc("decks.reparent", {"deckIds": [parent, child], "parentId": target})
        self.assertEqual(self.col.decks.name(parent), "Target::Source")
        self.assertEqual(self.col.decks.name(child), "Target::Child")

    def test_reparent_cycle_uses_anki_drag_semantics(self):
        parent, child = self.create("Parent"), self.create("Parent::Child")
        before = self.col.undo_status()
        result = self.rpc("decks.reparent", {"deckIds": [parent], "parentId": child})
        self.assertEqual(self.col.decks.name(parent), "Parent")
        self.assertFalse(result["changes"]["deck"])
        self.assertEqual(self.col.undo_status().undo, before.undo)

    def test_remove_descendants_cards_and_undo_redo(self):
        parent, child = self.create("Parent"), self.create("Parent::Child")
        note = self.note(child)
        result = self.rpc("decks.remove", {"deckIds": [parent, child, parent], "deleteCards": True})
        self.assertEqual(result["removedCount"], 2)
        self.assertEqual(self.col.card_count(), 0)
        self.assertIsNone(self.col.decks.get(child, default=False))
        self.col.undo()
        self.assertEqual(self.col.card_count(), 1)
        self.assertEqual(self.col.get_note(note.id).id, note.id)
        self.col.redo()
        self.assertEqual(self.col.card_count(), 0)

    def test_remove_without_deleting_cards_is_unsupported_and_unchanged(self):
        deck_id = self.create("Keep")
        self.note(deck_id)
        before = self.col.undo_status()
        self.error("decks.remove", {"deckIds": [deck_id], "deleteCards": False}, "unsupported")
        self.assertEqual(self.col.card_count(), 1)
        self.assertEqual(self.col.decks.name(deck_id), "Keep")
        self.assertEqual(self.col.undo_status(), before)

    def test_remove_default_retains_default_and_undo_restores_cards(self):
        self.note(1)
        result = self.rpc("decks.remove", {"deckIds": [1], "deleteCards": True})
        self.assertEqual(result["removedCount"], 0)
        self.assertIsNotNone(self.col.decks.get(1, default=False))
        self.assertEqual(self.col.card_count(), 0)
        self.col.undo()
        self.assertEqual(self.col.card_count(), 1)

    def test_collapsed_scopes_are_independent_and_not_undoable(self):
        deck_id = self.create("Collapse")
        self.rpc("decks.setCollapsed", {"deckId": deck_id, "collapsed": False})
        deck = self.rpc("decks.get", {"deckId": deck_id})
        self.assertFalse(deck["collapsed"])
        self.assertTrue(deck["browserCollapsed"])
        self.rpc("decks.setCollapsed", {"deckId": deck_id, "collapsed": False, "browser": True})
        self.assertFalse(self.rpc("decks.get", {"deckId": deck_id})["browserCollapsed"])
        # Native collapse writes persist without a separate undo entry.
        self.assertEqual(self.col.undo_status().undo, "Add Deck")

    def test_current_and_undo_redo(self):
        previous = self.col.decks.selected()
        deck_id = self.create("Current")
        result = self.rpc("decks.setCurrent", {"deckId": deck_id})
        self.assertTrue(result["changes"]["studyQueues"])
        self.assertEqual(self.rpc("decks.tree")["currentDeckId"], deck_id)
        self.col.undo()
        self.assertEqual(self.col.decks.selected(), previous)
        self.col.redo()
        self.assertEqual(self.col.decks.selected(), deck_id)

    def test_description_markdown_and_undo_redo(self):
        deck_id = self.create("Description")
        content = "# Header\n\n**Description**"
        self.rpc("decks.setDescription", {"deckId": deck_id, "description": content, "markdown": True})
        deck = self.rpc("decks.get", {"deckId": deck_id})
        self.assertEqual(deck["description"], content)
        self.assertTrue(deck["descriptionMarkdown"])
        self.col.undo()
        self.assertEqual(self.rpc("decks.get", {"deckId": deck_id})["description"], "")
        self.col.redo()
        self.assertEqual(self.rpc("decks.get", {"deckId": deck_id})["description"], content)
        self.rpc("decks.setDescription", {"deckId": deck_id, "description": "<p>HTML</p>"})
        self.assertTrue(self.rpc("decks.get", {"deckId": deck_id})["descriptionMarkdown"])

    def test_today_empty_and_studied_today_parity(self):
        empty = self.rpc("decks.todaySummary")
        self.assertEqual(empty, {"cards": 0, "reviews": 0, "elapsedSeconds": 0,
                                 "averageSeconds": 0, "againCount": 0, "correctCount": 0})
        deck_id = self.create("Reviews")
        card = self.note(deck_id).cards()[0]
        # Generated revlog entries straddle Anki's rollover; repeated answers
        # count separately, while manual/reschedule rows must not count.
        cutoff = (self.col.sched.day_cutoff - 86400) * 1000
        for offset, ease, kind, millis in [(-1000, 3, 1, 5000), (0, 3, 1, 5000),
                                           (1000, 1, 0, 1500), (2000, 3, 1, 2500),
                                           (3000, 0, 4, 1000), (4000, 0, 5, 1000)]:
            self.col.db.execute("insert into revlog values (?,?,?,?,?,?,?,?,?)",
                                cutoff + offset, card.id, -1, ease, 10, 5, 2500, millis, kind)
        summary = self.rpc("decks.todaySummary")
        self.assertEqual(summary, {"cards": 2, "reviews": 2, "elapsedSeconds": 4,
                                   "averageSeconds": 2, "againCount": 1, "correctCount": 1})
        self.assertEqual(self.col.studied_today(), self.col._backend.studied_today_message(
            cards=summary["cards"], seconds=summary["elapsedSeconds"]))

    def test_invalid_parameters_for_every_method(self):
        cases = {"tree": {"topDeckId": -1}, "get": {}, "create": {"name": 3},
                 "rename": {"deckId": 1, "name": False}, "reparent": {"deckIds": [False], "parentId": None},
                 "remove": {"deckIds": [1], "deleteCards": 1},
                 "setCollapsed": {"deckId": 1, "collapsed": "false"}, "setCurrent": {"deckId": -1},
                 "setDescription": {"deckId": 1, "description": 3}, "todaySummary": {"extra": True}}
        for suffix, params in cases.items():
            with self.subTest(method=suffix):
                self.error("decks." + suffix, params, "invalid_params")

    def test_semantically_empty_names_and_selections(self):
        for name in ["", " ", "Parent::::Child", "::Child", "Parent::"]:
            with self.subTest(name=name):
                self.error("decks.create", {"name": name}, "invalid_params")
        for method, params in [("reparent", {"deckIds": [], "parentId": None}),
                               ("remove", {"deckIds": [], "deleteCards": True})]:
            self.error("decks." + method, params, "invalid_params")

    def test_lone_surrogates_are_invalid_before_mutation(self):
        self.error("decks.create", {"name": "\ud800"}, "invalid_params")
        deck_id = self.create("Valid Unicode")
        self.error("decks.rename", {"deckId": deck_id, "name": "\ud800"}, "invalid_params")
        self.error("decks.setDescription", {"deckId": deck_id, "description": "\ud800"}, "invalid_params")
        self.assertEqual(self.rpc("decks.get", {"deckId": deck_id})["description"], "")

    def test_colon_normalized_rename_collision_rejects_overflow_atomically(self):
        name = "N" * 1023
        first = self.create(name)
        second = self.create(name + "+")
        source = self.create("Source")
        before = {deck["id"]: deck for deck in self.col.decks.all()}
        history = self.col.undo_status()
        self.error("decks.rename", {"deckId": source, "name": ":" + name}, "invalid_params")
        self.assertEqual({deck["id"]: deck for deck in self.col.decks.all()}, before)
        self.assertEqual(self.col.undo_status(), history)
        for deck_id in [first, second, source]:
            self.rpc("decks.get", {"deckId": deck_id})
        self.rpc("decks.tree")

    def test_colon_only_create_component_rejects_overflow_without_parent_or_history(self):
        before = {deck["id"]: deck for deck in self.col.decks.all()}
        history = self.col.undo_status()
        self.error("decks.create", {"name": "P" * 1018 + ":::"}, "invalid_params")
        self.assertIsNone(self.col.decks.id_for_name("P" * 1018))
        self.assertEqual({deck["id"]: deck for deck in self.col.decks.all()}, before)
        self.assertEqual(self.col.undo_status(), history)
        self.rpc("decks.get", {"deckId": 1})
        self.rpc("decks.tree")

    def test_colon_components_normalize_before_ancestor_and_descendant_bounds(self):
        self.create("i\u0307")
        source = self.create("X")
        child = self.create("X::" + "B" * 1014)
        before = {deck["id"]: deck for deck in self.col.decks.all()}
        history = self.col.undo_status()
        self.error("decks.create", {"name": "İ:: : ::" + "B" * 1014}, "invalid_params")
        self.error("decks.rename", {"deckId": source, "name": "İ:::"}, "invalid_params")
        self.assertEqual({deck["id"]: deck for deck in self.col.decks.all()}, before)
        self.assertEqual(self.col.undo_status(), history)
        self.assertEqual(self.rpc("decks.get", {"deckId": child})["name"], "X::" + "B" * 1014)
        self.rpc("decks.tree")

    def test_native_colon_normalization_preserves_valid_boundary_names_and_undo(self):
        name = "P" * 1017 + ":::"
        deck_id = self.create(name)
        self.assertEqual(self.rpc("decks.get", {"deckId": deck_id})["name"], "P" * 1017 + "::blank")
        self.rpc("decks.tree")
        self.col.undo()
        self.assertIsNone(self.col.decks.id_for_name("P" * 1017))
        self.col.redo()
        self.assertEqual(self.rpc("decks.get", {"deckId": deck_id})["name"], "P" * 1017 + "::blank")
        source = self.create("Source")
        self.rpc("decks.rename", {"deckId": source, "name": ": \u2003: e\u0301: \u2003:: :"})
        self.assertEqual(self.rpc("decks.get", {"deckId": source})["name"], "é::blank")
        self.col.undo()
        self.assertEqual(self.rpc("decks.get", {"deckId": source})["name"], "Source")
        self.col.redo()
        self.assertEqual(self.rpc("decks.get", {"deckId": source})["name"], "é::blank")
        self.rpc("decks.tree")

    def test_rename_and_reparent_reject_oversized_descendant_names_atomically(self):
        source = self.create("A")
        child = self.create("A::" + "B" * 1000)
        target = self.create("T" * 100)
        before = self.col.undo_status()
        self.error("decks.rename", {"deckId": source, "name": "R" * 100}, "invalid_params")
        self.assertEqual(self.col.decks.name(child), "A::" + "B" * 1000)
        self.error("decks.reparent", {"deckIds": [source], "parentId": target}, "invalid_params")
        self.assertEqual(self.col.decks.name(child), "A::" + "B" * 1000)
        self.assertEqual(self.col.undo_status(), before)
        self.rpc("decks.tree")

    def test_batch_reparent_checks_collision_suffix_lengths_before_mutation(self):
        name = "B" * 1021
        first = self.create("X::" + name)
        second = self.create("Y::" + name)
        parent = self.create("T")
        self.error("decks.reparent", {"deckIds": [first, second], "parentId": parent}, "invalid_params")
        self.assertEqual(self.col.decks.name(first), "X::" + name)
        self.assertEqual(self.col.decks.name(second), "Y::" + name)

    def test_length_preflight_uses_anki_unicode_collation(self):
        self.create("ﬃ" + "x" * 1021)
        source = self.create("Source")
        name = "ffi" + "x" * 1021
        self.rpc("decks.rename", {"deckId": source, "name": name})
        self.assertEqual(self.col.decks.name(source), name)
        self.error("decks.create", {"name": "\u0344" * 1024}, "invalid_params")

    def test_name_length_includes_existing_parent_casing(self):
        self.create("i\u0307")
        self.error("decks.create", {"name": "İ::" + "B" * 1021}, "invalid_params")
        source = self.create("X")
        child = self.create("X::" + "B" * 1018)
        self.error("decks.rename", {"deckId": source, "name": "İ::R"}, "invalid_params")
        self.assertEqual(self.col.decks.name(child), "X::" + "B" * 1018)

    def test_not_found_never_falls_back_to_default(self):
        missing = 9007199254740991
        cases = {"tree": {"topDeckId": missing}, "get": {"deckId": missing},
                 "create": {"name": "Child", "parentId": missing}, "rename": {"deckId": missing, "name": "New"},
                 "reparent": {"deckIds": [missing], "parentId": None},
                 "remove": {"deckIds": [missing], "deleteCards": True},
                 "setCollapsed": {"deckId": missing, "collapsed": False},
                 "setCurrent": {"deckId": missing}, "setDescription": {"deckId": missing, "description": "text"}}
        for suffix, params in cases.items():
            with self.subTest(method=suffix):
                self.error("decks." + suffix, params, "not_found")
        deck_id = self.create("Still here")
        self.error("decks.remove", {"deckIds": [deck_id, missing], "deleteCards": True}, "not_found")
        self.assertEqual(self.col.decks.name(deck_id), "Still here")
        self.error("decks.reparent", {"deckIds": [deck_id], "parentId": missing}, "not_found")


class DeckProcessTests(unittest.TestCase):
    def test_decks_and_preferences_persist_across_process_restart(self):
        with tempfile.TemporaryDirectory(prefix="memr-e01-persist-") as directory:
            with EnginePipe(directory) as engine:
                deck_id = engine.call("decks.create", {"name": "Persistent"})["result"]["deckId"]
                for method, params in [
                    ("decks.setCollapsed", {"deckId": deck_id, "collapsed": False}),
                    ("decks.setCollapsed", {"deckId": deck_id, "collapsed": False, "browser": True}),
                    ("decks.setDescription", {"deckId": deck_id, "description": "# Persistent", "markdown": True}),
                    ("decks.setCurrent", {"deckId": deck_id}),
                    ("prefs.set", {"preferences": {"nextDayStartsAt": 6, "learnAheadSeconds": 900,
                                                   "timeboxSeconds": 60, "showIntervals": False,
                                                   "showRemainingCounts": False,
                                                   "backup": {"intervalMinutes": 45, "daily": 0,
                                                              "weekly": 5, "monthly": 2}}}),
                ]:
                    self.assertIn("result", engine.call(method, params))
            with EnginePipe(directory) as engine:
                deck = engine.call("decks.get", {"deckId": deck_id})["result"]
                self.assertEqual((deck["collapsed"], deck["browserCollapsed"]), (False, False))
                self.assertEqual((deck["description"], deck["descriptionMarkdown"]), ("# Persistent", True))
                self.assertEqual(engine.call("decks.tree")["result"]["currentDeckId"], deck_id)
                self.assertEqual(engine.call("prefs.get")["result"]["nextDayStartsAt"], 6)
                self.assertEqual(engine.call("prefs.get")["result"]["backup"],
                                 {"intervalMinutes": 45, "daily": 0, "weekly": 5, "monthly": 2})
