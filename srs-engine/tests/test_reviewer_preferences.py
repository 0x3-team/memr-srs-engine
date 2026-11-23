"""Display preferences identify owned note types without changing native templates."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from memr_srs_engine.handlers.provenance import ensure_notetypes
from memr_srs_engine.render import render_card
from tests.helpers import temp_collection


class ReviewerPreferencesTests(unittest.TestCase):
    def test_invalid_ownership_metadata_never_claims_a_template(self):
        with temp_collection() as col:
            ids = ensure_notetypes(SimpleNamespace(col=col), {})["notetypeIds"]
            model = col.models.get(ids[0])
            note = col.new_note(model)
            note["Front"], note["Back"] = "Ownership fixture", "Answer"
            col.add_note(note, 1)
            card = note.cards()[0]
            valid_index = deepcopy(col.get_config("canonnotes.provenanceNotetypes"))
            invalid_indexes = [None, [], {}, {"version": True, "ids": {"basic": ids[0]}},
                               {"version": 1.0, "ids": {"basic": ids[0]}},
                               {"version": 1, "ids": {"basic": float(ids[0])}},
                               {"version": 1, "ids": {"basic": str(ids[0])}},
                               {"version": 1, "ids": {"basic": ids[0], "unrecognized": ids[0]}},
                               {"version": 1, "ids": {"basic": ids[0]}, "extra": True}]
            for index in invalid_indexes:
                with self.subTest(index=index):
                    col.set_config("canonnotes.provenanceNotetypes", index)
                    self.assertNotIn("memr-owned", render_card(col, card)["bodyClass"].split())
            col.set_config("canonnotes.provenanceNotetypes", valid_index)
            for marker in [None, [], {}, {"owner": "user", "kind": "basic", "schemaVersion": 1},
                           {"owner": "canonnotes", "kind": "basic", "schemaVersion": True},
                           {"owner": "canonnotes", "kind": "unknown", "schemaVersion": 1}]:
                with self.subTest(marker=marker):
                    model["canonnotesProvenance"] = marker
                    col.models.update_dict(model)
                    self.assertNotIn("memr-owned", render_card(col, card)["bodyClass"].split())

    def test_owned_marker_survives_rename_but_does_not_follow_a_clone_or_name(self):
        with temp_collection() as col:
            ids = ensure_notetypes(SimpleNamespace(col=col), {})["notetypeIds"]
            owned = col.models.get(ids[0])

            def rendered(model):
                note = col.new_note(model)
                note["Front"], note["Back"] = "Display fixture", "Answer"
                col.add_note(note, 1)
                card = note.cards()[0]
                before = deepcopy(col.models.get(model["id"]))
                result = render_card(col, card)
                self.assertEqual(result["css"], card.render_output().css)
                self.assertEqual(col.models.get(model["id"]), before)
                return result

            self.assertIn("memr-owned", rendered(owned)["bodyClass"].split())
            owned["name"] = "Renamed owned type"
            col.models.update_dict(owned)
            self.assertIn("memr-owned", rendered(owned)["bodyClass"].split())
            clone = deepcopy(owned)
            clone["id"], clone["name"] = 0, "User clone"
            clone = col.models.get(col.models.add_dict(clone).id)
            self.assertNotIn("memr-owned", rendered(clone)["bodyClass"].split())
            stock = col.models.by_name("Basic")
            stock["name"] = "Memr Basic"
            col.models.update_dict(stock)
            self.assertEqual(rendered(stock)["bodyClass"], "card card1")
            col.set_config("canonnotes.provenanceNotetypes", {"version": 1, "ids": {"basic": owned["id"]}})
            owned.pop("canonnotesProvenance")
            col.models.update_dict(owned)
            self.assertNotIn("memr-owned", rendered(owned)["bodyClass"].split())
