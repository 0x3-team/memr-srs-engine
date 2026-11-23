"""Native collection/media synchronization with explicit full-sync choices."""
import time
from anki import sync_pb2

from ..collection import mutation_result, undo_state
from ..errors import EngineError
from ..paths import checkpoint, mutation
from ..registry import method
from ..sync_auth import read_auth, remove_auth, sync_endpoint, valid_hkey, write_auth
from ..sync_transport import native_call, native_sync


def _signed_in(engine):
    saved = read_auth(engine)
    if saved is None:
        raise EngineError("not_found", "Sign in to a sync server first.")
    return saved


def _auth(saved, bridge):
    return sync_pb2.SyncAuth(hkey=saved["hkey"], endpoint=bridge.endpoint, io_timeout_secs=30)


def _completed(engine, summary, changed=False, required=None):
    # The terminal contract's optional syncRequired describes full-sync choices.
    # Login/media cannot declare the collection synchronized; use sync.status.
    extra = {"summary": summary}
    if required is not None:
        extra["syncRequired"] = required
    result = mutation(engine.col, **extra)
    if changed:
        # Sync changes native data outside the normal undoable operations.
        # Consumers must refresh every projection from the reopened collection.
        result["changes"] = dict.fromkeys(result["changes"], True)
    return result


@method("sync.status")
def status(engine, _params):
    saved = read_auth(engine)
    col = engine.col
    modified, schema, last_sync = col.db.first("select mod, scm, ls from col")
    required = saved["required"] if saved else "none"
    if saved and required in ("none", "normal") and last_sync:
        # Match pinned Anki's offline timestamp comparison. Its public
        # sync_status() may contact the server when local state is clean.
        if schema > last_sync:
            required = "full"
        elif modified > last_sync:
            required = "normal"
    return {"loggedIn": saved is not None, "endpoint": saved["endpoint"] if saved else None,
            "username": saved["username"] if saved else None, "lastSyncAt": saved["lastSyncAt"] if saved else None,
            "required": required,
            "local": {"notes": col.db.scalar("select count(*) from notes"),
                      "cards": col.db.scalar("select count(*) from cards"),
                      "modifiedAt": max(0, int(modified) // 1000)},
            # These counts are not supplied by the native sync protocol.
            "remote": None, "mediaPending": None}


@method("sync.login")
def login(engine, params):
    endpoint = sync_endpoint(params["endpoint"])
    username, password = params["username"], params["password"]
    if (not username.strip() or not password
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in username)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in username + password)):
        raise EngineError("invalid_params", "Enter a valid username and password.")
    with native_sync(engine, endpoint) as bridge:
        auth = native_call(engine, lambda: engine.col.sync_login(username, password, bridge.endpoint))
        checkpoint(engine)
        if not valid_hkey(auth.hkey):
            raise EngineError("internal", "The server returned invalid sync credentials.")
        write_auth(engine, {"version": 1, "endpoint": endpoint, "username": username, "hkey": auth.hkey,
                            "required": "normal", "lastSyncAt": None})
    return _completed(engine, "Signed in to the selected sync server.")


@method("sync.logout")
def logout(engine, _params):
    remove_auth(engine)
    return mutation(engine.col)


@method("sync.run")
def run(engine, _params):
    saved = _signed_in(engine)
    with native_sync(engine, saved["endpoint"]) as bridge:
        output = native_call(engine, lambda: engine.col.sync_collection(_auth(saved, bridge), sync_media=False))
        checkpoint(engine)
        if output.new_endpoint:
            raise EngineError("unsupported", "Choose a changed sync endpoint explicitly.")
        required = {sync_pb2.SyncCollectionResponse.NO_CHANGES: "none", sync_pb2.SyncCollectionResponse.NORMAL_SYNC: "none",
                    sync_pb2.SyncCollectionResponse.FULL_SYNC: "full", sync_pb2.SyncCollectionResponse.FULL_UPLOAD: "upload",
                    sync_pb2.SyncCollectionResponse.FULL_DOWNLOAD: "download"}.get(output.required)
        if required is None:
            raise EngineError("unsupported", "The server requested an unsupported sync action.")
        saved["required"] = required
        if required == "none":
            saved["lastSyncAt"] = int(time.time())
        write_auth(engine, saved)
    if required != "none":
        local = status(engine, {})["local"]
        choice = "upload or download" if required == "full" else required
        raise EngineError("conflict", f"Full sync requires an explicit {choice} choice. "
                          f"This collection has {local['notes']} notes and {local['cards']} cards. Remote totals are unavailable.",
                          {"method": "sync.resolveFullSync"})
    return _completed(engine, "Collection sync completed.", changed=output.required != sync_pb2.SyncCollectionResponse.NO_CHANGES, required="none")


@method("sync.resolveFullSync")
def resolve_full_sync(engine, params):
    saved = _signed_in(engine)
    direction = params["direction"]
    if saved["required"] not in ("full", direction):
        raise EngineError("conflict", "Run sync first and choose a direction allowed by the current conflict.")
    if direction == "download":
        from .maintenance import force_backup
        force_backup(engine)
    with native_sync(engine, saved["endpoint"]) as bridge:
        native_call(engine, lambda: engine.col.full_upload_or_download(auth=_auth(saved, bridge), server_usn=None,
                                                                      upload=direction == "upload"))
        if direction == "upload":
            # Anki 26.9.3 sets ls=scm before its upload transaction stamps mod
            # with a second clock read. All of mod was uploaded; align the local
            # watermark as native full download does, without changing mod.
            # Never move ls below scm: a wall-clock step back between the two
            # reads leaves mod < scm, and ls=mod would then report "full".
            engine.col.db.execute("update col set ls=max(mod, ls)")
        checkpoint(engine)
        saved.update(required="none", lastSyncAt=int(time.time()))
        write_auth(engine, saved)
    return _completed(engine, f"Full {direction} completed.", changed=True, required="none")


@method("sync.media")
def media(engine, _params):
    saved = _signed_in(engine)
    with native_sync(engine, saved["endpoint"]) as bridge:
        native_call(engine, lambda: engine.col.sync_media(_auth(saved, bridge)))
        try:
            while native_call(engine, engine.col.media_sync_status).active:
                checkpoint(engine)
                time.sleep(0.025)
            checkpoint(engine)
        finally:
            # Media work outlives its starting call. Reap it before closing the
            # bridge or allowing the collection owner to handle another request.
            engine.col.abort_media_sync()
            while True:
                try:
                    if not engine.col.media_sync_status().active:
                        break
                except Exception:
                    break  # An ended task reports its native error once.
                time.sleep(0.025)
    return _completed(engine, "Media sync completed.")


@method("sync.abort")
def abort(engine, _params):
    current = engine.progress.get()
    if current["status"] == "running":
        if current["kind"] in ("sync.login", "sync.run", "sync.resolveFullSync", "sync.media"):
            return engine.progress.abort(current["operationId"])
        return mutation_result(engine.progress.undo)
    return mutation_result(undo_state(engine.col))
