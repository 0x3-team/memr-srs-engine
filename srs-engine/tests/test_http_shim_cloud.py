"""Cloud-mode shim: restore/snapshot/media endpoints and restore gating."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import io
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from anki.collection import Collection

from tests.helpers import ENGINE_ROOT
from tests.test_http_shim import _free_port, _http_request, _rpc

import http_shim

HTTP_SHIM_SCRIPT = ENGINE_ROOT / "http_shim.py"


def _zstd_compress(data: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor().compress(data)


def _zstd_decompress(data: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()


def _snapshot_body(collection_path: Path) -> bytes:
    return _zstd_compress(collection_path.read_bytes())


def _post_restore(
    port: int,
    body: bytes = b"",
    sha: str | None = "auto",
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    merged = {"Content-Type": "application/octet-stream"}
    if sha == "auto":
        merged["x-snapshot-sha256"] = hashlib.sha256(body).hexdigest()
    elif sha is not None:
        merged["x-snapshot-sha256"] = sha
    if headers:
        merged.update(headers)
    return _http_request(port, "POST", "/restore", body=body, headers=merged, timeout=timeout)


def _json(response: tuple[int, dict[str, str], bytes]) -> dict:
    status, headers, raw = response
    return json.loads(raw)


@contextmanager
def running_cloud_shim(directory: str | Path, *, rpc_timeout: float = 15.0):
    """A CloudShim + server; no engine exists until a restore succeeds."""
    shim = http_shim.CloudShim(directory, rpc_timeout=rpc_timeout, stderr=subprocess.DEVNULL)
    server = http_shim.ShimHTTPServer(("127.0.0.1", 0), shim)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield shim, port
    finally:
        shim.mark_stopping()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        shim.shutdown()


def _seed_collection(directory: Path, *, notes: int = 200, decks: int = 2, reviews: int = 10) -> dict:
    """Build a closed collection with notes spread over decks, some review
    history, a non-default deck option and a suspended card."""
    directory.mkdir()
    col = Collection(str(directory / "collection.anki2"))
    model = col.models.by_name("Basic")
    deck_ids = [1] + [int(col.decks.id(f"Seeded deck {i}")) for i in range(1, decks)]
    config = col.decks.config_dict_for_deck_id(deck_ids[-1])
    config["new"]["perDay"] = 5555
    config["newCards"] = config.get("newCards", config["new"])
    col.decks.update_config(config)
    card_ids = []
    for index in range(notes):
        note = col.new_note(model)
        note["Front"], note["Back"] = f"Question {index}", f"Answer {index}"
        col.add_note(note, deck_ids[index % decks])
        card_ids.extend(int(card.id) for card in note.cards())
    # Some review history so the round trip exercises the revlog.
    from anki.scheduler.v3 import CardAnswer

    for index, card_id in enumerate(card_ids[:reviews]):
        card = col.get_card(card_id)
        states = col._backend.get_scheduling_states(card_id)
        new_state = (states.again, states.hard, states.good, states.easy)[index % 4]
        rating = (CardAnswer.AGAIN, CardAnswer.HARD, CardAnswer.GOOD, CardAnswer.EASY)[index % 4]
        col.sched.answer_card(CardAnswer(
            card_id=card_id,
            current_state=states.current,
            new_state=new_state,
            rating=rating,
            answered_at_millis=1794400000000 + index * 997,
            milliseconds_taken=1500 + index * 37,
        ))
    if len(card_ids) > reviews + 1:
        col.sched.suspend_cards([card_ids[reviews + 1]])
    col.close()
    return {"cardIds": card_ids, "deckIds": deck_ids}


class CloudGatingTests(unittest.TestCase):
    def test_endpoints_refused_until_restore(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-gate-") as tmpdir:
            with running_cloud_shim(tmpdir) as (_shim, port):
                status, _h, raw = _http_request(port, "GET", "/healthz")
                self.assertEqual(status, 200)
                payload = json.loads(raw)
                self.assertEqual(payload["status"], "awaitingRestore")
                self.assertEqual(payload["collectionOpen"], False)

                status, payload = _rpc(port, {"id": 7, "method": "engine.status", "params": {}})
                self.assertEqual(status, 409)
                self.assertEqual(
                    payload,
                    {"id": 7, "error": {"code": "conflict", "message": "Restore a collection first."}},
                )
                for path in ("/snapshot", "/media-index", "/media/x.png"):
                    with self.subTest(path=path):
                        status, _h, raw = _http_request(port, "GET", path)
                        self.assertEqual(status, 409)
                        self.assertEqual(json.loads(raw)["error"]["code"], "conflict")

                # After a restore the routes open up.
                status, _h, raw = _post_restore(port, b"")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"ok": True, "notes": 0, "cards": 0})
                status, payload = _rpc(port, {"id": 8, "method": "engine.status", "params": {}})
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"]["collectionOpen"], True)

    def test_restore_empty_body_then_rpc_and_double_restore_conflict(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-empty-") as tmpdir:
            with running_cloud_shim(tmpdir) as (_shim, port):
                status, _h, raw = _post_restore(port, b"")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"ok": True, "notes": 0, "cards": 0})

                status, payload = _rpc(
                    port, {"id": 1, "method": "decks.create", "params": {"name": "Fresh"}}
                )
                self.assertEqual(status, 200)
                self.assertIsInstance(payload["result"]["deckId"], int)

                status, _h, raw = _post_restore(port, b"")
                self.assertEqual(status, 409)
                self.assertEqual(
                    json.loads(raw)["error"],
                    {"code": "conflict", "message": "A collection is already open."},
                )

    def test_restore_rejects_bad_hash_garbage_and_missing_header(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-bad-") as tmpdir:
            seed_dir = Path(tmpdir) / "seed"
            _seed_collection(seed_dir, notes=10, decks=1, reviews=0)
            body = _snapshot_body(seed_dir / "collection.anki2")

            shim_dir = Path(tmpdir) / "cloud"
            with running_cloud_shim(shim_dir) as (_shim, port):
                # Wrong hash.
                status, _h, raw = _post_restore(port, body, sha="0" * 64)
                self.assertEqual(status, 400)
                self.assertIn("sha256", json.loads(raw)["error"]["message"])
                self.assertFalse((shim_dir / "collection.anki2").exists())

                # Malformed hash header.
                status, _h, raw = _post_restore(port, body, sha="not-a-hash")
                self.assertEqual(status, 400)
                self.assertFalse((shim_dir / "collection.anki2").exists())

                # Missing hash header on a non-empty body.
                status, _h, raw = _post_restore(port, body, sha=None)
                self.assertEqual(status, 400)
                self.assertFalse((shim_dir / "collection.anki2").exists())

                # Non-zstd bytes with a matching hash.
                garbage = b"definitely not a zstd frame"
                status, _h, raw = _post_restore(port, garbage)
                self.assertEqual(status, 400)
                self.assertFalse((shim_dir / "collection.anki2").exists())

                # Valid zstd of non-SQLite content.
                fake = _zstd_compress(b"a payload that is not a database")
                status, _h, raw = _post_restore(port, fake)
                self.assertEqual(status, 400)
                self.assertFalse((shim_dir / "collection.anki2").exists())

                # A good restore still works after every refusal.
                status, _h, raw = _post_restore(port, body)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"ok": True, "notes": 10, "cards": 10})
                self.assertTrue((shim_dir / "collection.anki2").is_file())


def _card_view(card: dict) -> dict:
    return {key: card[key] for key in (
        "id", "noteId", "deckId", "ord", "queue", "type", "due", "originalDue",
        "intervalDays", "ease", "repetitions", "lapses", "flags",
    )}


def _review_view(review: dict) -> dict:
    return {key: review[key] for key in (
        "id", "reviewedAt", "rating", "intervalDays", "lastIntervalDays",
        "ease", "elapsedSeconds", "kind",
    )}


def _collection_view(port: int, card_ids: list[int], note_ids: list[int], deck_ids: list[int]) -> dict:
    """Read scheduling-relevant state through engine RPCs for comparison."""
    view = {"cards": {}, "notes": {}, "decks": {}, "deckOptions": {}}
    for card_id in card_ids:
        info = _rpc(port, {"id": 1, "method": "cards.info", "params": {"cardId": card_id}})[1]
        view["cards"][card_id] = {
            "card": _card_view(info["result"]["card"]),
            "reviews": [_review_view(r) for r in info["result"]["reviews"]],
        }
    for note_id in note_ids:
        note = _rpc(port, {"id": 1, "method": "notes.get", "params": {"noteId": note_id}})[1]
        payload = note["result"]
        view["notes"][note_id] = {k: payload[k] for k in ("fields", "tags", "cardIds")}
    tree = _rpc(port, {"id": 1, "method": "decks.tree", "params": {}})[1]["result"]["decks"]
    view["decks"] = {
        d["id"]: {k: d[k] for k in ("name", "parentId", "collapsed", "filtered")}
        for d in tree
    }
    for deck_id in deck_ids:
        options = _rpc(
            port, {"id": 1, "method": "deckOptions.get", "params": {"deckId": deck_id}}
        )[1]
        view["deckOptions"][deck_id] = options["result"]
    return view


class CloudSnapshotTests(unittest.TestCase):
    def test_snapshot_round_trip_preserves_collection(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-rt-") as tmpdir:
            seed_dir = Path(tmpdir) / "seed"
            ids = _seed_collection(seed_dir, notes=200, decks=2, reviews=10)
            body = _snapshot_body(seed_dir / "collection.anki2")

            with running_cloud_shim(Path(tmpdir) / "b") as (_b, port_b):
                status, _h, raw = _post_restore(port_b, body)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"ok": True, "notes": 200, "cards": 200})

                status, headers, snap_body = _http_request(port_b, "GET", "/snapshot", timeout=60.0)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("x-snapshot-sha256"), hashlib.sha256(snap_body).hexdigest())
                self.assertEqual(int(headers.get("x-snapshot-bytes")), len(snap_body))

                # The body is a zstd-wrapped SQLite collection.
                decompressed = _zstd_decompress(snap_body)
                self.assertEqual(decompressed[:16], b"SQLite format 3\x00")

                # The collection stays usable after a snapshot.
                status, payload = _rpc(
                    port_b, {"id": 2, "method": "decks.create", "params": {"name": "After Snapshot"}}
                )
                self.assertEqual(status, 200)

            # The staged snapshot file passes Anki's own integrity check.
            with tempfile.TemporaryDirectory(prefix="memr-cloud-check-") as checkdir:
                candidate = Path(checkdir) / "collection.anki2"
                candidate.write_bytes(decompressed)
                check = Collection(str(candidate))
                try:
                    self.assertEqual(check.db.scalar("pragma quick_check"), "ok")
                    summary, ok = check.fix_integrity()
                    self.assertTrue(ok, summary)
                    self.assertEqual(check.db.scalar("select count() from notes"), 200)
                    self.assertEqual(check.db.scalar("select count() from revlog"), 10)
                finally:
                    check.close()

            # Restore into a second cloud shim and compare field-by-field.
            with running_cloud_shim(Path(tmpdir) / "c") as (_c, port_c):
                status, _h, raw = _post_restore(port_c, snap_body)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)["ok"], True)

                with running_cloud_shim(Path(tmpdir) / "b2") as (_b2, port_b2):
                    status, _h, raw = _post_restore(port_b2, body)
                    self.assertEqual(status, 200)
                    note_ids = sorted(
                        _rpc(port_b2, {"id": 1, "method": "search.notes", "params": {"search": ""}})[1]["result"]["noteIds"]
                    )
                    view_seed = _collection_view(port_b2, ids["cardIds"], note_ids[:30], ids["deckIds"])
                    view_restored = _collection_view(port_c, ids["cardIds"], note_ids[:30], ids["deckIds"])
                    self.assertEqual(view_restored, view_seed)


class CloudReplayTests(unittest.TestCase):
    def test_replay_on_restored_snapshot_equals_normal_answering(self):
        """The cloud reconnect path: snapshot at T, restore later, replay the
        journal entries after T. No review is lost and none is applied twice.
        """
        with tempfile.TemporaryDirectory(prefix="memr-cloud-replay-") as tmpdir:
            seed_dir = Path(tmpdir) / "seed"
            ids = _seed_collection(seed_dir, notes=60, decks=1, reviews=0)
            # Raise the new-card limit so 50 answers fit in one session, and
            # disable the load balancer: its day-count map is per-session
            # in-memory state that keeps ghost entries for already-answered
            # new cards, so two independent sessions legitimately pick
            # different load-balanced days for the same card. Exact
            # equivalence is asserted under deterministic scheduling; the
            # separate B-vs-C comparison proves replay itself is stable.
            col = Collection(str(seed_dir / "collection.anki2"))
            config = col.decks.config_dict_for_deck_id(1)
            config["new"]["perDay"] = 9999
            col.decks.update_config(config)
            from anki.config import Config
            col.set_config_bool(Config.Bool.LOAD_BALANCER_ENABLED, False)
            col.close()
            body = _snapshot_body(seed_dir / "collection.anki2")

            # Shim A answers 50 cards through the normal study flow, with a
            # snapshot taken after the first 20; the journal records all 50.
            journal = []
            with running_cloud_shim(Path(tmpdir) / "a") as (_a, port_a):
                status, _h, raw = _post_restore(port_a, body)
                self.assertEqual(status, 200)
                ratings = [3, 2, 4, 1]
                snap_body = None
                for index in range(50):
                    shown = _rpc(port_a, {"id": index + 1, "method": "study.next", "params": {"deckId": 1}})[1]["result"]
                    self.assertIsNotNone(shown)
                    card_id = shown["cardId"]
                    _rpc(port_a, {"id": index + 1, "method": "study.answer", "params": {
                        "cardId": card_id, "rating": ratings[index % 4],
                        "elapsedSeconds": 1.0 + (index % 7) * 0.5}})[1]
                    review = _rpc(port_a, {"id": index + 1, "method": "cards.info", "params": {"cardId": card_id}})[1]["result"]["reviews"][0]
                    journal.append({
                        "id": f"j-{index}", "cardId": card_id, "rating": review["rating"],
                        "elapsedSeconds": review["elapsedSeconds"], "answeredAt": review["id"] / 1000,
                    })
                    if index == 19:
                        status, headers, snap_body = _http_request(port_a, "GET", "/snapshot", timeout=60.0)
                        self.assertEqual(status, 200)
                self.assertEqual(len(journal), 50)

            # Shims B and C restore the mid-session snapshot and replay only
            # the journaled entries that came after it — the entries before
            # the snapshot are already inside it and must not be replayed.
            tail = journal[20:]
            results = {}
            for label in ("b", "c"):
                with running_cloud_shim(Path(tmpdir) / label) as (_x, port_x):
                    status, _h, raw = _post_restore(port_x, snap_body)
                    self.assertEqual(status, 200)
                    replay = _rpc(port_x, {"id": 1, "method": "study.replayAnswers", "params": {"answers": tail}})[1]["result"]
                    results[label] = replay
                    self.assertEqual(replay["skipped"], [])
                    self.assertEqual(replay["applied"], [entry["id"] for entry in tail])
                    results[label + "-view"] = {
                        card_id: _rpc(port_x, {"id": 2, "method": "cards.info", "params": {"cardId": card_id}})[1]["result"]
                        for card_id in {entry["cardId"] for entry in journal}
                    }
            # Two fresh restores produce identical replay reports; undo
            # revisions embed a random per-process session id, so they are
            # compared structurally, not literally.
            self.assertEqual(
                {k: results["b"][k] for k in ("applied", "skipped", "changes")},
                {k: results["c"][k] for k in ("applied", "skipped", "changes")},
            )
            for label in ("b", "c"):
                undo = results[label]["undo"]
                self.assertEqual(
                    {k: undo[k] for k in ("canUndo", "undoLabel", "canRedo", "redoLabel")},
                    {k: results["b"]["undo"][k] for k in ("canUndo", "undoLabel", "canRedo", "redoLabel")},
                )

            # And every card state — including the review log — matches the
            # collection that answered all 50 normally: nothing lost or
            # doubled. Wall-clock review ids differ between the two engines by
            # construction, so the log is compared on scheduling fields.
            def scheduling(r):
                return {key: r[key] for key in (
                    "rating", "intervalDays", "lastIntervalDays", "ease",
                    "elapsedSeconds", "kind",
                )}

            with running_cloud_shim(Path(tmpdir) / "a2") as (_a2, port_a2):
                status, _h, raw = _post_restore(port_a2, body)
                self.assertEqual(status, 200)
                for index in range(50):
                    shown = _rpc(port_a2, {"id": index + 1, "method": "study.next", "params": {"deckId": 1}})[1]["result"]
                    card_id = shown["cardId"]
                    entry = next(e for e in journal if e["cardId"] == card_id)
                    _rpc(port_a2, {"id": index + 1, "method": "study.answer", "params": {
                        "cardId": card_id, "rating": entry["rating"],
                        "elapsedSeconds": entry["elapsedSeconds"]}})[1]
                for card_id, view in results["b-view"].items():
                    normal = _rpc(port_a2, {"id": 9, "method": "cards.info", "params": {"cardId": card_id}})[1]["result"]
                    expected = _card_view(view["card"])
                    actual = _card_view(normal["card"])
                    # A learning-queue due is answered_at + step in seconds:
                    # replayed answers keep their journaled timestamp, while
                    # the normal re-answer ran at a different wall-clock time.
                    if expected["queue"] == 1:
                        del expected["due"], actual["due"]
                    self.assertEqual(expected, actual, card_id)
                    self.assertEqual(
                        [scheduling(r) for r in view["reviews"]],
                        [scheduling(r) for r in normal["reviews"]],
                        card_id,
                    )
                    # Replayed entries keep the journaled answer timestamp.
                    journaled = {e["cardId"]: e["answeredAt"] for e in tail}
                    if card_id in journaled:
                        replayed_first = [r for r in view["reviews"] if r["id"] == int(journaled[card_id] * 1000)]
                        self.assertTrue(replayed_first, card_id)


class CloudMediaTests(unittest.TestCase):
    def _restore_and_seed_media(self, port, directory: Path, files: dict[str, bytes]):
        status, _h, raw = _post_restore(port, b"")
        self.assertEqual(status, 200)
        media_dir = directory / "collection.media"
        media_dir.mkdir(exist_ok=True)
        for name, data in files.items():
            (media_dir / name).write_bytes(data)

    def test_media_index_and_fetch(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-media-") as tmpdir:
            directory = Path(tmpdir)
            files = {
                "a.png": b"\x89PNG" + bytes(range(200)),
                "clip.mp3": os.urandom(1024),
                "héllo.txt": "café".encode("utf-8"),
            }
            with running_cloud_shim(directory) as (_shim, port):
                self._restore_and_seed_media(port, directory, files)
                (directory / "collection.media" / "subdir").mkdir()

                status, _h, raw = _http_request(port, "GET", "/media-index")
                self.assertEqual(status, 200)
                index = {entry["name"]: entry for entry in json.loads(raw)["files"]}
                self.assertEqual(set(index), set(files))
                for name, data in files.items():
                    self.assertEqual(index[name]["bytes"], len(data))
                    self.assertEqual(index[name]["sha256"], hashlib.sha256(data).hexdigest())

                for name, data in files.items():
                    status, headers, raw = _http_request(
                        port, "GET", "/media/" + quote(name), timeout=10.0
                    )
                    self.assertEqual(status, 200, name)
                    self.assertEqual(headers.get("content-type"), "application/octet-stream")
                    self.assertEqual(raw, data)

                status, headers, raw = _http_request(port, "HEAD", "/media/a.png")
                self.assertEqual(status, 200)
                self.assertEqual(raw, b"")
                self.assertEqual(int(headers["content-length"]), len(files["a.png"]))

                status, _h, _r = _http_request(port, "GET", "/media/missing.bin")
                self.assertEqual(status, 404)

    def test_media_hostile_names_are_refused(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-evil-") as tmpdir:
            directory = Path(tmpdir)
            with running_cloud_shim(directory) as (_shim, port):
                status, _h, raw = _post_restore(port, b"")
                self.assertEqual(status, 200)
                hostile = [
                    "/media/../collection.anki2",
                    "/media/..",
                    "/media/a..b",
                    "/media/a/b",
                    "/media/%2e%2e",
                    "/media/%2Fetc%2Fpasswd",
                    "/media/%00",
                    "/media/" + "x" * 256,
                    "/media/",
                ]
                for path in hostile:
                    with self.subTest(path=path):
                        status, _h, _r = _http_request(port, "GET", path)
                        self.assertIn(status, (400, 404), path)
                        self.assertNotEqual(status, 200)
                # A name of exactly 255 bytes is not hostile by length alone.
                name = "n" * 255
                status, _h, _r = _http_request(port, "GET", "/media/" + name)
                self.assertEqual(status, 404)

    def test_restore_uses_chunked_upload(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-chunked-") as tmpdir:
            seed_dir = Path(tmpdir) / "seed"
            _seed_collection(seed_dir, notes=5, decks=1, reviews=0)
            body = _snapshot_body(seed_dir / "collection.anki2")
            sha = hashlib.sha256(body).hexdigest()

            with running_cloud_shim(Path(tmpdir) / "cloud") as (_shim, port):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15.0)
                chunked = b"".join(
                    f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
                    for chunk in (body[:64], body[64:512], body[512:])
                ) + b"0\r\n\r\n"
                conn.request(
                    "POST", "/restore", body=chunked,
                    headers={"Transfer-Encoding": "chunked", "x-snapshot-sha256": sha},
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read()), {"ok": True, "notes": 5, "cards": 5})
                conn.close()


class CloudSubprocessTests(unittest.TestCase):
    def _start_cloud_shim(self, tmpdir: str) -> tuple[subprocess.Popen, int, Path, Path]:
        srs_dir = Path(tmpdir) / "srs"
        cwd_dir = Path(tmpdir) / "cwd"
        scratch_tmp = Path(tmpdir) / "tmp"
        cwd_dir.mkdir()
        scratch_tmp.mkdir()
        port = _free_port()
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
        env.update(
            MEMR_SRS_DIR=str(srs_dir),
            MEMR_SRS_HOST="127.0.0.1",
            MEMR_SRS_PORT=str(port),
            MEMR_SRS_REQUIRE_RESTORE="1",
            TMPDIR=str(scratch_tmp),
            TEMP=str(scratch_tmp),
        )
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", str(HTTP_SHIM_SCRIPT)],
            cwd=str(cwd_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc, port, srs_dir, cwd_dir

    def _wait_healthz(self, proc, port, expected_status=None):
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                st, _, raw = _http_request(port, "GET", "/healthz", timeout=1.0)
                if st == 200 and json.loads(raw).get("ok"):
                    if expected_status is None or json.loads(raw).get("status") == expected_status:
                        return json.loads(raw)
            except OSError:
                pass
            self.assertIsNone(proc.poll(), "cloud shim exited before becoming healthy.")
            time.sleep(0.05)
        self.fail("cloud shim did not become healthy in time.")

    def test_sigterm_during_snapshot_exits_cleanly_without_corruption(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-term-") as tmpdir:
            seed_dir = Path(tmpdir) / "seed"
            _seed_collection(seed_dir, notes=400, decks=2, reviews=5)
            body = _snapshot_body(seed_dir / "collection.anki2")

            proc, port, srs_dir, cwd_dir = self._start_cloud_shim(tmpdir)
            try:
                self.assertEqual(self._wait_healthz(proc, port)["status"], "awaitingRestore")
                status, _h, raw = _post_restore(port, body)
                self.assertEqual(status, 200)

                replies: list = []

                def fetch():
                    try:
                        replies.append(_http_request(port, "GET", "/snapshot", timeout=10.0))
                    except (OSError, http.client.HTTPException) as exc:
                        replies.append(exc)

                client = threading.Thread(target=fetch, daemon=True)
                client.start()
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=10.0), 0)
                client.join(timeout=5.0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5.0)
                proc.stdout.close()
                proc.stderr.close()

            self.assertEqual(list(cwd_dir.iterdir()), [])
            col = Collection(str(srs_dir / "collection.anki2"))
            try:
                self.assertEqual(col.db.scalar("pragma quick_check"), "ok")
            finally:
                col.close()

    def test_existing_collection_is_not_adopted_in_cloud_mode(self):
        with tempfile.TemporaryDirectory(prefix="memr-cloud-exist-") as tmpdir:
            srs_dir = Path(tmpdir) / "srs"
            _seed_collection(srs_dir, notes=1, decks=1, reviews=0)
            cwd_dir = Path(tmpdir) / "cwd"
            cwd_dir.mkdir()
            env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
            env.update(
                MEMR_SRS_DIR=str(srs_dir),
                MEMR_SRS_REQUIRE_RESTORE="1",
            )
            proc = subprocess.run(
                [sys.executable, "-I", "-B", str(HTTP_SHIM_SCRIPT)],
                cwd=str(cwd_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("existing_collection", proc.stderr)


if __name__ == "__main__":
    unittest.main()
