"""Hostile SRS inputs and diagnostic privacy, using generated scratch data only."""
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest

from memr_srs_engine import handlers
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from tests.helpers import EnginePipe, temp_collection


class DiagnosticPrivacyTests(unittest.TestCase):
    def test_request_and_long_operation_exceptions_do_not_log_untrusted_text(self):
        marker = "synthetic-private-card-and-auth-value"

        def hostile(*_args):
            raise RuntimeError(marker)

        with temp_collection() as col:
            output, diagnostic = io.BytesIO(), io.StringIO()
            logger = logging.Logger("srs-hostile-privacy")
            logger.addHandler(logging.StreamHandler(diagnostic))
            registry = Registry()
            registry.handlers = {**registry.handlers, "decks.tree": hostile, "maintenance.checkDatabase": hostile}
            engine = Engine(col, registry, FrameWriter(output), logger)
            try:
                engine.dispatch(json.dumps({"id": 1, "method": "decks.tree", "params": {}}).encode())
                response = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(response["error"]["code"], "internal")
                engine.dispatch(json.dumps({"id": 2, "method": "maintenance.checkDatabase", "params": {}}).encode())
                engine.progress.thread.join(5)
                self.assertFalse(engine.progress.thread.is_alive())
                state = engine.progress.get()
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["error"]["code"], "internal")
                self.assertNotIn(marker, json.dumps(state))
                self.assertNotIn(marker, output.getvalue().decode())
                self.assertIn("Engine request failed", diagnostic.getvalue())
                self.assertIn("Long operation failed", diagnostic.getvalue())
                self.assertNotIn(marker, diagnostic.getvalue())
                self.assertNotIn("Traceback", diagnostic.getvalue())
            finally:
                engine.progress.close()

    def test_startup_exception_does_not_log_private_path_or_message(self):
        code = """
from memr_srs_engine.collection import CollectionOwner
def hostile(*_args):
    raise RuntimeError('synthetic-private-startup-value')
CollectionOwner.open = hostile
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory(prefix="memr-srs-startup-privacy-") as directory:
            with EnginePipe(directory, code=code) as pipe:
                response = pipe.receive()
                pipe.process.wait(timeout=5)
                self.assertEqual(pipe.process.returncode, 1)
                self.assertEqual(response["error"]["code"], "internal")
                self.assertNotIn("synthetic-private-startup-value", json.dumps(response))
                diagnostic = Path(directory, "engine.log").read_text()
                self.assertIn("Engine failed", diagnostic)
                self.assertNotIn("synthetic-private-startup-value", diagnostic)
                self.assertNotIn("Traceback", diagnostic)
