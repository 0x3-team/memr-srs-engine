"""Bounded ZIP/zstd admission before Anki restores a package.

Walk complete frame structure before bounded decoding: the pinned decoder can
return quiet EOF for truncated frames and skippable tails. Count actual output
to preserve small-media compatibility without trusting advertised content sizes.
Anki still validates collection and media-map semantics. Primary format:
https://github.com/facebook/zstd/blob/dev/doc/zstd_compression_format.md
"""
import os
from pathlib import Path
import stat
import struct
import zipfile
import zlib

from anki.import_export_pb2 import PackageMetadata
from google.protobuf.message import DecodeError

from ..errors import EngineError
from ..paths import MAX_UPLOAD_BYTES

MAX_EXPANDED_PACKAGE_BYTES = MAX_UPLOAD_BYTES  # 2 GiB across every member
MAX_PACKAGE_ENTRIES = 100000
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024  # before ZipFile allocates it
MAX_META_BYTES = 64 * 1024  # native reads this protobuf into memory
MAX_MEDIA_MAP_BYTES = 16 * 1024 * 1024  # native decodes this whole map in memory
MAX_ZSTD_WINDOW_BYTES = 8 * 1024 * 1024
MAX_ZSTD_BLOCK_BYTES = 128 * 1024
MAX_ZSTD_FRAMES = 100000
MAX_ZSTD_BLOCKS = 1000000
MAX_ZIP_NAME_BYTES = 20  # reserved names or an unsigned 64-bit decimal index
CHUNK_BYTES = 64 * 1024
DATABASES = {1: "collection.anki2", 2: "collection.anki21", 3: "collection.anki21b"}
RESERVED_NAMES = {"meta", "media", *DATABASES.values()}
RESERVED_NAME_BYTES = {value.encode("ascii") for value in RESERVED_NAMES}


class _Refused(ValueError):
    """Only these internally chosen messages may reach the RPC client."""


def _require(condition, message="The package format is invalid or unsupported."):
    if not condition:
        raise _Refused(message)


def _checkpoint(engine):
    if engine is not None:
        engine.progress.check_abort()


def _read(stream, size, engine):
    _checkpoint(engine)
    value = stream.read(size)
    _require(len(value) == size, "The package is truncated.")
    return value


def _skip(stream, size, engine):
    while size:
        count = min(CHUNK_BYTES, size)
        _read(stream, count, engine)
        size -= count


def _directory_preflight(stream, file_size, engine):
    """Bound and walk the central directory before Python loads any ZipInfo."""
    _require(file_size >= 22)
    stream.seek(max(0, file_size - 65557))
    tail = stream.read(65557)
    search_end = len(tail)
    while True:
        _checkpoint(engine)
        index = tail.rfind(b"PK\x05\x06", 0, search_end)
        _require(index >= 0)
        if index + 22 <= len(tail) and index + 22 + struct.unpack_from("<H", tail, index + 20)[0] == len(tail):
            break
        search_end = index
    disk, directory_disk, local_count, count, size, offset, _ = struct.unpack_from("<4H2IH", tail, index + 4)
    _require(disk == directory_disk == 0 and local_count == count, "Split ZIP archives are unsupported.")
    footer = file_size - len(tail) + index
    directory_end = footer
    locator = b""
    if footer >= 20:
        stream.seek(footer - 20)
        locator = _read(stream, 20, engine)
    # Exactly 65,535 entries also fit a classic footer; Python emits that form.
    needs_zip64 = size == 0xFFFFFFFF or offset == 0xFFFFFFFF
    if needs_zip64 or locator.startswith(b"PK\x06\x07"):
        classic = count, size, offset
        _require(locator.startswith(b"PK\x06\x07"))
        _, locator_disk, record_offset, disks = struct.unpack("<4sIQI", locator)
        _require(locator_disk == 0 and disks == 1, "Split ZIP archives are unsupported.")
        _require(record_offset + 56 == footer - 20)
        stream.seek(record_offset)
        values = struct.unpack("<4sQ2H2I4Q", _read(stream, 56, engine))
        signature, record_size, _, _, disk, directory_disk, local_count, count, size, offset = values
        # Python's ZipFile also expects the standard fixed-size ZIP64 record.
        _require(signature == b"PK\x06\x06" and record_size == 44)
        _require(disk == directory_disk == 0 and local_count == count, "Split ZIP archives are unsupported.")
        _require(all(old == sentinel or old == new for old, new, sentinel in
                     zip(classic, (count, size, offset), (65535, 0xFFFFFFFF, 0xFFFFFFFF))))
        directory_end = record_offset
    _require(size <= MAX_CENTRAL_DIRECTORY_BYTES, "The package ZIP directory exceeds 16 MiB.")
    _require(count <= MAX_PACKAGE_ENTRIES, "The package has too many ZIP members.")
    _require(offset + size == directory_end)
    stream.seek(offset)
    actual = 0
    while stream.tell() < directory_end:
        header = _read(stream, 46, engine)
        _require(header.startswith(b"PK\x01\x02"))
        name_size, extra_size, comment_size = struct.unpack_from("<3H", header, 28)
        _require(0 < name_size <= MAX_ZIP_NAME_BYTES)
        name = _read(stream, name_size, engine)
        _require(name in RESERVED_NAME_BYTES or name.isdigit())
        _require(stream.tell() + extra_size + comment_size <= directory_end)
        stream.seek(extra_size + comment_size, os.SEEK_CUR)
        actual += 1
        _require(actual <= MAX_PACKAGE_ENTRIES, "The package has too many ZIP members.")
    _require(actual == count and stream.tell() == directory_end)
    return offset


