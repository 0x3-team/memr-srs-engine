"""Native occlusion data, card identity and safe media boundaries."""
import copy
from pathlib import Path
import re
import struct
import tempfile
from unittest.mock import patch
import zlib
from urllib.parse import unquote

from tests.test_notes import NotesCase, HTML, MISSING
from tests.helpers import EnginePipe


def png(width=2, height=3):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    scanlines = (b"\0" + bytes([255, 0, 0, 255]) * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


def mask(identity="rectangle", **extra):
    return {"id": identity, "kind": "rectangle", "left": 0.1, "top": 0.2, "width": 0.3,
            "height": 0.4, "points": [], "group": None, "label": "", "inactive": False, **extra}


def occlusion(**extra):
    return {"image": "sample.png", "masks": [mask()], "mode": "hideAllGuessOne",
            "header": HTML, "backExtra": "<p>Back extra</p>", "tags": ["image::sample"], **extra}


class ImageOcclusionTests(NotesCase):
    def setUp(self):
        super().setUp()
        self.col.add_image_occlusion_notetype()
        self.io = self.col.models.by_name("Image Occlusion")["id"]
        self.image = Path(self.col.media.dir()) / "sample.png"
        self.image.write_bytes(png())

    def add_io(self, value=None, **extra):
        return self.rpc("imageOcclusion.addNote", {"notetypeId": self.io, "deckId": 1,
                                                   "occlusion": value or occlusion(), **extra})

    def test_all_methods_registered_and_safe_image_metadata(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("imageOcclusion.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)
        before = (self.image.read_bytes(), self.col.undo_status())
        self.assertEqual(self.rpc("imageOcclusion.getImage", {"filename": "sample.png"}),
                         {"filename": "sample.png", "width": 2, "height": 3})
        self.assertEqual((self.image.read_bytes(), self.col.undo_status()), before)
        svg = Path(self.col.media.dir()) / "sample.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 17 31"><script>throw 1</script></svg>')
        self.assertEqual(self.rpc("imageOcclusion.getImage", {"filename": "sample.svg"}),
                         {"filename": "sample.svg", "width": 17, "height": 31})

    def test_add_get_all_shapes_groups_modes_and_native_undo_redo(self):
        value = occlusion(masks=[mask("rect", group="group"), mask("ellipse", kind="ellipse", group="group"),
                                mask("polygon", kind="polygon", points=[{"x": .1, "y": .2},
                                     {"x": .4, "y": .2}, {"x": .1, "y": .6}]),
                                mask("inactive", inactive=True, label="label : \\ {{c4::text}} فارسی")])
        result = self.add_io(value)
        nid = result["noteId"]
        self.assertEqual(len(result["cardIds"]), 2)
        fetched = self.rpc("imageOcclusion.getNote", {"noteId": nid})
        self.assertRegex(fetched.pop("revision"), r"^[a-f0-9]{64}$")
        self.assertEqual(fetched, {"noteId": nid, "notetypeId": self.io, "occlusion": value})
        native = self.col.get_image_occlusion_note(nid)
        self.assertTrue(native.note.occlude_inactive)
        self.assertEqual(sorted(oc.ordinal for oc in native.note.occlusions), [0, 1, 2])
        self.assertIn("image-occlusion:ellipse", self.col.get_note(nid).fields[0])
        self.col.undo()
        self.error("imageOcclusion.getNote", {"noteId": nid}, "not_found")
        self.col.redo()
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], value)

    def test_add_to_requested_deck_preserves_selection_and_undo(self):
        deck = self.create("Target")
        current = self.col.decks.selected()
        added = self.add_io(deckId=float(deck), notetypeId=float(self.io))
        self.assertEqual(self.col.decks.selected(), current)
        self.assertEqual({self.col.get_card(cid).did for cid in added["cardIds"]}, {deck})
        self.col.undo()
        self.assertEqual(self.col.decks.selected(), current)
        self.assertEqual(self.col.note_count(), 0)
        self.col.redo()
        self.assertEqual(self.col.decks.selected(), current)
        self.assertEqual({self.col.get_card(cid).did for cid in added["cardIds"]}, {deck})

    def test_update_reorders_masks_preserves_card_scheduling_and_changes_image(self):
        value = occlusion(masks=[mask("one"), mask("two", kind="ellipse")])
        added = self.add_io(value)
        nid, card_ids = added["noteId"], added["cardIds"]
        card = self.col.get_card(card_ids[0])
        card.type, card.queue, card.due, card.ivl, card.factor = 2, 2, 42, 12, 2500
        self.col.update_card(card)
        scheduled = (card.id, card.ord, card.type, card.queue, card.due, card.ivl, card.factor)
        other = Path(self.col.media.dir()) / "other.png"
        other.write_bytes(png(5, 4))
        updated = occlusion(image="other.png", mode="hideOneGuessOne", header="new header", tags=["new"],
                            masks=[mask("two", kind="ellipse", left=.2, label="two"), mask("one")])
        self.rpc("imageOcclusion.updateNote", {"noteId": nid, "occlusion": updated})
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], updated)
        changed = self.col.get_card(card.id)
        self.assertEqual((changed.id, changed.ord, changed.type, changed.queue, changed.due, changed.ivl, changed.factor), scheduled)
        self.assertEqual(self.col.get_note(nid).card_ids(), card_ids)
        self.col.undo()
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], value)
        self.col.redo()
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], updated)

    def test_imported_native_masks_get_stable_fallback_identity(self):
        self.col.add_image_occlusion_note(self.io, str(self.image),
            '{{c2::image-occlusion:rect:left=.1:top=.2:width=.3:height=.4:oi=1}}'
            '{{c2::image-occlusion:ellipse:left=.2:top=.2:rx=.1:ry=.2:oi=1}}', "header", "back", [])
        nid = self.col.db.scalar("select id from notes")
        fetched = self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"]
        self.assertEqual([m["group"] for m in fetched["masks"]], ["group-2", "group-2"])
        cards = self.col.card_ids_of_note(nid)
        self.rpc("imageOcclusion.updateNote", {"noteId": nid, "occlusion": fetched})
        self.assertEqual(self.col.card_ids_of_note(nid), cards)
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], fetched)

    def test_removed_mask_ordinals_never_transfer_review_history_to_new_masks(self):
        value = occlusion(masks=[mask("one"), mask("two")])
        added = self.add_io(value)
        nid = added["noteId"]
        old = self.col.get_card(added["cardIds"][0])
        old.reps, old.due, old.ivl, old.type, old.queue = 91, 42, 12, 2, 2
        self.col.update_card(old)
        retained = self.col.get_card(added["cardIds"][1])
        changed = occlusion(masks=[mask("two"), mask("new")])
        self.rpc("imageOcclusion.updateNote", {"noteId": nid, "occlusion": changed})
        cards = {card.ord: card for card in self.col.get_note(nid).cards()}
        self.assertEqual((cards[0].id, cards[0].reps), (old.id, 91))
        self.assertEqual(cards[1].id, retained.id)
        self.assertIn(2, cards)
        self.assertNotEqual(cards[2].id, old.id)
        self.assertEqual(cards[2].reps, 0)
        # A second edit cannot reuse the orphaned c1 card either.
        self.rpc("imageOcclusion.updateNote", {"noteId": nid,
                 "occlusion": occlusion(masks=[mask("two"), mask("new"), mask("one")])})
        cards = {card.ord: card for card in self.col.get_note(nid).cards()}
        self.assertIn(3, cards)
        self.assertNotEqual(cards[3].id, old.id)
        self.assertEqual(cards[3].reps, 0)

    def test_media_names_with_percent_signs_are_literal_names_and_never_decoded(self):
        # Anki media names may contain %. The engine never decodes a name, so the sign is an
        # ordinary character; "50%25.png" is that exact file, not an alias of "50%.png".
        for name, size in (("50%.png", (2, 3)), ("50%25.png", (5, 7)), ("100% done.png", (4, 4))):
            (Path(self.col.media.dir()) / name).write_bytes(png(*size))
        self.assertEqual(self.rpc("imageOcclusion.getImage", {"filename": "50%.png"}),
                         {"filename": "50%.png", "width": 2, "height": 3})
        self.assertEqual(self.rpc("imageOcclusion.getImage", {"filename": "50%25.png"}),
                         {"filename": "50%25.png", "width": 5, "height": 7})
        added = self.add_io(occlusion(image="100% done.png"))
        fetched = self.rpc("imageOcclusion.getNote", {"noteId": added["noteId"]})
        self.assertEqual(fetched["occlusion"]["image"], "100% done.png")
        self.rpc("imageOcclusion.updateNote", {"noteId": added["noteId"], "occlusion": occlusion(image="50%25.png"),
                                               "expectedRevision": fetched["revision"]})
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": added["noteId"]})["occlusion"]["image"], "50%25.png")
        for filename in ("50%/outside.png", "50%\\outside.png", "..%2foutside.png", "%5c..%5coutside.png", "%\x00.png", "50%.png "):
            self.error("imageOcclusion.getImage", {"filename": filename}, "invalid_params")

    def test_study_loads_the_image_the_editor_shows_for_names_that_a_url_would_reinterpret(self):
        # Review round: native Anki stores the literal media name in the Image field, and a browser
        # resolves the rendered src as a URL that the gateway decodes exactly once. "50%25.png" must
        # load "50%25.png", not "50%.png"; a bare "50%.png" must not become a malformed escape.
        sizes = {"50%25.png": (5, 7), "50%.png": (2, 3), "100% done.png": (4, 4), "a#b.png": (6, 6), "sample.png": (2, 3)}
        for name, size in sizes.items():
            (Path(self.col.media.dir()) / name).write_bytes(png(*size))
        shown = None
        for name, size in sizes.items():
            added = self.add_io(occlusion(image=name))
            nid = added["noteId"]
            self.assertEqual(self.col.get_note(nid).fields[1], f'<img src="{name}">', "the stored field keeps the literal name")
            editor = self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"]["image"]
            self.assertEqual(editor, name)
            renders = [self.rpc("cards.preview", {"cardId": added["cardIds"][0]})]
            if shown is None and name == "50%25.png":
                shown = self.rpc("study.next", {"deckId": 1})
                renders.append(shown)
            for render in renders:
                for html in (render["questionHtml"], render["answerHtml"]):
                    references = re.findall(r'<img src="([^"]*)"', html)
                    self.assertEqual(len(references), 1, html)
                    # The gateway decodes the URL path once; that is what Study loads.
                    self.assertEqual(unquote(references[0], errors="strict"), name, f"Study must load {name}")
                    self.assertRegex(references[0], r"^(?:[^%\s#]|%[0-9A-F]{2})+$", "no raw % or fragment is left in the URL")
                    loaded = Path(self.col.media.dir()) / unquote(references[0], errors="strict")
                    self.assertEqual(loaded.read_bytes(), png(*size))
        self.assertIsNotNone(shown, "the Study queue rendered the occlusion card")

    def test_study_decodes_entities_in_a_stored_src_once_and_then_encodes_the_name(self):
        # Review round 2: an imported Image field holds the HTML text of the attribute, so
        # src="a&amp;b.png" names the file "a&b.png". The browser decodes the entity before it builds
        # the URL; the engine must do the same and only then percent-encode, or Study asks the media
        # route for the literal "a&amp;b.png". "%" and "&" together must each be handled once.
        cases = [
            ("a&b.png", "a&amp;b.png"),
            ("c&d.png", "c&#38;d.png"),
            ("e&f.png", "e&#x26;f.png"),
            ("50%25&b.png", "50%25&amp;b.png"),
            ("100%&more.png", "100%&amp;more.png"),
            ("a&amp;b.png", "a&amp;amp;b.png"),
            # Without a semicolon a legacy entity name is left alone inside an attribute value
            # when a letter or "=" follows, so these names are already literal.
            ("q&notes.png", "q&notes.png"),
            ("r&amp=1.png", "r&amp=1.png"),
        ]
        for index, (name, written) in enumerate(cases):
            with self.subTest(name=name, written=written):
                size = (3 + index, 4)
                (Path(self.col.media.dir()) / name).write_bytes(png(*size))
                nid = self.add_io(occlusion(image=name))["noteId"]
                note = self.col.get_note(nid)
                note.fields[1] = f'<img src="{written}">'
                self.col.update_note(note)
                self.assertEqual(self.col.get_note(nid).fields[1], f'<img src="{written}">', "the stored field is untouched")
                card = self.col.get_note(nid).card_ids()[0]
                renders = [self.rpc("cards.preview", {"cardId": card})]
                for render in renders:
                    for html in (render["questionHtml"], render["answerHtml"]):
                        references = re.findall(r'<img src="([^"]*)"', html)
                        self.assertEqual(len(references), 1, html)
                        self.assertNotIn("&", references[0], "the entity text must not reach the URL")
                        self.assertEqual(unquote(references[0], errors="strict"), name, f"Study must load {name}")
                        loaded = Path(self.col.media.dir()) / unquote(references[0], errors="strict")
                        self.assertEqual(loaded.read_bytes(), png(*size))

    def test_missing_and_unsafe_image_references_fail_before_native_read(self):
        for filename in ("../outside.png", "..\\outside.png", "/absolute.png", ".hidden.png", 'bad".png', "bad\x00.png"):
            self.error("imageOcclusion.getImage", {"filename": filename}, "invalid_params")
        self.error("imageOcclusion.getImage", {"filename": "missing.png"}, "not_found")
        link = Path(self.col.media.dir()) / "linked.png"
        link.symlink_to(self.image)
        self.error("imageOcclusion.getImage", {"filename": "linked.png"}, "invalid_params")
        nid = self.add_io()["noteId"]
        note = self.col.get_note(nid)
        note.fields[1] = '<img src="../../outside.png">'
        self.col.update_note(note)
        with patch.object(self.col, "get_image_occlusion_note", side_effect=AssertionError("unsafe backend read")):
            self.error("imageOcclusion.getNote", {"noteId": nid}, "invalid_params")

    def test_missing_media_still_exposes_editable_native_masks(self):
        nid = self.add_io()["noteId"]
        self.image.unlink()
        self.assertEqual(self.rpc("imageOcclusion.getNote", {"noteId": nid})["occlusion"], occlusion())
        self.error("imageOcclusion.updateNote", {"noteId": nid, "occlusion": occlusion()}, "not_found")

    def test_invalid_geometry_ids_polygons_unicode_and_field_size_are_atomic(self):
        nid = self.add_io()["noteId"]
        invalid = [occlusion(masks=[]), occlusion(masks=[mask(inactive=True)]),
                   occlusion(masks=[mask(width=0)]), occlusion(masks=[mask(left=.9)]),
                   occlusion(masks=[mask(), mask()]), occlusion(masks=[mask(identity="")]),
                   occlusion(masks=[mask(group="")]), occlusion(masks=[mask(label="\ud800")]),
                   occlusion(masks=[mask(points=[{"x": 0, "y": 0}])]),
                   occlusion(masks=[mask(kind="polygon", points=[{"x": 0, "y": 0}] * 3)]),
                   occlusion(header="bad\x1fheader"),
                   occlusion(masks=[mask(str(i), label="L" * 4096) for i in range(200)])]
        for value in invalid:
            with self.subTest(value=str(value)[:100]):
                before = (self.col.note_count(), self.col.undo_status(), self.col.get_note(nid).fields)
                self.error("imageOcclusion.addNote", {"notetypeId": self.io, "deckId": 1, "occlusion": value}, "invalid_params")
                self.error("imageOcclusion.updateNote", {"noteId": nid, "occlusion": value}, "invalid_params")
                self.assertEqual((self.col.note_count(), self.col.undo_status(), self.col.get_note(nid).fields), before)

    def test_missing_ids_wrong_kind_and_invalid_params_every_method(self):
        basic = self.add()["noteId"]
        for method, params in {"getImage": {}, "addNote": {}, "getNote": {}, "updateNote": {}}.items():
            self.error("imageOcclusion." + method, params, "invalid_params")
        for method, params in {"addNote": {"notetypeId": MISSING, "deckId": 1, "occlusion": occlusion()},
                               "getNote": {"noteId": MISSING}, "updateNote": {"noteId": MISSING, "occlusion": occlusion()}}.items():
            self.error("imageOcclusion." + method, params, "not_found")
        self.error("imageOcclusion.addNote", {"notetypeId": self.basic, "deckId": 1, "occlusion": occlusion()}, "invalid_params")
        self.error("imageOcclusion.getNote", {"noteId": basic}, "invalid_params")
        self.error("imageOcclusion.updateNote", {"noteId": basic, "occlusion": occlusion()}, "invalid_params")
        self.error("imageOcclusion.addNote", {"notetypeId": self.io, "deckId": MISSING, "occlusion": occlusion()}, "not_found")

    def test_size_limit_before_read_and_malformed_headers(self):
        large = Path(self.col.media.dir()) / "large.png"
        with large.open("wb") as stream:
            stream.truncate(100 * 1024 * 1024 + 1)
        with patch.object(self.col, "get_image_for_occlusion", side_effect=AssertionError("oversize native read")):
            self.error("imageOcclusion.getImage", {"filename": "large.png"}, "invalid_params")
        for data in (b"bad", b"\xff\xd8\xff", b'<svg width="0" height="2"></svg>',
                     b'<!DOCTYPE svg [<!ENTITY bad "bad">]><svg width="1" height="1"/>'):
            self.image.write_bytes(data)
            self.error("imageOcclusion.getImage", {"filename": "sample.png"}, "invalid_params")

    def test_malformed_imported_native_masks_fail_with_explicit_unsupported(self):
        nid = self.add_io()["noteId"]
        for data in ('rect:left=nan:top=.2:width=.3:height=.4',
                     'rect:left=1.1:top=.2:width=.3:height=.4',
                     'polygon:left=.1:top=.2:points=0,0,0 .2,.2 .3,.3',
                     'text:left=.1:top=.2:text=annotation:scale=nan'):
            note = self.col.get_note(nid)
            note.fields[0] = '{{c1::image-occlusion:' + data + '}}'
            self.col.update_note(note)
            self.error("imageOcclusion.getNote", {"noteId": nid}, "unsupported")

    def test_active_group_limit_and_exhausted_orphan_ordinals_do_not_alias_cards(self):
        value = occlusion(masks=[mask(str(i)) for i in range(500)])
        added = self.add_io(value)
        nid = added["noteId"]
        self.assertEqual(len(added["cardIds"]), 500)
        before = (self.col.note_count(), self.col.undo_status(), self.col.get_note(nid).fields)
        self.error("imageOcclusion.addNote", {"notetypeId": self.io, "deckId": 1,
                   "occlusion": occlusion(masks=[mask(str(i)) for i in range(501)])}, "invalid_params")
        self.error("imageOcclusion.updateNote", {"noteId": nid,
                   "occlusion": occlusion(masks=[mask("new")])}, "unsupported")
        self.assertEqual((self.col.note_count(), self.col.undo_status(), self.col.get_note(nid).fields), before)

    def test_mask_identity_persists_across_process_restart(self):
        with tempfile.TemporaryDirectory(prefix="memr-e03-io-persist-") as directory:
            path = Path(directory) / "collection.anki2"
            from anki.collection import Collection
            col = Collection(str(path))
            col.add_image_occlusion_notetype()
            io = col.models.by_name("Image Occlusion")["id"]
            (Path(col.media.dir()) / "sample.png").write_bytes(png())
            col.close()
            with EnginePipe(directory) as engine:
                added = engine.call("imageOcclusion.addNote", {"notetypeId": io, "deckId": 1, "occlusion": occlusion()})["result"]
            with EnginePipe(directory) as engine:
                self.assertEqual(engine.call("imageOcclusion.getNote", {"noteId": added["noteId"]})["result"]["occlusion"], occlusion())
