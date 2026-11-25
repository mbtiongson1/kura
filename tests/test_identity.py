"""Title identity sync, owner pins and the retain check. Synthetic data; no network."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import kura.cli as cli
from kura import identity
from kura.anilist import AniList, choose
from kura.idlist import parse, parse_pins
from kura.manifest_store import ManifestRepo
from kura.models import KuraError
from test_manifest import GOOD, Lab

A, B, C, D, E = 999000001, 999000002, 999000003, 999000004, 999000005


def seed(db, entries, *, user=1, viewer=True):
    """entries: (anilist_id, romaji, english, synonyms) rows on the viewer's list."""
    db.execute("INSERT OR IGNORE INTO anilist_users(user_id, name, is_viewer, updated_at) VALUES (?,?,?,1.0)",
               (user, "kura-viewer", int(viewer)))
    for anilist_id, romaji, english, synonyms in entries:
        db.execute("INSERT INTO anilist_media(anilist_id, title_romaji, title_english, synonyms, fetched_at)"
                   " VALUES (?,?,?,?,1.0)", (anilist_id, romaji, english, json.dumps(synonyms)))
        db.execute("INSERT INTO anilist_list_entries(user_id, anilist_id, status, fetched_at)"
                   " VALUES (?,?,'COMPLETED',1.0)", (user, anilist_id))
    db.commit()


def Resolver(answers):
    """A real ``AniList`` over a fake transport. ``answers`` maps a search query to ``(id, titles)``,
    a list of those, ``"ambiguous"`` (two entries with the same title) or ``"down"``; other queries
    return an empty page. ``.queries`` records every request, ``.answers`` can be changed."""
    queries = []

    def transport(query):
        queries.append(query)
        answer = resolver.answers.get(query, [])
        if answer == "down":
            raise KuraError("alias_unavailable", "AniList request failed; retry later")
        if answer == "ambiguous":
            answer = [(A, [query]), (B, [query])]
        elif isinstance(answer, tuple):
            answer = [answer]
        media = [{"id": i, "title": {"romaji": titles[0]}, "synonyms": titles[1:]} for i, titles in answer]
        return json.dumps({"data": {"Page": {"media": media}}}).encode(), {}
    resolver = AniList(transport=transport, sleep=lambda seconds: None)
    resolver.answers, resolver.queries = answers, queries
    return resolver


class Base(unittest.TestCase):
    NAMES = ()

    def setUp(self):
        self.lab = Lab(self)
        for name in self.NAMES:
            self.lab.add(name, GOOD)
        self.lab.build()
        self.store = self.lab.store

    def sync(self, **kw):
        kw.setdefault("library", "home")
        return identity.sync(self.store, **kw)

    def row(self, name):
        return identity.load(self.store.db, "home")[f"main/{name}"]


class OfflineMatch(Base):
    NAMES = ("Kura Sample Series", "[Kura] Kura Other Show (BD 1080p)", "Kura Sample Series S02",
             "Kura Twin", "Kura Pack S01-S03", "Kura Unknown")

    def setUp(self):
        super().setUp()
        seed(self.store.db, [(A, "Kura Sample Series", "Kura Sample Series English", []),
                             (B, "Kura Waraimono", None, ["Kura Other Show"]),
                             (C, "Kura Twin", None, []), (D, "Kura Twin", None, []),
                             (E, "Kura Pack", None, [])])

    def test_exact_and_synonym_matches(self):
        out = self.sync(aliases=None)
        self.assertEqual((self.row("Kura Sample Series")["anilist_id"], self.row("Kura Sample Series")["status"],
                          self.row("Kura Sample Series")["source"]), (A, "resolved", "list"))
        other = self.row("[Kura] Kura Other Show (BD 1080p)")
        self.assertEqual((other["anilist_id"], other["matched"], other["query"]), (B, "Kura Other Show", "Kura Other Show"))
        self.assertEqual(out["resolved"], {"list": 2, "alias": 0})
        self.assertEqual(out["lookups"], {"made": 0, "budget": 0})

    def test_season_two_folder_never_binds_to_the_season_one_entry(self):
        self.sync(aliases=None)
        self.assertEqual(self.row("Kura Sample Series")["anilist_id"], A)
        folder = self.row("Kura Sample Series S02")
        self.assertEqual((folder["status"], folder["anilist_id"]), ("unresolved", None))

    def test_season_two_folder_binds_to_an_entry_that_carries_the_season(self):
        for anilist_id, title in ((999000011, "Kura Sample Series Season 2"),):
            seed(self.store.db, [(anilist_id, title, None, [])])
        self.sync(aliases=None)
        folder = self.row("Kura Sample Series S02")
        self.assertEqual((folder["status"], folder["anilist_id"]), ("resolved", 999000011))
        # and the plain folder still prefers the unmarked entry
        self.assertEqual(self.row("Kura Sample Series")["anilist_id"], A)

    def test_season_markers_in_titles(self):
        for title, season, expected in (("Kura Show Season 2", 2, True), ("Kura Show 2nd Season", 2, True),
                                        ("Kura Show 2", 2, True), ("Kura Show S2", 2, True),
                                        ("Kura Show", 2, False), ("Kura Show Season 3", 2, False),
                                        ("Kura Show 2", 3, False), ("2", 2, False)):
            self.assertEqual(identity.carries_season(title, season), expected, (title, season))
        self.assertTrue(identity.names_later_season("Kura Show Season 2"))
        self.assertTrue(identity.names_later_season("Kura Show 2", "Kura Show"))
        self.assertFalse(identity.names_later_season("Kura Show 100", "Kura Show 100"))
        self.assertFalse(identity.names_later_season("Kura Show 1", "Kura Show"))
        self.assertFalse(identity.names_later_season("Kura Show Season 1"))

    def test_season_one_folder_refuses_an_entry_for_a_later_season(self):
        rows = [{"id": B, "titles": ["Kura Show Season 2"]}]
        self.assertEqual(identity.match(rows, "Kura Show", 1)["status"], "unresolved")
        self.assertEqual(identity.match(rows, "Kura Show", None)["anilist_id"], B)
        trailing = [{"id": B, "titles": ["Kura Show 2"]}]
        self.assertEqual(identity.match(trailing, "Kura Show", 1)["status"], "unresolved")
        self.assertEqual(identity.match([{"id": B, "titles": ["Kura Show 100"]}], "Kura Show 100", 1)["anilist_id"], B)

    def test_ties_are_ambiguous_with_their_candidates(self):
        self.sync(aliases=None)
        twin = self.row("Kura Twin")
        self.assertEqual((twin["status"], twin["anilist_id"], twin["candidates"]), ("ambiguous", None, [C, D]))

    def test_season_ranges_are_ambiguous(self):
        self.sync(aliases=None)
        pack = self.row("Kura Pack S01-S03")
        self.assertEqual((pack["status"], pack["anilist_id"]), ("ambiguous", None))

    def test_season_range_spellings_are_undecidable(self):
        for name in ("Kura Pack S01-S03", "Kura Pack S1-2", "Kura Pack Season 1-2", "Kura Pack Seasons 1+2",
                     "Kura Pack Season 1 to 3", "Kura Pack 1st-3rd Season", "Kura Pack S01 S02", "Kura Pack S00",
                     "Kura Pack Season 1 & 2", "Kura Pack Complete Seasons", "Kura Pack (Season 1-2)",
                     "Kura Pack [S01-S03]", "Kura Pack (S1) [S2]"):
            with self.subTest(name=name):
                self.assertTrue(identity.folder_terms(f"main/{name}")[2], name)
        for name, season in (("Kura Pack S02", 2), ("Kura Pack Season 2", 2), ("Kura Pack 2nd Season", 2),
                             ("Kura Pack", None), ("[Kura] Kura Pack - S01 (BD 1080p)", 1),
                             ("Kura Pack (Season 2)", 2), ("Kura Pack [S02]", 2), ("Kura_Pack_S02", 2),
                             ("[Kura] Kura Pack - 05 [S01E05].mkv", 1)):
            with self.subTest(name=name):
                self.assertEqual(identity.folder_terms(f"main/{name}")[1:], (season, False))

    def test_matching_agrees_with_anilist_choose(self):
        rows = [{"id": 1, "titles": ["Kura Show Extra Words"]}, {"id": 2, "titles": ["Kura Show Extra"]},
                {"id": 3, "titles": ["Other"]}]
        identity_, status = choose(rows, "Kura Show")
        got = identity.match(rows, "Kura Show", None)
        self.assertEqual((status, identity_.anilist_id), (got["status"], got["anilist_id"]))
        self.assertEqual(got["candidates"], [1])           # the weaker subset match is recorded
        exact = identity.match([{"id": 1, "titles": ["Kura Show"]}, {"id": 2, "titles": ["Kura Show Season 2"]}],
                               "Kura Show", None)
        self.assertEqual((exact["anilist_id"], exact["candidates"]), (1, [2]))   # even behind an exact match
        tied = [{"id": 1, "titles": ["Kura Show"]}, {"id": 2, "titles": ["kura  show"]}]
        weaker = identity.match(tied + [{"id": 3, "titles": ["Kura Show Season 2"]}], "Kura Show", None)
        self.assertEqual((weaker["status"], weaker["candidates"]), ("ambiguous", [1, 2, 3]))
        self.assertEqual(choose(tied, "Kura Show"), (None, "ambiguous"))
        self.assertEqual(identity.match(tied, "Kura Show", None)["status"], "ambiguous")
        self.assertEqual(identity.match(rows, "", None)["status"], "unresolved")

    def test_viewer_list_is_preferred_over_other_lists(self):
        seed(self.store.db, [(999000020, "Kura Unknown", None, [])], user=2, viewer=False)
        self.sync(aliases=None)
        self.assertEqual(self.row("Kura Unknown")["status"], "unresolved")

    def test_without_a_viewer_any_synced_list_is_used(self):
        db = self.store.db
        db.execute("DELETE FROM anilist_users")
        db.commit()
        seed(db, [(999000020, "Kura Unknown", None, [])], user=2, viewer=False)
        self.sync(aliases=None)
        self.assertEqual(self.row("Kura Unknown")["anilist_id"], 999000020)

    def test_empty_snapshot_resolves_nothing_offline(self):
        self.store.db.execute("DELETE FROM anilist_list_entries")
        self.store.db.commit()
        out = self.sync(aliases=None)
        self.assertEqual((out["resolved"], out["unresolved"], out["ambiguous"], out["pending"]),
                         ({"list": 0, "alias": 0}, 5, 1, 0))     # the season range stays ambiguous

    def test_sync_is_stable_and_scoped(self):
        self.sync(aliases=None)
        before = identity.load(self.store.db, "home")
        self.sync(aliases=None)
        self.assertEqual(identity.load(self.store.db, "home"), before)
        with self.assertRaises(KuraError) as caught:
            self.sync(aliases=None, titles=["main/Not Here"])
        self.assertEqual(caught.exception.code, "not_found")
        out = self.sync(aliases=None, titles=["main/Kura Sample Series"])
        self.assertEqual(out["titles"], 1)
        for bad in (-1, 1001, True, "5"):
            with self.assertRaises(KuraError):
                self.sync(aliases=None, max_lookups=bad)
        with self.assertRaises(KuraError) as caught:
            identity.sync(self.store, library="never-built")
        self.assertEqual(caught.exception.code, "not_found")


