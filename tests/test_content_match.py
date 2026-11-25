"""Content-based torrent matching and repeatable --torrents. Synthetic data only."""
import contextlib
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import torrentgen
from kura import cli, match as match_module
from kura.inventory import scan
from kura.match import match, match_many
from kura.torrentmeta import parse
from test_manifest import GOOD, Lab

P = torrentgen.payload


def write(root: Path, rel: str, data: bytes):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


class ContentMatching(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)

    def inv(self):
        return scan(self.lab.lib, "main")

    def one(self, built):
        matches, unmatched = match(self.inv(), [parse(built.data)])
        return matches, unmatched

    def test_renamed_folder_matches_by_content(self):
        built = torrentgen.build("KuraSampleSeries", GOOD)
        for name, data in GOOD:
            write(self.lab.lib, f"Kura Sample Series/{name}", data)
        matches, unmatched = self.one(built)
        self.assertEqual(unmatched, [])
        m = matches[0]
        self.assertEqual((m.strategy, m.title_rel, m.root), ("content", "Kura Sample Series", "main"))
        self.assertEqual(m.mapping, {0: ("Kura Sample Series", "ep01.mkv"),
                                     1: ("Kura Sample Series", "ep02.mkv")})

    def test_name_strategies_keep_precedence(self):
        built = self.lab.add("Kura Pack", GOOD)
        write(self.lab.lib, "Other Folder/ep01.mkv", GOOD[0][1])    # a content decoy
        write(self.lab.lib, "Other Folder/ep02.mkv", GOOD[1][1])
        matches, _ = self.one(built)
        self.assertEqual((matches[0].strategy, matches[0].title_rel), ("root_dir", "Kura Pack"))

    def test_name_ambiguity_is_not_rescued_by_content(self):
        built = self.lab.add("Kura Pack", GOOD)
        built.write_tree(self.lab.lib / "Kura Other")
        matches, unmatched = self.one(built)
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "ambiguous"))

    def test_nested_extra_folder_level_and_different_layout(self):
        built = torrentgen.build("Kura Season", [("Season 1/ep01.mkv", GOOD[0][1]),
                                                 ("Season 1/ep02.mkv", GOOD[1][1])])
        write(self.lab.lib, "Kura Show/Kura Show S01 [1080p]/Disc/ep01.mkv", GOOD[0][1])
        write(self.lab.lib, "Kura Show/Kura Show S01 [1080p]/Disc/ep02.mkv", GOOD[1][1])
        matches, _ = self.one(built)
        self.assertEqual(matches[0].strategy, "content")
        self.assertEqual(matches[0].title_rel, "Kura Show")
        self.assertEqual(matches[0].mapping[1], ("Kura Show", "Kura Show S01 [1080p]", "Disc", "ep02.mkv"))

    def test_same_sizes_different_names_need_the_size_fallback(self):
        a, b = P("big-a", 30000), P("big-b", 45000)
        built = torrentgen.build("Kura Movies", [("one.mkv", a), ("two.mkv", b)])
        write(self.lab.lib, "Kura Library/renamed-1.mkv", a)
        write(self.lab.lib, "Kura Library/renamed-2.mkv", b)
        matches, unmatched = self.one(built)       # default threshold is 64 MiB: nothing here
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "no_match"))
        with patch.object(match_module, "SIZE_ONLY_MIN", 20000):
            matches, _ = self.one(built)
        self.assertEqual(matches[0].mapping, {0: ("Kura Library", "renamed-1.mkv"),
                                              1: ("Kura Library", "renamed-2.mkv")})
        self.assertEqual(match_module.SIZE_ONLY_MIN, 64 * 1024 * 1024)

    def test_size_fallback_needs_uniqueness_on_both_sides(self):
        a = P("dup", 30000)
        with patch.object(match_module, "SIZE_ONLY_MIN", 20000):
            # two local files of that size in the title: refuse to guess
            write(self.lab.lib, "Kura Twice/x.mkv", a)
            write(self.lab.lib, "Kura Twice/y.mkv", P("dup2", 30000))
            built = torrentgen.build("Kura Single", [("only.mkv", a), ("pad.bin", P("pad", 100))])
            self.assertEqual(self.one(built)[0], [])
            # two torrent files of that size, one local file
            write(self.lab.lib, "Kura Once/x.mkv", a)
            twin = torrentgen.build("Kura Twin", [("p.mkv", a), ("q.mkv", P("dup3", 30000))])
            self.assertEqual(self.one(twin)[0], [])

    def test_two_titles_with_the_same_files_are_ambiguous(self):
        built = torrentgen.build("Kura Renamed", GOOD)
        for title in ("Kura Copy One", "Kura Copy Two"):
            for name, data in GOOD:
                write(self.lab.lib, f"{title}/{name}", data)
        matches, unmatched = self.one(built)
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "ambiguous"))

    def test_better_coverage_beats_a_partial_copy(self):
        built = torrentgen.build("Kura Renamed", [("a.mkv", P("a", 40000)), ("b.mkv", P("b", 40000)),
                                                  ("c.mkv", P("c", 5000))])
        for title, names in (("Kura Full", ("a.mkv", "b.mkv", "c.mkv")), ("Kura Most", ("a.mkv", "b.mkv"))):
            for n in names:
                write(self.lab.lib, f"{title}/{n}", {"a.mkv": P("a", 40000), "b.mkv": P("b", 40000),
                                                     "c.mkv": P("c", 5000)}[n])
        matches, _ = self.one(built)
        self.assertEqual(matches[0].title_rel, "Kura Full")

    def test_coverage_below_ninety_percent_is_unmatched(self):
        built = torrentgen.build("Kura Renamed", [("a.mkv", P("a", 40000)), ("b.mkv", P("b", 5000))])
        write(self.lab.lib, "Kura Half/b.mkv", P("b", 5000))        # 5000 of 45000 bytes
        matches, unmatched = self.one(built)
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "no_match"))
        write(self.lab.lib, "Kura Half/a.mkv", P("a", 40000))
        self.assertEqual(len(self.one(built)[0]), 1)

    def test_exactly_ninety_percent_qualifies_and_missing_file_is_unmapped(self):
        built = torrentgen.build("Kura Renamed", [("a.mkv", P("a", 90000)), ("b.mkv", P("b", 10000))])
        write(self.lab.lib, "Kura Most/a.mkv", P("a", 90000))
        matches, _ = self.one(built)
        self.assertEqual(matches[0].mapping, {0: ("Kura Most", "a.mkv")})

    def test_same_basename_in_different_season_folders_use_path_suffix(self):
        s1, s2 = P("s1", 30000), P("s2", 30000)       # same size, same basename
        built = torrentgen.build("Kura Show", [("S1/ep01.mkv", s1), ("S2/ep01.mkv", s2)])
        write(self.lab.lib, "Kura Local/Season 1/S1/ep01.mkv", s1)
        write(self.lab.lib, "Kura Local/Season 2/S2/ep01.mkv", s2)
        matches, _ = self.one(built)
        self.assertEqual(matches[0].mapping, {0: ("Kura Local", "Season 1", "S1", "ep01.mkv"),
                                              1: ("Kura Local", "Season 2", "S2", "ep01.mkv")})

    def test_two_torrent_files_never_share_one_local_file(self):
        data = P("same", 30000)
        built = torrentgen.build("Kura Show", [("S1/ep01.mkv", data), ("S2/ep01.mkv", data)])
        write(self.lab.lib, "Kura Local/ep01.mkv", data)
        matches, unmatched = self.one(built)           # 50% covered at best, and nothing is shared
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "no_match"))

    def test_pad_files_do_not_count_and_are_unmapped(self):
        built = torrentgen.build("Kura Renamed", GOOD, pad=True)
        for name, data in GOOD:
            write(self.lab.lib, f"Kura Local/{name}", data)
        matches, _ = self.one(built)
        meta = parse(built.data)
        pads = {f.index for f in meta.files if f.pad}
        self.assertTrue(pads)
        self.assertTrue(pads.isdisjoint(matches[0].mapping))
        self.assertEqual(len(matches[0].mapping), 2)

    def test_single_file_torrent_matches_a_renamed_file_by_size_only(self):
        built = torrentgen.build("kura-movie.mkv", size=30000)
        write(self.lab.lib, "Kura Movie/Renamed.mkv", built.entries[0].data)
        write(self.lab.lib, "Kura Movie/Bonus.mkv", P("bonus", 1000))
        self.assertEqual(self.one(built)[0], [])      # below the 64 MiB size-only floor
        with patch.object(match_module, "SIZE_ONLY_MIN", 20000):
            matches, _ = self.one(built)
        self.assertEqual((matches[0].strategy, matches[0].mapping),
                         ("content", {0: ("Kura Movie", "Renamed.mkv")}))

    def test_zero_byte_torrent_is_unmatched(self):
        built = torrentgen.build("Kura Empty", [("a.txt", b"")])
        self.assertEqual(self.one(built)[0], [])

    def test_match_many_across_roots(self):
        other = self.lab.tmp / "lib2"
        other.mkdir()
        built = torrentgen.build("Kura Renamed", GOOD)
        for name, data in GOOD:
            write(other, f"Kura Spare/{name}", data)
        invs = [self.inv(), scan(other, "spare")]
        matches, _ = match_many(invs, [parse(built.data)])
        self.assertEqual((matches[0].root, matches[0].title_rel), ("spare", "Kura Spare"))
        for name, data in GOOD:
            write(self.lab.lib, f"Kura Main/{name}", data)
        matches, unmatched = match_many([self.inv(), scan(other, "spare")], [parse(built.data)])
        self.assertEqual((matches, unmatched[0]["reason"]), ([], "ambiguous"))


