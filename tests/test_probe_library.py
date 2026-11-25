"""Library probing and the `kura probe` commands. Synthetic files only; nothing leaves the temp dir."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import mkvgen
from kura import cli_probe, mkvprobe, probe as probe_mod, rofs
from kura.inventory import scan
from kura.manifest_store import ManifestRepo
from kura.models import KuraError
from kura.store import Store
from test_cli_handover import run

WRITE_BITS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL


class Lib(unittest.TestCase):
    """A temp library of generated files, inventoried into a temp state store."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib = self.tmp / "lib"
        self.lib.mkdir()
        self.store = Store(self.tmp / "state")
        self.addCleanup(self.store.db.close)
        self.show = self.lib / "Kura Sample Series"
        self.show.mkdir()
        self.write("Kura Sample Series/[Kura Encodes] Kura Sample Series - 01 [1080p][x265].mkv",
                   mkvgen.build(codec="hevc", settings=mkvgen.X265_TEXT, sei="private"))
        self.write("Kura Sample Series/[Kura Encodes] Kura Sample Series - 02 [1080p][x265].mkv",
                   mkvgen.build(codec="hevc", settings=mkvgen.X265_TEXT, sei="private", duration=1421.0))
        self.write("Kura Sample Series/[Other Group] Kura Sample Series - 01 [720p].mkv", mkvgen.build())
        self.write("Kura Sample Series/Kura Sample Series - 03 [Third Group][480p].mkv",
                   mkvgen.build(settings=None, sei=None))
        self.write("Kura Sample Series/notes.txt", b"not a video")
        self.write("Kura Loose Movie.mkv", mkvgen.build(duration=5400.0))
        self.inventory()

    def write(self, rel, data):
        path = self.lib / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def inventory(self):
        inv = scan(self.lib, "main")
        rows = [{"rel_path": f"main/{t.rel_path}", "kind": t.kind, "total_bytes": t.total_bytes,
                 "newest_mtime_ns": t.newest_mtime_ns, "blocked": list(t.blocked),
                 "files": [f.json() for f in t.files]} for t in inv.titles]
        self.ids = ManifestRepo(self.store.db).save_inventory("home", inv.root_dev, rows, inv.scanned_at)

    def probe(self, **kw):
        kw.setdefault("library", "home")
        kw.setdefault("roots", {"main": self.lib})
        return probe_mod.probe_library(self.store, **kw)

    def rows(self):
        return {r[0]: (r[1], json.loads(r[2])) for r in self.store.db.execute(
            "SELECT rel_path, status, result FROM probe_results ORDER BY rel_path")}


