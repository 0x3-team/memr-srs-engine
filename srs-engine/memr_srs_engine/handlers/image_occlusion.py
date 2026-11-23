"""Translate typed masks to Anki's native, portable image-occlusion clozes."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import xml.etree.ElementTree as ET

from ..errors import EngineError
from ..registry import method
from .decks import _deck, _result
from .notes import _fields, _model, _note, _tags, _text

# The established per-file media limit, matching E05 and Anki media sync.
MAX_IMAGE_BYTES = 100 * 1024 * 1024


def _revision(note):
    value = [int(note.id), int(note.mid), int(note.mod), note.fields, note.tags]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _formatting(mask):
    """Native colors become HTML attributes, so accept only literal color syntax."""
    if "fill" in mask and (len(mask["fill"]) > 255 or not re.fullmatch(
            r"(?:#[a-fA-F0-9]{3,8}|[a-zA-Z]{1,32}|(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\([a-zA-Z0-9.,% +/\-]+\))", mask["fill"])):
        raise EngineError("invalid_params", "Occlusion colors must be literal colors without URLs or markup.")
    for key, low, high in (("angle", -10000, 10000), ("fontSize", .0001, 10), ("scale", .0001, 100)):
        if key in mask and (not math.isfinite(mask[key]) or not low <= mask[key] <= high):
            raise EngineError("invalid_params", "Occlusion formatting is outside the supported range.")
    if mask["kind"] == "text":
        value = mask.get("text")
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "}}" in value or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise EngineError("invalid_params", "Text annotations need visible text without control characters or the closing cloze delimiter }}.")
        _text(value)
    elif any(key in mask for key in ("text", "fontSize", "scale")):
        raise EngineError("invalid_params", "Text formatting belongs only to text annotations.")


def _filename(name):
    # A literal media name: nothing here or below decodes it, so "%" is an ordinary
    # character (Anki names such as "50%.png" are valid) and cannot hide a separator.
    _text(name)
    if (not name or name.startswith(".") or ".." in name or name.endswith((".", " "))
            or any(char in '/\\<>:"|?*\x00' or ord(char) < 32 or ord(char) == 127 for char in name)
            or len(name.encode("utf-8")) > 255
            or re.match(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", name, re.I)):
        raise EngineError("invalid_params", "The image must use a safe media filename.")
    return name


def _media_path(col, name, required=True):
    path = Path(col.media.dir()) / _filename(name)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not required:
            return path
        raise EngineError("not_found", "The requested media image is unavailable.") from None
    if not stat.S_ISREG(info.st_mode):
        raise EngineError("invalid_params", "Media images must be regular files without symbolic links.")
    if info.st_size > MAX_IMAGE_BYTES:
        raise EngineError("invalid_params", "Media images must not exceed 100 MiB.")
    return path


def _image_bytes(col, name):
    path = _media_path(col, name)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES:
                raise EngineError("invalid_params", "The media image changed or exceeds 100 MiB.")
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise EngineError("invalid_params", "Media images must not exceed 100 MiB.")
        return data
    except FileNotFoundError:
        raise EngineError("not_found", "The requested media image is unavailable.") from None
    except OSError:
        raise EngineError("invalid_params", "The media image could not be read safely.") from None


def _dimensions(data):
    """Read bounded image headers without executing SVG or an image decoder."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33 and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
        elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 13:
            width, height = struct.unpack("<HH", data[6:10])
        elif data.startswith(b"\xff\xd8"):
            offset, width, height = 2, 0, 0
            while offset + 4 <= len(data):
                if data[offset] != 255:
                    break
                while offset < len(data) and data[offset] == 255:
                    offset += 1
                marker, offset = data[offset], offset + 1
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    continue
                length = int.from_bytes(data[offset:offset + 2], "big")
                if length < 2 or offset + length > len(data):
                    break
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                    break
                offset += length
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X" and len(data) >= 30:
                width, height = 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            elif kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
                bits = int.from_bytes(data[21:25], "little")
                width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            elif kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
                width, height = [value & 0x3FFF for value in struct.unpack("<HH", data[26:30])]
            else:
                raise ValueError
        elif data[:4] == b"\x00\x00\x01\x00" and len(data) >= 22 and int.from_bytes(data[4:6], "little"):
            width, height = data[6] or 256, data[7] or 256
        elif b"<svg" in data[:1024] or b":svg" in data[:1024]:
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise ValueError
            root = ET.fromstring(data)
            if root.tag.rsplit("}", 1)[-1] != "svg":
                raise ValueError
            def dimension(value):
                match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)(?:px)?\s*", value or "")
                return float(match[1]) if match else 0
            width, height = dimension(root.get("width")), dimension(root.get("height"))
            if not width or not height:
                view = [float(part) for part in re.split(r"[\s,]+", root.get("viewBox", "").strip())]
                width, height = view[2:4]
            width, height = math.ceil(width), math.ceil(height)
        elif len(data) >= 16 and data[4:8] == b"ftyp" and b"avif" in data[8:min(len(data), 64)]:
            # AVIF spatial extents are properties inside meta/iprp/ipco. Reject
            # ambiguous images with different property sizes instead of guessing.
            extents = []
            def boxes(start, end, depth=0):
                if depth > 16:
                    raise ValueError
                while start + 8 <= end:
                    size, kind = int.from_bytes(data[start:start + 4], "big"), data[start + 4:start + 8]
                    header = 8
                    if size == 1:
                        size, header = int.from_bytes(data[start + 8:start + 16], "big"), 16
                    if size == 0:
                        size = end - start
                    if size < header or start + size > end:
                        raise ValueError
                    payload = start + header
                    if kind == b"ispe" and size >= header + 12:
                        extents.append(struct.unpack(">II", data[payload + 4:payload + 12]))
                    elif kind in (b"meta", b"iprp", b"ipco"):
                        boxes(payload + (4 if kind == b"meta" else 0), start + size, depth + 1)
                    start += size
            boxes(0, len(data))
            if not extents or len(set(extents)) != 1:
                raise EngineError("unsupported", "This AVIF has ambiguous spatial properties.")
            width, height = extents[0]
        else:
            raise ValueError
        if not 1 <= width <= 100000 or not 1 <= height <= 100000:
            raise ValueError
        return int(width), int(height)
    except (ValueError, IndexError, struct.error, ET.ParseError, OverflowError):
        raise EngineError("invalid_params", "The media file has no supported image dimensions.") from None


