"""Keep Anki's server alive only while its supervising Node pipe is open."""
import os
import signal
import subprocess
import sys
import threading


def main():
    # Native user/media directories must remain admissible on restart even
    # when the supervising application inherited a permissive CI/login umask.
    os.umask(0o077)
    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_args: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_args: stopping.set())

    def parent_pipe():
        while sys.stdin.buffer.read(1):
            pass
        stopping.set()

    threading.Thread(target=parent_pipe, daemon=True, name="sync-server-parent").start()
    child = subprocess.Popen([sys.executable, "-I", "-B", "-m", "anki.syncserver"],
                             stdin=subprocess.DEVNULL, env=dict(os.environ))
    try:
        while child.poll() is None and not stopping.wait(0.1):
            pass
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=1)
        return 0 if stopping.is_set() else child.returncode
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=1)


if __name__ == "__main__":
    sys.exit(main())
