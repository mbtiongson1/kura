"""Explicit opt-in real aria2 test. Only original CC0 bytes, loopback tracker/peer.
Run: .venv/bin/python tests/termux_e2e.py
Not collected by normal unit tests; requires aria2. No public content is downloaded.
"""
from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.parse
from unittest.mock import patch

from kura.cli import main
from kura.provider import parse_rss
from kura.torrent import encode
from helpers import NAME, PAYLOAD as BASE_PAYLOAD, sample

PAYLOAD = BASE_PAYLOAD * 32  # Large enough to observe a genuine partial transfer.


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def exercise():
    binary = shutil.which("aria2c")
    if not binary:
        raise RuntimeError("Install aria2 before running the runtime acceptance test")
    version = subprocess.check_output([binary, "--version"], text=True).splitlines()[0]
    row, descriptor = sample(payload=PAYLOAD)
    seed_port, leech_port = free_port(), free_port()
    announced = threading.Event()
    peers = {}

    class Tracker(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            port = int(query["port"][0])
            peers[port] = True
            if port == seed_port:
                announced.set()
            compact = b"".join(socket.inet_aton("127.0.0.1") + struct.pack("!H", p)
                               for p in list(peers) if p != port)
            body = encode({b"interval": 1, b"complete": 1, b"incomplete": 1, b"peers": compact})
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Tracker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tracker = f"http://127.0.0.1:{server.server_port}/announce"
    real_popen = subprocess.Popen
    seeder = None
    with tempfile.TemporaryDirectory(prefix="kura-authorized-") as temp:
        root = Path(temp)
        seed = root / "seed"
        seed.mkdir()
        (seed / NAME).write_bytes(PAYLOAD)
        descriptor_path = root / "sample.torrent"
        descriptor_path.write_bytes(descriptor)
        # Seeder is a test-only process, not an implemented Kura seeding policy.
        args = [binary, "--no-conf=true", "--enable-rpc=false", "--enable-dht=false",
                "--enable-dht6=false", "--bt-enable-lpd=false", "--bt-exclude-tracker=*",
                "--bt-tracker=" + tracker, "--seed-time=5", "--seed-ratio=100",
                "--check-integrity=true", "--disable-ipv6=true", "--summary-interval=0",
                "--console-log-level=error", "--listen-port=" + str(seed_port),
                "--dir=" + str(seed), "--torrent-file=" + str(descriptor_path)]
        # Remove hostile top-level URLs from the seeder descriptor too.
        from kura.torrent import inspect
        descriptor_path.write_bytes(inspect(descriptor, row.infohash, len(PAYLOAD)).sanitized)
        log = (root / "seeder.log").open("wb")
        try:
            seeder = real_popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            if not announced.wait(20):
                raise RuntimeError("Local seeder did not announce: " + (root / "seeder.log").read_text())

            throttle = False
            def local_popen(args, **kwargs):
                # Only the tracker and incoming peer port change for the controlled test.
                # All production safe-subprocess flags remain in force.
                assert "--no-conf=true" in args and "--enable-rpc=false" in args
                args = ["--bt-tracker=" + tracker if a.startswith("--bt-tracker=") else
                        "--listen-port=" + str(leech_port) if a.startswith("--listen-port=") else a for a in args]
                if throttle:
                    args.append("--max-download-limit=128K")
                return real_popen(args, **kwargs)

            rss = f'''<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item>
            <title>{NAME}</title><link>{row.url}</link>
            <nyaa:infoHash>{row.infohash}</nyaa:infoHash><nyaa:size>{len(PAYLOAD)} B</nyaa:size>
            <nyaa:seeders>1</nyaa:seeders><nyaa:categoryId>1_2</nyaa:categoryId>
            <nyaa:trusted>Yes</nyaa:trusted></item></channel></rss>'''.encode()

            def fixture_fetch(url, limit, descriptor=False):
                return globals_descriptor if descriptor else rss
            globals_descriptor = descriptor

            def cli(*args):
                output = io.StringIO()
                with patch.object(sys, "argv", ["kura", *args]), redirect_stdout(output):
                    try:
                        main()
                    except SystemExit as e:
                        code = e.code
                result = json.loads(output.getvalue())
                return code, result

            with patch.dict(os.environ, {"HOME": str(root / "home")}), \
                 patch("kura.provider.fetch", side_effect=fixture_fetch), \
                 patch("kura.downloader.subprocess.Popen", side_effect=local_popen):
                (root / "home").mkdir()
                assert cli("prefs", "set", "--resolution", "1080p", "--groups", "Kura")[0] == 0
                code, search = cli("search", "Kura Sample", "--episode", "3", "--json")
                assert code == 0 and len(search["data"]["candidates"]) == 1, search
                code, plan = cli("download", "Kura Sample", "--episode", "3", "--dry-run")
                assert code == 0 and plan["data"]["state"] == "planned", plan
                code, receipt = cli("download", "Kura Sample", "--episode", "3", "--yes")
                assert code == 0 and receipt["data"]["state"] == "complete", receipt
                evidence = receipt["data"]["evidence"]
                assert Path(evidence["path"]).read_bytes() == PAYLOAD
                assert evidence["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
                code, duplicate = cli("download", "Kura Sample", "--episode", "3", "--yes")
                assert code == 2 and duplicate["error"]["code"] == "duplicate", duplicate
                code, status = cli("downloads", "status")
                assert code == 0 and status["data"]["downloads"][0]["state"] == "complete"

                # A second fresh CLI state exercises a real interrupted transfer,
                # offline explicit resume, and remaining-allocation disk accounting.
                resume_home = root / "resume-home"
                resume_home.mkdir()
                partial = resume_home / ".local/share/kura/downloads" / row.infohash / NAME
                stop = threading.Event()
                interrupted_partial = threading.Event()
                def interrupt_partial():
                    deadline = time.monotonic() + 25
                    while not stop.is_set() and time.monotonic() < deadline:
                        if partial.exists():
                            st = partial.stat()
                            allocated = min(st.st_size, st.st_blocks * 512, len(PAYLOAD))
                            if 16384 <= allocated < len(PAYLOAD):
                                interrupted_partial.set()
                                os.kill(os.getpid(), signal.SIGINT)
                                return
                        stop.wait(0.02)
                    if not stop.is_set():
                        os.kill(os.getpid(), signal.SIGINT)  # Fail promptly, never leave a test hanging.
                with patch.dict(os.environ, {"HOME": str(resume_home)}):
                    throttle = True
                    interrupter = threading.Thread(target=interrupt_partial, daemon=True)
                    interrupter.start()
                    try:
                        code, interrupted = cli("download", "Kura Sample", "--episode", "3", "--yes")
                    finally:
                        stop.set()
                        interrupter.join(5)
                        throttle = False
                    assert code == 130 and interrupted_partial.is_set(), interrupted
                    st = partial.stat()
                    allocated = min(st.st_size, st.st_blocks * 512, len(PAYLOAD))
                    assert 0 < allocated < len(PAYLOAD), allocated
                    from kura.downloader import RESERVE
                    free = RESERVE + len(PAYLOAD) - allocated + 1024
                    assert free < RESERVE + len(PAYLOAD)
                    with patch("kura.provider.fetch", side_effect=AssertionError("Resume must not contact Nyaa")), \
                         patch("kura.downloader.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
                        code, resumed = cli("download", "Kura Sample", "--episode", "3", "--resume", "--yes")
                    assert code == 0 and resumed["data"]["state"] == "complete", resumed
                    assert Path(resumed["data"]["evidence"]["path"]).read_bytes() == PAYLOAD
                    resume_evidence = {"state": "complete", "provider": "not consulted",
                                       "allocated_before": allocated, "constrained_free": free,
                                       "sha256": resumed["data"]["evidence"]["sha256"]}
                print(json.dumps({"success": True, "platform": sys.platform,
                      "python": sys.version.split()[0], "aria2": version,
                      "fixture": "Original CC0 payload; loopback tracker and aria2 seeder; no public media",
                      "test_seams": "Fixture Nyaa HTTPS fetch; local tracker/peer-port substitution only",
                      "search": "passed", "dry_run": "passed", "download": "complete",
                      "duplicate": "rejected", "status": "complete", "evidence": evidence,
                      "partial_resume": resume_evidence}, indent=2))
        finally:
            if seeder is not None and seeder.poll() is None:
                seeder.terminate()
                try:
                    seeder.wait(5)
                except subprocess.TimeoutExpired:
                    seeder.kill()
                    seeder.wait()
            log.close()
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    exercise()
