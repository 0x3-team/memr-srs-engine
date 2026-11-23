"""U11 native text fidelity, imported formatting and atomic editor revisions."""
import copy
from pathlib import Path

from tests.test_image_occlusion import mask, occlusion, png
from tests.test_notes import NotesCase


def annotation(**extra):
    return mask("annotation", kind="text", width=0, height=0, inactive=True,
                text='Label: \\ path <b>literal</b> & "quoted" فارسی\nSecond line',
                fontSize=.08, scale=1.25, **extra)


class ImageOcclusionEditorTests(NotesCase):
    def setUp(self):
        super().setUp()
        self.col.add_image_occlusion_notetype()
        self.io = self.col.models.by_name("Image Occlusion")["id"]
        self.image = Path(self.col.media.dir()) / "sample.png"
        self.image.write_bytes(png(200, 120))

    def add_io(self, value):
        return self.rpc("imageOcclusion.addNote", {"notetypeId": self.io, "deckId": 1, "occlusion": value})

    def test_text_native_fidelity_update_and_undo(self):
        value = occlusion(masks=[mask(), annotation()])
        added = self.add_io(value)
        nid = added["noteId"]
        fetched = self.rpc("imageOcclusion.getNote", {"noteId": nid})
        self.assertEqual(fetched["occlusion"], value)
        self.assertRegex(fetched["revision"], r"^[a-f0-9]{64}$")
        self.assertEqual(len(added["cardIds"]), 1, "A background text annotation creates no card")
        native = self.col.get_image_occlusion_note(nid).note
        text = next(s for group in native.occlusions for s in group.shapes if s.shape == "text")
        props = {p.name: p.value for p in text.properties}
        self.assertEqual(props["text"], value["masks"][1]["text"])
        self.assertEqual((float(props["fs"]), float(props["scale"])), (.08, 1.25))
        changed = copy.deepcopy(value)
        changed["masks"][1].update(text="Changed annotation", fontSize=.12)
        self.rpc("imageOcclusion.updateNote", {"noteId": nid, "expectedRevision": fetched["revision"], "occlusion": changed})
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], changed)
        self.assertEqual(self.col.get_note(nid).card_ids(), added["cardIds"])
        self.col.undo()
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], value)
        self.col.redo()
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], changed)

    def test_native_imported_text_and_formatting_survive_editing(self):
        self.col.add_image_occlusion_note(self.io, str(self.image),
            '{{c2::image-occlusion:rect:left=.1:top=.2:width=.3:height=.4:angle=1250:fill=navy}}'
            '{{c0::image-occlusion:text:left=.2:top=.1:text=Native\\: text:scale=1.5:fs=.07:fill=teal}}', "header", "back", [])
        nid = self.col.db.scalar("select id from notes")
        fetched = self.rpc("imageOcclusion.getNote", {"noteId": nid})
        rect = next(m for m in fetched["occlusion"]["masks"] if m["kind"] == "rectangle")
        text = next(m for m in fetched["occlusion"]["masks"] if m["kind"] == "text")
        self.assertEqual((rect["angle"], rect["fill"]), (1250, "navy"))
        self.assertEqual((text["text"], text["fontSize"], text["scale"], text["fill"]), ("Native: text", .07, 1.5, "teal"))
        cards = self.col.card_ids_of_note(nid)
        self.rpc("imageOcclusion.updateNote", {"noteId": nid, "occlusion": fetched["occlusion"], "expectedRevision": fetched["revision"]})
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], fetched["occlusion"])
        self.assertEqual(self.col.card_ids_of_note(nid), cards)

    def test_invalid_text_and_formatting_fail_before_writing(self):
        value = occlusion(masks=[mask(), annotation()])
        nid = self.add_io(value)["noteId"]
        for field, bad in [("text", "closes }} early"), ("text", "bad\x00value"), ("text", "bad\ud800"), ("text", "x" * 4097), ("fill", "a" * 256),
                           ("fontSize", 0), ("fontSize", 11), ("scale", 0), ("scale", 101),
                           ("angle", 10001), ("fill", 'red" onmouseover="bad'), ("fill", "url(https://example.invalid/image)")]:
            invalid = copy.deepcopy(value)
            invalid["masks"][1][field] = bad
            before = (self.col.get_note(nid).fields, self.col.undo_status(), self.col.note_count())
            with self.subTest(field=field, bad=repr(bad)):
                self.error("imageOcclusion.updateNote", {"noteId": nid, "occlusion": invalid}, "invalid_params")
                self.error("imageOcclusion.addNote", {"deckId": 1, "notetypeId": self.io, "occlusion": invalid}, "invalid_params")
                self.assertEqual((self.col.get_note(nid).fields, self.col.undo_status(), self.col.note_count()), before)

    def test_same_timestamp_edit_conflicts_with_content_revision(self):
        value = occlusion()
        nid = self.add_io(value)["noteId"]
        baseline = self.rpc("imageOcclusion.getNote", {"noteId": nid})
        before_mod = self.col.get_note(nid).mod
        changed = copy.deepcopy(value)
        changed["header"] = "Other editor changed this header"
        self.rpc("imageOcclusion.updateNote", {"noteId": nid, "occlusion": changed})
        self.col.db.execute("update notes set mod=? where id=?", before_mod, nid)
        current = self.rpc("imageOcclusion.getNote", {"noteId": nid})
        self.assertNotEqual(current["revision"], baseline["revision"])
        before = (self.col.get_note(nid).fields, self.col.undo_status())
        self.error("imageOcclusion.updateNote", {"noteId": nid, "occlusion": value, "expectedRevision": baseline["revision"]}, "conflict")
        self.assertEqual((self.col.get_note(nid).fields, self.col.undo_status()), before)
        self.error("imageOcclusion.updateNote", {"noteId": nid, "occlusion": value, "expectedRevision": ""}, "invalid_params")

    def test_unrecognized_native_data_is_not_silently_dropped(self):
        nid = self.add_io(occlusion())["noteId"]
        for data in ["rect:left=.1:top=.2:width=.3:height=.4:future=value",
                     "rect:left=.1:left=.2:top=.2:width=.3:height=.4",
                     "text:left=.1:top=.2:text=label:scale=nan",
                     "rect:left=.1:top=.2:width=.3:height=.4:fill=url(bad)",
                     "text:left=.1:top=.2:text=" + "x" * 4097,
                     "rect:left=.1:top=.2:width=.3:height=.4:fill=" + "a" * 256]:
            note = self.col.get_note(nid)
            note.fields[0] = "{{c1::image-occlusion:" + data + "}}"
            self.col.update_note(note)
            before = (self.col.get_note(nid).fields, self.col.undo_status())
            self.error("imageOcclusion.getNote", {"noteId": nid}, "unsupported")
            self.error("imageOcclusion.updateNote", {"noteId": nid, "occlusion": occlusion()}, "unsupported")
            self.assertEqual((self.col.get_note(nid).fields, self.col.undo_status()), before)

    def test_polygon_bounds_match_native_drawn_points(self):
        value = occlusion(masks=[mask(kind="polygon", points=[{"x": .1, "y": .2}, {"x": .9, "y": .2}, {"x": .1, "y": .6}])])
        before = (self.col.note_count(), self.col.undo_status())
        self.error("imageOcclusion.addNote", {"deckId": 1, "notetypeId": self.io, "occlusion": value}, "invalid_params")
        self.assertEqual((self.col.note_count(), self.col.undo_status()), before)
