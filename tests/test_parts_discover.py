"""Automatic discovery of derived parts from the relation cache. Synthetic data only."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import anilistfake
import kura.cli as cli
from anilistfake import FakeAniList
from kura import anilist_relations, partsdiscover, titleparts
from kura.anilist import AniList
from kura.models import KuraError
import test_parts_retain as pr
from kura.ratelimit import SharedLimiter
from test_ratelimit import Wall

S1, S2, S3, OTHER = pr.S1, pr.S2, pr.S3, pr.OTHER
NOW = 1_800_000_000.0
MOVIE = 999000031


class Discover(pr.Parted):
    def setUp(self):
        super().setUp()
        self.row(S1)
        self.edges((S1, S2), (S2, S3))
        self.fetch(S1, S2, S3)

    def row(self, anilist_id, status="resolved", source="list", candidates=()):
        db = self.lab.store.db
        db.execute("DELETE FROM title_identities")
        db.execute("INSERT INTO title_identities VALUES ('home',?,?,?,?,NULL,NULL,?,1.0)",
                   (pr.TITLE, anilist_id if status in ("resolved", "pinned") else None, status, source,
                    json.dumps(list(candidates))))
        db.commit()

    def edges(self, *pairs):
        db = self.lab.store.db
        db.execute("DELETE FROM anilist_relations")
        for a, b in pairs:
            db.execute("INSERT INTO anilist_relations VALUES (?,?,'SEQUEL')", (a, b))
            db.execute("INSERT INTO anilist_relations VALUES (?,?,'PREQUEL')", (b, a))
        db.commit()

    def fetch(self, *ids, at=NOW, status="ok"):
        db = self.lab.store.db
        for i in ids:
            db.execute("INSERT OR REPLACE INTO anilist_relation_fetches VALUES (?,?,?)", (i, at, status))
        db.commit()

    def media(self, anilist_id, title, fmt="TV", episodes=2):
        db = self.lab.store.db
        db.execute("INSERT OR REPLACE INTO anilist_media(anilist_id, title_romaji, format, episodes, status, fetched_at)"
                   " VALUES (?,?,?,?,'FINISHED',1.0)", (anilist_id, title, fmt, episodes))
        db.commit()

    def run_discover(self, **kw):
        kw.setdefault("now", lambda: NOW + 3600)
        return partsdiscover.discover(self.lab.store, "home", **kw)

    def stored(self):
        return self.lab.store.db.execute(
            "SELECT anilist_id, selector, source, ordinal, chain_pos, seed_id FROM title_parts ORDER BY ordinal").fetchall()


class Writes(Discover):
    def test_files_spanning_two_seasons_write_the_whole_franchise(self):
        out = self.run_discover()
        self.assertEqual(out["written"], [pr.TITLE])
        self.assertEqual(self.stored(), [(S1, "auto", "relations", 1, 1, S1), (S2, "auto", "relations", 2, 2, S1),
                                         (S3, "auto", "relations", 3, 3, S1)])
        view = titleparts.effective(self.lab.store.db, "home")[pr.TITLE]
        self.assertEqual((view["mode"], view["status"]), ("auto", "parted"))

    def test_discovered_parts_protect_a_later_season_under_a_retain_list(self):
        self.run_discover()
        doc, reasons = self.reasons(S2)
        self.assertIn("retained", reasons)
        self.assertEqual(doc["moves"], [])

    def test_rerun_changes_nothing(self):
        self.run_discover()
        db = self.lab.store.db
        db.execute("UPDATE title_parts SET updated_at=5.0")
        db.commit()
        out = self.run_discover()
        self.assertEqual((out["written"], out["unchanged"]), ([], 1))
        self.assertEqual({r[0] for r in db.execute("SELECT updated_at FROM title_parts")}, {5.0})

    def test_dry_run_writes_nothing(self):
        out = self.run_discover(dry_run=True)
        self.assertEqual((out["written"], out["discovered"]), ([pr.TITLE], 1))
        self.assertEqual(self.stored(), [])

    def test_movie_rides_along_as_a_side_entry(self):
        self.media(MOVIE, "Kura Saga the Movie", "MOVIE", 1)
        self.edges((S1, S2), (S2, S3), (S3, MOVIE))
        self.fetch(MOVIE)
        self.run_discover()
        self.assertIn((MOVIE, "auto", "relations", 4, None, S1), self.stored())


class Refusals(Discover):
    def none_written(self, reason):
        out = self.run_discover()
        self.assertEqual((out["written"], self.stored()), ([], []))
        self.assertEqual(out["skipped"].get(reason), 1, out)

    def test_partly_crawled_franchise(self):
        self.lab.store.db.execute("DELETE FROM anilist_relation_fetches WHERE anilist_id=?", (S3,))
        self.lab.store.db.commit()
        self.none_written("relations_incomplete")

    def test_not_found_member(self):
        self.fetch(S3, status="not_found")
        self.none_written("relations_incomplete")

    def test_stale_fetch_and_the_age_option(self):
        self.fetch(S2, at=NOW - 200 * 86400)
        self.none_written("relations_stale")
        self.assertEqual(self.run_discover(max_age_d=400)["written"], [pr.TITLE])

    def test_missing_media_metadata(self):
        self.lab.store.db.execute("DELETE FROM anilist_media WHERE anilist_id=?", (S3,))
        self.lab.store.db.commit()
        self.none_written("media_unknown")

    def test_branching_chain(self):
        self.media(OTHER, "Kura Saga Season 4")
        self.fetch(OTHER)
        self.edges((S1, S2), (S1, OTHER), (S2, S3))
        self.none_written("chain_ambiguous")

    def test_cycle(self):
        self.edges((S1, S2), (S2, S3), (S3, S1))
        self.none_written("chain_ambiguous")

    def test_no_relations(self):
        self.edges()
        self.none_written("no_relations")

    def test_owner_identity_is_never_overridden(self):
        self.row(S1, status="pinned", source="owner")
        self.none_written("owner_decision")

    def test_not_anime_decision(self):
        self.row(None, status="not_anime", source="owner")
        self.none_written("not_anime")

    def test_unresolved_title(self):
        self.row(None, status="ambiguous")
        self.none_written("unidentified")

    def test_owner_parts_win(self):
        self.parts((S1, "auto"), (S2, "season:2"))
        self.row(S1, status="pinned", source="owner", candidates=[S2])
        out = self.run_discover()
        self.assertEqual(out["skipped"], {"owner_parts": 1})
        self.assertEqual({r[2] for r in self.stored()}, {"owner"})

    def test_seed_that_is_a_movie(self):
        self.media(MOVIE, "Kura Saga the Movie", "MOVIE", 1)
        self.edges((S1, S2), (S2, S3), (S3, MOVIE))
        self.fetch(MOVIE)
        self.row(MOVIE)
        self.none_written("seed_not_in_chain")

    def test_unknown_format(self):
        self.lab.store.db.execute("UPDATE anilist_media SET format=NULL WHERE anilist_id=?", (S2,))
        self.lab.store.db.commit()
        self.none_written("media_unknown")

    def test_oversized_franchise(self):
        old = partsdiscover.MAX_COMPONENT
        partsdiscover.MAX_COMPONENT = 2
        self.addCleanup(setattr, partsdiscover, "MAX_COMPONENT", old)
        self.none_written("too_large")


class Stale(Discover):
    def test_parts_of_a_title_that_is_no_longer_resolved_are_removed(self):
        self.run_discover()
        self.row(None, status="unresolved")
        out = self.run_discover()
        self.assertEqual((out["removed"], self.stored()), ([pr.TITLE], []))

    def test_parts_derived_from_another_identity_are_replaced_or_removed(self):
        self.run_discover()
        self.row(S2)
        self.edges()
        out = self.run_discover()
        self.assertEqual((out["removed"], self.stored()), ([pr.TITLE], []))

    def test_doubt_keeps_parts_derived_from_the_same_identity(self):
        self.run_discover()
        self.lab.store.db.execute("DELETE FROM anilist_relation_fetches WHERE anilist_id=?", (S3,))
        self.lab.store.db.commit()
        out = self.run_discover()
        self.assertEqual(out["skipped"], {"relations_incomplete": 1})
        self.assertEqual(len(self.stored()), 3)

    def test_non_media_title_is_not_iterated(self):
        self.add("Notes", [("readme.txt", pr.P("z", 100))])
        self.lab.build()
        self.run_discover()
        out = self.run_discover()
        self.assertNotIn("Notes", json.dumps(out))
        self.assertEqual(out["titles"], 1)


class OneSeason(pr.Parted):
    FILES = [("Season 1/Kura Saga - 01.mkv", pr.P("a", 30000)), ("Season 1/Kura Saga - 02.mkv", pr.P("b", 20000))]

    def test_files_that_do_not_span_write_nothing(self):
        db = self.lab.store.db
        db.execute("INSERT INTO title_identities VALUES ('home',?,?,'resolved','list',NULL,NULL,'[]',1.0)",
                   (pr.TITLE, S1))
        for a, b in ((S1, S2), (S2, S3)):
            db.execute("INSERT INTO anilist_relations VALUES (?,?,'SEQUEL')", (a, b))
            db.execute("INSERT INTO anilist_relations VALUES (?,?,'PREQUEL')", (b, a))
        for i in (S1, S2, S3):
            db.execute("INSERT INTO anilist_relation_fetches VALUES (?,?, 'ok')", (i, NOW))
        db.commit()
        out = partsdiscover.discover(self.lab.store, "home", now=lambda: NOW)
        self.assertEqual(out["skipped"], {"files_do_not_span": 1})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM title_parts").fetchone()[0], 0)


class CommandLine(Discover):
    def cli(self, *argv):
        buffer = io.StringIO()
        with patch("kura.cli.private_root", return_value=self.lab.state), patch("sys.argv", ["kura", *argv]), \
             contextlib.redirect_stdout(buffer):
            try:
                cli.main()
            except SystemExit as exited:
                return exited.code, json.loads(buffer.getvalue())

    def test_discover_command(self):
        code, out = self.cli("identity", "discover", "--library", "home", "--dry-run")
        self.assertEqual((code, out.get("dry_run"), out.get("written")), (0, True, [pr.TITLE]))
        self.assertEqual(self.stored(), [])
        code, out = self.cli("identity", "discover", "--library", "home")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.stored()), 3)
        code, out = self.cli("identity", "discover", "--library", "home", "--max-age-d", "-3")
        self.assertNotEqual(code, 0)


class Arguments(Discover):
    def test_bad_arguments(self):
        for kw in ({"max_age_d": -1}, {"max_age_d": True}, {"max_age_d": 99999}):
            with self.subTest(kw=kw), self.assertRaises(KuraError):
                partsdiscover.discover(self.lab.store, "home", **kw)
        with self.assertRaises(KuraError):
            partsdiscover.discover(self.lab.store, "")

    def test_library_without_manifest(self):
        with self.assertRaises(KuraError):
            partsdiscover.discover(self.lab.store, "nowhere")


class CrawlThenDiscover(Discover):
    """The relation cache is filled by the fetch command (fake AniList), then discovery reads it."""

    def test_end_to_end(self):
        db = self.lab.store.db
        for table in ("anilist_relations", "anilist_relation_fetches", "anilist_media"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        fake = FakeAniList()
        fake.relations = {S1: [(S2, "SEQUEL")], S2: [(S1, "PREQUEL"), (S3, "SEQUEL")], S3: [(S2, "PREQUEL")]}
        fake.media_overrides = {
            S1: {**anilistfake.media(S1), "title": {"romaji": "Kura Saga", "english": None, "native": None},
                 "episodes": 2},
            S2: {**anilistfake.media(S2), "title": {"romaji": "Kura Saga 2nd Season", "english": None,
                                                                   "native": None}, "episodes": 2},
            S3: {**anilistfake.media(S3), "title": {"romaji": "Kura Saga Season 3", "english": None,
                                                                   "native": None}, "episodes": 2}}
        wall = Wall(NOW)
        limiter = SharedLimiter(self.lab.store.root, "anilist", interval=2.0, wall=wall, sleep=wall.sleep)
        ani = AniList(max_retries=0, limiter=limiter, gql_transport=fake, wall=wall, sleep=wall.sleep)
        out = anilist_relations.relations_sync(self.lab.store, ani, library="home")
        self.assertEqual((out["seeds"], out["fetched"], out["complete"]), (1, 3, True))
        found = partsdiscover.discover(self.lab.store, "home", now=wall)
        self.assertEqual(found["written"], [pr.TITLE])
        self.assertEqual([r[0] for r in self.stored()], [S1, S2, S3])


if __name__ == "__main__":
    unittest.main()
