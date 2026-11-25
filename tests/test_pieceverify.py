"""Piece verification: boundary math, pad files, verdict rules, read-only safety, limits."""
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

import torrentgen
from kura import pieceverify, rofs
from kura.models import KuraError
from kura.pieceverify import default_mapping, verify_tree
from kura.torrentmeta import parse

PIECE = torrentgen.PIECE
WRITE_BITS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL)


def snapshot(root: Path):
    out = {}
    for path in sorted([root, *root.rglob("*")]):
        st = path.lstat()
        out[str(path.relative_to(root))] = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "lib"
        self.root.mkdir()

    def make(self, files, **kw):
        built = torrentgen.build("Kura Sample Series", files, **kw)
        built.write_tree(self.root)
        return built, parse(built.data)

    def statuses(self, result):
        return {f.path.split("/", 1)[-1]: f.status for f in result.files}


UNEVEN = [("ep01.mkv", torrentgen.payload("a", 20000)),
          ("ep02.mkv", torrentgen.payload("b", 3000)),
          ("ep03.mkv", torrentgen.payload("c", 25000))]


class Verified(Base):
    def test_three_uneven_files_complete(self):
        built, meta = self.make(UNEVEN)
        before = snapshot(self.root)
        r = verify_tree(meta, self.root)
        self.assertEqual(r.verdict, "complete")
        self.assertEqual({f.status for f in r.files}, {"verified"})
        self.assertEqual(r.pieces_total, torrentgen.piece_count(48000))
        self.assertEqual((r.pieces_ok, r.pieces_failed, r.pieces_skipped), (r.pieces_total, 0, 0))
        self.assertEqual(r.bytes_read, 48000)
        self.assertEqual(r.files[0].evidence["size"], 20000)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(r.json()["files"][0]["status"], "verified")

    def test_small_chunks_same_result(self):
        _, meta = self.make(UNEVEN)
        r = verify_tree(meta, self.root, chunk_size=1000)
        self.assertEqual(r.verdict, "complete")
        self.assertEqual(r.bytes_read, 48000)

    def test_single_file_torrent(self):
        built = torrentgen.build("Kura Sample Film", size=50000)
        built.write_tree(self.root)
        r = verify_tree(parse(built.data), self.root)
        self.assertEqual(r.verdict, "complete")

    def test_zero_length_files_and_base(self):
        files = [("a.mkv", torrentgen.payload("a", 20000)), ("empty.txt", b""),
                 ("b.mkv", torrentgen.payload("b", 5000))]
        built = torrentgen.build("Kura Sample Series", files)
        built.write_tree(self.root / "extra")
        meta = parse(built.data)
        r = verify_tree(meta, self.root, default_mapping(meta, ("extra",)))
        self.assertEqual(r.verdict, "complete")
        self.assertEqual(r.files[1].status, "verified")
        self.assertEqual(r.files[1].actual_size, 0)
        self.assertEqual(r.files[0].local, "extra/Kura Sample Series/a.mkv")

    def test_empty_file_missing_not_verified(self):
        files = [("a.mkv", torrentgen.payload("a", 20000)), ("empty.txt", b"")]
        _, meta = self.make(files)
        (self.root / "Kura Sample Series" / "empty.txt").unlink()
        r = verify_tree(meta, self.root)
        self.assertEqual(self.statuses(r)["empty.txt"], "missing")
        self.assertEqual(r.verdict, "partial")


class Pads(Base):
    def test_pad_files_zero_filled_never_opened(self):
        built, meta = self.make(UNEVEN, pad=True)
        self.assertTrue(any(f.pad for f in meta.files))
        opened = []
        real_open = os.open

        def spy(path, flags, *a, **k):
            opened.append(str(path))
            return real_open(path, flags, *a, **k)
        with patch("os.open", spy):
            r = verify_tree(meta, self.root)
        self.assertEqual(r.verdict, "complete")
        self.assertTrue(opened)
        self.assertFalse([p for p in opened if p.startswith(".pad")])
        pads = [f for f in r.files if f.status == "pad"]
        self.assertEqual(len(pads), 2)
        self.assertTrue(all(f.local is None and f.actual_size is None for f in pads))
        self.assertEqual(r.bytes_read, 48000)  # pad bytes are not read

    def test_pad_corruption_neighbour_detected(self):
        built, meta = self.make(UNEVEN, pad=True)
        target = self.root / "Kura Sample Series" / "ep01.mkv"
        data = bytearray(target.read_bytes())
        data[-1] ^= 1
        target.write_bytes(bytes(data))
        r = verify_tree(meta, self.root)
        self.assertEqual(self.statuses(r)["ep01.mkv"], "piece_failed")
        self.assertEqual(self.statuses(r)["ep02.mkv"], "verified")


