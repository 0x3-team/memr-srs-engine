"""Generated package/CSV round trips and hostile file boundaries."""
import csv
from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch
import uuid
import zipfile

from anki.collection import Collection

from memr_srs_engine import handlers
from memr_srs_engine.handlers import transfer
from memr_srs_engine.errors import EngineError
from memr_srs_engine.paths import resolve_upload, prune_exports, safe_directory, mutation
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine import protocol
from memr_srs_engine.registry import Registry
from memr_srs_engine.validate import validate_value
from tests.helpers import EnginePipe, build_collection, temp_collection


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memr-e05-transfer-")
        self.root = Path(self.temp.name)
        self.col = Collection(str(self.root / "collection.anki2"))
        self.output = io.BytesIO()
        logger = logging.getLogger("e05-transfer-tests")
        logger.addHandler(logging.NullHandler())
        self.engine = Engine(self.col, Registry(), FrameWriter(self.output), logger)
        self.sequence = 0
        self.data = build_collection(self.col)
        note = self.col.get_note(self.data["noteIds"][0])
        note["Front"] += '<img src="generated.txt">'
        self.col.update_note(note)
        Path(self.col.media.dir(), "generated.txt").write_text("generated media")
        card = self.col.get_card(self.data["cardIds"][0])
        card.type, card.queue, card.ivl, card.reps = 2, 2, 15, 4
        self.col.update_card(card)

    def tearDown(self):
        self.engine.progress.close()
        self.col.close()
        self.temp.cleanup()

    def rpc(self, method, params=None):
        self.sequence += 1
        start = self.output.tell()
        self.engine.dispatch(json.dumps(dict(id=self.sequence, method=method, params=params or {})).encode())
        frames = [json.loads(line) for line in self.output.getvalue()[start:].splitlines()]
        for frame in frames:
            if frame.get("event"):
                self.assertEqual(frame["event"], "progress")
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], frame["progress"]), [])
        responses = [frame for frame in frames if not frame.get("event")]
        self.assertEqual(len(responses), 1)
        response = responses[0]
        self.assertEqual(response["id"], self.sequence)
        if "error" in response:
            raise EngineError(**response["error"])
        result = response["result"]
        self.assertEqual(validate_value(self.engine.registry.catalogue[method]["result"], result), [])
        return result

    def long(self, method, params=None):
        ack = self.rpc(method, params)
        self.engine.progress.thread.join(15)
        self.assertFalse(self.engine.progress.thread.is_alive())
        state = self.rpc("progress.get", {"operationId": ack["operationId"]})
        if state["error"] is not None:
            raise EngineError(**state["error"])
        self.assertEqual(state["status"], "completed", state)
        return state["result"]

    def inspect(self, method, params):
        payload = "packageInspection" if method == "transfer.inspectPackage" else "csvInspection"
        return self.long(method, params)[payload]

    def upload(self, source=None, extension=".csv", text=None):
        path = safe_directory(self.engine, "incoming") / (str(uuid.uuid4()) + extension)
        if source:
            shutil.copyfile(source, path)
        else:
            path.write_text(text or "front,back\n", encoding="utf-8")
        return path

    def file_path(self, result):
        file = result["file"]
        path = self.root / "exports" / file["downloadToken"] / file["filename"]
        self.assertEqual(path.stat().st_size, file["bytes"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertGreaterEqual(file["expiresAt"], int(time.time()) + 590)
        self.assertEqual(list(path.parent.iterdir()), [path])
        return path

    def import_options(self, upload):
        return dict(uploadId=upload.name, mergeNotetypes=True, updateNotes="always",
                    updateNotetypes="always", includeScheduling=True, includeDeckConfigs=True)

    def csv_options(self, upload):
        return dict(uploadId=upload.name, delimiter=",", isHtml=True,
                    notetypeId=self.col.models.by_name("Basic")["id"], deckId=self.data["deckIds"][0],
                    mapping=[dict(column=0, target="field", fieldOrdinal=0),
                             dict(column=1, target="field", fieldOrdinal=1)],
                    duplicateMode="update", matchScope="notetype", tags=["generated"])

    def test_inspections_acknowledge_before_native_work(self):
        exported = self.long("transfer.exportPackage", dict(scope={}, includeScheduling=True,
            includeMedia=True, includeDeckConfigs=True))
        package = self.upload(self.file_path(exported), ".apkg")
        csv_upload = self.upload(text="Generated front,Generated answer\n")
        code = """
import time
from memr_srs_engine.handlers import transfer
for name in ('_restore', '_metadata'):
    original = getattr(transfer, name)
    def delayed(*args, _original=original, **kwargs):
        time.sleep(0.8)
        return _original(*args, **kwargs)
    setattr(transfer, name, delayed)
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        for method, upload in (("transfer.inspectPackage", package), ("transfer.inspectCsv", csv_upload)):
            with self.subTest(method=method), tempfile.TemporaryDirectory(dir=self.root) as directory:
                incoming = Path(directory) / "incoming"
                incoming.mkdir()
                shutil.copyfile(upload, incoming / upload.name)
                with EnginePipe(directory, code=code) as pipe:
                    # Warm startup before measuring the request/control lane.
                    self.assertIn("result", pipe.call("engine.status"))
                    started = time.monotonic()
                    response = pipe.call(method, {"uploadId": upload.name})
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertIn("operationId", response.get("result", {}), response)
                    operation_id = response["result"]["operationId"]
                    state = pipe.call("progress.get", {"operationId": operation_id})["result"]
                    self.assertEqual(state["status"], "running", state)
                    self.assertEqual(pipe.call("engine.status")["error"]["code"], "busy")
                    self.assertIn("result", pipe.call("progress.abort", {"operationId": operation_id}))
                    deadline = time.monotonic() + 5
                    while state["status"] == "running" and time.monotonic() < deadline:
                        state = pipe.call("progress.get", {"operationId": operation_id})["result"]
                        time.sleep(0.02)
                    self.assertEqual(state["status"], "aborted", state)
                    self.assertIsNone(state["result"])
                    self.assertFalse(state["abortable"])

    def test_apkg_round_trips_with_and_without_scheduling_and_media(self):
        for scheduling in (True, False):
            with self.subTest(scheduling=scheduling):
                result = self.long("transfer.exportPackage", dict(scope={}, includeScheduling=scheduling,
                    includeMedia=True, includeDeckConfigs=True))
                upload = self.upload(self.file_path(result), ".apkg")
                inspection = self.inspect("transfer.inspectPackage", dict(uploadId=upload.name))
                self.assertEqual((inspection["notes"], inspection["cards"], inspection["media"]), (1, 1, 1))
                self.assertEqual(inspection["notetypes"], 1)
                self.assertEqual(inspection["schedulingIncluded"], scheduling)
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    fresh = Collection(str(Path(directory) / "collection.anki2"))
                    original = self.engine.col
                    self.engine.col = fresh
                    try:
                        destination = Path(fresh.path).parent / "incoming"
                        destination.mkdir()
                        shutil.copyfile(upload, destination / upload.name)
                        imported = self.long("transfer.importPackage", self.import_options(upload))
                        self.assertEqual(imported["imported"]["notes"], 1)
                        note = fresh.get_note(fresh.find_notes("")[0])
                        self.assertEqual(note.fields, self.col.get_note(self.data["noteIds"][0]).fields)
                        card = fresh.get_card(fresh.find_cards("")[0])
                        self.assertEqual(card.reps, 4 if scheduling else 0)
                        self.assertEqual(Path(fresh.media.dir(), "generated.txt").read_text(), "generated media")
                        self.assertTrue(imported["undo"]["canUndo"])
                        fresh.undo(); self.assertEqual(fresh.note_count(), 0)
                        fresh.redo(); self.assertEqual(fresh.note_count(), 1)
                    finally:
                        self.engine.col = original
                        fresh.close()

    def test_colpkg_export_reopens_and_import_forces_backup(self):
        previous_scheduler = self.col.sched
        exported = self.long("transfer.exportCollection", {"includeMedia": True})
        self.assertIsNot(self.col.sched, previous_scheduler)
        native_offset = -int(datetime.now().astimezone().utcoffset().total_seconds() / 60)
        self.assertEqual(self.col.get_config("localOffset", 0), native_offset)
        upload = self.upload(self.file_path(exported), ".colpkg")
        self.assertEqual(self.inspect("transfer.inspectPackage", {"uploadId": upload.name})["notes"], 1)
        added = self.col.new_note(self.col.models.by_name("Basic")); added["Front"] = "later"
        self.col.add_note(added, 1)
        self.long("transfer.importPackage", self.import_options(upload))
        self.assertEqual(self.col.note_count(), 1)
        self.assertTrue(list((self.root / "backups").glob("*.colpkg")))
        self.assertEqual(Path(self.col.media.dir(), "generated.txt").read_text(), "generated media")

    def test_successful_native_imports_preserve_completed_result_after_late_abort(self):
        package = self.long("transfer.exportPackage", dict(scope={}, includeScheduling=True,
            includeMedia=True, includeDeckConfigs=True))
        apkg = self.upload(self.file_path(package), ".apkg")
        collection = self.long("transfer.exportCollection", {"includeMedia": True})
        colpkg = self.upload(self.file_path(collection), ".colpkg")
        csv_upload = self.upload(text="Late CSV question,Late CSV answer\n")

        def abort_after_call(original):
            def call(*args, **kwargs):
                result = original(*args, **kwargs)
                self.engine.progress.abort(self.engine.progress.current)
                return result
            return call

        self.col.remove_notes(self.col.find_notes(""))
        with patch.object(self.col, "import_anki_package", side_effect=abort_after_call(self.col.import_anki_package)):
            imported = self.long("transfer.importPackage", self.import_options(apkg))
        self.assertEqual(imported["imported"]["notes"], 1)
        self.assertEqual(self.col.note_count(), 1)
        self.assertTrue(imported["undo"]["canUndo"])

        with patch.object(self.col, "import_csv", side_effect=abort_after_call(self.col.import_csv)):
            imported = self.long("transfer.importCsv", self.csv_options(csv_upload))
        self.assertEqual(imported["imported"]["notes"], 1)
        self.assertEqual(self.col.note_count(), 2)
        self.assertTrue(imported["undo"]["canUndo"])

        restore = transfer._restore
        def abort_after_primary_restore(col, *args, **kwargs):
            result = restore(col, *args, **kwargs)
            if col is self.col:
                self.engine.progress.abort(self.engine.progress.current)
            return result
        previous_scheduler = self.col.sched
        with patch.object(transfer, "_restore", side_effect=abort_after_primary_restore):
            imported = self.long("transfer.importPackage", self.import_options(colpkg))
        self.assertEqual(self.col.note_count(), 1)
        self.assertIsNot(self.col.sched, previous_scheduler)
        self.assertTrue(all(imported["changes"].values()))
        self.assertFalse(imported["undo"]["canUndo"])

    def test_package_deck_and_search_selection_with_filtered_cards(self):
        parent = self.data["deckIds"][0]
        child = self.col.decks.id("Generated deck 1::Child")
        other = self.col.decks.id("Other deck")
        for deck, front in ((child, "Child question"), (other, "Other question")):
            note = self.col.new_note(self.col.models.by_name("Basic"))
            note["Front"] = front
            self.col.add_note(note, deck)
        filtered = self.col.sched.get_or_create_filtered_deck(0)
        filtered.name = "Generated filtered deck"
        filtered.config.search_terms[0].search = f"cid:{self.data['cardIds'][0]}"
        filtered_id = self.col.sched.add_or_update_filtered_deck(filtered).id
        self.col.sched.rebuild_filtered_deck(filtered_id)
        self.assertEqual(self.col.get_card(self.data["cardIds"][0]).odid, parent)
        # Anki's native deck limit uses current membership: the parent card is
        # now in the filtered deck, where a filtered-deck scope/search can select it.
        for scope, count in (({"deckId": parent, "includeChildren": False}, 0),
                             ({"deckId": parent, "includeChildren": True}, 1),
                             ({"deckId": filtered_id, "includeChildren": False}, 1),
                             ({"search": f"cid:{self.data['cardIds'][0]}"}, 1)):
            with self.subTest(scope=scope):
                result = self.long("transfer.exportPackage", dict(scope=scope, includeScheduling=True,
                    includeMedia=True, includeDeckConfigs=True))
                upload = self.upload(self.file_path(result), ".apkg")
                inspected = self.inspect("transfer.inspectPackage", {"uploadId": upload.name})
                self.assertEqual((inspected["notes"], inspected["cards"]), (count, count))

    def test_colpkg_refuses_if_forced_backup_fails(self):
        exported = self.long("transfer.exportCollection", {"includeMedia": False})
        upload = self.upload(self.file_path(exported), ".colpkg")
        with patch.object(self.col, "create_backup", side_effect=RuntimeError("backup failed")):
            ack = self.rpc("transfer.importPackage", self.import_options(upload))
            self.engine.progress.thread.join(5)
        state = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.col.note_count(), 1)

    def test_colpkg_native_preflight_refuses_missing_media_before_current_writes(self):
        Path(self.col.media.dir(), "second.txt").write_text("original second media")
        exported = self.long("transfer.exportCollection", {"includeMedia": True, "legacy": True})
        with zipfile.ZipFile(self.file_path(exported)) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        media = json.loads(entries["media"])
        indexes = sorted(media, key=int)
        self.assertEqual(len(indexes), 2)
        entries[indexes[0]] = b"must never replace current media"
        del entries[indexes[1]]
        path = self.upload(extension=".colpkg")
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        before = {file.name: file.read_bytes() for file in Path(self.col.media.dir()).iterdir() if file.is_file()}
        with patch.object(self.col, "create_backup", wraps=self.col.create_backup) as backup:
            with self.assertRaises(EngineError):
                self.long("transfer.importPackage", self.import_options(path))
            backup.assert_not_called()
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual({file.name: file.read_bytes() for file in Path(self.col.media.dir()).iterdir() if file.is_file()}, before)
        self.assertEqual(list(self.root.glob("srs-validate-*")), [])

    def test_csv_import_mapping_export_round_trip_and_undo(self):
        upload = self.upload(text='"generated, front","<b>answer</b>"\n')
        options = self.csv_options(upload)
        meta = self.inspect("transfer.inspectCsv", {"uploadId": upload.name, "delimiter": ",", "isHtml": True})
        self.assertEqual(meta["rows"], [["generated, front", "<b>answer</b>"]])
        imported = self.long("transfer.importCsv", options)
        self.assertEqual(imported["imported"]["notes"], 1)
        note = self.col.get_note(self.col.find_notes("tag:generated")[0])
        self.assertEqual(note.fields, meta["rows"][0])
        self.assertEqual(note.cards()[0].did, self.data["deckIds"][0])
        self.col.undo(); self.assertEqual(self.col.note_count(), 1)
        self.col.redo(); self.assertEqual(self.col.note_count(), 2)
        export = self.long("transfer.exportCsv", dict(scope={"search": "tag:generated"}, mode="notes",
            delimiter=",", includeHtml=True, includeTags=True, includeGuid=False, includeDeck=False, includeNotetype=False))
        again = self.upload(self.file_path(export), ".csv")
        options["uploadId"] = again.name
        options["duplicateMode"] = "preserve"
        result = self.long("transfer.importCsv", options)
        self.assertEqual(self.col.note_count(), 2)
        self.assertEqual(result["imported"]["skipped"], 1)
        self.long("transfer.exportCsv", dict(scope={}, mode="cards", delimiter="\t", includeHtml=False,
            includeTags=False, includeGuid=False, includeDeck=False, includeNotetype=False))

    def test_csv_directive_like_data_and_quoted_multiline_comments_survive(self):
        note = self.col.get_note(self.data["noteIds"][0])
        note["Front"] = "#separator:tab"
        note["Back"] = "Line one\n#separator:still note data"
        self.col.update_note(note)
        result = self.long("transfer.exportCsv", dict(scope={}, mode="notes", delimiter=",",
            includeHtml=True, includeTags=False, includeGuid=False, includeDeck=False, includeNotetype=False))
        path = self.file_path(result)
        with path.open(encoding="utf-8", newline="") as file:
            records = list(csv.reader(file, delimiter=","))
        self.assertEqual(records[0], ["#separator:comma"])
        self.assertIn(note.fields, records)
        upload = self.upload(path, ".csv")
        meta = self.inspect("transfer.inspectCsv", {"uploadId": upload.name, "delimiter": ","})
        self.assertEqual((meta["rows"], meta["totalRows"]), ([note.fields], 1))
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            fresh = Collection(str(Path(directory) / "collection.anki2"))
            self.engine.col = fresh
            try:
                copied = self.upload(path, ".csv")
                options = self.csv_options(copied)
                options["notetypeId"] = fresh.models.by_name("Basic")["id"]
                options["deckId"] = 1
                self.long("transfer.importCsv", options)
                self.assertEqual(fresh.get_note(fresh.find_notes("")[0]).fields, note.fields)
            finally:
                self.engine.col = self.col
                fresh.close()

    def test_csv_scan_abort_stops_before_native_import(self):
        upload = self.upload(text="first,answer\nsecond,answer\n")
        entered, resume = threading.Event(), threading.Event()
        reader = csv.reader
        parsed = []

        def paused_reader(*args, **kwargs):
            for row in reader(*args, **kwargs):
                parsed.append(row)
                entered.set()
                resume.wait(5)
                yield row

        with patch.object(transfer.csv, "reader", side_effect=paused_reader), \
                patch.object(self.col, "import_csv", wraps=self.col.import_csv) as native:
            ack = self.rpc("transfer.importCsv", self.csv_options(upload))
            self.assertTrue(entered.wait(5))
            self.rpc("progress.abort", {"operationId": ack["operationId"]})
            resume.set()
            self.engine.progress.thread.join(5)
            native.assert_not_called()
        state = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(state["status"], "aborted", state)

        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(len(parsed), 1)

    def test_csv_metadata_sample_matches_pinned_native_directives_and_delimiters(self):
        cases = [
            ("#separator:tab\n#html:true\n#columns:Front\tBack\nquestion\t<b>answer</b>\n", {}),
            ("#separator:comma\n#separator:tab\n#columns:Front\tBack\nquestion\tanswer\n", {}),
            ("#separator:tab\nquestion,answer\n", {"delimiter": ","}),
            ("\ufeff#separator:semicolon\r\n\"first;quoted\";\"line one\r\n#quoted data\"\r\n", {}),
            ("tags:legacy-tag\nfirst,answer\nsecond,answer\n", {"delimiter": ","}),
            ("tags:legacy-tag\n#html:true\nfirst,answer\nsecond,answer\n", {"delimiter": ","}),
            ("tags:legacy-tag\n" + "".join(f"row {index},answer\n" for index in range(6)), {"delimiter": ","}),
            ("\n#html:true\nquestion,answer\n", {"delimiter": ","}),
            ("#tags:one\n\n#html:true\nquestion,answer\n", {"delimiter": ","}),
            ("   \nquestion,answer\n", {"delimiter": ","}),
            (f"#notetype:{self.col.models.by_name('Basic')['id']}\n#deck:{self.data['deckIds'][0]}\n#tags:one two\n#tags column:3\nquestion,answer,tag\n", {"delimiter": ","}),
            ("question|answer\nsecond|<b>HTML</b>\n", {}),
        ]
        cases += [(f'"question"{delimiter}"answer"\n', {"delimiter": delimiter}) for delimiter in transfer.DELIMITERS]
        cases += [("question,answer\n", {"extension": extension}) for extension in (".csv", ".tsv", ".txt")]
        for text, params in cases:
            with self.subTest(text=text[:80]):
                params = dict(params)
                path = self.upload(text=text, extension=params.pop("extension", ".csv"))
                native = self.col.get_csv_metadata(str(path), transfer._delimiter(params["delimiter"]) if "delimiter" in params else None)
                metadata, _, _, _ = transfer._metadata(self.engine, path, params)
                self.assertEqual(metadata, native)
        path = self.upload(text="tags:legacy-tag\n" + "".join(f"row {index},answer\n" for index in range(6)))
        inspected = self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
        self.assertEqual(inspected["totalRows"], 6)
        self.assertEqual(inspected["rows"][0], ["row 0", "answer"])

    def test_csv_field_column_and_multiline_boundaries(self):
        for width, size, accepted in ((2, 65536, True), (2, 65537, False),
                                      (512, 1, True), (513, 1, False)):
            with self.subTest(width=width, size=size):
                path = self.upload()
                field = ("Line one\n#quoted data\n" + "x" * size)[:size]
                with path.open("w", encoding="utf-8", newline="") as file:
                    csv.writer(file, quoting=csv.QUOTE_ALL).writerow([field] * width)
                if accepted:
                    result = self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
                    self.assertEqual((len(result["rows"][0]), len(result["rows"][0][0]), result["totalRows"]), (width, size, 1))
                else:
                    with self.assertRaises(EngineError) as error:
                        self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
                    self.assertEqual(error.exception.code, "invalid_params")

    def test_csv_large_upload_native_metadata_reads_bounded_sample_and_explicit_preview(self):
        path = self.upload(text=("front," + "a" * 60000 + "\n") * 180)
        native = self.col.get_csv_metadata
        sizes = []
        def metadata(sample, delimiter):
            sizes.append(Path(sample).stat().st_size)
            self.assertNotEqual(Path(sample), path)
            return native(sample, delimiter)
        with patch.object(self.col, "get_csv_metadata", side_effect=metadata):
            result = self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
        self.assertEqual(result["totalRows"], 180)
        self.assertEqual(len(result["rows"]), 100)
        self.assertTrue(result["previewTruncated"])
        self.assertTrue(result["warnings"])
        self.assertLess(max(sizes), 400000)
        # Escaped JSON, rather than just source bytes, governs the wire budget.
        path = self.upload(text=("front," + "é" * 60000 + "\n") * 30)
        result = self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
        self.assertEqual(result["totalRows"], 30)
        self.assertLess(len(result["rows"]), 30)
        self.assertTrue(result["previewTruncated"])
        self.assertLess(len(json.dumps(result, ensure_ascii=True)), transfer.CSV_PREVIEW_LIMIT + 10000)

    def test_csv_leading_metadata_limit_and_comment_scan_cancellation(self):
        path = self.upload(text="#" + "x" * (transfer.CSV_METADATA_LIMIT - 2) + "\na,b\n")
        self.assertEqual(self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})["totalRows"], 1)
        path = self.upload(text="#" + "x" * transfer.CSV_METADATA_LIMIT + "\na,b\n")
        with self.assertRaises(EngineError) as error:
            self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
        self.assertEqual(error.exception.code, "invalid_params")
        self.assertIn("2 MiB", error.exception.message)
        entered, resume = threading.Event(), threading.Event()
        original = transfer._csv_delimiter
        def delayed(*args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("CSV scan was not resumed")
            return original(*args, **kwargs)
        with patch.object(transfer, "_csv_delimiter", side_effect=delayed), \
                patch.object(self.col, "get_csv_metadata", wraps=self.col.get_csv_metadata) as native:
            ack = self.rpc("transfer.inspectCsv", {"uploadId": path.name})
            try:
                self.assertTrue(entered.wait(5))
                self.rpc("progress.abort", {"operationId": ack["operationId"]})
            finally:
                resume.set()
                self.engine.progress.thread.join(5)
            native.assert_not_called()
        state = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(state["status"], "aborted", state)

    def test_scratch_backend_controls_lifetime_and_python_progress(self):
        entered, resume = threading.Event(), threading.Event()
        exported = self.long("transfer.exportPackage", dict(scope={}, includeScheduling=True,
            includeMedia=True, includeDeckConfigs=True))
        path = self.upload(self.file_path(exported), ".apkg")
        original = transfer._restore
        selected = []
        def delayed(col, *args, **kwargs):
            selected.append(col)
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("Scratch restore was not resumed")
            return original(col, *args, **kwargs)
        with patch.object(transfer, "_restore", side_effect=delayed), \
                patch.object(self.col, "set_wants_abort", wraps=self.col.set_wants_abort) as main_abort:
            ack = self.rpc("transfer.inspectPackage", {"uploadId": path.name})
            try:
                self.assertTrue(entered.wait(5))
                scratch = selected[0]
                self.assertIs(self.engine.progress.backend, scratch)
                with patch.object(scratch, "set_wants_abort", wraps=scratch.set_wants_abort) as scratch_abort:
                    self.rpc("progress.abort", {"operationId": ack["operationId"]})
                    scratch_abort.assert_called_once()
                main_abort.assert_not_called()
            finally:
                resume.set()
                self.engine.progress.thread.join(5)
        self.assertIs(self.engine.progress.backend, self.col)
        self.assertIsNone(scratch.db)
        self.assertEqual(list(self.root.glob("srs-inspect-*")), [])
        state = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(state["status"], "aborted", state)
        self.assertIsNone(state["result"])
        self.assertEqual(state["error"]["code"], "aborted")
        self.assertEqual(self.col.note_count(), 1)

    def test_progress_backend_abort_handoff_retained_handles_and_terminal_event(self):
        path = self.upload(text="a,b\n")
        first = self.rpc("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
        self.engine.progress.thread.join(5)
        prior = self.rpc("progress.get", {"operationId": first["operationId"]})
        self.assertEqual(prior["status"], "completed", prior)
        entered, resume = threading.Event(), threading.Event()
        handler = self.engine.registry.handlers["transfer.inspectCsv"]
        def delayed(engine, params):
            with engine.progress.using_backend(None):
                engine.progress.report(done=2, total=5, label="Synthetic scan boundary")
                entered.set()
                if not resume.wait(5):
                    raise TimeoutError("Scan fixture did not resume")
            return handler(engine, params)
        with patch.dict(self.engine.registry.handlers, {"transfer.inspectCsv": delayed}):
            second = self.rpc("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
            try:
                self.assertTrue(entered.wait(5))
                self.rpc("progress.abort", {"operationId": first["operationId"]})
                self.assertFalse(self.engine.progress.abort_requested)
                self.engine.progress.next_event = 0
                self.engine.progress.tick()
                state = self.rpc("progress.get", {"operationId": second["operationId"]})
                self.assertEqual((state["done"], state["total"], state["label"]), (2, 5, "Synthetic scan boundary"))
                self.assertEqual(self.rpc("progress.get", {"operationId": first["operationId"]}), prior)
                with self.assertRaises(EngineError) as error:
                    self.rpc("progress.abort", {"operationId": "missing"})
                self.assertEqual(error.exception.code, "not_found")
            finally:
                resume.set()
                self.engine.progress.thread.join(5)
        result = self.rpc("progress.get", {"operationId": second["operationId"]})
        self.assertEqual(result["status"], "completed", result)
        start = self.output.tell()
        self.engine.progress.tick()
        frames = self.output.getvalue()[start:].splitlines()
        self.assertEqual(len(frames), 1)
        self.assertEqual(json.loads(frames[0])["progress"], result)
        start = self.output.tell()
        self.engine.progress.tick()
        self.assertEqual(self.output.getvalue()[start:], b"")

    def test_consecutive_long_operations_preserve_each_terminal_once_with_request_ids(self):
        for status in ("completed", "failed", "aborted"):
            with self.subTest(status=status):
                offset = self.output.tell()
                def completed(engine, params):
                    return mutation(engine.col, emptyCards={"notes": []})
                def first(engine, params):
                    if status == "failed":
                        raise EngineError("internal", "Generated failure.")
                    if status == "aborted":
                        engine.progress.abort(engine.progress.current)
                        engine.progress.check_abort()
                    return completed(engine, params)
                with patch.dict(self.engine.registry.handlers, {"maintenance.emptyCards": first}):
                    first_id = self.sequence + 1
                    first_ack = self.rpc("maintenance.emptyCards")
                    self.engine.progress.thread.join(5)
                # Reading a terminal handle does not emit or consume its event.
                self.assertEqual(self.rpc("progress.get", {"operationId": first_ack["operationId"]})["status"], status)
                with patch.dict(self.engine.registry.handlers, {"maintenance.emptyCards": completed}):
                    second_id = self.sequence + 1
                    second_ack = self.rpc("maintenance.emptyCards")
                    self.engine.progress.thread.join(5)
                self.engine.progress.tick()
                self.engine.progress.tick()
                self.rpc("progress.get", {"operationId": first_ack["operationId"]})
                self.rpc("progress.get", {"operationId": second_ack["operationId"]})
                self.engine.progress.tick()
                events = [frame for line in self.output.getvalue()[offset:].splitlines()
                          if (frame := json.loads(line)).get("event") == "progress"]
                self.assertEqual([(frame["id"], frame["operationId"], frame["progress"]["status"]) for frame in events], [
                    (first_id, first_ack["operationId"], status),
                    (second_id, second_ack["operationId"], "completed")])

    def test_native_pipe_consecutive_long_operations_emit_both_terminals(self):
        code = """
from pathlib import Path
import time
from memr_srs_engine.registry import HANDLERS
from memr_srs_engine import handlers
original = HANDLERS['maintenance.checkDatabase']
calls = 0
def repair(engine, params):
    global calls
    calls += 1
    result = original(engine, params)
    if calls == 1:
        (Path(engine.col.path).parent / 'first-native-repair').touch()
        # Make the first main-loop tick observe running. Then the native
        # result completes before a queued second call, within the 250ms tick.
        time.sleep(0.075)
    return result
HANDLERS['maintenance.checkDatabase'] = repair
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        directory = self.root / "consecutive-native"
        directory.mkdir()
        with EnginePipe(directory, code=code) as pipe:
            pipe.call("engine.status")
            first_id = pipe.send("maintenance.checkDatabase")
            first, events = pipe.response(first_id)
            deadline = time.monotonic() + 5
            while not (directory / "first-native-repair").exists() and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertTrue((directory / "first-native-repair").exists())
            time.sleep(0.1)
            second_id = pipe.send("maintenance.checkDatabase")
            second, between = pipe.response(second_id)
            events.extend(between)
            while len(events) < 2:
                events.append(pipe.receive())
            self.assertEqual([(frame["id"], frame["operationId"], frame["progress"]["status"]) for frame in events], [
                (first_id, first["result"]["operationId"], "completed"),
                (second_id, second["result"]["operationId"], "completed")])
            _, trailing = pipe.response(pipe.send("progress.get", {"operationId": first["result"]["operationId"]}))
            self.assertEqual(trailing, [])

    def test_collection_generation_counts_only_admitted_mutating_dispatches(self):
        self.assertEqual(self.engine.collection_generation, 0)
        path = self.upload(text="a,b\n")
        self.inspect("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
        self.rpc("engine.status")
        self.rpc("progress.get")
        self.assertEqual(self.engine.collection_generation, 0)
        with self.assertRaises(EngineError):
            self.rpc("media.addFromUpload", {"unexpected": True})
        self.assertEqual(self.engine.collection_generation, 0)
        media = self.upload(text="generated media")
        params = {"uploadId": media.name, "filename": "generation.txt"}
        seen = []
        def failed(engine, args):
            seen.append(engine.collection_generation)
            raise EngineError("invalid_params", "Synthetic rejected mutation.")
        with patch.dict(self.engine.registry.handlers, {"media.addFromUpload": failed}):
            with self.assertRaises(EngineError):
                self.rpc("media.addFromUpload", params)
        self.assertEqual(seen, [1])
        self.assertEqual(self.engine.collection_generation, 1)
        self.rpc("media.addFromUpload", params)
        self.assertEqual(self.engine.collection_generation, 2)
        self.long("maintenance.removeEmptyCards", {"cardIds": []})
        self.assertEqual(self.engine.collection_generation, 3)
        entered, resume = threading.Event(), threading.Event()
        original = self.engine.registry.handlers["transfer.inspectCsv"]
        def delayed(engine, args):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("Generation fixture did not resume")
            return original(engine, args)
        with patch.dict(self.engine.registry.handlers, {"transfer.inspectCsv": delayed}):
            ack = self.rpc("transfer.inspectCsv", {"uploadId": path.name, "delimiter": ","})
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(EngineError) as error:
                    self.rpc("media.addFromUpload", params)
                self.assertEqual(error.exception.code, "busy")
                self.rpc("progress.get", {"operationId": ack["operationId"]})
                self.assertEqual(self.engine.collection_generation, 3)
            finally:
                resume.set()
                self.engine.progress.thread.join(5)
        self.assertEqual(self.engine.collection_generation, 3)

    def test_low_scratch_storage_refuses_before_native_restore(self):
        exported = self.long("transfer.exportPackage", dict(scope={}, includeScheduling=True,
            includeMedia=True, includeDeckConfigs=True))
        path = self.upload(self.file_path(exported), ".apkg")
        usage = SimpleNamespace(free=0)
        with patch.object(transfer.shutil, "disk_usage", return_value=usage), \
                patch.object(transfer, "_restore", wraps=transfer._restore) as restore:
            with self.assertRaises(EngineError) as error:
                self.inspect("transfer.inspectPackage", {"uploadId": path.name})
            restore.assert_not_called()
        self.assertEqual(error.exception.code, "internal")
        self.assertIn("free storage", error.exception.message)
        self.assertEqual(list(self.root.glob("srs-inspect-*")), [])

    def test_scheduler_bootstrap_cold_existing_and_repeated_reads(self):
        native_offset = -int(datetime.now().astimezone().utcoffset().total_seconds() / 60)
        with temp_collection() as col:
            self.assertIsNone(col.get_config("rollover"))
            self.assertIsNone(col.get_config("localOffset"))
            before_mod = col.db.scalar("select mod from col")
            before_undo = col.undo_status()
            hooks = []
            def observe(engine):
                hooks.append((engine.col.get_config("rollover"), engine.col.get_config("localOffset")))
            with patch.object(protocol, "STARTUP_HOOKS", [*protocol.STARTUP_HOOKS, observe]):
                engine = Engine(col, Registry(), FrameWriter(io.BytesIO()), self.engine.logger)
            self.assertEqual(hooks, [(4, col.get_config("localOffset"))])
            # Anki leaves an absent offset as its native zero default in UTC.
            # Check the actual offset, including a deliberately stale value below.
            self.assertEqual(col.get_config("localOffset", 0), native_offset)
            self.assertEqual(col.db.scalar("select mod from col"), before_mod)
            self.assertEqual(col.undo_status(), before_undo)
            col.set_config("rollover", 7, undoable=True)
            col.set_config("localOffset", native_offset + 123, undoable=True)
            before_undo = col.undo_status()
            col.close(); col.reopen(); col._load_scheduler()
            protocol.initialize_scheduling(col)
            self.assertEqual(col.get_config("rollover"), 7)
            self.assertEqual(col.get_config("localOffset", 0), native_offset)
            # Reopening resets native undo; timing adds no new undo operation.
            self.assertFalse(col.undo_status().undo)
            before = (col.db.all("select key, val from config order by key"),
                      col.db.scalar("select mod from col"), col.undo_status())
            for _ in range(3):
                protocol.initialize_scheduling(col)
                self.assertEqual((col.db.all("select key, val from config order by key"),
                                  col.db.scalar("select mod from col"), col.undo_status()), before)
            self.assertEqual(engine.collection_generation, 0)

    def test_scheduler_bootstrap_startup_failure_is_typed_and_releases_collection(self):
        code = """
from memr_srs_engine import protocol
from memr_srs_engine.errors import EngineError
def fail(col):
    raise EngineError('conflict', 'collection is open in another program')
protocol.initialize_scheduling = fail
from memr_srs_engine.__main__ import main
raise SystemExit(main())
"""
        directory = self.root / "bootstrap-failure"
        directory.mkdir()
        with EnginePipe(directory, code=code) as pipe:
            self.assertEqual(pipe.receive(), {"id": None, "error": {
                "code": "conflict", "message": "collection is open in another program"}})
            self.assertEqual(pipe.process.wait(timeout=5), 1)
            # The outer owner still closes a collection if bootstrap fails.
            col = Collection(str(pipe.collection_path))
            try:
                self.assertEqual(col.note_count(), 0)
            finally:
                col.close()

    def test_csv_and_collection_import_into_fresh_collections(self):
        csv_result = self.long("transfer.exportCsv", dict(scope={}, mode="notes", delimiter=",",
            includeHtml=True, includeTags=True, includeGuid=False, includeDeck=False, includeNotetype=False))
        collection_result = self.long("transfer.exportCollection", {"includeMedia": True})
        for kind, exported in ((".csv", csv_result), (".colpkg", collection_result)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.root) as directory:
                fresh = Collection(str(Path(directory) / "collection.anki2"))
                source = self.file_path(exported)
                original = self.engine.col
                self.engine.col = fresh
                try:
                    upload = self.upload(source, kind)
                    if kind == ".csv":
                        params = self.csv_options(upload)
                        params["notetypeId"] = fresh.models.by_name("Basic")["id"]
                        params["deckId"] = 1
                        self.long("transfer.importCsv", params)
                    else:
                        self.long("transfer.importPackage", self.import_options(upload))
                        self.assertEqual(Path(fresh.media.dir(), "generated.txt").read_text(), "generated media")
                    note = fresh.get_note(fresh.find_notes("")[0])
                    self.assertEqual(note.fields, original.get_note(self.data["noteIds"][0]).fields)
                    self.assertEqual((fresh.note_count(), fresh.card_count()), (1, 1))
                finally:
                    self.engine.col = original
                    fresh.close()

    def test_bad_upload_paths_symlinks_partial_and_oversize(self):
        for value in ("../escape.apkg", "/outside.apkg", "x\\y.apkg", str(uuid.uuid4()) + ".upload"):
            with self.subTest(value=value), self.assertRaises(EngineError) as caught:
                resolve_upload(self.engine, value)
            self.assertEqual(caught.exception.code, "invalid_params")
        missing = str(uuid.uuid4()) + ".apkg"
        with self.assertRaises(EngineError) as caught:resolve_upload(self.engine, missing)
        self.assertEqual(caught.exception.code, "not_found")
        upload = self.upload()
        upload.unlink(); upload.symlink_to(self.root / "collection.anki2")
        with self.assertRaises(EngineError):resolve_upload(self.engine, upload.name)
        upload.unlink(); upload.touch()
        with upload.open("wb") as file:file.truncate(2 * 1024 * 1024 * 1024 + 1)
        with self.assertRaises(EngineError):resolve_upload(self.engine, upload.name)

    def test_malformed_and_traversal_packages(self):
        for entry in ("../escape", "/escape", "x\\y", "regular-invalid", "١٢٣"):
            upload = self.upload(extension=".apkg")
            with zipfile.ZipFile(upload, "w") as package:
                package.writestr("collection.anki2", "invalid")
                package.writestr("media", "{}")
                package.writestr(entry, "invalid")
            with self.assertRaises(EngineError) as caught:
                self.inspect("transfer.inspectPackage", {"uploadId": upload.name})
            self.assertEqual(caught.exception.code, "invalid_params")

    def test_package_refuses_special_unix_members(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK, stat.S_IFDIR):
            with self.subTest(mode=mode):
                upload = self.upload(extension=".apkg")
                with zipfile.ZipFile(upload, "w") as archive:
                    archive.writestr("collection.anki2", "invalid")
                    archive.writestr("media", "{}")
                    special = zipfile.ZipInfo("0")
                    special.create_system = 3
                    special.external_attr = (mode | 0o600) << 16
                    archive.writestr(special, "invalid")
                with self.assertRaises(EngineError) as caught:
                    self.inspect("transfer.inspectPackage", {"uploadId": upload.name})
                self.assertEqual(caught.exception.code, "invalid_params")

    def test_native_package_rejects_media_map_traversal(self):
        exported = self.long("transfer.exportPackage", dict(scope={}, includeScheduling=True,
            includeMedia=True, includeDeckConfigs=True, legacy=True))
        with zipfile.ZipFile(self.file_path(exported)) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        original_media = json.loads(entries["media"])
        self.assertIn("generated.txt", original_media.values())
        for filename in ("benign.txt", "../escaped.txt"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory(dir=self.root) as directory:
                fresh = Collection(str(Path(directory) / "collection.anki2"))
                self.engine.col = fresh
                try:
                    upload = self.upload(extension=".apkg")
                    changed = {key: filename for key in original_media}
                    with zipfile.ZipFile(upload, "w") as archive:
                        for name, contents in entries.items():
                            archive.writestr(name, json.dumps(changed) if name == "media" else contents)
                    ack = self.rpc("transfer.importPackage", self.import_options(upload))
                    self.engine.progress.thread.join(5)
                    state = self.rpc("progress.get", {"operationId": ack["operationId"]})
                    if filename == "benign.txt":
                        self.assertEqual(state["status"], "completed", state)
                        self.assertEqual((fresh.note_count(), fresh.card_count()), (1, 1))
                    else:
                        self.assertEqual(state["status"], "failed", state)
                        # The native backend rejects a hostile map. The core
                        # mapper exposes a generic error without its diagnostics.
                        self.assertEqual(state["error"], {"code": "internal",
                            "message": "The engine could not complete the request."})
                        self.assertFalse((Path(directory) / "escaped.txt").exists())
                finally:
                    self.engine.col = self.col
                    fresh.close()

    def test_invalid_params_and_missing_resources_per_method(self):
        for name in ("inspectPackage", "importPackage", "inspectCsv", "importCsv", "exportPackage", "exportCollection", "exportCsv"):
            with self.subTest(name=name), self.assertRaises(EngineError) as caught:
                self.rpc("transfer." + name, {"unexpected": True})
            self.assertEqual(caught.exception.code, "invalid_params")
        upload = self.upload()
        options = self.csv_options(upload); options["deckId"] = 9007199254740000
        ack = self.rpc("transfer.importCsv", options); self.engine.progress.thread.join(5)
        state = self.rpc("progress.get", {"operationId": ack["operationId"]})
        self.assertEqual(state["error"]["code"], "not_found")
        for name, params in (("transfer.inspectCsv", {"uploadId": str(uuid.uuid4()) + ".csv"}),
                             ("transfer.inspectPackage", {"uploadId": str(uuid.uuid4()) + ".apkg"})):
            with self.assertRaises(EngineError) as caught:self.inspect(name, params)
            self.assertEqual(caught.exception.code, "not_found")
        for name, params in (("transfer.exportPackage", dict(scope={"deckId": 9007199254740000},
                includeScheduling=True, includeMedia=True, includeDeckConfigs=True)),
                ("transfer.exportCsv", dict(scope={"deckId": 9007199254740000}, mode="notes",
                delimiter=",", includeHtml=True, includeTags=True, includeGuid=True,
                includeDeck=True, includeNotetype=True))):
            ack = self.rpc(name, params); self.engine.progress.thread.join(5)
            state = self.rpc("progress.get", {"operationId": ack["operationId"]})
            self.assertEqual(state["error"]["code"], "not_found")

    def test_real_long_operation_abort_before_native_work_and_reset(self):
        upload = self.upload()
        methods = {"transfer.importPackage": self.import_options(self.upload(extension=".apkg")),
            "transfer.importCsv": self.csv_options(upload),
            "transfer.exportPackage": dict(scope={}, includeScheduling=True, includeMedia=True, includeDeckConfigs=True),
            "transfer.exportCollection": dict(includeMedia=True),
            "transfer.exportCsv": dict(scope={}, mode="notes", delimiter=",", includeHtml=True,
                includeTags=True, includeGuid=False, includeDeck=False, includeNotetype=False)}
        for name, params in methods.items():
            with self.subTest(name=name):
                entered, resume = threading.Event(), threading.Event()
                original = self.engine.registry.handlers[name]
                def delayed(engine, args):
                    entered.set(); resume.wait(5)
                    return original(engine, args)
                native_names = {"transfer.importPackage": "import_anki_package", "transfer.importCsv": "import_csv",
                    "transfer.exportPackage": "export_anki_package", "transfer.exportCollection": "export_collection_package",
                    "transfer.exportCsv": "export_note_csv"}
                native_name = native_names[name]
                with patch.object(self.col, native_name, wraps=getattr(self.col, native_name)) as native, \
                        patch.dict(self.engine.registry.handlers, {name: delayed}):
                    ack = self.rpc(name, params)
                    self.assertTrue(entered.wait(5))
                    self.rpc("progress.abort", {"operationId": ack["operationId"]})
                    self.assertTrue(self.engine.progress.abort_requested)
                    resume.set(); self.engine.progress.thread.join(5)
                    native.assert_not_called()
                state = self.rpc("progress.get", {"operationId": ack["operationId"]})
                self.assertEqual(state["status"], "aborted", state)
        self.long("transfer.exportCollection", {"includeMedia": False})
        self.assertFalse(self.engine.progress.abort_requested)

    def test_export_pruning_rejects_symlink_root_and_preserves_new_tokens(self):
        folder = safe_directory(self.engine, "exports") / ("a" * 43)
        folder.mkdir(); (folder / "old.csv").write_text("old")
        os.utime(folder, (1, 1)); prune_exports(self.engine)
        self.assertFalse(folder.exists())
        root = self.root / "exports"; root.rmdir(); root.symlink_to(self.root / "incoming")
        with self.assertRaises(EngineError):prune_exports(self.engine)

    def test_generic_startup_idle_hooks_and_failure_isolation(self):
        called = []
        def startup(engine):called.append("startup")
        def idle(engine):called.append("idle")
        def broken(engine):raise RuntimeError("unpublished diagnostic")
        with patch.object(protocol, "STARTUP_HOOKS", [startup, broken]), \
                patch.object(protocol, "IDLE_HOOKS", [(1800, idle)]):
            clock = [0]
            engine = Engine(self.col, Registry(), FrameWriter(io.BytesIO()),
                            self.engine.logger, lambda: clock[0])
            self.assertEqual(called, ["startup"])
            clock[0] = 1799; engine.run_idle_hooks()
            self.assertEqual(called, ["startup"])
            clock[0] = 1800
            with patch.object(type(engine.progress), "running", new_callable=PropertyMock,
                              return_value=True):
                engine.run_idle_hooks()
            self.assertEqual(called, ["startup"])
            engine.run_idle_hooks(); engine.run_idle_hooks()
            self.assertEqual(called, ["startup", "idle"])


if __name__ == "__main__":unittest.main()