class ContentBuild(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)

    def test_renamed_torrent_becomes_reacquirable_after_verification(self):
        built = torrentgen.build("KuraSampleSeries", GOOD)
        (self.lab.torrents / "x.torrent").write_bytes(built.data)
        for name, data in GOOD:
            write(self.lab.lib, f"Kura Sample Series/{name}", data)
        out = self.lab.build()
        self.assertEqual(out["matched"], 1)
        self.assertEqual(out["matched_by"], {"content": 1})
        self.assertEqual(self.lab.status("Kura Sample Series")["status"], "reacquirable")

    def test_decoy_with_same_sizes_matches_then_fails_verification(self):
        decoy = torrentgen.build("Kura Decoy", [("ep01.mkv", P("x", 30000)), ("ep02.mkv", P("y", 20000))])
        (self.lab.torrents / "decoy.torrent").write_bytes(decoy.data)
        for name, data in GOOD:
            write(self.lab.lib, f"Kura Sample Series/{name}", data)
        out = self.lab.build()
        self.assertEqual(out["matched_by"], {"content": 1})
        row = self.lab.status("Kura Sample Series")
        self.assertNotEqual(row["status"], "reacquirable")
        self.assertEqual(row["torrents"][0]["verdict"], "failed")
        self.assertIn("piece_failed", row["reasons"])

    def test_missing_file_inside_threshold_is_not_reacquirable(self):
        big = [("a.mkv", P("a", 90000)), ("b.mkv", P("b", 10000))]
        (self.lab.torrents / "x.torrent").write_bytes(torrentgen.build("Kura Renamed", big).data)
        write(self.lab.lib, "Kura Most/a.mkv", big[0][1])
        self.lab.build()
        self.assertNotEqual(self.lab.status("Kura Most")["status"], "reacquirable")

    def test_repeatable_torrents_dirs_dedupe_by_infohash(self):
        second = self.lab.tmp / "resolved"
        second.mkdir()
        built = torrentgen.build("KuraSampleSeries", GOOD)
        (self.lab.torrents / "a.torrent").write_bytes(built.data)
        (second / f"{built.infohash}.torrent").write_bytes(built.data)
        other = torrentgen.build("Kura Other", [("o.mkv", P("o", 20000)), ("p.mkv", P("p", 20000))])
        (second / "other.torrent").write_bytes(other.data)
        for name, data in GOOD:
            write(self.lab.lib, f"Kura Sample Series/{name}", data)
        out = manifest_build(self.lab, [self.lab.torrents, second, self.lab.torrents])
        self.assertEqual(out["torrents"]["dirs"], 2)
        self.assertEqual((out["torrents"]["scanned"], out["torrents"]["parsed"],
                          out["torrents"]["duplicates"]), (3, 2, 1))
        self.assertEqual(self.lab.status("Kura Sample Series")["status"], "reacquirable")
        self.assertEqual(out["unmatched"][0]["name"], "Kura Other")

    def test_torrent_dirs_argument_is_validated(self):
        from kura.models import KuraError
        for bad in ([], [self.lab.torrents] * 1 + [self.lab.tmp / str(i) for i in range(17)]):
            with self.assertRaises(KuraError) as ctx:
                manifest_build(self.lab, bad)
            self.assertEqual(ctx.exception.code, "invalid_argument")


