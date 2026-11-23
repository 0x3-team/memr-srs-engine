"""Card rendering parity on generated collections with anki==26.9.3."""
from html import escape
from html.parser import HTMLParser
import unittest

from anki import scheduler_pb2

from memr_srs_engine.registry import load_catalogue
from memr_srs_engine.render import compare_typed_answer, render_card
from memr_srs_engine.validate import validate_value
from tests.helpers import temp_collection


class _Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalogue = load_catalogue()
        cls.render_schema = catalogue["study.next"]["result"]
        cls.compare_schema = catalogue["study.compareTypedAnswer"]["result"]

    def setUp(self):
        self.collection = temp_collection()
        self.col = self.collection.__enter__()

    def tearDown(self):
        self.collection.__exit__(None, None, None)

    def card(self, model_name="Basic", fields=None, question=None, answer=None):
        model = self.col.models.by_name(model_name)
        if question is not None:
            model["tmpls"][0]["qfmt"] = question
        if answer is not None:
            model["tmpls"][0]["afmt"] = answer
        if question is not None or answer is not None:
            self.col.models.update_dict(model)
        note = self.col.new_note(model)
        defaults = ({"Text": "{{c1::answer}}", "Back Extra": "Extra"} if model_name == "Cloze"
                    else {"Front": "Question", "Back": "Answer"})
        for name, text in (defaults if fields is None else fields).items():
            note[name] = text
        self.col.add_note(note, 1)
        return note.cards()[0]

    def render(self, card, **kwargs):
        result = render_card(self.col, card, **kwargs)
        self.assertEqual(validate_value(self.render_schema, result), [])
        self.assertEqual(set(result), set(self.render_schema["properties"]))
        return result

    def compare(self, card, answer):
        result = compare_typed_answer(self.col, card, answer)
        self.assertEqual(validate_value(self.compare_schema, result), [])
        return result

    def configure(self, card, **settings):
        config = self.col.decks.config_dict_for_deck_id(card.current_deck_id())
        config.update(settings)
        self.col.decks.update_config(config)

    def test_native_html_css_identity_flags_mark_and_schema(self):
        card = self.card(fields={"Front": '<b>Question</b><img src="image name.png">',
                                 "Back": '<em>Answer</em><script>window.example = 1</script>'})
        self.col.set_user_flag_for_cards(7, [card.id])
        note = card.note()
        note.tags = ["MARKED"]
        self.col.update_note(note)
        card = self.col.get_card(card.id)
        native = card.render_output()
        before = self.col.undo_status()
        result = self.render(card)
        self.assertEqual(result["questionHtml"], native.question_text)
        self.assertEqual(result["answerHtml"], native.answer_text)
        self.assertEqual(result["css"], native.css)
        self.assertEqual((result["cardId"], result["noteId"], result["deckId"], result["ord"], result["queue"]),
                         (card.id, card.nid, card.did, card.ord, card.queue))
        self.assertEqual(result["bodyClass"], "card card1")
        self.assertEqual(result["flags"], 7)
        self.assertTrue(result["marked"])
        self.assertIsNone(result["typeAnswer"])
        self.assertIsNone(result["source"])
        self.assertEqual(self.col.undo_status(), before)

    def test_native_long_timer_cap_remains_schema_valid(self):
        card = self.card()
        for seconds in (1, 3600, 3601, 9999):
            with self.subTest(seconds=seconds):
                self.configure(card, maxTaken=seconds)
                self.assertEqual(card.time_limit(), seconds * 1000)
                self.assertEqual(self.render(card)["timer"]["limitSeconds"], seconds)

    def test_card_ord_body_class_and_cloze_rendering(self):
        card = self.card("Cloze", {"Text": "{{c1::first}} and {{c2::second::hint}}", "Back Extra": "More"})
        card = card.note().cards()[1]
        result = self.render(card)
        self.assertEqual(result["bodyClass"], "card card2")
        self.assertEqual(result["ord"], 1)
        self.assertIn("[hint]", result["questionHtml"])
        self.assertIn("second", result["answerHtml"])

    def test_typed_normal_uses_core_comparison_and_font(self):
        card = self.card("Basic (type in the answer)", {"Front": "Prompt", "Back": "<b>A &amp; B</b>"})
        result = self.render(card)
        self.assertEqual(result["typeAnswer"], {"field": "Back", "correct": "<b>A &amp; B</b>",
                                               "font": "Arial", "size": 20})
        inputs = _Inputs(result["questionHtml"]).inputs
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["id"], "typeans")
        self.assertIn("font-size: 20px", inputs[0]["style"])
        self.assertNotIn("[[type:", result["answerHtml"])
        comparison = self.compare(card, "A & B")
        self.assertEqual(comparison["comparisonHtml"], self.col.compare_answer("<b>A &amp; B</b>", "A & B", True))
        self.assertIn(comparison["comparisonHtml"], comparison["answerHtml"])
        self.assertNotIn("typeBad", comparison["comparisonHtml"])

    def test_typed_cloze_ordinal_repetitions_hints_and_html(self):
        card = self.card("Cloze", {"Text": "{{c1::other}} {{c2::<b>one</b>::hint}} {{c2::two}} {{c2::two::again}}",
                                    "Back Extra": "More"},
                         question="{{cloze:Text}} {{type:cloze:Text}}",
                         answer="{{cloze:Text}} <hr id=answer> {{type:cloze:Text}}")
        card = card.note().cards()[1]
        result = self.render(card)
        expected = self.col.extract_cloze_for_typing(card.note()["Text"], 2)
        self.assertEqual(expected, "<b>one</b>, two, two")
        self.assertEqual(result["typeAnswer"]["field"], "Text")
        self.assertEqual(result["typeAnswer"]["correct"], expected)
        self.assertIn("[hint]", result["questionHtml"])
        self.assertEqual(self.compare(card, "one, two, two")["comparisonHtml"],
                         self.col.compare_answer(expected, "one, two, two", True))

    def test_repeated_type_markers_follow_upstream_first_field(self):
        card = self.card(question="{{Front}} [[type:Back]] [[type:Front]]",
                         answer="{{FrontSide}}<hr id=answer> [[type:Back]]")
        result = self.render(card)
        self.assertEqual(len(_Inputs(result["questionHtml"]).inputs), 2)
        self.assertEqual(result["typeAnswer"]["field"], "Back")
        compared = self.compare(card, "Answer")
        self.assertEqual(compared["answerHtml"].count(compared["comparisonHtml"]), 3)
        self.assertEqual(compared["answerHtml"].count("<hr id=answer>"), 3)
        self.assertNotIn("[[type:", compared["answerHtml"])

    def test_missing_field_warning_is_escaped_and_answer_marker_removed(self):
        name = '<img src=x onerror=bad>\\1'
        card = self.card(question="{{Front}} [[type:" + name + "]]",
                         answer="{{FrontSide}} <hr id=answer> [[type:" + name + "]]")
        result = self.render(card)
        self.assertIsNone(result["typeAnswer"])
        self.assertIn(escape(self.col.tr.studying_type_answer_unknown_field(val=name)), result["questionHtml"])
        self.assertNotIn("<img", result["questionHtml"])
        self.assertFalse(_Inputs(result["questionHtml"]).inputs)
        compared = self.compare(card, "anything")
        self.assertEqual(compared["comparisonHtml"], "")
        self.assertNotIn("[[type:", compared["answerHtml"])
        self.assertIn("<hr id=answer>", compared["answerHtml"])

    def test_empty_typed_field_removes_markers_without_warning(self):
        card = self.card("Basic (type in the answer)", {"Front": "Prompt", "Back": ""})
        result = self.render(card)
        self.assertIsNone(result["typeAnswer"])
        self.assertFalse(_Inputs(result["questionHtml"]).inputs)
        self.assertEqual(result["questionHtml"], "Prompt\n\n")
        self.assertNotIn("[[type:", result["answerHtml"])
        self.assertEqual(self.compare(card, "anything")["comparisonHtml"], "")

    def test_missing_cloze_uses_empty_cards_warning(self):
        card = self.card("Cloze", {"Text": "{{c1::other}}", "Back Extra": "Extra"},
                         question="{{cloze:Text}} {{type:cloze:Text}}",
                         answer="{{cloze:Text}} {{type:cloze:Text}}")
        note = card.note()
        preview = note.ephemeral_card(ord=1)
        result = self.render(preview)
        self.assertIsNone(result["typeAnswer"])
        self.assertIn(escape(self.col.tr.studying_please_run_toolsempty_cards()), result["questionHtml"])
        self.assertEqual(result["nextStates"], [])

    def test_combining_option_and_unicode_normalization(self):
        for marker, combining in [("Back", True), ("nc:Back", False)]:
            with self.subTest(marker=marker):
                card = self.card(fields={"Front": "Question", "Back": "á"},
                                 question="{{Front}} {{type:" + marker + "}}",
                                 answer="{{FrontSide}}<hr id=answer>{{type:" + marker + "}}")
                self.assertEqual(self.render(card)["typeAnswer"]["field"], "Back")
                compared = self.compare(card, "a")
                self.assertEqual(compared["comparisonHtml"], self.col.compare_answer("á", "a", combining))
                self.assertEqual("typeBad" in compared["comparisonHtml"], combining)
                decomposed = self.compare(card, "a\u0301")
                self.assertNotIn("typeBad", decomposed["comparisonHtml"])

    def test_font_and_typed_html_cannot_escape_generated_attributes(self):
        font = 'A\' ; color:red; font-family:\'B" onfocus="bad &<\n\\'
        model = self.col.models.by_name("Basic (type in the answer)")
        model["flds"][1]["font"] = font
        model["flds"][1]["size"] = 32
        self.col.models.update_dict(model)
        card = self.card("Basic (type in the answer)")
        result = self.render(card)
        self.assertEqual(result["typeAnswer"]["font"], font)
        attributes = _Inputs(result["questionHtml"]).inputs[0]
        self.assertEqual(set(attributes), {"type", "id", "onkeypress", "style"})
        self.assertIn("\\27 ", attributes["style"])
        self.assertIn("\\5c ", attributes["style"])
        self.assertIn("font-size: 32px", attributes["style"])
        comparison = self.compare(card, '<img src=x onerror="bad"> & \\1')
        self.assertNotIn("<img", comparison["comparisonHtml"])
        self.assertNotIn("[[type:", comparison["answerHtml"])

    def test_typed_answer_without_answer_marker_preserves_separator(self):
        card = self.card(question="{{Front}} {{type:Back}}", answer="<hr id=answer>{{Back}}")
        result = self.render(card)
        self.assertEqual(result["answerHtml"], card.render_output().answer_text)
        compared = self.compare(card, "Answer")
        self.assertEqual(compared["answerHtml"], card.render_output().answer_text)
        self.assertEqual(compared["comparisonHtml"], self.col.compare_answer("Answer", "Answer", True))

    def test_untyped_comparison_is_empty_and_preserves_answer(self):
        card = self.card()
        self.assertEqual(self.compare(card, "unused"), {"answerHtml": card.render_output().answer_text,
                                                       "comparisonHtml": ""})

    def test_structured_sound_tts_and_native_av_references(self):
        card = self.card(fields={"Front": "Speak [sound:question.mp3]", "Back": "Answer [sound:../answer.mp3]"},
                         question="{{Front}} {{tts en_US voices=Alice,Bob speed=1.25:Front}}",
                         answer="{{FrontSide}}<hr id=answer>{{Back}} {{tts fa_IR speed=0.75:Back}}")
        result = self.render(card)
        sound = {"kind": "sound", "filename": "question.mp3", "text": None,
                 "language": None, "voices": [], "speed": 1.0}
        self.assertEqual(result["av"]["question"][0], sound)
        tts = result["av"]["question"][1]
        self.assertEqual(tts, {"kind": "tts", "filename": None, "text": "Speak [sound:question.mp3]", "language": "en_US",
                               "voices": ["Alice", "Bob"], "speed": 1.25})
        self.assertEqual(result["av"]["answer"][0]["filename"], "answer.mp3")
        self.assertEqual(result["av"]["answer"][1]["language"], "fa_IR")
        self.assertEqual(result["av"]["answer"][1]["speed"], 0.75)
        self.assertIn("[anki:play:q:0]", result["questionHtml"])
        self.assertIn("[anki:play:a:0]", result["answerHtml"])
        self.configure(card, autoplay=False, replayq=False)
        quiet = self.render(card)
        self.assertEqual(quiet["av"], result["av"])
        self.assertFalse(quiet["autoplay"])
        self.assertFalse(quiet["replayQuestionOnAnswer"])

    def test_deck_audio_timer_and_auto_advance_settings(self):
        card = self.card()
        self.configure(card, autoplay=False, replayq=False, timer=1, maxTaken=123,
                       secondsToShowQuestion=2.5, secondsToShowAnswer=4.25,
                       questionAction=1, answerAction=2, waitForAudio=False)
        result = self.render(card)
        self.assertEqual(result["timer"], {"show": True, "limitSeconds": 123})
        self.assertEqual(result["autoAdvance"], {"enabled": False, "questionSeconds": 2.5,
                                                 "answerSeconds": 4.25, "questionAction": "none",
                                                 "answerAction": "good", "waitForAudio": False})
        for ordinal, action in [(0, "bury"), (1, "again"), (2, "good"), (3, "hard"), (4, "none")]:
            with self.subTest(ordinal=ordinal):
                self.configure(card, answerAction=ordinal)
                self.assertEqual(self.render(card)["autoAdvance"]["answerAction"], action)
        self.configure(card, questionAction=0, maxTaken=0, timer=0,
                       secondsToShowQuestion=0, secondsToShowAnswer=0)
        result = self.render(card)
        # The pinned core normalizes maxTaken=0 to its 60-second default.
        self.assertEqual(result["timer"], {"show": False, "limitSeconds": 60})
        self.assertEqual(result["autoAdvance"]["questionAction"], "showAnswer")

    def test_filtered_card_uses_original_deck_config(self):
        card = self.card()
        self.configure(card, autoplay=False, timer=1, maxTaken=45)
        filtered = self.col.decks.new_filtered("Preview deck")
        card.odid, card.did = card.did, filtered
        self.col.update_card(card)
        result = self.render(card)
        self.assertEqual(result["deckId"], filtered)
        self.assertFalse(result["autoplay"])
        self.assertEqual(result["timer"], {"show": True, "limitSeconds": 45})

    def test_scheduler_snapshot_labels_seconds_and_counts(self):
        card = self.card()
        states = self.col._backend.get_scheduling_states(card.id)
        states.again.normal.learning.scheduled_secs = 142
        counts = {"new": 17, "learning": 5, "review": 11}
        result = self.render(card, states=states, counts=counts)
        labels = self.col.sched.describe_next_states(states)
        self.assertEqual([item["rating"] for item in result["nextStates"]], [1, 2, 3, 4])
        self.assertEqual([item["label"] for item in result["nextStates"]], list(labels))
        self.assertEqual([item["secondsUntilDue"] for item in result["nextStates"]],
                         [142, states.hard.normal.learning.scheduled_secs,
                          states.good.normal.learning.scheduled_secs, states.easy.normal.review.scheduled_days * 86400])
        self.assertEqual(result["counts"], counts)
        result["counts"]["new"] = 0
        self.assertEqual(counts["new"], 17)
        self.assertEqual(self.render(card)["counts"],
                         dict(zip(("new", "learning", "review"), self.col.sched.counts())))

    def test_native_intervals_for_review_relearning_and_filtered_states(self):
        card = self.card()
        variants = [
            (scheduler_pb2.SchedulingState(normal=scheduler_pb2.SchedulingState.Normal(
                review=scheduler_pb2.SchedulingState.Review(scheduled_days=12))), 12 * 86400),
            (scheduler_pb2.SchedulingState(normal=scheduler_pb2.SchedulingState.Normal(
                relearning=scheduler_pb2.SchedulingState.Relearning(
                    learning=scheduler_pb2.SchedulingState.Learning(scheduled_secs=180)))), 180),
            (scheduler_pb2.SchedulingState(filtered=scheduler_pb2.SchedulingState.Filtered(
                preview=scheduler_pb2.SchedulingState.Preview(scheduled_secs=42))), 42),
            (scheduler_pb2.SchedulingState(filtered=scheduler_pb2.SchedulingState.Filtered(
                rescheduling=scheduler_pb2.SchedulingState.ReschedulingFilter(
                    original_state=scheduler_pb2.SchedulingState.Normal(
                        review=scheduler_pb2.SchedulingState.Review(scheduled_days=5))))), 5 * 86400),
        ]
        for state, seconds in variants:
            with self.subTest(seconds=seconds):
                states = self.col._backend.get_scheduling_states(card.id)
                states.again.CopyFrom(state)
                result = self.render(card, states=states)
                self.assertEqual(result["nextStates"][0]["secondsUntilDue"], seconds)

    def test_fill_empty_and_ephemeral_preview_do_not_write_collection(self):
        model = self.col.models.by_name("Basic")
        note = self.col.new_note(model)
        preview = note.ephemeral_card()
        before = self.col.undo_status()
        card_count = self.col.card_count()
        result = self.render(preview, fill_empty=True)
        native = note.ephemeral_card(fill_empty=True).render_output()
        self.assertEqual(result["questionHtml"], native.question_text)
        self.assertEqual(result["answerHtml"], native.answer_text)
        self.assertEqual(result["css"], native.css)
        self.assertIn("(Front)", result["questionHtml"])
        self.assertEqual(result["nextStates"], [])
        self.assertEqual(result["counts"], {"new": 0, "learning": 0, "review": 0})
        self.assertEqual((result["cardId"], result["noteId"]), (0, 0))
        self.assertEqual(note.fields, ["", ""])
        self.assertEqual(self.col.card_count(), card_count)
        self.assertEqual(self.col.undo_status(), before)

    def test_fill_empty_preserves_persisted_cloze_card_ordinal(self):
        card = self.card("Cloze", {"Text": "{{c1::first}} and {{c2::second::hint}}", "Back Extra": ""})
        card = card.note().cards()[1]
        result = self.render(card, fill_empty=True)
        native = card.note().ephemeral_card(ord=1, fill_empty=True).render_output()
        self.assertEqual(result["questionHtml"], native.question_text)
        self.assertEqual(result["answerHtml"], native.answer_text)
        self.assertIn("[hint]", result["questionHtml"])
        self.assertEqual(result["bodyClass"], "card card2")
        self.assertEqual(result["cardId"], card.id)
        self.assertEqual(result["noteId"], card.nid)

    def source_card(self, source):
        model = self.col.models.by_name("Basic")
        if "Source" not in [field["name"] for field in model["flds"]]:
            self.col.models.add_field(model, self.col.models.new_field("Source"))
            self.col.models.update_dict(model)
        return self.card(fields={"Front": "Question", "Back": "Answer", "Source": source})

    def test_single_source_uri_parses_provenance_without_claiming_changed(self):
        revision = "a" * 64
        uri = "canonnotes:page/page%20one%2Ftwo?block=block%2Bthree&rev=" + revision
        card = self.source_card(uri)
        result = self.render(card)
        self.assertEqual(result["source"], {"pageId": "page one/two", "blockId": "block+three",
                                           "revision": revision, "uri": uri, "changed": None})
        self.assertNotIn(uri, result["questionHtml"])
        self.assertNotIn(uri, result["answerHtml"])
        card = self.source_card("canonnotes:page/page")
        self.assertEqual(self.render(card)["source"], {"pageId": "page", "blockId": None,
                                                     "revision": None, "uri": "canonnotes:page/page", "changed": None})

    def test_first_valid_source_line(self):
        for source in ("canonnotes:page/a\ncanonnotes:page/b",
                       "Plain source\ncanonnotes:page/%ff\ncanonnotes:page/a"):
            with self.subTest(source=source):
                self.assertEqual(self.render(self.source_card(source))["source"]["pageId"], "a")

    def test_plain_or_invalid_sources_have_no_invented_identity(self):
        sources = ["Plain source description", "",
                   "https://example.com/page/a", "canonnotes:page/", "canonnotes:page/%xx",
                   "canonnotes:page/%ff", "canonnotes:page/a?block=", "canonnotes:page/a?rev=" + "b" * 65,
                   "canonnotes:page/a?rev=not-a-hash", "canonnotes:page/a?block=b&block=c", "canonnotes:page/a#fragment",
                   "canonnotes:page/" + "p" * 257]
        for source in sources:
            with self.subTest(source=source):
                self.assertIsNone(self.render(self.source_card(source))["source"])


if __name__ == "__main__":
    unittest.main()
