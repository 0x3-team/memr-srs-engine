"""Package resource bounds using small structural fixtures and native exports."""
import hashlib
import io
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

import zstandard

from memr_srs_engine.errors import EngineError

try:
    from memr_srs_engine.handlers import package_bounds
except ImportError:
    package_bounds = None


MAGIC = b"\x28\xb5\x2f\xfd"


def raw_frame(data=b"", *, unknown=False, window=0x58, checksum=False):
    """Use raw structural fixtures; checksum cases use the pinned encoder."""
    if checksum:
        return zstandard.ZstdCompressor(write_checksum=True, write_content_size=not unknown).compress(data)
    if unknown:
        header = bytes([4 if checksum else 0, window])
    elif len(data) < 256:
        header = bytes([0x24 if checksum else 0x20, len(data)])
    elif len(data) < 65792:
        header = bytes([0x64 if checksum else 0x60]) + struct.pack("<H", len(data) - 256)
    else:
        header = bytes([0xA4 if checksum else 0xA0]) + struct.pack("<I", len(data))
    chunks = [data[offset:offset + 131072] for offset in range(0, len(data), 131072)] or [b""]
    blocks = b"".join(((len(value) << 3) | (index == len(chunks) - 1)).to_bytes(3, "little") + value
                      for index, value in enumerate(chunks))
    return MAGIC + header + blocks + (b"\0" * 4 if checksum else b"")


def rle_frame(sizes, *, window=0x68):
    return MAGIC + bytes([0, window]) + b"".join(
        ((size << 3) | 2 | (index == len(sizes) - 1)).to_bytes(3, "little") + b"z"
        for index, size in enumerate(sizes))


