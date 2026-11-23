"""Native rows, bounded session cursors and undoable browser preferences."""
from unittest.mock import patch
from unittest.mock import PropertyMock
from pathlib import Path
import tempfile
import unittest

from anki.collection import Collection
from memr_srs_engine.collection import mutation_result, undo_state
from memr_srs_engine.errors import EngineError
from tests.helpers import EnginePipe

from tests.test_search import SearchRpcCase


class BrowserTests(SearchRpcCase):
    def page(self, **kwargs):
        return self.rpc("browser.page", {"search": "", "mode": "cards", "columns": ["noteFld", "template"],
                                          "limit": 2, **kwargs})

    def test_columns_and_undoable_selection_in_both_modes(self):
        for mode in ("cards", "notes"):
            original = self.rpc("browser.columns", {"mode": mode})
            by_key = {c["key"]: c for c in original["available"]}
            self.assertTrue(by_key["noteFld"]["sortable"])
            self.assertFalse(by_key["answer"]["sortable"])
            self.assertEqual(by_key["difficulty"]["sortable"], mode == "cards")
            result = self.rpc("browser.setColumns", {"mode": mode, "columns": ["noteFld", "noteTags"]})
            self.assertTrue(result["changes"]["config"])
            self.assertTrue(result["undo"]["canUndo"])
            self.assertEqual(self.rpc("browser.columns", {"mode": mode})["selected"], ["noteFld", "noteTags"])
            self.col.undo()
            self.assertEqual(self.rpc("browser.columns", {"mode": mode})["selected"], original["selected"])
            self.col.redo()
            self.assertEqual(self.rpc("browser.columns", {"mode": mode})["selected"], ["noteFld", "noteTags"])

    def test_cursor_search_once_stable_pages_and_resume(self):
        notes = [self.note(value) for value in ("C", "A", "B", "D", "E")]
        expected = [notes[i].cards()[0].id for i in (1, 2, 0, 3, 4)]
        with patch.object(self.col, "find_cards", wraps=self.col.find_cards) as native:
            first = self.page()
            second = self.page(cursor=first["nextCursor"])
            again = self.page(cursor=first["nextCursor"])
            third = self.page(cursor=second["nextCursor"])
            self.assertEqual(native.call_count, 1)
        self.assertEqual([r["id"] for p in (first, second, third) for r in p["rows"]], expected)
        self.assertEqual(second, again)
        self.assertIsNone(third["nextCursor"])
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["revision"], third["revision"])
        self.assertEqual(self.page(offset=4)["rows"][0]["id"], expected[4])
        self.assertEqual(self.page(offset=99)["rows"], [])

    def test_cursor_lru_four_searches_and_session_isolation(self):
        for i in range(5):
            self.note(str(i))
        first = self.page()
        others = [self.page(search=f"-{i}") for i in (10, 11, 12)]
        self.page(cursor=first["nextCursor"])
        self.page(search="-13")
        self.error("browser.page", {"search": "-10", "mode": "cards", "columns": ["noteFld", "template"],
                                    "limit": 2, "cursor": others[0]["nextCursor"]}, "not_found")
        self.page(cursor=first["nextCursor"])

    def test_cursor_rejects_query_changes_and_collection_mutations(self):
        for i in range(3):
            self.note(str(i))
        first = self.page()
        params = {"search": "", "mode": "cards", "columns": ["noteFld", "template"], "limit": 2,
                  "cursor": first["nextCursor"]}
        for changed in ({"search": "is:new"}, {"mode": "notes"}, {"columns": ["noteFld"]},
                        {"descending": True}, {"sort": "noteCrt"}, {"offset": 0}):
            self.error("browser.page", {**params, **changed}, "invalid_params")
        self.note("new")
        self.error("browser.page", params, "conflict")
        self.col.undo()
        self.error("browser.page", params, "conflict")

    def test_native_rows_colours_state_and_note_aggregation(self):
        n = self.note("marked", tags=["marked"], reversed=True)
        cards = n.cards()
        cards[0].flags, cards[0].queue = 3, -1
        cards[1].queue = -2
        self.col.update_cards(cards)
        first = self.page()
        self.assertEqual([r["color"] for r in first["rows"]], ["flagGreen", "marked"])
        self.assertEqual([(r["flags"], r["marked"], r["suspended"], r["buried"]) for r in first["rows"]],
                         [(3, True, True, False), (0, True, False, True)])
        self.rpc("browser.setLayout", {**self.rpc("browser.getLayout"), "mode": "notes"})
        notes = self.page(mode="notes")
        self.assertEqual(len(notes["rows"]), 1)
        row = notes["rows"][0]
        self.assertEqual((row["id"], row["cardId"], row["noteId"]), (n.id, None, n.id))
        self.assertEqual(row["cells"][1], "2")
        self.assertEqual((row["color"], row["flags"]), ("marked", 0))
        self.assertTrue(row["marked"])
        self.assertTrue(row["suspended"])
        self.assertTrue(row["buried"])

    def test_reads_and_invalid_parameters_preserve_history(self):
        self.note("one")
        before = (self.col.mod, self.col.undo_status(), self.col.all_config())
        self.page()
        self.error("browser.page", {"search": "", "mode": "notes", "columns": ["noteFld"], "limit": 2}, "conflict")
        self.rpc("browser.columns", {"mode": "cards"})
        self.rpc("browser.savedSearches")
        self.rpc("browser.getLayout")
        invalid = [{"columns": []}, {"columns": ["unknown"]}, {"columns": ["noteFld", "noteFld"]},
                   {"sort": "answer"}, {"limit": 0}, {"cursor": "invalid"}]
        for values in invalid:
            self.error("browser.page", {"search": "", "mode": "cards", "columns": ["noteFld"], "limit": 2, **values}, "invalid_params")
        for columns in ([], ["unknown"], ["noteFld", "noteFld"]):
            self.error("browser.setColumns", {"mode": "notes", "columns": columns}, "invalid_params")
        self.assertEqual((self.col.mod, self.col.undo_status(), self.col.all_config()), before)
        self.rpc("browser.setLayout", {**self.rpc("browser.getLayout"), "mode": "notes"})
        before = (self.col.mod, self.col.undo_status(), self.col.all_config())
        self.page(mode="notes")
        self.assertEqual((self.col.mod, self.col.undo_status(), self.col.all_config()), before)

    def test_saved_search_crud_undo_redo_and_invalid_noop(self):
        original = self.rpc("browser.savedSearches")
        result = self.rpc("browser.saveSearch", {"name": "Study", "search": "is:new"})
        self.assertTrue(result["changes"]["config"])
        self.assertEqual(self.rpc("browser.savedSearches")["searches"], [{"name": "Study", "search": "is:new"}])
        self.col.undo()
        self.assertEqual(self.rpc("browser.savedSearches"), original)
        self.col.redo()
        self.rpc("browser.saveSearch", {"name": "Study", "search": "tag:marked"})
        self.assertEqual(self.rpc("browser.savedSearches")["searches"][0]["search"], "tag:marked")
        before = self.col.undo_status()
        self.error("browser.saveSearch", {"name": "Study", "search": "is:unknown"}, "invalid_params")
        self.error("browser.saveSearch", {"name": " ", "search": ""}, "invalid_params")
        self.error("browser.removeSavedSearch", {"name": "Missing"}, "not_found")
        self.assertEqual(self.col.undo_status(), before)
        self.rpc("browser.removeSavedSearch", {"name": "Study"})
        self.assertEqual(self.rpc("browser.savedSearches"), original)
        self.col.undo()
        self.assertEqual(self.rpc("browser.savedSearches")["searches"][0]["search"], "tag:marked")
        self.col.redo()
        self.assertEqual(self.rpc("browser.savedSearches"), original)

    def test_layout_persists_atomically_and_is_undoable(self):
        original = self.rpc("browser.getLayout")
        wanted = {"mode": "notes", "sidebarWidth": 120, "detailWidth": 2000, "detailVisible": False}
        before = (self.col.all_config(), self.col.undo_status())
        self.error("browser.setLayout", wanted, "invalid_params")
        self.assertEqual((self.col.all_config(), self.col.undo_status()), before)
        self.rpc("browser.setLayout", {**original, "mode": "notes"})
        self.rpc("browser.setLayout", wanted)
        self.assertEqual(self.rpc("browser.getLayout"), wanted)
        self.col.undo()
        self.assertEqual(self.rpc("browser.getLayout"), {**original, "mode": "notes"})
        self.col.redo()
        self.assertEqual(self.rpc("browser.getLayout"), wanted)
        self.error("browser.setLayout", {**wanted, "sidebarWidth": 119}, "invalid_params")
        self.assertEqual(self.rpc("browser.getLayout"), wanted)

    def test_layout_native_failures_and_noop_preserve_data_undo_and_redo(self):
        self.note("preserved")
        self.note("redo")
        self.col.undo()
        original = self.rpc("browser.getLayout")
        before = (self.col.all_config(), self.col.undo_status())
        self.assertFalse(any(self.rpc("browser.setLayout", original)["changes"].values()))
        self.assertEqual((self.col.all_config(), self.col.undo_status()), before)
        for native, changed in [("set_config", {"sidebarWidth": 333}), ("set_config_bool", {"mode": "notes"})]:
            with patch.object(self.col, native, side_effect=RuntimeError("Generated native write failure")):
                self.error("browser.setLayout", {**original, **changed}, "internal")
            self.assertEqual((self.col.all_config(), self.col.undo_status()), before)
        self.col.redo()
        self.assertEqual(len(self.col.find_notes("")), 2)

    def test_mutation_generation_catches_same_timestamp_nonundoable_writes_and_failures(self):
        notes = [self.note(str(i)) for i in range(3)]
        self.col.update_note(notes[0], skip_undo_entry=True)
        def native_write(engine, params):
            self.col.set_config("e04-native-nonundoable", params["name"])
            if params["search"] == "fail":
                raise EngineError("invalid_params", "Generated failure after a native config write.")
            return mutation_result(undo_state(self.col))
        self.engine.registry.handlers["browser.saveSearch"] = native_write
        with patch.object(type(self.col), "mod", new_callable=PropertyMock, return_value=123):
            for search in ("", "fail"):
                first = self.page()
                native_before = (self.col.mod, self.col.undo_status().last_step)
                params = {"name": search or "one", "search": search}
                if search:
                    self.error("browser.saveSearch", params, "invalid_params")
                else:
                    self.rpc("browser.saveSearch", params)
                self.assertEqual((self.col.mod, self.col.undo_status().last_step), native_before)
                self.error("browser.page", {"search": "", "mode": "cards", "columns": ["noteFld", "template"],
                                            "limit": 2, "cursor": first["nextCursor"]}, "conflict")

    def test_corrupt_native_preferences_fail_explicitly(self):
        for key, value, method, params in [("activeCols", 2, "browser.columns", {"mode": "cards"}),
                                           ("savedFilters", {"x": 2}, "browser.savedSearches", {}),
                                           ("canonnotesBrowserLayout", [], "browser.getLayout", {})]:
            self.col.set_config(key, value)
            self.error(method, params, "conflict")

    def test_integral_json_numbers_are_accepted_for_paging_and_layout(self):
        for value in ("one", "two"):
            self.note(value)
        self.assertEqual(len(self.page(limit=1.0, offset=0.0)["rows"]), 1)
        layout = {**self.rpc("browser.getLayout"), "sidebarWidth": 300.0, "detailWidth": 500.0}
        self.rpc("browser.setLayout", layout)
        self.assertEqual(self.rpc("browser.getLayout"), layout)


