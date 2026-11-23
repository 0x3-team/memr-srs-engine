"""study.replayAnswers applies journaled answers without disturbing the queue."""
import io
import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from anki.collection import Collection

from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from tests.test_decks import RpcCase


def _journal_entry(entry_id, card_id, rating, elapsed_seconds, answered_at):
    return {
        "id": entry_id,
        "cardId": card_id,
        "rating": rating,
        "elapsedSeconds": elapsed_seconds,
        "answeredAt": answered_at,
    }


class _EngineBox:
    """A second engine over its own collection directory for restore copies."""

    def __init__(self, collection_path):
        self.col = Collection(str(collection_path))
        self.output = io.BytesIO()
        logger = logging.getLogger("replay-tests")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger)
        self.sequence = 0

    def call(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        self.engine.dispatch(json.dumps(
            {"id": self.sequence, "method": method, "params": {} if params is None else params}
        ).encode())
        frame = json.loads(self.output.getvalue()[offset:])
        assert "result" in frame, frame
        return frame["result"]

    def close(self):
        self.engine.progress.close()
        self.col.close()


def _seed_collection(directory, count):
    """Create a closed collection with count new cards and a raised new limit."""
    directory.mkdir()
    col = Collection(str(directory / "collection.anki2"))
    model = col.models.by_name("Basic")
    config = col.decks.config_dict_for_deck_id(1)
    config["new"]["perDay"] = 9999
    col.decks.update_config(config)
    card_ids = []
    for index in range(count):
        note = col.new_note(model)
        note["Front"], note["Back"] = f"Question {index}", f"Answer {index}"
        col.add_note(note, 1)
        card_ids.extend(int(card.id) for card in note.cards())
    col.close()
    return card_ids


def _card_view(info):
    card = info["card"]
    return {key: card[key] for key in (
        "id", "noteId", "deckId", "ord", "queue", "type", "due", "originalDue",
        "intervalDays", "ease", "repetitions", "lapses", "flags", "position",
    )}


def _review_view(review):
    return {key: review[key] for key in (
        "id", "reviewedAt", "rating", "intervalDays", "lastIntervalDays",
        "ease", "elapsedSeconds", "kind",
    )}


class ReplayAnswersTests(RpcCase):
    def populate(self, count, deck_id=1):
        config = self.col.decks.config_dict_for_deck_id(deck_id)
        config["new"]["perDay"] = 9999
        self.col.decks.update_config(config)
        return [self.note(deck_id).card_ids()[0] for _ in range(count)]

    def test_replay_applies_in_order_and_reports_ids(self):
        card_ids = self.populate(3)
        answers = [
            _journal_entry("j-2", card_ids[1], 3, 1.5, 1795300001.0),
            _journal_entry("j-1", card_ids[0], 4, 2.5, 1795300000.0),
            _journal_entry("j-3", card_ids[2], 2, 0.5, 1795300002.0),
        ]
        result = self.rpc("study.replayAnswers", {"answers": answers})
        self.assertEqual(result["applied"], ["j-2", "j-1", "j-3"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(self.col.db.scalar("select count() from revlog"), 3)
        ratings = self.col.db.list("select ease from revlog where cid=? order by id", card_ids[1])
        self.assertEqual(ratings, [3])
        # The stored review timestamp comes from the journaled answeredAt.
        self.assertEqual(
            self.col.db.scalar("select id from revlog where cid=?", card_ids[1]),
            1795300001000,
        )
        self.assertTrue(result["undo"]["canUndo"])

    def test_replay_empty_list_and_result_shape(self):
        result = self.rpc("study.replayAnswers", {"answers": []})
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["skipped"], [])
        self.assertFalse(any(result["changes"].values()))
        self.assertIn("revision", result["undo"])

    def test_replay_skips_missing_suspended_and_buried_then_continues(self):
        keep, suspended, buried, extra = self.populate(4)
        self.rpc("cards.suspend", {"cardIds": [suspended], "scope": "card"})
        self.rpc("cards.bury", {"cardIds": [buried], "scope": "card"})
        answers = [
            _journal_entry("susp", suspended, 3, 1.0, 1795300000.0),
            _journal_entry("ok-1", keep, 3, 1.0, 1795300001.0),
            _journal_entry("miss", 99999999, 3, 1.0, 1795300002.0),
            _journal_entry("bury", buried, 3, 1.0, 1795300003.0),
            _journal_entry("ok-2", extra, 4, 1.0, 1795300004.0),
        ]
        result = self.rpc("study.replayAnswers", {"answers": answers})
        self.assertEqual(result["applied"], ["ok-1", "ok-2"])
        self.assertEqual(result["skipped"], [
            {"id": "susp", "reason": "suspended"},
            {"id": "miss", "reason": "missing"},
            {"id": "bury", "reason": "buried"},
        ])
        self.assertEqual(self.col.db.scalar("select count() from revlog"), 2)
        self.assertEqual(
            self.col.db.list("select distinct cid from revlog"), [keep, extra]
        )

    def test_replay_reports_each_application_honestly(self):
        # The engine does not store journal ids: deduplication belongs to the
        # host journal. A second application of the same list on the same
        # collection therefore applies again and is reported as applied, which
        # is exactly why the design replays each journal only onto fresh
        # restores.
        card_ids = self.populate(3)
        answers = [
            _journal_entry(f"d-{i}", card_id, 3, 1.0, 1795300000.0 + i)
            for i, card_id in enumerate(card_ids)
        ]
        first = self.rpc("study.replayAnswers", {"answers": answers})
        self.assertEqual(first["applied"], ["d-0", "d-1", "d-2"])
        self.assertEqual(first["skipped"], [])
        second = self.rpc("study.replayAnswers", {"answers": answers})
        self.assertEqual(second["applied"], ["d-0", "d-1", "d-2"])
        self.assertEqual(second["skipped"], [])

    def test_replay_does_not_disturb_the_study_queue(self):
        card_ids = self.populate(10)
        shown = self.rpc("study.next", {"deckId": 1})
        top = shown["cardId"]
        others = [card_id for card_id in card_ids if card_id != top][:5]
        answers = [
            _journal_entry(f"q-{i}", card_id, 3, 2.0, 1795300000.0 + i)
            for i, card_id in enumerate(others)
        ]
        result = self.rpc("study.replayAnswers", {"answers": answers})
        self.assertEqual(len(result["applied"]), 5)
        # The same card is still being shown and carries no review.
        self.assertEqual(self.rpc("study.next", {"deckId": 1})["cardId"], top)
        self.assertEqual(
            self.col.db.scalar("select count() from revlog where cid=?", top), 0
        )
        # And the shown session can still be answered normally afterwards.
        answer = self.rpc(
            "study.answer",
            {"cardId": top, "rating": 3, "elapsedSeconds": 1.0},
        )
        self.assertIn("counts", answer)
        self.assertEqual(
            self.col.db.scalar("select count() from revlog where cid=?", top), 1
        )

    def test_replay_state_after_built_queues(self):
        # Answering cards that are not at the head of a built queue must still
        # commit; the backend's queue-position check is retried from state.
        card_ids = self.populate(10)
        self.rpc("study.next", {"deckId": 1})
        # Answering the last card first is the position furthest from the head.
        answers = [
            _journal_entry("tail", card_ids[-1], 3, 1.0, 1795300000.0),
            _journal_entry("head", card_ids[0], 3, 1.0, 1795300001.0),
        ]
        result = self.rpc("study.replayAnswers", {"answers": answers})
        self.assertEqual(result["applied"], ["tail", "head"])
        self.assertEqual(self.col.db.scalar("select count() from revlog"), 2)

    def test_replay_of_normal_journal_matches_normal_answering(self):
        # Seed one collection, copy the closed file, answer normally on one
        # engine and replay the recorded journal on a second engine.
        with tempfile.TemporaryDirectory(prefix="memr-replay-") as tmpdir:
            base = Path(tmpdir) / "base"
            _seed_collection(base, 60)
            normal_dir = Path(tmpdir) / "normal"
            replay_dir = Path(tmpdir) / "replay"
            again_dir = Path(tmpdir) / "again"
            for target in (normal_dir, replay_dir, again_dir):
                target.mkdir()
                shutil.copyfile(
                    base / "collection.anki2", target / "collection.anki2"
                )
            normal = _EngineBox(normal_dir / "collection.anki2")
            replay = _EngineBox(replay_dir / "collection.anki2")
            again = _EngineBox(again_dir / "collection.anki2")
            try:
                journal = []
                ratings = [3, 2, 4, 1]
                for index in range(50):
                    shown = normal.call("study.next", {"deckId": 1})
                    self.assertIsNotNone(shown)
                    card_id = shown["cardId"]
                    normal.call(
                        "study.answer",
                        {"cardId": card_id, "rating": ratings[index % 4],
                         "elapsedSeconds": 1.0 + (index % 7) * 0.5},
                    )
                    review = normal.call("cards.info", {"cardId": card_id})["reviews"][0]
                    journal.append(_journal_entry(
                        f"j-{index}", card_id, review["rating"],
                        review["elapsedSeconds"], review["id"] / 1000,
                    ))
                self.assertEqual(len(journal), 50)

                result = replay.call("study.replayAnswers", {"answers": journal})
                self.assertEqual(result["applied"], [entry["id"] for entry in journal])
                self.assertEqual(result["skipped"], [])
                second = again.call("study.replayAnswers", {"answers": journal})
                # Replaying the same journal on a second fresh copy produces
                # the identical report, so the host replay is deterministic.
                self.assertEqual(second["applied"], result["applied"])
                self.assertEqual(second["skipped"], result["skipped"])

                answered_ids = {entry["cardId"] for entry in journal}
                for card_id in answered_ids:
                    info_normal = normal.call("cards.info", {"cardId": card_id})
                    info_replay = replay.call("cards.info", {"cardId": card_id})
                    info_again = again.call("cards.info", {"cardId": card_id})
                    self.assertEqual(
                        _card_view(info_replay), _card_view(info_normal), card_id
                    )
                    self.assertEqual(
                        _card_view(info_again), _card_view(info_replay), card_id
                    )
                    self.assertEqual(
                        len(info_replay["reviews"]), len(info_normal["reviews"])
                    )
                    for left, right in zip(
                        info_normal["reviews"], info_replay["reviews"]
                    ):
                        self.assertEqual(_review_view(left), _review_view(right))

                # The study queue remains usable after the replay.
                self.assertIsNotNone(replay.call("study.next", {"deckId": 1}))
                self.assertIsNotNone(again.call("study.next", {"deckId": 1}))
            finally:
                normal.close()
                replay.close()
                again.close()


if __name__ == "__main__":
    unittest.main()