def manifest_build(lab, dirs):
    from kura import manifest
    return manifest.build(lab.store, roots={"main": lab.lib}, library="home", torrents_dir=dirs,
                          policy=lab.policy())


class Scale(unittest.TestCase):
    def test_index_is_linear_not_quadratic(self):
        titles, per_title, torrents = 1500, 10, 500
        entries = []
        for t in range(titles):
            files = tuple(match_module_file(f"T{t:04d}/ep{i:02d}.mkv", 1000 + t * 100 + i)
                          for i in range(per_title))
            entries.append(match_module_title(f"T{t:04d}", files))
        inv = match_module_inventory(entries)
        metas = []
        for k in range(torrents):
            t = (k * 3) % titles
            files = [(f"ep{i:02d}.mkv", bytes(1000 + t * 100 + i)) for i in range(per_title)]
            metas.append(synthetic_meta(f"R{k}", files))
        started = time.monotonic()
        matches, unmatched = match(inv, metas)
        elapsed = time.monotonic() - started
        self.assertEqual(len(matches) + len(unmatched), torrents)
        self.assertEqual(len(matches), torrents)
        self.assertLess(elapsed, 5.0)


class RepeatedSignatures(unittest.TestCase):
    def test_many_identical_signatures_inside_one_torrent_pair_by_path(self):
        n = 300
        files = [(f"d{i:03d}/ep.mkv", P(f"r{i}", 1000)) for i in range(n)]
        built = torrentgen.build("Kura Wide", files)
        lab = Lab(self)
        for rel, data in files:
            write(lab.lib, f"Kura Local/x/{rel}", data)
        started = time.monotonic()
        matches, _ = match(scan(lab.lib, "main"), [parse(built.data)])
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(len(matches[0].mapping), n)
        self.assertEqual(matches[0].mapping[7], ("Kura Local", "x", "d007", "ep.mkv"))

    def test_non_distinctive_signature_is_ignored(self):
        lab = Lab(self)
        for t in range(match_module._ContentIndex.MAX_BUCKET + 5):
            write(lab.lib, f"T{t:04d}/cover.jpg", P("cover", 500))
        built = torrentgen.build("Kura Cover", [("cover.jpg", P("cover", 500))])
        self.assertEqual(match(scan(lab.lib, "main"), [parse(built.data)])[0], [])

    def test_shared_signatures_across_titles_and_torrents_stay_fast(self):
        titles, per, torrents = 200, 20, 500        # every signature sits in every title
        inv = match_module_inventory([
            match_module_title(f"T{t:04d}", tuple(match_module_file(f"T{t:04d}/ep{j:02d}.mkv", 1000 + j)
                                                   for j in range(per))) for t in range(titles)])
        metas = [synthetic_meta(f"R{k}", [(f"ep{j:02d}.mkv", bytes(1000 + j)) for j in range(per)])
                 for k in range(torrents)]
        started = time.monotonic()
        matches, unmatched = match(inv, metas)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual((len(matches), {u["reason"] for u in unmatched}), (0, {"ambiguous"}))