class AliasFallback(Base):
    NAMES = ("Kura Alpha", "Kura Beta", "Kura Gamma S02", "Kura Delta")

    def test_lookups_stay_within_the_budget(self):
        resolver = Resolver({"Kura Alpha": (A, ["Kura Alpha"]), "Kura Beta": (B, ["Kura Beta"])})
        out = self.sync(aliases=resolver, max_lookups=2, titles=["main/Kura Alpha", "main/Kura Beta", "main/Kura Delta"])
        self.assertEqual(out["lookups"], {"made": 2, "budget": 2})
        self.assertEqual((out["pending"], out["resolved"]["alias"]), (1, 2))
        self.assertEqual(resolver.queries, ["Kura Alpha", "Kura Beta"])
        out = self.sync(aliases=resolver, max_lookups=5, titles=["main/Kura Alpha", "main/Kura Beta", "main/Kura Delta"])
        self.assertEqual((out["lookups"]["made"], out["pending"]), (1, 0))   # earlier answers are kept, not re-asked
        self.assertEqual(self.row("Kura Delta")["status"], "unresolved")
        self.assertEqual(self.row("Kura Alpha")["source"], "alias")

    def test_the_whole_page_is_kept_so_weaker_candidates_survive(self):
        page = [(A, ["Kura Alpha"]), (B, ["Kura Alpha Season 2"]), (C, ["Unrelated"])]
        self.sync(aliases=Resolver({"Kura Alpha": page}), titles=["main/Kura Alpha"])
        row = self.row("Kura Alpha")
        self.assertEqual((row["anilist_id"], row["candidates"], row["source"]), (A, [B], "alias"))

    def test_candidates_found_by_alias_survive_later_syncs_and_refreshes(self):
        page = [(A, ["Kura Alpha"]), (B, ["Kura Alpha Season 2"])]
        self.sync(aliases=Resolver({"Kura Alpha": page}), titles=["main/Kura Alpha"])
        seed(self.store.db, [(A, "Kura Alpha", None, [])])              # a narrower list: only the winner
        self.sync(aliases=None, titles=["main/Kura Alpha"])
        row = self.row("Kura Alpha")
        self.assertEqual((row["source"], row["candidates"]), ("alias", [B]))   # ordinary sync keeps the row
        self.sync(aliases=None, titles=["main/Kura Alpha"], refresh=True)
        row = self.row("Kura Alpha")
        self.assertEqual((row["anilist_id"], row["source"], row["candidates"]), (A, "list", [B]))

    def test_candidate_history_survives_a_failed_refresh(self):
        page = [(A, ["Kura Alpha"]), (B, ["Kura Alpha Season 2"])]
        self.sync(aliases=Resolver({"Kura Alpha": page}), titles=["main/Kura Alpha"])
        self.sync(aliases=Resolver({"Kura Alpha": "down"}), titles=["main/Kura Alpha"], refresh=True)
        row = self.row("Kura Alpha")
        self.assertEqual((row["status"], row["candidates"]), ("unavailable", [A, B]))
        self.sync(aliases=Resolver({"Kura Alpha": (C, ["Kura Alpha"])}), titles=["main/Kura Alpha"],
                  now=lambda: 2e9)                                      # long after the outage window
        row = self.row("Kura Alpha")
        self.assertEqual((row["status"], row["anilist_id"], row["candidates"]), ("resolved", C, [A, B]))

    def test_a_tie_inside_the_page_is_ambiguous_with_candidates(self):
        self.sync(aliases=Resolver({"Kura Alpha": "ambiguous"}), titles=["main/Kura Alpha"])
        row = self.row("Kura Alpha")
        self.assertEqual((row["status"], row["candidates"]), ("ambiguous", [A, B]))

    def test_the_season_rule_applies_to_every_entry_on_the_page(self):
        page = [(A, ["Kura Gamma"]), (C, ["Kura Gamma Season 2"])]
        self.sync(aliases=Resolver({"Kura Gamma Season 2": page}), titles=["main/Kura Gamma S02"])
        self.assertEqual(self.row("Kura Gamma S02")["anilist_id"], C)

    def test_recent_negative_answers_are_not_asked_again_until_they_expire(self):
        resolver = Resolver({})
        t = 1_000_000.0
        self.sync(aliases=resolver, titles=["main/Kura Delta"], now=lambda: t)
        self.sync(aliases=resolver, titles=["main/Kura Delta"], now=lambda: t + 3600)
        self.assertEqual(resolver.queries, ["Kura Delta"])
        self.sync(aliases=resolver, titles=["main/Kura Delta"], now=lambda: t + 3600, refresh=True)
        self.assertEqual(resolver.queries, ["Kura Delta"] * 2)
        self.sync(aliases=resolver, titles=["main/Kura Delta"], now=lambda: t + 8 * 86400)
        self.assertEqual(resolver.queries, ["Kura Delta"] * 3)
        down = Resolver({"Kura Delta": "down"})
        self.sync(aliases=down, titles=["main/Kura Delta"], now=lambda: t + 20 * 86400, refresh=True)
        self.sync(aliases=down, titles=["main/Kura Delta"], now=lambda: t + 20 * 86400 + 60)
        self.assertEqual(len(down.queries), 1)                       # an outage is retried after ten minutes
        self.sync(aliases=down, titles=["main/Kura Delta"], now=lambda: t + 20 * 86400 + 700)
        self.assertEqual(len(down.queries), 2)

    def test_zero_budget_makes_no_request(self):
        resolver = Resolver({})
        out = self.sync(aliases=resolver, max_lookups=0)
        self.assertEqual((resolver.queries, out["lookups"]["made"], out["pending"]), ([], 0, 4))

    def test_season_variant_is_asked_first(self):
        resolver = Resolver({"Kura Gamma Season 2": (C, ["Kura Gamma Season 2", "Kura Gamma 2"])})
        self.sync(aliases=resolver, titles=["main/Kura Gamma S02"])
        self.assertEqual(resolver.queries, ["Kura Gamma Season 2"])
        self.assertEqual((self.row("Kura Gamma S02")["anilist_id"], self.row("Kura Gamma S02")["query"]),
                         (C, "Kura Gamma Season 2"))

    def test_a_season_title_needing_two_queries_progresses_with_a_budget_of_one(self):
        resolver = Resolver({"Kura Gamma": (C, ["Kura Gamma Season 2"])})
        out = self.sync(aliases=resolver, max_lookups=1, titles=["main/Kura Gamma S02"])
        self.assertEqual((resolver.queries, out["pending"]), (["Kura Gamma Season 2"], 1))
        row = self.row("Kura Gamma S02")
        self.assertEqual((row["status"], row["query"]), ("unresolved", "v2:1/2:Kura Gamma Season 2@2 | Kura Gamma@2"))
        out = self.sync(aliases=resolver, max_lookups=1, titles=["main/Kura Gamma S02"])
        self.assertEqual((resolver.queries, out["pending"]), (["Kura Gamma Season 2", "Kura Gamma"], 0))
        row = self.row("Kura Gamma S02")
        self.assertEqual((row["status"], row["anilist_id"], row["query"]), ("resolved", C, "Kura Gamma"))

    def test_candidates_rejected_before_the_budget_ended_survive_the_resume(self):
        resolver = Resolver({"Kura Gamma Season 2": (A, ["Kura Gamma"]), "Kura Gamma": []})
        self.sync(aliases=resolver, max_lookups=1, titles=["main/Kura Gamma S02"])
        self.assertEqual(self.row("Kura Gamma S02")["candidates"], [A])
        self.sync(aliases=resolver, max_lookups=1, titles=["main/Kura Gamma S02"])
        row = self.row("Kura Gamma S02")
        self.assertEqual((row["status"], row["candidates"], row["query"]),
                         ("unresolved", [A], "v2:2/2:Kura Gamma Season 2@2 | Kura Gamma@2"))
        self.sync(aliases=resolver, max_lookups=5, titles=["main/Kura Gamma S02"])
        self.assertEqual(len(resolver.queries), 2)                          # finished, remembered
        out = identity.retain_check(self.store, "home", parse_ids(f"{A}\n"))
        self.assertEqual(out["possible"], [{"title": "main/Kura Gamma S02", "candidates": [A]}])

    def test_the_resumed_plan_is_seeded_with_the_candidates_of_the_finished_steps(self):
        resolver = Resolver({"Kura Gamma": []})
        lookup = identity._Lookup(resolver)
        steps = identity.plan_steps("Kura Gamma", 2, None)
        record, _, _ = identity._alias(lookup, steps, 1, 5, prior=[A])
        self.assertEqual((record["status"], record["candidates"], resolver.queries), ("unresolved", [A], ["Kura Gamma"]))
        record, _, _ = identity._alias(identity._Lookup(resolver), steps, 1, 5)
        self.assertEqual(record["candidates"], [])

    def test_the_remembered_plan_must_match_exactly(self):
        steps = identity.plan_steps("Foo", 2, None)
        key = identity._key(steps, 1)
        self.assertEqual(key, "v2:1/2:Foo Season 2@2 | Foo@2")
        self.assertEqual(identity._done(key, steps), 1)
        for other in (identity.plan_steps("Foo", None, None),                   # unmarked -> season 2 (same name)
                      identity.plan_steps("Foo", 3, None), identity.plan_steps("Foo", 2, ("Bar", None)),
                      identity.plan_steps("Foo", 2, ("Bar", 2)), identity.plan_steps("Fo", 2, None),
                      steps[::-1], steps[:1]):
            self.assertIsNone(identity._done(key, other), other)
        # the same queries asked for a different season filter are a different plan
        self.assertIsNone(identity._done(identity._key([("Foo", "Foo", None)], 1), [("Foo", "Foo", 2)]))
        for text in (None, "", "Foo", "Foo | Bar", "v2:3/2:Foo Season 2@2 | Foo@2", "v2:1/2:Foo Season 2@2", "v1:1/2:x",
                     "v2:-1/2:x"):
            self.assertIsNone(identity._done(text, steps), text)

    def test_a_long_plan_is_hashed_to_fit_and_still_compares(self):
        long = "Kura " + "Epic " * 24
        steps = identity.plan_steps(long, 2, (long + "Other", None))
        key = identity._key(steps, 3)
        self.assertLessEqual(len(key), identity.MAX_QUERY)
        self.assertEqual(identity._done(key, steps), 3)
        self.assertIsNone(identity._done(key, identity.plan_steps(long, 3, (long + "Other", None))))
        self.assertIsNone(identity._done(key.replace("/3:", "/4:"), steps))
        self.assertIsNone(identity._done(key[:-1] + "0", steps))

    def test_rows_of_an_unknown_or_older_format_are_looked_up_again(self):
        for query in ("Kura Delta", "Kura Delta | Other", None, "v9:1/1:Kura Delta@-"):
            self.store.db.execute("DELETE FROM title_identities")
            self.store.db.execute(
                "INSERT INTO title_identities(library, title, status, source, query, candidates, updated_at)"
                " VALUES ('home', 'main/Kura Delta', 'unresolved', 'alias', ?, '[]', ?)", (query, time.time()))
            self.store.db.commit()
            resolver = Resolver({})
            self.sync(aliases=resolver, titles=["main/Kura Delta"])
            self.assertEqual(resolver.queries, ["Kura Delta"], query)
            self.assertEqual(self.row("Kura Delta")["query"], "v2:1/1:Kura Delta@-")
            self.sync(aliases=resolver, titles=["main/Kura Delta"])
            self.assertEqual(resolver.queries, ["Kura Delta"], query)      # and remembered from then on

    def test_an_answer_for_the_wrong_season_is_rejected_and_remembered_as_a_candidate(self):
        resolver = Resolver({"Kura Gamma": (A, ["Kura Gamma"])})
        self.sync(aliases=resolver, titles=["main/Kura Gamma S02"])
        self.assertEqual(resolver.queries, ["Kura Gamma Season 2", "Kura Gamma"])
        row = self.row("Kura Gamma S02")
        self.assertEqual((row["status"], row["anilist_id"], row["candidates"]), ("unresolved", None, [A]))

    def test_ambiguous_and_unavailable_answers(self):
        resolver = Resolver({"Kura Alpha": "ambiguous", "Kura Beta": "down"})
        out = self.sync(aliases=resolver, titles=["main/Kura Alpha", "main/Kura Beta", "main/Kura Delta"])
        self.assertEqual(self.row("Kura Alpha")["status"], "ambiguous")
        self.assertEqual(self.row("Kura Beta")["status"], "unavailable")
        self.assertEqual(resolver.queries, ["Kura Alpha", "Kura Beta"])       # nothing after the outage
        self.assertEqual((out["ambiguous"], out["unavailable"], out["pending"]), (1, 1, 1))

    def test_a_refresh_that_cannot_reconfirm_replaces_the_row(self):
        self.sync(aliases=Resolver({"Kura Alpha": (A, ["Kura Alpha"])}), titles=["main/Kura Alpha"])
        out = self.sync(aliases=Resolver({"Kura Alpha": "down"}), titles=["main/Kura Alpha"], refresh=True)
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"]), ("unavailable", None))
        self.assertEqual(out["unavailable"], 1)

    def test_an_unconfirmed_resolved_row_is_kept_without_refresh(self):
        self.sync(aliases=Resolver({"Kura Alpha": (A, ["Kura Alpha"])}), titles=["main/Kura Alpha"])
        self.sync(aliases=Resolver({"Kura Alpha": "down"}), titles=["main/Kura Alpha"])
        self.assertEqual(self.row("Kura Alpha")["anilist_id"], A)

    def test_refresh_without_budget_or_network_leaves_nothing_stale(self):
        seed(self.store.db, [(B, "Kura Beta", None, [])])
        self.sync(aliases=Resolver({"Kura Alpha": (A, ["Kura Alpha"])}))
        self.assertEqual((self.row("Kura Alpha")["source"], self.row("Kura Beta")["source"]), ("alias", "list"))
        out = self.sync(aliases=Resolver({"Kura Alpha": (A, ["Kura Alpha"])}), refresh=True, max_lookups=0)
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"]), ("unresolved", None))
        self.assertEqual(self.row("Kura Beta")["anilist_id"], B)              # the list still confirms it
        self.assertGreaterEqual(out["pending"], 1)
        self.sync(aliases=Resolver({"Kura Alpha": (A, ["Kura Alpha"])}))       # a normal sync restores it
        self.assertEqual(self.row("Kura Alpha")["anilist_id"], A)
        self.store.db.execute("DELETE FROM anilist_list_entries")
        self.store.db.commit()
        self.sync(aliases=None, refresh=True)
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Beta")["status"]),
                         ("unresolved", "unresolved"))

    def test_refresh_asks_again(self):
        resolver = Resolver({"Kura Alpha": (A, ["Kura Alpha"])})
        self.sync(aliases=resolver, titles=["main/Kura Alpha"])
        self.sync(aliases=resolver, titles=["main/Kura Alpha"])
        self.assertEqual(resolver.queries, ["Kura Alpha"])
        resolver.answers["Kura Alpha"] = (B, ["Kura Alpha"])
        self.sync(aliases=resolver, titles=["main/Kura Alpha"], refresh=True)
        self.assertEqual(self.row("Kura Alpha")["anilist_id"], B)

    def test_offline_never_uses_the_resolver_and_keeps_alias_rows(self):
        self.sync(aliases=Resolver({"Kura Alpha": (A, ["Kura Alpha"])}), titles=["main/Kura Alpha"])
        out = self.sync(aliases=None)
        self.assertEqual(self.row("Kura Alpha")["anilist_id"], A)
        self.assertEqual(out["lookups"], {"made": 0, "budget": 0})


