import io
import json
import logging
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from anki import collection_pb2
from memr_srs_engine import handlers
from memr_srs_engine.collection import mutation_result
from memr_srs_engine.errors import EngineError
from memr_srs_engine.protocol import Engine, FrameWriter, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, parse_request
from memr_srs_engine.progress import backend_progress
from memr_srs_engine.registry import HANDLERS, Registry, load_catalogue, method
from memr_srs_engine.validate import check_schema, validate_value
from tests.helpers import EnginePipe, FakeClock, LAUNCH, LONG_OPERATION_CODE, temp_collection


class ValidationTests(unittest.TestCase):
    def test_all_catalogue_schemas_and_closed_dialect(self):
        catalogue = load_catalogue()
        self.assertGreater(len(catalogue), 100)
        for schema in ({"type": "string", "pattern": "x"}, {"type": "object", "properties": {}, "required": []},
                       {"type": "integer", "minimum": 4, "maximum": 3}, {"type": ["string", "integer"]},
                       {"type": "array"}, {"type": "integer", "maximum": 2 ** 53}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                check_schema(schema)

    def test_schema_values_and_unicode(self):
        schema = {"type": "object", "properties": {
            "id": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
            "labels": {"type": "array", "items": {"type": "string", "maxLength": 1}, "maxItems": 2}},
            "required": ["id", "labels"], "additionalProperties": False}
        self.assertEqual(validate_value(schema, {"id": 1, "labels": ["📝"]}), [])
        self.assertEqual(validate_value(schema, {"id": 1.0, "labels": []}), [])
        for value in ({"id": True, "labels": []}, {"id": 2 ** 53, "labels": []},
                      {"id": 1.5, "labels": []}, {"id": 10 ** 400, "labels": []},
                      {"id": 1, "labels": ["ab"]}, {"id": 1, "labels": ["a", "b", "c"]},
                      {"id": 1, "labels": [], "extra": 0}, {"labels": []}):
            with self.subTest(value=value):
                self.assertTrue(validate_value(schema, value))
        self.assertTrue(validate_value({"type": "number"}, float("nan")))
        self.assertTrue(validate_value({"type": "number"}, float("inf")))

    def test_registry_rejects_unknown_and_duplicate_handlers(self):
        with self.assertRaises(ValueError):
            Registry(handlers={"outside.catalogue": lambda *_args: {}})
        with self.assertRaises(ValueError):
            method("engine.status")(lambda *_args: {})
        self.assertTrue({"engine.status", "engine.describe", "progress.get", "progress.abort"} <= HANDLERS.keys())
        self.assertTrue(HANDLERS.keys() <= load_catalogue().keys())

    def test_request_envelope_rejects_nonstandard_json(self):
        for frame in (b"[]", b'{}', b'{"id":true,"method":"engine.status"}',
                      b'{"id":1,"id":2,"method":"engine.status"}',
                      b'{"id":1,"method":"engine.status","params":{"a":NaN}}',
                      b'{"id":1,"method":"engine.status","extra":0}', b'\xff'):
            with self.subTest(frame=frame), self.assertRaises(EngineError):
                parse_request(frame)

    def test_response_limits_and_serialization(self):
        writer = FrameWriter(io.BytesIO(), max_bytes=30)
        self.assertLess(len(writer.encode({"id": 1, "result": {}})), 31)
        for value in ("x" * 31, float("nan"), object()):
            with self.subTest(value=type(value).__name__), self.assertRaises(EngineError):
                writer.encode({"id": 1, "result": value})
        self.assertEqual(MAX_RESPONSE_BYTES, 32 * 1024 * 1024)

    def test_partial_pipe_writes_keep_frames_complete(self):
        class PartialPipe(io.BytesIO):
            def write(self, data):
                return super().write(data[:3])
        pipe = PartialPipe()
        FrameWriter(pipe).write({"id": 1, "result": {"text": "📝"}})
        self.assertEqual(json.loads(pipe.getvalue()), {"id": 1, "result": {"text": "📝"}})

    def test_backend_progress_variants(self):
        self.assertEqual(backend_progress(collection_pb2.Progress(none={}))["total"], None)
        self.assertEqual(backend_progress(collection_pb2.Progress(importing="Importing"))["label"], "Importing")
        self.assertEqual(backend_progress(collection_pb2.Progress(database_check={"stage": "Checking", "stage_current": 2, "stage_total": 4})),
                         {"done": 2, "total": 4, "label": "Checking"})
        self.assertEqual(backend_progress(collection_pb2.Progress(compute_memory={"current_cards": 3, "total_cards": 6, "label": "Computing"}))["done"], 3)


class ProtocolTests(unittest.TestCase):
    def test_invalid_handler_results_keep_internal_error_mapping(self):
        code = """
from memr_srs_engine import handlers
from memr_srs_engine.registry import HANDLERS
bad_results = iter(({}, {"undo": None}, []))
HANDLERS['decks.create'] = lambda *_args: next(bad_results)
HANDLERS['study.undoStatus'] = lambda *_args: []
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory(prefix="memr-f02-result-") as directory:
            with EnginePipe(directory, code=code) as engine:
                for index in range(3):
                    with self.subTest(result=index):
                        self.assertEqual(engine.call("decks.create", {"name": "Invalid handler"})["error"]["code"], "internal")
                self.assertEqual(engine.call("study.undoStatus")["error"]["code"], "internal")

    def test_describe_without_collection_and_isolation(self):
        with tempfile.TemporaryDirectory(prefix="memr-f02-describe-") as directory:
            # A poisoned current directory must never enter isolated imports.
            Path(directory, "memr_srs_engine.py").write_text("raise RuntimeError('wrong module')\n")
            result = subprocess.run([sys.executable, "-I", "-B", str(LAUNCH), "--describe"],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            names = [entry["name"] for entry in json.loads(result.stdout)["methods"]]
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(set(names), set(HANDLERS))
            self.assertTrue({"engine.status", "engine.describe", "progress.get", "progress.abort"} <= set(names))
            self.assertTrue(set(names) <= load_catalogue().keys())
            self.assertFalse(Path(directory, "collection.anki2").exists())

    def test_invalid_frames_then_valid_request(self):
        # Isolate an unregistered catalogue method even when every area is implemented.
        code = """
from memr_srs_engine import handlers
from memr_srs_engine.registry import HANDLERS
HANDLERS.pop('engine.describe')
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory(prefix="memr-f02-frame-") as directory:
            with EnginePipe(directory, code=code) as engine:
                for frame in (b"not json\n", b"\xff\n", b"[]\n", b'{"id":true,"method":"engine.status"}\n'):
                    engine.raw(frame)
                    self.assertEqual(engine.receive()["error"]["code"], "invalid_params")
                engine.raw(b"x" * (MAX_REQUEST_BYTES + 1) + b"\n")
                self.assertEqual(engine.receive()["error"]["code"], "invalid_params")
                boundary = b'{"id":100000,"method":"engine.status","params":{}}'
                engine.raw(boundary + b" " * (MAX_REQUEST_BYTES - len(boundary)) + b"\n")
                self.assertEqual(engine.receive()["result"]["state"], "ready")
                self.assertEqual(engine.call("engine.status")["result"]["state"], "ready")
                self.assertEqual(engine.call("unknown.method")["error"]["code"], "unsupported")
                self.assertEqual(engine.call("engine.describe").get("error", {}).get("code"), "unsupported")
                self.assertEqual(engine.call("engine.status", {"unexpected": 1})["error"]["code"], "invalid_params")
                self.assertEqual(engine.call("progress.abort")["error"]["code"], "invalid_params")
                self.assertEqual(engine.call("progress.get", {"operationId": "missing"})["error"]["code"], "not_found")
                self.assertEqual(engine.call("progress.get")["result"]["status"], "idle")

    def test_truncated_eof_and_unterminated_oversize(self):
        for frame in (b'{"id":', b"x" * (MAX_REQUEST_BYTES + 100)):
            with self.subTest(size=len(frame)), tempfile.TemporaryDirectory(prefix="memr-f02-truncated-") as directory:
                with EnginePipe(directory) as engine:
                    engine.raw(frame)
                    engine.process.stdin.close()
                    self.assertEqual(engine.receive()["error"]["code"], "invalid_params")
                    self.assertEqual(engine.process.wait(timeout=5), 0)

    def test_stdout_diagnostics_go_only_to_log_without_exception_text(self):
        code = """
from memr_srs_engine import handlers
from memr_srs_engine.registry import HANDLERS
import os
def faulty(engine, params):
    print('python diagnostic')
    os.write(1, b'native diagnostic\\n')
    raise RuntimeError('backend detail')
HANDLERS['engine.status'] = faulty
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory(prefix="memr-f02-log-") as directory:
            with EnginePipe(directory, code=code) as engine:
                result = engine.call("engine.status")
                self.assertEqual(result["error"]["code"], "internal")
                self.assertNotIn("backend detail", json.dumps(result))
                engine.call("engine.shutdown")
                engine.process.wait(timeout=5)
                log = engine.log_path.read_text()
                for message in ("Engine request failed", "python diagnostic", "native diagnostic"):
                    self.assertIn(message, log)
                # Exception diagnostics may include card fields or credentials;
                # only the fixed application event belongs in the retained log.
                self.assertNotIn("Traceback", log)
                self.assertNotIn("backend detail", log)
                self.assertEqual(engine.log_path.stat().st_mode & 0o777, 0o600)

    def test_startup_refuses_handler_outside_catalogue(self):
        code = """
from memr_srs_engine.registry import method
method('outside.catalogue')(lambda *_args: {})
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory(prefix="memr-f02-registry-") as directory:
            with EnginePipe(directory, code=code) as engine:
                self.assertEqual(engine.receive()["error"]["code"], "internal")
                self.assertEqual(engine.process.wait(timeout=5), 1)
                log = engine.log_path.read_text()
                self.assertIn("Engine failed", log)
                self.assertNotIn("Traceback", log)
                self.assertNotIn("outside.catalogue", log)

    def test_long_ack_busy_events_complete_and_abort(self):
        with tempfile.TemporaryDirectory(prefix="memr-f02-long-") as directory:
            with EnginePipe(directory, code=LONG_OPERATION_CODE) as engine:
                ack = engine.call("maintenance.checkDatabase")["result"]
                operation_id = ack["operationId"]
                self.assertTrue(operation_id)
                events = []
                times = []
                for _index in range(3):
                    event = engine.receive()
                    self.assertEqual((event["event"], event["operationId"]), ("progress", operation_id))
                    events.append(event)
                    times.append(time.monotonic())
                self.assertTrue(all(0.18 < times[index] - times[index - 1] < 0.5 for index in range(1, len(times))))
                self.assertEqual(engine.call("engine.status")["error"]["code"], "busy")
                self.assertFalse(any(engine.call("sync.abort")["result"]["changes"].values()))
                self.assertEqual(engine.call("progress.get", {"operationId": operation_id})["result"]["status"], "running")
                while True:
                    event = engine.receive()
                    if event["progress"]["status"] != "running":
                        break
                self.assertEqual(event["progress"]["status"], "completed")
                terminal = engine.call("progress.get", {"operationId": operation_id})["result"]
                self.assertEqual(terminal["status"], "completed")
                self.assertIn("summary", terminal["result"])
                self.assertEqual(engine.call("engine.status")["result"]["state"], "ready")
                second = engine.call("maintenance.checkDatabase")["result"]["operationId"]
                self.assertNotEqual(second, operation_id)
                self.assertEqual(engine.call("progress.get", {"operationId": operation_id})["result"]["status"], "completed")
                self.assertIn("changes", engine.call("progress.abort", {"operationId": operation_id})["result"])
                self.assertEqual(engine.call("progress.get", {"operationId": second})["result"]["status"], "running")
                engine.receive()  # handler has installed its abort wrapper
                self.assertIn("changes", engine.call("progress.abort", {"operationId": second})["result"])
                while True:
                    event = engine.receive()
                    if event["progress"]["status"] != "running":
                        break
                self.assertEqual(event["progress"]["status"], "aborted")
                self.assertEqual(engine.call("progress.get", {"operationId": second})["result"]["error"]["code"], "aborted")
                self.assertEqual(engine.call("engine.status")["result"]["state"], "ready")

    def test_eof_sigterm_and_shutdown_join_long_operation(self):
        for ending in ("eof", "shutdown", "sigterm"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory(prefix="memr-f02-long-close-") as directory:
                with EnginePipe(directory, code=LONG_OPERATION_CODE) as engine:
                    engine.call("maintenance.checkDatabase")
                    engine.receive()
                    if ending == "eof":
                        engine.process.stdin.close()
                    elif ending == "sigterm":
                        engine.process.send_signal(signal.SIGTERM)
                    else:
                        self.assertEqual(engine.call("engine.shutdown")["result"], {})
                    self.assertEqual(engine.process.wait(timeout=5), 0)
                    # Prove the backend lock was released after the thread joined.
                    with EnginePipe(directory, collection_path=engine.collection_path) as reopened:
                        self.assertTrue(reopened.call("engine.status")["result"]["collectionOpen"])

    def test_fake_clock_progress_cadence_and_real_background_collection(self):
        with temp_collection() as col:
            clock = FakeClock()
            output = io.BytesIO()
            release = threading.Event()
            started = threading.Event()
            def long(engine, params):
                started.set()
                release.wait(5)
                engine.col.db.scalar("select count(*) from notes")
                return mutation_result()
            registry = Registry(handlers={**HANDLERS, "maintenance.checkDatabase": long})
            engine = Engine(col, registry, FrameWriter(output), logging.getLogger("test"), clock)
            try:
                engine.dispatch(b'{"id":1,"method":"maintenance.checkDatabase","params":{}}')
                self.assertTrue(started.wait(5))
                engine.progress.tick()
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                clock.advance(.249)
                engine.progress.tick()
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                clock.advance(.001)
                engine.progress.tick()
                self.assertEqual(len(output.getvalue().splitlines()), 2)
            finally:
                release.set()
                engine.progress.close()
            self.assertEqual(engine.progress.get()["status"], "completed")

    def test_status_pipe_timing_1000_round_trips(self):
        with tempfile.TemporaryDirectory(prefix="memr-f02-timing-") as directory:
            with EnginePipe(directory) as engine:
                engine.call("engine.status")  # exclude process import/open startup
                milliseconds = []
                for _index in range(1000):
                    start = time.perf_counter_ns()
                    self.assertEqual(engine.call("engine.status")["result"]["state"], "ready")
                    milliseconds.append((time.perf_counter_ns() - start) / 1_000_000)
                p95 = sorted(milliseconds)[949]
                print(f"F02 engine.status pipe timing: n=1000 median={statistics.median(milliseconds):.3f} ms p95={p95:.3f} ms max={max(milliseconds):.3f} ms", flush=True)
                self.assertLess(p95, 5)
