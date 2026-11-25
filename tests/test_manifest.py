"""Matching, title status, manifest build/show and AniList pacing. Synthetic data only."""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

import torrentgen
from kura import manifest
from kura.anilist import AniList, Identity, fetch
from kura.inventory import scan
from kura.manifest_store import ManifestRepo
from kura.match import match, match_many
from kura.models import KuraError
from kura.protect import load_policy
from kura.store import Store
from kura.torrentmeta import parse

P = torrentgen.payload
GOOD = [("ep01.mkv", P("a", 30000)), ("ep02.mkv", P("b", 20000))]


def snapshot(root: Path):
    out = {}
    for path in sorted([root, *root.rglob("*")]):
        st = path.lstat()
        out[str(path.relative_to(root))] = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns)
    return out


class Lab:
    """A temp library, a torrents directory, a state store and policy files."""

    def __init__(self, test):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib, self.torrents, self.state = self.tmp / "lib", self.tmp / "torrents", self.tmp / "state"
        self.lib.mkdir()
        self.torrents.mkdir()
        self.store = Store(self.state)
        test.addCleanup(self.store.db.close)
        self.deny, self.prot = self.tmp / "deny.txt", self.tmp / "protected.txt"
        self.deny.write_text("# hosts\nblocked.example\n")
        self.prot.write_text("# nothing yet\n")

    def add(self, name, files=None, *, torrent=True, where=None, **kw):
        built = torrentgen.build(name, files, **kw)
        built.write_tree(where or self.lib)
        if torrent:
            (self.torrents / f"{name}.torrent").write_bytes(built.data)
        return built

    def policy(self):
        return load_policy(self.deny, self.prot)

    def build(self, **kw):
        kw.setdefault("policy", self.policy())
        return manifest.build(self.store, roots={"main": self.lib}, library="home",
                              torrents_dir=self.torrents, **kw)

    def show(self, **kw):
        return manifest.show(self.store, "home", **kw)

    def status(self, title, **kw):
        rows = {t["title"]: t for t in self.show(**kw)["titles"]}
        return rows[f"main/{title}"]


class Matching(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)

    def inv(self):
        return scan(self.lab.lib, "main")

    def test_root_dir_nested_dir_and_file_name(self):
        pack = self.lab.add("Kura Pack", GOOD)
        single = self.lab.add("[Kura] Episode - 01.mkv", size=30000,
                              where=self.lab.lib / "Kura Folder")
        deep = self.lab.add("[Kura] Episode - 02.mkv", size=30000,
                            where=self.lab.lib / "Kura Deep" / "extras" / "more")
        loose = self.lab.add("Loose Movie.mkv", size=20000)
        metas = [parse(b.data) for b in (pack, single, deep, loose)]
        matches, unmatched = match(self.inv(), metas)
        self.assertEqual(unmatched, [])
        by = {m.infohash: m for m in matches}
        self.assertEqual(by[pack.infohash].strategy, "root_dir")
        self.assertEqual(by[pack.infohash].mapping[0], ("Kura Pack", "ep01.mkv"))
        self.assertEqual(by[single.infohash].strategy, "nested_dir")
        self.assertEqual(by[single.infohash].title_rel, "Kura Folder")
        self.assertEqual(by[single.infohash].mapping, {0: ("Kura Folder", "[Kura] Episode - 01.mkv")})
        self.assertEqual(by[deep.infohash].strategy, "file_name")
        self.assertEqual(by[deep.infohash].title_rel, "Kura Deep")
        self.assertEqual(by[deep.infohash].mapping[0],
                         ("Kura Deep", "extras", "more", "[Kura] Episode - 02.mkv"))
        self.assertEqual(by[loose.infohash].strategy, "root_dir")
        self.assertEqual(by[loose.infohash].mapping, {0: ("Loose Movie.mkv",)})

    def test_ambiguity_and_no_match(self):
        self.lab.add("Kura Pack", GOOD)
        built = self.lab.add("Kura Pack", GOOD, where=self.lab.lib / "Kura Other")
        other = self.lab.add("Kura Missing", [("m1.mkv", P("m1", 30000)), ("m2.mkv", P("m2", 20000))],
                             torrent=False)
        shutil.rmtree(self.lab.lib / "Kura Missing")
        matches, unmatched = match(self.inv(), [parse(built.data)])
        self.assertEqual(matches, [])
        self.assertEqual(unmatched[0]["reason"], "ambiguous")
        matches, unmatched = match(self.inv(), [parse(other.data)])
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "no_match"))

    def test_single_file_torrent_never_matches_a_directory_and_vice_versa(self):
        self.lab.add("Kura Pack", GOOD, torrent=False)
        single = torrentgen.build("Kura Pack", size=20000)
        matches, unmatched = match(self.inv(), [parse(single.data)])
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "no_match"))

    def test_same_name_in_two_roots_is_ambiguous(self):
        other = self.lab.tmp / "lib2"
        other.mkdir()
        built = self.lab.add("Kura Pack", GOOD)
        built.write_tree(other)
        invs = [scan(self.lab.lib, "main"), scan(other, "spare")]
        matches, unmatched = match_many(invs, [parse(built.data)])
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "ambiguous"))
        matches, _ = match_many(invs[:1], [parse(built.data)])
        self.assertEqual(matches[0].root, "main")