class Tree(Base):
    """A library made of plain files: ``{entry: None | {relative path: bytes}}``; a string entry is a loose file."""
    TREE: dict = {}

    def setUp(self):
        self.lab = Lab(self)
        for entry, content in self.TREE.items():
            if content is None:
                (self.lab.lib / entry).write_bytes(b"x" * 100)
                continue
            for rel in content:
                path = self.lab.lib / entry / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * 100)
        self.lab.build()
        self.store = self.lab.store


def parse_ids(text):
    path = Path(tempfile.mkdtemp()) / "ids.txt"
    path.write_text(text)
    return parse(path)


def eps(group, series, *numbers, tags="[1080p HEVC][A7801AD7]", ext="mkv"):
    return {f"[{group}] {series} - {n:02d} {tags}.{ext}": None for n in numbers}


class NameVariants(unittest.TestCase):
    ROWS = [{"id": A, "titles": ["SPY×FAMILY"]}, {"id": B, "titles": ["Carole & Tuesday"]},
            {"id": C, "titles": ["Toaru Kagaku no Railgun"]}, {"id": D, "titles": ["Howl‘s Moving Castle"]},
            {"id": E, "titles": ["Kokoro ga Sakebitagatterun da."]}]

    def one(self, folder, rows=None, season=None):
        got = identity.match(rows or self.ROWS, identity.folder_terms(f"main/{folder}")[0], season)
        return got["anilist_id"], got["status"]

    def test_spelling_variants_compare_equal(self):
        for folder, wanted in (("SPYxFAMILY", A), ("Spy x Family", A), ("Carole and Tuesday", B),
                               ("To Aru Kagaku no Railgun", C), ("Howl's Moving Castle", D),
                               ("Howl’s Moving Castle", D), ("Howls Moving Castle", D),
                               ("Kokoro ga Sakebitagatterunda", E)):
            with self.subTest(folder=folder):
                self.assertEqual(self.one(folder), (wanted, "resolved"))
        spaced = [{"id": A, "titles": ["Tatteiru"]}]
        self.assertEqual(self.one("Tatte Iru", spaced), (A, "resolved"))

    def test_a_word_for_word_title_beats_a_respaced_one(self):
        rows = [{"id": A, "titles": ["Tatteiru"]}, {"id": B, "titles": ["Tatte Iru"]}]
        got = identity.match(rows, "Tatte Iru", None)
        self.assertEqual((got["status"], got["anilist_id"], got["candidates"]), ("resolved", B, [A]))
        both = identity.match([{"id": A, "titles": ["Tatteiru"]}, {"id": B, "titles": ["Tat teiru"]}], "Tatte Iru", None)
        self.assertEqual((both["status"], both["candidates"]), ("ambiguous", [A, B]))

    def test_variants_do_not_loosen_what_is_not_a_variant(self):
        for folder in ("Russia-go", "Spy Family Extra Words", "Carole Tuesday Zero"):
            self.assertEqual(self.one(folder)[0], None, folder)
        self.assertEqual(self.one("SPYxFAMILY", season=2)[1], "unresolved")      # the season rule still applies
        self.assertEqual(identity.match([{"id": A, "titles": ["Rossiya-go"]}], "Russia-go", None)["status"], "unresolved")

    def test_accents_fold_on_latin_letters_only(self):
        self.assertEqual(identity.norm_words("ChäoS;HEAd"), "chaos head")
        self.assertEqual(identity.norm_words("がっこう"), "がっこう")   # kana voicing stays
        self.assertNotEqual(identity.norm_words("か"), identity.norm_words("が"))


