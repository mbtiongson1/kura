"""AniList ID list files (retain list, policy overrides). Synthetic IDs only."""
import os
from pathlib import Path
import tempfile
import unittest

from kura.idlist import IdList, MAX_BYTES, MAX_LINES, parse, parse_pins
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


class Pins(Case):
    def refused_pins(self, path):
        with self.assertRaises(KuraError) as caught:
            parse_pins(path)
        self.assertEqual(caught.exception.code, "invalid_policy")
        return caught.exception.details

    def test_ids_not_anime_comments_and_verbatim_keys(self):
        path = self.write("# owner pins\n\n999000001\tmain/Kura Alpha #1 (2019)\n"
                          "  # indented comment\nnot-anime\tmain/[x] Kura  Beta.mkv\n", "pins.tsv")
        pins = parse_pins(path)
        self.assertEqual(pins.entries, {"main/Kura Alpha #1 (2019)": 999000001, "main/[x] Kura  Beta.mkv": None})
        self.assertEqual(pins.summary(), {"sha256": pins.sha256, "pins": 2})
        reordered = self.write("not-anime\tmain/[x] Kura  Beta.mkv\n999000001\tmain/Kura Alpha #1 (2019)\n",
                               "again.tsv")
        self.assertEqual(parse_pins(reordered).sha256, pins.sha256)
        self.assertEqual(parse_pins(self.write("", "empty.tsv")).entries, {})
        odd = self.write("999000001\tmain/Kura Alpha \r\n999000002\tmain/a\tb\x0c\n999000003\tmain/c", "odd.tsv")
        self.assertEqual(parse_pins(odd).entries, {"main/Kura Alpha ": 999000001, "main/a\tb\x0c": 999000002,
                                                   "main/c": 999000003})

    def test_bad_lines_refuse_the_whole_file(self):
        for text in ("999000001 main/Kura Alpha\n",            # no tab
                     "abc\tmain/Kura Alpha\n", "0\tmain/Kura Alpha\n", "1000000000\tmain/Kura Alpha\n",
                     "999000001\t\n", "999000001\r\n",
                     "999000001\tmain/Kura Alpha\nnot-anime\tmain/Kura Alpha\n",
                     "999000001\tmain/Kura Alpha\n999000001\tmain/Kura Alpha\n"):
            details = self.refused_pins(self.write(text, "bad.tsv"))
            self.assertIn("line", details, text)
        self.assertEqual(self.refused_pins(self.write("", "bin.tsv", raw=b"\xff\tmain/x\n")), {})
        self.assertEqual(self.refused_pins(self.write("", "big.tsv", raw=b"#" * (MAX_BYTES + 1))), {})


class PinsWithParts(Case):
    def parse(self, text):
        return parse_pins(self.write(text, "pins.tsv"))

    def refused_at(self, text):
        with self.assertRaises(KuraError) as caught:
            self.parse(text)
        self.assertEqual(caught.exception.code, "invalid_policy")
        return caught.exception.details

    def test_old_files_keep_their_digest(self):
        plain = self.parse("999000001\tmain/Kura Alpha\nnot-anime\tmain/Kura Beta\n")
        import hashlib, json
        legacy = hashlib.sha256(json.dumps(sorted(plain.entries.items()), separators=(",", ":"),
                                           ensure_ascii=False).encode()).hexdigest()
        self.assertEqual((plain.sha256, plain.parts), (legacy, {}))

    def test_selector_lines(self):
        pins = self.parse("999000001@auto\tmain/Kura Saga\n999000002@auto\tmain/Kura Saga\n"
                          "999000003@season:02\tmain/Kura Other\n999000004@dir:Season 2\tmain/Kura Other\n"
                          "999000005\tmain/Kura Plain\n")
        self.assertEqual(pins.parts, {"main/Kura Saga": [(999000001, "auto"), (999000002, "auto")],
                                      "main/Kura Other": [(999000003, "season:2"), (999000004, "dir:Season 2")]})
        self.assertEqual(pins.entries, {"main/Kura Plain": 999000005})
        self.assertEqual(pins.summary()["pins"], 3)
        other = self.parse("999000005\tmain/Kura Plain\n999000004@dir:Season 2\tmain/Kura Other\n"
                           "999000003@season:2\tmain/Kura Other\n999000002@auto\tmain/Kura Saga\n"
                           "999000001@auto\tmain/Kura Saga\n")
        self.assertNotEqual(other.sha256, pins.sha256)            # part order is meaningful (ordinals)

    def test_refusals_carry_the_line_number(self):
        for text, line in [("999000001\tmain/A\n999000002@auto\tmain/A\n", 2),
                           ("999000002@auto\tmain/A\n999000001\tmain/A\n", 2),
                           ("1@auto\tmain/A\n2@auto\tmain/A\n3@rest\tmain/A\n4@rest\tmain/A\n", 4),
                           ("1@season:1\tmain/A\n2@season:1\tmain/A\n", 2),
                           ("1@auto\tmain/A\n1@auto\tmain/A\n", 2),
                           ("1@all\tmain/A\n2@auto\tmain/A\n", 2),
                           ("1@auto\tmain/A\n2@all\tmain/A\n", 2),
                           ("not-anime@auto\tmain/A\n", 1),
                           ("1@guess\tmain/A\n", 1), ("1@dir:../x\tmain/A\n", 1), ("1@dir:/x\tmain/A\n", 1),
                           ("1@dir:a\x01b\tmain/A\n", 1), ("1@ep:5-3\tmain/A\n", 1), ("1@\tmain/A\n", 1)]:
            with self.subTest(text=text):
                self.assertEqual(self.refused_at(text).get("line"), line)


if __name__ == "__main__":
    unittest.main()
