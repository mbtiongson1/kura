from dataclasses import replace
import hashlib
import json
import os
import signal
import time
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from kura.core import Core
from kura.downloader import run, RESERVE
from kura.models import KuraError, Preferences
from kura.provider import NoRedirect, Nyaa, fetch, parse_rss, validate_url
from kura.releases import eligible, parse_title, select
from kura.safety import destination
from kura.store import Store, validate_prefs
from kura.torrent import decode, encode, inspect, verify
from helpers import FakeProvider, NAME, PAYLOAD, sample


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = patch("kura.safety.private_root", return_value=self.root / "state")
        self.paths.start()
        self.store = Store(self.root / "state")
        self.core = Core(self.store, FakeProvider())
        self.row, self.data = sample()
        self.torrent = inspect(self.data, self.row.infohash, 1000000)

    def tearDown(self):
        self.store.db.close()
        self.paths.stop()
        self.tmp.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(KuraError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_preferences_persist_and_override(self):
        with self.store.lock():
            self.store.save_prefs(Preferences(resolution="720p", groups=("Kura",), min_seeders=7))
        second = Store(self.root / "state")
        self.assertEqual(second.prefs().groups, ("Kura",))
        second.db.close()
        self.assertEqual(self.core.preferences({"resolution": "1080p"}).resolution, "1080p")
        self.assertEqual(self.store.prefs().resolution, "720p")

    def test_all_preferences_enforced(self):
        for p in (Preferences(resolution="720p"), Preferences(groups=("Other",)),
                  Preferences(subtitles="raw"), Preferences(max_size_mb=1),
                  Preferences(min_seeders=11)):
            row = replace(self.row, size_bytes=2*1024**2) if p.max_size_mb == 1 else self.row
            self.assertEqual(eligible([row], "Kura Sample", 3, p), [])
        self.assertEqual(eligible([self.row], "Other", 3, Preferences()), [])
        self.assertEqual(eligible([self.row], "Kura Sample", 4, Preferences()), [])

    def test_bad_preferences(self):
        for p in (Preferences(max_size_mb=0), Preferences(max_size_mb=10001),
                  Preferences(min_seeders=-1), Preferences(groups=("bad\n",)),
                  Preferences(resolution="8k"), Preferences(subtitles="unknown")):
            self.error("invalid_preferences", validate_prefs, p)

    def test_title_parser(self):
        self.assertEqual(parse_title(NAME)[:2], ("Kura Sample", 3))
        self.assertEqual(parse_title("[Kura] Series 86 - 03 (1080p).mkv")[:2], ("Series 86", 3))
        self.assertEqual(parse_title("[Kura] Show S02E03 (1080p).mkv")[:2], ("Show S02", 3))
        for title in ("Show - 03-04 (1080p)", "Show - 03+04 (1080p)",
                      "Show [03] [1080p]", "Show - 03 Complete (1080p)",
                      "Show - 03 - 04 (1080p)", "Show - 03 Another Show (1080p)",
                      "Show - 1900 (1080p)", "Show 03 (1080p)", "Show - 03\n (1080p)"):
            self.assertIsNone(parse_title(title), title)

    def test_metadata_cannot_hide_batches_or_episode_markers(self):
        for tag in ("01 - 12", "03 ~ 04", "E04", "EP. 04", "S01E04", "04",
                    "03, 04", "S01E03E04", "03&04", "03 04", "03_04",
                    "Episode3", "Season1Episode3Episode4"):
            self.assertIsNone(parse_title(f"Show - 03 [{tag}] (1080p).mkv"), tag)
            self.assertIsNone(parse_title(f"Show [{tag}] - 03 (1080p).mkv"), tag)
            name = f"[Kura] Kura Sample - 03 [{tag}] (1080p).txt"
            row, data = sample(name)
            self.core.provider = FakeProvider([replace(self.row, infohash=row.infohash)], data)
            self.error("descriptor_identity", self.core.download, "Kura Sample", 3, {}, dry_run=True)

    def test_ambiguity_not_resolved_by_seeders(self):
        other = replace(self.row, id="2", infohash="a"*40, seeders=100)
        rows = eligible([self.row, other], "Kura Sample", 3, Preferences())
        self.error("ambiguous", select, rows, Preferences(), None)
        self.assertEqual(select(rows, Preferences(), "1"), self.row)

    def test_untrusted_requires_explicit_selection(self):
        row = replace(self.row, trusted=False)
        self.error("no_match", select, [row], Preferences(), None)
        self.assertEqual(select([row], Preferences(), "1"), row)

    def test_group_ranking(self):
        other = replace(self.row, id="2", infohash="a"*40, group="Other", seeders=100)
        p = Preferences(groups=("Kura", "Other"))
        rows = eligible([other, self.row], "Kura Sample", 3, p)
        self.assertEqual(select(rows, p, None), self.row)

    def test_duplicate_feed_rows(self):
        self.assertEqual(len(eligible([self.row, self.row], "Kura Sample", 3, Preferences())), 1)

    def test_path_boundary(self):
        for value in ("/sdcard", "private/../oops", "private//oops", "private/./x",
                      "private/a\\b", "shared/../../x", "private/x\n"):
            self.error("unsafe_destination", destination, value)
        link = self.root / "state" / "downloads"
        link.symlink_to(self.root)
        self.error("unsafe_path", destination, "private")

    def test_state_symlink(self):
        self.store.db.close()
        path = self.root / "state" / "state.sqlite3"
        path.unlink()
        path.symlink_to(self.root / "other")
        self.error("unsafe_path", Store, self.root / "state")

    def test_lock_prevents_concurrent_mutation(self):
        other = Store(self.root / "state")
        with self.store.lock():
            self.error("busy", self.enter_lock, other)
        other.db.close()

    @staticmethod
    def enter_lock(store):
        with store.lock():
            pass

    def test_downloader_inherits_mutation_lock(self):
        with patch("kura.downloader.run", return_value={}) as mocked:
            self.core.download("Kura Sample", 3, {}, yes=True)
        self.assertIsInstance(mocked.call_args.kwargs["lock_fd"], int)

    def test_dry_run_no_downloader_no_job(self):
        with patch("kura.downloader.run") as mocked:
            plan = self.core.download("Kura Sample", 3, {}, dry_run=True)
        self.assertEqual(plan["state"], "planned")
        self.assertEqual(plan["expected_size_bytes"], len(PAYLOAD))
        self.assertEqual(self.store.jobs(), [])
        mocked.assert_not_called()

    def test_confirmation_required(self):
        with patch("kura.downloader.run") as mocked:
            self.error("confirmation_required", self.core.download, "Kura Sample", 3, {})
        mocked.assert_not_called()
        self.assertEqual(self.store.jobs(), [])

    def test_wrong_descriptor_identity(self):
        row, data = sample("[Kura] Other - 03 (1080p).txt")
        self.core.provider = FakeProvider([replace(self.row, infohash=row.infohash)], data)
        self.error("descriptor_identity", self.core.download, "Kura Sample", 3, {}, dry_run=True)

    def test_descriptor_group_and_resolution_always_match_selection(self):
        for name in ("[Other] Kura Sample - 03 (1080p).txt", "[Kura] Kura Sample - 03 (720p).txt"):
            row, data = sample(name)
            self.core.provider = FakeProvider([replace(self.row, infohash=row.infohash)], data)
            self.error("descriptor_identity", self.core.download, "Kura Sample", 3,
                       {"resolution": "any"}, dry_run=True)

    def test_size_bound_authoritative(self):
        self.error("unsafe_torrent", inspect, self.data, self.row.infohash, 1)

    def test_descriptor_hash_mismatch(self):
        self.error("hash_mismatch", inspect, self.data, "0"*40, 1000000)

    def test_descriptor_drops_all_urls(self):
        self.assertEqual(set(decode(self.torrent.sanitized)), {b"info"})

    def test_unsafe_torrent_names(self):
        for name in ("../x.mkv", "/x.mkv", "-x.mkv", ".x.mkv", "x\\y.mkv", "x:y.mkv", "x.sh"):
            row, data = sample(name)
            self.error("unsafe_torrent", inspect, data, row.infohash, 1000000)

    def test_multifile_and_symlink_metadata_rejected(self):
        info = decode(self.data)[b"info"]
        for key in (b"files", b"attr", b"symlink path", b"name.utf-8"):
            bad = dict(info)
            bad[key] = b"x"
            data = encode({b"info": bad})
            ih = hashlib.sha1(encode(bad)).hexdigest()
            self.error("unsupported_torrent", inspect, data, ih, 1000000)

    def test_bencode_attacks(self):
        for data in (b"", b"i01e", b"i-0e", b"d1:ai1e1:ai2ee", b"999:abc",
                     b"l"*30+b"e"*30, b"d1:bi1e1:ai2ee", b"i1ee", b"01:x"):
            self.error("invalid_torrent", decode, data)

    def test_completion_piece_verification(self):
        (self.root / NAME).write_bytes(PAYLOAD)
        evidence = verify(self.torrent, self.root)
        self.assertEqual(evidence["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        (self.root / NAME).write_bytes(b"x"*len(PAYLOAD))
        self.error("verification_failed", verify, self.torrent, self.root)

    def test_missing_short_and_control_file_not_complete(self):
        self.error("verification_failed", verify, self.torrent, self.root)
        (self.root / NAME).write_bytes(b"x")
        self.error("verification_failed", verify, self.torrent, self.root)
        (self.root / NAME).write_bytes(PAYLOAD)
        (self.root / (NAME+".aria2")).touch()
        self.error("verification_failed", verify, self.torrent, self.root)

    def test_symlink_payload_rejected(self):
        target = self.root / "target"
        target.write_bytes(PAYLOAD)
        (self.root / NAME).symlink_to(target)
        self.error("unsafe_path", verify, self.torrent, self.root)

    def test_completion_and_duplicate(self):
        with patch("kura.downloader.run", return_value={"sha256": "proof"}):
            job = self.core.download("Kura Sample", 3, {}, yes=True)
            self.assertEqual(job["state"], "complete")
            self.error("duplicate", self.core.download, "Kura Sample", 3, {}, yes=True)
        self.assertEqual(self.store.jobs()[0]["state"], "complete")

    def test_failure_resume_and_interruption(self):
        with patch("kura.downloader.run", side_effect=KuraError("download_failed", "fixture")):
            self.error("download_failed", self.core.download, "Kura Sample", 3, {}, yes=True)
        self.assertEqual(self.store.jobs()[0]["state"], "failed")
        self.error("duplicate", self.core.download, "Kura Sample", 3, {}, yes=True)
        with patch("kura.downloader.run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.core.download("Kura Sample", 3, {}, yes=True, resume=True)
        self.assertEqual(self.store.jobs()[0]["state"], "interrupted")

    def test_resume_uses_recorded_descriptor_without_provider_or_seeder_gate(self):
        with patch("kura.downloader.run", side_effect=KuraError("download_failed", "fixture")):
            self.error("download_failed", self.core.download, "Kura Sample", 3, {}, yes=True)
        self.assertIsNotNone(self.store.descriptor(self.row.infohash))
        provider = Mock()
        provider.search.side_effect = AssertionError("Resume must not rediscover")
        provider.descriptor.side_effect = AssertionError("Resume must use validated local metadata")
        self.core.provider = provider
        self.store.save_prefs(Preferences(min_seeders=999))
        self.error("resume_preferences", self.core.download, "Kura Sample", 3,
                   {"resolution": "720p"}, resume=True, yes=True)
        self.error("resume_mismatch", self.core.download, "Kura Sample", 3,
                   {"destination": "private/elsewhere"}, resume=True, yes=True)
        self.error("resume_mismatch", self.core.download, "Kura Sample", 3,
                   {}, resume=True, yes=True, release_id="2")
        with patch("kura.downloader.run", return_value={"verified": True}):
            result = self.core.download("Kura Sample", 3, {}, resume=True, yes=True)
        self.assertEqual(result["state"], "complete")
        provider.search.assert_not_called()
        provider.descriptor.assert_not_called()

    def test_resume_without_record_refused(self):
        self.error("no_resume", self.core.download, "Kura Sample", 3, {}, resume=True, yes=True)

    def test_sigkill_recovery(self):
        with patch("kura.downloader.run", return_value={}):
            job = self.core.download("Kura Sample", 3, {}, yes=True)
        job["state"] = "acquiring"
        self.store.record(job)
        self.assertEqual(self.core.status()["downloads"][0]["state"], "interrupted")

    def test_unexpected_file_and_resume_symlink(self):
        folder = destination("private") / self.row.infohash
        folder.mkdir(parents=True)
        (folder / "foreign.txt").touch()
        self.error("destination_not_empty", self.core.download, "Kura Sample", 3, {}, yes=True)

    def test_no_source(self):
        self.core.provider = FakeProvider([])
        self.error("no_match", self.core.download, "Kura Sample", 3, {}, dry_run=True)

    def test_query_validation(self):
        self.error("invalid_query", self.core.search, "", 3, {})
        self.error("invalid_query", self.core.search, "Kura Sample", 0, {})

    def test_missing_downloader(self):
        with patch("kura.downloader.shutil.which", return_value=None):
            self.error("downloader_missing", run, self.torrent, self.root / "out", self.store.root)

    def test_disk_reserve(self):
        with patch("kura.downloader.shutil.which", return_value="/bin/aria2c"), \
             patch("kura.downloader.shutil.disk_usage", return_value=Mock(free=RESERVE)):
            self.error("insufficient_space", run, self.torrent, self.root / "out", self.store.root)

    def test_resume_counts_allocated_not_logical_size(self):
        out = self.root / "out"
        out.mkdir()
        payload = out / NAME
        payload.write_bytes(PAYLOAD)
        proc = Mock()
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        with patch("kura.downloader.shutil.which", return_value="/bin/aria2c"), \
             patch("kura.downloader.shutil.disk_usage", return_value=Mock(free=RESERVE+1)), \
             patch("kura.downloader.subprocess.Popen", return_value=proc):
            self.assertEqual(run(self.torrent, out, self.store.root)["size_bytes"], len(PAYLOAD))
        # A sparse logical length is not allocated disk space and cannot earn credit.
        payload.unlink()
        with payload.open("wb") as f:
            f.truncate(len(PAYLOAD))
        with patch("kura.downloader.shutil.which", return_value="/bin/aria2c"), \
             patch("kura.downloader.shutil.disk_usage", return_value=Mock(free=RESERVE+1)):
            self.error("insufficient_space", run, self.torrent, out, self.store.root)

    def test_subprocess_safe_and_nonzero(self):
        proc = Mock()
        proc.wait.return_value = 9
        proc.poll.return_value = 9
        with patch("kura.downloader.shutil.which", return_value="/bin/aria2c"), \
             patch("kura.downloader.subprocess.Popen", return_value=proc) as popen:
            self.error("download_failed", run, self.torrent, self.root / "out", self.store.root)
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kwargs)
        self.assertIn("--no-conf=true", args[0])
        self.assertIn("--enable-rpc=false", args[0])
        self.assertNotIn("HTTP_PROXY", kwargs["env"])
        self.assertFalse(list(self.store.root.glob("*.torrent")))

    def test_subprocess_timeout_and_ctrl_c_terminate(self):
        for error in (subprocess.TimeoutExpired("aria2c", 1), KeyboardInterrupt()):
            proc = Mock()
            proc.wait.side_effect = [error, 0]
            proc.poll.return_value = None
            with patch("kura.downloader.shutil.which", return_value="/bin/aria2c"), \
                 patch("kura.downloader.subprocess.Popen", return_value=proc):
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        run(self.torrent, self.root / "out", self.store.root)
                else:
                    self.error("download_timeout", run, self.torrent, self.root / "out", self.store.root)
            proc.terminate.assert_called_once()

    def test_repeated_python_interrupt_during_cleanup_still_reaps(self):
        proc = Mock()
        proc.wait.side_effect = [KeyboardInterrupt(), KeyboardInterrupt(), 0]
        proc.poll.return_value = None
        with patch("kura.downloader.shutil.which", return_value="/bin/aria2c"), \
             patch("kura.downloader.subprocess.Popen", return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                run(self.torrent, self.root / "out", self.store.root)
        self.assertEqual(proc.wait.call_count, 3)
        self.assertEqual(proc.terminate.call_count, 2)

    def test_exit_zero_without_payload_fails(self):
        proc = Mock()
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        with patch("kura.downloader.shutil.which", return_value="/bin/aria2c"), \
             patch("kura.downloader.subprocess.Popen", return_value=proc):
            self.error("verification_failed", run, self.torrent, self.root / "out", self.store.root)


class ProviderTests(unittest.TestCase):
    def test_url_allowlist(self):
        for url in ("http://nyaa.si/download/1.torrent", "https://evil.nyaa.si/download/1.torrent",
                    "https://nyaa.si@evil.test/download/1.torrent", "https://nyaa.si:443/download/1.torrent",
                    "https://nyaa.si/download/../1.torrent", "https://nyaa.si/download/1.torrent?q=x",
                    "https://nyaa.si/download/1.torrent#x", "file:///etc/passwd"):
            with self.assertRaises(KuraError):
                validate_url(url, True)

    def test_redirect_rejected(self):
        with self.assertRaises(KuraError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1")

    def test_provider_offline(self):
        with patch("kura.provider.socket.getaddrinfo", side_effect=OSError("offline")):
            with self.assertRaises(KuraError) as caught:
                fetch("https://nyaa.si/?page=rss", 100)
        self.assertEqual(caught.exception.code, "provider_unavailable")

    def test_private_dns(self):
        with patch("kura.provider.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(KuraError) as caught:
                fetch("https://nyaa.si/?page=rss", 100)
        self.assertEqual(caught.exception.code, "unsafe_url")

    def test_feed_fixture(self):
        ih = "a"*40
        data = f"""<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item>
        <title>[Kura] Kura Sample - 03 (1080p).txt</title>
        <link>https://nyaa.si/download/1.torrent</link><nyaa:infoHash>{ih}</nyaa:infoHash>
        <nyaa:size>42 KiB</nyaa:size><nyaa:seeders>2</nyaa:seeders>
        <nyaa:categoryId>1_2</nyaa:categoryId><nyaa:trusted>Yes</nyaa:trusted>
        </item></channel></rss>""".encode()
        rows = parse_rss(data)
        self.assertEqual(rows[0].episode, 3)
        self.assertTrue(rows[0].trusted)
        self.assertEqual(rows[0].size_bytes, 42*1024)
        self.assertEqual(parse_rss(data.replace(b"https://nyaa.si/download", b"https://evil.test/download")), [])

    def test_season_episode_query_uses_contiguous_form_and_dedupes(self):
        queries = []
        rss = b'''<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item>
        <title>[Kura] Show S02E03 (1080p).mkv</title>
        <link>https://nyaa.si/download/1.torrent</link>
        <nyaa:infoHash>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</nyaa:infoHash>
        <nyaa:size>1 MiB</nyaa:size><nyaa:seeders>2</nyaa:seeders>
        <nyaa:categoryId>1_2</nyaa:categoryId><nyaa:trusted>Yes</nyaa:trusted>
        </item></channel></rss>'''
        def fake_fetch(url, limit):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
            queries.append(query)
            return rss if "S02E03" in query else b"<rss><channel/></rss>"
        with patch("kura.provider.fetch", side_effect=fake_fetch):
            rows = Nyaa().search("Show S02", 3, "english")
        self.assertEqual(queries, ["Show S02 03", "Show S02E03"])
        self.assertEqual((rows[0].series, rows[0].episode), ("Show S02", 3))
        with patch("kura.provider.fetch", return_value=rss):
            self.assertEqual(len(Nyaa().search("Show S02", 3, "english")), 1)

    def test_xml_entities_rejected(self):
        with self.assertRaises(KuraError):
            parse_rss(b'<!DOCTYPE rss [<!ENTITY x "xx">]><rss/>')

    def test_utf16_entities_rejected(self):
        with self.assertRaises(KuraError):
            parse_rss('<!DOCTYPE rss [<!ENTITY x "xx">]><rss/>'.encode('utf-16'))

    def test_bounded_response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x"*101
        opener = Mock()
        opener.open.return_value = response
        with patch("kura.provider.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.1.1.1", 443))]), \
             patch("kura.provider.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(KuraError) as caught:
                fetch("https://nyaa.si/?page=rss", 100)
        self.assertEqual(caught.exception.code, "metadata_limit")


class CLITests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "android", "Linux/Android process lifecycle")
    def test_sigkill_terminates_downloader_child(self):
        with tempfile.TemporaryDirectory() as root:
            pidfile = Path(root) / "child.pid"
            script = ("from kura.downloader import parent_death_guard; "
                      "import subprocess,sys,pathlib; "
                      "p=subprocess.Popen([sys.executable,\'-c\',\'import time; time.sleep(60)\'],"
                      "preexec_fn=parent_death_guard()); "
                      "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); p.wait()")
            parent = subprocess.Popen([sys.executable, "-c", script, str(pidfile)])
            child_pid = None
            try:
                deadline = time.monotonic() + 10
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pidfile.exists())
                child_pid = int(pidfile.read_text())
                parent.kill()
                parent.wait(5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    stat = Path(f"/proc/{child_pid}/stat")
                    try:
                        state = stat.read_text().split(") ")[1]
                    except (FileNotFoundError, ProcessLookupError):
                        break
                    if state.startswith("Z"):
                        break
                    time.sleep(0.02)
                else:
                    self.fail("Downloader survived its supervisor SIGKILL")
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait()
                if child_pid:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_fresh_process_preferences_and_json_errors(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, "HOME": root}
            def call(*args):
                p = subprocess.run([sys.executable, "-m", "kura", *args], capture_output=True, text=True, env=env)
                return p.returncode, json.loads(p.stdout)
            self.assertEqual(call("prefs", "set", "--resolution", "720p")[0], 0)
            self.assertEqual(call("prefs", "show")[1]["data"]["resolution"], "720p")
            code, result = call("download", "Sample", "--episode", "oops")
            self.assertEqual(code, 2)
            self.assertEqual(result["error"]["code"], "invalid_arguments")
            self.assertEqual(call("downloads", "status")[1]["data"]["downloads"], [])


if __name__ == "__main__":
    unittest.main()