class ScanTorrents(unittest.TestCase):
    def test_symlink_and_non_torrent_ignored(self):
        lab = Lab(self)
        built = lab.add("Kura Pack", GOOD)
        (lab.torrents / "notes.txt").write_text("hello")
        os.symlink(lab.torrents / "Kura Pack.torrent", lab.torrents / "link.torrent")
        (lab.torrents / "dir.torrent").mkdir()
        items = manifest.scan_torrents(lab.torrents)
        self.assertEqual([n for n, _ in items], ["Kura Pack.torrent"])
        self.assertEqual(items[0][1], built.data)
        report = {n: r for n, d, r in manifest.iter_torrents(lab.torrents) if d is None}
        self.assertEqual(report, {"dir.torrent": "not_regular", "link.torrent": "symlink"})

    def test_limits_and_unsafe_directory(self):
        lab = Lab(self)
        lab.add("Kura Pack", GOOD)
        lab.add("Kura Pack Two", GOOD)
        with self.assertRaises(KuraError) as caught:
            manifest.scan_torrents(lab.torrents, max_files=1)
        self.assertEqual(caught.exception.code, "torrent_dir_limit")
        self.assertEqual(manifest.scan_torrents(lab.torrents, max_bytes=10), [])
        link = lab.tmp / "linked"
        os.symlink(lab.torrents, link)
        with self.assertRaises(KuraError) as caught:
            manifest.scan_torrents(link)
        self.assertEqual(caught.exception.code, "unsafe_path")

    def test_read_file_refuses_symlink_and_big_file(self):
        lab = Lab(self)
        built = lab.add("Kura Pack", GOOD)
        path = lab.torrents / "Kura Pack.torrent"
        os.symlink(path, lab.tmp / "alias.torrent")
        self.assertEqual(manifest.read_file(path), built.data)
        with self.assertRaises(KuraError) as caught:
            manifest.read_file(lab.tmp / "alias.torrent")
        self.assertEqual(caught.exception.code, "unsafe_path")
        with self.assertRaises(KuraError) as caught:
            manifest.read_file(path, 10)
        self.assertEqual(caught.exception.code, "file_too_large")