def _verify_outer_member(source, entry, directory_offset, engine):
    """Check actual ZIP output: ZipExtFile silently truncates at file_size.

    Only stored/deflated envelopes are admitted. zlib's max_length bounds every
    output allocation; unknown codecs must never reach ZipExtFile's decoders.
    """
    _require(entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
             "The package uses an unsupported outer ZIP compression method.")
    source.seek(entry.header_offset)
    header = _read(source, 30, engine)
    _require(header.startswith(b"PK\x03\x04"))
    flags, method = struct.unpack_from("<HH", header, 6)
    _require(flags == entry.flag_bits and method == entry.compress_type)
    name_size, extra_size = struct.unpack_from("<HH", header, 26)
    _require(name_size <= MAX_ZIP_NAME_BYTES)
    _require(_read(source, name_size, engine) == entry.filename.encode("ascii"))
    _skip(source, extra_size, engine)
    _require(source.tell() + entry.compress_size <= directory_offset)
    decoder = zlib.decompressobj(-15) if method == zipfile.ZIP_DEFLATED else None
    remaining, total, crc = entry.compress_size, 0, 0

    def count_output(value):
        nonlocal total, crc
        total += len(value)
        _require(total <= entry.file_size, "The package ZIP output exceeds its declared size.")
        crc = zlib.crc32(value, crc)

    while remaining:
        value = _read(source, min(CHUNK_BYTES, remaining), engine)
        remaining -= len(value)
        if decoder is None:
            count_output(value)
        else:
            while value:
                _checkpoint(engine)
                count_output(decoder.decompress(value, CHUNK_BYTES))
                _require(not decoder.unused_data)
                value = decoder.unconsumed_tail
    if decoder is not None:
        while not decoder.eof:
            _checkpoint(engine)
            value = decoder.decompress(b"", CHUNK_BYTES)
            _require(bool(value), "The package ZIP stream is truncated.")
            count_output(value)
    _require(total == entry.file_size and crc == entry.CRC, "The package ZIP size or checksum is invalid.")


def _plain_bound(stream, limit, engine):
    total = 0
    while True:
        _checkpoint(engine)
        value = stream.read(CHUNK_BYTES)
        if not value:
            return total
        total += len(value)
        _require(total <= limit, "The package exceeds an expanded member limit.")


def _zstd_structure(stream, limit, engine):
    """Require complete frames and bounded work before invoking the decoder."""
    total = frames = blocks = regular_frames = 0
    while True:
        _checkpoint(engine)
        magic = stream.read(4)
        if not magic:
            _require(regular_frames > 0)
            return
        _require(len(magic) == 4)
        frames += 1
        _require(frames <= MAX_ZSTD_FRAMES, "The package has too many zstd frames.")
        number = int.from_bytes(magic, "little")
        if 0x184D2A50 <= number <= 0x184D2A5F:
            size = int.from_bytes(_read(stream, 4, engine), "little")
            _require(size <= MAX_UPLOAD_BYTES)
            _skip(stream, size, engine)
            continue
        _require(number == 0xFD2FB528)
        regular_frames += 1
        descriptor = _read(stream, 1, engine)[0]
        _require(not descriptor & 8)  # reserved bit; bit 4 is explicitly unused
        single = bool(descriptor & 32)
        window = None
        if not single:
            value = _read(stream, 1, engine)[0]
            base = 1 << (10 + (value >> 3))
            window = base + (base >> 3) * (value & 7)
        dictionary_bytes = (0, 1, 2, 4)[descriptor & 3]
        dictionary = int.from_bytes(_read(stream, dictionary_bytes, engine), "little")
        _require(dictionary == 0, "Packages requiring an external zstd dictionary are unsupported.")
        flag = descriptor >> 6
        size_bytes = (1 if single else 0) if flag == 0 else (2, 4, 8)[flag - 1]
        content_size = int.from_bytes(_read(stream, size_bytes, engine), "little") if size_bytes else None
        if size_bytes == 2:
            content_size += 256
        if single:
            window = content_size
        _require(window <= MAX_ZSTD_WINDOW_BYTES, "The package zstd window exceeds 8 MiB.")
        block_maximum = min(window, MAX_ZSTD_BLOCK_BYTES)
        last = False
        while not last:
            header = int.from_bytes(_read(stream, 3, engine), "little")
            last, kind, size = bool(header & 1), (header >> 1) & 3, header >> 3
            _require(kind != 3 and size <= block_maximum)
            _require(kind != 2 or size > 0)
            blocks += 1
            _require(blocks <= MAX_ZSTD_BLOCKS, "The package has too many zstd blocks.")
            # Raw/RLE output sizes are exact. Compressed block output is counted
            # by the bounded decoder, never its structural maximum or frame FCS.
            total += 0 if kind == 2 else size
            _require(total <= limit, "The package exceeds an expanded member limit.")
            _skip(stream, 1 if kind == 1 else size, engine)
        if descriptor & 4:
            _read(stream, 4, engine)


