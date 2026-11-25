"""Title identity sync, owner pins and the retain check. Synthetic data; no network."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import kura.cli as cli
from kura import identity
from kura.anilist import Identity, choose
from kura.core import Core
from kura.idlist import parse
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


class Resolver:
    """Stands in for AniList: ``answers`` maps a query to ``(anilist_id, titles)``, ``"ambiguous"`` or ``"down"``."""

    def __init__(self, answers):
        self.answers, self.queries = answers, []

    def resolve(self, title):
        self.queries.append(title)
        answer = self.answers.get(title)
        if answer == "down":
            raise KuraError("alias_unavailable", "AniList rate limit reached; retry later")
        if answer == "ambiguous":
            return None, "ambiguous"
        if answer is None:
            return None, "unresolved"
        anilist_id, titles = answer
        return Identity(anilist_id, tuple(titles), titles[0]), "resolved"


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
        with patch.object(Core, "alias_status", return_value={"status": "unavailable", "titles": [], "identity": None}):
            out = self.sync(aliases=Resolver({}), titles=["main/Kura Alpha"], refresh=True)
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"]), ("unavailable", None))
        self.assertEqual(out["unavailable"], 1)

    def test_an_unconfirmed_resolved_row_is_kept_without_refresh(self):
        self.sync(aliases=Resolver({"Kura Alpha": (A, ["Kura Alpha"])}), titles=["main/Kura Alpha"])
        with patch.object(Core, "alias_status", return_value={"status": "unavailable", "titles": [], "identity": None}):
            self.sync(aliases=Resolver({}), titles=["main/Kura Alpha"])
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

        def racing(db, library, records, now):
            identity.pin(self.store, library, "main/Kura Alpha", E)
            original(db, library, records, now)
        with patch.object(identity, "_store", racing):
            self.sync(aliases=None)
        self.assertEqual((self.row("Kura Alpha")["status"], self.row("Kura Alpha")["anilist_id"]), ("pinned", E))

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

    def test_invalid_budget_is_refused_before_the_state_is_opened(self):
        code, out = self.cli("identity", "sync", "--library", "home", "--max-lookups", "99999")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))


if __name__ == "__main__":
    unittest.main()