class Failures(Base):
    def test_one_corrupt_byte_fails_both_files_sharing_piece(self):
        _, meta = self.make(UNEVEN)
        # ep01 is 20000 bytes: piece 1 covers 16384..32768 = ep01 tail + ep02 + ep03 head
        target = self.root / "Kura Sample Series" / "ep02.mkv"
        data = bytearray(target.read_bytes())
        data[10] ^= 0xFF
        target.write_bytes(bytes(data))
        r = verify_tree(meta, self.root)
        s = self.statuses(r)
        self.assertEqual((s["ep01.mkv"], s["ep02.mkv"], s["ep03.mkv"]),
                         ("piece_failed", "piece_failed", "piece_failed"))
        self.assertGreaterEqual(r.pieces_failed, 1)
        self.assertEqual(r.verdict, "failed")

    def test_corruption_in_first_piece_only_fails_that_file(self):
        _, meta = self.make(UNEVEN)
        target = self.root / "Kura Sample Series" / "ep01.mkv"
        data = bytearray(target.read_bytes())
        data[5] ^= 1
        target.write_bytes(bytes(data))
        s = self.statuses(verify_tree(meta, self.root))
        self.assertEqual(s["ep01.mkv"], "piece_failed")
        self.assertEqual(s["ep02.mkv"], "verified")  # its only piece is piece 1, intact

    def test_missing_file_boundary_and_partial(self):
        _, meta = self.make(UNEVEN)
        (self.root / "Kura Sample Series" / "ep02.mkv").unlink()
        r = verify_tree(meta, self.root)
        s = self.statuses(r)
        self.assertEqual(s["ep02.mkv"], "missing")
        self.assertEqual(s["ep01.mkv"], "unverified_boundary")
        self.assertEqual(s["ep03.mkv"], "unverified_boundary")
        self.assertEqual(r.verdict, "failed")
        self.assertGreater(r.pieces_skipped, 0)
        self.assertEqual(r.pieces_ok + r.pieces_failed + r.pieces_skipped, r.pieces_total)

    def test_missing_file_with_pads_leaves_neighbours_verified(self):
        _, meta = self.make(UNEVEN, pad=True)
        (self.root / "Kura Sample Series" / "ep02.mkv").unlink()
        r = verify_tree(meta, self.root)
        s = self.statuses(r)
        self.assertEqual((s["ep01.mkv"], s["ep03.mkv"]), ("verified", "verified"))
        self.assertEqual(r.verdict, "partial")

    def test_short_and_extra_bytes_size_mismatch_not_read(self):
        _, meta = self.make(UNEVEN, pad=True)
        top = self.root / "Kura Sample Series"
        (top / "ep01.mkv").write_bytes((top / "ep01.mkv").read_bytes()[:-1])
        with (top / "ep03.mkv").open("ab") as fh:
            fh.write(b"x")
        r = verify_tree(meta, self.root)
        s = self.statuses(r)
        self.assertEqual((s["ep01.mkv"], s["ep03.mkv"]), ("size_mismatch", "size_mismatch"))
        self.assertEqual(s["ep02.mkv"], "verified")
        self.assertEqual(r.files[0].actual_size, 19999)
        self.assertEqual(r.bytes_read, 3000)

    def test_unmapped_file_is_missing(self):
        _, meta = self.make(UNEVEN)
        mapping = default_mapping(meta)
        del mapping[1]
        r = verify_tree(meta, self.root, mapping)
        self.assertEqual(self.statuses(r)["ep02.mkv"], "missing")

    def test_nothing_present_fails(self):
        _, meta = self.make(UNEVEN)
        shutil.rmtree(self.root / "Kura Sample Series")
        r = verify_tree(meta, self.root)
        self.assertEqual(r.verdict, "failed")
        self.assertEqual(r.bytes_read, 0)
        self.assertEqual({f.status for f in r.files}, {"missing"})