def _io_note(col, note_id):
    note = _note(col, note_id)
    model = note.note_type()
    if model.get("originalStockKind") != 6:
        raise EngineError("invalid_params", "The requested note is not an image-occlusion note.")
    indexes = col._backend.get_image_occlusion_fields(model["id"])
    match = re.search(r'<img\s+[^>]*src\s*=\s*"([^"]+)"', note.fields[indexes.image])
    if not match:
        raise EngineError("invalid_params", "The occlusion note has no safe media image reference.")
    filename = _filename(match[1])
    _media_path(col, filename, required=False)
    native = col.get_image_occlusion_note(int(note_id))
    if not native.HasField("note"):
        raise EngineError("invalid_params", "The engine could not read this image-occlusion note.")
    if (len(native.note.header) > 1048576 or len(native.note.back_extra) > 1048576
            or len(native.note.tags) > 1000 or any(len(tag) > 1024 for tag in native.note.tags)):
        raise EngineError("unsupported", "The native occlusion note exceeds the supported text size.")
    return note, indexes, filename, native.note


def _encoded(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=") or "-"


def _decoded(value):
    return "" if value == "-" else base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _masks(native):
    masks = []
    identities = set()
    for occlusion in sorted(native.occlusions, key=lambda group: group.ordinal):
        if occlusion.ordinal > 500:
            raise EngineError("unsupported", "This note contains native cloze numbers beyond the supported card range.")
        for index, shape in enumerate(occlusion.shapes):
            props = {p.name: p.value for p in shape.properties}
            kind = {"rect": "rectangle", "ellipse": "ellipse", "polygon": "polygon", "text": "text"}.get(shape.shape)
            if kind is None:
                raise EngineError("unsupported", "This note contains unsupported native occlusion shapes.")
            allowed = {"left", "top", "width", "height", "angle", "fill", "oi", "cnid", "cngroup", "cnlabel", "cnorder"}
            allowed.update({"ellipse": {"rx", "ry"}, "polygon": {"points"}, "text": {"text", "fs", "scale"}}.get(kind, set()))
            if len(props) != len(shape.properties) or set(props) - allowed:
                raise EngineError("unsupported", "This note contains duplicate or unrecognized native properties. Its original data was retained.")
            try:
                raw_points = [point.split(",") for point in props.get("points", "").split()]
                if any(len(point) != 2 for point in raw_points):
                    raise ValueError
                points = [dict(zip(("x", "y"), map(float, point))) for point in raw_points]
                left, top = float(props.get("left", 0)), float(props.get("top", 0))
                width = float(props.get("width", float(props.get("rx", 0)) * 2))
                height = float(props.get("height", float(props.get("ry", 0)) * 2))
                if kind == "ellipse" and any(key in props and not math.isclose(float(props[key]) * 2, size, abs_tol=1e-12)
                                             for key, size in (("rx", width), ("ry", height))):
                    raise ValueError
                if kind == "polygon" and not width and points:
                    width = max(p["x"] for p in points) - min(p["x"] for p in points)
                    height = max(p["y"] for p in points) - min(p["y"] for p in points)
                fallback_group = f"group-{occlusion.ordinal}" if len(occlusion.shapes) > 1 and occlusion.ordinal else None
                mask = {"id": _decoded(props["cnid"]) if "cnid" in props else f"mask-{occlusion.ordinal}-{index}",
                        "kind": kind, "left": left, "top": top, "width": width, "height": height,
                        "points": points, "group": (_decoded(props["cngroup"]) or None) if "cngroup" in props else fallback_group,
                        "label": _decoded(props["cnlabel"]) if "cnlabel" in props else "", "inactive": occlusion.ordinal == 0}
                for native_key, key in (("fs", "fontSize"), ("scale", "scale"), ("angle", "angle")):
                    if native_key in props:
                        mask[key] = float(props[native_key])
                for key in ("text", "fill"):
                    if key in props:
                        mask[key] = props[key]
                _formatting(mask)
                if (props.get("oi", "0") not in ("0", "1")
                        or kind != "text" and (props.get("oi", "0") == "1") != native.occlude_inactive):
                    raise ValueError
                numbers = [left, top, width, height, *(coordinate for point in points for coordinate in point.values())]
                if (any(not math.isfinite(number) or not 0 <= number <= 1 for number in numbers)
                        or len(points) > 10000 or not mask["id"] or len(mask["id"]) > 255
                        or len(mask["label"]) > 4096 or (mask["group"] is not None and len(mask["group"]) > 255)
                        or len(masks) >= 10000 or mask["id"] in identities):
                    raise ValueError
                identities.add(mask["id"])
                masks.append((int(props.get("cnorder", len(masks))), occlusion.ordinal, mask))
            except (ValueError, UnicodeError, KeyError, OverflowError, EngineError):
                raise EngineError("unsupported", "This note contains malformed native occlusion data.") from None
    return sorted(masks, key=lambda entry: entry[0])


def _validated(occlusion):
    _filename(occlusion["image"])
    _fields({"flds": [None, None], "type": 0}, [occlusion["header"], occlusion["backExtra"]])
    _tags(occlusion["tags"])
    ids, active = set(), False
    for mask in occlusion["masks"]:
        for value in (mask["id"], mask["group"], mask["label"]):
            if value is not None:
                _text(value)
        if not mask["id"] or mask["id"] in ids or mask["group"] == "":
            raise EngineError("invalid_params", "Mask identities must be nonempty and unique.")
        ids.add(mask["id"])
        active |= not mask["inactive"]
        _formatting(mask)
        if mask["kind"] != "text" and (mask["width"] <= 0 or mask["height"] <= 0 or mask["left"] + mask["width"] > 1 + 1e-12
                or mask["top"] + mask["height"] > 1 + 1e-12):
            raise EngineError("invalid_params", "Masks must have nonzero dimensions inside the image.")
        points = mask["points"]
        if mask["kind"] != "polygon" and points:
            raise EngineError("invalid_params", "Only polygon masks may contain points.")
        if mask["kind"] == "polygon":
            if len(points) < 3 or len({(p["x"], p["y"]) for p in points}) < 3:
                raise EngineError("invalid_params", "Polygon masks require at least three distinct points.")
            area = sum(p["x"] * points[(i + 1) % len(points)]["y"] - p["y"] * points[(i + 1) % len(points)]["x"]
                       for i, p in enumerate(points))
            if not area:
                raise EngineError("invalid_params", "Polygon masks must have a nonzero area.")
            if (not math.isclose(max(p["x"] for p in points) - min(p["x"] for p in points), mask["width"], abs_tol=1e-12)
                    or not math.isclose(max(p["y"] for p in points) - min(p["y"] for p in points), mask["height"], abs_tol=1e-12)):
                raise EngineError("invalid_params", "Polygon dimensions must match its points.")
    if not active:
        raise EngineError("invalid_params", "An occlusion note requires at least one active mask.")
    if len({_key(mask) for mask in occlusion["masks"] if not mask["inactive"]}) > 500:
        raise EngineError("invalid_params", "An occlusion note supports at most 500 active mask groups.")
    return occlusion


def _key(mask):
    return ("group", mask["group"]) if mask["group"] is not None else ("mask", mask["id"])


def _clozes(occlusion, previous=(), reserved=()):
    prior = {_key(mask): ordinal for _, ordinal, mask in previous if ordinal}
    keys = list(dict.fromkeys(_key(mask) for mask in occlusion["masks"] if not mask["inactive"]))
    ordinals = {key: prior[key] for key in keys if key in prior}
    # Native updates retain empty cards. Reusing a removed ordinal would attach
    # that old card's scheduling history to an unrelated newly drawn mask.
    used = {ordinal for _, ordinal, _ in previous if ordinal} | set(reserved)
    next_ordinal = 1
    for key in keys:
        if key not in ordinals:
            while next_ordinal in used:
                next_ordinal += 1
            if next_ordinal > 500:
                raise EngineError("unsupported", "The note has exhausted its native mask ordinal space.")
            ordinals[key] = next_ordinal
            used.add(next_ordinal)
    result = []
    for index, mask in enumerate(occlusion["masks"]):
        ordinal = 0 if mask["inactive"] else ordinals[_key(mask)]
        shape = {"rectangle": "rect", "ellipse": "ellipse", "polygon": "polygon", "text": "text"}[mask["kind"]]
        props = {key: str(mask[key]) for key in ("left", "top", "width", "height")}
        if shape == "ellipse":
            props.update(rx=str(mask["width"] / 2), ry=str(mask["height"] / 2))
        if shape == "polygon":
            props["points"] = " ".join(str(p["x"]) + "," + str(p["y"]) for p in mask["points"])
        for key, native_key in (("text", "text"), ("fontSize", "fs"), ("scale", "scale"), ("angle", "angle"), ("fill", "fill")):
            if key in mask:
                props[native_key] = str(mask[key])
        # Unknown native properties are retained by Anki's parser but never
        # rendered as attributes. They keep editor identities through export.
        props.update(cnid=_encoded(mask["id"]), cngroup=_encoded(mask["group"] or ""),
                     cnlabel=_encoded(mask["label"]), cnorder=str(index))
        if occlusion["mode"] == "hideAllGuessOne":
            props["oi"] = "1"
        result.append("{{c" + str(ordinal) + "::image-occlusion:" + shape
                      + "".join(":" + key + "=" + value.replace("\\", "\\\\").replace(":", "\\:") for key, value in props.items()) + "}}<br>")
    serialized = "".join(result)
    if len(serialized) > 1048576:
        raise EngineError("invalid_params", "The serialized masks exceed the note-field size limit.")
    return serialized


@method("imageOcclusion.getImage")
def get_image(engine, params):
    name = _filename(params["filename"])
    data = _image_bytes(engine.col, name)
    # Native reads a protected snapshot; no caller path reaches the backend.
    with tempfile.TemporaryDirectory(prefix="memr-occlusion-image-") as directory:
        path = Path(directory) / name
        path.write_bytes(data)
        native = engine.col.get_image_for_occlusion(str(path))
    width, height = _dimensions(native.data)
    return {"filename": name, "width": width, "height": height}


@method("imageOcclusion.addNote")
def add_note(engine, params):
    col = engine.col
    model = _model(col, params["notetypeId"])
    if model.get("originalStockKind") != 6:
        raise EngineError("invalid_params", "Choose an image-occlusion note type.")
    deck = _deck(col, int(params["deckId"]))
    if deck["dyn"]:
        raise EngineError("invalid_params", "Notes must be added to a normal deck.")
    occlusion = _validated(params["occlusion"])
    data = _image_bytes(col, occlusion["image"])
    _dimensions(data)
    clozes = _clozes(occlusion)
    before = col.db.scalar("select max(id) from notes") or 0
    current = col.decks.selected()
    target = col.add_custom_undo_entry("Add Image Occlusion")
    with tempfile.TemporaryDirectory(prefix="memr-occlusion-add-") as directory:
        path = Path(directory) / occlusion["image"]
        path.write_bytes(data)
        try:
            if current != params["deckId"]:
                col.decks.set_current(int(params["deckId"]))
            col.add_image_occlusion_note(int(model["id"]), str(path), clozes,
                                         occlusion["header"], occlusion["backExtra"], _tags(occlusion["tags"]))
        finally:
            if col.decks.selected() != current:
                col.decks.set_current(current)
    note_id = col.db.scalar("select id from notes where id>? and mid=? order by id desc limit 1", before, model["id"])
    if note_id is None:
        raise EngineError("internal", "The engine did not create the occlusion note.")
    return _result(col, col.merge_undo_entries(target), noteId=note_id, cardIds=list(col.card_ids_of_note(note_id)))


@method("imageOcclusion.getNote")
def get_note(engine, params):
    note, _indexes, filename, native = _io_note(engine.col, params["noteId"])
    occlusion = {"image": filename, "masks": [mask for _, _, mask in _masks(native)],
                 "mode": "hideAllGuessOne" if native.occlude_inactive else "hideOneGuessOne",
                 "header": native.header, "backExtra": native.back_extra, "tags": list(native.tags)}
    return {"noteId": int(note.id), "notetypeId": int(note.mid), "occlusion": occlusion, "revision": _revision(note)}


@method("imageOcclusion.updateNote")
def update_note(engine, params):
    col = engine.col
    note, indexes, old_image, native = _io_note(col, params["noteId"])
    if "expectedRevision" in params:
        if not re.fullmatch(r"[a-f0-9]{64}", params["expectedRevision"]):
            raise EngineError("invalid_params", "Use the note revision returned by imageOcclusion.getNote.")
        if params["expectedRevision"] != _revision(note):
            raise EngineError("conflict", "The image-occlusion note changed. Reload its saved version before overwriting it.")
    occlusion = _validated(params["occlusion"])
    _dimensions(_image_bytes(col, occlusion["image"]))
    clozes = _clozes(occlusion, _masks(native), [card.ord + 1 for card in note.cards()])
    target = col.add_custom_undo_entry("Update Image Occlusion")
    col.update_image_occlusion_note(int(note.id), clozes, occlusion["header"], occlusion["backExtra"], _tags(occlusion["tags"]))
    if old_image != occlusion["image"]:
        note = col.get_note(note.id)
        note.fields[indexes.image] = '<img src="' + occlusion["image"] + '">'
        col.update_note(note)
    return _result(col, col.merge_undo_entries(target), noteId=int(note.id), cardIds=list(note.card_ids()))
