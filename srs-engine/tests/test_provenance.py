"""Provenance contracts run on disposable, real Anki note types and cards."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import quote

from anki import import_export_pb2 as pb
from memr_srs_engine.handlers import provenance
from memr_srs_engine.provenance_links import MAX_SOURCE_BYTES, parse_source_uri
from memr_srs_engine.render import _source_uri
from tests.helpers import temp_collection
from tests.test_decks import RpcCase


def link(page="page-example", block="block-example", revision="a" * 64):
    result = "canonnotes:page/" + quote(page, safe="")
    params = [(key, value) for key, value in (("block", block), ("rev", revision)) if value is not None]
    return result + ("?" + "&".join(key + "=" + quote(value, safe="") for key, value in params) if params else "")


class ProvenanceTests(RpcCase):
    def setup_types(self):
        return self.rpc("provenance.ensureNotetypes")["notetypeIds"]

    def linked_note(self, sources, *, model_id=None, front="Generated question"):
        if model_id is None:
            model_id = self.setup_types()[0]
        note = self.col.new_note(self.col.models.get(model_id))
        note.fields[0], note.fields[1] = front, "Generated answer"
        note["Source"] = "\n".join(sources)
        note.add_tag("canonnotes::from-note")
        self.col.add_note(note, 1)
        return note

    def snapshot(self):
        return {
            "notes": self.col.db.all("select * from notes order by id"),
            "cards": self.col.db.all("select * from cards order by id"),
            "revlog": self.col.db.all("select * from revlog order by id"),
            "models": [self.col.models.get(item.id) for item in self.col.models.all_names_and_ids()],
            "undo": self.col.undo_status(),
            "timestamps": self.col.db.all("select mod,scm,ls from col"),
        }

    def test_every_provenance_method_registered(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("provenance.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_creates_four_native_types_without_editing_user_types(self):
        before = {item.id: self.col.models.get(item.id) for item in self.col.models.all_names_and_ids()}
        result = self.rpc("provenance.ensureNotetypes")
        ids = result["notetypeIds"]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(result["changes"]["notetype"])
        for (kind, name), model_id in zip(provenance.NOTETYPES, ids):
            model = self.col.models.get(model_id)
            self.assertEqual(model["name"], name)
            self.assertEqual(model["flds"][-1]["name"], "Source")
            self.assertTrue(model["flds"][-1]["plainText"])
            self.assertTrue(model["flds"][-1]["collapsed"])
            self.assertEqual(model[provenance.OWNER_KEY]["kind"], kind)
            self.assertEqual(model["css"], provenance.DEFAULT_CSS)
            self.assertEqual(len(model["tmpls"]), 2 if kind == "basicReversed" else 1)
            self.assertEqual(model["type"], int(kind == "cloze"))
            self.assertTrue(all("Source" not in template[key] for template in model["tmpls"] for key in ("qfmt", "afmt")))
        self.assertEqual({key: self.col.models.get(key) for key in before}, before)
        self.assertEqual(self.col.get_config(provenance.INDEX_KEY)["ids"],
                         {kind: value for (kind, _), value in zip(provenance.NOTETYPES, ids)})

    def test_setup_is_idempotent_and_does_not_replace_undo(self):
        ids = self.setup_types()
        self.create("Unrelated user action")
        before = self.snapshot()
        result = self.rpc("provenance.ensureNotetypes")
        self.assertEqual(result["notetypeIds"], ids)
        self.assertFalse(any(result["changes"].values()))
        self.assertEqual(self.snapshot(), before)

    def test_setup_has_one_native_undo_and_redo(self):
        self.create("Prior undo action")
        previous = self.col.undo_status().undo
        ids = self.setup_types()
        self.col.undo()
        self.assertTrue(all(self.col.models.get(value) is None for value in ids))
        self.assertIsNone(self.col.get_config(provenance.INDEX_KEY))
        self.assertEqual(self.col.undo_status().undo, previous)
        self.col.redo()
        self.assertEqual(self.setup_types(), ids)

    def test_user_name_collision_is_rejected_before_any_creation(self):
        model = deepcopy(self.col.models.by_name("Basic"))
        model["id"], model["name"] = 0, "Memr Type-in"
        self.col.models.add_dict(model)
        before = self.snapshot()
        self.error("provenance.ensureNotetypes", {}, "conflict")
        self.assertEqual(self.snapshot(), before)

    def test_renamed_owned_type_and_user_clone_preserved(self):
        ids = self.setup_types()
        original = deepcopy(self.col.models.get(ids[0]))
        original["name"] = "My renamed Basic"
        self.col.models.update_dict(original)
        clone = self.col.models.copy(self.col.models.get(ids[0]), add=False)
        clone["name"] = "My custom clone"
        clone["css"] = ".card { text-align: left; }"
        clone_id = self.col.models.add_dict(clone).id
        before = self.snapshot()
        self.assertEqual(self.setup_types(), ids)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.col.models.get(clone_id)["css"], clone["css"])

    def test_deleted_owned_type_does_not_transfer_ownership_to_user_clone(self):
        ids = self.setup_types()
        clone = self.col.models.copy(self.col.models.get(ids[0]), add=False)
        clone["name"] = "User clone after original deletion"
        clone["css"] = ".card { text-align: left; }"
        clone_id = self.col.models.add_dict(clone).id
        self.col.models.remove(ids[0])
        before_clone = self.col.models.get(clone_id)
        replacement = self.setup_types()
        self.assertNotIn(replacement[0], [ids[0], clone_id])
        self.assertEqual(replacement[1:], ids[1:])
        self.assertEqual(self.col.models.get(clone_id), before_clone)

    def test_source_check_response_budget_is_bounded_and_read_only(self):
        note = self.linked_note([link()])
        params = {"sources": [{"pageId": "page-example", "blockId": "block-example", "revision": "a" * 64}]}
        expected = self.rpc("provenance.checkSources", params)
        self.assertEqual(expected["notes"][0]["noteId"], note.id)
        byte_count = len(json.dumps(expected, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        before = self.snapshot()
        with patch.object(provenance, "MAX_CHECK_SOURCES_BYTES", byte_count, create=True):
            self.assertEqual(self.rpc("provenance.checkSources", params), expected)
        with patch.object(provenance, "MAX_CHECK_SOURCES_BYTES", byte_count - 1, create=True):
            self.error("provenance.checkSources", params, "unsupported")
        self.assertEqual(self.snapshot(), before)

    def test_upgrade_adds_hidden_source_and_preserves_content_and_scheduling(self):
        ids = self.setup_types()
        note = self.linked_note([], model_id=ids[0])
        model = deepcopy(self.col.models.get(ids[0]))
        model["flds"].pop()
        model["css"] = ".card { font-size: 20px; }"
        model[provenance.OWNER_KEY].update(schemaVersion=0, managedCss=model["css"])
        self.col.models.update_dict(model)
        before_cards = self.col.db.all("select * from cards order by id")
        before_fields = self.col.get_note(note.id).fields[:]
        result = self.rpc("provenance.ensureNotetypes")
        self.assertEqual(result["notetypeIds"], ids)
        self.assertEqual(self.col.get_note(note.id).fields, before_fields + [""])
        self.assertEqual(self.col.db.all("select * from cards order by id"), before_cards)
        self.assertEqual(self.col.models.get(ids[0])["css"], provenance.DEFAULT_CSS)
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id).fields, before_fields)
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id).fields, before_fields + [""])

    def test_owned_custom_css_is_preserved_during_upgrade(self):
        ids = self.setup_types()
        model = deepcopy(self.col.models.get(ids[0]))
        model[provenance.OWNER_KEY].update(schemaVersion=0, managedCss="old managed CSS")
        model["css"] = ".card { color: blue; }"
        self.col.models.update_dict(model)
        self.setup_types()
        self.assertEqual(self.col.models.get(ids[0])["css"], model["css"])

    def test_owned_template_rendering_source_fails_without_rewriting_it(self):
        ids = self.setup_types()
        model = deepcopy(self.col.models.get(ids[0]))
        model["tmpls"][0]["afmt"] += "{{Source}}"
        self.col.models.update_dict(model)
        before = self.snapshot()
        self.error("provenance.ensureNotetypes", {}, "conflict")
        self.assertEqual(self.snapshot(), before)

    def test_invalid_ownership_and_future_versions_fail_closed(self):
        ids = self.setup_types()
        self.col.set_config(provenance.INDEX_KEY, {"version": 1, "ids": {"basic": "unsafe"}}, undoable=True)
        before = self.snapshot()
        self.error("provenance.ensureNotetypes", {}, "conflict")
        self.assertEqual(self.snapshot(), before)
        self.col.undo()
        model = deepcopy(self.col.models.get(ids[0]))
        model[provenance.OWNER_KEY]["schemaVersion"] = 2
        self.col.models.update_dict(model)
        before = self.snapshot()
        self.error("provenance.ensureNotetypes", {}, "unsupported")
        self.assertEqual(self.snapshot(), before)

    def test_interrupted_setup_retains_ownership_and_retry_does_not_duplicate(self):
        original = self.col.models.add_dict
        calls = []
        def interrupted(model):
            calls.append(model["name"])
            if len(calls) == 2:
                raise RuntimeError("injected native write failure")
            return original(model)
        with patch.object(self.col.models, "add_dict", side_effect=interrupted):
            self.error("provenance.ensureNotetypes", {}, "internal")
        first_id = self.col.models.id_for_name("Memr Basic")
        self.assertIsNotNone(first_id)
        self.assertEqual(self.col.models.get(first_id)[provenance.OWNER_KEY]["kind"], "basic")
        ids = self.setup_types()
        self.assertEqual(ids[0], first_id)
        self.assertEqual(len([item for item in self.col.models.all_names_and_ids() if item.name.startswith("Memr")]), 4)

    def test_multiline_sources_are_decoded_deduplicated_and_not_rendered(self):
        sources = [link("page with spaces/موضوع", "block&one"), link("page-two", None, None)]
        note = self.linked_note(["not a source", sources[0], sources[0], sources[1]])
        result = self.rpc("provenance.sourceOf", {"noteId": note.id})["sources"]
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["pageId"], "page with spaces/موضوع")
        self.assertEqual(result[0]["blockId"], "block&one")
        self.assertIsNone(result[1]["revision"])
        self.assertEqual(self.rpc("notes.get", {"noteId": note.id})["source"], result[0])
        render = self.rpc("cards.preview", {"cardId": int(note.card_ids()[0])})
        self.assertEqual(render["source"], result[0])
        self.assertNotIn("canonnotes:page/", render["questionHtml"] + render["answerHtml"])

    def test_source_parsers_reject_the_same_malformed_and_ambiguous_uris(self):
        invalid = ["https://example.invalid/page", "canonnotes://page/id", "canonnotes:page/",
                   "canonnotes:page/p?block=", "canonnotes:page/p?other=x", "canonnotes:page/p?block=a&block=b",
                   "canonnotes:page/p?rev=" + "a" * 63, "canonnotes:page/p?rev=" + "g" * 64,
                   "canonnotes:page/p#fragment", "canonnotes:page/%zz", "canonnotes:page/%FF",
                   "canonnotes:page/%00", "canonnotes:page/p?block=%0A", "canonnotes:page/%C2%80",
                   "canonnotes:page/p?block=%C2%9F", "canonnotes:page/" + "p" * 257]
        note = self.linked_note(invalid + [link("valid-page")])
        for uri in invalid:
            with self.subTest(uri=uri[:80]):
                self.assertIsNone(parse_source_uri(uri))
                self.assertIsNone(_source_uri(uri))
        self.assertEqual(self.rpc("provenance.sourceOf", {"noteId": note.id})["sources"][0]["pageId"], "valid-page")
        self.assertEqual(self.rpc("notes.get", {"noteId": note.id})["source"]["pageId"], "valid-page")

    def test_reverse_lookup_uses_exact_page_and_optional_block_identities(self):
        ids = self.setup_types()
        page = 'page*" OR deck:*'
        first = self.linked_note([link(page, "first")], model_id=ids[1], front="Reversed example")
        second = self.linked_note([link(page, "second")], model_id=ids[0], front="Basic example")
        self.linked_note([link(page + "-suffix", "first")], model_id=ids[0], front="Different source")
        result = self.rpc("provenance.cardsForSource", {"pageId": page})
        self.assertEqual(result["noteIds"], sorted([first.id, second.id]))
        self.assertEqual(result["cardIds"], sorted(list(first.card_ids()) + list(second.card_ids())))
        exact = self.rpc("provenance.cardsForSource", {"pageId": page, "blockId": "first"})
        self.assertEqual(exact["noteIds"], [first.id])
        self.assertEqual(self.rpc("provenance.cardsForSource", {"pageId": "absent"}), {"cardIds": [], "noteIds": []})

    def test_source_checks_distinguish_changed_missing_and_untracked_without_writes(self):
        entries = [link("current"), link("changed"), link("missing"), link("untracked", revision=None), link("unchecked")]
        note = self.linked_note(entries)
        before = self.snapshot()
        result = self.rpc("provenance.checkSources", {"sources": [
            {"pageId": "current", "blockId": "block-example", "revision": "A" * 64},
            {"pageId": "changed", "blockId": "block-example", "revision": "b" * 64},
            {"pageId": "missing", "blockId": "block-example", "revision": None},
            {"pageId": "untracked", "blockId": "block-example", "revision": "a" * 64},
        ]})
        self.assertEqual(result["notes"][0]["noteId"], note.id)
        checked = result["notes"][0]["sources"]
        self.assertEqual([source["status"] for source in checked], ["current", "changed", "missing", "untracked", "unchecked"])
        self.assertEqual([source["changed"] for source in checked], [False, True, None, None, None])
        self.rpc("provenance.sourceOf", {"noteId": note.id})
        self.rpc("provenance.cardsForSource", {"pageId": "current"})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rpc("provenance.checkSources", {"sources": []}), {"notes": []})

    def test_invalid_parameters_and_unknown_note_are_safe(self):
        self.setup_types()
        before = self.snapshot()
        for method, params in [
            ("provenance.ensureNotetypes", {"extra": True}),
            ("provenance.sourceOf", {"noteId": -1}),
            ("provenance.cardsForSource", {"pageId": ""}),
            ("provenance.cardsForSource", {"pageId": "a\x00"}),
            ("provenance.cardsForSource", {"pageId": "page", "blockId": ""}),
            ("provenance.checkSources", {}),
            ("provenance.checkSources", {"sources": [{"pageId": "p", "blockId": None, "revision": ""}]}),
            ("provenance.checkSources", {"sources": [{"pageId": "p", "blockId": None, "revision": "x" * 64}]}),
            ("provenance.checkSources", {"sources": [{"pageId": "p", "blockId": None, "revision": "a" * 64},
                                                     {"pageId": "p", "blockId": None, "revision": None}]}),
        ]:
            with self.subTest(method=method, params=params):
                self.error(method, params, "invalid_params")
        self.error("provenance.sourceOf", {"noteId": 9007199254740991}, "not_found")
        self.assertEqual(self.snapshot(), before)

    def test_source_count_and_field_size_boundaries_fail_explicitly(self):
        note = self.linked_note([link(f"page-{index}") for index in range(1000)])
        self.assertEqual(len(self.rpc("provenance.sourceOf", {"noteId": note.id})["sources"]), 1000)
        note["Source"] += "\n" + link("overflow")
        self.col.update_note(note)
        self.error("provenance.sourceOf", {"noteId": note.id}, "unsupported")
        note["Source"] = link("oversize") + "x" * MAX_SOURCE_BYTES
        self.col.update_note(note)
        self.error("provenance.sourceOf", {"noteId": note.id}, "unsupported")
        self.error("provenance.cardsForSource", {"pageId": "oversize"}, "unsupported")

    def test_source_and_tag_survive_native_package_export_import(self):
        note = self.linked_note([link()])
        with tempfile.TemporaryDirectory(prefix="memr-provenance-package-") as directory:
            path = str(Path(directory) / "linked.apkg")
            self.col.export_anki_package(out_path=path,
                options=pb.ExportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True,
                                                  with_media=False, legacy=False),
                limit=None)
            with temp_collection() as other:
                other.import_anki_package(pb.ImportAnkiPackageRequest(package_path=path,
                    options=pb.ImportAnkiPackageOptions(merge_notetypes=False, with_scheduling=True, with_deck_configs=True)))
                imported = other.get_note(other.find_notes("tag:canonnotes::from-note")[0])
                self.assertEqual(imported["Source"], note["Source"])
                self.assertIn("canonnotes::from-note", imported.tags)
                self.assertEqual(imported.fields[:2], note.fields[:2])
