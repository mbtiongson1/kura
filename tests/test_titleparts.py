"""Part assignment engine: pure rules, table-driven. Synthetic shows only."""
import random
import unittest

from kura.identity import carries_season, carries_season_strict
from kura.titleparts import assign, canonical_selector, cumulative, season_part


def part(i, ordinal=None, chain=None, selector="auto", source="relations"):
    return {"anilist_id": i, "selector": selector, "source": source, "ordinal": ordinal or i, "chain_pos": chain}


def info(titles, fmt="TV", episodes=12, status="FINISHED"):
    return {"titles": titles, "format": fmt, "episodes": episodes, "status": status}


# Example Show: three seasons (the third split in two cours), a movie.
MEDIA = {
    1: info(["Example Show"], episodes=12),
    2: info(["Example Show 2nd Season", "Example Show Second Season"], episodes=12),
    3: info(["Example Show Season 3"], episodes=12),
    4: info(["Example Show Season 3 Part 2"], episodes=12),
    5: info(["Example Show Movie: Sunset"], fmt="MOVIE", episodes=1),
}
PARTS = [part(1, chain=1), part(2, chain=2), part(3, chain=3), part(4, chain=4), part(5)]


def eps(prefix, lo, hi, tail=".mkv"):
    return [f"{prefix}Example Show - {n:02d}{tail}" for n in range(lo, hi + 1)]


def run(files, parts=PARTS, media=MEDIA):
    out = assign(files, parts, media)
    return {p: v["part"] for p, v in out["files"].items()}, out


class Selectors(unittest.TestCase):
    def test_canonical(self):
        for text, want in [("all", "all"), (" auto ", "auto"), ("rest", "rest"), ("season:02", "season:2"),
                           ("ep:013-24", "ep:13-24"), ("dir:Season 2", "dir:Season 2"),
                           ("file:Movies/a.mkv", "file:Movies/a.mkv")]:
            self.assertEqual(canonical_selector(text), want)
        for bad in ["", "season:100", "season:x", "ep:5-3", "ep:0-3", "ep:1-10000", "dir:", "dir:/abs", "dir:a//b",
                    "dir:a/../b", "dir:./a", "file:a\x01b", "dir:" + "x" * 501, "guess", "auto:1", 5]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                canonical_selector(bad)


class Seasons(unittest.TestCase):
    def test_strict_carries(self):
        self.assertFalse(carries_season_strict("Example Show Season 3 Part 2", 2))
        self.assertTrue(carries_season_strict("Example Show Season 3 Part 2", 3))
        self.assertTrue(carries_season_strict("Example Show Second Season", 2))
        self.assertTrue(carries_season_strict("Example Show 3rd Season", 3))
        self.assertTrue(carries_season_strict("Example Show 2", 2))
        self.assertFalse(carries_season_strict("Example Show Part 2", 2))
        self.assertFalse(carries_season_strict("Example Show Cour 2", 2))
        self.assertFalse(carries_season_strict("Example Show Season 3 2", 2))      # an explicit marker wins

    def test_loose_matcher_is_unchanged(self):
        self.assertTrue(carries_season("Example Show Season 3 Part 2", 2))

    def test_season_part(self):
        self.assertEqual(season_part(2, PARTS, MEDIA), ([2], None))
        self.assertEqual(season_part(3, PARTS, MEDIA), ([3, 4], None))   # a split cour: both carry season 3
        self.assertEqual(season_part(1, PARTS, MEDIA), ([1], None))
        self.assertEqual(season_part(7, PARTS, MEDIA), ([], "season_unmatched"))