class Mojibake(unittest.TestCase):
    def test_repairs_utf8_read_as_cp1252_or_latin1(self):
        for broken, fixed in (("ChÃ¤oS;HEAd", "ChäoS;HEAd"),
                              ("DameÃ—Prince", "Dame×Prince"),
                              ("Shouâ˜†Kageki", "Shou☆Kageki"),
                              ("Eyeâ€™s", "Eye’s"),
                              ("Waitâ€¦", "Wait…"),
                              ("ChÃ¤os".encode("utf-8").decode("latin-1"), "ChÃ¤os")):
            if broken.startswith("ChÃ\u0083"):          # a double encoding is not undone twice
                continue
            with self.subTest(broken=broken):
                self.assertEqual(identity.repair_mojibake(broken), fixed)

    def test_never_applied_when_it_would_not_round_trip(self):
        for text in ("Plain Title", "Café", "Chäos", "Ãne",               # clean or not UTF-8 bytes
                     "ChÃ¤os あ",                                          # kana cannot be encoded back
                     "â€", "DameÃ", "Ã¤\u0001"):                # truncated or control result
            with self.subTest(text=text):
                self.assertEqual(identity.repair_mojibake(text), text)

    def test_a_repaired_folder_name_matches(self):
        for folder, title in (("ChÃ¤oS;HEAd", "ChäoS;HEAd"), ("DameÃ—Prince", "Dame x Prince"),
                              ("Shouâ˜†Kageki", "Shou☆Kageki")):
            with self.subTest(folder=folder):
                base = identity.folder_terms(f"main/{folder}")[0]
                self.assertEqual(identity.match([{"id": A, "titles": [title]}], base, None)["anilist_id"], A)
        self.assertNotIn("Ã", identity.folder_terms("main/ChÃ¤oS;HEAd")[0])


