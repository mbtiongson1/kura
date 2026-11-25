"""CLI envelopes for the handover commands. Synthetic data only; no network."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import torrentgen
import kura.cli as cli
from kura.store import Store
from test_manifest import GOOD, Lab, snapshot

P = torrentgen.payload


def run(*argv, home=None, store=True):
    """Run the CLI in-process. Returns (exit code, decoded envelope)."""
    env = {"HOME": home} if home else {}
    buffer = io.StringIO()
    banned = patch("kura.cli.Store", side_effect=AssertionError("state touched")) if not store \
        else contextlib.nullcontext()
    with patch.dict(os.environ, env), patch("sys.argv", ["kura", *argv]), banned, \
         contextlib.redirect_stdout(buffer):
        try:
            cli.main()
        except SystemExit as exited:
            code = exited.code
    return code, json.loads(buffer.getvalue())


class Stateless(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        self.public = self.lab.add("Kura Sample Series", GOOD, announce="http://tracker.example/announce")
        self.file = str(self.lab.torrents / "Kura Sample Series.torrent")

    def test_inventory_inspect_verify_never_open_state(self):
        before = snapshot(self.lab.tmp)
        code, out = run("library", "inventory", "--root", f"main={self.lab.lib}", "--files", store=False)
        self.assertEqual((code, out["success"], out["phase"], out["schema_version"]), (0, True, "library", 1))
        title = out["data"]["roots"][0]["titles"][0]
        self.assertEqual((title["rel_path"], title["file_count"], len(title["files"])),
                         ("Kura Sample Series", 2, 2))
        code, out = run("library", "inventory", "--root", f"main={self.lab.lib}", store=False)
        self.assertNotIn("files", out["data"]["roots"][0]["titles"][0])

        code, out = run("torrent", "inspect", self.file, "--tracker-denylist", str(self.lab.deny),
                        "--protected", str(self.lab.prot), store=False)
        self.assertEqual(code, 0)
        self.assertEqual(out["data"]["torrent"]["infohash"], self.public.infohash)
        self.assertEqual(out["data"]["protection"], {"protected": False, "reasons": []})
        self.assertIn("tracker.example", out["data"]["magnet"])
        self.assertTrue(out["data"]["policy"]["has_denylist"])

        code, out = run("torrent", "verify", self.file, "--root", str(self.lab.lib), store=False)
        self.assertEqual(code, 0)
        self.assertEqual(out["data"]["verification"]["verdict"], "complete")
        self.assertEqual(out["data"]["torrent"]["file_count"], 2)
        self.assertEqual(snapshot(self.lab.tmp), before)       # nothing created anywhere, no state

    def test_verify_base_budget_and_failure_verdicts(self):
        nested = self.lab.tmp / "elsewhere" / "inner"
        nested.mkdir(parents=True)
        self.public.write_tree(nested)
        code, out = run("torrent", "verify", self.file, "--root", str(self.lab.tmp / "elsewhere"),
                        "--base", "inner", store=False)
        self.assertEqual(out["data"]["verification"]["verdict"], "complete")
        code, out = run("torrent", "verify", self.file, "--root", str(self.lab.lib),
                        "--max-read-gib", "0.000001", store=False)
        self.assertEqual((code, out["data"]["verification"]["verdict"]), (0, "budget_exhausted"))
        (self.lab.lib / "Kura Sample Series" / "ep01.mkv").unlink()
        code, out = run("torrent", "verify", self.file, "--root", str(self.lab.lib), store=False)
        self.assertEqual(out["data"]["verification"]["verdict"], "failed")

    def test_private_torrent_inspect_hides_credentials(self):
        built = self.lab.add("Kura Private Show", GOOD, private=True, torrent=False,
                             announce="http://private.example/announce?passkey=SECRETKEY")
        path = self.lab.tmp / "private.torrent"
        path.write_bytes(built.data)
        code, out = run("torrent", "inspect", str(path), store=False)
        text = json.dumps(out)
        self.assertEqual(code, 0)
        self.assertNotIn("SECRETKEY", text)
        self.assertEqual(out["data"]["protection"]["reasons"], ["private_flag"])
        self.assertIsNone(out["data"]["magnet"])
        self.assertEqual(out["data"]["torrent"]["tracker_hosts"], ["private.example"])

    def test_error_envelopes(self):
        link = self.lab.tmp / "liblink"
        os.symlink(self.lab.lib, link)
        cases = [
            (("library", "inventory", "--root", f"main={link}"), "unsafe_path"),
            (("library", "inventory", "--root", "nolabel"), "invalid_arguments"),
            (("library", "inventory", "--root", f"a={self.lab.lib}", "--root", f"a={self.lab.lib}"),
             "invalid_arguments"),
            (("library", "inventory"), "invalid_arguments"),
            (("torrent", "inspect", str(self.lab.tmp / "missing.torrent")), "not_found"),
            (("torrent", "verify", self.file, "--root", str(self.lab.lib), "--base", "../x"),
             "invalid_arguments"),
            (("torrent", "verify", self.file, "--root", str(self.lab.lib), "--max-read-gib", "0"),
             "invalid_arguments"),
            (("torrent", "inspect", str(self.lab.deny)), "invalid_torrent"),
            (("torrent", "inspect", self.file, "--tracker-denylist", str(self.lab.tmp / "none")),
             "invalid_policy"),
        ]
        for argv, code_name in cases:
            code, out = run(*argv, store=False)
            self.assertEqual((code, out["success"], out["error"]["code"]), (2, False, code_name), argv)

    def test_stateless_commands_run_without_home(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOME", None)
            with patch("pathlib.Path.home", side_effect=RuntimeError("no home")):
                code, out = run("library", "inventory", "--root", f"main={self.lab.lib}", store=False)
        self.assertEqual(code, 0)

    def test_stateless_commands_never_construct_the_resolver(self):
        with patch("kura.cli_handover.AniList", side_effect=AssertionError("constructed")):
            code, _ = run("torrent", "inspect", self.file, store=False)
            self.assertEqual(code, 0)
            code, _ = run("library", "inventory", "--root", f"main={self.lab.lib}", store=False)
            self.assertEqual(code, 0)


class Stateful(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        self.lab.add("Kura Sample Series", GOOD)
        self.lab.add("Kura Extra Show", GOOD)
        (self.lab.lib / "Kura Extra Show" / "notes.txt").write_text("extra")
        self.home = str(self.lab.tmp / "home")
        os.mkdir(self.home)
        self.build_args = ("manifest", "build", "--library", "home", "--root", f"main={self.lab.lib}",
                           "--torrents", str(self.lab.torrents), "--tracker-denylist", str(self.lab.deny),
                           "--protected", str(self.lab.prot))

    def fake_resolver(self):
        resolver = Mock()
        resolver.return_value.resolve.return_value = (None, "unresolved")
        return resolver

    def cli(self, *argv, resolver=None):
        buffer = io.StringIO()
        with patch.dict(os.environ, {"HOME": self.home}), patch("sys.argv", ["kura", *argv]), \
             patch("kura.cli_handover.AniList", resolver or self.fake_resolver()), \
             contextlib.redirect_stdout(buffer):
            try:
                cli.main()
            except SystemExit as exited:
                return exited.code, json.loads(buffer.getvalue())

    def test_pipeline_build_then_show(self):
        code, out = self.cli(*self.build_args, "--no-anilist")
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "manifest"))
        self.assertEqual(out["data"]["counts"], {"reacquirable": 1, "partial": 1})
        self.assertTrue(out["data"]["policy_complete"])
        code, out = self.cli("manifest", "show", "--library", "home", "--status", "partial")
        self.assertEqual(code, 0)
        self.assertEqual([t["title"] for t in out["data"]["titles"]], ["main/Kura Extra Show"])
        self.assertIn("uncovered_files", out["data"]["titles"][0]["reasons"])
        self.assertTrue(Path(self.home, ".local/share/kura/state.sqlite3").exists())

    def test_anilist_is_on_by_default_and_off_with_flag(self):
        resolver = self.fake_resolver()
        code, out = self.cli(*self.build_args, "--no-anilist", resolver=resolver)
        self.assertEqual((code, out["data"]["anilist"]["enabled"]), (0, False))
        resolver.assert_not_called()
        code, out = self.cli(*self.build_args, resolver=resolver)
        resolver.assert_called_once()      # wired to the shared limiter, nothing else
        self.assertEqual(list(resolver.call_args.kwargs), ["limiter"])
        self.assertEqual((resolver.call_args.kwargs["limiter"].service, resolver.call_args.args), ("anilist", ()))
        self.assertEqual((code, out["data"]["anilist"]["enabled"], out["data"]["anilist"]["unresolved"]),
                         (0, True, 2))

    def test_errors(self):
        code, out = self.cli("manifest", "show", "--library", "never-built")
        self.assertEqual((code, out["error"]["code"]), (2, "not_found"))
        code, out = self.cli("manifest", "show", "--library", "home", "--status", "bogus")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out = self.cli("manifest", "build", "--library", "home")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        bad = list(self.build_args)
        bad[bad.index("--protected") + 1] = str(self.lab.tmp / "none")
        code, out = self.cli(*bad)
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_policy"))
        code, out = self.cli(*self.build_args, "--max-read-gib", "-1", "--no-anilist")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        bad = list(self.build_args)
        bad[bad.index("--torrents") + 1] = str(self.lab.tmp / "nowhere")
        code, out = self.cli(*bad, "--no-anilist")
        self.assertEqual((code, out["error"]["code"]), (2, "not_found"))

    def test_interrupt_is_reported_as_130(self):
        with patch("kura.manifest.verify_tree", side_effect=KeyboardInterrupt):
            code, out = self.cli(*self.build_args, "--no-anilist")
        self.assertEqual((code, out["error"]["code"]), (130, "interrupted"))

    def test_concurrent_build_is_refused_as_busy(self):
        self.cli("manifest", "show", "--library", "x")         # creates the state directory
        held = Store(Path(self.home, ".local", "share", "kura"))
        self.addCleanup(held.db.close)
        with held.lock():
            code, out = self.cli(*self.build_args, "--no-anilist")
        self.assertEqual((code, out["error"]["code"]), (2, "busy"))


if __name__ == "__main__":
    unittest.main()
