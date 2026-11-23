"""Tag trees, exact/subtree semantics and native registry undo."""
from contextlib import ExitStack
import threading
from unittest.mock import patch

from tests.test_notes import HTML, NotesCase
from anki.collection import AddNoteRequest
from memr_srs_engine.validate import validate_value


class TagTests(NotesCase):
    def snapshot(self):
        return (self.col.db.all("select * from notes order by id"),
                self.col.db.all("select * from cards order by id"),
                self.col.db.all("select * from tags order by tag"),
                str(self.col.undo_status()))

    def test_bulk_tag_contracts_acknowledge_operations_and_keep_mutation_flags(self):
        for kind in ("list", "rename", "reparent", "remove", "clearUnused"):
            entry = self.engine.registry.catalogue["tags." + kind]
            self.assertTrue(entry["longOperation"], kind)
            self.assertEqual(entry["mutating"], kind != "list")
            self.assertEqual(set(entry["result"]["properties"]), {"operationId", "changes", "undo"})
        self.assertFalse(self.engine.registry.catalogue["tags.setCollapsed"]["longOperation"])

    def test_tags_registry_and_native_tree_counts(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("tags.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)
        self.add(tags=["Parent", "Parent::Child", "Parent::Other"])
        self.add(["Other", "A"], tags=["Parent::Child"])
        self.col.tags.set_collapsed("Unused", True)
        result = self.rpc("tags.list")["tags"]
        self.assertEqual([(t["name"], t["parent"], t["level"], t["noteCount"]) for t in result], [
            ("Parent", None, 0, 2), ("Parent::Child", "Parent", 1, 2),
            ("Parent::Other", "Parent", 1, 1), ("Unused", None, 0, 0)])
        self.assertEqual(len(self.rpc("tags.list", {"prefix": "parent::c"})["tags"]), 1)

    def test_subtree_rename_native_merges_and_undo_redo(self):
        nid = self.add(tags=["Old", "Old::Child", "Target"])["noteId"]
        self.col.tags.rename("old", "Target")
        native_tags = self.col.get_note(nid).tags
        self.col.undo()
        result = self.rpc("tags.rename", {"name": "old", "newName": "Target", "includeChildren": True})
        self.assertEqual(result["count"], 1)
        # Pinned prefix rename retains duplicate tag entries on collision.
        self.assertEqual(self.col.get_note(nid).tags, native_tags)
        self.assertNotIn("Old", self.col.tags.all())
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, ["Old", "Old::Child", "Target"])
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).tags, native_tags)

    def test_exact_rename_preserves_descendants_and_literal_dollar(self):
        nid = self.add(tags=["Old", "Old::Child"])["noteId"]
        result = self.rpc("tags.rename", {"name": "Old", "newName": "New$1", "includeChildren": False})
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_note(nid).tags, ["New$1", "Old::Child"])
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, ["Old", "Old::Child"])
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).tags, ["New$1", "Old::Child"])

    def test_reparent_subtree_top_level_cycles_and_undo_redo(self):
        nid = self.add(tags=["Old::Child::Leaf"])["noteId"]
        self.rpc("tags.reparent", {"names": ["Old::Child"], "parent": "New"})
        self.assertEqual(self.col.get_note(nid).tags, ["New::Child::Leaf"])
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, ["Old::Child::Leaf"])
        self.col.redo()
        self.rpc("tags.reparent", {"names": ["New::Child"], "parent": ""})
        self.assertEqual(self.col.get_note(nid).tags, ["Child::Leaf"])
        history = self.col.undo_status()
        self.error("tags.reparent", {"names": ["Child"], "parent": "Child::Leaf"}, "invalid_params")
        self.assertEqual(self.col.undo_status(), history)

    def test_remove_subtree_and_exact_selection_preserve_unselected_tags(self):
        nid = self.add(tags=["Root", "Root::Keep", "Leaf", "Other"])["noteId"]
        with patch.object(self.col.tags, "find_and_replace", wraps=self.col.tags.find_and_replace) as exact:
            result = self.rpc("tags.remove", {"names": ["Root"], "includeChildren": False})
        self.assertEqual(exact.call_count, 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_note(nid).tags, ["Leaf", "Other", "Root::Keep"])
        with patch.object(self.col.tags, "remove", wraps=self.col.tags.remove) as subtree:
            result = self.rpc("tags.remove", {"names": ["Leaf"], "includeChildren": False})
        self.assertEqual(subtree.call_count, 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_note(nid).tags, ["Other", "Root::Keep"])
        self.assertNotIn("Leaf", self.col.tags.all())
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, ["Leaf", "Other", "Root::Keep"])
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, ["Leaf", "Other", "Root", "Root::Keep"])
        self.col.redo()
        self.col.redo()
        result = self.rpc("tags.remove", {"names": ["Root"], "includeChildren": True})
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_note(nid).tags, ["Other"])
        self.assertFalse(any(tag.startswith("Root") for tag in self.col.tags.all()))
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, ["Other", "Root::Keep"])

    def test_mixed_exact_removal_rejects_before_writes_and_preserves_registry_and_history(self):
        nid = self.add([HTML, "Answer"], tags=["Root", "Root::Keep", "Leaf", "Other"])["noteId"]
        card = self.col.get_note(nid).cards()[0]
        card.reps = 53
        self.col.update_card(card)
        self.col.tags.rename("Other", "Redo")
        self.col.undo()
        self.assertTrue(self.col.undo_status().undo)
        self.assertTrue(self.col.undo_status().redo)
        before = self.snapshot()

        def reject_registry_removal(_names):
            # This real native rejection exposed the old two-transaction path:
            # note tags were already removed, and native history was cleared.
            return self.col.tags.find_and_replace([], "[", "", True, False)

        with patch.object(self.col.tags, "find_and_replace", wraps=self.col.tags.find_and_replace) as exact, \
                patch.object(self.col.tags, "remove", side_effect=reject_registry_removal) as registry:
            frame = self.frame("tags.remove", {"names": ["Root", "Leaf"], "includeChildren": False})
        self.assertEqual((exact.call_count, registry.call_count), (0, 0))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(frame.get("error", {}).get("code"), "unsupported", frame)
        self.assertIn("not jointly atomic", frame["error"]["message"])
        self.assertEqual(self.col.get_note(nid).fields, [HTML, "Answer"])
        self.assertEqual(self.col.get_card(card.id).reps, 53)
        self.col.redo()
        self.assertIn("Redo", self.col.get_note(nid).tags)
        self.col.undo()
        self.assertIn("Other", self.col.get_note(nid).tags)

    def test_exact_selected_subtree_and_virtual_parent_noop_use_one_native_transaction(self):
        nid = self.add(tags=["Root::Drop", "Root::Drop::Leaf", "Root::Keep"])["noteId"]
        before = self.snapshot()[:-1]
        result = self.rpc("tags.remove", {"names": ["Root"], "includeChildren": False})
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.snapshot()[:-1], before)
        with patch.object(self.col.tags, "remove", wraps=self.col.tags.remove) as native:
            result = self.rpc("tags.remove", {"names": ["Root::Drop", "Root::Drop::Leaf"], "includeChildren": False})
        self.assertEqual(native.call_count, 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_note(nid).tags, ["Root::Keep"])
        self.col.undo()
        self.assertEqual(self.snapshot()[:-1], before)
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).tags, ["Root::Keep"])

    def test_list_counts_each_note_once_per_ancestor_in_bounded_native_batches(self):
        model = self.col.models.get(self.basic)
        requests = []
        for index in range(513):
            note = self.col.new_note(model)
            note.fields = [f"Generated {index}", "Answer"]
            note.tags = ["Root", "Root::One", "Root::Two", "ffi", "ﬃ"]
            requests.append(AddNoteRequest(note=note, deck_id=1))
        self.col.add_notes(requests)
        before = self.snapshot()
        generation = self.engine.collection_generation
        native_all = self.col.db.all
        sizes = []

        def bounded_scan(sql, *args):
            rows = native_all(sql, *args)
            if "from notes" in sql.lower() and "tags" in sql.lower():
                sizes.append(len(rows))
            return rows

        with patch.object(self.col.db, "all", side_effect=bounded_scan):
            result = self.rpc("tags.list")
        self.assertGreaterEqual(len(sizes), 2)
        self.assertLessEqual(max(sizes), 512)
        self.assertEqual({tag["name"]: tag["noteCount"] for tag in result["tags"]},
                         {"Root": 513, "Root::One": 513, "Root::Two": 513, "ffi": 513, "ﬃ": 513})
        self.assertFalse(any(result["changes"].values()))
        self.assertEqual(self.engine.collection_generation, generation)
        self.assertEqual(self.snapshot(), before)

    def lifecycle_fixture(self, kind):
        self.add([HTML + "<p>" + kind + "</p>", "Answer"],
                 tags=["Root", "Root::Keep", "Leaf", "Other"], allowDuplicate=True)
        if kind == "clearUnused":
            self.col.tags.set_collapsed("Unused", False)
        self.col.tags.rename("Other", "Redo")
        self.col.undo()
        self.assertTrue(self.col.undo_status().redo)
        return {
            "list": ({}, "Counting tag notes", None),
            "rename": ({"name": "Root", "newName": "Renamed", "includeChildren": True}, "Renaming tags", "rename"),
            "reparent": ({"names": ["Root"], "parent": "Moved"}, "Reparenting tags", "reparent"),
            "remove": ({"names": ["Leaf"], "includeChildren": False}, "Removing tags", "remove"),
            "clearUnused": ({}, "Clearing unused tags", "clear_unused_tags"),
        }[kind]

    def assert_controls_and_cancel_before_commit(self, kind):
        params, stage, _native_method = self.lifecycle_fixture(kind)
        before = self.snapshot()
        reached, release = threading.Event(), threading.Event()
        report = self.engine.progress.report

        def hold_stage(done=0, total=None, label=""):
            report(done, total, label)
            if label == stage:
                reached.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("The test did not release the tag checkpoint.")

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.engine.progress, "report", side_effect=hold_stage))
            commits = [stack.enter_context(patch.object(self.col.tags, name, wraps=getattr(self.col.tags, name)))
                       for name in ("rename", "reparent", "remove", "find_and_replace", "clear_unused_tags")]
            try:
                ack = self.dispatch_frame("tags." + kind, params)["result"]
                self.assertEqual(validate_value(self.engine.registry.catalogue["tags." + kind]["result"], ack), [])
                self.assertFalse(any(ack["changes"].values()))
                self.assertTrue(reached.wait(timeout=2), kind)
                self.assertIsNone(self.engine.progress.backend)
                operation_id = ack["operationId"]
                running = self.dispatch_frame("progress.get", {"operationId": operation_id})["result"]
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], running), [])
                self.assertEqual((running["status"], running["kind"]), ("running", "tags." + kind))
                self.assertEqual(self.dispatch_frame("notes.get", {"noteId": 0})["error"]["code"], "busy")
                abort = self.dispatch_frame("progress.abort", {"operationId": operation_id})["result"]
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.abort"]["result"], abort), [])
            finally:
                release.set()
                if self.engine.progress.thread is not None:
                    self.engine.progress.thread.join(timeout=5)
            self.assertTrue(all(commit.call_count == 0 for commit in commits))
        terminal = self.dispatch_frame("progress.get", {"operationId": operation_id})["result"]
        self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], terminal), [])
        self.assertEqual(terminal["status"], "aborted", terminal)
        self.assertEqual(terminal["error"]["code"], "aborted")
        self.assertIsNone(terminal["result"])
        self.assertEqual(self.snapshot(), before)

    def test_list_controls_and_abort_preserve_data_and_history(self):
        self.assert_controls_and_cancel_before_commit("list")

    def test_rename_controls_and_abort_preserve_data_and_history(self):
        self.assert_controls_and_cancel_before_commit("rename")

    def test_reparent_controls_and_abort_preserve_data_and_history(self):
        self.assert_controls_and_cancel_before_commit("reparent")

    def test_remove_controls_and_abort_preserve_data_and_history(self):
        self.assert_controls_and_cancel_before_commit("remove")

    def test_clear_unused_controls_and_abort_preserve_data_and_history(self):
        self.assert_controls_and_cancel_before_commit("clearUnused")

    def test_late_abort_keeps_each_committed_tag_result_and_undo_redo(self):
        for kind in ("rename", "reparent", "remove", "clearUnused"):
            with self.subTest(kind=kind):
                params, _stage, native_method = self.lifecycle_fixture(kind)
                before = self.snapshot()[:-1]
                committed, release = threading.Event(), threading.Event()
                native_commit = getattr(self.col.tags, native_method)

                def hold_completed(*args, **kwargs):
                    result = native_commit(*args, **kwargs)
                    committed.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("The test did not release the committed tag operation.")
                    return result

                with patch.object(self.col.tags, native_method, side_effect=hold_completed) as commit:
                    try:
                        operation_id = self.dispatch_frame("tags." + kind, params)["result"]["operationId"]
                        self.assertTrue(committed.wait(timeout=2), kind)
                        abort = self.dispatch_frame("progress.abort", {"operationId": operation_id})["result"]
                        self.assertEqual(validate_value(self.engine.registry.catalogue["progress.abort"]["result"], abort), [])
                    finally:
                        release.set()
                        if self.engine.progress.thread is not None:
                            self.engine.progress.thread.join(timeout=5)
                    self.assertEqual(commit.call_count, 1)
                terminal = self.dispatch_frame("progress.get", {"operationId": operation_id})["result"]
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], terminal), [])
                self.assertEqual(terminal["status"], "completed", terminal)
                self.assertIsNone(terminal["error"])
                self.assertGreater(terminal["result"]["count"], 0)
                self.assertTrue(any(terminal["result"]["changes"].values()))
                after = self.snapshot()[:-1]
                self.assertNotEqual(after, before)
                self.col.undo()
                self.assertEqual(self.snapshot()[:-1], before)
                self.col.redo()
                self.assertEqual(self.snapshot()[:-1], after)

    def test_clear_unused_native_count_and_undo_redo(self):
        self.add(tags=["Used"])
        self.col.tags.set_collapsed("Unused", False)
        self.assertEqual(self.rpc("tags.clearUnused")["count"], 1)
        self.assertEqual(self.col.tags.all(), ["Used"])
        self.col.undo()
        self.assertIn("Unused", self.col.tags.all())
        self.col.redo()
        self.assertNotIn("Unused", self.col.tags.all())

    def test_collapse_is_native_nonundoable_and_preserves_prior_history(self):
        self.add(tags=["Root::Child"])
        before = self.col.undo_status()
        self.rpc("tags.setCollapsed", {"name": "Root", "collapsed": False})
        node = next(t for t in self.rpc("tags.list")["tags"] if t["name"] == "Root")
        self.assertFalse(node["collapsed"])
        after = self.col.undo_status()
        self.assertEqual((after.undo, after.redo), (before.undo, before.redo))

    def test_invalid_params_and_missing_names_every_method(self):
        self.add(tags=["Keep"])
        for method, params in {"list": {"prefix": False}, "rename": {"name": "Keep", "newName": "", "includeChildren": True},
                               "reparent": {"names": [], "parent": ""}, "remove": {"names": [], "includeChildren": True},
                               "clearUnused": {"extra": True}, "setCollapsed": {"name": "Keep", "collapsed": 1}}.items():
            self.error("tags." + method, params, "invalid_params")
        for method, params in {"rename": {"name": "missing", "newName": "New", "includeChildren": True},
                               "reparent": {"names": ["Keep", "missing"], "parent": "New"},
                               "remove": {"names": ["Keep", "missing"], "includeChildren": True},
                               "setCollapsed": {"name": "missing", "collapsed": False}}.items():
            before = self.col.undo_status()
            self.error("tags." + method, params, "not_found")
            self.assertEqual(self.col.undo_status(), before)
        self.assertIn("Keep", self.col.tags.all())

    def test_name_unicode_control_and_projected_depth_boundaries(self):
        self.add(tags=["Root::Child"])
        for name in ["two tags", "::x", "x::::y", "x\ud800", "x\x00"]:
            self.error("tags.rename", {"name": "Root", "newName": name, "includeChildren": True}, "invalid_params")
        history = self.col.undo_status()
        self.error("tags.rename", {"name": "Root", "newName": "x" * 1024, "includeChildren": True}, "invalid_params")
        self.assertEqual(self.col.undo_status(), history)

    def test_unicode_tag_identity_counts_rename_and_remove_follow_native_collation(self):
        one = self.add(tags=["ffi", "ffi::Child"])["noteId"]
        two = self.add(["Other", "A"], tags=["ﬃ", "ﬃ::Child"])["noteId"]
        counts = {tag["name"]: tag["noteCount"] for tag in self.rpc("tags.list")["tags"]}
        self.assertEqual(counts, {"ffi": 1, "ffi::Child": 1, "ﬃ": 1, "ﬃ::Child": 1})
        self.assertEqual([tag["name"] for tag in self.rpc("tags.list", {"prefix": "ffi"})["tags"]], ["ffi", "ffi::Child"])
        self.rpc("tags.rename", {"name": "ffi", "newName": "Renamed", "includeChildren": True})
        self.assertEqual(self.col.get_note(one).tags, ["Renamed", "Renamed::Child"])
        self.assertEqual(self.col.get_note(two).tags, ["ﬃ", "ﬃ::Child"])
        self.col.undo()
        self.rpc("tags.remove", {"names": ["FFI"], "includeChildren": True})
        self.assertEqual(self.col.get_note(one).tags, [])
        self.assertEqual(self.col.get_note(two).tags, ["ﬃ", "ﬃ::Child"])