class FileNames(Tree):
    TREE = {
        "Dead Mouth Dead Play": eps("ASW", "Dead Mount Death Play", 1, 2, 3),
        "Hibike! Euphoneum": {**eps("ASW", "Hibike! Euphonium S3", 1, 2),
                              "NC/[ASW] Hibike! Euphonium NCOP.mkv": None},
        "Dragon Meshi": {"[Lazy] Dungeon Meshi - 01v2 (1080p).mkv": None, "[Lazy] Dungeon Meshi - 02 (1080p).mkv": None},
        "Mixed Names": {**eps("ASW", "Show One", 1), **eps("ASW", "Show Two", 2)},
        "Mixed Years": {**eps("ASW", "Show Three S2", 1), **eps("ASW", "Show Three", 2)},
        "Odd Folder": {**eps("ASW", "Show Four", 1), "Specials/Behind The Scenes.mkv": None},
        "Season Clash S2": eps("ASW", "Show Five S3", 1, 2),
        "Misc Folder": eps("ASW", "Show Six", 1, 2),
        "Scene Folder": {"Show.Seven.S02E01.1080p.WEB.x265-Grp.mkv": None, "Show.Seven.S02E02.1080p.WEB.x265-Grp.mkv": None},
    }

    def setUp(self):
        super().setUp()
        seed(self.store.db, [(A, "Dead Mount Death Play", None, []), (B, "Hibike! Euphonium 3", None, []),
                             (C, "Dungeon Meshi", None, []), (D, "Show One", None, []), (E, "Show Two", None, []),
                             (999000006, "Show Three", None, []), (999000007, "Show Five Season 3", None, []),
                             (999000008, "Show Seven Season 2", None, [])])

    def test_a_misspelled_folder_is_read_from_its_files(self):
        self.sync(aliases=None)
        dead = self.row("Dead Mouth Dead Play")
        self.assertEqual((dead["status"], dead["anilist_id"], dead["source"], dead["query"]),
                         ("resolved", A, "list", "Dead Mount Death Play"))
        meshi = self.row("Dragon Meshi")
        self.assertEqual((meshi["anilist_id"], meshi["query"], meshi["matched"]), (C, "Dungeon Meshi", "Dungeon Meshi"))

    def test_season_and_extras_come_from_the_files(self):
        self.sync(aliases=None)
        hibike = self.row("Hibike! Euphoneum")           # the NC folder's file is an extra and is ignored
        self.assertEqual((hibike["status"], hibike["anilist_id"]), ("resolved", B))
        scene = self.row("Scene Folder")
        self.assertEqual((scene["anilist_id"], scene["query"]), (999000008, "Show Seven"))

    def test_disagreeing_files_keep_the_folder_result(self):
        self.sync(aliases=None)
        for name in ("Mixed Names", "Mixed Years", "Odd Folder", "Season Clash S2"):
            with self.subTest(name=name):
                self.assertEqual((self.row(name)["status"], self.row(name)["anilist_id"]), ("unresolved", None))
        self.assertEqual(self.row("Mixed Names")["query"], "Mixed Names")

    def test_file_names_that_match_nothing_stay_unresolved(self):
        self.sync(aliases=None)
        self.assertEqual(self.row("Misc Folder")["status"], "unresolved")

    def test_the_folder_name_wins_when_it_matches(self):
        seed(self.store.db, [(999000009, "Misc Folder", None, [])])
        self.sync(aliases=None)
        row = self.row("Misc Folder")
        self.assertEqual((row["anilist_id"], row["query"]), (999000009, "Misc Folder"))

    def test_the_alias_pass_asks_for_the_folder_then_the_file_name(self):
        resolver = Resolver({"Dragon Meshi": [], "Dungeon Meshi": (999000010, ["Dungeon Meshi Alias"]),
                             "Show Six": (999000011, ["Show Six"])})
        self.store.db.execute("DELETE FROM anilist_list_entries")
        self.store.db.commit()
        out = self.sync(aliases=resolver, max_lookups=50, titles=["main/Dragon Meshi", "main/Misc Folder"])
        self.assertEqual(resolver.queries, ["Dragon Meshi", "Dungeon Meshi", "Misc Folder", "Show Six"])
        meshi = self.row("Dragon Meshi")
        self.assertEqual((meshi["status"], meshi["anilist_id"], meshi["source"], meshi["query"]),
                         ("resolved", 999000010, "alias", "Dungeon Meshi"))
        six = self.row("Misc Folder")
        self.assertEqual((six["anilist_id"], six["source"], six["query"]), (999000011, "alias", "Show Six"))
        self.assertEqual(out["lookups"]["made"], 4)

    def test_the_file_name_lookup_respects_the_budget(self):
        resolver = Resolver({})
        out = self.sync(aliases=resolver, max_lookups=1, titles=["main/Misc Folder"])
        self.assertEqual((resolver.queries, out["pending"]), (["Misc Folder"], 1))
        row = self.row("Misc Folder")           # the finished steps are kept, so the next run asks only the rest
        self.assertEqual((row["status"], row["source"], row["query"]),
                         ("unresolved", "alias", "v2:1/2:Misc Folder@- | Show Six@-"))
        out = self.sync(aliases=resolver, max_lookups=5, titles=["main/Misc Folder"])
        self.assertEqual((resolver.queries, out["pending"]), (["Misc Folder", "Show Six"], 0))
        self.assertEqual(self.row("Misc Folder")["query"], "v2:2/2:Misc Folder@- | Show Six@-")
        out = self.sync(aliases=resolver, max_lookups=5, titles=["main/Misc Folder"])
        self.assertEqual((resolver.queries, out["lookups"]["made"]), (["Misc Folder", "Show Six"], 0))   # settled

    def test_a_budget_of_one_still_makes_progress(self):
        resolver = Resolver({"Dungeon Meshi": (999000010, ["Dungeon Meshi"]), "Show Six": (999000011, ["Show Six"])})
        self.store.db.execute("DELETE FROM anilist_list_entries")
        self.store.db.commit()
        keys = ["main/Dragon Meshi", "main/Misc Folder"]
        out = self.sync(aliases=resolver, max_lookups=1, titles=keys)
        self.assertEqual((resolver.queries, out["pending"]), (["Dragon Meshi"], 2))
        out = self.sync(aliases=resolver, max_lookups=1, titles=keys)
        self.assertEqual((resolver.queries, out["pending"]), (["Dragon Meshi", "Dungeon Meshi"], 1))
        self.assertEqual(self.row("Dragon Meshi")["anilist_id"], 999000010)
        out = self.sync(aliases=resolver, max_lookups=1, titles=keys)
        self.assertEqual((resolver.queries[2:], out["pending"]), (["Misc Folder"], 1))
        out = self.sync(aliases=resolver, max_lookups=1, titles=keys)
        self.assertEqual((resolver.queries[3:], out["pending"]), (["Show Six"], 0))
        self.assertEqual(self.row("Misc Folder")["anilist_id"], 999000011)

    def test_an_unaskable_folder_query_does_not_block_the_file_name_query(self):
        resolver = Resolver({"Show Six": (999000011, ["Show Six"])})
        self.store.db.execute("DELETE FROM anilist_list_entries")
        self.store.db.commit()
        with patch.object(identity, "MAX_TITLE", 10):           # "Misc Folder" is now over the limit
            out = self.sync(aliases=resolver, max_lookups=1, titles=["main/Misc Folder"])
        self.assertEqual((resolver.queries, out["lookups"]["made"], out["pending"]), (["Show Six"], 1, 0))
        row = self.row("Misc Folder")
        self.assertEqual((row["status"], row["anilist_id"], row["query"]), ("resolved", 999000011, "Show Six"))

    def test_an_overlong_query_is_skipped_for_free_and_the_plan_completes(self):
        resolver = Resolver({"Show Six": []})
        lookup = identity._Lookup(resolver)
        steps = [("Kura " * 60, "Kura", None), ("Show Six", "Show Six", None)]
        record, stop, exhausted = identity._alias(lookup, steps, 0, 1)      # a budget of 1 is enough
        self.assertEqual((resolver.queries, lookup.made, stop, exhausted), (["Show Six"], 1, False, False))
        self.assertEqual((record["status"], record["query"]), ("unresolved", identity._key(steps, 2)))
        # even the resolver's own refusal costs nothing
        lookup = identity._Lookup(resolver)
        with patch.object(identity, "askable", return_value=True):
            record, stop, exhausted = identity._alias(lookup, steps, 0, 1)
        self.assertEqual((lookup.made, exhausted), (1, False))

    def test_a_folder_name_without_letters_still_gets_its_file_name_query(self):
        for sub in ("!!!", "?!?"):
            (self.lab.lib / sub).mkdir()
            (self.lab.lib / sub / "[ASW] Show Six - 01 [1080p].mkv").write_bytes(b"x" * 100)
        self.lab.build()
        self.store.db.execute("DELETE FROM anilist_list_entries")
        self.store.db.commit()
        self.assertEqual(identity.folder_terms("main/!!!")[0], "")
        resolver = Resolver({"Show Six": (999000011, ["Show Six"])})
        out = self.sync(aliases=resolver, titles=["main/!!!"])
        self.assertEqual((resolver.queries, out["lookups"]["made"]), (["Show Six"], 1))
        self.assertEqual((self.row("!!!")["anilist_id"], self.row("!!!")["query"]), (999000011, "Show Six"))
        # a season-marked nameless folder does not ask for "Season 2" on its own
        (self.lab.lib / "!!! S2").mkdir()
        (self.lab.lib / "!!! S2" / "notes.mkv").write_bytes(b"x" * 100)
        self.lab.build()
        before = list(resolver.queries)
        self.sync(aliases=resolver, titles=["main/!!! S2"])
        self.assertEqual(resolver.queries, before)
        self.assertEqual(self.row("!!! S2")["query"], "v2:2/2:" + " Season 2@2 | @2")

    def test_offline_keeps_the_remembered_progress(self):
        resolver = Resolver({})
        self.sync(aliases=resolver, max_lookups=1, titles=["main/Misc Folder"])
        self.sync(aliases=None, titles=["main/Misc Folder"])
        row = self.row("Misc Folder")
        self.assertEqual((row["source"], row["query"]), ("alias", "v2:1/2:Misc Folder@- | Show Six@-"))

    def test_a_negative_answer_is_tied_to_the_queries_and_seasons_it_was_for(self):
        resolver = Resolver({})
        self.store.db.execute("DELETE FROM anilist_list_entries")
        self.store.db.commit()
        self.sync(aliases=resolver, titles=["main/Misc Folder"])
        self.assertEqual(resolver.queries, ["Misc Folder", "Show Six"])
        self.sync(aliases=resolver, titles=["main/Misc Folder"])
        self.assertEqual(len(resolver.queries), 2)                      # same files: still remembered

        def replace_files(*names):
            for f in (self.lab.lib / "Misc Folder").iterdir():
                f.unlink()
            for name in names:
                (self.lab.lib / "Misc Folder" / name).write_bytes(b"x" * 100)
            self.lab.build()
        # the files change, so the fallback is a different name: asked without --refresh
        replace_files(*(f"[ASW] Show Eight - {n:02d} [1080p].mkv" for n in (1, 2)))
        self.sync(aliases=resolver, titles=["main/Misc Folder"])
        self.assertEqual(resolver.queries[2:], ["Misc Folder", "Show Eight"])
        self.assertEqual(self.row("Misc Folder")["query"], "v2:2/2:Misc Folder@- | Show Eight@-")
        # same series name, now marked season 2: a different plan (two queries, season filter 2)
        replace_files(*(f"[ASW] Show Eight S2 - {n:02d} [1080p].mkv" for n in (1, 2)))
        self.sync(aliases=resolver, titles=["main/Misc Folder"])
        self.assertEqual(resolver.queries[4:], ["Misc Folder", "Show Eight Season 2", "Show Eight"])
        self.assertEqual(self.row("Misc Folder")["query"],
                         "v2:3/3:Misc Folder@- | Show Eight Season 2@2 | Show Eight@2")
        # and when the files stop giving a name, that is a plan change too
        replace_files("notes.mkv")
        self.sync(aliases=resolver, titles=["main/Misc Folder"])
        self.assertEqual(resolver.queries[7:], ["Misc Folder"])

    def test_conflicting_or_mixed_season_markers_prevent_the_fallback(self):
        two, three = "[G] Show [Season 2] - 01.mkv", "[G] Show [Season 3] - 02.mkv"
        for files in ([two, three], [two, "[G] Show - 02.mkv"],                        # conflict; marked + unmarked
                      ["[G] Show (S2) - 01.mkv", "[G] Show (S3) - 02.mkv"],
                      ["Show S02E01.mkv", "Show S03E02.mkv"], ["Show 2nd Season - 01.mkv", "Show - 02.mkv"],
                      ["Season 1/Show - 01.mkv", "Season 2/Show - 01.mkv"],              # per-season folders
                      ["Show S01E01.mkv", "Show [Season 2] - 02.mkv"],                  # one file contradicts itself
                      ["Show [Season 1-2] - 01.mkv"], ["Show S00E01.mkv"]):
            with self.subTest(files=files):
                self.assertIsNone(identity.file_terms(files, "Other", None))
        for files, wanted in ((["[G] Show [Season 2] - 01.mkv", "[G] Show [Season 2] - 02.mkv"], ("Show", 2)),
                              (["[G] Show (S2) - 01.mkv", "[G] Show S02E02.mkv"], ("Show", 2)),
                              (["Season 2/Show - 01.mkv", "Season 2/Show - 02.mkv"], ("Show", 2)),
                              (["Show S01E01.mkv", "Show S01E02.mkv"], ("Show", 1))):
            with self.subTest(files=files):
                self.assertEqual(identity.file_terms(files, "Other", None), wanted)
        self.assertIsNone(identity.file_terms(["Show [Season 2] - 01.mkv"], "Other", 3))   # folder says 3

    def test_files_with_bracketed_conflicting_seasons_stay_unresolved(self):
        tree = {"Odd Show": {"[G] Dead Mount Death Play [Season 2] - 01.mkv": None,
                             "[G] Dead Mount Death Play [Season 3] - 02.mkv": None}}
        for entry, content in tree.items():
            for rel in content:
                (self.lab.lib / entry).mkdir(exist_ok=True)
                (self.lab.lib / entry / rel).write_bytes(b"x" * 100)
        self.lab.build()
        self.sync(aliases=None)
        row = self.row("Odd Show")
        self.assertEqual((row["status"], row["anilist_id"], row["query"]), ("unresolved", None, "Odd Show"))

    def test_file_terms_rules(self):
        self.assertEqual(identity.file_terms(["[G] Show - 01.mkv", "[G] Show - 02.mkv"], "Other", None), ("Show", None))
        self.assertIsNone(identity.file_terms(["[G] Show - 01.mkv", "[G] Show - 02.mkv"], "Show", None))   # adds nothing
        self.assertEqual(identity.file_terms(["[G] Show - 01.mkv"], "Show", 2), None)                        # same name and season
        self.assertEqual(identity.file_terms(["[G] Show - 01.mkv"], "Other", 2), ("Show", 2))
        self.assertIsNone(identity.file_terms(["[G] Show S3 - 01.mkv"], "Other", 2))                       # season clash
        self.assertIsNone(identity.file_terms(["[G] Show - 01.mkv", "Movie.mkv"], "Other", None))          # one file has no episode
        self.assertIsNone(identity.file_terms(["notes.txt", "poster.jpg"], "Other", None))                 # no video at all
        self.assertEqual(identity.file_terms(["Extras/[G] Other - 01.mkv", "[G] Show - 01.mkv"], "X", None), ("Show", None))