class Sampling(Lib):
    def test_groups_probe_the_first_file_of_each_cluster(self):
        out = self.probe()
        self.assertEqual(out["sample"], {"strategy": "groups", "titles": 2, "files_selected": 4})
        self.assertEqual((out["probed"], out["cached"], out["changed"], out["failed"]), (4, 0, 0, {}))
        self.assertEqual((out["budget_exhausted"], out["pending"]), (False, 0))
        self.assertGreater(out["bytes_read"], 0)
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        names = sorted(rows)
        self.assertTrue(any("- 02 " in n for n in names) is False)            # same cluster as episode 1
        self.assertFalse(any(n.endswith("notes.txt") for n in names))
        for status, result in rows.values():
            self.assertEqual((status, result["schema"]), ("ok", "kura.probe/1"))
        encoder = {n: r["encoder"] and r["encoder"]["library"] for n, (_, r) in rows.items()}
        self.assertEqual(sorted(str(v) for v in encoder.values()), ["None", "x264", "x264", "x265"])
        self.assertEqual(sum(r["bytes_read"] for _, r in rows.values()), out["bytes_read"])

    def test_all_probes_every_video_file_and_reuses_the_cache(self):
        self.probe()
        out = self.probe(sample="all")
        self.assertEqual(out["sample"]["files_selected"], 5)
        self.assertEqual((out["probed"], out["cached"]), (1, 4))

    def test_second_run_reads_nothing_and_opens_nothing(self):
        first = self.probe()
        with patch.object(rofs, "open_file_ro", side_effect=AssertionError("opened a file")):
            second = self.probe()
        self.assertEqual((second["probed"], second["cached"], second["bytes_read"]), (0, first["probed"], 0))
        self.assertEqual(second["pending"], 0)

    def test_reprobe_ignores_the_cache(self):
        self.probe()
        out = self.probe(reprobe=True)
        self.assertEqual((out["probed"], out["cached"]), (4, 0))

    def test_probe_version_bump_invalidates_the_cache(self):
        self.probe()
        with patch.object(mkvprobe, "PROBE_VERSION", 2):
            out = self.probe()
        self.assertEqual((out["probed"], out["cached"]), (4, 0))
        self.assertEqual({r[0] for r in self.store.db.execute("SELECT probe_version FROM probe_results")}, {2})

    def test_title_filter_and_unknown_title(self):
        out = self.probe(titles=["main/Kura Loose Movie.mkv"])
        self.assertEqual((out["sample"]["files_selected"], out["sample"]["titles"], out["probed"]), (1, 1, 1))
        with self.assertRaises(KuraError) as caught:
            self.probe(titles=["main/Nonexistent"])
        self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_max_files_bounds_the_work_per_run_and_repeated_runs_progress(self):
        out = self.probe(sample="all", max_files=2)
        self.assertEqual((out["sample"]["files_selected"], out["probed"], out["pending"]), (5, 2, 3))
        self.assertFalse(out["budget_exhausted"])
        out = self.probe(sample="all", max_files=2)
        self.assertEqual((out["cached"], out["probed"], out["pending"]), (2, 2, 1))
        out = self.probe(sample="all", max_files=2)
        self.assertEqual((out["cached"], out["probed"], out["pending"]), (4, 1, 0))

    def test_unlabelled_root_counts_as_failed_even_for_cached_files(self):
        out = self.probe(roots={"elsewhere": self.lib})
        self.assertEqual((out["probed"], out["failed"], out["pending"]), (0, {"no_root": 4}, 0))
        self.assertEqual(self.rows(), {})
        self.probe()
        again = self.probe(roots={"elsewhere": self.lib})
        self.assertEqual((again["cached"], again["failed"]), (0, {"no_root": 4}))

    def test_mp4_is_recorded_as_unsupported_container(self):
        self.write("Kura Other Show/Kura Other Show - 01 [1080p].mp4", b"\x00\x00\x00\x18ftypisom" + bytes(64))
        self.inventory()
        out = self.probe(titles=["main/Kura Other Show"])
        self.assertEqual(out["probed"], 1)
        (status, result), = self.rows().values()
        self.assertEqual((status, result["container"]), ("unsupported_container", "mp4"))

    def test_signature_clusters(self):
        sig = probe_mod.signature
        self.assertEqual(sig("Season 1/[Grp] Show - 01 [1080p][x265 10bit].mkv"),
                         sig("[Grp] Show - 07 [1080p][HEVC].mkv")[:1] + sig("[Grp] Show - 07 [1080p][x265 10bit].mkv")[1:])
        self.assertNotEqual(sig("[A] Show - 01 [1080p].mkv"), sig("[B] Show - 01 [1080p].mkv"))
        self.assertNotEqual(sig("[A] Show - 01 [1080p].mkv"), sig("[A] Show - 01 [720p].mkv"))
        self.assertEqual(sig("Show - 01 [1080p]-Kura.mkv")[0], "kura")


