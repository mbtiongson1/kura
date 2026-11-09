"""Real process-lifecycle regression: repeated Ctrl-C while a child exits slowly."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from kura.models import KuraError
from kura.store import Store


@unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "android", "Linux/Android")
class RepeatedInterruptTests(unittest.TestCase):
    def wait_file(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return
            time.sleep(0.02)
        self.fail(f"Process did not create {path.name}")

    def test_repeated_sigint_retains_lock_until_slow_child_reaped(self):
        with tempfile.TemporaryDirectory(prefix="kura-signals-") as temp:
            root = Path(temp)
            pidfile, terminating, reaped = (root / x for x in ("pid", "terminating", "reaped"))
            fake = root / "aria2c"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import os, signal, time\n"
                "from pathlib import Path\n"
                "def stop(signum, frame):\n"
                f"    Path({str(terminating)!r}).write_text('terminating')\n"
                "    time.sleep(1)\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                "while True: time.sleep(1)\n")
            fake.chmod(0o700)
            helpers = str(Path(__file__).parent.resolve())
            script = (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0,{helpers!r}); "
                "from helpers import sample; from kura.torrent import inspect; "
                "from kura.downloader import run; from kura.store import Store; "
                "from unittest.mock import patch; "
                "root=Path(sys.argv[1]); row,data=sample(); "
                "torrent=inspect(data,row.infohash,1000000); store=Store(root/'state')\n"
                "try:\n"
                "    with store.lock() as fd, patch('kura.downloader.shutil.which',return_value=sys.argv[2]):\n"
                "        run(torrent,root/'out',store.root,lock_fd=fd)\n"
                "except KeyboardInterrupt:\n"
                "    (root/'reaped').write_text('reaped')\n")
            parent = subprocess.Popen([sys.executable, "-c", script, str(root), str(fake)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            child_pid = None
            try:
                self.wait_file(pidfile)
                child_pid = int(pidfile.read_text())
                os.kill(parent.pid, signal.SIGINT)
                self.wait_file(terminating)
                # Deliver real repeated interrupts during the child's delayed exit.
                os.kill(parent.pid, signal.SIGINT)
                os.kill(parent.pid, signal.SIGINT)
                other = Store(root / "state")
                try:
                    with self.assertRaises(KuraError) as caught:
                        with other.lock():
                            pass
                    self.assertEqual(caught.exception.code, "busy")
                finally:
                    other.db.close()
                _, stderr = parent.communicate(timeout=8)
                self.assertEqual(parent.returncode, 0, stderr.decode())
                self.assertEqual(reaped.read_text(), "reaped")
                # Parent only returns from run after wait() has reaped the child.
                self.assertFalse(Path(f"/proc/{child_pid}").exists())
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(5)
                if child_pid:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if parent.stderr:
                    parent.stderr.close()