class PackageBoundsTests(unittest.TestCase):
    def setUp(self):
        if package_bounds is None:
            self.fail("Package resource preflight has not been implemented.")
        self.temp = tempfile.TemporaryDirectory(prefix="memr-package-bounds-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "generated.apkg"

    def package(self, entries=None):
        if entries is None:
            entries = [("meta", b"\x08\x03"), ("collection.anki21b", raw_frame(b"db")),
                       ("media", raw_frame())]
        with warnings.catch_warnings(), zipfile.ZipFile(self.path, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            for name, data in entries:
                entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                entry.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(entry, data)
        return self.path

    def refused(self, path=None, engine=None):
        with self.assertRaises(EngineError) as caught:
            package_bounds.validate_package_bounds(path or self.path, engine)
        self.assertEqual(caught.exception.code, "invalid_params")

    def test_latest_and_legacy_envelopes_are_admitted_without_mutation(self):
        for entries, bound in ((None, 4), ([("collection.anki2", b"db"), ("media", b"{}")], 4),
                               ([("meta", b"\x08\x02"), ("collection.anki21", b"db"), ("media", b"{}")], 6)):
            with self.subTest(entries=entries):
                path = self.package(entries)
                before = hashlib.sha256(path.read_bytes()).digest()
                self.assertEqual(package_bounds.validate_package_bounds(path), bound)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    def test_total_expansion_boundary_counts_all_members(self):
        self.package()
        # Two bytes of metadata plus two bytes of database output, empty map.
        with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", 4):
            package_bounds.validate_package_bounds(self.path)
        with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", 3):
            self.refused()

    def test_rle_bomb_over_two_gib_is_refused_without_allocating_output(self):
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", rle_frame([131072] * 16385)),
                      ("media", raw_frame())])
        self.assertLess(self.path.stat().st_size, 70000)
        self.refused()

    def test_compressed_payload_is_validated_instead_of_trusting_content_size(self):
        compressed = MAGIC + b"\x80\x58\x01\0\0\0" + b"\x0d\0\0x"
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", compressed), ("media", raw_frame())])
        self.refused()

    def test_tiny_compressed_members_count_actual_bytes_at_budget_boundary(self):
        compressed = zstandard.ZstdCompressor(write_content_size=False).compress(b"z" * 1000)
        entries = [("meta", b"\x08\x03"), ("collection.anki21b", raw_frame(b"db")), ("media", raw_frame())]
        entries += [(str(index), compressed) for index in range(10)]
        self.package(entries)
        self.assertLess(self.path.stat().st_size, 2000)
        with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", 10004):
            self.assertEqual(package_bounds.validate_package_bounds(self.path), 10004)
        with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", 10003):
            self.refused()

    def test_many_small_media_below_two_gib_are_not_charged_block_maxima(self):
        compressed = zstandard.ZstdCompressor(write_content_size=False).compress(b"z" * 100)
        entries = [("meta", b"\x08\x03"), ("collection.anki21b", raw_frame(b"db")), ("media", raw_frame())]
        # The former 128 KiB estimate charged these 16,384 members 2 GiB by
        # themselves. Actual expanded media occupies only 1,638,400 bytes.
        entries += [(str(index), compressed) for index in range(16384)]
        self.package(entries)
        self.assertEqual(package_bounds.validate_package_bounds(self.path), 1638404)

    def test_decoder_uses_positive_bounded_reads_and_stops_at_budget_plus_one(self):
        frame = zstandard.ZstdCompressor(write_content_size=False).compress(b"z" * 200000)
        requests = []
        native = zstandard.ZstdDecompressor
        class Decoder:
            def __init__(self, **options):
                self.inner = native(**options)
                self.options = options
            def stream_reader(self, source, **options):
                self.reader = self.inner.stream_reader(source, **options)
                return self
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.reader.close()
            def read(self, size):
                requests.append(size)
                return self.reader.read(size)
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", frame), ("media", raw_frame())])
        with patch.object(zstandard, "ZstdDecompressor", Decoder), \
                patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", 100002):
            self.refused()
        self.assertEqual(requests, [65536, 34465])

    def test_quiet_truncation_and_partial_tails_are_refused_before_decoder(self):
        frame = zstandard.ZstdCompressor(write_checksum=True).compress(b"generated")
        invalid = [frame[:end] for end in range(len(frame))]
        invalid += [frame + MAGIC[:end] for end in (1, 2, 3)]
        invalid.append(frame + struct.pack("<II", 0x184D2A50, 10) + b"short")
        for value in invalid:
            with self.subTest(length=len(value)):
                self.package([("meta", b"\x08\x03"), ("collection.anki21b", value), ("media", raw_frame())])
                with patch.object(package_bounds, "_decode_zstd", side_effect=AssertionError("Decoded incomplete structure")):
                    self.refused()

    def test_checksum_corruption_and_missing_required_decoder_fail_closed(self):
        frame = bytearray(zstandard.ZstdCompressor(write_checksum=True).compress(b"generated"))
        frame[-1] ^= 1
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", frame), ("media", raw_frame())])
        self.refused()
        self.package()
        with patch.dict("sys.modules", {"zstandard": None}), self.assertRaises(EngineError) as caught:
            package_bounds.validate_package_bounds(self.path)
        self.assertEqual(caught.exception.code, "engine_unavailable")

    def test_metadata_and_media_map_have_independent_limits(self):
        self.package()
        with patch.object(package_bounds, "MAX_META_BYTES", 2):
            package_bounds.validate_package_bounds(self.path)
        with patch.object(package_bounds, "MAX_META_BYTES", 1):
            self.refused()
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", raw_frame(b"db")),
                      ("media", rle_frame([8]))])
        with patch.object(package_bounds, "MAX_MEDIA_MAP_BYTES", 8):
            package_bounds.validate_package_bounds(self.path)
        with patch.object(package_bounds, "MAX_MEDIA_MAP_BYTES", 7):
            self.refused()
        self.package([("collection.anki2", b"db"), ("media", b"{}")])
        with patch.object(package_bounds, "MAX_MEDIA_MAP_BYTES", 1):
            self.refused()

    def test_window_boundary_is_checked_independently_of_output(self):
        for window, admitted in ((0x68, True), (0x69, False)):
            self.package([("meta", b"\x08\x03"), ("collection.anki21b", raw_frame(b"db", unknown=True, window=window)),
                          ("media", raw_frame())])
            if admitted:
                package_bounds.validate_package_bounds(self.path)
            else:
                self.refused()

    def test_frame_and_block_count_limits_bound_structural_work(self):
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", raw_frame(b"a") + raw_frame(b"b")),
                      ("media", raw_frame())])
        with patch.object(package_bounds, "MAX_ZSTD_FRAMES", 2):
            package_bounds.validate_package_bounds(self.path)
        with patch.object(package_bounds, "MAX_ZSTD_FRAMES", 1):
            self.refused()
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", rle_frame([1, 1])),
                      ("media", raw_frame())])
        with patch.object(package_bounds, "MAX_ZSTD_BLOCKS", 2):
            package_bounds.validate_package_bounds(self.path)
        with patch.object(package_bounds, "MAX_ZSTD_BLOCKS", 1):
            self.refused()

    def test_concatenated_and_skippable_frames_and_checksum_are_walked(self):
        skip = struct.pack("<II", 0x184D2A5F, 3) + b"xyz"
        frames = skip + raw_frame(b"ab", checksum=True) + raw_frame(b"cd", unknown=True) + skip
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", frames), ("media", raw_frame())])
        with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", 6):
            package_bounds.validate_package_bounds(self.path)
        with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", 5):
            self.refused()

    def test_content_size_encodings_and_unused_flag_are_supported(self):
        for frame in (raw_frame(b"x" * 256), raw_frame(b"x" * 65792),
                      MAGIC + b"\xe0" + struct.pack("<Q", 1) + b"\x09\0\0x",
                      MAGIC + b"\x30\x01\x09\0\0x",
                      MAGIC + b"\x21\0\x01\x09\0\0x"):
            with self.subTest(frame_bytes=len(frame)):
                self.package([("meta", b"\x08\x03"), ("collection.anki21b", frame), ("media", raw_frame())])
                package_bounds.validate_package_bounds(self.path)

    def test_truncated_reserved_unknown_or_external_dictionary_frames_are_refused(self):
        frames = [b"", b"bad magic", MAGIC, MAGIC + b"\x08\x58",
                  MAGIC + b"\0\x58\x07\0\0",  # reserved block
                  raw_frame(b"db")[:-1], raw_frame(b"db", checksum=True)[:-1],
                  raw_frame(b"db") + b"junk", MAGIC + b"\x01\x58\x01\x01\0\0",
                  MAGIC + b"\0\x58\0\0\x20",  # raw block over 128 KiB
                  struct.pack("<II", 0x184D2A50, 100) + b"short",
                  struct.pack("<II", 0x184D2A50, 0)]
        for frame in frames:
            with self.subTest(frame=frame[:16]):
                self.package([("meta", b"\x08\x03"), ("collection.anki21b", frame), ("media", raw_frame())])
                self.refused()

    def test_zip64_footer_is_admitted_and_multidisk_is_refused(self):
        self.package()
        data = self.path.read_bytes()
        footer = len(data) - 22
        count, size, offset = struct.unpack_from("<HII", data, footer + 10)
        record = struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, offset)
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, footer, 1)
        end = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 65535, 65535, 0xFFFFFFFF, 0xFFFFFFFF, 0)
        self.path.write_bytes(data[:footer] + record + locator + end)
        package_bounds.validate_package_bounds(self.path)
        valid = self.path.read_bytes()
        changed = bytearray(valid)
        struct.pack_into("<HH", changed, len(changed) - 14, 2, 2)
        self.path.write_bytes(changed)
        self.refused()
        changed = bytearray(valid)
        struct.pack_into("<I", changed, footer + len(record) + 4, 1)
        self.path.write_bytes(changed)
        self.refused()

    def test_directory_and_entry_caps_refuse_before_zipfile_allocation(self):
        self.package()
        for name, value in (("MAX_CENTRAL_DIRECTORY_BYTES", 1), ("MAX_PACKAGE_ENTRIES", 2)):
            with self.subTest(name=name), patch.object(package_bounds, name, value), \
                    patch.object(package_bounds.zipfile, "ZipFile", side_effect=AssertionError("Allocated before preflight")):
                self.refused()

    def test_classic_maximum_entry_count_does_not_require_zip64_footer(self):
        self.package()
        data = self.path.read_bytes()
        offset = data.index(b"PK\x01\x02")
        header = bytearray(data[offset:offset + 46])
        struct.pack_into("<3H", header, 28, 1, 0, 0)
        # Test the footer/CD stage without allocating 65,535 ZipInfo objects.
        directory = (bytes(header) + b"0") * 65535
        footer = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 65535, 65535, len(directory), 0, 0)
        stream = io.BytesIO(directory + footer)
        self.assertEqual(package_bounds._directory_preflight(stream, len(directory) + 22, None), 0)

    def test_zip_comment_is_supported_but_trailing_bytes_and_false_count_are_refused(self):
        self.package()
        with zipfile.ZipFile(self.path, "a") as archive:
            archive.comment = b"ordinary ZIP comment"
        package_bounds.validate_package_bounds(self.path)
        data = self.path.read_bytes()
        self.path.write_bytes(data + b"trailing")
        self.refused()
        self.package()
        data = bytearray(self.path.read_bytes())
        struct.pack_into("<HH", data, len(data) - 14, 2, 2)
        self.path.write_bytes(data)
        self.refused()

    def test_invalid_metadata_missing_selected_database_and_unsafe_members_are_refused(self):
        for entries in ([("meta", b"\x08\x04"), ("collection.anki21b", raw_frame()), ("media", raw_frame())],
                        [("meta", b"bad protobuf"), ("collection.anki21b", raw_frame()), ("media", raw_frame())],
                        [("meta", b"\x08\x03"), ("collection.anki2", b"db"), ("media", raw_frame())],
                        [("collection.anki2", b"db")],
                        [("collection.anki2", b"db"), ("media", b"{}"), ("../escape", b"x")],
                        [("collection.anki2", b"db"), ("media", b"{}"), ("\u0661", b"x")],
                        [("collection.anki2", b"db"), ("media", b"{}"), ("media", b"{}")]):
            with self.subTest(names=[name for name, _ in entries]):
                self.package(entries)
                self.refused()

    def test_symlink_encrypted_and_corrupt_zip_members_are_refused(self):
        self.package()
        with zipfile.ZipFile(self.path, "a") as archive:
            entry = zipfile.ZipInfo("0")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, b"target")
        self.refused()
        self.package()
        data = bytearray(self.path.read_bytes())
        directory = data.index(b"PK\x01\x02")
        struct.pack_into("<H", data, directory + 8, 1)
        self.path.write_bytes(data)
        self.refused()

    def test_outer_deflate_cannot_hide_output_past_declared_size(self):
        with zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("collection.anki2", b"db" + b"z" * 65536)
            archive.writestr("media", b"{}")
        data = bytearray(self.path.read_bytes())
        directory = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, 14, zlib.crc32(b"db"))
        struct.pack_into("<I", data, 22, 2)
        struct.pack_into("<I", data, directory + 16, zlib.crc32(b"db"))
        struct.pack_into("<I", data, directory + 24, 2)
        self.path.write_bytes(data)
        # Characterize the upstream trap: its reader returns the forged prefix.
        with zipfile.ZipFile(self.path) as archive:
            self.assertEqual(archive.read("collection.anki2"), b"db")
        self.refused()

    def test_unbounded_outer_codecs_are_refused(self):
        with zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_BZIP2) as archive:
            archive.writestr("collection.anki2", b"db")
            archive.writestr("media", b"{}")
        self.refused()
        self.package([("collection.anki2", b"db"), ("media", b"{}")])
        data = bytearray(self.path.read_bytes())
        data[30 + len("collection.anki2")] ^= 1
        self.path.write_bytes(data)
        self.refused()

    def test_oversize_raw_file_and_symlink_paths_are_refused(self):
        with self.path.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024 * 1024 + 1)
        self.refused()
        self.package()
        link = self.path.with_name("linked.apkg")
        link.symlink_to(self.path)
        self.refused(link)

    def test_cancellation_propagates_during_member_walk(self):
        self.package([("meta", b"\x08\x03"), ("collection.anki21b", rle_frame([100] * 100)),
                      ("media", raw_frame())])
        class Progress:
            calls = 0
            def check_abort(self):
                self.calls += 1
                if self.calls == 50:
                    raise EngineError("aborted", "The operation was aborted.")
            def report(self, **values):
                pass
        with self.assertRaises(EngineError) as caught:
            package_bounds.validate_package_bounds(self.path, SimpleNamespace(progress=Progress()))
        self.assertEqual(caught.exception.code, "aborted")