class Changes(Lib):
    def test_changed_file_is_not_probed_and_not_cached(self):
        path = next(self.show.glob("*Other Group*"))
        path.write_bytes(path.read_bytes() + b"appended after the inventory")
        out = self.probe()
        self.assertEqual((out["probed"], out["changed"], out["pending"]), (3, 1, 0))
        rel = f"Kura Sample Series/{path.name}"
        row = self.store.db.execute("SELECT status, result, bytes_read FROM probe_results WHERE rel_path=?",
                                    (rel,)).fetchone()
        self.assertEqual(row, ("changed", "{}", 0))
        again = self.probe()
        self.assertEqual((again["cached"], again["changed"]), (3, 1))        # retried, still changed
        self.inventory()
        healed = self.probe()
        self.assertEqual((healed["changed"], healed["probed"], healed["cached"]), (0, 1, 3))

    def test_file_modified_during_the_probe_is_changed_and_not_cached(self):
        path = next(self.show.glob("*Other Group*"))
        real, state = os.pread, {"hit": False}

        def racing(fd, n, offset):
            data = real(fd, n, offset)
            if not state["hit"] and os.fstat(fd).st_size == path.stat().st_size and fd > 2:
                state["hit"] = True
                os.utime(path, ns=(5, 5))          # someone touches the file mid-probe
            return data

        with patch("os.pread", racing):
            out = self.probe(titles=["main/Kura Sample Series"])
        self.assertEqual(out["changed"], 1)
        row = self.store.db.execute("SELECT status FROM probe_results WHERE rel_path LIKE '%Other Group%'").fetchone()
        self.assertEqual(row, ("changed",))

    def test_mtime_change_alone_counts_as_changed(self):
        path = next(self.show.glob("*Other Group*"))
        os.utime(path, ns=(1, 1))
        self.assertEqual(self.probe()["changed"], 1)

    def test_vanished_file_is_changed(self):
        next(self.show.glob("*Other Group*")).unlink()
        out = self.probe()
        self.assertEqual((out["changed"], out["probed"]), (1, 3))

    def test_symlink_swapped_in_after_the_inventory_is_refused(self):
        path = next(self.show.glob("*Other Group*"))
        target = self.tmp / "outside.mkv"
        target.write_bytes(mkvgen.build())
        path.unlink()
        path.symlink_to(target)
        out = self.probe()
        self.assertEqual((out["probed"], out["failed"]), (3, {"unsafe": 1}))
        status = self.store.db.execute("SELECT status FROM probe_results WHERE rel_path LIKE '%Other Group%'").fetchone()
        self.assertEqual(status, ("unsafe",))
        self.assertEqual(self.probe()["failed"], {"unsafe": 1})               # never a cache hit

    def test_fifo_swapped_in_after_the_inventory_is_refused_without_blocking(self):
        path = next(self.show.glob("*Other Group*"))
        path.unlink()
        os.mkfifo(path)
        out = self.probe()
        self.assertEqual((out["probed"], out["failed"]), (3, {"unsafe": 1}))

    def test_root_that_is_a_symlink_is_refused(self):
        link = self.tmp / "linked"
        link.symlink_to(self.lib)
        out = self.probe(roots={"main": link})
        self.assertEqual((out["probed"], out["failed"]), (0, {"unsafe_path": 4}))