class BrowserProcessTests(unittest.TestCase):
    def test_restart_expires_session_cursors_and_preserves_native_browser_preferences(self):
        with tempfile.TemporaryDirectory(prefix="memr-e04-restart-") as directory:
            path = Path(directory) / "collection.anki2"
            col = Collection(str(path))
            try:
                for text in ("A", "B", "C"):
                    note = col.new_note(col.models.by_name("Basic"))
                    note["Front"] = text
                    col.add_note(note, 1)
            finally:
                col.close()
            params = {"search": "", "mode": "cards", "columns": ["noteFld"], "limit": 1}
            with EnginePipe(directory) as engine:
                cursor = engine.call("browser.page", params)["result"]["nextCursor"]
                self.assertTrue(cursor)
                self.assertIn("result", engine.call("browser.setColumns", {"mode": "cards", "columns": ["noteFld", "noteTags"]}))
                self.assertIn("result", engine.call("browser.saveSearch", {"name": "Restart", "search": "is:new"}))
                layout = engine.call("browser.getLayout")["result"]
                self.assertIn("result", engine.call("browser.setLayout", {**layout, "sidebarWidth": 321}))
            with EnginePipe(directory) as engine:
                stale = engine.call("browser.page", {**params, "cursor": cursor})
                self.assertEqual(stale["error"]["code"], "not_found")
                self.assertEqual(engine.call("browser.columns", {"mode": "cards"})["result"]["selected"], ["noteFld", "noteTags"])
                self.assertEqual(engine.call("browser.savedSearches")["result"]["searches"], [{"name": "Restart", "search": "is:new"}])
                self.assertEqual(engine.call("browser.getLayout")["result"]["sidebarWidth"], 321)
