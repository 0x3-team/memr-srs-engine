"""Media RPC contracts exercised against disposable, real Anki collections."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from memr_srs_engine import handlers
from memr_srs_engine.handlers.media import MAX_MEDIA_BYTES
from memr_srs_engine.validate import validate_value
from tests.helpers import EnginePipe
from tests.test_decks import RpcCase as DeckRpcCase

PNG = b"\x89PNG\r\n\x1a\n" + b"generated" * 32
AUDIO = b"OggS" + b"generated" * 32

# Real cancellation runs through the pinned engine process. The gate holds the
# real handler until the abort RPC lands, proving the pre-native checkpoint
# ends the operation instead of running to completion.
MEDIA_ABORT_CODE = """
import threading
from memr_srs_engine import handlers
from memr_srs_engine.registry import HANDLERS
real_check = HANDLERS['media.check']
gate = threading.Event()
def gated(engine, params):
    gate.wait(2.0)
    return real_check(engine, params)
HANDLERS['media.check'] = gated
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""


class RpcCase(DeckRpcCase):
    def frame(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": method,
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

    def operation(self, method="media.check", params=None, operation_id=None):
        """Drive a real background long operation to its terminal state."""
        if operation_id is None:
            operation_id = self.rpc(method, {} if params is None else params)["operationId"]
        deadline = time.monotonic() + 10
        while self.engine.progress.get(operation_id)["status"] == "running":
            self.assertLess(time.monotonic(), deadline, "long operation never finished")
            time.sleep(0.01)
        state = self.engine.progress.get(operation_id)
        schema = self.engine.registry.catalogue["progress.get"]["result"]
        self.assertEqual(validate_value(schema, state), [])
        return state

    def upload(self, data=PNG, extension="png"):
        """Land a completed upload inside <srs>/incoming like the gateway does."""
        upload_id = str(uuid.uuid4()) + (("." + extension) if extension else "")
        incoming = Path(self.col.path).parent / "incoming"
        incoming.mkdir(mode=0o700, exist_ok=True)
        path = incoming / upload_id
        path.write_bytes(data)
        return upload_id

    def add_media(self, name, data=PNG):
        return self.rpc("media.addFromUpload", {"uploadId": self.upload(data), "filename": name})["filename"]

    def note(self, front, deck_id=1):
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"], note["Back"] = front, "Generated answer"
        self.col.add_note(note, deck_id)
        return note

    def trash_dir(self):
        return Path(self.col.path).parent / "media.trash"

    def media_dir(self):
        return Path(self.col.media.dir())


class MediaTests(RpcCase):
    def test_all_methods_registered(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("media.")}
        self.assertEqual(expected, {"media.addFromUpload", "media.check", "media.trashUnused",
                                    "media.trash", "media.emptyTrash", "media.emptyTrashWithBackup", "media.restoreTrash"})
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_add_from_upload_preserves_name_and_bytes(self):
        stored = self.add_media("Generated Card.png", PNG)
        self.assertEqual(stored, "generated card.png")
        self.assertEqual((self.media_dir() / stored).read_bytes(), PNG)
        audio = self.add_media("voice.MP3", AUDIO)
        self.assertEqual(audio, "voice.mp3")
        self.assertEqual((self.media_dir() / audio).read_bytes(), AUDIO)

    def test_add_from_upload_dedupes_conflicts_and_keeps_identical(self):
        first = self.add_media("same.png", PNG)
        identical = self.add_media("same.png", PNG)
        self.assertEqual(identical, first)
        conflict = self.add_media("same.png", AUDIO)
        self.assertTrue(conflict.startswith("same-") and conflict.endswith(".png"), conflict)
        self.assertNotEqual(conflict, first)
        self.assertEqual((self.media_dir() / conflict).read_bytes(), AUDIO)
        self.assertEqual((self.media_dir() / first).read_bytes(), PNG)

    def test_add_from_upload_normalizes_and_truncates_names(self):
        self.assertEqual(self.add_media("weird|name?.png"), "weirdname.png")
        stored = self.add_media("n" * 300 + ".PNG")
        self.assertLessEqual(len(stored.encode("utf-8")), 255)
        self.assertTrue(stored.endswith(".png"))
        self.assertEqual(self.add_media("plain"), "plain")

    def test_add_from_upload_id_and_upload_validation(self):
        for upload_id in ["", "not-a-uuid", str(uuid.uuid4()).upper(), "0" * 36,
                          str(uuid.uuid4()) + ".UPPER", str(uuid.uuid4()) + ".png.upload"]:
            with self.subTest(uploadId=upload_id):
                self.error("media.addFromUpload", {"uploadId": upload_id, "filename": "a.png"}, "invalid_params")
        self.error("media.addFromUpload", {"uploadId": str(uuid.uuid4()), "filename": "a.png"}, "not_found")
        incoming = Path(self.col.path).parent / "incoming"
        incoming.mkdir(mode=0o700, exist_ok=True)
        directory_id = str(uuid.uuid4())
        (incoming / directory_id).mkdir()
        self.error("media.addFromUpload", {"uploadId": directory_id, "filename": "a.png"}, "invalid_params")

    def test_add_from_upload_rejects_unsafe_names_before_touching_media(self):
        upload_id = self.upload()
        for name in ["", "   ", ".", "..", "a/b.png", "a\\b.png", "../x.png", "..\\x.png",
                     "nul\x00.png", "ctrl\x07.png", "del\x7f.png", "\ud800.png"]:
            with self.subTest(name=name):
                self.error("media.addFromUpload", {"uploadId": upload_id, "filename": name}, "invalid_params")
        self.assertEqual(list(self.media_dir().iterdir()), [])

    def test_add_from_upload_size_limit(self):
        key = "MEMR_SRS_UPLOAD_MAX_BYTES"
        previous = os.environ.get(key)
        os.environ[key] = "8"
        try:
            self.error("media.addFromUpload", {"uploadId": self.upload(PNG), "filename": "big.png"},
                       "invalid_params")
            self.assertEqual(self.add_media("small.png", b"tiny"), "small.png")
        finally:
            if previous is None:
                del os.environ[key]
            else:
                os.environ[key] = previous

    def test_sparse_media_limit_and_one_byte_over(self):
        upload_id = self.upload(b"", extension="bin")
        path = Path(self.col.path).parent / "incoming" / upload_id
        for size, admitted in ((MAX_MEDIA_BYTES, True), (MAX_MEDIA_BYTES + 1, False)):
            with self.subTest(size=size):
                with path.open("wb") as file:
                    file.truncate(size)
                observed = []
                def native_add(staged):
                    observed.append(Path(staged).stat().st_size)
                    return "limit.bin"
                # Exercise sparse-file preflight and bounded copying without a
                # 100 MiB protobuf allocation in this admission-boundary test.
                with patch.object(self.col.media, "add_file", side_effect=native_add) as native:
                    if admitted:
                        self.assertEqual(self.rpc("media.addFromUpload", {"uploadId": upload_id,
                            "filename": "limit.bin"})["filename"], "limit.bin")
                        self.assertEqual(observed, [MAX_MEDIA_BYTES])
                    else:
                        with patch("memr_srs_engine.handlers.media.shutil.copyfileobj") as copy:
                            self.error("media.addFromUpload", {"uploadId": upload_id,
                                "filename": "limit.bin"}, "invalid_params")
                            copy.assert_not_called()
                        native.assert_not_called()
                self.assertEqual(list(self.media_dir().iterdir()), [])

    def test_check_clean_collection(self):
        state = self.operation()
        self.assertEqual(state["status"], "completed")
        result = state["result"]
        self.assertEqual((result["missing"], result["unused"], result["warnings"]), ([], [], []))
        self.assertFalse(any(result["changes"].values()))
        self.assertIn("undo", result)

    def test_check_reports_missing_unused_subfolder_and_broken_notes(self):
        used = self.add_media("used.png")
        unused = self.add_media("unused.png", AUDIO)
        os.makedirs(self.media_dir() / "nested")
        self.note(f'<img src="{used}"><img src="gone.png">[sound:gone.mp3]')
        state = self.operation()
        self.assertEqual(state["status"], "completed")
        result = state["result"]
        self.assertEqual(result["missing"], ["gone.mp3", "gone.png"])
        self.assertEqual(result["unused"], [unused])
        self.assertNotIn(used, result["unused"])
        self.assertTrue(any("Broken media references" in warning for warning in result["warnings"]))
        report = "\n".join(result["warnings"])
        self.assertIn("Missing: gone.png", report)
        self.assertIn("Folder: nested", report)

    def test_check_abort_before_native_work(self):
        release = threading.Event()
        real = self.engine.registry.handlers["media.check"]

        def gated(engine, params):
            release.wait(5)
            return real(engine, params)

        self.engine.registry.handlers["media.check"] = gated
        operation_id = self.rpc("media.check")["operationId"]
        self.rpc("progress.abort", {"operationId": operation_id})
        release.set()
        state = self.operation(operation_id=operation_id)
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(state["error"]["code"], "aborted")
        # The engine stays usable and the abort flag does not leak forward.
        self.assertEqual(self.operation()["status"], "completed")

    def test_check_late_abort_is_a_noop(self):
        operation_id = self.operation()["operationId"]
        result = self.rpc("progress.abort", {"operationId": operation_id})
        self.assertIn("changes", result)
        self.assertEqual(self.engine.progress.get(operation_id)["status"], "completed")

    def test_trash_unused_moves_and_lists(self):
        keep = self.add_media("keep.png")
        gone = self.add_media("gone.png", AUDIO)
        self.note(f'<img src="{keep}">')
        result = self.rpc("media.trashUnused", {"filenames": [gone, gone]})
        self.assertEqual(result["count"], 1)
        self.assertFalse((self.media_dir() / gone).exists())
        self.assertTrue((self.media_dir() / keep).exists())
        listing = self.rpc("media.trash")
        self.assertEqual(listing["files"], [{"filename": gone, "bytes": len(AUDIO)}])

    def test_trash_unused_rejects_referenced_missing_and_paths_atomically(self):
        used = self.add_media("used.png")
        free = self.add_media("free.png", AUDIO)
        self.note(f'<img src="{used}">')
        for filenames, code in [([used], "invalid_params"), (["missing.png"], "not_found"),
                                (["../evil.png"], "invalid_params"), (["a\\b.png"], "invalid_params"),
                                (["sub/free.png"], "invalid_params"), ([""], "invalid_params"),
                                (["\ud800.png"], "invalid_params"), ([free, used], "invalid_params"),
                                ([free, "missing.png"], "not_found")]:
            with self.subTest(filenames=filenames):
                self.error("media.trashUnused", {"filenames": filenames}, code)
        link = self.media_dir() / "sneaky.png"
        link.symlink_to(Path(self.col.path))
        self.error("media.trashUnused", {"filenames": ["sneaky.png"]}, "invalid_params")
        link.unlink()
        self.assertTrue((self.media_dir() / free).exists())
        self.assertTrue((self.media_dir() / used).exists())
        self.assertEqual(self.rpc("media.trash")["files"], [])

    def test_trash_unused_empty_selection_and_result_undo(self):
        result = self.rpc("media.trashUnused", {"filenames": []})
        self.assertEqual(result["count"], 0)
        self.assertFalse(any(result["changes"].values()))

    def test_empty_and_restore_report_actual_counts(self):
        first, second = self.add_media("a.png"), self.add_media("b.png", AUDIO)
        self.rpc("media.trashUnused", {"filenames": [first, second]})
        listing = {entry["filename"]: entry["bytes"] for entry in self.rpc("media.trash")["files"]}
        self.assertEqual(listing, {first: len(PNG), second: len(AUDIO)})
        restored = self.rpc("media.restoreTrash")
        self.assertEqual(restored["count"], 2)
        self.assertTrue((self.media_dir() / first).exists())
        self.assertEqual(self.rpc("media.trash")["files"], [])
        self.rpc("media.trashUnused", {"filenames": [first, second]})
        emptied = self.rpc("media.emptyTrash")
        self.assertEqual(emptied["count"], 2)
        remaining = list(self.trash_dir().iterdir()) if self.trash_dir().is_dir() else []
        self.assertEqual(remaining, [])
        self.assertEqual(self.rpc("media.restoreTrash")["count"], 0)
        self.assertEqual(self.rpc("media.emptyTrash")["count"], 0)

    def test_restore_conflict_renames_instead_of_overwriting(self):
        original = self.add_media("clash.png", PNG)
        self.rpc("media.trashUnused", {"filenames": [original]})
        self.add_media("clash.png", AUDIO)
        result = self.rpc("media.restoreTrash")
        self.assertEqual(result["count"], 1)
        names = {path.name: path.read_bytes() for path in self.media_dir().iterdir()}
        self.assertEqual(names.get("clash.png"), AUDIO)
        self.assertEqual(set(names.values()), {PNG, AUDIO})
        self.assertTrue(all(name.startswith("clash") and name.endswith(".png") for name in names))

    def test_trash_refuses_link_and_directory_entries(self):
        target = self.add_media("loose.png")
        self.rpc("media.trashUnused", {"filenames": [target]})
        link = self.trash_dir() / "linked.png"
        link.symlink_to(Path(self.col.path))
        for method in ("media.trash", "media.emptyTrash", "media.restoreTrash"):
            with self.subTest(method=method):
                self.error(method, {}, "invalid_params")
        link.unlink()
        (self.trash_dir() / "folder").mkdir()
        self.error("media.restoreTrash", {}, "invalid_params")
        (self.trash_dir() / "folder").rmdir()
        self.assertEqual(self.rpc("media.trash")["files"], [{"filename": target, "bytes": len(PNG)}])

    def test_trash_dir_link_is_refused(self):
        self.trash_dir().symlink_to(self.media_dir())
        for method in ("media.trash", "media.emptyTrash", "media.restoreTrash"):
            with self.subTest(method=method):
                self.error(method, {}, "invalid_params")
        self.trash_dir().unlink()
        self.assertEqual(self.rpc("media.trash")["files"], [])

    def test_trash_listing_has_no_side_effects(self):
        self.assertEqual(self.rpc("media.trash")["files"], [])
        self.assertFalse(self.trash_dir().exists())

    def test_trashed_file_is_no_longer_unused(self):
        name = self.add_media("taken.png")
        self.rpc("media.trashUnused", {"filenames": [name]})
        self.error("media.trashUnused", {"filenames": [name]}, "not_found")

    def test_invalid_params_for_every_method(self):
        upload_id = str(uuid.uuid4())
        cases = [("addFromUpload", {}), ("addFromUpload", {"uploadId": upload_id}),
                 ("addFromUpload", {"uploadId": upload_id, "filename": 3}),
                 ("addFromUpload", {"uploadId": upload_id, "filename": "a.png", "extra": 1}),
                 ("trashUnused", {}), ("trashUnused", {"filenames": "a.png"}),
                 ("trashUnused", {"filenames": [3]}), ("trashUnused", {"filenames": ["a.png"], "extra": 1}),
                 ("trash", {"extra": 1}), ("emptyTrash", {"extra": 1}),
                 ("restoreTrash", {"extra": 1}), ("check", {"extra": 1})]
        for suffix, params in cases:
            with self.subTest(method=suffix, params=params):
                self.error("media." + suffix, params, "invalid_params")

    def test_lone_surrogates_are_invalid_before_mutation(self):
        upload_id = self.upload()
        self.error("media.addFromUpload", {"uploadId": upload_id, "filename": "\ud800.png"}, "invalid_params")
        self.error("media.trashUnused", {"filenames": ["\ud800"]}, "invalid_params")


class MediaProcessTests(unittest.TestCase):
    def test_media_check_aborts_before_native_work(self):
        with tempfile.TemporaryDirectory(prefix="memr-e05-media-abort-") as directory:
            with EnginePipe(directory, code=MEDIA_ABORT_CODE) as engine:
                operation_id = engine.call("media.check")["result"]["operationId"]
                aborted = engine.call("progress.abort", {"operationId": operation_id})["result"]
                self.assertIn("changes", aborted)
                deadline = time.monotonic() + 10
                while True:
                    state = engine.call("progress.get", {"operationId": operation_id})["result"]
                    if state["status"] != "running" or time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                self.assertEqual(state["status"], "aborted")
                self.assertEqual(state["error"]["code"], "aborted")
                self.assertEqual(engine.call("engine.status")["result"]["state"], "ready")

    def test_media_check_completes_through_process(self):
        with tempfile.TemporaryDirectory(prefix="memr-e05-media-check-") as directory:
            with EnginePipe(directory) as engine:
                operation_id = engine.call("media.check")["result"]["operationId"]
                deadline = time.monotonic() + 10
                while True:
                    state = engine.call("progress.get", {"operationId": operation_id})["result"]
                    if state["status"] != "running" or time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["result"]["missing"], [])
                self.assertEqual(state["result"]["unused"], [])
                self.assertEqual(state["result"]["warnings"], [])