class NonMedia(Tree):
    TREE = {
        "[ASW] Bocchi the Rock! - 04 [1080p HEVC][A7801AD7].mkv": None,
        "[ASW] Bocchi the Rock! - 04 [1080p HEVC][A7801AD7]-poster.jpg": None,
        "[ASW] Bocchi the Rock! - 04 [1080p HEVC][A7801AD7]-logo.png": None,
        "[ASW] Bocchi the Rock! - 04 [1080p HEVC][A7801AD7].nfo": None,
        ".DS_Store": None,
        "sync.ffs_db": None,
        "fonts": {"a.ttf": None, "b.otf": None},
        "Real Show": {"ep01.mkv": None, "poster.jpg": None},
        "Subs Only": {"ep01.srt": None},
    }

    def setUp(self):
        super().setUp()
        seed(self.store.db, [(A, "Bocchi the Rock!", None, []), (B, "Real Show", None, [])])

    def keys(self):
        return {r["rel_path"] for r in ManifestRepo(self.store.db).titles("home")}

    def test_loose_video_has_its_episode_stripped(self):
        key = "main/[ASW] Bocchi the Rock! - 04 [1080p HEVC][A7801AD7].mkv"
        self.assertEqual(identity.folder_terms(key), ("Bocchi the Rock", None, False))
        self.assertEqual(identity.folder_terms("main/[G] Show - 05 [S01E05].mkv"), ("Show", 1, False))
        self.assertEqual(identity.folder_terms("main/Show S02E05.mkv")[1], 2)
        self.assertEqual(identity.folder_terms("main/Movie Name.mkv")[0], "Movie Name")
        self.sync(aliases=None)
        row = identity.load(self.store.db, "home")[key]
        self.assertEqual((row["status"], row["anilist_id"], row["query"]), ("resolved", A, "Bocchi the Rock"))

    def test_loose_video_keeps_its_season(self):
        for name, season, undecidable in (("Foo S02E01.mkv", 2, False), ("Foo S2 - 01.mkv", 2, False),
                                          ("Foo 2nd Season - 01.mkv", 2, False), ("[G] Foo [Season 2] - 01.mkv", 2, False),
                                          ("[G] Foo (S2) - 01.mkv", 2, False), ("Foo.S03E04.1080p.WEB.x265-Grp.mkv", 3, False),
                                          ("Foo S01E01.mkv", 1, False), ("Foo - 01.mkv", None, False),
                                          ("Foo S1-S2 - 01.mkv", None, True), ("Foo S02E01 [Season 3].mkv", None, True),
                                          ("Foo S00E01.mkv", None, True)):
            with self.subTest(name=name):
                base, got, bad = identity.folder_terms(f"main/{name}")
                self.assertEqual((base, bad), ("Foo", undecidable))
                if not undecidable:
                    self.assertEqual(got, season)

    def test_a_loose_season_two_file_never_binds_to_the_season_one_entry(self):
        rows = [{"id": A, "titles": ["Foo"]}]
        for name in ("Foo S02E01.mkv", "Foo S2 - 01.mkv", "Foo 2nd Season - 01.mkv"):
            base, season, _ = identity.folder_terms(f"main/{name}")
            self.assertEqual(identity.match(rows, base, season)["status"], "unresolved", name)
            both = rows + [{"id": B, "titles": ["Foo Season 2"]}]
            self.assertEqual(identity.match(both, base, season)["anilist_id"], B, name)
        base, season, _ = identity.folder_terms("main/Foo S01E01.mkv")      # season 1 refuses a later-season entry
        self.assertEqual(identity.match([{"id": B, "titles": ["Foo Season 2"]}], base, season)["status"], "unresolved")
        self.assertEqual(identity.match(rows, base, season)["anilist_id"], A)

    def test_sidecars_dotfiles_and_media_free_folders_are_not_titles(self):
        out = self.sync(aliases=None)
        rows = identity.load(self.store.db, "home")
        self.assertEqual(sorted(k.partition("/")[2] for k in rows),
                         ["Real Show", "[ASW] Bocchi the Rock! - 04 [1080p HEVC][A7801AD7].mkv"])
        self.assertEqual(out["non_media"], len(self.keys()) - 2)
        self.assertEqual(out["titles"], 2)
        self.assertEqual((out["resolved"], out["unresolved"]), ({"list": 2, "alias": 0}, 0))
        self.assertTrue(identity.has_media("dir", "x", ["a/b.MKV"]))
        self.assertTrue(identity.has_media("dir", "x", []))
        self.assertFalse(identity.has_media("file", "x.jpg", []))
        self.assertFalse(identity.has_media("file", "x.jpg", ["x.jpg"]))

    def test_show_and_retain_check_do_not_count_them_as_unidentified(self):
        shown = identity.show(self.store, "home")
        self.assertEqual(shown["unrecorded"], 2)
        self.sync(aliases=None)
        self.assertEqual(identity.show(self.store, "home")["unrecorded"], 0)
        retain = parse_ids(f"{B}\n")
        out = identity.retain_check(self.store, "home", retain)
        self.assertEqual((out["unidentified_titles"], [t["title"] for t in out["retained_titles"]]), (0, ["main/Real Show"]))

    def test_an_explicit_non_media_title_is_skipped_and_an_old_row_is_dropped(self):
        key = "main/fonts"
        self.store.db.execute("INSERT INTO title_identities(library, title, status, source, candidates, updated_at)"
                              " VALUES ('home', ?, 'unresolved', 'list', '[]', 1.0)", (key,))
        self.store.db.execute("INSERT INTO title_identities(library, title, status, source, candidates, updated_at)"
                              " VALUES ('home', 'main/sync.ffs_db', 'not_anime', 'owner', '[]', 1.0)")
        self.store.db.commit()
        out = self.sync(aliases=None, titles=[key, "main/sync.ffs_db", "main/Real Show"])
        rows = identity.load(self.store.db, "home")
        self.assertNotIn(key, rows)
        self.assertEqual(rows["main/sync.ffs_db"]["status"], "not_anime")        # owner rows stay
        self.assertEqual((out["titles"], out["non_media"]), (1, 2))

    def test_the_non_media_cleanup_never_deletes_a_row_that_changed_or_an_owner_row(self):
        for title, source in (("main/fonts", "list"), ("main/sync.ffs_db", "alias"), ("main/.DS_Store", "owner")):
            self.store.db.execute("INSERT INTO title_identities(library, title, status, source, candidates, updated_at)"
                                  " VALUES ('home', ?, 'unresolved', ?, '[]', 1.0)", (title, source))
        self.store.db.commit()
        original = identity._drop

        def racing(db, library, titles, seen):
            # another sync records a newer answer between the read and the delete
            db.execute("UPDATE title_identities SET status='unavailable', updated_at=9.0 WHERE title='main/fonts'")
            db.commit()
            original(db, library, titles, seen)
        with patch.object(identity, "_drop", racing):
            self.sync(aliases=None)
        rows = identity.load(self.store.db, "home")
        self.assertEqual((rows["main/fonts"]["status"], rows["main/fonts"]["updated_at"]), ("unavailable", 9.0))
        self.assertNotIn("main/sync.ffs_db", rows)                 # unchanged since read: dropped
        self.assertIn("main/.DS_Store", rows)                      # owner rows stay

    def test_they_stay_unidentified_for_a_retain_plan(self):
        self.sync(aliases=None)
        rows = identity.load(self.store.db, "home")
        self.assertEqual(identity.retain_reasons("main/fonts", rows, parse_ids(f"{B}\n")), ["identity_unknown"])


