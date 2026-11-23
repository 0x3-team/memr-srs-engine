"""Notetype RPCs exercise native schema migrations and disposable previews."""
import copy
import hashlib
import io
import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from memr_srs_engine import handlers
from memr_srs_engine.handlers import notetypes
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from tests.helpers import EnginePipe, temp_collection


HTML = '<table style="color: red"><tr><td>\u062a\u0633\u062a &amp; <script>1 < 2</script></td></tr></table>'


class NotetypeRegistrationTests(unittest.TestCase):
    def test_all_notetype_methods_have_handlers(self):
        registry = Registry()
        expected = {name for name in registry.catalogue if name.startswith("notetypes.")}
        self.assertEqual(expected & registry.handlers.keys(), expected)


class NotetypeContractTests(unittest.TestCase):
    def test_bulk_native_mutations_use_operation_acknowledgements(self):
        catalogue = Registry().catalogue
        for name in ("update", "remove", "changeNotetype"):
            entry = catalogue["notetypes." + name]
            self.assertTrue(entry["longOperation"], name)
            self.assertEqual(set(entry["result"]["properties"]), {"operationId", "changes", "undo"})

    def test_new_fields_and_templates_have_no_previous_ordinal(self):
        definition = Registry().catalogue["notetypes.update"]["params"]["properties"]["notetype"]
        for key in ("fields", "templates"):
            ordinal = definition["properties"][key]["items"]["properties"]["ord"]
            self.assertEqual(validate_value(ordinal, None), [], key)

    def test_stock_kind_creation_uses_the_same_add_method(self):
        schema = Registry().catalogue["notetypes.add"]["params"]
        self.assertEqual(validate_value(schema, {"stockKind": "basicReversed", "name": "My reversed"}), [])