class Directories(unittest.TestCase):
    def test_season_dirs(self):
        got, out = run(["Season 1/" + f for f in eps("", 1, 2)] + ["Season 2/" + f for f in eps("", 1, 2)]
                       + ["2nd Season/x - 05.mkv", "S2/x - 06.mkv"])
        self.assertEqual(sorted(set(got.values())), [1, 2])
        self.assertTrue(all(got[p] == 1 for p in got if p.startswith("Season 1/")))
        self.assertEqual(got["2nd Season/x - 05.mkv"], 2)
        self.assertEqual(got["S2/x - 06.mkv"], 2)
        self.assertTrue(out["complete"])

    def test_range_dir_is_ambiguous(self):
        got, out = run(["Season 1-3/a - 01.mkv"])
        self.assertEqual(out["files"]["Season 1-3/a - 01.mkv"]["reason"], "dir_ambiguous")
        self.assertFalse(out["complete"])

    def test_dir_and_file_marker_conflict(self):
        _, out = run(["Season 2/Example Show S03E01.mkv"])
        self.assertEqual(out["files"]["Season 2/Example Show S03E01.mkv"]["reason"], "marker_conflict")

    def test_file_marker_alone(self):
        got, _ = run(["Example Show S02E01.mkv"])
        self.assertEqual(got["Example Show S02E01.mkv"], 2)

    def test_unmatched_season_stays_unassigned(self):
        _, out = run(["Season 9/a - 01.mkv"])
        self.assertEqual(out["files"]["Season 9/a - 01.mkv"]["reason"], "season_unmatched")

    def test_split_cour_resolved_by_numbering_in_the_group(self):
        files = ["Season 3/" + f for f in eps("", 1, 24)]
        got, out = run(files)
        self.assertEqual({got[f] for f in files[:12]}, {3})
        self.assertEqual({got[f] for f in files[12:]}, {4})
        self.assertTrue(out["complete"])

    def test_split_cour_with_a_gap_is_unassigned(self):
        files = ["Season 3/" + f for f in eps("", 1, 23)]
        got, out = run(files)
        self.assertEqual(set(got.values()), {None})
        self.assertEqual(out["reasons"], {"absolute_mismatch": 23})

    def test_dir_naming_one_part(self):
        media = {**MEDIA, 6: info(["Example Show: Despair Arc"])}
        got, _ = run(["Despair Arc/a - 01.mkv"], [part(1, chain=1), part(6)], media)
        self.assertEqual(got["Despair Arc/a - 01.mkv"], 6)

    def test_dir_equal_to_the_title_is_not_decisive(self):
        got, out = run(["Example Show/a - 01.mkv"], [part(1, chain=1), part(2, chain=2)])
        self.assertIsNone(got["Example Show/a - 01.mkv"])


class Cumulative(unittest.TestCase):
    TWO = [part(1, chain=1), part(2, chain=2)]

    def test_absolute_numbering_maps(self):
        got, out = run(eps("", 1, 24), self.TWO)
        self.assertEqual({got[f] for f in eps("", 1, 12)}, {1})
        self.assertEqual({got[f] for f in eps("", 13, 24)}, {2})
        self.assertEqual(out["files"][eps("", 13, 13)[0]]["rule"], "cumulative")

    def test_boundary_required(self):
        got, out = run(eps("", 1, 23), self.TWO)
        self.assertEqual(set(got.values()), {None})
        self.assertEqual(out["reasons"], {"absolute_mismatch": 23})

    def test_duplicate_numbers_unassign_everything(self):
        files = eps("a/", 1, 12) + eps("b/", 1, 12)
        got, _ = run(files, self.TWO)
        self.assertEqual(set(got.values()), {None})

    def test_version_duplicates_collapse(self):
        files = eps("", 1, 12) + ["Example Show - 05v2.mkv"]
        got, out = run(files, self.TWO)
        self.assertEqual(set(got.values()), {1})
        self.assertTrue(out["complete"])

    def test_unknown_count(self):
        media = {**MEDIA, 1: info(["Example Show"], episodes=None)}
        got, out = run(eps("", 1, 12), self.TWO, media)
        self.assertEqual(out["reasons"], {"count_unknown": 12})

    def test_unfinished_middle_part(self):
        media = {**MEDIA, 1: info(["Example Show"], status="RELEASING")}
        self.assertEqual(cumulative({"x": (1, None)}, self.TWO, media)[1], "absolute_mismatch")

    def test_decimal_episode_is_not_an_episode(self):
        got, _ = run(["Example Show - 12.5.mkv"], self.TWO)
        self.assertIsNone(got["Example Show - 12.5.mkv"])


class Movies(unittest.TestCase):
    def test_movie_by_title(self):
        got, _ = run(["Movies/Example Show Movie Sunset.mkv"] + eps("Season 1/", 1, 1))
        self.assertEqual(got["Movies/Example Show Movie Sunset.mkv"], 5)

    def test_two_movies_with_subset_titles_are_ambiguous(self):
        media = {**MEDIA, 6: info(["Example Show Movie"], fmt="MOVIE", episodes=1),
                 5: info(["Example Show Movie Sunset"], fmt="MOVIE", episodes=1)}
        got, _ = run(["Example Show Movie Sunset.mkv", "Example Show Movie.mkv"],
                     [part(5), part(6)], media)
        self.assertEqual(got["Example Show Movie Sunset.mkv"], 5)         # longest title wins uniquely
        self.assertEqual(got["Example Show Movie.mkv"], 6)
        media[6] = info(["Example Show Movie Sunset"], fmt="MOVIE")      # identical titles: no decision
        _, out = run(["Example Show Movie Sunset.mkv"], [part(5), part(6)], media)
        self.assertEqual(out["files"]["Example Show Movie Sunset.mkv"]["reason"], "name_ambiguous")


