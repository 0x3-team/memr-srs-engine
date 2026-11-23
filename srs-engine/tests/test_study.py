"""Real review RPCs and paired SM-2/FSRS scheduling on generated collections."""
from datetime import datetime, timedelta
import io
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from anki.collection import Collection
from anki.cards import Card
from memr_srs_engine.handlers import deck_options
from memr_srs_engine.handlers import study
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from tests.test_decks import RpcCase


class StudyTests(RpcCase):
    def populate(self, count=1, deck_id=1):
        return [self.note(deck_id).card_ids()[0] for _ in range(count)]

    def test_all_methods_registered_and_invalid_params(self):
        methods = {name for name in self.engine.registry.catalogue if name.startswith("study.")}
        self.assertEqual(methods & self.engine.registry.handlers.keys(), methods)
        for method in methods:
            with self.subTest(method=method):
                self.error(method, {"unexpected": True}, "invalid_params")

    def test_unknown_ids_and_empty_undo(self):
        for method in ("overview", "next", "congratulations", "customStudyDefaults", "unbury"):
            self.error("study." + method, {"deckId": 88888}, "not_found")
        self.error("study.customStudy", {"deckId": 88888, "kind": "increaseNew", "amount": 1}, "not_found")
        self.error("study.answer", {"cardId": 88888, "rating": 3, "elapsedSeconds": 1}, "not_found")
        self.error("study.compareTypedAnswer", {"cardId": 88888, "typedAnswer": ""}, "not_found")
        self.rpc("study.undoStatus")
        self.error("study.undo", {}, "not_found")
        self.error("study.redo", {}, "not_found")

    def test_overview_renders_html_and_native_markdown_descriptions(self):
        deck_id = self.create("Description")
        self.populate(deck_id=deck_id)
        html = '<p>Practice <strong>carefully</strong>.</p><script>void 0</script>'
        self.rpc("decks.setDescription", {"deckId": deck_id, "description": html, "markdown": False})
        shown = self.rpc("study.overview", {"deckId": deck_id})
        self.assertEqual(shown["description"], html)
        self.assertGreater(shown["counts"]["new"], 0)
        markdown = '**Practice** <script>alert(1)</script> [unsafe](javascript:alert(1))'
        self.rpc("decks.setDescription", {"deckId": deck_id, "description": markdown, "markdown": True})
        rendered = self.rpc("study.overview", {"deckId": deck_id})["description"]
        self.assertEqual(rendered, self.col.render_markdown(markdown))
        self.assertIn('<strong>Practice</strong>', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.rpc("decks.setDescription", {"deckId": deck_id, "description": "", "markdown": False})
        self.assertEqual(self.rpc("study.overview", {"deckId": deck_id})["description"], "")

    def test_targeted_undo_rejects_intervening_same_label_and_preserves_legacy_calls(self):
        first, second = self.create("First deletion"), self.create("Second deletion")
        old = self.rpc("decks.remove", {"deckIds": [first], "deleteCards": True})["undo"]
        current = self.rpc("decks.remove", {"deckIds": [second], "deleteCards": True})["undo"]
        self.assertEqual(old["undoLabel"], current["undoLabel"])
        self.assertNotEqual(old["revision"], current["revision"])
        self.error("study.undo", {"expectedRevision": old["revision"]}, "conflict")
        self.assertIsNone(self.col.decks.get(first, default=False))
        self.assertIsNone(self.col.decks.get(second, default=False))
        self.assertEqual(self.rpc("study.undoStatus"), current)
        undone = self.rpc("study.undo", {"expectedRevision": current["revision"]})
        self.assertIsNotNone(self.col.decks.get(second, default=False))
        self.error("study.redo", {"expectedRevision": current["revision"]}, "conflict")
        self.rpc("study.redo", {"expectedRevision": undone["undo"]["revision"]})
        self.assertIsNone(self.col.decks.get(second, default=False))
        self.rpc("study.undo")
        self.assertIsNotNone(self.col.decks.get(second, default=False))

    def test_targeted_undo_revision_changes_when_native_steps_are_merged(self):
        deck_id = self.create("Merged changes")
        before = self.rpc("study.undoStatus")
        step = self.col.undo_status().last_step
        self.rpc("decks.setDescription", {"deckId": deck_id, "description": "Changed"})
        self.col.merge_undo_entries(step)
        self.assertEqual(self.col.undo_status().last_step, step)
        self.error("study.undo", {"expectedRevision": before["revision"]}, "conflict")
        self.assertEqual(self.rpc("decks.get", {"deckId": deck_id})["description"], "Changed")

    def test_targeted_undo_revision_is_local_to_the_engine_session(self):
        deck_id = self.create("Session boundary")
        before = self.rpc("study.undoStatus")
        self.engine.progress.close()
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), self.engine.logger)
        self.assertNotEqual(self.rpc("study.undoStatus")["revision"], before["revision"])
        self.error("study.undo", {"expectedRevision": before["revision"]}, "conflict")
        self.assertIsNotNone(self.col.decks.get(deck_id, default=False))

    def test_integral_json_numbers_are_accepted_by_native_calls(self):
        deck_id = self.create("Integral JSON numbers")
        card_id = self.populate(deck_id=deck_id)[0]
        deck_params = {"deckId": float(deck_id)}
        for name in ("overview", "congratulations", "customStudyDefaults"):
            self.rpc("study." + name, deck_params)
        shown = self.rpc("study.next", deck_params)
        self.assertEqual(shown["cardId"], card_id)
        self.rpc("study.compareTypedAnswer", {"cardId": float(card_id), "typedAnswer": ""})
        self.rpc("study.answer", {"cardId": float(card_id), "rating": 4.0, "elapsedSeconds": 1.234})
        self.rpc("study.undo")
        self.rpc("study.customStudy", {**deck_params, "kind": "increaseNew", "amount": 2.0})
        self.rpc("study.undo")
        filtered = self.rpc("study.customStudy", {**deck_params, "kind": "byTags", "amount": 1.0})
        self.assertTrue(self.col.decks.get(filtered["deckId"], default=False)["dyn"])
        self.rpc("study.undo")
        self.rpc("cards.bury", {"cardIds": [card_id]})
        self.assertEqual(self.rpc("study.unbury", deck_params)["count"], 1)
        self.error("study.next", {"deckId": 88888.0}, "not_found")
        self.error("study.next", {"deckId": 1.5}, "invalid_params")
        self.error("study.answer", {"cardId": card_id, "rating": 3.5, "elapsedSeconds": 1}, "invalid_params")

    def test_next_idempotent_answer_elapsed_cap_and_undo_redo(self):
        card_id = self.populate()[0]
        before = self.col.get_card(card_id)._to_backend_card()
        shown = self.rpc("study.next", {"deckId": 1})
        self.assertEqual(self.rpc("study.next", {"deckId": 1}), shown)
        self.assertEqual(shown["cardId"], card_id)
        self.assertEqual(len(shown["nextStates"]), 4)
        with patch.object(self.col.sched, "answer_card", wraps=self.col.sched.answer_card) as commit:
            result = self.rpc("study.answer", {"cardId": card_id, "rating": 4, "elapsedSeconds": 123.456})
        self.assertEqual(commit.call_args.args[0].milliseconds_taken, 60000)
        self.assertTrue(result["changes"]["card"])
        self.assertIsNone(result["leech"])
        self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), 60000)
        self.assertIsNone(self.rpc("study.next", {"deckId": 1}))
        status = self.rpc("study.undoStatus")
        self.assertTrue(status["canUndo"])
        undone = self.rpc("study.undo")
        self.assertEqual(undone["card"]["cardId"], card_id)
        self.assertEqual(self.col.get_card(card_id)._to_backend_card(), before)
        self.assertEqual(self.col.db.scalar("select count() from revlog"), 0)
        self.assertTrue(undone["undo"]["canRedo"])
        redone = self.rpc("study.redo")
        self.assertIsNone(redone["card"])
        self.assertEqual(self.col.db.scalar("select count() from revlog"), 1)

    def test_client_elapsed_milliseconds_and_original_deck_cap(self):
        card_id = self.populate()[0]
        config = self.col.decks.config_dict_for_deck_id(1)
        config["maxTaken"] = 2
        self.col.decks.update_config(config)
        self.rpc("study.next", {"deckId": 1})
        self.rpc("study.answer", {"cardId": card_id, "rating": 3, "elapsedSeconds": 1.234})
        self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), 1234)
        self.rpc("study.undo")
        self.rpc("study.answer", {"cardId": card_id, "rating": 3, "elapsedSeconds": 10})
        self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), 2000)

    def test_native_long_elapsed_caps_boundaries_and_undo(self):
        card_id = self.populate()[0]
        cases = ((9999, 0, 0), (9999, 1.234, 1234), (9999, 9999, 9999000),
                 (9999, 20000, 9999000), (9999, sys.float_info.max, 9999000),
                 (2, sys.float_info.max, 2000))
        for limit, elapsed, expected in cases:
            with self.subTest(limit=limit, elapsed=elapsed):
                config = self.col.decks.config_dict_for_deck_id(1)
                config["maxTaken"] = limit
                self.col.decks.update_config(config)
                before = self.col.get_card(card_id)._to_backend_card()
                shown = self.rpc("study.next", {"deckId": 1})
                self.assertEqual(shown["timer"]["limitSeconds"], limit)
                self.rpc("study.answer", {"cardId": card_id, "rating": 4, "elapsedSeconds": elapsed})
                self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), expected)
                self.rpc("study.undo")
                self.assertEqual(self.col.get_card(card_id)._to_backend_card(), before)
                self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 0)
                self.rpc("study.redo")
                self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), expected)
                self.rpc("study.undo")

    def test_elapsed_invalid_values_do_not_mutate_answer_or_undo(self):
        card_id = self.populate()[0]
        self.rpc("study.next", {"deckId": 1})
        before = self.col.get_card(card_id)._to_backend_card()
        undo = self.rpc("study.undoStatus")
        for elapsed in (-1, True, False, float("inf"), float("-inf"), float("nan")):
            with self.subTest(elapsed=elapsed):
                self.error("study.answer", {"cardId": card_id, "rating": 4, "elapsedSeconds": elapsed}, "invalid_params")
                self.assertEqual(self.col.get_card(card_id)._to_backend_card(), before)
                self.assertEqual(self.rpc("study.undoStatus"), undo)
                self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 0)

    def test_answer_requires_current_snapshot_and_detects_stale_state(self):
        card_id, other = self.populate(2)
        request = {"cardId": card_id, "rating": 3, "elapsedSeconds": 1}
        self.error("study.answer", request, "conflict")
        shown = self.rpc("study.next", {"deckId": 1})
        request["cardId"] = shown["cardId"]
        self.error("study.answer", {**request, "rating": 0}, "invalid_params")
        self.error("study.answer", {**request, "elapsedSeconds": -1}, "invalid_params")
        self.error("study.answer", {**request, "stateToken": "stale"}, "conflict")
        self.error("study.answer", {**request, "cardId": other}, "conflict")
        card = self.col.get_card(request["cardId"])
        card.due += 10
        self.col.update_card(card)
        self.error("study.answer", request, "conflict")
        shown = self.rpc("study.next", {"deckId": 1})
        request["cardId"] = shown["cardId"]
        request["stateToken"] = study._token(shown)
        self.rpc("study.answer", request)
        self.error("study.answer", request, "conflict")

    def test_card_info_preserves_pending_typed_answer_and_undo(self):
        deck_id = self.create("Typed card info")
        note = self.col.new_note(self.col.models.by_name("Basic (type in the answer)"))
        note["Front"], note["Back"] = "Generated question", "Generated answer"
        self.col.add_note(note, deck_id)
        card_id = note.card_ids()[0]
        shown = self.rpc("study.next", {"deckId": deck_id})
        self.assertIsNone(self.col.get_card(card_id).last_review_time)
        for _ in range(2):
            self.assertEqual(self.rpc("cards.info", {"cardId": card_id})["reviews"], [])
        # Anki fills this cached timestamp as a side effect of reading stats.
        self.assertEqual(self.col.get_card(card_id).last_review_time, 0)
        compared = self.rpc("study.compareTypedAnswer", {"cardId": card_id, "typedAnswer": note["Back"]})
        self.assertIn("typeGood", compared["comparisonHtml"])
        self.rpc("study.answer", {"cardId": card_id, "rating": 4, "elapsedSeconds": 1.25,
                                  "stateToken": study._token(shown)})
        self.assertEqual(self.rpc("cards.info", {"cardId": card_id})["card"]["repetitions"], 1)
        self.assertIsNone(self.rpc("study.next", {"deckId": deck_id}))
        undone = self.rpc("study.undo")
        self.assertEqual(undone["card"]["cardId"], card_id)
        self.assertEqual(self.rpc("cards.info", {"cardId": card_id})["reviews"], [])

    def test_card_info_does_not_refresh_a_stale_pending_answer(self):
        for field in ("due", "last_review_time"):
            for edit_before_info in (False, True):
                with self.subTest(field=field, edit_before_info=edit_before_info):
                    deck_id = self.create(f"Stale info {field} {edit_before_info}")
                    card_id = self.populate(deck_id=deck_id)[0]
                    self.rpc("study.next", {"deckId": deck_id})
                    if not edit_before_info:
                        self.rpc("cards.info", {"cardId": card_id})
                    card = self.col.get_card(card_id)
                    setattr(card, field, (getattr(card, field) or 0) + 10)
                    self.col.update_card(card)
                    before = card._to_backend_card()
                    undo = self.rpc("study.undoStatus")
                    if edit_before_info:
                        self.rpc("cards.info", {"cardId": card_id})
                        before = self.col.get_card(card_id)._to_backend_card()
                    self.error("study.answer", {"cardId": card_id, "rating": 4, "elapsedSeconds": 1}, "conflict")
                    self.assertEqual(self.col.get_card(card_id)._to_backend_card(), before)
                    self.assertEqual(self.rpc("study.undoStatus"), undo)
                    self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 0)

    def test_other_card_info_preserves_pending_answer(self):
        first, second = self.populate(2)
        shown = self.rpc("study.next", {"deckId": 1})
        other = second if shown["cardId"] == first else first
        self.rpc("cards.info", {"cardId": other})
        self.rpc("study.answer", {"cardId": shown["cardId"], "rating": 4, "elapsedSeconds": 1})
        self.assertEqual(self.col.get_card(shown["cardId"]).reps, 1)
        self.assertEqual(self.col.get_card(other).reps, 0)

    def test_overview_congratulations_counts_and_limits(self):
        deck_id = self.create("Limited")
        card_ids = self.populate(3, deck_id)
        config = self.col.decks.config_dict_for_deck_id(deck_id)
        config["new"]["perDay"] = 0
        self.col.decks.update_config(config)
        self.col.sched.bury_cards([card_ids[0]])
        self.col.sched.suspend_cards([card_ids[1]])
        overview = self.rpc("study.overview", {"deckId": deck_id})
        self.assertEqual(overview["counts"]["new"], 0)
        self.assertEqual((overview["buried"], overview["suspended"]), (1, 1))
        self.assertTrue(overview["newLimitReached"])
        congrats = self.rpc("study.congratulations", {"deckId": deck_id})
        self.assertTrue(congrats["newLimitReached"])
        self.assertTrue(congrats["canCustomStudy"])
        self.assertIsNone(congrats["nextDueAt"])
        self.assertEqual(congrats["today"]["reviews"], 0)

    def test_answer_rejects_changed_options_with_unchanged_current_card_state(self):
        self.populate()
        shown = self.rpc("study.next", {"deckId": 1})
        before = self.col.sched.get_queued_cards().cards[0].states.current
        config = self.col.decks.config_dict_for_deck_id(1)
        config["new"]["ints"][1] = 25
        self.col.decks.update_config(config)
        after = self.col.sched.get_queued_cards().cards[0].states.current
        self.assertEqual(before, after)
        self.error("study.answer", {"cardId": shown["cardId"], "rating": 4, "elapsedSeconds": 1}, "conflict")

    def test_answer_accepts_learning_clock_drift_with_shown_native_states(self):
        for fsrs in (False, True):
            for relearning in (False, True):
                with self.subTest(fsrs=fsrs, relearning=relearning):
                    self.col.set_config("fsrs", fsrs)
                    deck_id = self.create(f"Clock {fsrs} {relearning}")
                    card_id = self.populate(deck_id=deck_id)[0]
                    if relearning:
                        card = self.col.get_card(card_id)
                        card.type, card.queue, card.due, card.ivl, card.factor = 2, 2, self.col.sched.today, 10, 2500
                        self.col.update_card(card)
                    self.rpc("study.next", {"deckId": deck_id})
                    self.rpc("study.answer", {"cardId": card_id, "rating": 1, "elapsedSeconds": 1})
                    shown = self.rpc("study.next", {"deckId": deck_id})
                    states = self.engine._study[2]
                    # Rust reads SystemTime, independently of Python's clock.
                    time.sleep(1.1)
                    current = self.col.sched.get_queued_cards().cards[0].states
                    self.assertNotEqual(current.current, states.current)
                    with patch.object(self.col.sched, "answer_card", wraps=self.col.sched.answer_card) as commit:
                        self.rpc("study.answer", {"cardId": card_id, "rating": 3, "elapsedSeconds": 2,
                                                  "stateToken": study._token(shown)})
                    self.assertEqual(commit.call_args.args[0].current_state, states.current)
                    self.assertEqual(commit.call_args.args[0].new_state, states.good)
                    self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 2)

    def test_learning_clock_drift_does_not_hide_a_changed_due_date(self):
        card_id = self.populate()[0]
        self.rpc("study.next", {"deckId": 1})
        self.rpc("study.answer", {"cardId": card_id, "rating": 1, "elapsedSeconds": 1})
        self.rpc("study.next", {"deckId": 1})
        time.sleep(1.1)
        card = self.col.get_card(card_id)
        card.due -= 30
        self.col.update_card(card)
        before = card._to_backend_card()
        undo = self.rpc("study.undoStatus")
        self.error("study.answer", {"cardId": card_id, "rating": 3, "elapsedSeconds": 2}, "conflict")
        self.assertEqual(self.col.get_card(card_id)._to_backend_card(), before)
        self.assertEqual(self.rpc("study.undoStatus"), undo)
        self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 1)

    def test_answer_rejects_moved_card_without_losing_new_deck_timer_or_undo(self):
        short = self.col.decks.config_dict_for_deck_id(1)
        short["maxTaken"] = 2
        self.col.decks.update_config(short)
        long = self.col.decks.add_config("Long timer", clone_from=short)
        long["maxTaken"] = 9999
        self.col.decks.update_config(long)
        destination = self.create("Default::Moved")
        self.col.decks.set_config_id_for_deck_dict(self.col.decks.get(destination), long["id"])
        card_id = self.populate()[0]
        shown = self.rpc("study.next", {"deckId": 1})
        self.assertEqual(shown["timer"]["limitSeconds"], 2)
        states = self.col.sched.get_queued_cards().cards[0].states
        self.rpc("cards.setDeck", {"cardIds": [card_id], "deckId": destination})
        queued = self.col.sched.get_queued_cards().cards[0]
        self.assertEqual(queued.card.id, card_id)
        self.assertEqual(queued.states, states)
        moved = self.col.get_card(card_id)._to_backend_card()
        undo = self.rpc("study.undoStatus")
        request = {"cardId": card_id, "rating": 4, "elapsedSeconds": 20000}
        self.error("study.answer", request, "conflict")
        self.assertEqual(self.col.get_card(card_id)._to_backend_card(), moved)
        self.assertEqual(self.rpc("study.undoStatus"), undo)
        self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 0)
        refreshed = self.rpc("study.next", {"deckId": 1})
        self.assertEqual(refreshed["deckId"], destination)
        self.assertEqual(refreshed["timer"]["limitSeconds"], 9999)
        self.rpc("study.answer", request)
        self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), 9999000)
        self.rpc("study.undo")
        self.assertEqual(self.col.get_card(card_id)._to_backend_card(), moved)
        self.rpc("study.redo")
        self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), 9999000)

    def test_answer_uses_current_timer_limit_when_scheduling_states_are_unchanged(self):
        card_id = self.populate()[0]
        config = self.col.decks.config_dict_for_deck_id(1)
        config["maxTaken"] = 2
        self.col.decks.update_config(config)
        self.rpc("study.next", {"deckId": 1})
        states = self.col.sched.get_queued_cards().cards[0].states
        config["maxTaken"] = 9999
        self.col.decks.update_config(config)
        self.assertEqual(self.col.sched.get_queued_cards().cards[0].states, states)
        self.rpc("study.answer", {"cardId": card_id, "rating": 4, "elapsedSeconds": sys.float_info.max})
        self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), 9999000)
        self.rpc("study.undo")
        self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 0)
        self.rpc("study.redo")
        self.assertEqual(self.col.db.scalar("select time from revlog where cid=?", card_id), 9999000)

    def test_answer_rejects_customizer_enabled_after_next_without_mutation(self):
        card_id = self.populate()[0]
        self.rpc("study.next", {"deckId": 1})
        states = self.col.sched.get_queued_cards().cards[0].states
        self.col.set_config("cardStateCustomizer", "states.good = states.easy;", undoable=True)
        self.assertEqual(self.col.sched.get_queued_cards().cards[0].states, states)
        before = self.col.get_card(card_id)._to_backend_card()
        undo = self.rpc("study.undoStatus")
        request = {"cardId": card_id, "rating": 3, "elapsedSeconds": 1}
        self.error("study.answer", request, "unsupported")
        self.assertEqual(self.col.get_card(card_id)._to_backend_card(), before)
        self.assertEqual(self.rpc("study.undoStatus"), undo)
        self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 0)
        self.error("study.answer", {**request, "cardId": 88888}, "not_found")
        self.col.set_config("cardStateCustomizer", "", undoable=True)
        self.rpc("study.next", {"deckId": 1})
        self.rpc("study.answer", request)
        self.assertEqual(self.col.db.scalar("select count() from revlog where cid=?", card_id), 1)

    def test_leech_tag_and_suspend_actions(self):
        for action in (0, 1):
            with self.subTest(action=action):
                card_id = self.populate()[0]
                config = self.col.decks.config_dict_for_deck_id(1)
                config["lapse"]["leechFails"] = 1
                config["lapse"]["leechAction"] = action
                self.col.decks.update_config(config)
                card = self.col.get_card(card_id)
                card.type, card.queue, card.due, card.ivl, card.factor = 2, 2, self.col.sched.today, 5, 2500
                self.col.update_card(card)
                shown = self.rpc("study.next", {"deckId": 1})
                self.assertEqual(shown["cardId"], card_id)
                result = self.rpc("study.answer", {"cardId": card_id, "rating": 1, "elapsedSeconds": 1})
                self.assertEqual(result["leech"], {"tagged": True, "suspended": action == 0})
                self.rpc("study.undo")
                self.assertFalse(self.col.get_note(card.nid).has_tag("leech"))
                self.rpc("study.redo")
                self.col.sched.suspend_cards([card_id])

    def test_typed_answer_and_non_typed_passthrough(self):
        card_id = self.populate()[0]
        plain = self.rpc("study.compareTypedAnswer", {"cardId": card_id, "typedAnswer": "A"})
        self.assertEqual(plain["comparisonHtml"], "")
        self.assertIn("Generated answer", plain["answerHtml"])
        model = self.col.models.by_name("Basic (type in the answer)")
        note = self.col.new_note(model)
        note["Front"], note["Back"] = "Type this", "café"
        self.col.add_note(note, 1)
        compared = self.rpc("study.compareTypedAnswer", {"cardId": note.card_ids()[0], "typedAnswer": "cafe"})
        self.assertIn("typeans", compared["comparisonHtml"])
        self.assertIn(compared["comparisonHtml"], compared["answerHtml"])
        self.error("study.compareTypedAnswer", {"cardId": note.card_ids()[0], "typedAnswer": "\ud800"}, "invalid_params")

    def test_unbury_modes_subdecks_and_undo(self):
        child = self.create("Default::Child")
        manual, sibling, suspended = self.populate(3, child)
        self.col.sched.bury_cards([manual])
        self.col.sched.bury_cards([sibling], manual=False)
        self.col.sched.suspend_cards([suspended])
        result = self.rpc("study.unbury", {"deckId": 1, "kind": "manual"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.col.get_card(sibling).queue, -2)
        self.assertEqual(self.col.get_card(suspended).queue, -1)
        self.rpc("study.undo")
        self.assertEqual(self.col.get_card(manual).queue, -3)
        self.rpc("study.redo")
        self.assertEqual(self.rpc("study.unbury", {"deckId": 1, "kind": "siblings"})["count"], 1)
        self.assertEqual(self.rpc("study.unbury", {"deckId": 1})["count"], 0)

    def test_custom_study_defaults_increase_limits_and_undo(self):
        child = self.create("Default::Child")
        self.populate(2)
        self.populate(3, child)
        defaults = self.rpc("study.customStudyDefaults", {"deckId": 1})
        self.assertEqual((defaults["availableNew"], defaults["availableNewInChildren"]), (2, 3))
        for kind in ("increaseNew", "increaseReview"):
            with self.subTest(kind=kind):
                result = self.rpc("study.customStudy", {"deckId": 1, "kind": kind, "amount": 4})
                self.assertEqual(result["deckId"], 1)
                changed = self.rpc("study.customStudyDefaults", {"deckId": 1})
                key = "extendNew" if kind == "increaseNew" else "extendReview"
                self.assertEqual(changed[key], 4)
                self.rpc("study.undo")
                self.assertEqual(self.rpc("study.customStudyDefaults", {"deckId": 1})[key], 0)
                self.rpc("study.redo")
                self.assertEqual(self.rpc("study.customStudyDefaults", {"deckId": 1})[key], 4)

    def test_custom_study_filters_and_defaults_tags(self):
        for kind in ("forgotten", "ahead", "previewNew", "byTags"):
            with self.subTest(kind=kind):
                # Each filter needs matching cards, not a fabricated empty deck.
                card_id = self.populate()[0]
                note = self.col.get_card(card_id).note()
                note.tags = ["chosen", "other"]
                self.col.update_note(note)
                if kind in ("forgotten", "ahead"):
                    self.rpc("study.next", {"deckId": 1})
                    shown = self.rpc("study.next", {"deckId": 1})
                    self.rpc("study.answer", {"cardId": shown["cardId"], "rating": 1 if kind == "forgotten" else 4,
                                              "elapsedSeconds": 1})
                params = {"deckId": 1, "kind": kind, "amount": 10}
                if kind == "byTags":
                    params.update(tags=["chosen"], excludeTags=["absent"], cardState="all")
                result = self.rpc("study.customStudy", params)
                self.assertTrue(self.col.decks.get(result["deckId"], default=False)["dyn"])
                self.rpc("study.next", {"deckId": result["deckId"]})
                self.error("study.customStudyDefaults", {"deckId": result["deckId"]}, "invalid_params")
                self.rpc("study.undo")
                self.assertIsNone(self.col.decks.get(result["deckId"], default=False))
                self.rpc("study.redo")
                self.col.sched.empty_filtered_deck(result["deckId"])
                self.col.decks.remove([result["deckId"]])
                self.col.decks.set_current(1)
        defaults = self.rpc("study.customStudyDefaults", {"deckId": 1})
        self.assertIn({"name": "chosen", "include": True, "exclude": False}, defaults["tags"])
        self.error("study.customStudy", {"deckId": 1, "kind": "increaseNew", "amount": 1, "tags": []}, "invalid_params")
        self.error("study.customStudy", {"deckId": 1, "kind": "byTags", "amount": 1, "tags": ["\ud800"]}, "invalid_params")

    def test_customizer_fails_closed(self):
        self.populate()
        other_deck = self.create("Unsupported scheduling destination")
        self.col.set_config("cardStateCustomizer", "states.good = states.easy;", undoable=True)
        before = self.rpc("study.undoStatus")
        self.error("study.next", {"deckId": other_deck}, "unsupported")
        self.assertEqual(self.col.decks.get_current_id(), 1)
        self.assertEqual(self.rpc("study.undoStatus"), before)
        self.error("study.next", {"deckId": 88888}, "not_found")
        self.rpc("study.undo")
        result = self.rpc("study.redo")
        self.assertIsNone(result["card"])
        self.assertTrue(result["changes"]["config"])


class SchedulingParityTests(unittest.TestCase):
    def test_engine_matches_direct_pylib_sm2_and_fsrs(self):
        for fsrs in (False, True):
            with self.subTest(preset="FSRS" if fsrs else "SM-2"), tempfile.TemporaryDirectory(prefix="memr-e02-parity-") as directory:
                root = Path(directory)
                seed = Collection(str(root / "seed.anki2"))
                seed.set_config("fsrs", fsrs)
                seed.set_config("loadBalancerEnabled", False)
                config = seed.decks.config_dict_for_deck_id(1)
                config["name"] = "E02 FSRS default" if fsrs else "E02 SM-2 default"
                config["new"]["perDay"], config["rev"]["perDay"] = 9999, 9999
                seed.decks.update_config(config)
                for index in range(12):
                    note = seed.new_note(seed.models.by_name("Basic"))
                    note["Front"], note["Back"] = f"Generated parity {index}", f"Answer {index}"
                    seed.add_note(note, 1)
                    if index % 3 == 0:
                        card = seed.get_card(note.card_ids()[0])
                        card.type, card.queue, card.due, card.ivl, card.factor = 2, 2, seed.sched.today, 10, 2500
                        card.reps, card.lapses = 5, 1
                        seed.update_card(card)
                seed.close()
                shutil.copyfile(root / "seed.anki2", root / "engine.anki2")
                shutil.copyfile(root / "seed.anki2", root / "direct.anki2")
                actual, direct = Collection(str(root / "engine.anki2")), Collection(str(root / "direct.anki2"))
                logger = logging.getLogger("e02-parity")
                logger.addHandler(logging.NullHandler())
                engine = Engine(actual, Registry(), FrameWriter(io.BytesIO()), logger)
                answers = 0
                frozen_millis = int(time.time()) * 1000
                try:
                    with patch("anki.scheduler.v3.int_time", return_value=frozen_millis):
                        for rating in [1, 2, 3, 4] * 12:
                            # Anki's Rust CardStateUpdater reads SystemTime for
                            # learning due values, separately from answered_at.
                            # Compare both native calls in the same observed
                            # second; keep exact due/state assertions below.
                            fraction = time.time() % 1
                            if fraction > 0.25:
                                time.sleep(1.01 - fraction)
                            native_second = int(time.time())
                            payload = study.next_card(engine, {"deckId": 1})
                            queue = direct.sched.get_queued_cards()
                            self.assertEqual(payload is None, not bool(queue.cards))
                            if payload is None:
                                break
                            top = queue.cards[0]
                            self.assertEqual(payload["cardId"], top.card.id)
                            native = Card(direct, backend_card=top.card)
                            native.start_timer()
                            top.states.current.custom_data = top.card.custom_data
                            built = direct.sched.build_answer(card=native, states=top.states, rating=rating - 1)
                            built.milliseconds_taken = min(1234, native.time_limit())
                            direct.sched.answer_card(built)
                            study.answer(engine, {"cardId": payload["cardId"], "rating": rating, "elapsedSeconds": 1.234})
                            self.assertEqual(int(time.time()), native_second,
                                             "Native parity inputs crossed a clock second; this fixture did not establish equal-time comparison.")
                            # Only modification/review timestamps and review-row ids
                            # are excluded; due, counters, memory states and timing remain.
                            self.assertEqual(actual.db.all("select id,nid,did,ord,type,queue,due,ivl,factor,reps,lapses,left,odue,odid,flags,data from cards order by id"),
                                             direct.db.all("select id,nid,did,ord,type,queue,due,ivl,factor,reps,lapses,left,odue,odid,flags,data from cards order by id"))
                            self.assertEqual(actual.db.all("select cid,usn,ease,ivl,lastIvl,factor,time,type from revlog order by id"),
                                             direct.db.all("select cid,usn,ease,ivl,lastIvl,factor,time,type from revlog order by id"))
                            answers += 1
                    self.assertGreaterEqual(answers, 12)
                    self.assertEqual(actual.db.scalar("select count() from cards where json_extract(data,'$.s') is not null") > 0, fsrs)
                    print(f"E02 parity {'FSRS' if fsrs else 'SM-2'}: {answers} paired answers; cards and revlog match.")
                finally:
                    engine.progress.close()
                    actual.close()
                    direct.close()


class EasyDaysAnswerTests(unittest.TestCase):
    """study.answer honours a preset's Easy Days the way pinned Anki does.

    Anki 26.09.3 applies Easy Days on answer only inside the review load
    balancer (rslib scheduler/states/fuzz.rs with_review_fuzz ->
    load_balancer.rs find_interval). A Minimum (0) day keeps weight 0.0001,
    but a day with no reviews yet gets full weight, so a few cards may still
    land there. Without the load balancer, plain fuzz ignores Easy Days.
    """
    CARDS = 60

    def seed(self, path, fsrs, load_balancer):
        col = Collection(str(path))
        try:
            col.set_config("fsrs", fsrs)
            col.load_balancer_enabled = load_balancer
            config = col.decks.config_dict_for_deck_id(1)
            config["new"]["perDay"], config["rev"]["perDay"] = 9999, 9999
            col.decks.update_config(config)
            for index in range(self.CARDS):
                note = col.new_note(col.models.by_name("Basic"))
                note["Front"], note["Back"] = f"Easy days {index}", "Answer"
                col.add_note(note, 1)
                # Fixed card ids fix Anki's per-card fuzz/load-balancer seeds.
                col.db.execute("update cards set id = ? where nid = ?", 1_600_000_000_000 + index, note.id)
                card = col.get_card(1_600_000_000_000 + index)
                card.type, card.queue, card.due, card.ivl, card.factor, card.reps = 2, 2, col.sched.today, 20, 2500, 5
                col.update_card(card)
        finally:
            col.close()

    def answer_all(self, path, easy_days, direct=False):
        col = Collection(str(path))
        logger = logging.getLogger("easy-days-answer")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        engine = Engine(col, Registry(), FrameWriter(io.BytesIO()), logger)
        answered = 0
        try:
            if direct:
                config = col.decks.config_dict_for_deck_id(1)
                config["easyDaysPercentages"] = easy_days
                col.decks.update_config(config)
                while (queue := col.sched.get_queued_cards()).cards:
                    top = queue.cards[0]
                    card = Card(col, backend_card=top.card)
                    card.start_timer()
                    top.states.current.custom_data = top.card.custom_data
                    col.sched.answer_card(col.sched.build_answer(card=card, states=top.states, rating=2))
                    answered += 1
            else:
                value = deck_options.get(engine, {"deckId": 1})
                value["config"]["easyDays"] = easy_days
                deck_options.update(engine, {**{key: value[key] for key in
                    ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}, "applyToChildren": False})
                while (payload := study.next_card(engine, {"deckId": 1})) is not None:
                    study.answer(engine, {"cardId": payload["cardId"], "rating": 3, "elapsedSeconds": 1})
                    answered += 1
            self.assertEqual(answered, self.CARDS, "Every seeded review card must be answered.")
            self.assertEqual(list(col._backend.get_deck_config(1).config.easy_days_percentages), easy_days)
            today = datetime.fromtimestamp(col.sched.day_cutoff - 86400).date()
            # Only cards this run answered; a seeded card keeps queue 2 unanswered.
            rows = col.db.all("select id, due, ivl from cards where queue = 2 and id in "
                              "(select cid from revlog where type = 1) order by id")
            self.assertEqual(len(rows), self.CARDS, "Every answered card must come back in the result rows.")
            weekdays = [0] * 7
            for _cid, due, _ivl in rows:
                weekdays[(today + timedelta(days=due - col.sched.today)).weekday()] += 1
            return col.sched.today, rows, weekdays
        finally:
            engine.progress.close()
            col.close()

    def test_answer_helper_refuses_a_run_that_leaves_cards_unanswered(self):
        with tempfile.TemporaryDirectory(prefix="memr-easy-days-") as directory:
            path = Path(directory) / "seed.anki2"
            self.seed(path, False, True)
            real_next, shown = study.next_card, []

            def stop_after_five(engine, params):
                shown.append(None)
                return real_next(engine, params) if len(shown) <= 5 else None
            with patch.object(study, "next_card", stop_after_five), self.assertRaises(AssertionError):
                self.answer_all(path, [1.0] * 7)

    def test_answer_moves_reviews_off_a_minimum_weekday_like_native_anki(self):
        for fsrs in (False, True):
            for load_balancer in (True, False):
                with (self.subTest(fsrs=fsrs, load_balancer=load_balancer),
                      tempfile.TemporaryDirectory(prefix="memr-easy-days-") as directory):
                    root = Path(directory)
                    self.seed(root / "seed.anki2", fsrs, load_balancer)
                    for name in ("uniform", "easy", "direct"):
                        shutil.copyfile(root / "seed.anki2", root / f"{name}.anki2")
                    day, uniform_rows, baseline = self.answer_all(root / "uniform.anki2", [1.0] * 7)
                    self.assertEqual(len(uniform_rows), self.CARDS)
                    target = max(range(7), key=baseline.__getitem__)
                    easy_days = [1.0] * 7
                    easy_days[target] = 0.0
                    easy_day, easy_rows, shifted = self.answer_all(root / "easy.anki2", easy_days)
                    direct_day, direct_rows, _ = self.answer_all(root / "direct.anki2", easy_days, direct=True)
                    self.assertEqual({day, easy_day, direct_day}, {day}, "The fixture crossed a scheduling day.")
                    # Memr's answer path schedules exactly as Anki's own reviewer.
                    self.assertEqual(easy_rows, direct_rows)
                    print(f"Easy Days answer {'FSRS' if fsrs else 'SM-2'} load balancer {'on' if load_balancer else 'off'}: "
                          f"weekday {target} at 0 -> {baseline} became {shifted}")
                    if load_balancer:
                        self.assertGreaterEqual(baseline[target], 10, baseline)
                        self.assertLessEqual(shifted[target], baseline[target] // 4,
                                             f"weekday {target}: {baseline} -> {shifted}")
                    else:
                        self.assertEqual(easy_rows, uniform_rows)


if __name__ == "__main__":
    unittest.main()