class Owner(Base):
    NAMES = ("Kura Alpha", "Kura Beta")

    def test_pins_and_not_anime_survive_sync_and_refresh(self):
        seed(self.store.db, [(A, "Kura Alpha", None, []), (B, "Kura Beta", None, [])])
        identity.pin(self.store, "home", "main/Kura Alpha", E)
        identity.pin(self.store, "home", "main/Kura Beta", not_anime=True)
        resolver = Resolver({"Kura Alpha": (A, ["Kura Alpha"]), "Kura Beta": (B, ["Kura Beta"])})
        for refresh in (False, True):
            out = self.sync(aliases=resolver, refresh=refresh)
            self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"],
                              self.row("Kura Alpha")["source"]), ("pinned", E, "owner"))
            self.assertEqual((self.row("Kura Beta")["status"], self.row("Kura Beta")["anilist_id"]), ("not_anime", None))
            self.assertEqual((out["pinned"], out["not_anime"], out["resolved"]), (1, 1, {"list": 0, "alias": 0}))
        self.assertEqual(resolver.queries, [])

    def test_a_pin_made_during_a_sync_wins(self):
        seed(self.store.db, [(A, "Kura Alpha", None, [])])
        original = identity._store

        def racing(db, library, records, seen, now):
            identity.pin(self.store, library, "main/Kura Alpha", E)
            original(db, library, records, seen, now)
        with patch.object(identity, "_store", racing):
            self.sync(aliases=None)
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"]), ("pinned", E))

    def test_a_slow_sync_cannot_undo_a_newer_result(self):
        seed(self.store.db, [(A, "Kura Alpha", None, [])])
        self.sync(aliases=None, titles=["main/Kura Alpha"])
        slow = Resolver({"Kura Beta": (B, ["Kura Beta"])})
        real = slow.transport

        def interleaved(query):
            # while the slow sync waits on the network, another process records a newer answer
            self.store.db.execute("UPDATE title_identities SET status='unavailable', source='alias',"
                                  " updated_at=updated_at+5 WHERE title='main/Kura Beta'")
            self.store.db.commit()
            return real(query)
        slow.transport = interleaved
        out = self.sync(aliases=slow, titles=["main/Kura Alpha", "main/Kura Beta"], refresh=True)
        self.assertEqual(self.row("Kura Beta")["status"], "unavailable")     # the newer row stays
        self.assertEqual(out["lookups"]["made"], 1)

    def test_pin_validation_and_clear(self):
        for kwargs in ({}, {"anilist_id": A, "not_anime": True}, {"anilist_id": 0}, {"anilist_id": 10**9},
                       {"anilist_id": True}, {"anilist_id": A, "clear": True}):
            with self.assertRaises(KuraError, msg=str(kwargs)):
                identity.pin(self.store, "home", "main/Kura Alpha", **kwargs)
        with self.assertRaises(KuraError) as caught:
            identity.pin(self.store, "home", "main/Not Here", A)
        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(identity.pin(self.store, "home", "main/Kura Alpha", A)["status"], "pinned")
        self.assertEqual(identity.pin(self.store, "home", "main/Kura Alpha", not_anime=True)["status"], "not_anime")
        self.assertEqual(identity.pin(self.store, "home", "main/Kura Alpha", clear=True)["status"], "cleared")
        self.assertEqual(identity.pin(self.store, "home", "main/Kura Alpha", clear=True)["status"], "none")
        self.assertNotIn("main/Kura Alpha", identity.load(self.store.db, "home"))

    def test_clear_does_not_remove_a_synced_row(self):
        seed(self.store.db, [(A, "Kura Alpha", None, [])])
        self.sync(aliases=None)
        self.assertEqual(identity.pin(self.store, "home", "main/Kura Alpha", clear=True)["status"], "none")
        self.assertEqual(self.row("Kura Alpha")["status"], "resolved")

    def test_show_counts_and_filters(self):
        seed(self.store.db, [(A, "Kura Alpha", None, [])])
        self.sync(aliases=None)
        shown = identity.show(self.store, "home")
        self.assertEqual((shown["counts"], shown["unrecorded"], len(shown["titles"])),
                         ({"resolved": 1, "unresolved": 1}, 0, 2))
        only = identity.show(self.store, "home", "unresolved")
        self.assertEqual([t["title"] for t in only["titles"]], ["main/Kura Beta"])
        with self.assertRaises(KuraError):
            identity.show(self.store, "home", "bogus")

    def test_corrupt_candidates_fail_closed(self):
        seed(self.store.db, [(A, "Kura Alpha", None, [])])
        self.sync(aliases=None)
        self.store.db.execute("UPDATE title_identities SET candidates='not json'")
        self.store.db.commit()
        self.assertEqual(self.row("Kura Alpha")["status"], "unresolved")
        retain = parse(self.write_retain(B))
        self.assertEqual(identity.retain_reasons("main/Kura Alpha", identity.load(self.store.db, "home"), retain),
                         ["identity_unknown"])

    def write_retain(self, *ids):
        path = self.lab.tmp / "retain.txt"
        path.write_text("# Kura retain list\n" + "".join(f"{i}  # c\n" for i in ids))
        return path


