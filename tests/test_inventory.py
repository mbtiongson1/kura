"""Read-only file access and metadata-only inventory: hostile trees, limits, no writes."""
import builtins
import io
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from kura import inventory, rofs
from kura.inventory import scan
from kura.models import KuraError

LINUX = sys.platform.startswith("linux")
WRITE_BITS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL)


def snapshot(root: Path):
    """Names, types, sizes and mtimes of everything below root, without following links."""
    out = {}
    for path in sorted([root, *root.rglob("*")]):
        st = path.lstat()
        out[str(path.relative_to(root))] = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns)
    return out


def fd_count():
    for base in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(base):
            return len(os.listdir(base))
    return None


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()  # /var is a symlink on macOS
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "library"
        self.root.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.bin").write_bytes(b"outside" * 10)

    def write(self, rel, data=b"x"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def titles(self, **kw):
        return {t.rel_path: t for t in scan(self.root, "main", **kw).titles}


class RootTests(TreeCase):
    def test_root_symlink_and_symlinked_parent_refused(self):
        link = self.tmp / "link"
        link.symlink_to(self.root)
        with self.assertRaises(KuraError) as ctx:
            rofs.open_root(link)
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(KuraError) as ctx:
            scan(link, "main")
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(KuraError) as ctx:
            scan(link / "library", "main")
        self.assertEqual(ctx.exception.code, "unsafe_path")

    def test_relative_traversal_missing_and_file_roots_refused(self):
        for bad, code in ((Path("library"), "unsafe_path"),
                          (self.root / ".." / "library", "unsafe_path"),
                          (self.tmp / "absent", "not_found")):
            with self.assertRaises(KuraError) as ctx:
                rofs.open_root(bad)
            self.assertEqual(ctx.exception.code, code)
        self.write("file.bin")
        with self.assertRaises(KuraError):
            rofs.open_root(self.root / "file.bin")

    def test_open_root_returns_directory_fd(self):
        fd = rofs.open_root(self.root)
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            os.close(fd)


class RofsTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.root_fd = rofs.open_root(self.root)
        self.addCleanup(os.close, self.root_fd)

    def open_code(self, parts):
        with self.assertRaises(KuraError) as ctx:
            os.close(rofs.open_file_ro(self.root_fd, parts))
        return ctx.exception.code

    def test_reads_regular_file_in_nested_directory(self):
        self.write("Series/Season 1/ep1.bin", b"0123456789")
        fd = rofs.open_file_ro(self.root_fd, ("Series", "Season 1", "ep1.bin"))
        try:
            self.assertEqual(b"".join(rofs.read_exact(fd, 10, chunk=3)), b"0123456789")
        finally:
            os.close(fd)

    def test_symlinked_intermediate_directory_refused(self):
        (self.root / "Series").mkdir()
        os.symlink(self.outside, self.root / "Series" / "linked")
        self.assertEqual(self.open_code(("Series", "linked", "secret.bin")), "unsafe_path")

    def test_symlinked_top_directory_and_final_symlink_refused(self):
        os.symlink(self.outside, self.root / "top")
        os.symlink(self.outside / "secret.bin", self.root / "final.bin")
        self.assertEqual(self.open_code(("top", "secret.bin")), "unsafe_path")
        self.assertEqual(self.open_code(("final.bin",)), "unsafe_path")

    def test_directory_as_final_component_refused(self):
        (self.root / "Series").mkdir()
        self.assertEqual(self.open_code(("Series",)), "unsafe_path")

    def test_file_as_intermediate_component_refused(self):
        self.write("plain.bin")
        self.assertEqual(self.open_code(("plain.bin", "x")), "unsafe_path")

    def test_fifo_refused_without_blocking(self):
        os.mkfifo(self.root / "pipe")
        self.assertEqual(self.open_code(("pipe",)), "unsafe_path")

    def test_missing_file_is_not_found(self):
        self.assertEqual(self.open_code(("nope.bin",)), "not_found")
        self.assertEqual(self.open_code(("nodir", "nope.bin")), "not_found")

    def test_invalid_components_refused(self):
        for parts in ((), ("",), (".",), ("..",), ("a", ".."), ("a/b",), ("a\0b",), ["a"], ("a", 1)):
            self.assertEqual(self.open_code(parts), "unsafe_path", parts)

    def test_no_descriptor_leak_on_success_or_failure(self):
        self.write("Series/ep1.bin", b"abc")
        os.symlink(self.outside, self.root / "Series" / "linked")
        before = fd_count()
        if before is None:
            self.skipTest("no fd listing on this platform")
        for _ in range(20):
            os.close(rofs.open_file_ro(self.root_fd, ("Series", "ep1.bin")))
            for parts in (("Series", "linked", "secret.bin"), ("Series", "missing"), ("Series",)):
                with self.assertRaises(KuraError):
                    rofs.open_file_ro(self.root_fd, parts)
        self.assertEqual(fd_count(), before)

    def test_every_open_is_read_only_nofollow_and_never_modifies_tree(self):
        self.write("Series/ep1.bin", b"abc")
        before = snapshot(self.root)
        flags = []
        real = os.open

        def spy(path, fl, *a, **k):
            flags.append(fl)
            return real(path, fl, *a, **k)

        with patch("kura.rofs.os.open", spy):
            os.close(rofs.open_file_ro(self.root_fd, ("Series", "ep1.bin")))
            os.close(rofs.open_root(self.root))
        self.assertGreaterEqual(len(flags), 3)
        for fl in flags:
            self.assertEqual(fl & WRITE_BITS, 0)
            self.assertTrue(fl & os.O_NOFOLLOW)
            self.assertTrue(fl & os.O_CLOEXEC)
        self.assertTrue(flags[0] & os.O_DIRECTORY)
        self.assertTrue(flags[1] & os.O_NONBLOCK)
        self.assertFalse(flags[1] & os.O_DIRECTORY)
        self.assertEqual(snapshot(self.root), before)

    @unittest.skipUnless(hasattr(os, "O_NOATIME"), "O_NOATIME is Linux only")
    def test_noatime_requested_on_final_open(self):
        self.write("ep1.bin", b"abc")
        flags = []
        real = os.open

        def spy(path, fl, *a, **k):
            flags.append(fl)
            return real(path, fl, *a, **k)

        with patch("kura.rofs.os.open", spy):
            os.close(rofs.open_file_ro(self.root_fd, ("ep1.bin",)))
        self.assertTrue(flags[0] & os.O_NOATIME)

    def test_noatime_permission_error_falls_back(self):
        self.write("ep1.bin", b"abc")
        fake, real, calls = 0x10000000, os.open, []

        def fake_noatime(path, fl, *a, **k):
            calls.append(fl)
            if fl & fake:
                raise PermissionError(1, "not owner")
            return real(path, fl, *a, **k)

        with patch.object(rofs, "_NOATIME", fake), patch("kura.rofs.os.open", fake_noatime):
            os.close(rofs.open_file_ro(self.root_fd, ("ep1.bin",)))
        self.assertEqual([bool(c & fake) for c in calls], [True, False])

    def test_read_exact_boundaries(self):
        path = self.write("f.bin", b"0123456789")
        fd = rofs.open_file_ro(self.root_fd, ("f.bin",))
        self.addCleanup(os.close, fd)
        self.assertEqual(list(rofs.read_exact(fd, 0)), [])
        self.assertEqual(b"".join(rofs.read_exact(fd, 4, chunk=2)), b"0123")
        self.assertEqual(os.read(fd, 100), b"456789")  # never read beyond n
        os.lseek(fd, 0, os.SEEK_SET)
        with self.assertRaises(KuraError) as ctx:
            b"".join(rofs.read_exact(fd, 11, chunk=4))
        self.assertEqual(ctx.exception.code, "short_read")
        self.assertEqual(path.read_bytes(), b"0123456789")

    def test_read_exact_rejects_bad_arguments(self):
        for n, chunk in ((-1, 1), (1, 0), (1, -5)):
            with self.assertRaises(KuraError):
                list(rofs.read_exact(0, n, chunk))


class ScanBasicsTests(TreeCase):
    def test_titles_files_sizes_and_json_shape(self):
        self.write("Kura Sample Series/Season 1/ep01.bin", b"a" * 10)
        self.write("Kura Sample Series/Season 1/ep02.bin", b"b" * 20)
        self.write("Kura Sample Series/extras.bin", b"c" * 5)
        self.write("Kura Sample Movie.bin", b"m" * 7)
        (self.root / "Empty Title").mkdir()
        inv = scan(self.root, "main")
        self.assertEqual([t.rel_path for t in inv.titles],
                         ["Empty Title", "Kura Sample Movie.bin", "Kura Sample Series"])
        series = inv.titles[2]
        self.assertEqual((series.kind, series.total_bytes, series.blocked), ("dir", 35, ()))
        self.assertEqual([f.rel_path for f in series.files],
                         ["Kura Sample Series/Season 1/ep01.bin",
                          "Kura Sample Series/Season 1/ep02.bin",
                          "Kura Sample Series/extras.bin"])
        first = series.files[0]
        st = (self.root / first.rel_path).stat()
        self.assertEqual((first.size, first.mtime_ns, first.dev, first.ino),
                         (10, st.st_mtime_ns, st.st_dev, st.st_ino))
        self.assertEqual(inv.root_dev, self.root.stat().st_dev)
        movie = inv.titles[1]
        self.assertEqual((movie.kind, len(movie.files), movie.total_bytes), ("file", 1, 7))
        self.assertEqual(inv.titles[0].files, ())
        self.assertEqual(inv.entries, 7)
        self.assertEqual(inv.label, "main")
        plain = inv.json()
        self.assertEqual(set(plain), {"label", "entries", "titles"})
        self.assertEqual(set(plain["titles"][2]),
                         {"rel_path", "kind", "file_count", "total_bytes",
                          "newest_mtime_ns", "blocked"})
        self.assertEqual(plain["titles"][2]["file_count"], 3)
        with_files = inv.json(files=True)["titles"][2]["files"]
        self.assertEqual(set(with_files[0]), {"rel_path", "size", "mtime_ns", "dev", "ino"})

    def test_rel_path_feeds_rofs(self):
        self.write("Kura Sample Series/Season 1/ep01.bin", b"hello")
        entry = scan(self.root, "main").titles[0].files[0]
        root_fd = rofs.open_root(self.root)
        try:
            fd = rofs.open_file_ro(root_fd, tuple(entry.rel_path.split("/")))
            self.assertEqual(b"".join(rofs.read_exact(fd, entry.size)), b"hello")
            os.close(fd)
        finally:
            os.close(root_fd)

    def test_newest_mtime_includes_directories_and_blocked_entries(self):
        self.write("T/a.bin")
        os.utime(self.root / "T" / "a.bin", ns=(1, 1_000))
        os.utime(self.root / "T", ns=(1, 5_000))
        self.assertEqual(self.titles()["T"].newest_mtime_ns, 5_000)
        os.symlink(self.outside, self.root / "T" / "link")
        os.utime(self.root / "T" / "link", ns=(1, 9_000), follow_symlinks=False)
        os.utime(self.root / "T", ns=(1, 5_000))
        self.assertEqual(self.titles()["T"].newest_mtime_ns, 9_000)

    def test_label_validation(self):
        for bad in ("", "../x", "a b", "-x", "x" * 65, None):
            with self.assertRaises(KuraError) as ctx:
                scan(self.root, bad)
            self.assertEqual(ctx.exception.code, "invalid_label")

    def test_argument_validation(self):
        for kw in ({"max_depth": 0}, {"max_depth": 65}, {"max_entries": 0}):
            with self.assertRaises(KuraError) as ctx:
                scan(self.root, "main", **kw)
            self.assertEqual(ctx.exception.code, "invalid_argument")


class HostileTreeTests(TreeCase):
    def test_top_level_symlinks_not_followed_and_blocked(self):
        os.symlink(self.outside, self.root / "LinkedDir")
        os.symlink(self.outside / "secret.bin", self.root / "linked.bin")
        os.symlink(self.tmp / "dangling", self.root / "dangling")
        titles = self.titles()
        for name in ("LinkedDir", "linked.bin", "dangling"):
            self.assertEqual(titles[name].blocked, (f"symlink:{name}",))
            self.assertEqual((titles[name].files, titles[name].total_bytes), ((), 0))
        self.assertEqual(titles["LinkedDir"].kind, "file")  # a link, not a directory

    def test_nested_symlinked_dir_and_file_not_followed_title_blocked(self):
        self.write("Series/ok.bin", b"12345")
        os.symlink(self.outside, self.root / "Series" / "Season 1")
        os.symlink(self.outside / "secret.bin", self.root / "Series" / "ep.bin")
        title = self.titles()["Series"]
        self.assertEqual(title.blocked, ("symlink:Series/Season 1", "symlink:Series/ep.bin"))
        self.assertEqual([f.rel_path for f in title.files], ["Series/ok.bin"])
        self.assertEqual(title.total_bytes, 5)

    def test_fifo_blocked_without_hanging(self):
        os.mkfifo(self.root / "pipe")
        self.write("Series/ok.bin")
        os.mkfifo(self.root / "Series" / "nested-pipe")
        titles = self.titles()
        self.assertEqual(titles["pipe"].blocked, ("special_file:pipe",))
        self.assertEqual(titles["Series"].blocked, ("special_file:Series/nested-pipe",))
        self.assertEqual(len(titles["Series"].files), 1)

    def test_control_character_name_blocked(self):
        self.write("Series/bad\nname.bin")
        self.write("Series/fine.bin")
        title = self.titles()["Series"]
        self.assertEqual(len(title.blocked), 1)
        self.assertTrue(title.blocked[0].startswith("control_name:"))
        self.assertEqual([f.rel_path for f in title.files], ["Series/fine.bin"])

    @unittest.skipUnless(LINUX, "undecodable names cannot be created on macOS filesystems")
    def test_undecodable_name_blocked_and_title_rel_paths_unique(self):
        (self.root / "Series").mkdir()
        for raw in (b"bad\xff.bin", b"bad\xfe.bin"):
            with open(os.fsencode(self.root) + b"/Series/" + raw, "wb") as handle:
                handle.write(b"x")
        for raw in (b"top\xff", b"top\xfe"):
            with open(os.fsencode(self.root) + b"/" + raw, "wb") as handle:
                handle.write(b"x")
        titles = self.titles()
        self.assertEqual(len(titles["Series"].blocked), 2)
        self.assertTrue(all(b.startswith("undecodable_name:") for b in titles["Series"].blocked))
        self.assertEqual(titles["Series"].files, ())
        self.assertEqual(len([t for t in titles if t.startswith("top")]), 2)
        for t in titles.values():
            t.rel_path.encode("utf-8")

    def test_depth_limit_blocks_deeper_directories(self):
        self.write("T/a/b/c/deep.bin")
        self.write("T/a/shallow.bin")
        title = self.titles(max_depth=3)["T"]  # T=1, a=2, b=3, c=4
        self.assertEqual(title.blocked, ("depth_limit:T/a/b/c",))
        self.assertEqual([f.rel_path for f in title.files], ["T/a/shallow.bin"])
        self.assertEqual(self.titles(max_depth=4)["T"].blocked, ())
        self.assertEqual(self.titles(max_depth=1)["T"].blocked, ("depth_limit:T/a",))

    def test_deep_tree_does_not_blow_the_stack(self):
        path = self.root / "T"
        for i in range(100):
            path /= "d"
        path.mkdir(parents=True)
        title = self.titles(max_depth=64)["T"]
        self.assertEqual(len(title.blocked), 1)
        self.assertTrue(title.blocked[0].startswith("depth_limit:"))

    def test_entry_limit(self):
        for i in range(5):
            self.write(f"T/f{i}.bin")
        self.assertEqual(scan(self.root, "main", max_entries=6).entries, 6)
        with self.assertRaises(KuraError) as ctx:
            scan(self.root, "main", max_entries=5)
        self.assertEqual(ctx.exception.code, "inventory_limit")

    def test_entry_limit_is_shared_across_titles(self):
        for i in range(10):
            self.write(f"T{i}.bin")
        with self.assertRaises(KuraError) as ctx:
            scan(self.root, "main", max_entries=9)
        self.assertEqual(ctx.exception.code, "inventory_limit")

    def test_quarantine_directory_skipped_only_at_top_level(self):
        self.write(".kura-quarantine/old/ep.bin")
        self.write("Series/.kura-quarantine/ep.bin")
        titles = self.titles()
        self.assertNotIn(".kura-quarantine", titles)
        self.assertEqual(len(titles["Series"].files), 1)
        self.assertEqual(set(self.titles(skip=())), {".kura-quarantine", "Series"})
        self.assertEqual(scan(self.root, "main").entries, 3)  # skipped entries not counted

    def test_blocked_list_is_capped(self):
        (self.root / "T").mkdir()
        for i in range(inventory.MAX_BLOCKED + 10):
            os.symlink(self.outside, self.root / "T" / f"l{i:03d}")
        blocked = self.titles()["T"].blocked
        self.assertEqual(len(blocked), inventory.MAX_BLOCKED + 1)
        self.assertEqual(blocked[-1], "blocked_truncated")

    def test_mount_crossing_blocks_every_entry_not_on_root_device(self):
        self.write("Series/ep.bin")
        self.write("movie.bin")
        real = os.fstat
        calls = []

        def lying_fstat(fd):
            calls.append(fd)
            st = real(fd)
            if len(calls) == 1:  # the root: pretend it is on another device
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *st[3:]))
            return st

        with patch("kura.inventory.os.fstat", lying_fstat):
            titles = {t.rel_path: t for t in scan(self.root, "main").titles}
        self.assertEqual(titles["Series"].blocked, ("mount_crossing:Series",))
        self.assertEqual(titles["movie.bin"].blocked, ("mount_crossing:movie.bin",))
        self.assertEqual(titles["Series"].files, ())

    def test_directory_swapped_for_another_device_after_listing_is_blocked(self):
        self.write("Series/Season 1/ep.bin")
        real = os.fstat
        seen = []

        def lying_fstat(fd):
            st = real(fd)
            seen.append(st)
            if len(seen) == 3:  # "Season 1": a different mount appeared there
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *st[3:]))
            return st

        with patch("kura.inventory.os.fstat", lying_fstat):
            title = {t.rel_path: t for t in scan(self.root, "main").titles}["Series"]
        self.assertEqual(title.blocked, ("mount_crossing:Series/Season 1",))
        self.assertEqual(title.files, ())

    def test_unreadable_directory_blocked(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.write("Series/locked/ep.bin")
        locked = self.root / "Series" / "locked"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        title = self.titles()["Series"]
        self.assertEqual(title.blocked, ("unreadable:Series/locked",))


class ReadOnlyScanTests(TreeCase):
    def test_scan_never_opens_a_file_and_leaves_tree_unchanged(self):
        self.write("Series/Season 1/ep1.bin", b"abc")
        self.write("movie.bin", b"abc")
        os.symlink(self.outside, self.root / "link")
        os.mkfifo(self.root / "pipe")
        before = snapshot(self.root)
        real_open = os.open
        opened = []

        def only_directories(path, flags, *a, **k):
            opened.append(flags)
            if not flags & os.O_DIRECTORY:
                raise AssertionError("scan opened a non-directory")
            self.assertEqual(flags & WRITE_BITS, 0)
            self.assertTrue(flags & os.O_NOFOLLOW)
            return real_open(path, flags, *a, **k)

        def forbidden(*a, **k):
            raise AssertionError("scan used a buffered open")

        with patch("os.open", only_directories), patch.object(builtins, "open", forbidden), \
                patch.object(io, "open", forbidden), patch("os.fdopen", forbidden):
            inv = scan(self.root, "main")
        self.assertGreaterEqual(len(opened), 3)
        self.assertEqual(len(inv.titles), 4)
        self.assertEqual(snapshot(self.root), before)

    def test_scan_is_deterministic(self):
        for name in ("b", "a", "c"):
            self.write(f"{name}/x.bin")
        one, two = scan(self.root, "main"), scan(self.root, "main")
        self.assertEqual(one.titles, two.titles)
        self.assertEqual([t.rel_path for t in one.titles], ["a", "b", "c"])

    def test_no_descriptor_leak(self):
        self.write("Series/Season 1/ep.bin")
        os.symlink(self.outside, self.root / "Series" / "link")
        before = fd_count()
        if before is None:
            self.skipTest("no fd listing on this platform")
        for _ in range(10):
            scan(self.root, "main")
        with self.assertRaises(KuraError):
            scan(self.root, "main", max_entries=1)
        self.assertEqual(fd_count(), before)


if __name__ == "__main__":
    unittest.main()
