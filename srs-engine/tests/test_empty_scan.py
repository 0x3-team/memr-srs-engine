"""Native snapshot scans, bounded cancellation, IPC and owned child cleanup."""
import hashlib
import io
import json
import logging
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from anki.collection import AddNoteRequest

from memr_srs_engine.handlers import empty_scan
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from tests.helpers import EnginePipe, temp_collection


class EmptyScanTests(unittest.TestCase):
    def setUp(self):
        self.collection = temp_collection()
        self.col = self.collection.__enter__()
        self.root = Path(self.col.path).parent
        self.output = io.BytesIO()
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logging.getLogger("empty-scan-tests"))
        self.sequence = 0

    def tearDown(self):
        self.engine.progress.close()
        self.collection.__exit__(None, None, None)

    def rpc(self, method, params=None):
        self.sequence += 1
        start = self.output.tell()
        self.engine.dispatch(json.dumps({"id": self.sequence, "method": method, "params": params or {}}).encode())
        frames = [json.loads(line) for line in self.output.getvalue()[start:].splitlines()]
        for frame in frames:
            if frame.get("event"):
                self.assertEqual(frame["event"], "progress")
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], frame["progress"]), [])
        responses = [frame for frame in frames if not frame.get("event")]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], self.sequence)
        return responses[0]

    def terminal(self, method="maintenance.emptyCards", params=None, expected="completed"):
        ack = self.rpc(method, params)
        self.assertIn("operationId", ack.get("result", {}), ack)
        self.engine.progress.thread.join(10)
        self.assertFalse(self.engine.progress.thread.is_alive())
        state = self.rpc("progress.get", {"operationId": ack["result"]["operationId"]})["result"]
        self.assertEqual(state["status"], expected, state)
        self.assertEqual(list(self.root.glob("srs-empty-*")), [])
        return state

    def source_state(self):
        files = {}
        for suffix in ("", "-wal"):
            path = Path(self.col.path + suffix)
            files[suffix] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        return files, self.col.undo_status(), self.col.db.scalar("select mod from col")

    def test_real_native_snapshot_matches_primary_and_preserves_source_and_undo(self):
        model = self.col.models.by_name("Basic")
        for front in ("", "Generated nonempty", ""):
            note = self.col.new_note(model)
            note["Front"] = front
            self.col.add_note(note, 1)
        native = self.col.get_empty_cards()
        expected = [{"noteId": int(note.note_id), "cardIds": list(note.card_ids),
                     "allCardsEmpty": bool(note.will_delete_note)} for note in native.notes]
        before = self.source_state()
        children = []
        popen = subprocess.Popen
        def track(*args, **kwargs):
            process = popen(*args, **kwargs)
            children.append(process)
            return process
        with patch.object(empty_scan.subprocess, "Popen", side_effect=track):
            result = self.terminal()["result"]
        self.assertEqual(sorted(result["emptyCards"]["notes"], key=lambda note: note["noteId"]),
                         sorted(expected, key=lambda note: note["noteId"]))
        self.assertEqual(self.source_state(), before)
        self.assertTrue(result["undo"]["canUndo"])
        self.assertFalse(any(result["changes"].values()))
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].args[1:3], ["-I", "-B"])
        self.assertEqual(children[0].returncode, 0)

    def test_abort_during_real_sqlite_backup_skips_child_and_preserves_source(self):
        before = self.source_state()
        connect = sqlite3.connect
        engine = self.engine
        class CancelBackup(sqlite3.Connection):
            def backup(self, target, **kwargs):
                original = kwargs["progress"]
                def progress(*args):
                    engine.progress.abort(engine.progress.current)
                    original(*args)
                kwargs["progress"] = progress
                return super().backup(target, **kwargs)
        def connection(*args, **kwargs):
            return connect(*args, **kwargs, factory=CancelBackup)
        with patch.object(empty_scan.sqlite3, "connect", side_effect=connection), \
                patch.object(empty_scan.subprocess, "Popen", wraps=subprocess.Popen) as child:
            state = self.terminal(expected="aborted")
            child.assert_not_called()
        self.assertIsNone(state["result"])
        self.assertEqual(state["error"]["code"], "aborted")
        self.assertEqual(self.source_state(), before)

    def test_real_native_child_scan_is_cancelled_and_reaped(self):
        model = self.col.models.by_name("Basic")
        notes = []
        for index in range(50000):
            note = self.col.new_note(model)
            note["Front"] = f"Generated cancellation note {index}"
            note["Back"] = "Generated answer"
            notes.append(AddNoteRequest(note=note, deck_id=1))
        self.col.add_notes(notes)
        before = self.source_state()
        children, entered = [], threading.Event()
        popen = subprocess.Popen
        def track(*args, **kwargs):
            process = popen(*args, **kwargs)
            children.append(process)
            entered.set()
            return process
        with patch.object(empty_scan.subprocess, "Popen", side_effect=track):
            ack = self.rpc("maintenance.emptyCards")["result"]
            self.assertTrue(entered.wait(5))
            process = children[0]
            started = Path(process.args[-2])
            deadline = time.monotonic() + 5
            while not started.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertTrue(started.exists(), "The real native scan must reach its start boundary")
            self.assertIsNone(process.poll(), "The fixture must still be scanning when abort is requested")
            begin = time.monotonic()
            self.assertIn("result", self.rpc("progress.abort", {"operationId": ack["operationId"]}))
            abort_ms = (time.monotonic() - begin) * 1000
            self.engine.progress.thread.join(3)
            self.assertFalse(self.engine.progress.thread.is_alive())
            self.assertLess(time.monotonic() - begin, 2)
            print(f"E05 native empty-card scan: notes=50000 abortReply={abort_ms:.3f}ms "
                  f"terminal={(time.monotonic() - begin) * 1000:.3f}ms child={process.returncode}")
        state = self.rpc("progress.get", {"operationId": ack["operationId"]})["result"]
        self.assertEqual(state["status"], "aborted", state)
        self.assertIsNotNone(process.returncode)
        self.assertNotEqual(process.returncode, 0, "Cancellation must stop the real child before completion")
        self.assertEqual(self.source_state(), before)
        self.assertEqual(list(self.root.glob("srs-empty-*")), [])

    def test_child_startup_failure_and_invalid_ipc_fail_cleanly(self):
        with patch.object(empty_scan.subprocess, "Popen", side_effect=OSError("private startup diagnostic")):
            state = self.terminal(expected="failed")
        self.assertEqual(state["error"]["code"], "internal")
        self.assertNotIn("private startup diagnostic", json.dumps(state))
        payloads = [b"not json", b'{"result":null}', b'{"result":{"notes":[]},"result":{"notes":[]}}',
                    b'{"result":{"notes":[{"noteId":true,"cardIds":[],"allCardsEmpty":false}]}}',
                    b'{"error":{"code":"outside_contract","message":"invalid"}}']
        for payload in payloads:
            with self.subTest(payload=payload[:80]):
                code = "import pathlib,sys; pathlib.Path(sys.argv[3]).write_bytes(" + repr(payload) + ")"
                with patch.object(empty_scan, "WORKER_CODE", code):
                    state = self.terminal(expected="failed")
                self.assertEqual(state["error"]["code"], "internal")
        code = "import pathlib,sys; f=pathlib.Path(sys.argv[3]).open('wb'); f.truncate(int(sys.argv[5])+1); f.close()"
        with patch.object(empty_scan, "WORKER_CODE", code):
            state = self.terminal(expected="failed")
        self.assertEqual(state["error"]["code"], "internal")
        code = "import pathlib,sys; pathlib.Path(sys.argv[3]).write_text('{\"error\":{\"code\":\"invalid_params\",\"message\":\"Native fixture error.\"}}')"
        with patch.object(empty_scan, "WORKER_CODE", code):
            state = self.terminal(expected="failed")
        self.assertEqual(state["error"], {"code": "invalid_params", "message": "Native fixture error."})

    def test_pipe_control_and_shutdown_reap_scratch_child(self):
        code = """
from pathlib import Path
import sys
from memr_srs_engine.handlers import empty_scan
empty_scan.WORKER_CODE = 'import time; time.sleep(30)'
popen = empty_scan.subprocess.Popen
def tracked(*args, **kwargs):
    process = popen(*args, **kwargs)
    original = process.wait
    def wait(*args, **kwargs):
        result = original(*args, **kwargs)
        root = Path(sys.argv[sys.argv.index('--collection') + 1]).parent
        (root / 'scan-reaped').write_text(str(result))
        return result
    process.wait = wait
    root = Path(sys.argv[sys.argv.index('--collection') + 1]).parent
    (root / 'scan-spawned').touch()
    return process
empty_scan.subprocess.Popen = tracked
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        for ending in ("abort", "shutdown", "eof", "sigterm"):
            with self.subTest(ending=ending):
                directory = self.root / ending
                directory.mkdir()
                with EnginePipe(directory, code=code) as pipe:
                    pipe.call("engine.status")
                    begin = time.monotonic()
                    ack = pipe.call("maintenance.emptyCards")["result"]
                    self.assertLess(time.monotonic() - begin, 0.5)
                    deadline = time.monotonic() + 5
                    while not (directory / "scan-spawned").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((directory / "scan-spawned").exists())
                    self.assertEqual(pipe.call("progress.get", {"operationId": ack["operationId"]})["result"]["status"], "running")
                    self.assertEqual(pipe.call("engine.status")["error"]["code"], "busy")
                    begin = time.monotonic()
                    if ending == "abort":
                        pipe.call("progress.abort", {"operationId": ack["operationId"]})
                        state = {"status": "running"}
                        while state["status"] == "running" and time.monotonic() - begin < 3:
                            state = pipe.call("progress.get", {"operationId": ack["operationId"]})["result"]
                            time.sleep(0.01)
                        self.assertEqual(state["status"], "aborted", state)
                        self.assertTrue(pipe.call("engine.status")["result"]["collectionOpen"])
                    elif ending == "shutdown":
                        self.assertEqual(pipe.call("engine.shutdown")["result"], {})
                        self.assertEqual(pipe.process.wait(timeout=3), 0)
                    elif ending == "eof":
                        pipe.process.stdin.close()
                        self.assertEqual(pipe.process.wait(timeout=3), 0)
                    else:
                        pipe.process.send_signal(signal.SIGTERM)
                        self.assertEqual(pipe.process.wait(timeout=3), 0)
                    self.assertLess(time.monotonic() - begin, 2)
                    self.assertTrue((directory / "scan-reaped").exists())
                    self.assertEqual(list(directory.glob("srs-empty-*")), [])
