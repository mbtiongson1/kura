"""Multi-part titles under a retain list: the effective view, quarantine plans and the retain check.
Synthetic data only; parts are inserted directly (discovery and pins come in later steps)."""
import json
import unittest
from unittest.mock import patch

from kura import identity, quarantine, titleparts
from kura.idlist import parse
import torrentgen
from test_manifest import Lab
from test_quarantine import Base

P = torrentgen.payload
S1, S2, S3 = 999000021, 999000022, 999000023
OTHER = 999000099
FILES = [("Season 1/Kura Saga - 01.mkv", P("a", 30000)), ("Season 1/Kura Saga - 02.mkv", P("b", 20000)),
         ("Season 2/Kura Saga - 01.mkv", P("c", 30000)), ("Season 2/Kura Saga - 02.mkv", P("d", 20000))]
ODD = FILES + [("Misc/Kura Saga - 03.mkv", P("e", 10000))]       # an episode no rule can place
TITLE = "main/Kura Saga"


class Parted(Base):
    FILES = FILES

    def setUp(self):
        super().setUp()
        self.add("Kura Saga", self.FILES)
        self.lab.build()
        db = self.lab.store.db
        for i, title in ((S1, "Kura Saga"), (S2, "Kura Saga 2nd Season"), (S3, "Kura Saga Season 3")):
            db.execute("INSERT INTO anilist_media(anilist_id, title_romaji, format, episodes, status, fetched_at)"
                       " VALUES (?,?, 'TV', 2, 'FINISHED', 1.0)", (i, title))
        db.commit()

    def parts(self, *rows, source="owner", seed=None):
        db = self.lab.store.db
        db.execute("DELETE FROM title_parts")
        for n, (anilist_id, selector) in enumerate(rows, 1):
            db.execute("INSERT INTO title_parts VALUES ('home',?,?,?,?,?,?,?,1.0)",
                       (TITLE, anilist_id, selector, source, n, n if source == "relations" else None, seed))
        db.commit()

    def identity_row(self, anilist_id, status="pinned", candidates=()):
        db = self.lab.store.db
        db.execute("DELETE FROM title_identities")
        db.execute("INSERT INTO title_identities VALUES ('home',?,?,?,'owner',NULL,NULL,?,1.0)",
                   (TITLE, anilist_id, status, json.dumps(list(candidates))))
        db.commit()

    def retain(self, *ids):
        path = self.lab.tmp / "retain.txt"
        path.write_text("# Kura retain list\n" + "".join(f"{i}\n" for i in ids))
        return parse(path)

    def reasons(self, *ids, **kw):
        doc = self.plan(retain=self.retain(*ids), **kw)
        return doc, self.refused(doc).get(TITLE, {}).get("reasons")


class OwnerParts(Parted):
    def setUp(self):
        super().setUp()
        self.parts((S1, "auto"), (S2, "auto"))
        self.identity_row(S1, candidates=[S2])      # the safety-net row an owner pin writes

    def test_part_two_retained_part_one_not(self):
        doc, reasons = self.reasons(S2)
        self.assertEqual(reasons, ["retained"])
        self.assertEqual(doc["moves"], [])

    def test_no_part_listed_is_proposed(self):
        doc, reasons = self.reasons(OTHER)
        self.assertIsNone(reasons)
        self.assertEqual([m["from"] for m in doc["moves"]], ["Kura Saga"])

    def test_every_declared_part_counts_even_without_files(self):
        self.parts((S1, "auto"), (S2, "auto"), (S3, "dir:Specials"))
        self.assertEqual(self.reasons(S3)[1], ["retained"])

    def test_a_later_pin_or_clear_makes_owner_parts_stale(self):
        for change in (lambda: self.identity_row(OTHER), lambda: self.identity_row(S1, candidates=[S2, OTHER]),
                       lambda: self.identity_row(S1, status="resolved"),
                       lambda: self.lab.store.db.execute("DELETE FROM title_identities")):
            change()
            self.assertEqual(self.reasons(S1)[1], ["identity_unknown"])
            self.assertEqual(self.reasons(OTHER)[1], ["identity_unknown"])

    def test_relatives_are_not_consulted_in_owner_mode(self):
        self.assertIsNone(self.reasons(S3)[1])

    def test_not_listing_anything_consults_no_parts(self):
        with patch.object(titleparts, "effective", side_effect=AssertionError("parts read")):
            doc = self.plan()
        self.assertEqual([m["from"] for m in doc["moves"]], ["Kura Saga"])


