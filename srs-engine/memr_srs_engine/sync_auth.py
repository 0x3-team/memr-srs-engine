"""Sync identity validation and private, atomically replaced credentials."""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import EngineError

LIMIT = 16384


def valid_hkey(value):
    return (type(value) is str and 0 < len(value) <= 4096
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def sync_endpoint(value):
    if (type(value) is not str or not value or len(value) > 2048 or "\\" in value
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
            or re.search(r"%(?![0-9a-fA-F]{2})", value)):
        raise EngineError("invalid_params", "Use a valid sync endpoint URL.")
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https"):
            raise EngineError("unsupported", "Sync requires HTTPS, or HTTP on loopback.")
        host = (url.hostname or "").encode("idna").decode("ascii").lower().removesuffix(".")
        port = url.port
        path = unquote(url.path, errors="strict")
        if (not host or "%" in host or host.endswith(".") or url.username is not None
                or url.password is not None or url.query or url.fragment
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char == "\\" for char in path)):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise EngineError("invalid_params", "Use a valid sync endpoint URL.") from None
    if any(host == domain or host.endswith("." + domain) for domain in ("ankiweb.net", "ankiuser.net")):
        raise EngineError("unsupported", "AnkiWeb synchronization is not supported.")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if url.scheme != "https" and not (url.scheme == "http" and loopback):
        raise EngineError("unsupported", "Sync requires HTTPS, or HTTP on loopback.")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    path = url.path or "/"
    return urlunsplit((url.scheme, authority, path if path.endswith("/") else path + "/", "", ""))


def auth_path(engine):
    return Path(engine.col.path).parent / "sync-auth.json"


def _private(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o077 and info.st_size <= LIMIT


def _exists(path):
    try:
        if not _private(path.lstat()):
            raise EngineError("conflict", "Saved sync credentials require a private regular file.")
        return True
    except FileNotFoundError:
        return False


def read_auth(engine):
    path = auth_path(engine)
    if not _exists(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not _private(os.fstat(stream.fileno())):
                raise ValueError()
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError()
        value = json.loads(data)
        if (type(value) is not dict or set(value) != {"version", "endpoint", "username", "hkey", "required", "lastSyncAt"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["username"]) is not str or not 0 < len(value["username"]) <= 255
                or not valid_hkey(value["hkey"])
                or value["required"] not in {"none", "normal", "full", "upload", "download"}
                or value["lastSyncAt"] is not None and (type(value["lastSyncAt"]) is not int or not 0 <= value["lastSyncAt"] <= 9007199254740991)
                or sync_endpoint(value["endpoint"]) != value["endpoint"]):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise EngineError("conflict", "Saved sync credentials are invalid.") from None


def write_auth(engine, value):
    path = auth_path(engine)
    _exists(path)
    fd, temporary = tempfile.mkstemp(prefix=".sync-auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        _exists(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remove_auth(engine):
    path = auth_path(engine)
    if _exists(path):
        path.unlink()
