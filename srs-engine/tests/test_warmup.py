"""Collection open schedules a paced warm-up; engine stop cancels it; writes proceed."""
import errno
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from anki.collection import Collection

from memr_srs_engine import handlers, warmup
from memr_srs_engine.handlers import maintenance
from memr_srs_engine.protocol import Engine, FrameWriter
from memr_srs_engine.registry import Registry
from tests.helpers import temp_collection


def build_engine(col):
    output = io.BytesIO()
    logger = logging.getLogger("warmup-tests")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return Engine(col, Registry(), FrameWriter(output), logger), output


def descriptors_pinning(path, identity):
    """Open descriptors still pinning a replaced file's `(st_dev, st_ino)`.

    st_ino alone cannot name a descriptor's target: once the replaced
    file's last descriptor closes, the filesystem may hand the freed number
    to a new, live file — or the number may collide with an inode on a
    different device entirely (pipes, sockets, anon inodes). A descriptor
    counts only when both halves of the identity match AND its
    /proc/self/fd link still resolves to the replaced path, marked deleted.
    procfs reports the resolved dentry path, so the comparison uses the
    canonical path, not a possibly symlinked logical one.
    """
    deleted = f"{os.path.realpath(path)} (deleted)"
    pinned = []
    for entry in os.listdir("/proc/self/fd"):
        try:
            stat = os.fstat(int(entry))
            if (stat.st_dev, stat.st_ino) != identity:
                continue
            if os.readlink(Path("/proc/self/fd") / entry) == deleted:
                pinned.append(entry)
        except (OSError, ValueError):
            pass
    return pinned


