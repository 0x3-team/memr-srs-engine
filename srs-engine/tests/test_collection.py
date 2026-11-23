import signal
import tempfile
import unittest

from anki.collection import Collection
from anki import errors
from memr_srs_engine.errors import public_error
from tests.helpers import EnginePipe, build_collection, temp_collection


class CollectionTests(unittest.TestCase):
    def test_generated_collection_builder(self):
        with temp_collection() as col:
            ids = build_collection(col, deck_count=3, notes_per_deck=2)
            self.assertEqual((len(ids["deckIds"]), len(ids["noteIds"]), len(ids["cardIds"])), (3, 6, 6))
            self.assertEqual(col.card_count(), 6)

    def test_second_process_conflict_and_shutdown_release(self):
        with tempfile.TemporaryDirectory(prefix="memr-f02-lock-") as directory:
            with EnginePipe(directory) as first:
                self.assertTrue(first.call("engine.status")["result"]["collectionOpen"])
                with EnginePipe(directory) as second:
                    failure = second.receive()
                    self.assertEqual(failure["error"]["code"], "conflict")
                    self.assertEqual(failure["error"]["message"], "collection is open in another program")
                    self.assertEqual(second.process.wait(timeout=5), 1)
                self.assertEqual(first.call("engine.shutdown")["result"], {})
                self.assertEqual(first.process.wait(timeout=5), 0)
                col = Collection(str(first.collection_path))
                col.close()

    def test_eof_releases_collection(self):
        with tempfile.TemporaryDirectory(prefix="memr-f02-eof-") as directory:
            with EnginePipe(directory) as engine:
                engine.call("engine.status")
                engine.process.stdin.close()
                self.assertEqual(engine.process.wait(timeout=5), 0)
                col = Collection(str(engine.collection_path))
                col.close()

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "SIGTERM unavailable")
    def test_sigterm_releases_collection(self):
        with tempfile.TemporaryDirectory(prefix="memr-f02-term-") as directory:
            with EnginePipe(directory) as engine:
                engine.call("engine.status")
                engine.process.send_signal(signal.SIGTERM)
                self.assertEqual(engine.process.wait(timeout=5), 0)
                col = Collection(str(engine.collection_path))
                col.close()

    def test_backend_errors_are_typed_and_redacted(self):
        cases = [(errors.Interrupted, "aborted"), (errors.NotFoundError, "not_found"),
                 (errors.DeletedError, "not_found"), (errors.ExistsError, "conflict"),
                 (errors.InvalidInput, "invalid_params"), (errors.SearchError, "invalid_params"),
                 (errors.TemplateError, "invalid_params"), (errors.NetworkError, "engine_unavailable"),
                 (errors.SchedulerUpgradeRequired, "unsupported"), (errors.BackendIOError, "internal")]
        for exception, code in cases:
            with self.subTest(exception=exception.__name__):
                mapped = public_error(exception("private diagnostic", None, None, None)).as_dict()
                self.assertEqual(mapped["code"], code)
                self.assertNotIn("private diagnostic", str(mapped))
        self.assertEqual(public_error(errors.DBError("Anki already open, or media currently syncing.", None, None, None)).code, "conflict")