class Unsafe(Base):
    def test_symlink_fifo_dir_and_symlinked_parent(self):
        built, meta = self.make(UNEVEN)
        top = self.root / "Kura Sample Series"
        outside = self.tmp / "outside.bin"
        outside.write_bytes(torrentgen.payload("a", 20000))
        (top / "ep01.mkv").unlink()
        (top / "ep01.mkv").symlink_to(outside)
        (top / "ep02.mkv").unlink()
        os.mkfifo(top / "ep02.mkv")
        (top / "ep03.mkv").unlink()
        (top / "ep03.mkv").mkdir()
        r = verify_tree(meta, self.root)
        self.assertEqual({f.status for f in r.files}, {"unsafe"})
        self.assertEqual(r.bytes_read, 0)

    def test_symlinked_parent_dir(self):
        built, meta = self.make(UNEVEN)
        real = self.root / "Kura Sample Series"
        moved = self.tmp / "elsewhere"
        real.rename(moved)
        real.symlink_to(moved)
        r = verify_tree(meta, self.root)
        self.assertEqual({f.status for f in r.files}, {"unsafe"})
        self.assertEqual(r.bytes_read, 0)

    def test_root_symlink_raises(self):
        _, meta = self.make(UNEVEN)
        link = self.tmp / "rootlink"
        link.symlink_to(self.root)
        with self.assertRaises(KuraError) as ctx:
            verify_tree(meta, link)
        self.assertEqual(ctx.exception.code, "unsafe_path")

    def test_hostile_mapping_components_unsafe(self):
        _, meta = self.make(UNEVEN)
        outside = self.tmp / "secret.bin"
        outside.write_bytes(b"x" * 20000)
        mapping = default_mapping(meta)
        mapping[0] = ("..", "secret.bin")
        r = verify_tree(meta, self.root, mapping)
        self.assertEqual(r.files[0].status, "unsafe")

    def test_invalid_arguments(self):
        _, meta = self.make(UNEVEN)
        with self.assertRaises(KuraError):
            verify_tree(meta, self.root, chunk_size=0)
        with self.assertRaises(KuraError):
            verify_tree(meta, self.root, max_read_bytes=-1)


class Changes(Base):
    def test_modification_during_verify_marks_changed(self):
        _, meta = self.make(UNEVEN, pad=True)
        target = self.root / "Kura Sample Series" / "ep01.mkv"
        calls = []

        def stop():
            calls.append(1)
            if len(calls) == 20:  # well after the first chunks of ep01 were read
                st = target.stat()
                target.write_bytes(target.read_bytes())  # same size, same inode
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
            return False
        r = verify_tree(meta, self.root, should_stop=stop, chunk_size=1000)
        self.assertGreaterEqual(len(calls), 20)
        self.assertEqual(self.statuses(r)["ep01.mkv"], "changed")
        self.assertNotEqual(r.verdict, "complete")

    def test_replaced_file_inode_marks_changed(self):
        _, meta = self.make(UNEVEN, pad=True)
        target = self.root / "Kura Sample Series" / "ep01.mkv"
        data, calls = target.read_bytes(), []

        def stop():
            calls.append(1)
            if len(calls) == 5:
                tmp = target.with_name("swap")
                tmp.write_bytes(data)
                os.replace(tmp, target)  # same content and size, new inode
            return False
        r = verify_tree(meta, self.root, should_stop=stop, chunk_size=1000)
        self.assertEqual(self.statuses(r)["ep01.mkv"], "changed")

    def test_truncated_during_verify(self):
        _, meta = self.make(UNEVEN, pad=True)
        target = self.root / "Kura Sample Series" / "ep01.mkv"
        calls = []

        def stop():
            calls.append(1)
            if len(calls) == 5:
                os.truncate(target, 2000)
            return False
        r = verify_tree(meta, self.root, should_stop=stop, chunk_size=1000)
        self.assertEqual(self.statuses(r)["ep01.mkv"], "changed")
        self.assertEqual(self.statuses(r)["ep03.mkv"], "verified")
        self.assertEqual(r.pieces_ok + r.pieces_failed + r.pieces_skipped, r.pieces_total)


