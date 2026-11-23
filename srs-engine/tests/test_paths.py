"""Durable publication of export files, from the folder entry down to the bytes."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memr_srs_engine.paths import export_destination, safe_directory


class ExportPublishDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memr-paths-")
        self.root = Path(self.temp.name)
        self.engine = SimpleNamespace(col=SimpleNamespace(path=str(self.root / "collection.anki2")),
                                      progress=SimpleNamespace(check_abort=lambda: None))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def identity(path):
        info = os.stat(path)
        return info.st_dev, info.st_ino

    def record(self, events, fail_directories=False):
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                events.append(("fsync-directory", (info.st_dev, info.st_ino)))
                if fail_directories:
                    raise OSError(5, "simulated directory sync failure")
            else:
                events.append(("fsync-file", (info.st_dev, info.st_ino)))
            real_fsync(fd)

        def replace(source, destination):
            events.append(("replace", None))
            real_replace(source, destination)

        return patch("memr_srs_engine.paths.os.fsync", fsync), patch("memr_srs_engine.paths.os.replace", replace)

    @unittest.skipIf(os.name == "nt", "Windows cannot open a directory to synchronize it.")
    def test_publish_syncs_the_token_folder_and_the_exports_directory_after_the_rename(self):
        events = []
        fsync, replace = self.record(events)
        with fsync, replace, export_destination(self.engine, "deck.apkg") as (partial, publish):
            partial.write_bytes(b"package bytes")
            folder = partial.parent
            exports = safe_directory(self.engine, "exports")
            published = publish()
        self.assertEqual(published["bytes"], len(b"package bytes"))
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["fsync-file", "replace", "fsync-directory", "fsync-directory"], events)
        self.assertEqual([identity for kind, identity in events if kind == "fsync-directory"],
                         [self.identity(folder), self.identity(exports)],
                         "the renamed file's folder, then the folder's own entry in exports/")
        self.assertTrue((folder / "deck.apkg").is_file())

    @unittest.skipIf(os.name == "nt", "Windows cannot open a directory to synchronize it.")
    def test_publish_failing_to_sync_the_directory_fails_the_export_and_leaves_nothing_behind(self):
        events = []
        fsync, replace = self.record(events, fail_directories=True)
        with fsync, replace:
            with self.assertRaises(OSError):
                with export_destination(self.engine, "deck.apkg") as (partial, publish):
                    partial.write_bytes(b"package bytes")
                    folder = partial.parent
                    publish()
        self.assertIn("fsync-directory", [kind for kind, _ in events])
        self.assertFalse(folder.exists(), "an export whose rename is not durable is not offered for download")


if __name__ == "__main__":
    unittest.main()
