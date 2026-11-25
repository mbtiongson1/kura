"""The scoring-only release-name tokenizer. Synthetic names only."""
import unittest

from kura import names


class Tokens(unittest.TestCase):
    def test_leading_group_episode_version_and_tags(self):
        n = names.parse("[Kura Encodes] Kura Sample Series - 05v2 [1080p][x265 10bit][ABCD1234].mkv")
        self.assertEqual((n["group"], n["episode"], n["version"], n["resolution"]), ("Kura Encodes", 5, 2, "1080p"))
        self.assertEqual((n["codec"], n["bit_depth"], n["crc"], n["extra"]), ("hevc", 10, "ABCD1234", False))

    def test_trailing_group_scene_style(self):
        n = names.parse("Kura.Sample.Series.S01E05.1080p.WEB-DL.x264-Example.mkv")
        self.assertEqual((n["group"], n["episode"], n["version"], n["source"], n["codec"]),
                         ("Example", 5, None, "WEB-DL", "avc"))

    def test_web_dl_suffix_is_not_a_group(self):
        n = names.parse("Show - 03 1080p WEB-DL.mkv")
        self.assertEqual((n["group"], n["episode"], n["source"]), (None, 3, "WEB-DL"))

    def test_hyphenated_words_are_not_groups(self):
        for text in ("Show - S01E01 - Anti-Hero.mkv", "Show - 01 Blu-ray", "Show - 01 Dual-Audio", "Show - 01 Multi-Subs"):
            with self.subTest(text=text):
                self.assertIsNone(names.parse(text)["group"])
        self.assertEqual(names.parse("Show - 01 1080p BluRay-Example.mkv")["group"], "Example")
        self.assertEqual(names.parse("Show.01.720p.x265-Example")["group"], "Example")

    def test_scene_groups_after_numeric_tags_and_years_are_not_episodes(self):
        n = names.parse("Show.S01E01.1080p.WEB-DL.DDP5.1.H.264-GROUP.mkv")
        self.assertEqual((n["group"], n["episode"]), ("GROUP", 1))
        self.assertEqual(names.parse("Show.1080p.AAC2.0-GROUP.mkv")["group"], "GROUP")
        self.assertIsNone(names.parse("[Gr] Show - 2019 Special (BD 1080p).mkv")["episode"])
        self.assertEqual(names.parse("[Gr] Show - 1080 [720p]")["episode"], None)

    def test_season_and_ordinal_titles(self):
        self.assertEqual(names.parse("Show S02E05v2.mkv")["season"], 2)
        self.assertIsNone(names.parse("Show - 05.mkv")["season"])
        n = names.parse("[G] Show - 2nd Season - 05v2.mkv")
        self.assertEqual((n["episode"], n["version"]), (5, 2))

    def test_tag_like_leading_brackets_are_not_groups(self):
        self.assertIsNone(names.parse("[1080p] Show - 01.mkv")["group"])
        self.assertIsNone(names.parse("[ABCD1234] Show - 01.mkv")["group"])

    def test_sources(self):
        for text, expected in (("[Example Subs] Show - 01 (BDRip 1080p).mkv", "BDRip"),
                               ("Show - 01 [BD 1080p Hi10P]", "BD"), ("Show - 01 [BD Remux]", "Remux"),
                               ("Show - 01 (WEBRip 720p)", "WEBRip"), ("Show - 01 WEB 1080p", "WEB"),
                               ("Show - 01 [TV]", "TV"), ("Show - 01 DVDRip", "DVD"), ("Show - 01", None)):
            with self.subTest(text=text):
                self.assertEqual(names.parse(text)["source"], expected)

    def test_resolution_forms(self):
        for text, expected in (("Show - 01 1920x1080", "1080p"), ("Show - 01 [4K]", "2160p"),
                               ("Show - 01 480i", "480i"), ("Show - 01", None)):
            self.assertEqual(names.parse(text)["resolution"], expected)

    def test_bit_depth_forms(self):
        self.assertEqual(names.parse("Show - 01 [Hi10P]")["bit_depth"], 10)
        self.assertEqual(names.parse("Show - 01 8bit")["bit_depth"], 8)
        self.assertIsNone(names.parse("Show - 01")["bit_depth"])

    def test_episode_forms(self):
        for text, ep, ver in (("Show - 07", 7, None), ("Show E12", 12, None), ("Show EP03v3", 3, 3),
                              ("Show - 108", 108, None), ("Show 12v2 [1080p]", 12, 2), ("Show #4", 4, None),
                              ("Show - 01-12", None, None), ("Show 1080p", None, None)):
            with self.subTest(text=text):
                n = names.parse(text)
                self.assertEqual((n["episode"], n["version"]), (ep, ver))

    def test_resolution_is_not_an_episode(self):
        self.assertIsNone(names.parse("Show [1080p][x265]")["episode"])

    def test_extras_flag(self):
        self.assertTrue(names.parse("Show/Extras/anything.mkv")["extra"])
        self.assertTrue(names.parse("Show - NCOP1.mkv")["extra"])
        self.assertFalse(names.parse("Show - 01.mkv")["extra"])

    def test_unknown_fields_are_none_and_garbage_is_tolerated(self):
        for text in ("", "....", "[]", "-", "\x00\x01", "x" * 5000):
            n = names.parse(text)
            self.assertEqual(set(n), set(names.FIELDS))
        n = names.parse("")
        self.assertTrue(all(n[k] is None for k in names.FIELDS if k != "extra"))

    def test_path_and_backslashes_use_the_last_component(self):
        self.assertEqual(names.parse("Season 1\\[Example Subs] Show - 02.mkv")["group"], "Example Subs")

    def test_pure_and_repeatable(self):
        name = "[Example Subs] Show - 02 [720p].mkv"
        self.assertEqual(names.parse(name), names.parse(name))


if __name__ == "__main__":
    unittest.main()