class Build(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        self.public = self.lab.add("Kura Sample Series", GOOD, announce="http://tracker.example/announce")
        self.secret = self.lab.add("Kura Private Show", GOOD, private=True,
                                   announce="http://private.example/announce?passkey=SECRETKEY")
        self.extra = self.lab.add("Kura Extra Show", GOOD)
        (self.lab.lib / "Kura Extra Show" / "notes.txt").write_text("extra file")
        self.lab.add("Kura Orphan", GOOD, torrent=False)

    def test_end_to_end_statuses(self):
        before = snapshot(self.lab.lib), snapshot(self.lab.torrents)
        out = self.lab.build()
        self.assertEqual((snapshot(self.lab.lib), snapshot(self.lab.torrents)), before)
        self.assertEqual(out["counts"], {"reacquirable": 1, "protected": 1, "partial": 1, "unmatched": 1})
        self.assertEqual(out["matched"], 3)
        self.assertEqual(out["verification"]["run"], 2)          # the private one is never read
        self.assertEqual(out["verification"]["skipped_protected"], 1)
        self.assertFalse(out["interrupted"])
        ok = self.lab.status("Kura Sample Series")
        self.assertEqual((ok["status"], ok["reasons"]), ("reacquirable", []))
        self.assertEqual(ok["coverage"], {"files": 2, "covered": 2, "verified": 2, "uncovered": 0})
        self.assertEqual(ok["torrents"][0]["verdict"], "complete")
        self.assertTrue(ok["torrents"][0]["magnet"].startswith("magnet:?xt=urn:btih:"))
        priv = self.lab.status("Kura Private Show")
        self.assertEqual(priv["status"], "protected")
        self.assertIn("private_flag", priv["reasons"])
        self.assertIsNone(priv["torrents"][0]["magnet"])
        self.assertTrue(priv["torrents"][0]["protected"])
        self.assertIsNone(priv["torrents"][0]["verdict"])
        extra = self.lab.status("Kura Extra Show")
        self.assertEqual(extra["status"], "partial")
        self.assertIn("uncovered_files", extra["reasons"])
        self.assertEqual(extra["coverage"]["uncovered"], 1)
        self.assertEqual(self.lab.status("Kura Orphan")["reasons"], ["no_torrent_linked"])

    def test_no_credential_ever_reaches_state_or_output(self):
        out = self.lab.build()
        text = json.dumps(out) + json.dumps(self.lab.show())
        dump = "\n".join(self.lab.store.db.iterdump())
        for blob in (text, dump):
            self.assertNotIn("SECRETKEY", blob)
            self.assertNotIn("passkey", blob)
            self.assertNotIn("private.example", blob)
        row = ManifestRepo(self.lab.store.db).torrent(self.secret.infohash)
        self.assertIsNone(row["magnet"])
        self.assertIsNone(row["info"])
        pub = ManifestRepo(self.lab.store.db).torrent(self.public.infohash)
        self.assertIsNotNone(pub["info"])
        self.assertIn("tracker.example", pub["magnet"])
        self.assertEqual(pub["protection"]["policy_sha256"], self.lab.policy().sha256)

    def test_cache_hit_invalidation_and_reverify(self):
        self.lab.build()
        again = self.lab.build()
        self.assertEqual((again["verification"]["run"], again["verification"]["cached"]), (0, 2))
        target = self.lab.lib / "Kura Sample Series" / "ep01.mkv"
        stat_ = target.stat()
        os.utime(target, ns=(stat_.st_atime_ns, stat_.st_mtime_ns + 5_000_000_000))
        touched = self.lab.build()
        self.assertEqual((touched["verification"]["run"], touched["verification"]["cached"]), (1, 1))
        forced = self.lab.build(reverify=True)
        self.assertEqual((forced["verification"]["run"], forced["verification"]["cached"]), (2, 0))
        self.assertEqual(self.lab.status("Kura Sample Series")["status"], "reacquirable")

    def test_corrupted_file_is_not_reacquirable_and_failure_is_cached(self):
        target = self.lab.lib / "Kura Sample Series" / "ep02.mkv"
        data = bytearray(target.read_bytes())
        data[19000] ^= 0xFF
        target.write_bytes(bytes(data))
        out = self.lab.build()
        row = self.lab.status("Kura Sample Series")
        self.assertEqual(row["status"], "partial")
        self.assertIn("piece_failed", row["reasons"])
        self.assertEqual(out["counts"].get("reacquirable", 0), 0)
        self.assertEqual(self.lab.build()["verification"]["cached"], 2)

    def test_changed_file_without_reverification_is_stale(self):
        self.lab.build()
        target = self.lab.lib / "Kura Sample Series" / "ep01.mkv"
        target.write_bytes(P("z", 30000))
        out = self.lab.build(max_read_bytes=0)
        self.assertEqual(out["verification"]["deferred"], 1)       # the extra-file title is cached
        row = self.lab.status("Kura Sample Series")
        self.assertEqual((row["status"], row["reasons"]), ("stale", ["evidence_changed"]))
        self.assertNotIn("reacquirable", self.lab.build()["counts"])   # content really differs

    def test_budget_exhausted_is_unverified_then_completed(self):
        out = self.lab.build(max_read_bytes=10_000)
        self.assertEqual(out["verification"]["bytes_read"], 10_000)
        self.assertEqual(out["verification"]["deferred"], 1)
        waiting = self.lab.status("Kura Sample Series")             # never reached: no verification
        self.assertEqual((waiting["status"], waiting["reasons"]), ("unverified", ["not_verified"]))
        self.assertIn("verification_incomplete", self.lab.status("Kura Extra Show")["reasons"])
        self.assertEqual(self.lab.show()["counts"].get("reacquirable", 0), 0)
        done = self.lab.build()
        self.assertEqual(done["counts"]["reacquirable"], 1)

    def test_vanished_title_becomes_stale(self):
        self.lab.build()
        shutil.rmtree(self.lab.lib / "Kura Sample Series")
        self.lab.build()
        row = self.lab.status("Kura Sample Series")
        self.assertEqual((row["status"], row["reasons"]), ("stale", ["absent_from_latest_scan"]))
        self.assertEqual(row["files"], 0)

    def test_blocked_title_is_never_reacquirable(self):
        os.symlink(self.lab.tmp, self.lab.lib / "Kura Sample Series" / "shortcut")
        self.lab.build()
        row = self.lab.status("Kura Sample Series")
        self.assertEqual((row["status"], row["reasons"]), ("blocked", ["blocked:symlink"]))

    def test_policy_incomplete_downgrades(self):
        self.lab.build(policy=load_policy(None, None))
        row = self.lab.status("Kura Sample Series")
        self.assertEqual((row["status"], row["reasons"]), ("unverified", ["policy_incomplete"]))
        self.assertFalse(self.lab.build(policy=load_policy(self.lab.deny, None))["policy_complete"])

    def test_denylisted_tracker_and_protected_list_and_path(self):
        self.lab.add("Kura Blocked Show", GOOD, announce="http://tracker.blocked.example/announce")
        self.lab.add("Kura Listed Show", GOOD)
        self.lab.add("Kura Path Show", GOOD, torrent=False)
        (self.lab.lib / "Kura Path Show" / "ep01.mkv").write_bytes(b"x")
        listed = parse((self.lab.torrents / "Kura Listed Show.torrent").read_bytes())
        self.lab.prot.write_text(f"{listed.infohash}\npath:Kura Path Show\npath:Kura Sample Series/ep01.mkv\n")
        self.lab.build()
        self.assertIn("tracker_denylist", self.lab.status("Kura Blocked Show")["reasons"])
        self.assertIn("protected_infohash", self.lab.status("Kura Listed Show")["reasons"])
        self.assertEqual(self.lab.status("Kura Path Show")["reasons"], ["path_protected"])
        inside = self.lab.status("Kura Sample Series")
        self.assertEqual((inside["status"], inside["reasons"]), ("protected", ["path_protected"]))

    def test_unparsable_and_duplicate_descriptors_are_reported_not_linked(self):
        (self.lab.torrents / "broken.torrent").write_bytes(b"not bencode")
        (self.lab.torrents / "copy of public.torrent").write_bytes(self.public.data)
        os.symlink(self.lab.torrents / "Kura Sample Series.torrent", self.lab.torrents / "ln.torrent")
        out = self.lab.build()
        reasons = {s["file"]: s["reason"] for s in out["torrents"]["skipped"]}
        self.assertEqual(reasons["ln.torrent"], "symlink")
        self.assertIn("broken.torrent", reasons)
        self.assertEqual(out["torrents"]["duplicates"], 1)
        self.assertEqual(out["torrents"]["parsed"], 3)

    def test_ambiguous_torrent_is_unmatched(self):
        self.lab.add("Kura Sample Series", GOOD, torrent=False, where=self.lab.lib / "Kura Elsewhere")
        out = self.lab.build()
        self.assertIn({"infohash": self.public.infohash, "name": "Kura Sample Series",
                       "reason": "ambiguous"}, out["unmatched"])
        self.assertEqual(self.lab.status("Kura Sample Series")["status"], "unmatched")

    def test_interrupt_keeps_committed_torrents_and_releases_the_lock(self):
        real, calls = manifest.verify_tree, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with patch("kura.manifest.verify_tree", flaky), self.assertRaises(KeyboardInterrupt):
            self.lab.build()
        repo = ManifestRepo(self.lab.store.db)
        done = [t for t in repo.titles("home") if repo.latest_verification(t["id"], self.extra.infohash)
                or repo.latest_verification(t["id"], self.public.infohash)]
        self.assertEqual(len(done), 1)
        self.assertFalse(self.lab.store.db.in_transaction)
        resumed = self.lab.build()
        self.assertEqual((resumed["verification"]["cached"], resumed["verification"]["run"]), (1, 1))
        self.assertEqual(resumed["counts"]["reacquirable"], 1)

    def test_should_stop_marks_build_interrupted(self):
        out = self.lab.build(should_stop=lambda: True)
        self.assertTrue(out["interrupted"])
        self.assertEqual(out["verification"]["run"], 0)
        self.assertNotIn("reacquirable", out["counts"])

    def test_show_filters_and_errors(self):
        self.lab.build()
        only = self.lab.show(status="protected")
        self.assertEqual([t["title"] for t in only["titles"]], ["main/Kura Private Show"])
        self.assertEqual(only["counts"]["reacquirable"], 1)       # counts ignore the filter
        one = self.lab.show(title="main/Kura Orphan")
        self.assertEqual(len(one["titles"]), 1)
        with self.assertRaises(KuraError) as caught:
            self.lab.show(status="bogus")
        self.assertEqual(caught.exception.code, "invalid_arguments")
        with self.assertRaises(KuraError) as caught:
            manifest.show(self.lab.store, "nope")
        self.assertEqual(caught.exception.code, "not_found")

    def test_input_validation(self):
        for kw in ({"library": "bad/name"}, {"roots": {}}, {"roots": {"a b": self.lab.lib}},
                   {"roots": {"main": Path("relative")}}, {"max_read_bytes": -1}):
            args = {"roots": {"main": self.lab.lib}, "library": "home",
                    "torrents_dir": self.lab.torrents, "policy": self.lab.policy(), **kw}
            with self.assertRaises(KuraError):
                manifest.build(self.lab.store, **args)

    def test_removed_torrent_stops_covering_but_keeps_protecting(self):
        self.lab.build()
        (self.lab.torrents / "Kura Sample Series.torrent").unlink()
        (self.lab.torrents / "Kura Private Show.torrent").unlink()
        self.lab.build()
        gone = self.lab.status("Kura Sample Series")
        self.assertEqual((gone["status"], gone["reasons"]), ("unmatched", ["no_torrent_linked"]))
        self.assertEqual(self.lab.status("Kura Private Show")["status"], "protected")

    def test_obsolete_links_are_superseded_before_the_inventory_is_published(self):
        self.lab.build()
        (self.lab.torrents / "Kura Sample Series.torrent").unlink()
        with patch.object(ManifestRepo, "save_inventory", side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            self.lab.build()
        row = self.lab.status("Kura Sample Series")
        self.assertEqual((row["status"], row["reasons"]), ("unmatched", ["no_torrent_linked"]))

    def test_roots_not_scanned_this_time_keep_their_links(self):
        self.lab.build()
        other = self.lab.tmp / "lib2"
        other.mkdir()
        manifest.build(self.lab.store, roots={"spare": other}, library="home",
                       torrents_dir=self.lab.torrents, policy=self.lab.policy())
        self.assertEqual(self.lab.status("Kura Sample Series")["status"], "reacquirable")

    def test_ambiguity_after_a_clean_build_supersedes_the_old_link(self):
        self.lab.build()
        self.lab.add("Kura Sample Series", GOOD, torrent=False, where=self.lab.lib / "Kura Elsewhere")
        self.lab.build()
        self.assertEqual(self.lab.status("Kura Sample Series")["status"], "unmatched")

    def test_policy_change_applies_even_if_the_build_is_interrupted(self):
        self.lab.build()
        self.lab.deny.write_text("tracker.example\n")
        self.lab.build(should_stop=lambda: True)               # descriptors reclassified up front
        row = self.lab.status("Kura Sample Series")
        self.assertEqual(row["status"], "protected")
        self.assertIn("tracker_denylist", row["reasons"])

    def test_title_assessed_under_a_new_policy_needs_reclassified_torrents(self):
        self.lab.build()
        repo = ManifestRepo(self.lab.store.db)
        title = next(t for t in repo.titles("home") if t["rel_path"] == "main/Kura Sample Series")
        stale = {**title["identity"]["assessment"], "policy_sha256": "0" * 64}
        repo.save_inventory("home", title["root_dev"], [{
            "rel_path": title["rel_path"], "kind": title["kind"], "total_bytes": title["total_bytes"],
            "newest_mtime_ns": title["newest_mtime_ns"], "files": repo.title_files(title["id"]),
            "identity": {"anilist": None, "assessment": stale}}], title["seen_at"])
        row = self.lab.status("Kura Sample Series")
        self.assertEqual((row["status"], row["reasons"]), ("unverified", ["not_reclassified"]))

    def test_duplicate_descriptor_with_denylisted_tracker_protects_the_torrent(self):
        mirror = torrentgen.build("Kura Sample Series", GOOD, announce="http://tracker.blocked.example/announce")
        self.assertEqual(mirror.infohash, self.public.infohash)  # announce data is outside the infohash
        (self.lab.torrents / "a mirror.torrent").write_bytes(mirror.data)   # sorts after the public copy
        (self.lab.torrents / "Kura Sample Series (copy).torrent").write_bytes(mirror.data)
        out = self.lab.build()
        self.assertEqual(out["torrents"]["duplicates"], 2)
        row = self.lab.status("Kura Sample Series")
        self.assertEqual(row["status"], "protected")
        self.assertIn("tracker_denylist", row["reasons"])
        stored = ManifestRepo(self.lab.store.db).torrent(self.public.infohash)
        self.assertIsNone(stored["magnet"])
        self.assertIsNone(stored["info"])

    def test_new_protection_is_recorded_before_an_interrupt_can_expose_old_links(self):
        self.lab.build()
        mirror = torrentgen.build("Kura Sample Series", GOOD, announce="http://tracker.blocked.example/announce")
        (self.lab.torrents / "Kura Sample Series.torrent").write_bytes(mirror.data)
        self.lab.build(should_stop=lambda: True)
        row = self.lab.status("Kura Sample Series")
        self.assertEqual(row["status"], "protected")
        self.assertIn("tracker_denylist", row["reasons"])

    def test_protection_shared_across_libraries_is_never_silently_cleared(self):
        mirror = torrentgen.build("Kura Sample Series", GOOD, announce="http://tracker.blocked.example/announce")
        (self.lab.torrents / "Kura Sample Series.torrent").write_bytes(mirror.data)
        self.lab.build()                                       # library "home": protected descriptor
        other_lib, other_torrents = self.lab.tmp / "lib2", self.lab.tmp / "torrents2"
        other_lib.mkdir()
        other_torrents.mkdir()
        self.public.write_tree(other_lib)
        (other_torrents / "x.torrent").write_bytes(self.public.data)    # public copy, same infohash
        manifest.build(self.lab.store, roots={"main": other_lib}, library="second",
                       torrents_dir=other_torrents, policy=self.lab.policy())
        row = {t["title"]: t for t in manifest.show(self.lab.store, "second")["titles"]}["main/Kura Sample Series"]
        self.assertEqual(row["status"], "protected")
        self.assertIn("conflicting_descriptor", row["reasons"])
        self.assertEqual(self.lab.status("Kura Sample Series")["status"], "protected")

    def test_only_a_complete_torrent_makes_its_files_reacquirable(self):
        pack = self.lab.add("Kura Pad Pack", GOOD, pad=True)
        (self.lab.lib / "Kura Pad Pack" / "ep02.mkv").unlink()    # title now holds ep01 only
        self.lab.build()
        row = self.lab.status("Kura Pad Pack")
        self.assertEqual(row["torrents"][0]["verdict"], "partial")
        self.assertEqual(row["coverage"]["verified"], 0)
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["reasons"], ["torrent_incomplete"])
        self.assertEqual(pack.infohash, row["torrents"][0]["infohash"])


class Identity_(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        self.lab.add("[Kura] Kura Sample Series (BD 1080p)", GOOD)
        self.lab.add("Kura Orphan", GOOD, torrent=False)

    def test_aliases_off_means_no_lookup(self):
        resolver = Mock()
        out = self.lab.build()
        resolver.resolve.assert_not_called()
        self.assertEqual(out["anilist"], {"enabled": False, "resolved": 0, "unavailable": 0, "unresolved": 0})

    def test_resolved_identity_is_recorded_and_survives_a_rebuild(self):
        resolver = Mock()
        resolver.resolve.return_value = (Identity(4242, ("Kura Sample Series",), "Kura Sample Series"), "resolved")
        out = self.lab.build(aliases=resolver)
        self.assertEqual(out["anilist"]["resolved"], 1)
        self.assertEqual(resolver.resolve.call_args.args[0], "Kura Sample Series")  # tags stripped
        row = self.lab.status("[Kura] Kura Sample Series (BD 1080p)")
        self.assertEqual(row["anilist_id"], 4242)
        self.lab.build()                                            # aliases off: identity kept
        self.assertEqual(self.lab.status("[Kura] Kura Sample Series (BD 1080p)")["anilist_id"], 4242)
        self.assertEqual(resolver.resolve.call_count, 1)            # unlinked titles never queried

    def test_resolver_failure_never_fails_the_build(self):
        for error in (KuraError("alias_unavailable", "rate limited", status=429),
                      KuraError("something_else", "boom")):
            resolver = Mock()
            resolver.resolve.side_effect = error
            out = self.lab.build(aliases=resolver, reverify=False)
            self.assertEqual(out["anilist"]["unavailable"], 1)
            self.assertEqual(out["counts"]["reacquirable"], 1)
            self.assertIsNone(self.lab.status("[Kura] Kura Sample Series (BD 1080p)")["anilist_id"])


class TitleStatus(unittest.TestCase):
    def base(self, **identity):
        assessment = {"policy_complete": True, "path_protected": False, "policy_sha256": "x"}
        return {"blocked": [], "identity": {"assessment": {**assessment, **identity}}}

    def test_unreadable_protection_record_fails_closed(self):
        link = {"infohash": "a" * 40, "strategy": "root_dir", "mapping": {"0": ["T", "f"]}}
        files = [{"rel_path": "T/f", "size": 1, "mtime_ns": 1, "dev": 1, "ino": 1}]
        for torrents in ({}, {"a" * 40: {"protection": {}, "private": False}},
                         {"a" * 40: {"protection": {"protected": False}, "private": None}}):
            got = manifest.title_status(self.base(), files, [link], {"a" * 40: None}, torrents)
            self.assertEqual((got["status"], got["reasons"]), ("protected", ["protection_unknown"]))

    def test_missing_assessment_cannot_be_reacquirable(self):
        link = {"infohash": "a" * 40, "strategy": "root_dir", "mapping": {"0": ["T", "f"]}}
        files = [{"rel_path": "T/f", "size": 1, "mtime_ns": 1, "dev": 1, "ino": 1}]
        verification = {"verdict": "complete", "files": [
            {"index": 0, "local": "T/f", "status": "verified",
             "evidence": {"size": 1, "mtime_ns": 1, "dev": 1, "ino": 1}}]}
        torrent = {"protection": {"protected": False, "reasons": [], "policy_sha256": "x"}, "private": False}
        title = {"blocked": [], "identity": None}
        got = manifest.title_status(title, files, [link], {"a" * 40: verification}, {"a" * 40: torrent})
        self.assertEqual((got["status"], got["reasons"]), ("unverified", ["not_reclassified"]))
        got = manifest.title_status(self.base(policy_complete=False), files, [link], {"a" * 40: verification},
                                    {"a" * 40: torrent})
        self.assertEqual((got["status"], got["reasons"]), ("unverified", ["policy_incomplete"]))
        got = manifest.title_status(self.base(), files, [link], {"a" * 40: verification}, {"a" * 40: torrent})
        self.assertEqual(got["status"], "reacquirable")
        got = manifest.title_status(self.base(), files, [link], {"a" * 40: None}, {"a" * 40: torrent})
        self.assertEqual((got["status"], got["reasons"]), ("unverified", ["not_verified"]))

    def test_incomplete_verification_is_never_reacquirable(self):
        link = {"infohash": "a" * 40, "strategy": "root_dir", "mapping": {"0": ["T", "f"]}}
        files = [{"rel_path": "T/f", "size": 1, "mtime_ns": 1, "dev": 1, "ino": 1}]
        torrent = {"protection": {"protected": False, "reasons": [], "policy_sha256": "x"}, "private": False}
        verification = {"verdict": "interrupted", "files": [
            {"index": 0, "local": "T/f", "status": "unverified", "evidence": None}]}
        got = manifest.title_status(self.base(), files, [link], {"a" * 40: verification}, {"a" * 40: torrent})
        self.assertEqual(got["status"], "stale")        # file exists now, was not seen then
        verification["files"][0]["evidence"] = {"size": 1, "mtime_ns": 1, "dev": 1, "ino": 1}
        got = manifest.title_status(self.base(), files, [link], {"a" * 40: verification}, {"a" * 40: torrent})
        self.assertEqual(got["status"], "unverified")
        self.assertIn("verification_incomplete", got["reasons"])


class Clock:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


BODY = json.dumps({"data": {"Page": {"media": [{
    "id": 7, "title": {"romaji": "Kura Sample Series", "english": None, "native": None},
    "synonyms": []}]}}}).encode()


def limited(retry_after="7"):
    return KuraError("alias_unavailable", "limit", status=429, **{"Retry-After": retry_after})


class AniListPacing(unittest.TestCase):
    def make(self, transport, **kw):
        clock = Clock()
        return AniList(transport=transport, sleep=clock.sleep, clock=clock, wall=lambda: 5000.0, **kw), clock

    def test_requests_are_paced_to_thirty_a_minute(self):
        client, clock = self.make(Mock(return_value=(BODY, {})))
        for _ in range(3):
            self.assertEqual(client.resolve("Kura Sample Series")[1], "resolved")
        self.assertEqual(clock.slept, [2.0, 2.0])

    def test_retry_after_is_honoured_then_succeeds(self):
        transport = Mock(side_effect=[limited("7"), (BODY, {})])
        client, clock = self.make(transport)
        self.assertEqual(client.resolve("Kura Sample Series")[1], "resolved")
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(clock.slept, [7.0])

    def test_long_window_gives_up_and_never_hammers(self):
        transport = Mock(side_effect=limited("600"))
        client, clock = self.make(transport, max_wait=30)
        with self.assertRaises(KuraError) as caught:
            client.resolve("Kura Sample Series")
        self.assertEqual(caught.exception.code, "alias_unavailable")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(clock.slept, [])
        for _ in range(5):
            with self.assertRaises(KuraError):
                client.resolve("Another Title")
        self.assertEqual(transport.call_count, 1)              # window still open: no requests
        clock.now += 601
        transport.side_effect = [(BODY, {})]
        self.assertEqual(client.resolve("Kura Sample Series")[1], "resolved")

    def test_repeated_429_is_bounded(self):
        transport = Mock(side_effect=limited("1"))
        client, clock = self.make(transport, max_retries=2)
        with self.assertRaises(KuraError):
            client.resolve("Kura Sample Series")
        self.assertEqual(transport.call_count, 3)

    def test_remaining_zero_defers_until_reset(self):
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5010"}
        transport = Mock(return_value=(BODY, headers))
        client, clock = self.make(transport)
        client.resolve("Kura Sample Series")
        client.resolve("Kura Sample Series")
        self.assertEqual(clock.slept, [10.0])

    def test_lower_limit_applies_to_the_very_next_request(self):
        transport = Mock(return_value=(BODY, {"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "5"}))
        client, clock = self.make(transport, max_wait=120)
        client.resolve("Kura Sample Series")
        client.resolve("Kura Sample Series")
        self.assertEqual(clock.slept, [60.0])

    def test_max_wait_bounds_the_whole_lookup_across_retries(self):
        transport = Mock(side_effect=limited("20"))
        client, clock = self.make(transport, max_wait=30, max_retries=5)
        with self.assertRaises(KuraError):
            client.resolve("Kura Sample Series")
        self.assertLessEqual(sum(clock.slept), 30)
        self.assertEqual(transport.call_count, 2)           # 20 s slept once, a second 20 s would exceed 30

    def test_lower_advertised_limit_slows_the_pace(self):
        transport = Mock(return_value=(BODY, {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"}))
        client, clock = self.make(transport)
        client.resolve("Kura Sample Series")
        client.resolve("Kura Sample Series")
        client.resolve("Kura Sample Series")
        self.assertAlmostEqual(clock.slept[-1], 6.0)

    def test_header_limit_widens_pace_but_is_capped(self):
        transport = Mock(return_value=(BODY, {"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "80"}))
        client, clock = self.make(transport)
        client.resolve("Kura Sample Series")
        client.resolve("Kura Sample Series")
        self.assertAlmostEqual(clock.slept[-1], 2.0)           # first request already reserved 2s
        client.resolve("Kura Sample Series")
        self.assertAlmostEqual(clock.slept[-1], 60 / 90)

    def test_other_errors_are_not_retried(self):
        transport = Mock(side_effect=KuraError("alias_unavailable", "down"))
        client, _ = self.make(transport)
        with self.assertRaises(KuraError):
            client.resolve("Kura Sample Series")
        self.assertEqual(transport.call_count, 1)

    def test_fetch_translates_http_429(self):
        err = urllib.error.HTTPError("https://graphql.anilist.co/", 429, "Too Many", {"Retry-After": "9"}, None)
        self.addCleanup(err.close)
        opener = Mock()
        opener.open.side_effect = err
        with patch("kura.anilist.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.1.1.1", 443))]), \
             patch("kura.anilist.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(KuraError) as caught:
                fetch("dark item")
        self.assertEqual(caught.exception.code, "alias_unavailable")
        self.assertEqual(caught.exception.details["status"], 429)
        self.assertEqual(caught.exception.details["Retry-After"], "9")


if __name__ == "__main__":
    unittest.main()