class Extras(unittest.TestCase):
    def test_extras_inherit_their_directory_part(self):
        got, out = run(["Season 2/Extras/NCOP.mkv", "Season 2/a - 01.mkv"])
        self.assertEqual(got["Season 2/Extras/NCOP.mkv"], 2)

    def test_root_extras_stay_unassigned_and_never_block(self):
        got, out = run(["Extras/NCOP.mkv", "Season 1/a - 01.mkv"])
        self.assertIsNone(got["Extras/NCOP.mkv"])
        self.assertTrue(out["complete"])
        self.assertEqual(out["unassigned_video"], 0)


class Owner(unittest.TestCase):
    def owner(self, *rows):
        return [part(i, ordinal=n, selector=s, source="owner") for n, (i, s) in enumerate(rows, 1)]

    def test_precedence_file_dir_season_ep_rest(self):
        parts = self.owner((1, "rest"), (2, "ep:1-50"), (3, "season:4"), (4, "dir:Arc"), (5, "dir:Arc/Deep"),
                           (6, "file:Arc/Deep/special.mkv"))
        files = ["Arc/Deep/special.mkv", "Arc/Deep/x - 02.mkv", "Arc/x - 03.mkv", "S04E01.mkv", "x - 05.mkv",
                 "Other/readme-movie.mkv"]
        got, _ = run(files, parts, {})
        self.assertEqual(got, {"Arc/Deep/special.mkv": 6, "Arc/Deep/x - 02.mkv": 5, "Arc/x - 03.mkv": 4,
                               "S04E01.mkv": 3, "x - 05.mkv": 2, "Other/readme-movie.mkv": 1})

    def test_equal_specificity_conflict(self):
        parts = self.owner((1, "dir:A"), (2, "dir:A"))
        _, out = run(["A/x - 01.mkv"], parts, {})
        self.assertEqual(out["files"]["A/x - 01.mkv"]["reason"], "selector_conflict")

    def test_no_selector_matches(self):
        _, out = run(["x - 01.mkv"], self.owner((1, "dir:A"), (2, "dir:B")), {})
        self.assertEqual(out["files"]["x - 01.mkv"]["reason"], "no_selector")

    def test_owner_auto_part_uses_the_rules(self):
        parts = self.owner((1, "dir:Specials"), (2, "auto"), (3, "auto"))
        got, _ = run(["Specials/a - 01.mkv", "Season 2/a - 01.mkv"], parts,
                     {2: info(["Example Show"]), 3: info(["Example Show 2nd Season"])})
        self.assertEqual(got, {"Specials/a - 01.mkv": 1, "Season 2/a - 01.mkv": 3})


class Invariants(unittest.TestCase):
    def test_single_part_assigns_everything(self):
        files = ["a - 01.mkv", "x/b.mkv", "Extras/NCOP.mkv", "notes.txt"]
        got, out = run(files, [part(7, source="owner", selector="all")], {})
        self.assertEqual(got, {"a - 01.mkv": 7, "x/b.mkv": 7, "Extras/NCOP.mkv": 7})
        self.assertEqual({v["rule"] for v in out["files"].values()}, {"single"})

    def test_deterministic_under_permutation(self):
        files = (["Season 1/" + f for f in eps("", 1, 12)] + ["Season 3/" + f for f in eps("", 1, 24)]
                 + ["Extras/NCOP.mkv", "Movies/Example Show Movie Sunset.mkv", "weird.mkv"])
        want = assign(files, PARTS, MEDIA)
        rng = random.Random(7)
        for _ in range(5):
            shuffled = files[:]
            rng.shuffle(shuffled)
            self.assertEqual(assign(shuffled, PARTS, MEDIA), want)
            self.assertEqual(assign(shuffled, PARTS[::-1], MEDIA), want)


if __name__ == "__main__":
    unittest.main()