class CollectionWarmupTests(unittest.TestCase):
    def test_open_schedules_warmup_and_reads_the_collection_file(self):
        with temp_collection() as col:
            engine, _output = build_engine(col)
            try:
                state = engine._collection_warmup
                self.assertIsInstance(state, warmup.CollectionWarmup)
                self.assertTrue((state.thread and state.thread.is_alive()) or state.done.is_set())
                self.assertTrue(state.done.wait(10), "warm-up did not finish its pass")
                self.assertGreater(state.bytes_read, 0)
                # The pass reads the whole collection file, including revlog
                # pages; the engine may checkpoint it smaller afterwards.
                self.assertGreaterEqual(state.bytes_read, os.path.getsize(col.path))
            finally:
                engine.progress.close()

    def test_warm_pass_reads_every_byte_of_each_file(self):
        with tempfile.TemporaryDirectory(prefix="memr-warmup-pass-") as directory:
            first = Path(directory) / "collection.anki2"
            second = Path(directory) / "collection.anki2-wal"
            first.write_bytes(b"\0" * (warmup.CHUNK_BYTES + 123))
            second.write_bytes(b"\1" * 7)
            state = warmup.CollectionWarmup(SimpleNamespace(stop=threading.Event()),
                                            [first, second], sleep=lambda _seconds: None)
            state.start()
            self.assertTrue(state.done.wait(5), "warm-up did not finish its pass")
            self.assertEqual(state.bytes_read, first.stat().st_size + second.stat().st_size)

    def test_collection_files_cover_main_and_wal(self):
        with tempfile.TemporaryDirectory(prefix="memr-warmup-files-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * 4096)
            col = SimpleNamespace(path=str(path))
            self.assertEqual(warmup.collection_files(col), [path])
            wal = path.with_name(path.name + "-wal")
            wal.write_bytes(b"\1" * 2048)
            self.assertEqual(warmup.collection_files(col), [path, wal])

    @unittest.skipUnless(os.path.exists("/proc/locks"), "procfs locks view unavailable")
    def test_pass_keeps_fds_open_and_preserves_posix_locks(self):
        # POSIX drops all of a process's fcntl locks on an inode when ANY of
        # its descriptors on it closes; the warm-up must never close its read
        # descriptor while Anki's SQLite lock protects the collection.
        import fcntl
        with tempfile.TemporaryDirectory(prefix="memr-warmup-locks-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * (warmup.CHUNK_BYTES + 1))
            state = None
            holder = os.open(path, os.O_RDWR)
            fcntl.lockf(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                ino = os.stat(path).st_ino
                def lock_held():
                    # /proc/locks: "n: POSIX ADVISORY WRITE <pid> <dev>:<ino> ..."
                    # Match on inode and owner pid; btrfs reports a different st_dev.
                    return any(parts[4] == str(os.getpid()) and parts[5].endswith(f":{ino}")
                               for parts in (line.split() for line
                                             in Path("/proc/locks").read_text().splitlines())
                               if len(parts) > 5)
                self.assertTrue(lock_held(), "test setup failed to take the byte lock")
                state = warmup.CollectionWarmup(SimpleNamespace(stop=threading.Event()),
                                                [path], sleep=lambda _seconds: None)
                state.start()
                self.assertTrue(state.done.wait(5), "warm-up did not finish its pass")
                self.assertTrue(lock_held(), "warm-up released a POSIX lock it did not own")
                self.assertTrue(state.held_fds, "warm-up kept no descriptors")
                for fd in state.held_fds:
                    os.fstat(fd)  # raises if the descriptor was closed
            finally:
                os.close(holder)
                if state is not None:
                    for fd in state.held_fds:
                        os.close(fd)

    def test_warmup_thread_lowers_its_priority(self):
        with tempfile.TemporaryDirectory(prefix="memr-warmup-prio-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * 4096)
            demoted = threading.Event()
            original = warmup.lower_thread_priority
            warmup.lower_thread_priority = demoted.set
            try:
                state = warmup.CollectionWarmup(SimpleNamespace(stop=threading.Event()),
                                                [path], sleep=lambda _seconds: None)
                state.start()
                self.assertTrue(state.done.wait(5), "warm-up did not finish its pass")
            finally:
                warmup.lower_thread_priority = original
            self.assertTrue(demoted.is_set(),
                            "the warm-up thread must demote its CPU and I/O priority")

    @unittest.skipUnless(sys.platform == "linux" and hasattr(os, "getpriority"),
                         "per-thread nice is Linux-specific")
    def test_warmup_demotes_only_its_own_thread(self):
        # Real pass, nothing patched out: the worker must reach nice 19 while
        # the calling thread's priority stays untouched. On Darwin
        # setpriority(PRIO_PROCESS) is process-wide, which is why the demotion
        # is Linux-only in the first place.
        with tempfile.TemporaryDirectory(prefix="memr-warmup-nice-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * (2 * warmup.CHUNK_BYTES))
            caller_nice = os.getpriority(os.PRIO_PROCESS, 0)
            worker_nice = []
            seen = threading.Event()

            def observe(_seconds):
                worker_nice.append(os.getpriority(os.PRIO_PROCESS, 0))
                seen.set()

            state = warmup.CollectionWarmup(SimpleNamespace(stop=threading.Event()),
                                            [path], sleep=observe)
            state.start()
            self.assertTrue(state.done.wait(5), "warm-up did not finish its pass")
            state.close()
            self.assertTrue(seen.is_set(), "warm-up thread never paced a chunk")
            self.assertEqual(worker_nice[-1], 19, "warm-up thread must run at the lowest nice")
            self.assertEqual(os.getpriority(os.PRIO_PROCESS, 0), caller_nice,
                             "warm-up must not demote the calling thread")

    def test_non_linux_makes_no_process_wide_priority_call(self):
        # On Darwin, os.nice → setpriority(PRIO_PROCESS) is process-wide and
        # would demote the whole engine for the session; it must not run.
        with patch.object(warmup.sys, "platform", "darwin"), \
                patch.object(os, "nice") as nice:
            warmup.lower_thread_priority()
        nice.assert_not_called()

    def test_ioprio_syscall_table_matches_kernel_numbers(self):
        # x86_64: __NR_ioprio_set=251 (asm/unistd_64.h). aarch64 follows
        # asm-generic/unistd.h: __NR_ioprio_set=30 — 31 is ioprio_get, and
        # calling it would silently keep the default I/O class.
        self.assertEqual(warmup._IOPRIO_SET_SYSCALLS,
                         {"x86_64": 251, "aarch64": 30})

    def test_cancel_ends_the_pass_without_engine_stop(self):
        # A collection replace keeps the engine alive; the pass has its own
        # cancel scope independent of engine.stop.
        with tempfile.TemporaryDirectory(prefix="memr-warmup-cancelonly-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * (3 * warmup.CHUNK_BYTES))
            engine = SimpleNamespace(stop=threading.Event())
            entered = threading.Event()
            release = threading.Event()

            def pause(_seconds):
                entered.set()
                release.wait(5)

            state = warmup.CollectionWarmup(engine, [path], sleep=pause)
            state.start()
            self.assertTrue(entered.wait(5), "warm-up never began its paced pass")
            state.cancel()
            release.set()
            self.assertTrue(state.done.wait(5), "cancelled warm-up did not exit")
            self.assertFalse(engine.stop.is_set(), "collection cancel must not stop the engine")
            self.assertLess(state.bytes_read, path.stat().st_size,
                            "a cancelled pass must not finish the file")

    def test_shutdown_releases_warmup_descriptors(self):
        # engine.shutdown closes the collection; the pass's descriptors go
        # only after SQLite released its record locks.
        with tempfile.TemporaryDirectory(prefix="memr-warmup-shutdown-") as directory:
            col = Collection(str(Path(directory) / "collection.anki2"))
            engine, output = build_engine(col)
            state = engine._collection_warmup
            self.assertTrue(state.done.wait(10))
            held = list(state.held_fds)
            self.assertTrue(held, "warm-up held no descriptors")
            engine.dispatch(json.dumps({"id": 1, "method": "engine.shutdown",
                                        "params": {}}).encode())
            self.assertTrue(state.closed)
            self.assertEqual(state.held_fds, [])
            for fd in held:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertTrue(engine.stop.is_set())
            frame = json.loads(output.getvalue())
            self.assertIn("result", frame, frame)

    def test_replace_releases_old_inode_and_warms_the_new_file(self):
        # import_collection does a same-process replace: col.close(),
        # import_collection_package swaps the file, col.reopen(). The old
        # warm-up must release the replaced inode and the new file must be
        # warmed again.
        second = None
        with temp_collection() as col:
            engine, _output = build_engine(col)
            first = engine._collection_warmup
            self.assertTrue(first.done.wait(10))
            info = os.stat(col.path)
            old_identity = (info.st_dev, info.st_ino)
            backup = maintenance.force_backup(engine)
            handlers.transfer.import_collection(engine, backup)
            self.assertTrue(first.closed, "replace must close the old warm-up")
            self.assertEqual(first.held_fds, [])
            second = engine._collection_warmup
            self.assertIsNot(second, first)
            self.assertTrue(second.done.wait(10))
            self.assertGreater(second.bytes_read, 0, "the new file was not warmed")
            self.assertIn(Path(col.path), second.paths)
            if os.path.isdir("/proc/self/fd"):
                # Nothing in this process may still point at the old inode.
                # A live file that recycled the freed inode number — or an
                # inode on another device — shares st_ino without pinning it.
                pinned = descriptors_pinning(Path(col.path), old_identity)
                self.assertEqual(pinned, [],
                                 "a descriptor still pins the replaced inode")
        if second is not None:
            second.close()  # after col.close(): ordering matches the engine paths

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "procfs fd view unavailable")
    def test_inode_check_ignores_a_live_file_reusing_the_number(self):
        # Once the replaced file's last descriptor closed, its inode number
        # is free; a live file that recycles it must not count as a pin.
        with tempfile.TemporaryDirectory(prefix="memr-warmup-ino-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * 8)
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
            path.unlink()
            recycled = None
            for attempt in range(4096):
                candidate = Path(directory) / f"file-{attempt}"
                candidate.write_bytes(b"\1")
                if (candidate.stat().st_dev, candidate.stat().st_ino) == identity:
                    recycled = candidate
                    break
            if recycled is None:
                self.skipTest("the filesystem did not recycle the freed inode number")
            holder = os.open(recycled, os.O_RDONLY)
            try:
                # The naive st_ino-only comparison misfires on this descriptor.
                self.assertEqual(os.fstat(holder).st_ino, identity[1])
                self.assertEqual(descriptors_pinning(path, identity), [])
            finally:
                os.close(holder)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "procfs fd view unavailable")
    def test_inode_check_counts_a_descriptor_holding_the_replaced_file(self):
        with tempfile.TemporaryDirectory(prefix="memr-warmup-pin-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * 8)
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
            holder = os.open(path, os.O_RDONLY)
            try:
                # A live path carrying the identity is not yet a pin; only a
                # descriptor that still shows the file as deleted counts.
                self.assertEqual(descriptors_pinning(path, identity), [])
                path.unlink()
                self.assertEqual(descriptors_pinning(path, identity), [str(holder)])
            finally:
                os.close(holder)
            self.assertEqual(descriptors_pinning(path, identity), [])

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "procfs fd view unavailable")
    def test_inode_check_follows_symlinked_directories(self):
        # procfs reports the resolved dentry path; a logical path through a
        # symlinked directory (a symlinked TMPDIR) must still find the pin.
        with tempfile.TemporaryDirectory(prefix="memr-warmup-link-") as directory:
            real_dir = Path(directory) / "real"
            real_dir.mkdir()
            link_dir = Path(directory) / "link"
            link_dir.symlink_to(real_dir)
            path = link_dir / "collection.anki2"
            path.write_bytes(b"\0" * 8)
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
            holder = os.open(path, os.O_RDONLY)
            path.unlink()
            try:
                self.assertEqual(descriptors_pinning(path, identity), [str(holder)])
            finally:
                os.close(holder)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "procfs fd view unavailable")
    def test_inode_check_ignores_a_matching_inode_on_another_device(self):
        # The device half of the identity is load-bearing: a descriptor with
        # a matching st_ino on a different device must not count, even when
        # its link still shows the replaced path as deleted.
        with tempfile.TemporaryDirectory(prefix="memr-warmup-dev-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * 8)
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
            holder = os.open(path, os.O_RDONLY)
            path.unlink()
            try:
                real_fstat = os.fstat

                def fstat(fd):
                    stat = real_fstat(fd)
                    if fd != holder:
                        return stat
                    return os.stat_result((stat.st_mode, stat.st_ino, stat.st_dev + 1,
                                           stat.st_nlink, stat.st_uid, stat.st_gid,
                                           stat.st_size, int(stat.st_atime),
                                           int(stat.st_mtime), int(stat.st_ctime)))

                with patch("os.fstat", side_effect=fstat):
                    self.assertEqual(descriptors_pinning(path, identity), [])
            finally:
                os.close(holder)

    def test_missing_and_unreadable_files_end_the_pass_quietly(self):
        with tempfile.TemporaryDirectory(prefix="memr-warmup-io-") as directory:
            engine = SimpleNamespace(stop=threading.Event())
            missing = Path(directory) / "collection.anki2"
            state = warmup.CollectionWarmup(engine, [missing])
            state.start()
            self.assertTrue(state.done.wait(5))
            self.assertEqual(state.bytes_read, 0)
            self.assertFalse(engine.stop.is_set())
            state.close()

            unreadable = Path(directory) / "other.anki2"
            unreadable.write_bytes(b"\0" * 8)
            unreadable.chmod(0)
            try:
                # Mode 000 is not a refusal for every runner: a root process
                # (the release job's container) opens it anyway. The contract
                # under test is that a refused open ends the pass quietly, so
                # inject the refusal instead of relying on the file mode.
                real_open = os.open

                def open_(path, *args, **kwargs):
                    if Path(path) == unreadable:
                        raise PermissionError(errno.EACCES,
                                              os.strerror(errno.EACCES),
                                              os.fspath(path))
                    return real_open(path, *args, **kwargs)

                with patch("os.open", side_effect=open_):
                    state = warmup.CollectionWarmup(engine, [unreadable])
                    state.start()
                    self.assertTrue(state.done.wait(5))
                    self.assertEqual(state.bytes_read, 0)
                    self.assertFalse(engine.stop.is_set())
                    state.close()
            finally:
                unreadable.chmod(0o600)

    def test_shrinking_file_mid_pass_ends_quietly(self):
        with tempfile.TemporaryDirectory(prefix="memr-warmup-shrink-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * (3 * warmup.CHUNK_BYTES))
            original = path.stat().st_size
            engine = SimpleNamespace(stop=threading.Event())
            shrunk = threading.Event()

            def shrink(_seconds):
                if not shrunk.is_set():
                    shrunk.set()
                    path.write_bytes(b"\0" * 1024)

            state = warmup.CollectionWarmup(engine, [path], sleep=shrink)
            state.start()
            self.assertTrue(state.done.wait(5))
            state.close()
            self.assertLess(state.bytes_read, original,
                            "the pass must stop quietly at the shrunken end")
            self.assertFalse(engine.stop.is_set())

    def test_stop_cancels_mid_pass(self):
        with tempfile.TemporaryDirectory(prefix="memr-warmup-cancel-") as directory:
            path = Path(directory) / "collection.anki2"
            path.write_bytes(b"\0" * (3 * warmup.CHUNK_BYTES))
            engine = SimpleNamespace(stop=threading.Event())
            entered = threading.Event()
            release = threading.Event()

            def pause(_seconds):
                entered.set()
                release.wait(5)

            state = warmup.CollectionWarmup(engine, [path], sleep=pause)
            state.start()
            self.assertTrue(entered.wait(5), "warm-up never began its paced pass")
            engine.stop.set()
            release.set()
            self.assertTrue(state.done.wait(5), "stopped warm-up did not exit")
            self.assertLess(state.bytes_read, path.stat().st_size,
                            "a cancelled pass must not finish the file")

    def test_write_during_warmup_completes_promptly(self):
        with temp_collection() as col, \
                tempfile.TemporaryDirectory(prefix="memr-warmup-write-") as directory:
            engine, output = build_engine(col)
            try:
                path = Path(directory) / "big.bin"
                path.write_bytes(b"\0" * (2 * warmup.CHUNK_BYTES))
                gate = threading.Event()
                entered = threading.Event()

                def pause(_seconds):
                    entered.set()
                    gate.wait(5)

                blocker = warmup.CollectionWarmup(engine, [path], sleep=pause)
                blocker.start()
                self.assertTrue(entered.wait(5), "warm-up was not mid-pass")
                start = time.monotonic()
                engine.dispatch(json.dumps({"id": 1, "method": "decks.create",
                                            "params": {"name": "Concurrent"}}).encode())
                elapsed = time.monotonic() - start
                gate.set()
                self.assertTrue(blocker.done.wait(5))
                frame = json.loads(output.getvalue())
                self.assertIn("result", frame, frame)
                self.assertLess(elapsed, 5, "a review write stalled behind the warm-up")
            finally:
                engine.progress.close()
                engine.stop.set()


if __name__ == "__main__":
    unittest.main()