class Budget(Lib):
    def test_hard_cap_discards_a_cut_probe_and_resumes(self):
        files = sorted(self.show.glob("*.mkv"))
        first = self.probe(max_read_bytes=1500)
        self.assertTrue(first["budget_exhausted"])
        self.assertLessEqual(first["bytes_read"], 1500)
        self.assertGreater(first["pending"], 0)
        self.assertEqual(first["probed"] + first["pending"], 4)
        stored = len(self.rows())
        self.assertEqual(stored, first["probed"])                             # the cut probe left no row
        done = self.probe()
        self.assertEqual((done["pending"], done["cached"], done["probed"]), (0, first["probed"], 4 - first["probed"]))
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(len(files), 4)

    def test_budget_smaller_than_any_probe_reads_at_most_that_and_is_retried_later(self):
        out = self.probe(max_read_bytes=10)
        self.assertLessEqual(out["bytes_read"], 10)
        self.assertEqual(out["pending"], 0)                                    # the budget is the per-file allowance
        self.assertEqual({s for s, _ in self.rows().values()}, {"partial"})
        again = self.probe()
        self.assertEqual((again["cached"], again["probed"]), (0, 4))           # a larger allowance retries them
        self.assertEqual({s for s, _ in self.rows().values()}, {"ok"})

    def test_a_small_run_budget_is_the_per_file_allowance_so_runs_progress(self):
        self.write("Kura Big Show/Kura Big Show - 01 [1080p].mkv",
                   mkvgen.build(attach=[("big.ttf", "font/ttf", 3 * (1 << 20))]))
        self.inventory()
        for _ in range(3):
            out = self.probe(titles=["main/Kura Big Show"], max_read_bytes=300)
        self.assertEqual((out["cached"], out["pending"]), (1, 0))

    def test_unreachable_files_do_not_use_up_max_files(self):
        for path in sorted(self.show.glob("*.mkv"))[:2]:
            path.write_bytes(path.read_bytes() + b"changed")
        out = self.probe(sample="all", max_files=1, titles=["main/Kura Sample Series"])
        self.assertEqual((out["changed"], out["probed"], out["pending"]), (2, 1, 1))

    def test_per_file_cap_applies(self):
        self.write("Kura Big Show/Kura Big Show - 01 [1080p].mkv",
                   mkvgen.build(attach=[("big.ttf", "font/ttf", 3 * (1 << 20))]))
        self.inventory()
        out = self.probe(titles=["main/Kura Big Show"], per_file_bytes=200)
        self.assertEqual(out["probed"], 1)
        self.assertLessEqual(out["bytes_read"], 200)
        (status, result), = self.rows().values()
        self.assertEqual(status, "partial")                                    # per-file cut is a result

    def test_per_file_cut_is_retried_with_a_larger_allowance(self):
        self.write("Kura Big Show/Kura Big Show - 01 [1080p].mkv",
                   mkvgen.build(attach=[("big.ttf", "font/ttf", 3 * (1 << 20))]))
        self.inventory()
        keys = {"titles": ["main/Kura Big Show"]}
        self.assertEqual(self.probe(per_file_bytes=200, **keys)["probed"], 1)
        again = self.probe(per_file_bytes=200, **keys)
        self.assertEqual((again["cached"], again["probed"]), (1, 0))          # same allowance: nothing new to learn
        self.assertEqual(self.probe(per_file_bytes=100, **keys)["cached"], 1)
        larger = self.probe(per_file_bytes=300, **keys)
        self.assertEqual((larger["cached"], larger["probed"]), (0, 1))        # a larger allowance retries it
        full = self.probe(**keys)
        self.assertEqual(full["probed"], 1)
        self.assertEqual(self.probe(**keys)["cached"], 1)                      # complete result is cached
        self.assertEqual(self.rows()["Kura Big Show/Kura Big Show - 01 [1080p].mkv"][0], "ok")

    def test_a_cut_file_does_not_starve_later_files_under_max_files(self):
        self.write("Kura Big Show/Kura Big Show - 01 [1080p].mkv",
                   mkvgen.build(attach=[("big.ttf", "font/ttf", 3 * (1 << 20))]))
        self.inventory()
        total = 5
        for _ in range(total + 1):
            out = self.probe(per_file_bytes=200, max_files=1)
            if out["pending"] == 0:
                break
        self.assertEqual(out["pending"], 0)
        self.assertEqual(len(self.rows()), total)

    def test_bytes_read_before_a_read_error_are_charged(self):
        real = os.pread
        state = {"n": 0}

        def flaky(fd, n, offset):
            state["n"] += 1
            if state["n"] > 3:
                raise OSError(5, "Input/output error")
            return real(fd, n, offset)

        with patch("os.pread", flaky):
            out = self.probe(titles=["main/Kura Loose Movie.mkv"])
        self.assertEqual((out["probed"], out["failed"]), (0, {"io_error": 1}))
        self.assertGreater(out["bytes_read"], 0)

    def test_should_stop_leaves_the_rest_pending(self):
        calls = iter([False, True])
        out = self.probe(should_stop=lambda: next(calls, True))
        self.assertEqual((out["probed"], out["pending"]), (1, 3))

    def test_argument_validation(self):
        for kw in ({"sample": "some"}, {"max_files": 0}, {"max_read_bytes": 0}, {"per_file_bytes": 0},
                   {"library": ""}, {"titles": [""]}):
            with self.subTest(kw), self.assertRaises(KuraError) as caught:
                self.probe(**kw)
            self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_concurrent_mutation_is_refused(self):
        with self.store.lock():
            with self.assertRaises(KuraError) as caught:
                self.probe()
        self.assertEqual(caught.exception.code, "busy")


