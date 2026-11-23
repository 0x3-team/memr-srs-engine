"""ADR-12 source links shared by note reads, card rendering and provenance RPCs."""
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import EngineError

MAX_SOURCE_BYTES = 2_050_000

def valid_identity(value):
    return (type(value) is str and 0 < len(value) <= 256
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in value))


def parse_source_uri(uri):
    if (type(uri) is not str or len(uri) > 2048
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in uri)
            or re.search(r"%(?![0-9a-fA-F]{2})", uri)):
        return None
    try:
        parts = urlsplit(uri)
        if (parts.scheme != "canonnotes" or parts.netloc or parts.fragment
                or not parts.path.startswith("page/")):
            return None
        page = unquote(parts.path[5:], errors="strict")
        query = parse_qs(parts.query, keep_blank_values=True, errors="strict")
        if set(query) - {"block", "rev"} or any(len(values) != 1 for values in query.values()):
            return None
        block = query.get("block", [None])[0]
        revision = query.get("rev", [None])[0]
        if not valid_identity(page) or (block is not None and not valid_identity(block)):
            return None
        if revision is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", revision):
            return None
    except (ValueError, UnicodeError):
        return None
    return {"pageId": page, "blockId": block, "revision": revision,
            "uri": uri, "changed": None}


def source_links(value):
    if len(str(value).encode("utf-8", errors="surrogatepass")) > MAX_SOURCE_BYTES:
        raise EngineError("unsupported", "The Source field exceeds its bounded lookup size.")
    sources, seen = [], set()
    for line in str(value).splitlines():
        source = parse_source_uri(line.strip())
        if source is None:
            continue
        identity = (source["pageId"], source["blockId"],
                    source["revision"].lower() if source["revision"] else None)
        if identity in seen:
            continue
        seen.add(identity)
        sources.append(source)
        if len(sources) > 1000:
            raise EngineError("unsupported", "A note has more than 1000 source links.")
    return sources


def first_source(note):
    if "Source" not in note:
        return None
    for line in note["Source"].splitlines():
        source = parse_source_uri(line.strip())
        if source is not None:
            return source
    return None
