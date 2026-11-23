"""Opt-in Source+fields de-duplication is atomic inside the native add RPC."""
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import quote

from tests.test_decks import RpcCase
from anki.config import Config


def source(page="source-page", block="source-block", revision="a" * 64):
    values = [(key, value) for key, value in (("block", block), ("rev", revision)) if value is not None]
    return "canonnotes:page/" + quote(page, safe="") + ("?" + "&".join(key + "=" + quote(value, safe="") for key, value in values) if values else "")


class SourceDeduplicationTests(RpcCase):
    def setUp(self):
        super().setUp()
        self.basic, _, self.cloze, _ = self.rpc("provenance.ensureNotetypes")["notetypeIds"]

    def params(self, **changes):
        return {"notetypeId": self.basic, "deckId": 1,
                "fields": ["Generated question", "Generated answer", source()],
                "tags": ["canonnotes::from-note"], "deduplicateBySource": True, **changes}

    def add(self, **changes):
        return self.rpc("notes.add", self.params(**changes))

    def snapshot(self):
        return {"notes": self.col.db.all("select * from notes order by id"),
                "cards": self.col.db.all("select * from cards order by id"),
                "revlog": self.col.db.all("select * from revlog order by id"),
                "col": self.col.db.all("select mod,scm,ls from col"), "undo": self.col.undo_status()}

    def test_repeat_add_reuses_exact_note_without_a_native_write(self):
        first = self.add()
        self.assertFalse(first["deduplicated"])
        card = self.col.get_card(first["cardIds"][0])
        card.reps, card.lapses, card.ivl = 7, 2, 12
        self.col.update_card(card)
        before = self.snapshot()
        repeated = self.add(tags=["a-different-tag"])
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(repeated["noteId"], first["noteId"])
        self.assertEqual(repeated["cardIds"], first["cardIds"])
        self.assertEqual(repeated["duplicateState"], "duplicate")
        self.assertFalse(any(repeated["changes"].values()))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.col.get_note(first["noteId"]).tags, ["canonnotes::from-note"])

    def test_distinct_answer_source_revision_or_type_remains_distinct(self):
        first = self.add()
        answers = self.add(fields=["Generated question", "Another answer", source()])
        pages = self.add(fields=["Generated question", "Generated answer", source(page="another-page")])
        revisions = self.add(fields=["Generated question", "Generated answer", source(revision="b" * 64)])
        clone = deepcopy(self.col.models.get(self.basic))
        clone["id"], clone["name"] = 0, "User clone"
        model_id = self.col.models.add_dict(clone).id
        other_type = self.add(notetypeId=model_id)
        self.assertEqual(len({entry["noteId"] for entry in [first, answers, pages, revisions, other_type]}), 5)
        self.assertTrue(all(not entry["deduplicated"] for entry in [first, answers, pages, revisions, other_type]))

    def test_multiple_source_order_encoding_and_duplicate_lines_are_canonical(self):
        left, right = source(page="page space", block=None), source(page="z", revision=None)
        first = self.add(fields=["Q", "A", right + "\n" + left + "\n" + right])
        stored = self.col.get_note(first["noteId"])
        self.assertEqual(stored["Source"], "\n".join(sorted([left, right])))
        repeat = self.add(fields=["Q", "A", left.replace("%20", " ") + "\n" + right])
        self.assertTrue(repeat["deduplicated"])
        self.assertEqual(repeat["noteId"], first["noteId"])

    def test_imported_equivalent_source_format_is_reused_without_rewriting(self):
        left, right = source(page="page space", block=None), source(page="z", revision=None)
        imported = self.col.new_note(self.col.models.get(self.basic))
        imported.fields = ["Q", "A", "  " + right + "\n" + left.replace("%20", " ").replace("a" * 64, "A" * 64) + "\n" + right + "  "]
        self.col.add_note(imported, 1)
        before = self.snapshot()
        repeated = self.add(fields=["Q", "A", left + "\n" + right])
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(repeated["noteId"], imported.id)
        self.assertEqual(self.snapshot(), before)

    def test_source_count_and_canonical_encoding_limits_fail_before_writing(self):
        values = ["\n".join(source(page=f"page-{index}") for index in range(1001)),
                  "canonnotes:page/" + "\U0001f49a" * 250,
                  "\n".join("canonnotes:page/" + "\u00e9" * 250 + str(index) for index in range(1000))]
        for value in values:
            with self.subTest(length=len(value)):
                before = self.snapshot()
                self.error("notes.add", self.params(fields=["Q", "A", value]), "invalid_params")
                self.assertEqual(self.snapshot(), before)

    def test_candidate_limits_are_checked_before_loading_large_fields(self):
        first = self.add()
        for rows in [[(first["noteId"], 1)] * 10001, [(first["noteId"], 16 * 1024 * 1024 + 1)]]:
            with self.subTest(rows=len(rows), bytes=rows[0][1]):
                before = self.snapshot()
                read = self.col.db.all
                def bounded_rows(query, *args):
                    return rows if "length(cast(flds as blob))" in query else read(query, *args)
                with patch.object(self.col.db, "all", side_effect=bounded_rows), patch.object(self.col, "get_note") as get_note:
                    self.error("notes.add", self.params(), "unsupported")
                    get_note.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_source_first_field_and_invalid_imported_links_do_not_confuse_matching(self):
        clone = deepcopy(self.col.models.get(self.basic))
        clone["id"], clone["name"] = 0, "Source first"
        clone["flds"] = [clone["flds"][2], *clone["flds"][:2]]
        for field in clone["flds"]:
            field["ord"] = None
        model_id = self.col.models.add_dict(clone).id
        imported = self.col.new_note(self.col.models.get(model_id))
        imported.fields = [source() + "\n" + source(), "Q", "A"]
        self.col.add_note(imported, 1)
        before = self.snapshot()
        repeated = self.add(notetypeId=model_id, fields=[source(), "Q", "A"])
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(repeated["noteId"], imported.id)
        self.assertEqual(self.snapshot(), before)
        invalid = self.col.new_note(self.col.models.get(self.basic))
        invalid.fields = ["Q", "A", source() + "\ninvalid source"]
        self.col.add_note(invalid, 1)
        distinct = self.add(fields=["Q", "A", source()])
        self.assertFalse(distinct["deduplicated"])
        self.assertNotEqual(distinct["noteId"], invalid.id)

    def test_exact_source_count_limit_and_html_text_equality(self):
        links = "\n".join(source(page=f"page-{index}") for index in range(1000))
        first = self.add(fields=["<b>Q</b>", "A", links])
        repeat = self.add(fields=["<b>Q</b>", "A", links])
        distinct = self.add(fields=["Q", "A", links])
        self.assertEqual(repeat["noteId"], first["noteId"])
        self.assertNotEqual(distinct["noteId"], first["noteId"])
        self.assertEqual(len(self.col.get_note(first["noteId"])["Source"].splitlines()), 1000)

    def test_native_unicode_normalization_is_respected_without_rewriting_existing_fields(self):
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        first = self.add(fields=["Caf\u00e9", "R\u00e9ponse", source()])
        before = self.snapshot()
        repeated = self.add(fields=["Cafe\u0301", "Re\u0301ponse", source()])
        self.assertEqual(repeated["noteId"], first["noteId"])
        self.assertEqual(self.snapshot(), before)
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        distinct = self.add(fields=["Cafe\u0301", "Re\u0301ponse", source()])
        self.assertFalse(distinct["deduplicated"])
        self.assertNotEqual(distinct["noteId"], first["noteId"])

    def test_nfc_equivalent_imported_answer_is_reused_after_normalization_enabled(self):
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        imported = self.col.new_note(self.col.models.get(self.basic))
        imported.fields = ["Question", "Re\u0301ponse", source()]
        self.col.add_note(imported, 1)
        self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
        before = self.snapshot()
        repeated = self.add(fields=["Question", "R\u00e9ponse", source()])
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(repeated["noteId"], imported.id)
        self.assertEqual(self.col.get_note(imported.id).fields[1], "Re\u0301ponse")
        self.assertEqual(self.snapshot(), before)

    def test_cloze_cards_keep_ordinals_and_existing_cards_on_repeat(self):
        fields = ["One {{c1::alpha}} and {{c2::beta}}", "Extra", source()]
        first = self.add(notetypeId=self.cloze, fields=fields)
        self.assertEqual(len(first["cardIds"]), 2)
        repeated = self.add(notetypeId=self.cloze, fields=fields)
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(repeated["cardIds"], first["cardIds"])
        self.assertEqual([self.col.get_card(value).ord for value in first["cardIds"]], [0, 1])

    def test_invalid_or_missing_source_is_rejected_before_writing(self):
        for value in ["", "https://example.test/page", source() + "\nnot-a-source", "canonnotes:page/%ZZ", source(revision="bad")]:
            with self.subTest(value=value):
                before = self.snapshot()
                self.error("notes.add", self.params(fields=["Q", "A", value]), "invalid_params")
                self.assertEqual(self.snapshot(), before)
        native_basic = self.col.models.by_name("Basic")["id"]
        self.error("notes.add", self.params(notetypeId=native_basic, fields=["Q", "A"]), "unsupported")

    def test_default_duplicate_behavior_is_unchanged(self):
        values = self.params()
        values.pop("deduplicateBySource")
        first = self.rpc("notes.add", values)
        self.assertNotIn("deduplicated", first)
        self.error("notes.add", values, "conflict")
        second = self.rpc("notes.add", {**values, "allowDuplicate": True})
        self.assertNotIn("deduplicated", second)
        self.assertNotEqual(first["noteId"], second["noteId"])

    def test_field_name_guard_prevents_stale_mapping_before_any_write(self):
        first = self.add(expectedFieldNames=["Front", "Back", "Source"])
        before = self.snapshot()
        self.error("notes.add", self.params(expectedFieldNames=["Back", "Front", "Source"]), "conflict")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.add(expectedFieldNames=["Front", "Back", "Source"])["noteId"], first["noteId"])

    def test_escaped_artifact_text_does_not_create_native_audio_or_html(self):
        added = self.add(fields=["&lt;script&gt;literal&lt;/script&gt; &#91;sound:foreign.mp3&#93;", "Answer", source()])
        rendered = self.rpc("cards.preview", {"cardId": added["cardIds"][0]})
        self.assertEqual(rendered["av"], {"question": [], "answer": []})
        self.assertNotIn("<script>", rendered["questionHtml"])
        self.assertIn("&lt;script&gt;literal&lt;/script&gt;", rendered["questionHtml"])

    def test_new_add_has_native_undo_and_retry_after_undo_adds_once(self):
        first = self.add()
        self.col.undo()
        self.assertEqual(self.col.note_count(), 0)
        second = self.add()
        self.assertFalse(second["deduplicated"])
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(self.add()["noteId"], second["noteId"])
        self.assertEqual(self.col.card_count(), 1)

    def test_invalid_deck_and_empty_or_invalid_cloze_do_not_reuse_a_note(self):
        self.add()
        before = self.snapshot()
        self.error("notes.add", self.params(deckId=9007199254740991), "not_found")
        self.error("notes.add", self.params(fields=["", "A", source()]), "invalid_params")
        self.error("notes.add", self.params(notetypeId=self.cloze, fields=["{{c501::bad}}", "A", source()]), "invalid_params")
        self.assertEqual(self.snapshot(), before)

    def test_nfc_equivalent_imported_question_is_reused_after_normalization_enabled(self):
        for stored, current in [("Cafe\u0301", "Caf\u00e9"),
                                ("\u00e9 e\u0301", "\u00e9 \u00e9"),
                                ("\u212aelvin", "Kelvin")]:
            with self.subTest(stored=stored):
                self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
                imported = self.col.new_note(self.col.models.get(self.basic))
                imported.fields = [stored, "Answer", source()]
                self.col.add_note(imported, 1)
                self.col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, True)
                before = self.snapshot()
                repeated = self.add(fields=[current, "Answer", source()])
                self.assertTrue(repeated["deduplicated"])
                self.assertEqual(repeated["noteId"], imported.id)
                self.assertEqual(self.col.get_note(imported.id).fields[0], stored)
                self.assertEqual(self.snapshot(), before)