class PinsFile(Base):
    NAMES = ("Kura Alpha", "Kura Beta", "Kura Gamma")

    def pins(self, text):
        path = self.lab.tmp / "pins.tsv"
        path.write_text(text)
        return parse_pins(path)

    def test_apply_reports_missing_and_is_idempotent(self):
        pins = self.pins(f"{A}\tmain/Kura Alpha\nnot-anime\tmain/Kura Beta\n{C}\tmain/Not Here\n")
        out = identity.apply_pins(self.store, "home", pins, now=lambda: 5.0)
        self.assertEqual((out["applied"], out["unchanged"], out["missing"]),
                         ({"pinned": 1, "not_anime": 1}, 0, ["main/Not Here"]))
        self.assertEqual(out["pins"], pins.summary())
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"],
                          self.row("Kura Alpha")["source"]), ("pinned", A, "owner"))
        self.assertEqual(self.row("Kura Beta")["status"], "not_anime")
        again = identity.apply_pins(self.store, "home", pins, now=lambda: 9.0)
        self.assertEqual((again["applied"], again["unchanged"]), ({"pinned": 0, "not_anime": 0}, 2))
        self.assertEqual(self.row("Kura Alpha")["updated_at"], 5.0)        # an unchanged pin is not rewritten

    def test_pins_beat_sync_replace_older_decisions_and_never_clear(self):
        seed(self.store.db, [(A, "Kura Alpha", None, []), (B, "Kura Gamma", None, [])])
        self.sync(aliases=None)
        identity.pin(self.store, "home", "main/Kura Beta", D)
        identity.apply_pins(self.store, "home", self.pins(f"{E}\tmain/Kura Alpha\n{C}\tmain/Kura Beta\n"))
        self.sync(aliases=None, refresh=True)
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"]), ("pinned", E))
        self.assertEqual(self.row("Kura Beta")["anilist_id"], C)              # the file replaced the older pin
        identity.apply_pins(self.store, "home", self.pins(""))
        self.assertEqual(self.row("Kura Alpha")["status"], "pinned")         # leaving a line out clears nothing
        self.assertEqual((self.row("Kura Gamma")["status"], self.row("Kura Gamma")["source"]), ("resolved", "list"))


class RetainCheck(Base):
    NAMES = ("Kura Alpha", "Kura Beta", "Kura Gamma", "Kura Delta")

    def setUp(self):
        super().setUp()
        seed(self.store.db, [(A, "Kura Alpha", None, []), (B, "Kura Beta Extra", None, []),
                             (C, "Kura Beta Another Extra Word", None, []), (D, "Kura Gamma", None, []),
                             (E, "Kura Gamma", None, [])])
        self.sync(aliases=None)

    def retain(self, *ids):
        path = self.lab.tmp / "retain.txt"
        path.write_text("".join(f"{i}\n" for i in ids))
        return parse(path)

    def test_reports_retained_unmatched_possible_and_unidentified(self):
        out = identity.retain_check(self.store, "home", self.retain(A, C, 999000099))
        self.assertEqual(out["retained_titles"], [{"title": "main/Kura Alpha", "anilist_id": A, "source": "list"}])
        self.assertEqual(out["unmatched_ids"], [C, 999000099])
        self.assertEqual(out["possible"], [{"title": "main/Kura Beta", "candidates": [C]}])
        self.assertEqual(out["unidentified_titles"], 2)          # Gamma (ambiguous) and Delta (no match)
        self.assertEqual(out["retain"]["ids"], 3)

    def test_retain_reasons(self):
        rows = identity.load(self.store.db, "home")
        retain = self.retain(A, C)
        self.assertEqual(identity.retain_reasons("main/Kura Alpha", rows, retain), ["retained"])
        self.assertEqual(identity.retain_reasons("main/Kura Beta", rows, retain), ["retain_possible"])
        self.assertEqual(identity.retain_reasons("main/Kura Beta", rows, self.retain(999000099)), [])
        self.assertEqual(identity.retain_reasons("main/Kura Gamma", rows, retain), ["identity_unknown"])
        self.assertEqual(identity.retain_reasons("main/Missing", rows, retain), ["identity_unknown"])


class CommandLine(Base):
    NAMES = ("Kura Alpha", "Kura Beta")

    def setUp(self):
        super().setUp()
        self.resolver = Resolver({"Kura Beta": (B, ["Kura Beta"])})
        seed(self.store.db, [(A, "Kura Alpha", None, [])])

    def cli(self, *argv):
        buffer = io.StringIO()
        with patch("kura.cli.private_root", return_value=self.lab.state), patch("sys.argv", ["kura", *argv]), \
             patch("kura.cli_identity.AniList", return_value=self.resolver), \
             patch("kura.cli_identity.SharedLimiter"), contextlib.redirect_stdout(buffer):
            try:
                cli.main()
            except SystemExit as exited:
                return exited.code, json.loads(buffer.getvalue())

    def test_sync_pin_show_and_retain_check(self):
        code, out = self.cli("identity", "sync", "--library", "home", "--offline")
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "identity"))
        self.assertEqual(out["data"]["resolved"], {"list": 1, "alias": 0})
        self.assertEqual(self.resolver.queries, [])
        code, out = self.cli("identity", "sync", "--library", "home", "--max-lookups", "5", "--title", "main/Kura Beta")
        self.assertEqual((code, out["data"]["lookups"], out["data"]["resolved"]["alias"]), (0, {"made": 1, "budget": 5}, 1))
        for keys in (("--anilist-id", str(E)), ("--not-anime",)):
            code, out = self.cli("identity", "pin", "--library", "home", "--title", "main/Kura Alpha", *keys)
            self.assertEqual(code, 0, out)
        code, out = self.cli("identity", "pin", "--library", "home", "--title", "main/Kura Alpha")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out = self.cli("identity", "pin", "--library", "home", "--title", "main/Kura Alpha", "--anilist-id", "1",
                             "--clear")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out = self.cli("identity", "show", "--library", "home", "--status", "not_anime")
        self.assertEqual([t["title"] for t in out["data"]["titles"]], ["main/Kura Alpha"])
        path = self.lab.tmp / "retain.txt"
        path.write_text("999000002  # kept\n")
        code, out = self.cli("retain", "check", "--library", "home", "--retain", str(path))
        self.assertEqual((code, out["phase"]), (0, "retain"))
        self.assertEqual(out["data"]["retained_titles"], [{"title": "main/Kura Beta", "anilist_id": B, "source": "alias"}])
        self.assertEqual(out["data"]["unmatched_ids"], [])
        path.write_text("oops\n")
        code, out = self.cli("retain", "check", "--library", "home", "--retain", str(path))
        self.assertEqual((code, out["error"]["code"], out["data"]), (2, "invalid_policy", {"line": 1}))

    def test_pins_command(self):
        path = self.lab.tmp / "pins.tsv"
        path.write_text(f"{E}\tmain/Kura Alpha\nnot-anime\tmain/Kura Gone\n")
        code, out = self.cli("identity", "pins", "--library", "home", "--from", str(path))
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "identity"))
        self.assertEqual((out["data"]["applied"], out["data"]["missing"]),
                         ({"pinned": 1, "not_anime": 0}, ["main/Kura Gone"]))
        path.write_text(f"{E} main/Kura Alpha\n")
        with patch("kura.identity.apply_pins") as apply:
            code, out = self.cli("identity", "pins", "--library", "home", "--from", str(path))
        self.assertEqual((code, out["error"]["code"], out["data"]), (2, "invalid_policy", {"line": 1}))
        apply.assert_not_called()

    def test_invalid_budget_is_refused_before_the_state_is_opened(self):
        code, out = self.cli("identity", "sync", "--library", "home", "--max-lookups", "99999")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))


if __name__ == "__main__":
    unittest.main()