class NotetypeTests(unittest.TestCase):
    def setUp(self):
        self.collection = temp_collection()
        self.col = self.collection.__enter__()
        self.output = io.BytesIO()
        logger = logging.getLogger("e03-notetype-tests")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger)
        self.sequence = 0
        self.progress_events = []
        self.operation_requests = {}

    def tearDown(self):
        self.engine.progress.close()
        self.collection.__exit__(None, None, None)

    def request(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": method,
                                         "params": {} if params is None else params}).encode())
        responses = []
        for raw in self.output.getvalue()[offset:].splitlines():
            frame = json.loads(raw)
            if "event" in frame:
                self.assertEqual(set(frame), {"event", "id", "operationId", "progress"})
                self.assertEqual(frame["event"], "progress")
                self.assertEqual(frame["operationId"], frame["progress"]["operationId"])
                self.assertEqual(frame["id"], self.operation_requests[frame["operationId"]])
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], frame["progress"]), [])
                self.progress_events.append(frame)
            else:
                self.assertEqual(frame["id"], self.sequence)
                responses.append(frame)
        self.assertEqual(len(responses), 1)
        response = responses[0]
        if "result" in response and self.engine.registry.catalogue[method]["longOperation"]:
            operation_id = response["result"]["operationId"]
            self.assertNotIn(operation_id, self.operation_requests)
            self.operation_requests[operation_id] = response["id"]
        return response

    def frame(self, method, params=None):
        frame = self.request(method, params)
        if "result" in frame and self.engine.registry.catalogue[method]["longOperation"]:
            self.assertEqual(validate_value(self.engine.registry.catalogue[method]["result"], frame["result"]), [])
            operation_id = frame["result"]["operationId"]
            self.engine.progress.thread.join(timeout=5)
            self.assertFalse(self.engine.progress.thread.is_alive(), method)
            terminal = self.request("progress.get", {"operationId": operation_id})["result"]
            self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], terminal), [])
            self.assertFalse(terminal["abortable"])
            if terminal["status"] == "completed":
                self.assertIsNone(terminal["error"])
                return {"id": frame["id"], "result": terminal["result"]}
            self.assertIn(terminal["status"], ("failed", "aborted"))
            self.assertIsNone(terminal["result"])
            return {"id": frame["id"], "error": terminal["error"]}
        return frame

    def rpc(self, method, params=None):
        frame = self.frame(method, params)
        self.assertIn("result", frame, frame)
        result = frame["result"]
        entry = self.engine.registry.catalogue[method]
        schema = (self.engine.registry.catalogue["progress.get"]["result"]["properties"]["result"]
                  if entry["longOperation"] else entry["result"])
        self.assertEqual(validate_value(schema, result), [])
        if self.engine.registry.catalogue[method]["mutating"]:
            self.assertEqual(set(result["changes"]), {
                "card", "note", "deck", "tag", "notetype", "config", "deckConfig",
                "mtime", "browserTable", "noteText", "studyQueues"})
            status = self.col.undo_status()
            expected = {
                "canUndo": bool(status.undo), "undoLabel": status.undo or None,
                "canRedo": bool(status.redo), "redoLabel": status.redo or None}
            if method not in {"progress.abort", "sync.abort"}:
                expected["revision"] = self.engine.undo_revision()
            self.assertEqual(result["undo"], expected)
        return result

    def error(self, method, params, code):
        before = self.snapshot()
        frame = self.frame(method, params)
        self.assertEqual(frame.get("error", {}).get("code"), code, frame)
        self.assertNotIn("Traceback", json.dumps(frame))
        self.assertEqual(self.snapshot(), before, method)

    def snapshot(self):
        return (self.col.db.all("select * from notes order by id"),
                self.col.db.all("select * from cards order by id"),
                self.col.db.all("select * from notetypes order by id"),
                self.col.db.all("select * from fields order by ntid,ord"),
                self.col.db.all("select * from templates order by ntid,ord"),
                str(self.col.undo_status()))

    def preview_snapshot(self):
        tables = ("cards", "col", "config", "deck_config", "decks", "fields", "graves",
                  "notes", "notetypes", "revlog", "tags", "templates")
        rows = tuple((table, sorted(self.col.db.all(f"select * from {table}"), key=repr))
                     for table in tables)
        files = tuple((path.name, hashlib.sha256(path.read_bytes()).hexdigest())
                      for path in (Path(self.col.path), Path(self.col.path + "-wal")) if path.exists())
        return rows, files, str(self.col.undo_status())

    def basic(self):
        return self.rpc("notetypes.get", {"notetypeId": self.col.models.by_name("Basic")["id"]})

    def clone(self, name="Fixture"):
        result = self.rpc("notetypes.clone", {"notetypeId": self.basic()["id"], "name": name})
        return self.rpc("notetypes.get", {"notetypeId": result["notetypeId"]})

    def add_note(self, model_id, fields=None):
        note = self.col.new_note(self.col.models.get(model_id))
        note.fields = fields or [HTML, "answer"]
        self.col.add_note(note, 1)
        return note

    def test_registry_and_list_use_counts(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("notetypes.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)
        model = self.basic()
        self.add_note(model["id"])
        item = next(item for item in self.rpc("notetypes.list")["notetypes"] if item["id"] == model["id"])
        self.assertEqual(item, {"id": model["id"], "name": "Basic", "kind": "normal", "noteCount": 1})
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["noteCount"], 1)

    def test_json_integer_valued_numbers_reach_native_apis_as_integers(self):
        model = self.clone()
        model["id"], model["sortField"] = float(model["id"]), 1.0
        for field in model["fields"]:
            field["ord"], field["size"] = float(field["ord"]), float(field["size"])
        model["templates"][0]["ord"] = 0.0
        model["templates"][0]["deckId"] = 1.0
        self.rpc("notetypes.update", {"notetype": model})
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["sortField"], 1)
        note = self.add_note(int(model["id"]))
        self.rpc("notetypes.previewTemplate", {"notetype": model, "cardId": float(note.card_ids()[0]),
                                               "templateOrdinal": 0.0, "fields": ["Q", "A"]})
        target = self.basic()["id"]
        self.rpc("notetypes.changeNotetype", {"noteIds": [float(note.id)], "notetypeId": float(target),
                                               "fieldMap": [0.0, 1.0], "templateMap": [0.0]})

    def test_stock_all_six_kinds_are_native_and_addable(self):
        before = self.snapshot()
        stock = self.rpc("notetypes.stock")["notetypes"]
        self.assertEqual(len(stock), 6)
        self.assertEqual([model["kind"] for model in stock],
                         ["normal", "normal", "normal", "normal", "cloze", "imageOcclusion"])
        self.assertEqual(self.snapshot(), before)
        for index, kind in enumerate(("basic", "basicReversed", "basicOptionalReversed", "typeIn", "cloze", "imageOcclusion")):
            model = self.rpc("notetypes.stock", {"kind": kind})["notetypes"][0]
            self.assertEqual(model, stock[index])
            model["name"] = f"Added {kind}"
            added = self.rpc("notetypes.add", {"notetype": model})
            self.assertTrue(added["changes"]["notetype"])
            read = self.rpc("notetypes.get", {"notetypeId": added["notetypeId"]})
            self.assertEqual(read["kind"], model["kind"])
            self.assertEqual([f["name"] for f in read["fields"]], [f["name"] for f in model["fields"]])
            self.col.undo()
            self.assertIsNone(self.col.models.get(added["notetypeId"]))
            self.col.redo()
            self.assertEqual(self.col.models.get(added["notetypeId"])["name"], model["name"])

    def test_add_from_stock_retains_original_stock_identity_and_validates_modes(self):
        for kind, original in (("basicReversed", 2), ("basicOptionalReversed", 3), ("typeIn", 4),
                               ("cloze", 5), ("imageOcclusion", 6)):
            added = self.rpc("notetypes.add", {"stockKind": kind, "name": "Stock " + kind})
            self.assertEqual(self.col.models.get(added["notetypeId"])["originalStockKind"], original)
        self.error("notetypes.add", {}, "invalid_params")
        self.error("notetypes.add", {"name": "No source"}, "invalid_params")
        self.error("notetypes.add", {"stockKind": "basic", "notetype": self.basic()}, "invalid_params")
        self.error("notetypes.add", {"notetype": self.basic(), "name": "Unused name"}, "invalid_params")

    def test_new_middle_sort_rejects_before_active_writes_and_preserves_undo_redo(self):
        model = self.clone()
        note = self.add_note(model["id"])
        card = self.col.get_card(note.card_ids()[0])
        card.reps = 37
        self.col.update_card(card)
        redo_model = copy.deepcopy(model)
        redo_model["css"] = ".card { color: navy; }"
        self.rpc("notetypes.update", {"notetype": redo_model})
        self.col.undo()
        status = self.col.undo_status()
        self.assertTrue(status.undo)
        self.assertTrue(status.redo)
        model["fields"].insert(1, {**model["fields"][0], "name": "New middle", "ord": None})
        model["sortField"] = 1
        native_update = self.col.models.update_dict
        active_writes = []

        def reject_second_update(candidate):
            active_writes.append(copy.deepcopy(candidate))
            if len(active_writes) == 2:
                candidate = copy.deepcopy(candidate)
                candidate["tmpls"][0]["qfmt"] = "{{Missing}}"
            return native_update(candidate)

        # The old two-write path commits fields, then loses history when the
        # second real native transaction rejects this injected template.
        before = self.snapshot()
        with patch.object(self.col.models, "update_dict", side_effect=reject_second_update):
            frame = self.frame("notetypes.update", {"notetype": model})
        self.assertEqual(active_writes, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(frame.get("error", {}).get("code"), "unsupported", frame)
        read = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        self.assertEqual([field["name"] for field in read["fields"]], ["Front", "Back"])
        self.assertEqual(read["sortField"], 0)
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.assertEqual(self.col.get_card(card.id).reps, 37)
        self.col.redo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["css"], redo_model["css"])
        self.col.undo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["css"], model["css"])

    def test_new_middle_sort_can_follow_a_separate_schema_commit(self):
        model = self.clone()
        note = self.add_note(model["id"])
        model["fields"].insert(1, {**model["fields"][0], "name": "New middle", "ord": None})
        model["sortField"] = 2
        with patch.object(self.col.models, "update_dict", wraps=self.col.models.update_dict) as native_update:
            self.rpc("notetypes.update", {"notetype": model})
        self.assertEqual(native_update.call_count, 1)
        model = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        self.assertEqual(model["sortField"], 2)
        model["sortField"] = 1
        with patch.object(self.col.models, "update_dict", wraps=self.col.models.update_dict) as native_update:
            self.rpc("notetypes.update", {"notetype": model})
        self.assertEqual(native_update.call_count, 1)
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "", "answer"])
        self.col.undo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["sortField"], 2)
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "", "answer"])
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.col.redo()
        self.col.redo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["sortField"], 1)

    def test_new_last_sort_field_commits_once_and_undo_redo_restores_values(self):
        model = self.clone()
        note = self.add_note(model["id"])
        model["fields"].append({**model["fields"][0], "name": "New last", "ord": None})
        model["sortField"] = 2
        with patch.object(self.col.models, "update_dict", wraps=self.col.models.update_dict) as native_update:
            self.rpc("notetypes.update", {"notetype": model})
        self.assertEqual(native_update.call_count, 1)
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["sortField"], 2)
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer", ""])
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.col.redo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["sortField"], 2)

    def test_new_middle_sort_without_a_retained_ordinal_collision_commits_once(self):
        model = self.clone()
        note = self.add_note(model["id"])
        model["fields"] = [model["fields"][0],
                           {**model["fields"][0], "name": "New middle", "ord": None},
                           {**model["fields"][0], "name": "New last", "ord": None}]
        model["templates"][0]["answerFormat"] = "{{Front}}"
        model["sortField"] = 1
        with patch.object(self.col.models, "update_dict", wraps=self.col.models.update_dict) as native_update:
            self.rpc("notetypes.update", {"notetype": model})
        self.assertEqual(native_update.call_count, 1)
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["sortField"], 1)
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "", ""])
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.col.redo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["sortField"], 1)

    def cancellation_fixture(self, kind):
        model = self.clone("Lifecycle " + kind)
        note = self.add_note(model["id"])
        card = self.col.get_card(note.card_ids()[0])
        card.reps = 41
        self.col.update_card(card)
        redo_model = copy.deepcopy(model)
        redo_model["css"] = ".card { color: teal; }"
        self.rpc("notetypes.update", {"notetype": redo_model})
        self.col.undo()
        self.assertTrue(self.col.undo_status().redo)
        if kind == "update":
            model["css"] = ".card { color: purple; }"
            return {"notetype": model}, "Updating note type", "update_dict"
        if kind == "remove":
            return {"notetypeId": model["id"], "deleteNotes": True}, "Removing note type", "remove"
        return {"noteIds": [note.id], "notetypeId": self.basic()["id"], "fieldMap": [1, 0],
                "templateMap": [0]}, "Converting note types", "change_notetype_of_notes"

    def assert_precommit_cancel_preserves_collection(self, kind):
        params, label, native_method = self.cancellation_fixture(kind)
        before = self.snapshot()
        reached, release = threading.Event(), threading.Event()
        native_report = self.engine.progress.report

        def hold_commit(done=0, total=None, label=""):
            native_report(done, total, label)
            if label == commit_label:
                reached.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("The test did not release the commit checkpoint.")

        commit_label = label
        with patch.object(self.engine.progress, "report", side_effect=hold_commit), \
                patch.object(self.col.models, native_method, wraps=getattr(self.col.models, native_method)) as commit:
            try:
                ack = self.request("notetypes." + kind, params)["result"]
                self.assertEqual(validate_value(self.engine.registry.catalogue["notetypes." + kind]["result"], ack), [])
                self.assertFalse(any(ack["changes"].values()))
                self.assertTrue(reached.wait(timeout=2), kind)
                self.assertIsNone(self.engine.progress.backend)
                operation_id = ack["operationId"]
                running = self.rpc("progress.get", {"operationId": operation_id})
                self.assertEqual((running["status"], running["kind"]), ("running", "notetypes." + kind))
                self.assertTrue(running["abortable"])
                self.assertEqual(self.request("notetypes.list")["error"]["code"], "busy")
                self.rpc("progress.abort", {"operationId": operation_id})
            finally:
                release.set()
                if self.engine.progress.thread is not None:
                    self.engine.progress.thread.join(timeout=5)
            self.assertEqual(commit.call_count, 0)
        terminal = self.rpc("progress.get", {"operationId": operation_id})
        self.assertEqual(terminal["status"], "aborted")
        self.assertEqual(terminal["error"]["code"], "aborted")
        self.assertIsNone(terminal["result"])
        self.assertEqual(self.snapshot(), before)

    def test_long_update_controls_and_precommit_abort_preserve_data_and_history(self):
        self.assert_precommit_cancel_preserves_collection("update")

    def test_long_remove_controls_and_precommit_abort_preserve_data_and_history(self):
        self.assert_precommit_cancel_preserves_collection("remove")

    def test_long_change_controls_and_precommit_abort_preserve_data_and_history(self):
        self.assert_precommit_cancel_preserves_collection("changeNotetype")

    def test_late_abort_keeps_completed_native_changes_and_undo_redo(self):
        for kind in ("update", "remove", "changeNotetype"):
            with self.subTest(kind=kind):
                params, _label, native_method = self.cancellation_fixture(kind)
                before = self.snapshot()[:-1]
                committed, release = threading.Event(), threading.Event()
                native_commit = getattr(self.col.models, native_method)

                def hold_after_commit(*args, **kwargs):
                    result = native_commit(*args, **kwargs)
                    committed.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("The test did not release the completed commit.")
                    return result

                with patch.object(self.col.models, native_method, side_effect=hold_after_commit):
                    try:
                        operation_id = self.request("notetypes." + kind, params)["result"]["operationId"]
                        self.assertTrue(committed.wait(timeout=2), kind)
                        abort = self.request("progress.abort", {"operationId": operation_id})["result"]
                        self.assertEqual(validate_value(self.engine.registry.catalogue["progress.abort"]["result"], abort), [])
                    finally:
                        release.set()
                        if self.engine.progress.thread is not None:
                            self.engine.progress.thread.join(timeout=5)
                terminal = self.rpc("progress.get", {"operationId": operation_id})
                self.assertEqual(terminal["status"], "completed", terminal)
                self.assertIsNone(terminal["error"])
                self.assertTrue(any(terminal["result"]["changes"].values()))
                if kind != "update":
                    self.assertEqual(terminal["result"]["count"], 1)
                after = self.snapshot()[:-1]
                self.assertNotEqual(after, before)
                self.col.undo()
                self.assertEqual(self.snapshot()[:-1], before)
                self.col.redo()
                self.assertEqual(self.snapshot()[:-1], after)

    def test_schema_abort_targets_scratch_and_detaches_before_close(self):
        params, _label, _native_method = self.cancellation_fixture("update")
        before = self.snapshot()
        reached, release = threading.Event(), threading.Event()
        created, detached = [], []
        native_collection = notetypes.Collection

        def disposable(path):
            scratch = native_collection(path)
            created.append(scratch)
            seed = scratch._backend.add_or_update_notetype
            close = scratch.close

            def hold_seed(*args, **kwargs):
                reached.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("The test did not release the disposable schema.")
                return seed(*args, **kwargs)

            def close_detached(*args, **kwargs):
                detached.append(self.engine.progress.backend is not scratch)
                return close(*args, **kwargs)

            scratch._backend.add_or_update_notetype = hold_seed
            scratch.close = close_detached
            return scratch

        with patch.object(notetypes, "Collection", side_effect=disposable), \
                patch.object(self.col, "set_wants_abort", wraps=self.col.set_wants_abort) as active_abort:
            try:
                operation_id = self.request("notetypes.update", params)["result"]["operationId"]
                self.assertTrue(reached.wait(timeout=2))
                self.assertIs(self.engine.progress.backend, created[0])
                with patch.object(created[0], "set_wants_abort", wraps=created[0].set_wants_abort) as scratch_abort:
                    self.rpc("progress.abort", {"operationId": operation_id})
                self.assertEqual(scratch_abort.call_count, 1)
            finally:
                release.set()
                if self.engine.progress.thread is not None:
                    self.engine.progress.thread.join(timeout=5)
            self.assertEqual(active_abort.call_count, 0)
        terminal = self.rpc("progress.get", {"operationId": operation_id})
        self.assertEqual(terminal["status"], "aborted", terminal)
        self.assertEqual(detached, [True])
        self.assertIs(self.engine.progress.backend, self.col)
        self.assertEqual(self.snapshot(), before)

    def test_consecutive_long_calls_preserve_the_previous_terminal_event(self):
        model = self.clone()
        model["css"] = ".card { color: teal; }"
        self.rpc("notetypes.update", {"notetype": model})
        first = self.engine.progress.current
        model["css"] = ".card { color: navy; }"
        self.rpc("notetypes.update", {"notetype": model})
        self.assertNotEqual(self.engine.progress.current, first)
        previous = [event for event in self.progress_events if event["operationId"] == first]
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0]["progress"]["status"], "completed")
        self.assertIsNotNone(previous[0]["progress"]["result"])

    def test_clone_complete_definition_and_undo(self):
        original = self.basic()
        result = self.rpc("notetypes.clone", {"notetypeId": original["id"], "name": "Exact copy"})
        clone = self.rpc("notetypes.get", {"notetypeId": result["notetypeId"]})
        for key in ("kind", "fields", "templates", "css", "sortField", "latexHeader", "latexFooter", "latexSvg"):
            self.assertEqual(clone[key], original[key], key)
        self.col.undo()
        self.assertIsNone(self.col.models.get(clone["id"]))
        self.col.redo()
        self.assertIsNotNone(self.col.models.get(clone["id"]))

    def test_update_field_rename_reorder_add_options_and_byte_preservation(self):
        model = self.clone()
        note = self.add_note(model["id"])
        edited = copy.deepcopy(model)
        back, front = edited["fields"][1], edited["fields"][0]
        front.update(name="Question", sticky=True, rtl=True, font="Inter", size=31,
                     plainText=True, collapsed=True, excludeFromSearch=True)
        extra = {**model["fields"][0], "name": "Source", "ord": None}
        edited["fields"] = [back, extra, front]
        edited["sortField"] = 2
        edited["templates"][0]["questionFormat"] = "{{Question}}"
        result = self.rpc("notetypes.update", {"notetype": edited})
        self.assertTrue(result["changes"]["notetype"])
        self.assertEqual(self.col.get_note(note.id).fields, ["answer", "", HTML])
        read = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        self.assertEqual(read["sortField"], 2)
        self.assertEqual([f["name"] for f in read["fields"]], ["Back", "Source", "Question"])
        self.assertEqual(read["fields"][2], {**front, "ord": 2})
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]}), model | {"noteCount": 1})
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id).fields, ["answer", "", HTML])

    def test_update_field_deletion_and_sort_preserves_retained_values(self):
        model = self.clone()
        note = self.add_note(model["id"])
        model["fields"] = [model["fields"][1]]
        model["templates"][0].update(questionFormat="{{Back}}", answerFormat="{{Back}}")
        self.rpc("notetypes.update", {"notetype": model})
        self.assertEqual(self.col.get_note(note.id).fields, ["answer"])
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id).fields, ["answer"])

    def test_distinct_native_unicode_field_and_template_names_remain_editable(self):
        model = self.clone()
        note = self.add_note(model["id"])
        model["fields"][0]["name"], model["fields"][1]["name"] = "ffi", "ﬃ"
        model["templates"][0].update(name="ffi", questionFormat="{{ffi}}", answerFormat="{{ﬃ}}")
        model["templates"].append({**model["templates"][0], "name": "ﬃ", "ord": None,
                                     "questionFormat": "{{ﬃ}}", "answerFormat": "{{ffi}}"})
        self.rpc("notetypes.update", {"notetype": model})
        read = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        self.assertEqual([field["name"] for field in read["fields"]], ["ffi", "ﬃ"])
        self.assertEqual([template["name"] for template in read["templates"]], ["ffi", "ﬃ"])
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        read["css"] = ".card { color: navy; }"
        self.rpc("notetypes.update", {"notetype": read})
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.col.undo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["css"], model["css"])
        self.col.redo()
        self.assertEqual(self.rpc("notetypes.get", {"notetypeId": model["id"]})["css"], ".card { color: navy; }")

    def test_update_templates_add_rename_reorder_delete_preserves_card_identity(self):
        model = self.clone()
        note = self.add_note(model["id"])
        original_card = note.card_ids()[0]
        model["templates"].append({**model["templates"][0], "ord": None, "name": "Reverse",
                                   "questionFormat": "{{Back}}", "answerFormat": "{{Front}}"})
        model["css"] = ".card { color: purple; }"
        model["latexHeader"], model["latexFooter"], model["latexSvg"] = "header", "footer", True
        self.rpc("notetypes.update", {"notetype": model})
        cards = self.col.get_note(note.id).cards()
        self.assertEqual(len(cards), 2)
        reverse_card = next(card.id for card in cards if card.ord == 1)
        model = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        model["templates"] = list(reversed(model["templates"]))
        model["templates"][0]["name"] = "Reversed renamed"
        self.rpc("notetypes.update", {"notetype": model})
        self.assertEqual(self.col.get_card(original_card).ord, 1)
        self.assertEqual(self.col.get_card(reverse_card).ord, 0)
        read = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        self.assertEqual((read["css"], read["latexHeader"], read["latexFooter"], read["latexSvg"]),
                         (".card { color: purple; }", "header", "footer", True))
        read["templates"] = [read["templates"][0]]
        self.rpc("notetypes.update", {"notetype": read})
        self.assertEqual(self.col.get_note(note.id).card_ids(), [reverse_card])
        self.col.undo()
        self.assertEqual(set(self.col.get_note(note.id).card_ids()), {original_card, reverse_card})
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id).card_ids(), [reverse_card])

    def test_rename_undo_redo_and_conflicts(self):
        model = self.clone()
        self.rpc("notetypes.rename", {"notetypeId": model["id"], "name": "Renamed"})
        self.assertEqual(self.col.models.get(model["id"])["name"], "Renamed")
        self.col.undo()
        self.assertEqual(self.col.models.get(model["id"])["name"], "Fixture")
        self.col.redo()
        self.assertEqual(self.col.models.get(model["id"])["name"], "Renamed")
        self.error("notetypes.rename", {"notetypeId": model["id"], "name": "basic"}, "conflict")
        for name in ("", " \t", "Bad\x00name", "\ud800", "\u0344" * 255):
            self.error("notetypes.rename", {"notetypeId": model["id"], "name": name}, "invalid_params")

    def test_imported_invalid_template_clone_and_rename_preserve_prior_undo(self):
        model = self.clone()
        broken = copy.deepcopy(self.col.models.get(model["id"]))
        broken["tmpls"][0]["qfmt"] = "{{Missing}}"
        # Reproduce a legacy imported schema; public writes retain full checks.
        self.col.models.update_dict(broken, skip_checks=True)
        self.error("notetypes.clone", {"notetypeId": model["id"], "name": "Invalid copy"}, "invalid_params")
        self.error("notetypes.rename", {"notetypeId": model["id"], "name": "Invalid renamed"}, "invalid_params")

    def test_remove_requires_delete_confirmation_and_is_undoable(self):
        model = self.clone()
        note = self.add_note(model["id"])
        self.error("notetypes.remove", {"notetypeId": model["id"], "deleteNotes": False}, "conflict")
        result = self.rpc("notetypes.remove", {"notetypeId": model["id"], "deleteNotes": True})
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["changes"]["note"])
        self.assertIsNone(self.col.models.get(model["id"]))
        self.assertEqual(self.col.db.scalar("select count() from notes where id = ?", note.id), 0)
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.col.redo()
        self.assertIsNone(self.col.models.get(model["id"]))

    def test_remove_unused_notetype_without_deleting_notes(self):
        model = self.clone()
        self.assertEqual(self.rpc("notetypes.remove", {"notetypeId": model["id"], "deleteNotes": False})["count"], 0)

    def test_change_notetype_destination_mapping_preserves_html_card_history_and_source(self):
        source = self.clone("Source type")
        source["fields"].append({**source["fields"][0], "ord": None, "name": "Source"})
        self.rpc("notetypes.update", {"notetype": source})
        source = self.rpc("notetypes.get", {"notetypeId": source["id"]})
        target = self.rpc("notetypes.clone", {"notetypeId": source["id"], "name": "Target type"})["notetypeId"]
        note = self.add_note(source["id"], [HTML, "answer", "canonnotes:page/example?rev=abc"])
        card = note.cards()[0]
        card.reps, card.ivl = 7, 13
        self.col.update_card(card)
        result = self.rpc("notetypes.changeNotetype", {"noteIds": [note.id, note.id], "notetypeId": target,
                                                     "fieldMap": [1, 0, 2], "templateMap": [0]})
        self.assertEqual(result["count"], 1)
        converted = self.col.get_note(note.id)
        self.assertEqual(converted.mid, target)
        self.assertEqual(converted.fields, ["answer", HTML, "canonnotes:page/example?rev=abc"])
        self.assertEqual(converted.card_ids(), [card.id])
        self.assertEqual((self.col.get_card(card.id).reps, self.col.get_card(card.id).ivl), (7, 13))
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer", "canonnotes:page/example?rev=abc"])
        self.assertEqual(self.col.get_note(note.id).mid, source["id"])
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id).mid, target)

    def test_change_notetype_cloze_and_explicit_dropped_fields(self):
        note = self.add_note(self.basic()["id"], ["{{c1::one}} {{c2::two}}", "extra"])
        cloze = self.col.models.by_name("Cloze")["id"]
        self.rpc("notetypes.changeNotetype", {"noteIds": [note.id], "notetypeId": cloze,
                                             "fieldMap": [0, -1], "templateMap": []})
        self.assertEqual(self.col.get_note(note.id).fields, ["{{c1::one}} {{c2::two}}", ""])
        self.assertEqual(len(self.col.get_note(note.id).card_ids()), 2)
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, ["{{c1::one}} {{c2::two}}", "extra"])
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id).mid, cloze)

    def test_change_notetype_remaps_templates_and_creates_fresh_card_history(self):
        source_id = self.col.models.by_name("Basic (and reversed card)")["id"]
        target = self.rpc("notetypes.stock", {"kind": "basicReversed"})["notetypes"][0]
        target["name"] = "Three templates"
        target["templates"].append({**target["templates"][0], "name": "Combined", "ord": None,
                                     "questionFormat": "{{Front}} / {{Back}}"})
        target_id = self.rpc("notetypes.add", {"notetype": target})["notetypeId"]
        note = self.add_note(source_id)
        old_cards = note.cards()
        old_cards[0].reps, old_cards[1].reps = 13, 29
        self.col.update_cards(old_cards)
        params = {"noteIds": [note.id], "notetypeId": target_id, "fieldMap": [0, 1], "templateMap": [1, 0, -1]}
        self.error("notetypes.changeNotetype", params | {"templateMap": [0, 0, -1]}, "invalid_params")
        self.rpc("notetypes.changeNotetype", params)
        cards = {card.ord: card for card in self.col.get_note(note.id).cards()}
        self.assertEqual((cards[0].id, cards[0].reps), (old_cards[1].id, 29))
        self.assertEqual((cards[1].id, cards[1].reps), (old_cards[0].id, 13))
        self.assertNotIn(cards[2].id, [card.id for card in old_cards])
        self.assertEqual(cards[2].reps, 0)
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).mid, source_id)
        self.assertEqual([(card.id, card.ord, card.reps) for card in self.col.get_note(note.id).cards()],
                         [(old_cards[0].id, 0, 13), (old_cards[1].id, 1, 29)])
        self.col.redo()
        self.assertEqual(len(self.col.get_note(note.id).card_ids()), 3)

    def test_change_notetype_rejects_bad_maps_mixed_sources_and_missing_selection(self):
        source = self.basic()
        target = self.clone()["id"]
        note = self.add_note(source["id"])
        other = self.add_note(target)
        base = {"noteIds": [note.id], "notetypeId": target, "fieldMap": [0, 1], "templateMap": [0]}
        for override in ({"noteIds": []}, {"fieldMap": [0]}, {"fieldMap": [0, 0]},
                         {"fieldMap": [0, 2]}, {"templateMap": []}, {"templateMap": [1]},
                         {"noteIds": [note.id, other.id]}):
            self.error("notetypes.changeNotetype", base | override, "invalid_params")
        self.error("notetypes.changeNotetype", base | {"noteIds": [note.id, 9007199254740991]}, "not_found")
        self.error("notetypes.changeNotetype", base | {"notetypeId": 9007199254740991}, "not_found")

    def test_preview_unsaved_fields_templates_css_without_collection_or_undo_changes(self):
        model = self.basic()
        note = self.add_note(model["id"])
        card = note.cards()[0]
        edited = copy.deepcopy(model)
        edited["fields"][0]["name"] = "Edited front"
        edited["templates"][0]["questionFormat"] = '<main>{{Edited front}}</main>[sound:sample.mp3]'
        edited["templates"][0]["answerFormat"] = "{{FrontSide}}<aside>{{Back}}</aside>"
        edited["css"] = ".card { font-size: 27px; }"
        before = self.snapshot()
        rendered = self.rpc("notetypes.previewTemplate", {"notetype": edited, "fields": [HTML, "answer"],
                                                         "templateOrdinal": 0, "cardId": card.id})
        self.assertIn("<main>" + HTML + "</main>", rendered["questionHtml"])
        self.assertIn("<aside>answer</aside>", rendered["answerHtml"])
        self.assertEqual(rendered["css"], edited["css"])
        self.assertEqual(rendered["cardId"], card.id)
        self.assertEqual(rendered["noteId"], note.id)
        self.assertEqual(rendered["av"]["question"][0]["filename"], "sample.mp3")
        self.assertEqual(self.snapshot(), before)

    def test_preview_stock_and_cloze_and_typein_are_native(self):
        for kind, fields in (("basic", ["front", "back"]), ("cloze", ["{{c1::one}} {{c2::two}}", "extra"]),
                             ("typeIn", ["front", "back"])):
            model = self.rpc("notetypes.stock", {"kind": kind})["notetypes"][0]
            rendered = self.rpc("notetypes.previewTemplate", {"notetype": model, "fields": fields,
                                                             "templateOrdinal": 1 if kind == "cloze" else 0})
            if kind == "cloze":
                self.assertIn('class="cloze"', rendered["questionHtml"])
                self.assertIn("two", rendered["answerHtml"])
            elif kind == "typeIn":
                self.assertIn('id="typeans"', rendered["questionHtml"])
                self.assertEqual(rendered["typeAnswer"]["correct"], "back")
            else:
                self.assertIn("front", rendered["questionHtml"])
                self.assertIn("back", rendered["answerHtml"])

    def test_preview_schema_edits_preserve_real_card_context_and_source_bytes(self):
        model = self.clone("Context type")
        native = self.col.models.get(model["id"])
        context = " | {{Deck}} | {{Subdeck}} | {{Card}} | {{CardFlag}} | {{CardID}} | {{Tags}} | {{Type}}"
        native["tmpls"][0]["qfmt"] = "{{Front}}" + context
        native["tmpls"][0]["afmt"] = "{{FrontSide}}<hr>{{Back}}"
        self.col.models.update_dict(native)
        deck_id = self.col.decks.id("Synthetic::Named deck")
        note = self.col.new_note(native)
        note.fields, note.tags = [HTML, "answer"], ["marked", "synthetic-tag"]
        self.col.add_note(note, deck_id)
        card = note.cards()[0]
        card.flags, card.reps = 4, 67
        self.col.update_card(card)
        model = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        override_id = self.col.decks.id("Override deck")
        # Retain both undo directions while all previews remain read-only.
        redo = copy.deepcopy(model)
        redo["css"] = ".card { color: navy; }"
        self.rpc("notetypes.update", {"notetype": redo})
        self.col.undo()
        before = self.preview_snapshot()
        params = {"notetype": model, "cardId": card.id, "noteId": note.id, "templateOrdinal": 0}
        baseline = self.rpc("notetypes.previewTemplate", params)
        expected = (HTML + f" | Synthetic::Named deck | Named deck | Card 1 | flag4 | {card.id}"
                    " | marked synthetic-tag | Context type")
        self.assertEqual(baseline["questionHtml"], expected)
        edited = copy.deepcopy(model)
        edited["fields"][0]["name"] = "Renamed front"
        edited["templates"][0]["questionFormat"] = "{{Renamed front}}" + context
        for supplied in ({"cardId": card.id, "noteId": note.id}, {"noteId": note.id}):
            result = self.rpc("notetypes.previewTemplate", {
                "notetype": edited, "templateOrdinal": 0, **supplied})
            self.assertEqual(result["questionHtml"], expected)
            self.assertEqual(result["answerHtml"], baseline["answerHtml"])
            self.assertEqual((result["cardId"], result["noteId"], result["deckId"], result["flags"]),
                             (card.id, note.id, deck_id, 4))
            self.assertTrue(result["marked"])
            self.assertEqual(self.preview_snapshot(), before)
        edited["templates"][0]["name"] = "Unsaved card"
        edited["templates"][0]["deckId"] = override_id
        # An override controls future generation, while the sample retains its deck.
        before_override = self.preview_snapshot()
        result = self.rpc("notetypes.previewTemplate", params | {"notetype": edited})
        self.assertEqual(result["questionHtml"], expected.replace(" | Card 1 | ", " | Unsaved card | "))
        self.assertEqual(result["deckId"], deck_id)
        self.assertEqual(self.preview_snapshot(), before_override)
        self.assertEqual(self.col.get_note(note.id).fields, [HTML, "answer"])
        self.assertEqual(self.col.get_card(card.id).reps, 67)
        self.assertTrue(self.col.undo_status().undo)
        self.assertTrue(self.col.undo_status().redo)
        self.col.redo()
        self.assertEqual(self.col.models.get(model["id"])["css"], redo["css"])

    def test_preview_preserves_original_deck_and_user_special_field_names(self):
        model = self.clone("Filtered context")
        native = self.col.models.get(model["id"])
        native["tmpls"][0]["qfmt"] = "{{Front}} | {{Deck}} | {{Subdeck}} | {{CardFlag}} | {{CardID}}"
        self.col.models.update_dict(native)
        note = self.add_note(model["id"])
        original_deck = self.col.decks.id("Original::Child")
        filtered = self.col.decks.new_deck_legacy(True)
        filtered["name"] = "Filtered sample"
        filtered_id = self.col.decks.add_deck_legacy(filtered).id
        card = note.cards()[0]
        card.did, card.odid, card.flags = filtered_id, original_deck, 3
        self.col.update_card(card)
        model = self.rpc("notetypes.get", {"notetypeId": model["id"]})
        model["fields"][0]["name"] = "Deck"
        model["templates"][0]["questionFormat"] = "{{Deck}} | {{Subdeck}} | {{CardFlag}} | {{CardID}}"
        before = self.preview_snapshot()
        result = self.rpc("notetypes.previewTemplate", {
            "notetype": model, "cardId": card.id, "templateOrdinal": 0})
        self.assertEqual(result["questionHtml"], HTML + f" | Child | flag3 | {card.id}")
        self.assertEqual(result["deckId"], filtered_id)
        self.assertEqual(self.preview_snapshot(), before)

    def test_preview_matches_retained_template_identity_and_rejects_mismatched_cards(self):
        added = self.rpc("notetypes.add", {"stockKind": "basicReversed", "name": "Two samples"})
        note = self.add_note(added["notetypeId"])
        cards = sorted(note.cards(), key=lambda card: card.ord)
        second_deck = self.col.decks.id("Second::Deck")
        cards[1].did, cards[1].flags = second_deck, 5
        self.col.update_card(cards[1])
        model = self.rpc("notetypes.get", {"notetypeId": added["notetypeId"]})
        model["templates"].reverse()
        model["fields"][1]["name"] = "Renamed back"
        model["templates"][0]["questionFormat"] = "{{Renamed back}} | {{Deck}} | {{Card}} | {{CardFlag}} | {{CardID}}"
        model["templates"][1]["answerFormat"] = "{{Renamed back}}"
        before = self.preview_snapshot()
        for supplied in ({"cardId": cards[1].id}, {"noteId": note.id}):
            result = self.rpc("notetypes.previewTemplate", {
                "notetype": model, "templateOrdinal": 0, **supplied})
            self.assertEqual(result["questionHtml"], f"answer | Second::Deck | Card 2 | flag5 | {cards[1].id}")
            self.assertEqual(result["cardId"], cards[1].id)
            self.assertEqual(result["deckId"], second_deck)
        with patch.object(notetypes, "Collection", side_effect=AssertionError("scratch must not open")):
            frame = self.frame("notetypes.previewTemplate", {
                "notetype": model, "templateOrdinal": 0, "cardId": cards[0].id})
        self.assertEqual(frame.get("error", {}).get("code"), "invalid_params", frame)
        self.assertEqual(self.preview_snapshot(), before)
        # A new template has no matching retained card; note-only falls back to
        # the native uncommitted sample, rather than borrowing another card.
        new_template = copy.deepcopy(model["templates"][0])
        new_template["ord"], new_template["name"] = None, "New card"
        model["templates"].append(new_template)
        result = self.rpc("notetypes.previewTemplate", {
            "notetype": model, "templateOrdinal": 2, "noteId": note.id})
        self.assertEqual(result["questionHtml"], "answer | (Deck) | New card | flag0 | 0")
        self.assertEqual((result["cardId"], result["deckId"]), (0, 1))
        with patch.object(notetypes, "Collection", side_effect=AssertionError("scratch must not open")):
            frame = self.frame("notetypes.previewTemplate", {
                "notetype": model, "templateOrdinal": 2, "cardId": cards[1].id})
        self.assertEqual(frame.get("error", {}).get("code"), "invalid_params", frame)
        self.assertEqual(self.preview_snapshot(), before)

    def test_preview_cloze_card_identity_must_match_requested_ordinal(self):
        added = self.rpc("notetypes.add", {"stockKind": "cloze", "name": "Cloze samples"})
        note = self.add_note(added["notetypeId"], ["{{c1::one}} {{c2::two}}", "extra"])
        cards = sorted(note.cards(), key=lambda card: card.ord)
        cards[1].flags = 2
        self.col.update_card(cards[1])
        model = self.rpc("notetypes.get", {"notetypeId": added["notetypeId"]})
        model["fields"][0]["name"] = "Renamed text"
        model["templates"][0]["questionFormat"] = "{{cloze:Renamed text}} | {{CardFlag}} | {{CardID}}"
        model["templates"][0]["answerFormat"] = "{{cloze:Renamed text}}"
        before = self.preview_snapshot()
        result = self.rpc("notetypes.previewTemplate", {
            "notetype": model, "templateOrdinal": 1, "cardId": cards[1].id})
        self.assertIn('class="cloze"', result["questionHtml"])
        self.assertIn(f" | flag2 | {cards[1].id}", result["questionHtml"])
        self.assertIn("two", result["answerHtml"])
        with patch.object(notetypes, "Collection", side_effect=AssertionError("scratch must not open")):
            frame = self.frame("notetypes.previewTemplate", {
                "notetype": model, "templateOrdinal": 1, "cardId": cards[0].id})
        self.assertEqual(frame.get("error", {}).get("code"), "invalid_params", frame)
        self.assertEqual(self.preview_snapshot(), before)

    def test_preview_scratch_cleanup_on_schema_mismatch_and_render_error(self):
        model = self.basic()
        note = self.add_note(model["id"])
        model["fields"][0]["name"] = "Renamed front"
        model["templates"][0]["questionFormat"] = "{{Renamed front}}"
        before = self.preview_snapshot()
        native_collection = notetypes.Collection
        for mismatch in (True, False):
            opened, closed = [], []

            def create(path):
                scratch = native_collection(path)
                opened.append(path)
                native_close, native_all = scratch.close, scratch.db.all

                def close():
                    closed.append(path)
                    return native_close()

                def query(sql, *args, **kwargs):
                    rows = native_all(sql, *args, **kwargs)
                    if mismatch and "from pragma_table_info(" in sql:
                        rows.append(["unexpected", "TEXT", 1, None, 0])
                    return rows

                scratch.close, scratch.db.all = close, query
                return scratch

            with patch.object(notetypes, "Collection", side_effect=create):
                if mismatch:
                    frame = self.frame("notetypes.previewTemplate", {
                        "notetype": model, "templateOrdinal": 0, "cardId": note.card_ids()[0]})
                else:
                    with patch.object(notetypes, "_render", side_effect=notetypes.EngineError("invalid_params", "Invalid sample.")):
                        frame = self.frame("notetypes.previewTemplate", {
                            "notetype": model, "templateOrdinal": 0, "cardId": note.card_ids()[0]})
            self.assertEqual(frame.get("error", {}).get("code"), "unsupported" if mismatch else "invalid_params", frame)
            self.assertEqual(len(opened), 1)
            self.assertEqual(closed, opened)
            self.assertFalse(Path(opened[0]).parent.exists())
            self.assertEqual(self.preview_snapshot(), before)

    def test_preview_cloze_ordinal_matches_native_generation_limit(self):
        model = self.rpc("notetypes.stock", {"kind": "cloze"})["notetypes"][0]
        params = {"notetype": model, "fields": ["{{c500::upper boundary}}", ""], "templateOrdinal": 499}
        result = self.rpc("notetypes.previewTemplate", params)
        self.assertIn('class="cloze"', result["questionHtml"])
        self.assertIn("upper boundary", result["answerHtml"])
        self.error("notetypes.previewTemplate", params | {"templateOrdinal": 500}, "invalid_params")

    def test_preview_uses_native_samples_or_given_note_when_fields_are_omitted(self):
        model = self.basic()
        sample = self.rpc("notetypes.previewTemplate", {"notetype": model, "templateOrdinal": 0})
        self.assertIn("(Front)", sample["questionHtml"])
        note = self.add_note(model["id"])
        before = self.snapshot()
        rendered = self.rpc("notetypes.previewTemplate", {"notetype": model, "noteId": note.id,
                                                         "templateOrdinal": 0})
        self.assertIn(HTML, rendered["questionHtml"])
        self.assertEqual(rendered["noteId"], note.id)
        self.assertEqual(self.snapshot(), before)

    def test_preview_latex_generates_a_reference_without_running_external_tools(self):
        model = self.basic()
        before = self.snapshot()
        result = self.rpc("notetypes.previewTemplate", {"notetype": model, "fields": ["[latex]x[/latex]", "back"],
                                                       "templateOrdinal": 0})
        self.assertIn("latex-", result["questionHtml"])
        self.assertIn(".png", result["questionHtml"])
        self.assertNotIn("Error", result["questionHtml"])
        self.assertEqual(self.snapshot(), before)

    def test_preview_rejects_bad_field_counts_template_and_card(self):
        model = self.basic()
        base = {"notetype": model, "fields": ["front", "back"], "templateOrdinal": 0}
        for override in ({"fields": []}, {"templateOrdinal": 1}, {"fields": ["\ud800", "back"]}):
            self.error("notetypes.previewTemplate", base | override, "invalid_params")
        self.error("notetypes.previewTemplate", base | {"cardId": 9007199254740991}, "not_found")
        invalid = copy.deepcopy(model)
        invalid["templates"][0]["questionFormat"] = "{{Missing}}"
        self.error("notetypes.previewTemplate", base | {"notetype": invalid}, "invalid_params")

    def test_update_rejects_invalid_schema_without_partial_mutation(self):
        model = self.clone()
        for change in (lambda m: m.update(fields=[]), lambda m: m.update(templates=[]),
                       lambda m: m.update(sortField=2), lambda m: m.update(kind="cloze"),
                       lambda m: m["fields"][1].update(name="Front"),
                       lambda m: m["fields"][1].update(ord=0),
                       lambda m: m["fields"][1].update(ord=999),
                       lambda m: m["fields"][0].update(name="Bad:name"),
                       lambda m: m["templates"][0].update(deckId=9007199254740991),
                       lambda m: m["templates"][0].update(questionFormat="{{Missing}}"),
                       lambda m: m.update(css="\ud800")):
            invalid = copy.deepcopy(model)
            change(invalid)
            self.error("notetypes.update", {"notetype": invalid}, "invalid_params")
        self.error("notetypes.add", {"notetype": model}, "conflict")
        self.error("notetypes.clone", {"notetypeId": model["id"], "name": "Basic"}, "conflict")

    def test_every_method_rejects_extra_parameters_and_missing_ids(self):
        for name in sorted(self.engine.registry.catalogue):
            if name.startswith("notetypes."):
                self.error(name, {"unexpected": True}, "invalid_params")
        missing = 9007199254740991
        for name, params in (("get", {"notetypeId": missing}), ("clone", {"notetypeId": missing, "name": "Copy"}),
                             ("rename", {"notetypeId": missing, "name": "Name"}),
                             ("remove", {"notetypeId": missing, "deleteNotes": True})):
            self.error("notetypes." + name, params, "not_found")
        invalid = self.basic() | {"id": missing}
        self.error("notetypes.update", {"notetype": invalid}, "not_found")


class NotetypeProcessTests(unittest.TestCase):
    def test_notetype_edits_survive_restart(self):
        with tempfile.TemporaryDirectory(prefix="memr-e03-notetype-persist-") as directory:
            with EnginePipe(directory) as engine:
                stock = engine.call("notetypes.stock", {"kind": "basic"})["result"]["notetypes"][0]
                stock["name"] = "Persistent model"
                model_id = engine.call("notetypes.add", {"notetype": stock})["result"]["notetypeId"]
                self.assertIn("result", engine.call("notetypes.rename", {"notetypeId": model_id, "name": "Persistent renamed"}))
            with EnginePipe(directory) as engine:
                model = engine.call("notetypes.get", {"notetypeId": model_id})["result"]
                self.assertEqual(model["name"], "Persistent renamed")
                self.assertEqual([field["name"] for field in model["fields"]], ["Front", "Back"])
