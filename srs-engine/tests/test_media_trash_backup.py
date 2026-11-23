"""Trash deletion must retain a byte-verified, natively restorable backup."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from memr_srs_engine.errors import EngineError
from memr_srs_engine.handlers.transfer import restored_collection
from tests.test_media import RpcCase, PNG, AUDIO
from tests.helpers import EnginePipe
from memr_srs_engine.handlers import recovery


class MediaTrashBackupTests(RpcCase):
    def test_empty_trash_preserves_exact_bytes_in_retained_backup(self):
        name = self.add_media("retained.png", PNG)
        note = self.note("Retained note")
        self.rpc("media.trashUnused", {"filenames": [name]})
        self.add_media(name, AUDIO)
        before = {item["id"] for item in self.rpc("maintenance.backups")["backups"]}
        result = self.rpc("media.emptyTrash")
        self.assertEqual(result["count"], 1)
        retained = [item for item in self.rpc("maintenance.backups")["backups"] if item["id"] not in before]
        self.assertEqual(len(retained), 1, "Emptying trash must first retain a restorable backup")
        self.assertTrue(retained[0]["id"].startswith("media-trash-recovery-"))
        backup = Path(self.col.path).parent / "backups" / retained[0]["id"]
        with restored_collection(self.engine, backup) as restored:
            self.assertEqual((Path(restored.media.dir()) / name).read_bytes(), PNG)
            self.assertEqual(restored.get_note(note.id)["Front"], "Retained note")
        self.assertEqual((self.media_dir() / name).read_bytes(), AUDIO)
        self.assertEqual(self.rpc("media.trash")["files"], [])

    def test_failed_verification_preserves_trash(self):
        name = self.add_media("preserved.png", PNG)
        self.rpc("media.trashUnused", {"filenames": [name]})
        with patch("memr_srs_engine.handlers.recovery._verify", side_effect=EngineError("internal", "Generated verification failure")):
            self.error("media.emptyTrash", {}, "internal")
        self.assertEqual((self.trash_dir() / name).read_bytes(), PNG)
        self.assertEqual(self.rpc("maintenance.backups")["backups"], [])

    def prepare_trash(self):
        name = self.add_media("verified-trash.png", PNG)
        self.rpc("media.trashUnused", {"filenames": [name]})
        return name

    def test_long_operation_publishes_backup_identity_and_empty_is_a_noop(self):
        name = self.prepare_trash()
        state = self.operation("media.emptyTrashWithBackup", {"filenames": [name]})
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["result"]["count"], 1)
        backup = self.rpc("maintenance.backups")["backups"][0]
        self.assertIn(backup["id"], state["result"]["summary"])
        before = self.rpc("maintenance.backups")
        again = self.operation("media.emptyTrashWithBackup", {"filenames": []})
        self.assertEqual(again["status"], "completed")
        self.assertEqual(again["result"]["count"], 0)
        self.assertEqual(self.rpc("maintenance.backups"), before)

    def test_changed_or_duplicate_confirmation_never_begins_backup(self):
        name = self.prepare_trash()
        for names in ([], ["another-file.png"], [name, name]):
            with self.subTest(names=names), patch.object(recovery, "create_media_trash_backup", side_effect=AssertionError("Unconfirmed inventory backed up")):
                state = self.operation("media.emptyTrashWithBackup", {"filenames": names})
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error"]["code"], "conflict")
            self.assertEqual((self.trash_dir() / name).read_bytes(), PNG)

    def test_long_backup_verification_failure_never_deletes(self):
        name = self.prepare_trash()
        with patch.object(recovery, "_verify", side_effect=EngineError("internal", "Generated verification failure")):
            state = self.operation("media.emptyTrashWithBackup", {"filenames": [name]})
        self.assertEqual(state["status"], "failed")
        self.assertEqual((self.trash_dir() / name).read_bytes(), PNG)
        self.assertEqual(self.rpc("maintenance.backups")["backups"], [])

    def test_cancel_during_copy_preserves_exact_trash_and_undo(self):
        name = self.prepare_trash()
        undo = self.col.undo_status()
        original = recovery._copy_file
        def cancel(*args):
            self.engine.progress.abort(self.engine.progress.current)
            return original(*args)
        with patch.object(recovery, "_copy_file", side_effect=cancel):
            state = self.operation("media.emptyTrashWithBackup", {"filenames": [name]})
        self.assertEqual(state["status"], "aborted")
        self.assertEqual((self.trash_dir() / name).read_bytes(), PNG)
        self.assertEqual(self.col.undo_status(), undo)
        self.assertEqual(self.rpc("maintenance.backups")["backups"], [])
        self.assertEqual(list(Path(self.col.path).parent.glob("srs-recovery-*")), [])

    def test_cancel_after_retained_backup_preserves_trash_and_backup(self):
        name = self.prepare_trash()
        original = recovery.create_media_trash_backup
        def cancel(engine):
            backup = original(engine)
            engine.progress.abort(engine.progress.current)
            return backup
        with patch.object(recovery, "create_media_trash_backup", side_effect=cancel):
            state = self.operation("media.emptyTrashWithBackup", {"filenames": [name]})
        self.assertEqual(state["status"], "aborted")
        self.assertEqual((self.trash_dir() / name).read_bytes(), PNG)
        self.assertEqual(len(self.rpc("maintenance.backups")["backups"]), 1)

    def test_inventory_change_after_publication_prevents_deletion(self):
        name = self.prepare_trash()
        original = recovery.create_media_trash_backup
        def change(engine):
            backup = original(engine)
            (self.trash_dir() / name).write_bytes(AUDIO)
            return backup
        with patch.object(recovery, "create_media_trash_backup", side_effect=change):
            state = self.operation("media.emptyTrashWithBackup", {"filenames": [name]})
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"]["code"], "conflict")
        self.assertEqual((self.trash_dir() / name).read_bytes(), AUDIO)
        self.assertEqual(len(self.rpc("maintenance.backups")["backups"]), 1)

    def test_late_cancellation_cannot_relabel_committed_deletion(self):
        name = self.prepare_trash()
        original = self.col.media.empty_trash
        def cancel_after_native():
            original()
            self.engine.progress.abort(self.engine.progress.current)
        with patch.object(self.col.media, "empty_trash", side_effect=cancel_after_native):
            state = self.operation("media.emptyTrashWithBackup", {"filenames": [name]})
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["result"]["count"], 1)
        self.assertEqual(self.rpc("media.trash")["files"], [])
        self.assertEqual(len(self.rpc("maintenance.backups")["backups"]), 1)


class MediaTrashControlTests(unittest.TestCase):
    def test_native_control_lane_aborts_before_backup_without_deleting(self):
        code = """
import time
from memr_srs_engine import handlers
from memr_srs_engine.registry import HANDLERS
original = HANDLERS['media.emptyTrashWithBackup']
def gated(engine, params):
    deadline = time.monotonic() + 5
    while not engine.progress.abort_requested and time.monotonic() < deadline:
        time.sleep(.01)
    return original(engine, params)
HANDLERS['media.emptyTrashWithBackup'] = gated
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory(prefix="memr-trash-abort-") as directory:
            trash = Path(directory) / "media.trash"
            trash.mkdir()
            (trash / "retained.png").write_bytes(PNG)
            with EnginePipe(directory, code=code) as engine:
                operation_id = engine.call("media.emptyTrashWithBackup", {"filenames": ["retained.png"]})["result"]["operationId"]
                aborted = engine.call("progress.abort", {"operationId": operation_id})
                self.assertIn("result", aborted)
                deadline = time.monotonic() + 10
                while True:
                    state = engine.call("progress.get", {"operationId": operation_id})["result"]
                    if state["status"] != "running":
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                self.assertEqual(state["status"], "aborted")
                self.assertEqual((trash / "retained.png").read_bytes(), PNG)
                self.assertEqual(engine.call("maintenance.backups")["result"]["backups"], [])
