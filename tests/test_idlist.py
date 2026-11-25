"""AniList ID list files (retain list, policy overrides). Synthetic IDs only."""
import os
from pathlib import Path
import tempfile
import unittest

from kura.idlist import IdList, MAX_BYTES, MAX_LINES, parse
from kura.models import KuraError


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(os.path.realpath(self._tmp.name))

    def write(self, text, name="retain.txt", raw=None):
        path = self.dir / name
        path.write_bytes(raw if raw is not None else text.encode())
        return path

    def refused(self, path, **kw):
        with self.assertRaises(KuraError) as caught:
            parse(path, **kw)
        self.assertEqual(caught.exception.code, "invalid_policy")
        return caught.exception.details


class Parsing(Case):
    def test_comments_blank_lines_and_duplicates(self):
        got = parse(self.write("# Kura retain list\n\n999000002  # second\n999000001\n999000002\n   # only a comment\n"))
        self.assertEqual(got.ids, {999000001: None, 999000002: None})
        self.assertEqual(got.summary(), {"sha256": got.sha256, "ids": 2})
        self.assertIn(999000001, got)
        self.assertNotIn(5, got)

    def test_digest_depends_only_on_the_sorted_ids(self):
        a = parse(self.write("999000002\n999000001\n"))
        b = parse(self.write("999000001 # x\r\n999000002\r\n\r\n", name="b.txt"))
        c = parse(self.write("999000001\n", name="c.txt"))
        self.assertEqual(a.sha256, b.sha256)
        self.assertNotEqual(a.sha256, c.sha256)
        self.assertEqual(len(a.sha256), 64)

    def test_empty_file_is_an_empty_list(self):
        got = parse(self.write("# nothing\n"))
        self.assertEqual((got.ids, got.summary()["ids"]), ({}, 0))

    def test_bad_lines_report_their_number(self):
        for text, line in (("999000001\nabc\n", 2), ("0\n", 1), ("-5\n", 1), ("1000000000\n", 1),
                           ("\n\n999000001 999000002\n", 3), ("999000001.5\n", 1), ("0999\n", 1),
                           ("٣٤\n", 1), ("999000001 archival\n", 1), ("12345678901\n", 1)):
            with self.subTest(text=text):
                self.assertEqual(self.refused(self.write(text)), {"line": line})

    def test_limits_and_encoding(self):
        self.assertNotIn("line", self.refused(self.write("", raw=b"999000001\n\xff\xfe\n")))
        self.assertNotIn("line", self.refused(self.write("", raw=b"#" * (MAX_BYTES + 1))))
        self.refused(self.write("\n" * (MAX_LINES + 1)))
        self.assertEqual(len(parse(self.write("\n".join(str(999000001 + i) for i in range(MAX_LINES)))).ids),
                         MAX_LINES)

    def test_unreadable_missing_and_symlinked_files_are_refused(self):
        self.refused(self.dir / "missing.txt")
        target = self.write("999000001\n")
        link = self.dir / "link.txt"
        link.symlink_to(target)
        self.refused(link)
        self.refused(self.dir)

    def test_a_fifo_is_refused_without_blocking(self):
        fifo = self.dir / "pipe"
        os.mkfifo(fifo)
        self.refused(fifo)


class Tags(Case):
    TAGS = ("archival", "efficient")

    def test_tagged_lines(self):
        got = parse(self.write("999000001 archival  # keep\n999000002\tefficient\n999000001 archival\n"),
                    tags=self.TAGS)
        self.assertEqual(got.ids, {999000001: "archival", 999000002: "efficient"})

    def test_digest_covers_the_tags(self):
        a = parse(self.write("999000001 archival\n"), tags=self.TAGS)
        b = parse(self.write("999000001 efficient\n", name="b.txt"), tags=self.TAGS)
        self.assertNotEqual(a.sha256, b.sha256)

    def test_bad_tag_missing_tag_and_conflict(self):
        for text, line in (("999000001\n", 1), ("999000001 other\n", 1), ("999000001 archival extra\n", 1),
                           ("999000001 archival\n999000001 efficient\n", 2)):
            with self.subTest(text=text):
                self.assertEqual(self.refused(self.write(text), tags=self.TAGS), {"line": line})


if __name__ == "__main__":
    unittest.main()