def match_module_file(rel, size):
    from kura.inventory import FileEntry
    return FileEntry(rel, size, 1, 1, hash(rel) & 0xFFFFFF)


def match_module_title(rel, files):
    from kura.inventory import Title
    return Title(rel, "dir", files, sum(f.size for f in files), 1, ())


def match_module_inventory(titles):
    from kura.inventory import Inventory
    return Inventory("main", 1, tuple(titles), 0, 0.0)


def synthetic_meta(name, files):
    """A TorrentMeta without piece data: matching never looks at pieces."""
    from kura.torrentmeta import TorrentFile, TorrentMeta
    entries, offset = [], 0
    for i, (fname, data) in enumerate(files):
        entries.append(TorrentFile(i, (name, fname), len(data), offset, False))
        offset += len(data)
    return TorrentMeta(infohash=f"{abs(hash(name)):040x}"[:40], name=name, multi_file=True,
                       piece_length=16384, pieces=b"", files=tuple(entries), total_length=offset,
                       private=False, announce_urls=(), source=None, hybrid=False,
                       descriptor_sha256="0" * 64)


class CliRepeatable(unittest.TestCase):
    def test_torrents_flag_repeats(self):
        lab = Lab(self)
        second = lab.tmp / "resolved"
        second.mkdir()
        built = torrentgen.build("KuraSampleSeries", GOOD)
        (second / "x.torrent").write_bytes(built.data)
        for name, data in GOOD:
            write(lab.lib, f"Kura Sample Series/{name}", data)
        home = lab.tmp / "home"
        home.mkdir()
        argv = ["kura", "manifest", "build", "--library", "home", "--root", f"main={lab.lib}",
                "--torrents", str(lab.torrents), "--torrents", str(second),
                "--tracker-denylist", str(lab.deny), "--protected", str(lab.prot), "--no-anilist"]
        buffer = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(home)}), patch("sys.argv", argv), \
             patch("kura.cli_handover.AniList", Mock()), contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as exited:
                cli.main()
        out = json.loads(buffer.getvalue())
        self.assertEqual((exited.exception.code, out["data"]["torrents"]["dirs"],
                          out["data"]["counts"]), (0, 2, {"reacquirable": 1}))
        self.assertEqual(out["data"]["matched_by"], {"content": 1})


if __name__ == "__main__":
    unittest.main()