class NativePackageBoundsTests(unittest.TestCase):
    """Run under the shared heavy lock: real pinned Anki encoders, synthetic data."""
    def test_pinned_latest_legacy_collection_and_backup_exports_are_admitted(self):
        from anki.collection import Collection
        from anki.import_export_pb2 import ExportAnkiPackageOptions
        if package_bounds is None:
            self.fail("Package resource preflight has not been implemented.")
        with tempfile.TemporaryDirectory(prefix="memr-package-bounds-native-") as temp:
            root = Path(temp)
            col = Collection(str(root / "collection.anki2"))
            try:
                note = col.new_note(col.models.by_name("Basic"))
                note["Front"] = '<img src="generated.txt"> synthetic'
                col.add_note(note, 1)
                Path(col.media.dir(), "generated.txt").write_bytes(b"z" * (8 * 1024 * 1024))
                for legacy in (False, True):
                    path = root / f"generated-{legacy}.apkg"
                    col.export_anki_package(out_path=str(path), options=ExportAnkiPackageOptions(
                        with_scheduling=True, with_media=True, with_deck_configs=True, legacy=legacy), limit=None)
                    self.assertGreater(package_bounds.validate_package_bounds(path), 8 * 1024 * 1024)
                    path = root / f"generated-{legacy}.colpkg"
                    col.export_collection_package(str(path), include_media=True, legacy=legacy)
                    col.reopen()
                    self.assertGreater(package_bounds.validate_package_bounds(path), 8 * 1024 * 1024)
                folder = root / "backups"
                folder.mkdir()
                col.create_backup(backup_folder=str(folder), force=True, wait_for_completion=True)
                backups = list(folder.glob("*.colpkg"))
                self.assertTrue(backups)
                for path in backups:
                    package_bounds.validate_package_bounds(path)
            finally:
                col.close()

    def test_valid_tiny_media_below_actual_limit_remains_compatible(self):
        from anki.collection import Collection
        from anki.import_export_pb2 import ExportAnkiPackageOptions
        if package_bounds is None:
            self.fail("Package resource preflight has not been implemented.")
        with tempfile.TemporaryDirectory(prefix="memr-package-bounds-native-") as temp:
            root = Path(temp)
            col = Collection(str(root / "collection.anki2"))
            try:
                note = col.new_note(col.models.by_name("Basic"))
                note["Front"] = "".join(f'<img src="{index}.bin">' for index in range(10))
                col.add_note(note, 1)
                for index in range(10):
                    Path(col.media.dir(), f"{index}.bin").write_bytes(b"z" * 1000)
                path = root / "tiny-media.apkg"
                col.export_anki_package(out_path=str(path), options=ExportAnkiPackageOptions(
                    with_scheduling=True, with_media=True, with_deck_configs=True, legacy=False), limit=None)
                self.assertLess(path.stat().st_size, 100000)
                self.assertLess(Path(col.path).stat().st_size + 10000 + 51200, 1048576)
                actual = package_bounds.validate_package_bounds(path)
                self.assertLess(actual, 1048576)
                with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", actual):
                    self.assertEqual(package_bounds.validate_package_bounds(path), actual)
                with patch.object(package_bounds, "MAX_EXPANDED_PACKAGE_BYTES", actual - 1), self.assertRaises(EngineError) as caught:
                    package_bounds.validate_package_bounds(path)
                self.assertEqual(caught.exception.code, "invalid_params")
            finally:
                col.close()