class ReadOnly(Lib):
    def test_inputs_are_opened_without_write_bits_and_nothing_is_created(self):
        real = os.open
        seen = []

        def spy(path, flags, *a, **kw):
            seen.append((path, flags, kw.get("dir_fd")))
            return real(path, flags, *a, **kw)

        before = {p: p.stat().st_mtime_ns for p in self.lib.rglob("*")}
        with patch("os.open", spy):
            self.probe(sample="all")
        inputs = [(p, f) for p, f, d in seen if d is not None or p == "/"]
        self.assertGreater(len(inputs), 5)
        for path, flags in inputs:
            self.assertEqual(flags & WRITE_BITS, 0, (path, flags))
            self.assertTrue(flags & os.O_NOFOLLOW, (path, flags))
        lock_opens = [p for p, f, d in seen if d is None and p != "/"]
        self.assertTrue(all(str(p).endswith("operation.lock") for p in lock_opens), lock_opens)
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.lib.rglob("*")}, before)
        self.assertEqual(sorted(p.name for p in self.lib.iterdir()), ["Kura Loose Movie.mkv", "Kura Sample Series"])

    def test_state_directory_inside_or_above_a_root_is_refused(self):
        for root in (self.tmp, self.tmp / "state", self.tmp / "state" / "deeper", Path("/")):
            with self.subTest(root=str(root)), self.assertRaises(KuraError) as caught:
                self.probe(roots={"main": root})
            self.assertEqual(caught.exception.code, "unsafe_state_dir")

    def test_cli_refuses_an_overlapping_state_directory_before_creating_it(self):
        home = self.tmp / "home2"
        home.mkdir()
        code, out = run("probe", "library", "--library", "home", "--root", f"main={home}", home=str(home))
        self.assertEqual((code, out["error"]["code"]), (2, "unsafe_state_dir"))
        self.assertEqual(list(home.iterdir()), [])                            # nothing was created under the root

    def test_a_root_on_another_device_than_the_inventory_is_refused_even_when_cached(self):
        self.probe()
        self.store.db.execute("UPDATE library_titles SET root_dev=root_dev+1")
        self.store.db.commit()
        out = self.probe()
        self.assertEqual((out["cached"], out["probed"], out["failed"]), (0, 0, {"root_mismatch": 4}))

    def test_cached_results_still_need_a_valid_root(self):
        self.probe()
        link = self.tmp / "linked"
        link.symlink_to(self.lib)
        again = self.probe(roots={"main": link})
        self.assertEqual((again["cached"], again["failed"]), (0, {"unsafe_path": 4}))
        gone = self.probe(roots={"main": self.tmp / "missing"})
        self.assertEqual((gone["cached"], gone["failed"]), (0, {"not_found": 4}))

    def test_reads_use_pread_on_the_rofs_descriptor(self):
        with patch("os.read", side_effect=AssertionError("sequential read")):
            self.probe()
        calls = []
        real = os.pread

        def spy(fd, n, offset):
            calls.append((n, offset))
            return real(fd, n, offset)

        with patch("os.pread", spy):
            self.probe(reprobe=True)
        self.assertTrue(calls)

    def test_no_network_modules_are_loaded_by_the_probe_modules(self):
        for module in (mkvprobe, probe_mod, cli_probe):
            for name in ("socket", "http", "urllib", "ssl", "subprocess"):
                self.assertFalse(hasattr(module, name), (module.__name__, name))


