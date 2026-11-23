"""Native notes: byte-faithful HTML, duplicate/cloze checks and undo contracts."""
import copy
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from tests.test_decks import RpcCase
from tests.helpers import EnginePipe
from anki.notes import NoteFieldsCheckResult
from anki.config import Config
from memr_srs_engine.validate import validate_value


HTML = '<table style="border: 1px solid red"><tr><td>α &amp; β</td></tr></table>\n<script>const text = "<b>script as text</b>";</script>\t<span data-x="a&b">tail</span>'
MISSING = 9007199254740991


class NotesCase(RpcCase):
    def setUp(self):
        super().setUp()
        self.basic = self.col.models.by_name("Basic")["id"]
        self.progress_events = []

    def dispatch_frame(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        request_id = self.sequence
        self.engine.dispatch(json.dumps({"id": request_id, "method": method,
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
                self.assertEqual(frame["id"], request_id)
                responses.append(frame)
        self.assertEqual(len(responses), 1, frames)
        return responses[0]

    def frame(self, method, params=None):
        frame = self.dispatch_frame(method, params)
        if not self.engine.registry.catalogue[method]["longOperation"] or "operationId" not in frame.get("result", {}):
            return frame
        self.assertEqual(validate_value(self.engine.registry.catalogue[method]["result"], frame["result"]), [])
        self.engine.progress.thread.join(timeout=10)
        self.assertFalse(self.engine.progress.thread.is_alive(), method)
        operation = self.engine.progress.get(frame["result"]["operationId"])
        self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], operation), [])
        if operation["status"] == "completed":
            return {"id": frame["id"], "result": operation["result"]}
        return {"id": frame["id"], "error": operation["error"]}

    def rpc(self, method, params=None):
        if not self.engine.registry.catalogue[method]["longOperation"]:
            return super().rpc(method, params)
        frame = self.frame(method, params)
        self.assertIn("result", frame, frame)
        result = frame["result"]
        schema = self.engine.registry.catalogue["progress.get"]["result"]["properties"]["result"]
        self.assertEqual(validate_value(schema, result), [])
        self.assertEqual(set(result["changes"]), {
            "card", "note", "deck", "tag", "notetype", "config", "deckConfig",
            "mtime", "browserTable", "noteText", "studyQueues"})
        status = self.col.undo_status()
        expected = {"canUndo": bool(status.undo), "undoLabel": status.undo or None,
                    "canRedo": bool(status.redo), "redoLabel": status.redo or None}
        if self.engine.registry.catalogue[method]["mutating"]:
            expected["revision"] = self.engine.undo_revision()
        self.assertEqual(result["undo"], expected)
        return result

    def add(self, fields=None, tags=None, **extra):
        return self.rpc("notes.add", {"notetypeId": self.basic, "deckId": 1,
                                      "fields": fields or ["Question", "Answer"], "tags": tags or [], **extra})


class NoteTests(NotesCase):
    def test_all_methods_registered_and_float_ids_normalized(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("notes.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)
        nid = self.add(notetypeId=float(self.basic), deckId=1.0)["noteId"]
        self.assertEqual(self.rpc("notes.get", {"noteId": float(nid)})["id"], nid)

    def test_html_get_update_roundtrip_unchanged_and_undo_redo(self):
        added = self.add([HTML, '<p style="color: red">answer</p>'])
        self.assertEqual(added["duplicateState"], "ok")
        self.assertTrue(added["changes"]["note"])
        nid = added["noteId"]
        fetched = self.rpc("notes.get", {"noteId": nid})
        self.assertEqual(fetched["fields"][0].encode(), HTML.encode())
        result = self.rpc("notes.update", {"noteId": nid, "fields": fetched["fields"], "tags": ["new"],
                                           "expectedModifiedAt": fetched["modifiedAt"]})
        self.assertTrue(result["changes"]["note"])
        self.assertEqual(self.rpc("notes.get", {"noteId": nid})["fields"], fetched["fields"])
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, [])
        self.assertEqual(self.col.get_note(nid).fields, fetched["fields"])
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).tags, ["new"])
        status = self.col.undo_status()
        self.error("notes.update", {"noteId": nid, "fields": ["lost", "lost"], "tags": [],
                                    "expectedModifiedAt": 0}, "conflict")
        self.assertEqual(self.col.undo_status(), status)
        self.assertEqual(self.col.get_note(nid).fields, fetched["fields"])

    def test_duplicate_status_ids_and_explicit_override(self):
        first = self.add(["<b>Question</b>", "Answer"])["noteId"]
        checked = self.rpc("notes.fieldsCheck", {"notetypeId": self.basic, "fields": ["Question", "Other"]})
        self.assertEqual(checked, {"status": "duplicate", "duplicateNoteIds": [first]})
        self.error("notes.add", {"notetypeId": self.basic, "deckId": 1,
                                 "fields": ["Question", "Other"], "tags": []}, "conflict")
        self.assertEqual(self.col.note_count(), 1)
        duplicate = self.add(["Question", "Other"], allowDuplicate=True)
        self.assertEqual((duplicate["duplicateState"], duplicate["duplicateNoteIds"]), ("duplicate", [first]))
        self.assertEqual(self.rpc("notes.fieldsCheck", {"notetypeId": self.basic,
                         "fields": ["<b>Question</b>", "Answer"], "noteId": first})["duplicateNoteIds"], [duplicate["noteId"]])
        self.col.undo()
        self.assertEqual(self.col.note_count(), 1)
        self.col.redo()
        self.assertEqual(self.col.note_count(), 2)

    def test_update_noop_preserves_imported_html_and_existing_undo_redo(self):
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        nid = self.add(["<b>e\u0301</b>", "answer"], tags=["old"])["noteId"]
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        self.col.tags.bulk_add([nid], "redo")
        self.col.undo()
        fetched = self.rpc("notes.get", {"noteId": nid})
        before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                  self.col.db.all("select * from tags"), str(self.col.undo_status()))
        with patch.object(self.col, "update_note", wraps=self.col.update_note) as full_write, \
                patch.object(self.col.tags, "bulk_add", wraps=self.col.tags.bulk_add) as tag_write:
            result = self.rpc("notes.update", {"noteId": nid, "fields": fetched["fields"], "tags": fetched["tags"]})
        self.assertEqual((full_write.call_count, tag_write.call_count), (0, 0))
        self.assertFalse(any(result["changes"].values()))
        self.assertTrue(result["undo"]["canRedo"])
        self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                          self.col.db.all("select * from tags"), str(self.col.undo_status())), before)
        self.col.redo()
        self.assertIn("redo", self.col.get_note(nid).tags)

    def test_update_tag_only_preserves_imported_bytes_without_reusing_stale_abort(self):
        from memr_srs_engine.handlers import notes as handler
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        nid = self.add(["<b>e\u0301</b>" + HTML, "answer"], tags=["old"])["noteId"]
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        entered, release = threading.Event(), threading.Event()
        selection = handler._selection

        def held_selection(*args, **kwargs):
            selected = selection(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("The test did not release selection.")
            return selected

        with patch.object(handler, "_selection", side_effect=held_selection):
            try:
                operation_id = self.dispatch_frame("notes.mark", {"noteIds": [nid], "marked": True})["result"]["operationId"]
                self.assertTrue(entered.wait(2))
                self.rpc("progress.abort", {"operationId": operation_id})
            finally:
                release.set()
                self.engine.progress.thread.join(5)
        terminal = self.rpc("progress.get", {"operationId": operation_id})
        self.assertEqual(terminal["status"], "aborted")
        self.assertTrue(self.engine.progress.abort_requested)
        before = self.rpc("notes.get", {"noteId": nid})
        with patch.object(self.col, "update_note", wraps=self.col.update_note) as full_write, \
                patch.object(self.col.tags, "bulk_add", wraps=self.col.tags.bulk_add) as tag_write, \
                patch.object(self.engine.progress, "start", wraps=self.engine.progress.start) as start, \
                patch.object(self.engine.progress, "using_backend", wraps=self.engine.progress.using_backend) as route:
            self.rpc("notes.update", {"noteId": nid, "fields": before["fields"], "tags": ["old", "new"]})
        self.assertEqual((full_write.call_count, tag_write.call_count, start.call_count, route.call_count), (0, 1, 0, 0))
        self.assertEqual(self.col.get_note(nid).fields, before["fields"])
        self.assertEqual(self.col.card_ids_of_note(nid), before["cardIds"])
        self.assertEqual(self.engine.progress.get(operation_id), terminal)
        self.assertTrue(self.engine.progress.abort_requested)
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).tags, ["old"])
        self.assertEqual(self.col.get_note(nid).fields, before["fields"])
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).tags, ["new", "old"])
        self.assertTrue(self.col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT))

    def test_update_rejects_field_normalization_before_write_and_accepts_exact_edits(self):
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        nid = self.add(["<b>e\u0301</b>", "answer"], tags=["old"])["noteId"]
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                  self.col.db.all("select * from tags"), str(self.col.undo_status()))
        for fields in (["<b>e\u0301</b>", "edited answer"], ["<b>a\u0301</b>", "answer"]):
            with self.subTest(fields=fields), patch.object(self.col, "update_note", wraps=self.col.update_note) as active:
                frame = self.frame("notes.update", {"noteId": nid, "fields": fields, "tags": ["new"]})
                self.assertEqual(frame.get("error", {}).get("code"), "unsupported", frame)
                self.assertIn("NFC-normalized", frame["error"]["message"])
                self.assertEqual(active.call_count, 0)
                self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                                  self.col.db.all("select * from tags"), str(self.col.undo_status())), before)
        fields = ["<b>é</b>" + HTML, "edited answer"]
        with patch.object(self.col, "update_note", wraps=self.col.update_note) as active:
            self.rpc("notes.update", {"noteId": nid, "fields": fields, "tags": ["new"]})
        self.assertEqual(active.call_count, 1)
        self.assertEqual(self.col.get_note(nid).fields, fields)
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).fields, ["<b>e\u0301</b>", "answer"])
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).fields, fields)
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        decomposed = ["<b>e\u0301</b>" + HTML, "explicit decomposed edit"]
        self.rpc("notes.update", {"noteId": nid, "fields": decomposed, "tags": ["new"]})
        self.assertEqual(self.col.get_note(nid).fields, decomposed)
        self.assertFalse(self.col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT))

    def test_update_general_tag_replacement_rejects_before_changing_imported_fields(self):
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        nid = self.add(["<b>e\u0301</b>", "answer"], tags=["old"])["noteId"]
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        fields = self.col.get_note(nid).fields
        before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                  self.col.db.all("select * from tags"), str(self.col.undo_status()))
        with patch.object(self.col, "update_notes", wraps=self.col.update_notes) as active:
            self.error("notes.update", {"noteId": nid, "fields": fields, "tags": ["new-a", "new-b"]}, "unsupported")
        self.assertEqual(active.call_count, 0)
        self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                          self.col.db.all("select * from tags"), str(self.col.undo_status())), before)

    def test_duplicate_ids_preserve_native_html_and_whitespace_semantics(self):
        nid = self.add([HTML, "A"])["noteId"]
        self.assertEqual(self.rpc("notes.fieldsCheck", {"notetypeId": self.basic, "fields": [HTML, "B"]}),
                         {"status": "duplicate", "duplicateNoteIds": [nid]})

    def test_set_tags_respects_native_unicode_identity(self):
        nid = self.add(tags=["ffi", "ﬃ"])["noteId"]
        self.rpc("notes.setTags", {"noteIds": [nid], "tags": ["ffi"], "mode": "remove"})
        self.assertEqual(self.col.get_note(nid).tags, ["ﬃ"])
        self.col.undo()
        self.rpc("notes.setTags", {"noteIds": [nid], "tags": ["FFI"], "mode": "remove"})
        self.assertEqual(self.col.get_note(nid).tags, ["ﬃ"])

    def test_tag_only_native_paths_preserve_imported_html_bytes_and_missing_cards(self):
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        model = self.col.models.by_name("Basic (and reversed card)")
        note = self.col.new_note(model)
        original = '<b>e\u0301</b><script>const x = "<b>text</b>";</script>'
        note.fields, note.tags = [original, "answer"], ["old", "wild*", "wildX", "ffi", "ﬃ"]
        self.col.add_note(note, 1)
        cards = note.cards()
        self.col.remove_cards_and_orphaned_notes([cards[1].id])
        retained = self.col.card_ids_of_note(note.id)
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        for method, extra in [("mark", {"marked": True}),
                              ("setTags", {"tags": ["new"], "mode": "add"}),
                              ("setTags", {"tags": ["wild*", "ffi"], "mode": "remove"}),
                              ("setTags", {"tags": ["one"], "mode": "replace"})]:
            self.rpc("notes." + method, {"noteIds": [note.id], **extra})
            self.assertEqual(self.col.get_note(note.id).fields[0].encode(), original.encode())
            self.assertEqual(self.col.card_ids_of_note(note.id), retained)
            self.col.undo()
            self.assertEqual(self.col.get_note(note.id).fields[0].encode(), original.encode())
            self.assertEqual(self.col.card_ids_of_note(note.id), retained)
            self.col.redo()
            self.assertEqual(self.col.get_note(note.id).fields[0].encode(), original.encode())
            self.assertEqual(self.col.card_ids_of_note(note.id), retained)

    def test_general_tag_replace_preflights_field_bytes_and_required_cards(self):
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        imported = self.col.new_note(self.col.models.get(self.basic))
        imported.fields, imported.tags = ["<b>e\u0301</b>", "answer"], ["old"]
        self.col.add_note(imported, 1)
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        params = {"noteIds": [imported.id], "tags": ["new-a", "new-b"], "mode": "replace"}
        before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                  self.col.db.all("select * from tags"), str(self.col.undo_status()))
        with patch.object(self.col, "update_notes", wraps=self.col.update_notes) as active_write:
            self.error("notes.setTags", params, "unsupported")
        self.assertEqual(active_write.call_count, 0)
        self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                          self.col.db.all("select * from tags"), str(self.col.undo_status())), before)
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        self.assertEqual(self.rpc("notes.setTags", params)["count"], 1)
        self.assertEqual(self.col.get_note(imported.id).fields, imported.fields)
        ordinary = self.add([HTML, "A"], tags=["old"])["noteId"]
        card_ids = self.col.card_ids_of_note(ordinary)
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        self.assertEqual(self.rpc("notes.setTags", {**params, "noteIds": [ordinary]})["count"], 1)
        self.assertEqual(self.col.get_note(ordinary).fields, [HTML, "A"])
        self.assertEqual(self.col.card_ids_of_note(ordinary), card_ids)
        reverse = self.col.new_note(self.col.models.by_name("Basic (and reversed card)"))
        reverse.fields, reverse.tags = ["Reverse question", "Reverse answer"], ["old"]
        self.col.add_note(reverse, 1)
        self.col.remove_cards_and_orphaned_notes([reverse.cards()[1].id])
        before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                  self.col.db.all("select * from tags"), str(self.col.undo_status()))
        with patch.object(self.col, "update_notes", wraps=self.col.update_notes) as active_write:
            self.error("notes.setTags", {**params, "noteIds": [reverse.id]}, "unsupported")
        self.assertEqual(active_write.call_count, 0)
        self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                          self.col.db.all("select * from tags"), str(self.col.undo_status())), before)

    def test_general_tag_replace_uses_existing_note_card_generation(self):
        model = copy.deepcopy(self.col.models.by_name("Basic (and reversed card)"))
        model["id"], model["name"] = 0, "Optional native tag validation"
        for index, template in enumerate(model["tmpls"]):
            template["qfmt"] = "{{#Back}}{{Front}} " + str(index) + "{{/Back}}"
        model = self.col.models.get(self.col.models.add_dict(model).id)
        note = self.col.new_note(model)
        note.fields, note.tags = ["Historical card", "answer"], ["old"]
        self.col.add_note(note, 1)
        cards = note.cards()
        self.col.remove_cards_and_orphaned_notes([card.id for card in cards if card.ord == 0])
        note.fields[1] = ""
        self.col.update_note(note)
        before = self.col.card_ids_of_note(note.id)
        self.assertEqual([card.ord for card in note.cards()], [1])
        self.assertEqual(self.rpc("notes.setTags", {"noteIds": [note.id], "tags": ["new-a", "new-b"],
                                                   "mode": "replace"})["count"], 1)
        self.assertEqual(self.col.get_note(note.id).tags, ["new-a", "new-b"])
        self.assertEqual(self.col.card_ids_of_note(note.id), before)
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).tags, ["old"])
        self.assertEqual(self.col.card_ids_of_note(note.id), before)
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id).tags, ["new-a", "new-b"])

    def test_native_field_states_and_cloze_number_boundaries(self):
        cloze = self.col.models.by_name("Cloze")["id"]
        for mid, fields, status in [
            (self.basic, ["<br>", ""], "empty"),
            (self.basic, ["{{c1::text}}", ""], "notetypeNotCloze"),
            (cloze, ["plain text", ""], "missingCloze"),
            (cloze, ["{{c1::text}}", "{{c2::wrong field}}"], "fieldNotCloze"),
            (cloze, ["{{c1,3::outer {{c2::inner}}}}", ""], "ok"),
        ]:
            with self.subTest(status=status):
                result = self.rpc("notes.fieldsCheck", {"notetypeId": mid, "fields": fields})
                self.assertEqual(result["status"], status)
                note = self.col.new_note(self.col.models.get(mid))
                note.fields = fields
                native = {NoteFieldsCheckResult.NORMAL: "ok", NoteFieldsCheckResult.EMPTY: "empty",
                          NoteFieldsCheckResult.MISSING_CLOZE: "missingCloze",
                          NoteFieldsCheckResult.NOTETYPE_NOT_CLOZE: "notetypeNotCloze",
                          NoteFieldsCheckResult.FIELD_NOT_CLOZE: "fieldNotCloze"}[note.fields_check()]
                self.assertEqual(result["status"], native)
        added = self.add(["{{c1,3::outer {{c2::inner}}}}", ""], notetypeId=cloze)
        self.assertEqual(len(added["cardIds"]), 3)
        for text in ("{{c0::bad}}", "{{c501::bad}}", "{{c65536::bad}}", "{{c1,::bad}}", "{{c9999999999999999999999::bad}}"):
            self.error("notes.fieldsCheck", {"notetypeId": cloze, "fields": [text, ""]}, "invalid_params")
        high = self.add(["{{c500::upper boundary}}", ""], notetypeId=cloze)
        self.assertEqual(self.col.get_card(high["cardIds"][0]).ord, 499)

    def test_remove_counts_notes_with_multiple_cards_and_undo_redo(self):
        reverse = self.col.models.by_name("Basic (and reversed card)")["id"]
        ids = [self.add([f"Q{i}", "A"], notetypeId=reverse)["noteId"] for i in range(2)]
        self.assertEqual(self.col.card_count(), 4)
        result = self.rpc("notes.remove", {"noteIds": [*ids, ids[0]]})
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.col.note_count(), 0)
        self.col.undo()
        self.assertEqual(self.col.card_count(), 4)
        self.col.redo()
        self.assertEqual(self.col.card_count(), 0)

    def test_mark_and_all_set_tags_modes_are_exact_atomic_and_undoable(self):
        one = self.add(tags=["old", "marked::child"])["noteId"]
        two = self.add(["Other", "A"], tags=["keep"])["noteId"]
        result = self.rpc("notes.mark", {"noteIds": [one, one], "marked": True})
        self.assertEqual(result["count"], 1)
        self.assertIn("marked", self.col.get_note(one).tags)
        self.col.undo()
        self.assertNotIn("marked", self.col.get_note(one).tags)
        self.col.redo()
        self.rpc("notes.mark", {"noteIds": [one], "marked": False})
        self.assertIn("marked::child", self.col.get_note(one).tags)
        for mode, tags, expected in [("add", ["new"], ["marked::child", "new", "old"]),
                                      ("remove", ["OLD"], ["marked::child", "new"]),
                                      ("replace", ["replacement"], ["replacement"])]:
            prior = self.col.get_note(one).tags
            self.assertEqual(self.rpc("notes.setTags", {"noteIds": [one], "mode": mode, "tags": tags})["count"], 1)
            self.assertEqual(self.col.get_note(one).tags, expected)
            self.col.undo()
            self.assertEqual(self.col.get_note(one).tags, prior)
            self.col.redo()
        before = self.col.undo_status()
        self.error("notes.setTags", {"noteIds": [one, MISSING], "mode": "replace", "tags": []}, "not_found")
        self.assertEqual(self.col.undo_status(), before)
        self.assertEqual(self.col.get_note(two).tags, ["keep"])

    def test_find_duplicates_search_scope_and_literal_markup(self):
        one = self.add(["<b>Shared</b>", "A"], tags=["scope"])["noteId"]
        two = self.add(["Shared", "B"], tags=["scope"], allowDuplicate=True)["noteId"]
        self.add(["Shared", "C"], allowDuplicate=True)
        result = self.rpc("notes.findDuplicates", {"fieldName": "Front", "search": "tag:scope"})
        self.assertEqual(result["groups"], [{"text": "Shared", "noteIds": [one, two]}])
        self.error("notes.findDuplicates", {"fieldName": "Missing"}, "not_found")
        self.error("notes.findDuplicates", {"fieldName": "Front", "search": 'deck:"'}, "invalid_params")

    def test_find_replace_native_regex_case_and_note_field_scope_undo(self):
        one = self.add(["Abc 123", HTML])["noteId"]
        two = self.add(["Abc 123", "Other"], allowDuplicate=True)["noteId"]
        params = {"noteIds": [one], "search": "abc ([0-9]+)", "replacement": "number $1", "regex": True,
                  "matchCase": False, "fieldName": "Front"}
        self.assertEqual(self.rpc("notes.findReplace", params)["count"], 1)
        self.assertEqual(self.col.get_note(one).fields, ["number 123", HTML])
        self.assertEqual(self.col.get_note(two).fields[0], "Abc 123")
        self.col.undo()
        self.assertEqual(self.col.get_note(one).fields[0], "Abc 123")
        self.col.redo()
        prior = self.col.undo_status()
        self.error("notes.findReplace", {**params, "search": "["}, "invalid_params")
        self.assertEqual(self.col.undo_status(), prior)
        self.error("notes.findReplace", {**params, "fieldName": "missing"}, "not_found")
        self.assertEqual(self.rpc("notes.findReplace", {**params, "search": "NUMBER", "regex": False,
                         "matchCase": True, "fieldName": None})["count"], 0)

    def test_add_history_newest_first_and_bounded(self):
        first, second = self.add()["noteId"], self.add(["Second", "A"])["noteId"]
        self.assertEqual([n["id"] for n in self.rpc("notes.addHistory", {"limit": 1})["notes"]], [second])
        self.assertEqual([n["id"] for n in self.rpc("notes.addHistory")["notes"]], [second, first])

    def test_editor_defaults_sticky_values_and_undo_redo(self):
        deck = self.create("Editor")
        original = self.rpc("notes.defaultsForAdding", {"notetypeId": self.basic})
        params = {"deckId": deck, "notetypeId": self.basic, "fields": ["clear", HTML],
                  "stickyFields": [1], "tags": ["default"]}
        self.rpc("notes.setAddingDefaults", params)
        expected = {"deckId": deck, "notetypeId": self.basic, "fields": ["", HTML],
                    "stickyFields": [1], "tags": ["default"]}
        self.assertEqual(self.rpc("notes.defaultsForAdding"), expected)
        self.col.undo()
        self.assertEqual(self.rpc("notes.defaultsForAdding", {"notetypeId": self.basic}), original)
        self.col.redo()
        self.assertEqual(self.rpc("notes.defaultsForAdding"), expected)
        self.error("notes.setAddingDefaults", {**params, "stickyFields": [2]}, "invalid_params")
        self.assertEqual(self.rpc("notes.defaultsForAdding"), expected)
        self.col.decks.remove([deck])
        self.assertEqual(self.rpc("notes.defaultsForAdding")["deckId"], 1)

    def test_malformed_editor_defaults_recover_and_can_be_repaired(self):
        from memr_srs_engine.handlers.notes import ADDING_DEFAULTS
        native = self.rpc("notes.defaultsForAdding", {"notetypeId": self.basic})
        valid = {"fieldNames": ["Front", "Back"], "fields": ["", HTML], "stickyFields": [1], "tags": ["tag"]}
        corrupt = [None, [], {"notetypeId": [], "deckId": False, "byNotetype": []},
                   {"byNotetype": {str(self.basic): []}},
                   {"byNotetype": {str(self.basic): {**valid, "fields": False}}},
                   {"byNotetype": {str(self.basic): {**valid, "stickyFields": [2]}}},
                   {"byNotetype": {str(self.basic): {**valid, "tags": [False]}}}]
        for saved in corrupt:
            self.col.set_config(ADDING_DEFAULTS, saved)
            self.assertEqual(self.rpc("notes.defaultsForAdding", {"notetypeId": self.basic}), native)
            self.rpc("notes.setAddingDefaults", {"notetypeId": self.basic, "deckId": 1,
                     "fields": ["", HTML], "stickyFields": [1], "tags": ["repaired"]})
            self.assertEqual(self.rpc("notes.defaultsForAdding")["fields"], ["", HTML])

    def test_tag_count_boundary_preflights_mark_source_and_whole_batch(self):
        tags = [f"tag-{i:04}" for i in range(1000)]
        full = self.add(tags=tags)["noteId"]
        other = self.add(["Other", "A"])["noteId"]
        before = self.col.undo_status()
        self.error("notes.mark", {"noteIds": [other, full], "marked": True}, "invalid_params")
        self.assertEqual(self.col.undo_status(), before)
        self.assertEqual(self.col.get_note(other).tags, [])
        self.error("notes.setTags", {"noteIds": [other, full], "tags": ["extra"], "mode": "add"}, "invalid_params")
        self.assertEqual(self.col.get_note(other).tags, [])
        self.assertEqual(self.rpc("notes.setTags", {"noteIds": [full], "tags": [tags[0]], "mode": "add"})["count"], 0)
        model = copy.deepcopy(self.col.models.get(self.basic))
        model["id"], model["name"] = 0, "Source tag limit"
        model["flds"].append(self.col.models.new_field("Source"))
        model["id"] = self.col.models.add_dict(model).id
        source = {"pageId": "page", "blockId": None, "revision": None, "uri": "canonnotes:page/page", "changed": None}
        count, before = self.col.note_count(), self.col.undo_status()
        self.error("notes.add", {"notetypeId": model["id"], "deckId": 1,
                   "fields": ["Source question", "A", ""], "tags": tags, "source": source}, "invalid_params")
        self.assertEqual((self.col.note_count(), self.col.undo_status()), (count, before))

    def test_find_replace_overflow_is_rejected_before_active_mutation(self):
        first = self.add(["x" * 524289, "A"])["noteId"]
        second = self.add(["x", "A"])["noteId"]
        before = self.col.undo_status()
        params = {"noteIds": [second, first], "search": "x", "replacement": "xx", "regex": False,
                  "matchCase": True, "fieldName": "Front"}
        self.error("notes.findReplace", params, "invalid_params")
        self.assertEqual(self.col.undo_status(), before)
        self.assertEqual(self.col.get_note(second).fields[0], "x")
        self.assertEqual(self.col.get_note(first).fields[0], "x" * 524289)

    def test_find_replace_checks_changed_cloze_numbers_before_any_active_write(self):
        cloze = self.col.models.by_name("Cloze")["id"]
        nid = self.add(["{{c1::text}}", "A"], notetypeId=cloze)["noteId"]
        before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                  str(self.col.undo_status()))
        for replacement in ("501", "0", "1,", "65536"):
            with self.subTest(replacement=replacement), \
                    patch.object(self.col, "find_and_replace", wraps=self.col.find_and_replace) as active_write:
                self.error("notes.findReplace", {"noteIds": [nid], "search": "1", "replacement": replacement,
                           "regex": False, "matchCase": True, "fieldName": "Text"}, "invalid_params")
                self.assertEqual(active_write.call_count, 0)
                self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                                  str(self.col.undo_status())), before)
        result = self.rpc("notes.findReplace", {"noteIds": [nid], "search": "1", "replacement": "500",
                          "regex": False, "matchCase": True, "fieldName": "Text"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_note(nid).fields[0], "{{c500::text}}")
        self.assertIn(499, [card.ord for card in self.col.get_note(nid).cards()])
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).fields[0], "{{c1::text}}")

    def test_find_replace_keeps_unchanged_imported_cloze_and_io_zero_semantics(self):
        cloze = self.col.models.by_name("Cloze")
        imported = self.col.new_note(cloze)
        imported.fields = ["{{c501::imported}}", "Extra"]
        self.col.add_note(imported, 1)
        result = self.rpc("notes.findReplace", {"noteIds": [imported.id], "search": "absent", "replacement": "same",
                          "regex": False, "matchCase": True, "fieldName": "Text"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.col.get_note(imported.id).fields[0], "{{c501::imported}}")
        io = self.rpc("notetypes.add", {"stockKind": "imageOcclusion", "name": "IO replace semantics"})["notetypeId"]
        note = self.col.new_note(self.col.models.get(io))
        note.fields = ["{{c0::image-occlusion:rect:left=.1:top=.1:width=.2:height=.2}}", '<img src="sample.png">', "", "", ""]
        self.col.add_note(note, 1)
        self.assertEqual(self.rpc("notes.findReplace", {"noteIds": [note.id], "search": ".2", "replacement": ".3",
                         "regex": False, "matchCase": True, "fieldName": "Occlusion"})["count"], 1)
        self.assertIn("{{c0::", self.col.get_note(note.id).fields[0])

    def test_find_replace_precommit_cancel_controls_busy_and_exact_undo_redo(self):
        from memr_srs_engine.handlers import notes as handler
        nid = self.add(["original", "A"])["noteId"]
        self.rpc("notes.update", {"noteId": nid, "fields": ["redo value", "A"], "tags": []})
        self.col.undo()
        before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                  str(self.col.undo_status()))
        entered, release = threading.Event(), threading.Event()
        native_probe = handler._probe_replace

        def hold_after_native_preflight(engine, params, fields):
            native_probe(engine, params, fields)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("The synthetic preflight barrier timed out.")

        params = {"noteIds": [nid], "search": "original", "replacement": "changed", "regex": False,
                  "matchCase": True, "fieldName": "Front"}
        try:
            with patch.object(handler, "_probe_replace", side_effect=hold_after_native_preflight), \
                    patch.object(self.col, "find_and_replace", wraps=self.col.find_and_replace) as active_write:
                ack = self.dispatch_frame("notes.findReplace", params)["result"]
                self.assertIn("operationId", ack)
                self.assertTrue(entered.wait(2))
                running = self.dispatch_frame("progress.get", {"operationId": ack["operationId"]})["result"]
                self.assertEqual(running["status"], "running")
                self.assertEqual(self.dispatch_frame("notes.get", {"noteId": nid})["error"]["code"], "busy")
                self.dispatch_frame("progress.abort", {"operationId": ack["operationId"]})
                release.set()
                self.engine.progress.thread.join(timeout=3)
                self.assertFalse(self.engine.progress.thread.is_alive())
                self.assertEqual(active_write.call_count, 0)
        finally:
            release.set()
        terminal = self.engine.progress.get(ack["operationId"])
        self.assertEqual(terminal["status"], "aborted")
        self.assertEqual(terminal["error"]["code"], "aborted")
        self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                          str(self.col.undo_status())), before)
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).fields[0], "redo value")

    def test_find_replace_cancel_after_native_commit_reports_completed(self):
        nid = self.add(["original", "A"])["noteId"]
        committed, release = threading.Event(), threading.Event()
        native_replace = self.col.find_and_replace

        def hold_after_commit(**params):
            changed = native_replace(**params)
            committed.set()
            if not release.wait(3):
                raise RuntimeError("The synthetic commit barrier timed out.")
            return changed

        params = {"noteIds": [nid], "search": "original", "replacement": "changed", "regex": False,
                  "matchCase": True, "fieldName": "Front"}
        try:
            with patch.object(self.col, "find_and_replace", side_effect=hold_after_commit):
                ack = self.dispatch_frame("notes.findReplace", params)["result"]
                self.assertTrue(committed.wait(2))
                self.dispatch_frame("progress.abort", {"operationId": ack["operationId"]})
                release.set()
                self.engine.progress.thread.join(timeout=3)
                self.assertFalse(self.engine.progress.thread.is_alive())
        finally:
            release.set()
        terminal = self.engine.progress.get(ack["operationId"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertEqual(terminal["result"]["count"], 1)
        self.assertEqual(self.col.get_note(nid).fields[0], "changed")
        self.col.undo()
        self.assertEqual(self.col.get_note(nid).fields[0], "original")
        self.col.redo()
        self.assertEqual(self.col.get_note(nid).fields[0], "changed")

    def test_find_replace_checks_all_bounded_scratch_batches_before_active_write(self):
        from memr_srs_engine.handlers import notes as handler
        ids = [self.add([str(index) + "x" * 4000, "A"])["noteId"] for index in range(66)]
        last = self.add(["x" * 524289, "A"])["noteId"]
        before = self.col.undo_status()
        params = {"noteIds": [*ids, last], "search": "x", "replacement": "x" * 48, "regex": False,
                  "matchCase": True, "fieldName": "Front"}
        with patch.object(handler, "_probe_replace", wraps=handler._probe_replace) as probe, \
                patch.object(self.col, "find_and_replace", wraps=self.col.find_and_replace) as active_write:
            self.error("notes.findReplace", params, "invalid_params")
        self.assertGreaterEqual(probe.call_count, 3)
        self.assertEqual(active_write.call_count, 0)
        self.assertEqual(self.col.undo_status(), before)
        self.assertEqual(self.col.get_note(ids[0]).fields[0], "0" + "x" * 4000)
        self.assertEqual(self.col.get_note(last).fields[0], "x" * 524289)

    def test_find_replace_large_conservative_bound_preserves_valid_native_noop(self):
        from memr_srs_engine.handlers import notes as handler
        field = "a" * 100000
        nid = self.add([field, "A"])["noteId"]
        before = self.col.undo_status()
        with patch.object(handler, "_probe_replace_isolated", wraps=handler._probe_replace_isolated) as isolated:
            result = self.rpc("notes.findReplace", {"noteIds": [nid], "search": "not-present",
                              "replacement": "x" * 48, "regex": False, "matchCase": True, "fieldName": "Front"})
        self.assertEqual(isolated.call_count, 1)
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.col.get_note(nid).fields[0], field)
        self.assertEqual((self.col.undo_status().undo, self.col.undo_status().redo), (before.undo, before.redo))

    def test_find_replace_allocator_failure_is_isolated_from_active_engine(self):
        nid = self.add(["x" * 1048576, "A"])["noteId"]
        before = self.col.undo_status()
        params = {"noteIds": [nid], "search": "", "replacement": "x" * 65536,
                  "regex": False, "matchCase": True, "fieldName": "Front"}
        self.error("notes.findReplace", params, "unsupported")
        self.assertEqual(self.col.undo_status(), before)
        self.assertEqual(len(self.rpc("notes.get", {"noteId": nid})["fields"][0]), 1048576)

    def test_find_replace_cancel_reaps_isolated_native_process_and_scratch(self):
        from memr_srs_engine.handlers import notes as handler
        nid = self.add(["a" * 100000, "A"])["noteId"]
        before = (self.col.db.all("select * from notes"), str(self.col.undo_status()))
        spawned, children, directories = threading.Event(), [], []
        native_popen, native_directory = handler.subprocess.Popen, handler.tempfile.TemporaryDirectory

        def popen(*args, **kwargs):
            process = native_popen(*args, **kwargs)
            children.append(process)
            spawned.set()
            return process

        def directory(*args, **kwargs):
            temporary = native_directory(*args, **kwargs)
            directories.append(Path(temporary.name))
            return temporary

        held_code = handler.REPLACEMENT_PROBE.replace(
            "    _validate_replacement(col, payload", "    import time; time.sleep(3)\n    _validate_replacement(col, payload")
        params = {"noteIds": [nid], "search": "not-present", "replacement": "x" * 48,
                  "regex": False, "matchCase": True, "fieldName": "Front"}
        with patch.object(handler.subprocess, "Popen", side_effect=popen), \
                patch.object(handler.tempfile, "TemporaryDirectory", side_effect=directory), \
                patch.object(handler, "REPLACEMENT_PROBE", held_code):
            ack = self.dispatch_frame("notes.findReplace", params)["result"]
            self.assertTrue(spawned.wait(2))
            self.dispatch_frame("progress.abort", {"operationId": ack["operationId"]})
            self.engine.progress.thread.join(timeout=3)
            self.assertFalse(self.engine.progress.thread.is_alive())
        self.assertEqual(self.engine.progress.get(ack["operationId"])["status"], "aborted")
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertEqual((self.col.db.all("select * from notes"), str(self.col.undo_status())), before)

    def test_other_bulk_note_methods_cancel_before_native_write_and_keep_controls_live(self):
        from memr_srs_engine.handlers import notes as handler
        for method in ("notes.remove", "notes.mark", "notes.setTags", "notes.findDuplicates"):
            with self.subTest(method=method):
                nid = self.add([method, "Shared answer"])["noteId"]
                before = (self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                          str(self.col.undo_status()))
                entered, release = threading.Event(), threading.Event()
                if method == "notes.findDuplicates":
                    owner, name = self.col._backend, "strip_html"
                    params = {"fieldName": "Back"}
                elif method == "notes.remove":
                    owner, name = handler, "_selected_ids"
                    params = {"noteIds": [nid]}
                else:
                    owner, name = handler, "_selection"
                    params = {"noteIds": [nid], **({"marked": True} if method == "notes.mark"
                              else {"tags": ["new"], "mode": "replace"})}
                original = getattr(owner, name)

                def hold(*args, **kwargs):
                    value = original(*args, **kwargs)
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("The synthetic note preparation barrier timed out.")
                    return value

                try:
                    with patch.object(owner, name, side_effect=hold), \
                            patch.object(self.col, "update_notes", wraps=self.col.update_notes) as update, \
                            patch.object(self.col, "remove_notes", wraps=self.col.remove_notes) as remove:
                        ack = self.dispatch_frame(method, params)["result"]
                        self.assertTrue(entered.wait(2))
                        self.assertEqual(self.dispatch_frame("progress.get")["result"]["status"], "running")
                        self.assertEqual(self.dispatch_frame("notes.get", {"noteId": nid})["error"]["code"], "busy")
                        self.dispatch_frame("progress.abort", {"operationId": ack["operationId"]})
                        release.set()
                        self.engine.progress.thread.join(timeout=3)
                        self.assertFalse(self.engine.progress.thread.is_alive())
                        self.assertEqual((update.call_count, remove.call_count), (0, 0))
                finally:
                    release.set()
                self.assertEqual(self.engine.progress.get(ack["operationId"])["status"], "aborted")
                self.assertEqual((self.col.db.all("select * from notes"), self.col.db.all("select * from cards"),
                                  str(self.col.undo_status())), before)

    def test_other_bulk_note_commits_win_late_cancel_and_keep_native_undo_redo(self):
        for method in ("notes.remove", "notes.mark", "notes.setTags"):
            with self.subTest(method=method):
                nid = self.add([method, "Answer"])["noteId"]
                owner = self.col if method == "notes.remove" else self.col.tags
                name = "remove_notes" if method == "notes.remove" else "bulk_add"
                original = getattr(owner, name)
                committed, release = threading.Event(), threading.Event()

                def hold(*args, **kwargs):
                    value = original(*args, **kwargs)
                    committed.set()
                    if not release.wait(3):
                        raise RuntimeError("The synthetic note commit barrier timed out.")
                    return value

                params = {"noteIds": [nid], **({} if method == "notes.remove" else {"marked": True}
                          if method == "notes.mark" else {"tags": ["new"], "mode": "replace"})}
                try:
                    with patch.object(owner, name, side_effect=hold):
                        ack = self.dispatch_frame(method, params)["result"]
                        self.assertTrue(committed.wait(2))
                        self.dispatch_frame("progress.abort", {"operationId": ack["operationId"]})
                        release.set()
                        self.engine.progress.thread.join(timeout=3)
                        self.assertFalse(self.engine.progress.thread.is_alive())
                finally:
                    release.set()
                terminal = self.engine.progress.get(ack["operationId"])
                self.assertEqual(terminal["status"], "completed", terminal)
                self.assertEqual(terminal["result"]["count"], 1)
                self.col.undo()
                self.assertEqual(self.col.get_note(nid).tags, [])
                self.col.redo()
                if method == "notes.remove":
                    self.assertFalse(self.col.db.scalar("select count(*) from notes where id=?", nid))
                else:
                    self.assertEqual(self.col.get_note(nid).tags, ["marked"] if method == "notes.mark" else ["new"])

    def test_next_long_start_retains_prior_terminal_event_and_read_metadata(self):
        nid = self.add()["noteId"]
        self.rpc("notes.mark", {"noteIds": [nid], "marked": True})
        operation_id = self.engine.progress.current
        before = str(self.col.undo_status())
        result = self.rpc("notes.findDuplicates", {"fieldName": "Front"})
        events = [event for event in self.progress_events if event["operationId"] == operation_id]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["progress"]["status"], "completed")
        self.assertFalse(any(result["changes"].values()))
        self.assertEqual(str(self.col.undo_status()), before)

    def test_source_field_is_canonical_and_omission_preserves_it(self):
        source = {"pageId": "page space", "blockId": "block", "revision": "a" * 64,
                  "uri": "canonnotes:page/page%20space?block=block&rev=" + "a" * 64, "changed": None}
        self.error("notes.add", {"notetypeId": self.basic, "deckId": 1, "fields": ["Q", "A"],
                                 "tags": [], "source": source}, "unsupported")
        model = copy.deepcopy(self.col.models.get(self.basic))
        model["id"], model["name"] = 0, "With source"
        model["flds"].append(self.col.models.new_field("Source"))
        model["id"] = self.col.models.add_dict(model).id
        nid = self.add([HTML, "A", ""], notetypeId=model["id"], source=source)["noteId"]
        note = self.rpc("notes.get", {"noteId": nid})
        self.assertEqual(note["source"], source)
        self.assertEqual(note["fields"][-1], source["uri"])
        self.rpc("notes.update", {"noteId": nid, "fields": note["fields"], "tags": note["tags"]})
        self.assertEqual(self.rpc("notes.get", {"noteId": nid})["source"], source)
        self.rpc("notes.update", {"noteId": nid, "fields": note["fields"], "tags": note["tags"], "source": None})
        self.assertIsNone(self.rpc("notes.get", {"noteId": nid})["source"])
        self.col.undo()
        self.assertEqual(self.rpc("notes.get", {"noteId": nid})["source"], source)
        self.col.redo()
        self.assertIsNone(self.rpc("notes.get", {"noteId": nid})["source"])

    def test_missing_ids_and_batch_preflight_do_not_mutate(self):
        nid = self.add()["noteId"]
        cases = {"get": {"noteId": MISSING}, "defaultsForAdding": {"notetypeId": MISSING},
                 "add": {"notetypeId": MISSING, "deckId": 1, "fields": ["Q", "A"], "tags": []},
                 "update": {"noteId": MISSING, "fields": ["Q", "A"], "tags": []},
                 "remove": {"noteIds": [nid, MISSING]}, "mark": {"noteIds": [nid, MISSING], "marked": True},
                 "fieldsCheck": {"notetypeId": self.basic, "fields": ["Q", "A"], "noteId": MISSING},
                 "setAddingDefaults": {"notetypeId": self.basic, "deckId": MISSING, "fields": ["", ""],
                                       "stickyFields": [], "tags": []}}
        for method, params in cases.items():
            before = self.col.undo_status()
            self.error("notes." + method, params, "not_found")
            self.assertEqual(self.col.undo_status(), before)
        self.error("notes.get", {"noteId": 0}, "not_found")
        self.assertEqual(self.col.note_count(), 1)

    def test_invalid_params_for_every_method_and_semantic_boundaries(self):
        cases = {"get": {}, "defaultsForAdding": {"deckId": -1}, "add": {}, "update": {},
                 "remove": {"noteIds": []}, "mark": {"noteIds": [], "marked": True},
                 "setTags": {"noteIds": [], "tags": [], "mode": "add"},
                 "findDuplicates": {"fieldName": ""}, "findReplace": {}, "fieldsCheck": {},
                 "addHistory": {"limit": 0}, "setAddingDefaults": {}}
        for method, params in cases.items():
            self.error("notes." + method, params, "invalid_params")
        for fields, tags in [(["Q"], []), (["Q\x1f", "A"], []), (["\ud800", "A"], []),
                              (["Q", "A"], ["two tags"]), (["Q", "A"], ["x::::y"])]:
            self.error("notes.add", {"notetypeId": self.basic, "deckId": 1, "fields": fields, "tags": tags}, "invalid_params")
        self.assertEqual(self.col.note_count(), 0)

    def test_defaults_persist_across_native_process_restart(self):
        with tempfile.TemporaryDirectory(prefix="memr-e03-defaults-") as directory:
            with EnginePipe(directory) as process:
                defaults = process.call("notes.defaultsForAdding")["result"]
                params = {"notetypeId": defaults["notetypeId"], "deckId": defaults["deckId"],
                          "fields": ["clear", HTML], "stickyFields": [1], "tags": ["persistent"]}
                self.assertIn("result", process.call("notes.setAddingDefaults", params))
            with EnginePipe(directory) as process:
                result = process.call("notes.defaultsForAdding")["result"]
                self.assertEqual(result["fields"], ["", HTML])
                self.assertEqual(result["tags"], ["persistent"])