class Incomplete(Parted):
    FILES = ODD

    def test_an_unassigned_episode_refuses_the_title_whatever_is_listed(self):
        self.parts((S1, "auto"), (S2, "auto"))
        self.identity_row(S1)
        self.assertEqual(self.reasons(OTHER)[1], ["parts_incomplete"])
        self.assertEqual(self.reasons(S2)[1], ["retained", "parts_incomplete"])

    def test_the_retain_check_reports_it(self):
        self.parts((S1, "auto"), (S2, "auto"))
        self.identity_row(S1)
        out = identity.retain_check(self.lab.store, "home", self.retain(OTHER))
        self.assertEqual(out["incomplete_titles"], [{"title": TITLE, "unassigned_video": 1,
                                                     "reasons": {"absolute_mismatch": 1}}])


class AutoParts(Parted):
    def setUp(self):
        super().setUp()
        self.identity_row(None, status="ambiguous")
        self.parts((S1, "auto"), (S2, "auto"), (S3, "auto"), source="relations", seed=S1)

    def test_a_listed_relative_without_files_blocks_as_possible(self):
        self.assertEqual(self.reasons(S3)[1], ["retain_possible"])

    def test_a_listed_part_with_files_is_retained(self):
        self.assertEqual(self.reasons(S2)[1], ["retained"])

    def test_the_identity_candidates_are_consulted(self):
        self.identity_row(None, status="ambiguous", candidates=[OTHER])
        self.assertEqual(self.reasons(OTHER)[1], ["retain_possible"])

    def test_unlisted_title_is_proposed(self):
        self.assertIsNone(self.reasons(OTHER)[1])

    def test_a_moved_identity_makes_the_parts_stale(self):
        self.identity_row(S2, status="resolved")
        self.assertEqual(self.reasons(OTHER)[1], ["identity_unknown"])

    def test_unreadable_candidates_fail_closed(self):
        self.lab.store.db.execute("UPDATE title_identities SET candidates='not json'")
        self.lab.store.db.commit()
        self.assertEqual(self.reasons(OTHER)[1], ["identity_unknown"])

    def test_the_unresolved_state_keeps_the_parts(self):
        self.identity_row(None, status="unresolved")
        self.assertIsNone(self.reasons(OTHER)[1])


class Guards(Parted):
    def test_no_parts_means_the_old_rules(self):
        self.identity_row(S1)
        self.assertEqual(self.reasons(S1)[1], ["retained"])
        self.assertIsNone(self.reasons(OTHER)[1])
        views = titleparts.effective(self.lab.store.db, "home")
        self.assertEqual(views[TITLE]["mode"], "single")

    def test_defence_in_depth_catches_a_retained_part_in_the_move_list(self):
        self.parts((S1, "auto"), (S2, "auto"))
        real = titleparts.retain_reasons
        calls = []

        def lenient_then_real(*args, **kw):
            calls.append(1)
            return [] if len(calls) == 1 else real(*args, **kw)
        with patch.object(quarantine.titleparts, "retain_reasons", side_effect=lenient_then_real):
            with self.assertRaises(quarantine.KuraError) as cm:
                self.plan(retain=self.retain(S2))
        self.assertEqual(cm.exception.code, "retain_violation")


class RetainCheck(Parted):
    def test_owner_parts_are_listed_and_ids_are_bound(self):
        self.parts((S1, "auto"), (S2, "auto"))
        self.identity_row(S1, candidates=[S2])
        out = identity.retain_check(self.lab.store, "home", self.retain(S2, OTHER))
        self.assertEqual(out["retained_titles"], [
            {"title": TITLE, "anilist_id": S1, "source": "owner",
             "parts": [{"anilist_id": S1, "selector": "auto", "retained": False},
                       {"anilist_id": S2, "selector": "auto", "retained": True}]}])
        self.assertEqual(out["unmatched_ids"], [OTHER])
        self.assertEqual(out["unidentified_titles"], 0)
        self.assertNotIn("incomplete_titles", out)

    def test_relatives_are_reported_by_kind(self):
        self.identity_row(None, status="ambiguous", candidates=[OTHER])
        self.parts((S1, "auto"), (S2, "auto"), (S3, "auto"), source="relations", seed=S1)
        out = identity.retain_check(self.lab.store, "home", self.retain(S3, S1, OTHER))
        self.assertEqual(out["possible"], [{"title": TITLE, "candidates": [OTHER], "via": "candidate"},
                                           {"title": TITLE, "candidates": [S3], "via": "relative"}])
        self.assertEqual([t["title"] for t in out["retained_titles"]], [TITLE])
        self.assertEqual(out["unmatched_ids"], [S3, OTHER])

    def test_stale_parts_count_as_unidentified(self):
        self.identity_row(S2, status="resolved")
        self.parts((S1, "auto"), (S2, "auto"), source="relations", seed=S1)
        out = identity.retain_check(self.lab.store, "home", self.retain(S1))
        self.assertEqual((out["unidentified_titles"], out["retained_titles"]), (1, []))


if __name__ == "__main__":
    unittest.main()