class Commands(Lib):
    def test_probe_file_is_stateless(self):
        target = self.lib / "Kura Loose Movie.mkv"
        before = {p: p.read_bytes() for p in self.lib.rglob("*") if p.is_file()}
        code, out = run("probe", "file", str(target), store=False)
        self.assertEqual((code, out["success"], out["phase"], out["schema_version"]), (0, True, "probe", 1))
        self.assertEqual((out["data"]["schema"], out["data"]["status"], out["data"]["duration_s"]),
                         ("kura.probe/1", "ok", 5400.0))
        self.assertEqual({p: p.read_bytes() for p in self.lib.rglob("*") if p.is_file()}, before)
        code, out = run("probe", "file", str(target), "--max-read-kib", "1", store=False)
        self.assertEqual(code, 0)
        self.assertLessEqual(out["data"]["bytes_read"], 1024)

    def test_probe_file_refusals(self):
        link = self.tmp / "link.mkv"
        link.symlink_to(self.lib / "Kura Loose Movie.mkv")
        for argv, error in (([str(link)], "unsafe_path"), (["relative/file.mkv"], "invalid_arguments"),
                            ([str(self.lib / "missing.mkv")], "not_found"), ([str(self.lib)], "unsafe_path"),
                            ([str(self.lib / ".." / "lib" / "Kura Loose Movie.mkv")], "unsafe_path"),
                            ([str(self.lib / "Kura Loose Movie.mkv"), "--max-read-kib", "0"], "invalid_arguments")):
            with self.subTest(argv):
                code, out = run("probe", "file", *argv, store=False)
                self.assertEqual((code, out["success"], out["error"]["code"]), (2, False, error))

    def test_probe_library_through_the_cli(self):
        home = self.tmp / "home"
        state = home / ".local" / "share" / "kura"
        state.parent.mkdir(parents=True)
        shutil.copytree(self.tmp / "state", state)
        self.store.db.close()
        argv = ["probe", "library", "--library", "home", "--root", f"main={self.lib}"]
        code, out = run(*argv, home=str(home))
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "probe"))
        self.assertEqual((out["data"]["probed"], out["data"]["sample"]["strategy"]), (4, "groups"))
        code, out = run(*argv, "--sample", "all", "--max-read-mib", "1", home=str(home))
        self.assertEqual((out["data"]["probed"], out["data"]["cached"]), (1, 4))
        code, out = run(*argv, "--reprobe", "--title", "main/Kura Loose Movie.mkv", home=str(home))
        self.assertEqual(out["data"]["probed"], 1)
        code, out = run("probe", "library", "--library", "home", home=str(home))
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out = run(*argv[:-1], "main", home=str(home))
        self.assertEqual(out["error"]["code"], "invalid_arguments")
        code, out = run(*argv, "--max-read-mib", "0", home=str(home))
        self.assertEqual(out["error"]["code"], "invalid_arguments")
        code, out = run(*argv, "--max-files", "0", home=str(home))
        self.assertEqual(out["error"]["code"], "invalid_arguments")
        code, out = run(*argv, "--root", f"main={self.lib}", home=str(home))
        self.assertEqual(out["error"]["code"], "invalid_arguments")

    def test_probe_registers_both_operations(self):
        import kura.cli as cli
        args = cli.parser().parse_args(["probe", "file", "/x"])
        self.assertEqual((args.command, args.operation), ("probe", "file"))
        args = cli.parser().parse_args(["probe", "library", "--library", "a", "--root", "m=/x"])
        self.assertEqual((args.sample, args.max_files, args.reprobe), ("groups", 2000, False))


if __name__ == "__main__":
    unittest.main()