def _decode_zstd(stream, limit, engine):
    try:
        import zstandard
    except ImportError:
        raise EngineError("engine_unavailable", "The required package decoder is unavailable.") from None
    total = 0
    try:
        decoder = zstandard.ZstdDecompressor(max_window_size=MAX_ZSTD_WINDOW_BYTES)
        with decoder.stream_reader(stream, read_size=CHUNK_BYTES, read_across_frames=True, closefd=False) as reader:
            while True:
                _checkpoint(engine)
                value = reader.read(min(CHUNK_BYTES, limit - total + 1))
                if not value:
                    return total
                total += len(value)
                _require(total <= limit, "The package exceeds an expanded member limit.")
    except zstandard.ZstdError:
        raise _Refused("The package zstd data or checksum is invalid.") from None


def _zstd_bound(stream, limit, engine):
    start = stream.tell()
    _zstd_structure(stream, limit, engine)
    stream.seek(start)
    return _decode_zstd(stream, limit, engine)


def validate_package_bounds(path, engine=None):
    """Refuse unsafe envelopes or excessive actual expansion before native I/O.

    Returns actual expanded bytes across members. This does not prove SQLite
    integrity, media-map semantics, or transactional restore behavior.
    Aborts propagate unchanged; public validation failures are invalid_params.
    """
    try:
        _checkpoint(engine)
        path = Path(path)
        info = path.lstat()
        _require(stat.S_ISREG(info.st_mode), "The package must be a regular file.")
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as source:
            opened = os.fstat(source.fileno())
            _require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino))
            _require(opened.st_size <= MAX_UPLOAD_BYTES, "The package exceeds the upload size limit.")
            directory_offset = _directory_preflight(source, opened.st_size, engine)
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                _require(len(names) == len(set(names)) and len(names) <= MAX_PACKAGE_ENTRIES)
                _require("media" in names and any(name in names for name in DATABASES.values()))
                declared = 0
                for entry in entries:
                    _checkpoint(engine)
                    _require(entry.filename in RESERVED_NAMES or entry.filename.isascii() and entry.filename.isdigit())
                    _require(not entry.flag_bits & 1 and not entry.is_dir()
                             and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG))
                    declared += entry.file_size
                    _require(declared <= MAX_UPLOAD_BYTES, "The package ZIP output exceeds 2 GiB.")
                    if entry.filename == "meta":
                        _require(entry.file_size <= MAX_META_BYTES, "The package metadata exceeds 64 KiB.")
                for entry in entries:
                    _verify_outer_member(source, entry, directory_offset, engine)
                metadata = b""
                if "meta" in names:
                    with archive.open("meta") as member:
                        metadata = member.read(MAX_META_BYTES + 1)
                    _require(len(metadata) <= MAX_META_BYTES, "The package metadata exceeds 64 KiB.")
                    version = PackageMetadata.FromString(metadata).version
                else:
                    version = 2 if "collection.anki21" in names else 1
                _require(version in DATABASES and DATABASES[version] in names)
                total = len(metadata)
                for index, entry in enumerate(entries):
                    _checkpoint(engine)
                    if engine is not None:
                        engine.progress.report(done=index, total=len(entries), label="Checking package safety limits")
                    if entry.filename == "meta":
                        continue
                    limit = MAX_EXPANDED_PACKAGE_BYTES - total
                    if entry.filename == "media":
                        limit = min(limit, MAX_MEDIA_MAP_BYTES)
                    compressed = version == 3 and (entry.filename in (DATABASES[3], "media") or entry.filename.isdigit())
                    with archive.open(entry) as member:
                        total += (_zstd_bound if compressed else _plain_bound)(member, limit, engine)
                    _require(total <= MAX_EXPANDED_PACKAGE_BYTES, "The package expanded output exceeds 2 GiB.")
                _checkpoint(engine)
                return total
    except _Refused as error:
        raise EngineError("invalid_params", str(error)) from None
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, DecodeError, struct.error, zlib.error):
        raise EngineError("invalid_params", "The package format is invalid or unsupported.") from None