class Limits(Base):
    def test_budget_exact_boundary_still_complete(self):
        _, meta = self.make(UNEVEN)
        r = verify_tree(meta, self.root, max_read_bytes=48000, chunk_size=1000)
        self.assertEqual((r.verdict, r.bytes_read), ("complete", 48000))

    def test_budget_exhausted_is_exact_and_files_unverified(self):
        _, meta = self.make(UNEVEN, pad=True)
        r = verify_tree(meta, self.root, max_read_bytes=25000, chunk_size=7000)
        self.assertEqual(r.verdict, "budget_exhausted")
        self.assertEqual(r.bytes_read, 25000)
        self.assertGreater(r.pieces_skipped, 0)
        s = self.statuses(r)
        self.assertEqual(s["ep01.mkv"], "verified")
        self.assertEqual(s["ep03.mkv"], "unverified")
        self.assertEqual(r.pieces_ok + r.pieces_failed + r.pieces_skipped, r.pieces_total)

    def test_zero_budget_reads_nothing(self):
        _, meta = self.make(UNEVEN)
        r = verify_tree(meta, self.root, max_read_bytes=0)
        self.assertEqual((r.verdict, r.bytes_read), ("budget_exhausted", 0))
        self.assertEqual({f.status for f in r.files}, {"unverified"})

    def test_should_stop_interrupts_per_chunk(self):
        _, meta = self.make(UNEVEN)
        calls = []

        def stop():
            calls.append(1)
            return len(calls) > 10  # 3 precheck calls, then a few chunks
        r = verify_tree(meta, self.root, should_stop=stop, chunk_size=1000)
        self.assertEqual(r.verdict, "interrupted")
        self.assertEqual(r.bytes_read, 7000)
        self.assertNotEqual(self.statuses(r)["ep03.mkv"], "verified")

    def test_stop_before_anything(self):
        _, meta = self.make(UNEVEN)
        r = verify_tree(meta, self.root, should_stop=lambda: True)
        self.assertEqual((r.verdict, r.bytes_read), ("interrupted", 0))
        self.assertEqual({f.status for f in r.files}, {"unverified"})

    def test_memory_bounded_reads(self):
        _, meta = self.make([("big.bin", torrentgen.payload("z", 200000))])
        sizes = []
        real = os.read

        def spy(fd, n):
            sizes.append(n)
            return real(fd, n)
        with patch("os.read", spy):
            r = verify_tree(meta, self.root, chunk_size=4096)
        self.assertEqual(r.verdict, "complete")
        self.assertLessEqual(max(sizes), 4096)


class ReadOnly(Base):
    def test_open_flags_never_write_and_tree_unchanged(self):
        _, meta = self.make(UNEVEN, pad=True)
        before = snapshot(self.root)
        flags = []
        real_open = os.open

        def spy(path, f, *a, **k):
            flags.append(f)
            return real_open(path, f, *a, **k)
        with patch("os.open", spy), \
                patch("os.rename", side_effect=AssertionError), \
                patch("os.replace", side_effect=AssertionError), \
                patch("os.unlink", side_effect=AssertionError), \
                patch("os.truncate", side_effect=AssertionError), \
                patch("os.mkdir", side_effect=AssertionError), \
                patch("os.write", side_effect=AssertionError):
            r = verify_tree(meta, self.root)
        self.assertEqual(r.verdict, "complete")
        self.assertTrue(flags)
        for f in flags:
            self.assertEqual(f & WRITE_BITS, 0, hex(f))
            self.assertTrue(f & os.O_NOFOLLOW)
        self.assertEqual(snapshot(self.root), before)

    def test_module_has_no_direct_file_opens(self):
        import re
        src = Path(pieceverify.__file__).read_text()
        self.assertIsNone(re.search(r"\bos\.open\(|\bio\.open\(|(?<![\w.])open\(", src))


if __name__ == "__main__":
    unittest.main()
