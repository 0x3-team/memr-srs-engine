"""E04 RPCs use disposable collections and the pinned native search parser."""
import io
import json
import logging
import unittest

from memr_srs_engine import handlers
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from memr_srs_engine.collection import undo_state
from tests.helpers import temp_collection


class SearchRpcCase(unittest.TestCase):
    def setUp(self):
        self.collection = temp_collection()
        self.col = self.collection.__enter__()
        self.output = io.BytesIO()
        logger = logging.getLogger("e04-tests")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger)
        self.sequence = 0
        self.progress_events = []

    def tearDown(self):
        self.engine.progress.close()
        self.collection.__exit__(None, None, None)

    def frame(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": method,
                                         "params": {} if params is None else params}).encode())
        frames = [json.loads(line) for line in self.output.getvalue()[offset:].splitlines()]
        responses = []
        for frame in frames:
            if frame.get("event"):
                self.assertEqual(frame["event"], "progress")
                self.assertEqual(frame["operationId"], frame["progress"]["operationId"])
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"],
                                                frame["progress"]), [])
                self.progress_events.append(frame)
            else:
                self.assertEqual(frame["id"], self.sequence)
                responses.append(frame)
        self.assertEqual(len(responses), 1, frames)
        return responses[0]

    def rpc(self, method, params=None):
        frame = self.frame(method, params)
        self.assertIn("result", frame, frame)
        result = frame["result"]
        self.assertEqual(validate_value(self.engine.registry.catalogue[method]["result"], result), [])
        entry = self.engine.registry.catalogue[method]
        if entry["mutating"] and not entry["longOperation"] and not method.startswith("progress."):
            self.assertEqual(result["undo"], {**undo_state(self.col), "revision": self.engine.undo_revision()})
        return result

    def error(self, method, params, code):
        frame = self.frame(method, params)
        self.assertEqual(frame.get("error", {}).get("code"), code, frame)
        self.assertNotIn("Traceback", json.dumps(frame))
        return frame["error"]

    def note(self, front, back="Generated answer", tags=(), deck_id=1, reversed=False):
        model = self.col.models.by_name("Basic (and reversed card)" if reversed else "Basic")
        note = self.col.new_note(model)
        note["Front"], note["Back"], note.tags = front, back, list(tags)
        self.col.add_note(note, deck_id)
        return note


class SearchTests(SearchRpcCase):
    def test_native_boolean_field_tag_deck_and_note_search(self):
        deck = self.col.decks.id('Language::A " B')
        a = self.note("alpha", tags=["science::one"], deck_id=deck, reversed=True)
        b = self.note("beta", tags=["other"])
        self.note("gamma")
        cases = [("(Front:alpha OR Front:beta) -tag:other", [c.id for c in a.cards()]),
                 ('"deck:Language::A \\" B"', [c.id for c in a.cards()]),
                 ("tag:science::*", [c.id for c in a.cards()]),
                 (f"nid:{b.id}", [b.cards()[0].id]), ("Front:no-match", [])]
        for search, expected in cases:
            with self.subTest(search=search):
                self.assertEqual(self.rpc("search.cards", {"search": search})["cardIds"], expected)
        self.assertEqual(self.rpc("search.notes", {"search": "Front:alpha"})["noteIds"], [a.id])

    def test_native_sort_both_modes_and_reversed(self):
        notes = [self.note(value) for value in ("zebra", "apple", "middle")]
        for mode, key in [("cards", "cardIds"), ("notes", "noteIds")]:
            expected = [notes[i].cards()[0].id if mode == "cards" else notes[i].id for i in (1, 2, 0)]
            self.assertEqual(self.rpc("search." + mode, {"search": "", "sort": "noteFld"})[key], expected)
            self.assertEqual(self.rpc("search." + mode, {"search": "", "sort": "noteFld", "descending": True})[key], expected[::-1])

    def test_numeric_sort_descending_is_absolute(self):
        low, high = self.note("low").cards()[0], self.note("high").cards()[0]
        low.ivl, high.ivl = 5, 30
        self.col.update_cards([low, high])
        self.assertEqual(self.rpc("search.cards", {"search": "", "sort": "cardIvl"})["cardIds"], [low.id, high.id])
        self.assertEqual(self.rpc("search.cards", {"search": "", "sort": "cardIvl", "descending": True})["cardIds"], [high.id, low.id])

    def test_build_escapes_literal_filters_and_supports_native_join_replace(self):
        deck = self.col.decks.id('Only "quoted" * deck')
        note = self.note("is:due", tags=['topic'], deck_id=deck)
        self.note("other")
        params = {"terms": [{"kind": "deck", "value": 'Only "quoted" * deck'},
                             {"kind": "text", "value": "is:due"}], "join": "and"}
        search = self.rpc("search.build", params)["search"]
        self.assertEqual(self.rpc("search.notes", {"search": search})["noteIds"], [note.id])
        built = self.rpc("search.build", {"terms": [{"kind": "tag", "value": "topic"}], "join": "or",
                                          "existingSearch": "Front:missing", "operation": "join"})["search"]
        self.assertEqual(self.rpc("search.notes", {"search": built})["noteIds"], [note.id])
        replaced = self.rpc("search.build", {"terms": [{"kind": "deck", "value": 'Only "quoted" * deck'}],
                                             "join": "and", "existingSearch": "deck:Default is:new", "operation": "replace"})["search"]
        self.assertEqual(self.rpc("search.notes", {"search": replaced})["noteIds"], [note.id])

    def test_build_each_filter_and_negation(self):
        note = self.note("alpha", tags=["yes"])
        filters = [{"kind": "notetype", "value": "Basic"}, {"kind": "flag", "value": "0"},
                   {"kind": "state", "value": "new"}, {"kind": "added", "value": "1"},
                   {"kind": "field", "field": "Front", "value": "alpha"},
                   {"kind": "raw", "value": "tag:no", "negate": True}]
        search = self.rpc("search.build", {"terms": filters, "join": "and"})["search"]
        self.assertEqual(self.rpc("search.notes", {"search": search})["noteIds"], [note.id])
        self.rpc("search.build", {"terms": [{"kind": "due", "value": "-1"}], "join": "and"})
        self.assertEqual(self.rpc("search.build", {"terms": [], "join": "and"})["search"], "")

    def test_search_errors_keep_anki_message_and_validate_sort(self):
        for method in ("search.cards", "search.notes"):
            error = self.error(method, {"search": "is:unknown"}, "invalid_params")
            self.assertNotEqual(error["message"], "The engine rejected the input.")
            for params in [{"search": "", "sort": "n.id; drop table notes"}, {"search": "", "sort": "answer"},
                           {"search": "\ud800"}, {"search": "", "descending": 1}]:
                self.error(method, params, "invalid_params")
        self.error("search.notes", {"search": "", "sort": "difficulty"}, "invalid_params")

    def test_build_invalid_semantics_are_read_only(self):
        self.note("unchanged")
        before = (self.col.mod, self.col.undo_status(), self.col.find_notes(""))
        invalid = [{"kind": "flag", "value": "8"}, {"kind": "state", "value": "unknown"},
                   {"kind": "added", "value": "-1"}, {"kind": "due", "value": "1.5"},
                   {"kind": "field", "value": "x"}, {"kind": "raw", "value": "is:unknown"}]
        for term in invalid:
            self.error("search.build", {"terms": [term], "join": "and"}, "invalid_params")
        self.assertEqual((self.col.mod, self.col.undo_status(), self.col.find_notes("")), before)
