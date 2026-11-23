"""Native full-media recovery and refusal before destructive replacement."""
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from anki import errors as anki_errors
from anki.collection import Collection, media_paths_from_col_path

from memr_srs_engine.errors import EngineError
from memr_srs_engine.handlers import maintenance, recovery, transfer
from memr_srs_engine.paths import mutation, safe_directory
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memr-recovery-test-")
        self.root = Path(self.temp.name)
        self.col = Collection(str(self.root / "collection.anki2"))
        logger = logging.getLogger("e05-recovery-tests")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.output = io.BytesIO()
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger)
        self.sequence = 0
        self.note = self.col.new_note(self.col.models.by_name("Basic"))
        self.note["Front"] = 'Original <img src="referenced.bin">'
        self.col.add_note(self.note, 1)
        self.media = Path(self.col.media.dir())
        self.files = {"referenced.bin": b"original\0\xff", "unreferenced.bin": b"unreferenced original",
                      "_support.css": b"static original", "é.txt": b"unicode original", "empty.bin": b""}
        for name, value in self.files.items():
            (self.media / name).write_bytes(value)
        nested = self.media / "nested"
        nested.mkdir()
        (nested / "kept.bin").write_bytes(b"nested original")
        trash = self.root / "media.trash"
        trash.mkdir()
        (trash / "kept.bin").write_bytes(b"trash original")

    def tearDown(self):
        self.engine.progress.close()
        self.col.close()
        self.temp.cleanup()

    def rpc(self, name, params=None):
        self.sequence += 1
        start = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": name, "params": params or {}}).encode())
        frames = [json.loads(line) for line in self.output.getvalue()[start:].splitlines()]
        for frame in frames:
            if frame.get("event"):
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], frame["progress"]), [])
        responses = [frame for frame in frames if not frame.get("event") and frame.get("id") == self.sequence]
        self.assertEqual(len(responses), 1, frames)
        self.assertIn("result", responses[0], responses[0])
        return responses[0]["result"]

    def long(self, name, params=None, status="completed"):
        ack = self.rpc(name, params)
        self.engine.progress.thread.join(10)
        self.assertFalse(self.engine.progress.thread.is_alive())
        terminal = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(terminal["status"], status, terminal)
        self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], terminal), [])
        return terminal

    def prepare(self, status="completed", force=True):
        def backup(engine, _params):
            path = (maintenance.force_backup(engine, include_media=True) if force
                    else recovery.create_recovery_backup(engine))
            return mutation(engine.col, summary=path.name)
        with patch.dict(self.engine.registry.handlers, {"maintenance.backupNow": backup}):
            terminal = self.long("maintenance.backupNow", status=status)
        return self.root / "backups" / terminal["result"]["summary"] if status == "completed" else terminal

    def originals(self):
        self.assertEqual(self.col.get_note(self.note.id)["Front"], self.note["Front"])
        self.assertEqual({name: (self.media / name).read_bytes() for name in self.files}, self.files)

    def no_scratch(self):
        self.assertEqual([path.name for path in self.root.glob("srs-recovery-*")], [])

    def source_hashes(self):
        return {path.name: digest(path) for path in (Path(self.col.path), Path(self.col.path + "-wal")) if path.exists()}

    def cache_hashes(self):
        path = Path(media_paths_from_col_path(self.col.path)[1])
        return {file.name: digest(file) for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
                if file.exists()}

    def test_retained_native_round_trip_includes_unreferenced_static_unicode_empty_media(self):
        undo, before = self.col.undo_status(), self.source_hashes()
        path = self.prepare(force=False)
        self.assertTrue(path.name.startswith("recovery-"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.col.undo_status(), undo)
        self.assertEqual(self.source_hashes(), before)
        self.assertIn(path.name, [entry["id"] for entry in self.rpc("maintenance.backups")["backups"]])
        retained = digest(path)
        with transfer.restored_collection(self.engine, path) as proof:
            self.assertEqual(proof.get_note(self.note.id)["Front"], self.note["Front"])
            directory = Path(proof.media.dir())
            self.assertEqual({file.name: file.read_bytes() for file in directory.iterdir()}, self.files)
        for name in self.files:
            (self.media / name).write_bytes(b"overwritten bytes")
        (self.media / "introduced.bin").write_bytes(b"new unused file")
        note = self.col.get_note(self.note.id)
        note["Front"] = "Changed current note"
        self.col.update_note(note)
        self.long("maintenance.restoreBackup", {"backupId": path.name})
        self.originals()
        self.assertEqual((self.media / "introduced.bin").read_bytes(), b"new unused file")
        self.assertEqual((self.media / "nested" / "kept.bin").read_bytes(), b"nested original")
        self.assertEqual((self.root / "media.trash" / "kept.bin").read_bytes(), b"trash original")
        self.assertEqual(digest(path), retained)
        self.no_scratch()

    def test_native_no_db_change_still_creates_fresh_unique_media_recovery(self):
        first = self.prepare()
        (self.media / "unreferenced.bin").write_bytes(b"media-only change")
        with patch.object(self.col, "create_backup", wraps=self.col.create_backup) as native:
            second = self.prepare()
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertEqual(native.call_count, 1)
        with transfer.restored_collection(self.engine, second) as proof:
            self.assertEqual(Path(proof.media.dir(), "unreferenced.bin").read_bytes(), b"media-only change")

    def test_unsafe_media_refused_before_snapshot_or_copy(self):
        cases = ("symlink", "backslash", "control", "unnormalized", "fifo")
        for case in cases:
            with self.subTest(case=case):
                bad = self.media / {"symlink": "link.bin", "backslash": "bad\\name", "control": "bad\nname",
                                    "unnormalized": "e\u0301.txt", "fifo": "pipe"}[case]
                if case == "symlink":
                    bad.symlink_to(self.media / "referenced.bin")
                elif case == "fifo":
                    if not hasattr(os, "mkfifo"):
                        continue
                    os.mkfifo(bad)
                else:
                    bad.write_bytes(b"unsafe generated media")
                try:
                    with patch.object(recovery, "_snapshot", side_effect=AssertionError("Unsafe snapshot")), \
                            patch.object(recovery, "_copy_file", side_effect=AssertionError("Unsafe copy")):
                        with self.assertRaises(EngineError) as caught:
                            recovery.create_recovery_backup(self.engine)
                    self.assertEqual(caught.exception.code, "invalid_params")
                    self.originals()
                    self.no_scratch()
                finally:
                    bad.unlink()

    def test_low_storage_refuses_before_snapshot(self):
        with patch.object(recovery.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), \
                patch.object(recovery, "_snapshot", side_effect=AssertionError("Copied despite low storage")):
            with self.assertRaises(EngineError) as caught:
                recovery.create_recovery_backup(self.engine)
        self.assertEqual(caught.exception.code, "internal")
        self.originals()
        self.no_scratch()

    def test_copy_cancellation_preserves_live_undo_and_publishes_no_recovery(self):
        before, undo = self.source_hashes(), self.col.undo_status()
        original = recovery._copy_file
        def cancel(*args):
            self.engine.progress.abort(self.engine.progress.current)
            return original(*args)
        with patch.object(recovery, "_copy_file", side_effect=cancel):
            terminal = self.prepare("aborted", force=False)
        self.assertEqual(terminal["error"]["code"], "aborted")
        self.assertEqual(self.col.undo_status(), undo)
        self.assertEqual(self.source_hashes(), before)
        self.assertEqual(list((self.root / "backups").glob("recovery-*.colpkg")), [])
        self.originals()
        self.no_scratch()

    def test_changed_source_file_refuses_and_cleans_private_copies(self):
        original = recovery._copy_file
        changed = []
        def change_after_copy(engine, path, info, destination):
            result = original(engine, path, info, destination)
            if not changed:
                path.write_bytes(b"concurrent source change")
                changed.append(path)
            return result
        undo = self.col.undo_status()
        with patch.object(recovery, "_copy_file", side_effect=change_after_copy):
            terminal = self.prepare("failed")
        self.assertEqual(terminal["error"]["code"], "conflict")
        self.assertEqual(self.col.undo_status(), undo)
        self.assertEqual(changed[0].read_bytes(), b"concurrent source change")
        self.assertEqual(list((self.root / "backups").glob("recovery-*.colpkg")), [])
        self.no_scratch()

    def test_native_scratch_export_failure_preserves_source_and_undo(self):
        undo, before = self.col.undo_status(), self.source_hashes()
        native = Collection.export_collection_package
        def fail(clone, *args, **kwargs):
            self.assertIsNot(clone, self.col)
            return native(clone, str(self.root / "missing" / "file.colpkg"), **kwargs)
        with patch.object(Collection, "export_collection_package", fail):
            self.prepare("failed", force=False)
        self.assertEqual(self.col.undo_status(), undo)
        self.assertEqual(self.source_hashes(), before)
        self.assertEqual(list((self.root / "backups").glob("recovery-*.colpkg")), [])
        self.originals()
        self.no_scratch()

    def test_failed_media_hash_proof_refuses_before_publication(self):
        with patch.object(recovery, "_hash_file", return_value=(1, b"invalid hash")):
            terminal = self.prepare("failed")
        self.assertEqual(terminal["error"]["code"], "internal")
        self.assertEqual(list((self.root / "backups").glob("recovery-*.colpkg")), [])
        self.originals()
        self.no_scratch()

    def test_existing_recovery_name_is_never_overwritten(self):
        selected = uuid.uuid4()
        path = safe_directory(self.engine, "backups") / f"recovery-{selected}.colpkg"
        path.write_bytes(b"existing retained backup")
        with patch.object(recovery.uuid, "uuid4", return_value=selected):
            with self.assertRaises(FileExistsError):
                recovery.create_recovery_backup(self.engine)
        self.assertEqual(path.read_bytes(), b"existing retained backup")
        self.originals()
        self.no_scratch()

    def test_abort_before_publication_removes_only_own_empty_reservation(self):
        original = recovery.checkpoint
        def abort_at_reservation(engine):
            if any(path.stat().st_size == 0 for path in (self.root / "backups").glob("recovery-*.colpkg")):
                engine.progress.abort(engine.progress.current)
            return original(engine)
        with patch.object(recovery, "checkpoint", side_effect=abort_at_reservation):
            self.prepare("aborted")
        self.assertEqual(list((self.root / "backups").glob("recovery-*.colpkg")), [])
        self.originals()
        self.no_scratch()

    def source_package(self):
        source_root = self.root / "source"
        source_root.mkdir()
        source = Collection(str(source_root / "collection.anki2"))
        upload = safe_directory(self.engine, "incoming") / (str(uuid.uuid4()) + ".colpkg")
        try:
            note = source.new_note(source.models.by_name("Basic"))
            note["Front"] = 'Imported <img src="referenced.bin">'
            source.add_note(note, 1)
            Path(source.media.dir(), "referenced.bin").write_bytes(b"imported media")
            source.export_collection_package(str(upload), include_media=True, legacy=False)
            source.reopen()
        finally:
            source.close()
        return {"uploadId": upload.name, "mergeNotetypes": True, "updateNotes": "always", "updateNotetypes": "always",
                "includeScheduling": True, "includeDeckConfigs": True}

    def test_actual_primary_publication_then_failure_retains_complete_recoverable_originals(self):
        params = self.source_package()
        native = self.col._backend.import_collection_package
        published, indexes = [], []
        cache = self.cache_hashes()
        def fail_after_publication(**kwargs):
            indexes.append(Path(kwargs["media_db"]))
            result = native(**kwargs)
            published.append((self.media / "referenced.bin").read_bytes())
            raise anki_errors.BackendIOError("Generated post-publication failure", None, None, None)
        with patch.object(self.col._backend, "import_collection_package", side_effect=fail_after_publication):
            failed = self.long("transfer.importPackage", params, "failed")
        self.assertEqual(published, [b"imported media"])
        self.assertEqual(self.cache_hashes(), cache)
        self.assertEqual(len(indexes), 1)
        self.assertNotEqual(str(indexes[0]), media_paths_from_col_path(self.col.path)[1])
        self.assertFalse(indexes[0].parent.exists())
        self.assertEqual(failed["error"]["code"], "internal")
        self.assertNotIn(str(self.root), json.dumps(failed))
        self.assertNotIn("Generated post-publication failure", json.dumps(failed))
        archives = list((self.root / "backups").glob("recovery-*.colpkg"))
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        self.assertIn(archive.name, failed["error"]["message"])
        retained = digest(archive)
        self.long("maintenance.restoreBackup", {"backupId": archive.name})
        self.originals()
        self.assertEqual(digest(archive), retained)
        self.no_scratch()

    def test_abort_after_recovery_publication_retains_identifier_before_primary_write(self):
        params = self.source_package()
        native = maintenance.force_backup
        retained = []
        def abort_after_backup(engine, **kwargs):
            path = native(engine, **kwargs)
            retained.append(path)
            engine.progress.abort(engine.progress.current)
            return path
        with patch.object(maintenance, "force_backup", side_effect=abort_after_backup):
            terminal = self.long("transfer.importPackage", params, "aborted")
        self.assertEqual(len(retained), 1)
        self.assertTrue(retained[0].exists())
        self.assertIn(retained[0].name, terminal["error"]["message"])
        self.assertNotIn(str(self.root), json.dumps(terminal))
        self.originals()
        self.no_scratch()

    def test_recovery_failure_refuses_before_any_primary_restore(self):
        params = self.source_package()
        native = transfer._restore
        primary = []
        def record(col, *args, **kwargs):
            if col is self.col:
                primary.append(True)
            return native(col, *args, **kwargs)
        with patch.object(recovery, "_verify", side_effect=EngineError("internal", "Generated proof failure")), \
                patch.object(transfer, "_restore", side_effect=record):
            self.long("transfer.importPackage", params, "failed")
        self.assertEqual(primary, [])
        self.originals()
        self.no_scratch()
