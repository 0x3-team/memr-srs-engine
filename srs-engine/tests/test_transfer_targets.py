"""Import results identify only notes actually added or updated by native work."""
from tests.test_media import RpcCase
from types import SimpleNamespace
from memr_srs_engine.handlers.transfer import _import_targets


class TransferTargetTests(RpcCase):
    def test_csv_result_identifies_new_and_updated_notes(self):
        original = self.note("Existing target")
        unrelated = self.note("Unrelated target")
        upload = self.upload(b"Existing target,Updated answer\nNew target,New answer\n", "csv")
        params = dict(uploadId=upload, delimiter=",", isHtml=False,
                      notetypeId=original.mid, deckId=1,
                      mapping=[dict(column=0, target="field", fieldOrdinal=0),
                               dict(column=1, target="field", fieldOrdinal=1)],
                      duplicateMode="update", matchScope="notetype", tags=[])
        state = self.operation("transfer.importCsv", params)
        self.assertEqual(state["status"], "completed", state)
        outcome = state["result"]
        self.assertEqual(outcome["imported"]["updated"], 1)
        self.assertEqual(outcome["imported"]["skipped"], 0)
        self.assertEqual(self.col.get_note(original.id)["Back"], "Updated answer")
        expected = {original.id, *self.col.find_notes('"Front:New target"')}
        self.assertEqual(set(outcome.get("affectedIds", [])), expected)
        self.assertNotIn(unrelated.id, outcome["affectedIds"])
        self.assertEqual(set(self.col.find_notes(outcome["search"])), expected)
        params["duplicateMode"] = "preserve"
        repeated = self.operation("transfer.importCsv", params)["result"]
        self.assertEqual(repeated["affectedIds"], [])
        self.assertEqual(self.col.find_notes(repeated["search"]), [])

    def test_import_identity_limits_are_explicit_and_not_partial(self):
        def notes(count):
            return [SimpleNamespace(id=SimpleNamespace(nid=index + 1)) for index in range(count)]
        bounded = _import_targets(SimpleNamespace(new=notes(100000), updated=[]))
        self.assertEqual(bounded["affectedIds"], list(range(1, 100001)))
        self.assertNotIn("search", bounded)
        oversized = _import_targets(SimpleNamespace(new=notes(100001), updated=[]))
        self.assertNotIn("affectedIds", oversized)
        self.assertNotIn("search", oversized)
        self.assertIn("100,000", oversized["warnings"][0])
        deduplicated = _import_targets(SimpleNamespace(new=notes(2), updated=notes(2)))
        self.assertEqual(deduplicated["affectedIds"], [1, 2])
