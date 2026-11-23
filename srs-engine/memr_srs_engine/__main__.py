"""Process lifetime and diagnostic output isolation."""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys


def main():
    parser = argparse.ArgumentParser(description="Memr SRS engine")
    parser.add_argument("--collection", type=Path)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    if not args.describe and args.collection is None:
        parser.error("--collection is required unless --describe is used")

    # Keep a private protocol descriptor; even native/library stdout diagnostics
    # go to the log. The host captures stderr when no explicit log is supplied.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    log_stream = sys.stderr
    if args.log is not None:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(args.log, 0o600)
        log_stream = os.fdopen(fd, "a", encoding="utf-8")
    sys.stdout.flush()
    os.dup2(log_stream.fileno(), sys.stdout.fileno())
    sys.stdout = log_stream
    logging.basicConfig(stream=log_stream, level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    logger = logging.getLogger("memr.srs")
    owner = None
    engine = None
    try:
        from . import handlers, warmup  # fixed area imports register handlers and hooks
        from .collection import CollectionOwner
        from .protocol import Engine, FrameWriter
        from .registry import Registry

        registry = Registry()
        writer = FrameWriter(output)
        if args.describe:
            writer.write(registry.describe())
            return 0
        owner = CollectionOwner(args.collection)
        col = owner.open()
        engine = Engine(col, registry, writer, logger)
        # Signal handlers do no collection work; run() joins the background
        # operation before the outer finally releases Anki's collection lock.
        signal.signal(signal.SIGTERM, lambda _signum, _frame: engine.stop.set())
        signal.signal(signal.SIGINT, lambda _signum, _frame: engine.stop.set())
        # FileIO has no buffered-reader mutex held by the daemon input thread
        # when SIGTERM/shutdown ends the process with stdin still open.
        engine.run(sys.stdin.buffer.raw)
        return 0
    except Exception as exception:
        logger.error("Engine failed")
        # Startup has no request id. Return a typed diagnostic and exit nonzero.
        from .errors import public_error
        data = json.dumps({"id": None, "error": public_error(exception).as_dict()}, allow_nan=False)
        try:
            output.write((data + "\n").encode("utf-8"))
        except BrokenPipeError:
            pass
        return 1
    finally:
        if owner is not None:
            owner.close()
        if engine is not None:
            # owner.close() released SQLite's record locks; releasing the
            # warm-up descriptors here keeps ordering identical to the
            # engine.shutdown path (process exit would release them anyway).
            warmup_state = getattr(engine, "_collection_warmup", None)
            if warmup_state is not None:
                warmup_state.close()
        output.close()
        logging.shutdown()
        if args.log is not None:
            log_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
