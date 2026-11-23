"""Maintenance contracts on generated collections with the pinned native backend."""
import io
import json
import logging
from pathlib import Path
import threading
import unittest
from unittest.mock import PropertyMock, patch
import zipfile

from anki import errors as anki_errors

from memr_srs_engine import handlers
from memr_srs_engine.errors import EngineError
from memr_srs_engine.handlers import maintenance
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from tests.helpers import FakeClock, build_collection, temp_collection


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.collection = temp_collection()
        self.col = self.collection.__enter__()
        self.output = io.BytesIO()
        self.clock = FakeClock()
        logger = logging.getLogger("e05-maintenance-tests")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger, self.clock)
        self.sequence = 0

    def tearDown(self):
        self.engine.progress.close()
        self.collection.__exit__(None, None, None)

    def frame(self, name, params=None):
        self.sequence += 1
        offset = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": name,
                                         "params": {} if params is None else params}).encode())
        frames = [json.loads(line) for line in self.output.getvalue()[offset:].splitlines()]
        for frame in frames:
            if frame.get("event"):
                self.assertEqual(frame["event"], "progress")
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], frame["progress"]), [])
        responses = [frame for frame in frames if not frame.get("event")]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], self.sequence)
        return responses[0]

    def rpc(self, name, params=None):
        frame = self.frame(name, params)
        self.assertIn("result", frame, frame)
        result = frame["result"]
        self.assertEqual(validate_value(self.engine.registry.catalogue[name]["result"], result), [])
        return result

    def long(self, name, params=None, expected="completed"):
        result = self.rpc(name, params)
        self.engine.progress.thread.join(timeout=10)
        self.assertFalse(self.engine.progress.thread.is_alive())
        terminal = self.rpc("progress.get", {"operationId": result["operationId"]})
        self.assertEqual(terminal["status"], expected, terminal)
        return terminal

    def note(self, question):
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"], note["Back"] = question, "Generated answer"
        self.col.add_note(note, 1)
        return note

    def test_methods_registered_and_restore_schema(self):
        expected = {"maintenance." + name for name in (
            "checkDatabase", "emptyCards", "removeEmptyCards", "backupNow", "backups", "restoreBackup")}
        self.assertTrue(expected <= self.engine.registry.handlers.keys())
        schema = self.engine.registry.catalogue["maintenance.restoreBackup"]
        self.assertTrue(schema["longOperation"])
        self.assertTrue(schema["mutating"])

    def test_check_database_native_summary_and_mutation(self):
        build_collection(self.col, notes_per_deck=3)
        result = self.long("maintenance.checkDatabase")["result"]
        self.assertTrue(result["summary"])
        self.assertTrue(result["changes"]["studyQueues"])
        self.assertEqual(self.col.card_count(), 3)
        self.assertFalse(result["undo"]["canUndo"])

    def test_check_database_native_repair_preserves_success_after_late_abort(self):
        note = self.note("Generated repair question")
        card_id = int(note.cards()[0].id)
        # A regular card cannot retain a filtered-deck original-deck pointer.
        self.col.db.execute("update cards set odid=999999999 where id=?", card_id)
        self.assertEqual(self.col.db.scalar("select odid from cards where id=?", card_id), 999999999)
        native = self.col.fix_integrity
        repaired = []
        def abort_after_repair():
            result = native()
            repaired.append(self.col.db.scalar("select odid from cards where id=?", card_id))
            self.engine.progress.abort(self.engine.progress.current)
            return result
        with patch.object(self.col, "fix_integrity", side_effect=abort_after_repair):
            terminal = self.long("maintenance.checkDatabase")
        self.assertEqual(repaired, [0])
        self.assertEqual(self.col.db.scalar("select odid from cards where id=?", card_id), 0)
        self.assertTrue(all(terminal["result"]["changes"].values()))
        self.assertTrue(terminal["result"]["summary"])
        self.assertFalse(terminal["result"]["undo"]["canUndo"])

    def test_check_database_large_summary_completes_with_explicit_warning(self):
        summary = "Generated repair report\n" * 4000
        with patch.object(self.col, "fix_integrity", return_value=(summary, False)):
            result = self.long("maintenance.checkDatabase")["result"]
        self.assertEqual(len(result["summary"]), maintenance.SUMMARY_LIMIT)
        self.assertTrue(result["summary"].endswith("[Database check summary truncated.]"))
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("truncated", result["warnings"][0])
        self.assertTrue(all(result["changes"].values()))

    def test_empty_cards_and_remove_with_native_undo_redo(self):
        note = self.note("")
        card_id = int(note.cards()[0].id)
        good = self.note("Nonempty question")
        self.assertEqual(self.long("maintenance.emptyCards")["result"]["emptyCards"], {"notes": [
            {"noteId": int(note.id), "cardIds": [card_id], "allCardsEmpty": True}]})
        result = self.long("maintenance.removeEmptyCards", {"cardIds": [card_id, card_id]})["result"]
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["changes"]["card"])
        self.assertTrue(result["changes"]["note"])
        self.assertTrue(result["undo"]["canUndo"])
        self.assertEqual(self.col.find_notes(""), [good.id])
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id)["Front"], "")
        self.assertEqual(self.col.get_card(card_id).nid, note.id)
        self.col.redo()
        self.assertEqual(self.long("maintenance.emptyCards")["result"]["emptyCards"], {"notes": []})

    def test_remove_refuses_nonempty_or_missing_cards_before_mutation(self):
        empty = self.note("").cards()[0].id
        nonempty = self.note("Nonempty").cards()[0].id
        for ids, code in (([empty, nonempty], "invalid_params"), ([empty, 0], "not_found")):
            with self.subTest(ids=ids):
                frame = self.long("maintenance.removeEmptyCards", {"cardIds": ids}, expected="failed")
                self.assertEqual(frame.get("error", {}).get("code"), code, frame)
                self.assertEqual(self.col.card_count(), 2)
        result = self.long("maintenance.removeEmptyCards", {"cardIds": []})["result"]
        self.assertEqual(result["count"], 0)
        self.assertFalse(any(result["changes"].values()))

    def test_partial_empty_note_retains_its_nonempty_card(self):
        note = self.col.new_note(self.col.models.by_name("Basic (and reversed card)"))
        note["Front"], note["Back"] = "Generated question", "Generated answer"
        self.col.add_note(note, 1)
        cards = {card.ord: int(card.id) for card in note.cards()}
        note["Back"] = ""
        self.col.update_note(note)
        self.assertEqual(self.long("maintenance.emptyCards")["result"]["emptyCards"], {"notes": [
            {"noteId": int(note.id), "cardIds": [cards[1]], "allCardsEmpty": False}]})
        self.assertEqual(self.long("maintenance.removeEmptyCards", {"cardIds": [cards[1]]})["result"]["count"], 1)
        self.assertEqual([int(card.id) for card in self.col.get_note(note.id).cards()], [cards[0]])
        self.col.undo()
        self.assertEqual(len(self.col.get_note(note.id).cards()), 2)

    def test_empty_card_scan_abort_prevents_removal_and_late_abort_preserves_result(self):
        note = self.note("")
        card_id = int(note.cards()[0].id)
        scan = maintenance.scan_empty_cards
        def abort_after_scan(engine):
            report = scan(engine)
            self.engine.progress.abort(self.engine.progress.current)
            return report
        with patch.object(maintenance, "scan_empty_cards", side_effect=abort_after_scan), \
                patch.object(self.col, "remove_cards_and_orphaned_notes", wraps=self.col.remove_cards_and_orphaned_notes) as remove:
            terminal = self.long("maintenance.removeEmptyCards", {"cardIds": [card_id]}, expected="aborted")
            remove.assert_not_called()
        self.assertIsNone(terminal["result"])
        self.assertEqual(self.col.find_notes(""), [note.id])
        remove = self.col.remove_cards_and_orphaned_notes
        def abort_after_commit(ids):
            result = remove(ids)
            self.engine.progress.abort(self.engine.progress.current)
            return result
        with patch.object(self.col, "remove_cards_and_orphaned_notes", side_effect=abort_after_commit):
            result = self.long("maintenance.removeEmptyCards", {"cardIds": [card_id]})["result"]
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["changes"]["card"])
        self.assertTrue(result["undo"]["canUndo"])
        self.col.undo()
        self.assertEqual(self.col.find_notes(""), [note.id])

    def test_schema_invalid_params(self):
        cases = [("emptyCards", {"extra": 1}), ("checkDatabase", {"extra": 1}),
                 ("backupNow", {"extra": 1}), ("backups", {"extra": 1}),
                 ("removeEmptyCards", {"cardIds": [True]}),
                 ("restoreBackup", {}), ("restoreBackup", {"backupId": 1}),
                 ("restoreBackup", {"backupId": "x", "extra": 1})]
        for method, params in cases:
            with self.subTest(method=method, params=params):
                self.assertEqual(self.frame("maintenance." + method, params)["error"]["code"], "invalid_params")

    def test_native_backup_completed_list_and_no_change(self):
        self.note("Backup question")
        terminal = self.long("maintenance.backupNow")
        self.assertFalse(any(terminal["result"]["changes"].values()))
        backups = self.rpc("maintenance.backups")["backups"]
        self.assertEqual(len(backups), 1)
        entry = backups[0]
        path = Path(self.col.path).parent / "backups" / entry["id"]
        self.assertEqual(entry["bytes"], path.stat().st_size)
        self.assertGreater(entry["createdAt"], 0)
        self.assertNotIn(str(path.parent), json.dumps(backups))
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("collection.anki21b", archive.namelist())
        with patch.object(self.col, "create_backup", wraps=self.col.create_backup) as create:
            self.assertEqual(maintenance.force_backup(self.engine), path)
            self.assertTrue(create.call_args.kwargs["force"])
            self.assertTrue(create.call_args.kwargs["wait_for_completion"])

    def test_verified_backup_preserves_success_after_late_abort(self):
        self.note("Generated late-abort backup question")
        force = maintenance.force_backup
        completed = []
        def abort_after_verified_backup(engine):
            path = force(engine)
            completed.append(path)
            self.engine.progress.abort(self.engine.progress.current)
            return path
        with patch.object(maintenance, "force_backup", side_effect=abort_after_verified_backup):
            terminal = self.long("maintenance.backupNow")
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0].is_file())
        self.assertIn(completed[0].name, [entry["id"] for entry in self.rpc("maintenance.backups")["backups"]])
        self.assertEqual(terminal["result"]["summary"], "Collection backup completed.")
        self.assertFalse(any(terminal["result"]["changes"].values()))

    def test_native_backup_proof_interruption_stays_aborted_and_cleans_scratch(self):
        note = self.note("Generated interrupted-proof question")
        with patch.object(handlers.transfer, "_restore",
                          side_effect=anki_errors.Interrupted("private native diagnostic", None, None, None)) as restore:
            terminal = self.long("maintenance.backupNow", expected="aborted")
        restore.assert_called_once()
        self.assertIsNone(terminal["result"])
        self.assertEqual(terminal["error"]["code"], "aborted")
        self.assertNotIn("private native diagnostic", json.dumps(terminal))
        self.assertEqual(self.col.get_note(note.id)["Front"], "Generated interrupted-proof question")
        self.assertEqual(list(Path(self.col.path).parent.glob("srs-verify-backup-*")), [])

    def test_backup_false_without_valid_archive_refuses(self):
        with patch.object(self.col, "create_backup", return_value=False):
            with self.assertRaises(EngineError):
                maintenance.force_backup(self.engine)
        folder = Path(self.col.path).parent / "backups"
        (folder / "backup-invalid.colpkg").write_bytes(b"not a completed archive")
        with patch.object(self.col, "create_backup", return_value=False):
            with self.assertRaises(EngineError):
                maintenance.force_backup(self.engine)

    def test_backup_valid_zip_with_unrestorable_database_refuses(self):
        self.note("Keep the current collection")
        path = maintenance.force_backup(self.engine)
        # An envelope/CRC-only check accepts this, but it cannot restore a DB.
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("collection.anki21", b"not a SQLite database")
            archive.writestr("media", b"{}")
        with patch.object(self.col, "create_backup", return_value=False):
            with self.assertRaises(EngineError) as error:
                maintenance.force_backup(self.engine)
        self.assertEqual(error.exception.code, "internal")
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(list(Path(self.col.path).parent.glob("srs-verify-backup-*")), [])

    def test_backup_true_without_completed_file_refuses(self):
        maintenance.force_backup(self.engine)
        with patch.object(self.col, "create_backup", return_value=True):
            with self.assertRaises(EngineError):
                maintenance.force_backup(self.engine)

    def test_unchanged_native_backup_refuses_tampered_archive(self):
        path = maintenance.force_backup(self.engine)
        with zipfile.ZipFile(path) as archive:
            entry = archive.getinfo("collection.anki21b")
            offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
        with path.open("r+b") as stream:
            stream.seek(offset)
            byte = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([byte[0] ^ 1]))
        with self.assertRaises(EngineError) as raised:
            maintenance.force_backup(self.engine)
        self.assertEqual(raised.exception.code, "internal")

    def test_backup_directory_link_refused_without_writing_target(self):
        folder = Path(self.col.path).parent / "backups"
        folder.rmdir()
        target = folder.parent / "outside"
        target.mkdir()
        folder.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.frame("maintenance.backups")["error"]["code"], "invalid_params")
        self.long("maintenance.backupNow", expected="failed")
        self.assertEqual(list(target.iterdir()), [])
        clock = FakeClock()
        with self.assertLogs(self.engine.logger, level="ERROR") as logs:
            other = Engine(self.col, Registry(), FrameWriter(io.BytesIO()), self.engine.logger, clock)
            clock.advance(1)
            other.run_idle_hooks()
        self.assertEqual(len(logs.output), 1, "An unavailable directory must not be retried each idle tick")

    def test_restore_round_trip_preserves_collection_object_and_safety_backup(self):
        original = self.note("Original question")
        path = maintenance.force_backup(self.engine)
        self.note("Added after backup")
        before = self.col
        with patch.object(self.col, "create_backup", wraps=self.col.create_backup) as create:
            self.long("maintenance.restoreBackup", {"backupId": path.name})
        self.assertIs(self.engine.col, before)
        self.assertEqual(self.col.find_notes(""), [original.id])
        self.assertEqual(self.col.get_note(original.id)["Front"], "Original question")
        self.assertTrue(any(call.kwargs["force"] and call.kwargs["wait_for_completion"]
                            for call in create.call_args_list))
        # Native backups have second-resolution names and may replace the source
        # name. Restoring must preserve its bytes before the safety backup runs.
        safety_backup = Path(self.col.path).parent / "backups" / self.rpc("maintenance.backups")["backups"][0]["id"]
        with temp_collection() as target:
            other = Engine(target, Registry(), FrameWriter(io.BytesIO()), self.engine.logger, FakeClock())
            handlers.transfer.import_collection(other, safety_backup)
            self.assertEqual(target.note_count(), 2)
            self.assertEqual(target.get_note(original.id)["Front"], "Original question")

    def test_restore_refuses_backup_failure_and_keeps_current_data(self):
        path = maintenance.force_backup(self.engine)
        added = self.note("Must survive refusal")
        with patch.object(self.col, "create_backup", side_effect=OSError("private diagnostic")):
            terminal = self.long("maintenance.restoreBackup", {"backupId": path.name}, "failed")
        self.assertEqual(terminal["error"]["code"], "internal")
        self.assertNotIn("private diagnostic", json.dumps(terminal))
        self.assertEqual(self.col.get_note(added.id)["Front"], "Must survive refusal")

    def test_restore_internal_missing_file_is_not_a_missing_backup(self):
        self.note("Generated question")
        path = maintenance.force_backup(self.engine)
        with patch.object(handlers.transfer, "import_collection", side_effect=FileNotFoundError("private path")):
            terminal = self.long("maintenance.restoreBackup", {"backupId": path.name}, "failed")
        self.assertEqual(terminal["error"], {"code": "internal",
            "message": "The engine could not complete the request."})
        self.assertEqual(self.col.note_count(), 1)

    def test_restore_rejects_malformed_missing_and_link_ids(self):
        for backup_id in ("", ".", "..", "../backup.colpkg", "/backup.colpkg", "x\\backup.colpkg",
                          "backup\x00.colpkg", "backup\n.colpkg", "backup\x85.colpkg", "\ud800.colpkg",
                          "é" * 125 + ".colpkg", ".hidden.colpkg", "wrong.txt"):
            with self.subTest(backup_id=backup_id):
                terminal = self.long("maintenance.restoreBackup", {"backupId": backup_id}, "failed")
                self.assertEqual(terminal["error"]["code"], "invalid_params")
        terminal = self.long("maintenance.restoreBackup", {"backupId": "missing.colpkg"}, "failed")
        self.assertEqual(terminal["error"]["code"], "not_found")
        folder = Path(self.col.path).parent / "backups"
        link = folder / "backup-link.colpkg"
        link.symlink_to(Path(self.col.path))
        self.assertEqual(self.rpc("maintenance.backups"), {"backups": []})
        terminal = self.long("maintenance.restoreBackup", {"backupId": link.name}, "failed")
        self.assertEqual(terminal["error"]["code"], "invalid_params")

    def test_periodic_interval_busy_and_no_change(self):
        maintenance.initialize(self.engine)
        self.assertEqual(list((Path(self.col.path).parent / "backups").iterdir()), [])
        with patch.object(self.col, "create_backup", wraps=self.col.create_backup) as create:
            self.clock.advance(1799)
            maintenance.periodic_tick(self.engine)
            self.assertEqual(create.call_count, 0)
            self.clock.advance(1)
            with patch.object(type(self.engine.progress), "running", new_callable=PropertyMock,
                              return_value=True):
                maintenance.periodic_tick(self.engine)
            self.assertEqual(create.call_count, 0)
            maintenance.periodic_tick(self.engine)
            self.assertEqual(create.call_count, 1)
            self.assertFalse(create.call_args.kwargs["force"])
            self.assertFalse(create.call_args.kwargs["wait_for_completion"])
            self.col.await_backup_completion()
            self.clock.advance(1799)
            maintenance.periodic_tick(self.engine)
            self.assertEqual(create.call_count, 1)
            self.clock.advance(1)
            maintenance.periodic_tick(self.engine)
            self.assertEqual(create.call_count, 2)
            self.col.await_backup_completion()
            self.assertEqual(len(self.rpc("maintenance.backups")["backups"]), 1)
            maintenance.periodic_tick(self.engine)
            self.assertEqual(create.call_count, 2)

    def test_periodic_failure_is_throttled_and_collection_remains_usable(self):
        maintenance.initialize(self.engine)
        self.clock.advance(1800)
        with patch.object(self.col, "create_backup", side_effect=OSError("private diagnostic")) as create:
            maintenance.periodic_tick(self.engine)
            maintenance.periodic_tick(self.engine)
            self.assertEqual(create.call_count, 1)
        self.note("Still usable")
        self.assertEqual(self.col.card_count(), 1)

    def test_periodic_is_registered_with_engine_idle_loop(self):
        self.assertTrue(hasattr(self.engine, "_maintenance_backups"))
        with patch.object(self.col, "create_backup", wraps=self.col.create_backup) as create:
            self.clock.advance(1799)
            self.engine.run_idle_hooks()
            self.assertEqual(create.call_count, 0)
            self.clock.advance(1)
            self.engine.run_idle_hooks()
            self.assertEqual(create.call_count, 1)
            self.assertFalse(create.call_args.kwargs["force"])

    def test_periodic_does_not_overlap_forced_backup(self):
        maintenance.initialize(self.engine)
        entered, resume = threading.Event(), threading.Event()
        native = self.col.create_backup
        errors = []

        def blocked(**kwargs):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("Fixture did not resume")
            return native(**kwargs)

        def force():
            try:
                maintenance.force_backup(self.engine)
            except Exception as error:
                errors.append(error)

        with patch.object(self.col, "create_backup", side_effect=blocked) as create:
            thread = threading.Thread(target=force)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.clock.advance(1800)
                maintenance.periodic_tick(self.engine)
                self.assertEqual(create.call_count, 1)
            finally:
                resume.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_long_methods_honor_real_abort_before_native_work(self):
        path = maintenance.force_backup(self.engine)
        for name, params in (("checkDatabase", {}), ("emptyCards", {}), ("removeEmptyCards", {"cardIds": [0]}), ("backupNow", {}),
                             ("restoreBackup", {"backupId": path.name})):
            with self.subTest(name=name):
                full_name = "maintenance." + name
                original = self.engine.registry.handlers[full_name]
                entered, resume = threading.Event(), threading.Event()

                def delayed(engine, value):
                    entered.set()
                    if not resume.wait(5):
                        raise TimeoutError("Fixture did not resume")
                    return original(engine, value)

                if name == "restoreBackup":
                    target = handlers.transfer
                    native_name = "import_collection"
                else:
                    target = maintenance if name in ("emptyCards", "removeEmptyCards") else self.col
                    native_name = ("fix_integrity" if name == "checkDatabase" else
                                   "scan_empty_cards" if name in ("emptyCards", "removeEmptyCards") else "create_backup")
                with patch.object(target, native_name, wraps=getattr(target, native_name)) as native_call, \
                        patch.dict(self.engine.registry.handlers, {full_name: delayed}):
                    ack = self.rpc(full_name, params)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.rpc("progress.abort", {"operationId": ack["operationId"]})
                    finally:
                        resume.set()
                        self.engine.progress.thread.join(timeout=5)
                    native_call.assert_not_called()
                terminal = self.rpc("progress.get", {"operationId": ack["operationId"]})
                self.assertEqual(terminal["status"], "aborted", terminal)
                self.assertEqual(terminal["error"]["code"], "aborted")
