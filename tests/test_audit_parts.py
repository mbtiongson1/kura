"""The audit of multi-part titles: per-part policy, flags and SeaDex, with synthetic state."""
import json
import unittest

from kura import audit
from test_audit import A, B, C, GROUP, KEY, MB, NOW, OTHER, SHOW, Lab, ep


def sfile(season, n, group=GROUP, size=250 * MB):
    return (f"Season {season}/{ep(n, group=group)}", size)


class Parted(Lab):
    def parts(self, *rows, source="owner", seed=None, mode_row=True):
        for n, (anilist_id, selector) in enumerate(rows, 1):
            self.db.execute("INSERT INTO title_parts VALUES ('home',?,?,?,?,?,?,?,1.0)",
                            (KEY, anilist_id, selector, source, n, n if source == "relations" else None, seed))
        if mode_row and source == "owner":
            self.db.execute("INSERT OR REPLACE INTO title_identities VALUES ('home',?,?,'pinned','owner',NULL,?,?,1.0)",
                            (KEY, rows[0][0], f"parts:{len(rows)}", json.dumps([r[0] for r in rows[1:]])))
        self.db.commit()

    def media(self, anilist_id, title, episodes=3, fmt="TV"):
        self.db.execute("INSERT OR REPLACE INTO anilist_media(anilist_id, title_romaji, format, episodes, status,"
                        " fetched_at) VALUES (?,?,?,?, 'FINISHED', 1.0)", (anilist_id, title, fmt, episodes))
        self.db.commit()

    def two_seasons(self, g2=GROUP):
        self.title(files=[sfile(1, n) for n in (1, 2, 3)] + [sfile(2, n, group=g2) for n in (1, 2, 3)])
        self.media(A, SHOW)
        self.media(B, f"{SHOW} 2nd Season")
        self.parts((A, "auto"), (B, "auto"))


class PerPart(Parted):
    def test_the_document_shape(self):
        self.two_seasons()
        doc = self.doc()
        self.assertEqual((doc["anilist_id"], doc["identity"]), (A, {"status": "parted", "source": "owner", "mode": "owner"}))
        self.assertEqual([(p["anilist_id"], p["files"]["video"], p["episodes"]["held"], p["episodes"]["local_range"],
                           p["rule_counts"]) for p in doc["parts"]],
                         [(A, 3, 3, [1, 3], {"dir_season": 3}), (B, 3, 3, [1, 3], {"dir_season": 3})])
        self.assertEqual(doc["unassigned"], {"video": 0, "reasons": {}, "sample": []})
        self.assertEqual(doc["files"]["video"], 6)                  # title-wide stats stay
        report = self.audit()
        self.assertEqual(report["counts"]["parts"], {"titles": 1, "parts": 2, "by_policy": {"archival": 0, "efficient": 2},
                                                     "unassigned_video": 0})

    def test_single_part_documents_are_unchanged(self):
        self.eps()
        self.identify()
        doc = self.doc()
        self.assertNotIn("parts", doc)
        self.assertNotIn("policy_mixed", doc)
        self.assertEqual(doc["identity"], {"status": "resolved", "source": "list"})
        self.assertNotIn("parts", self.audit()["counts"])

    def test_policy_is_decided_per_part(self):
        self.two_seasons()
        self.viewer(scores={A: 9.5, B: 7.0})
        doc = self.doc()
        self.assertEqual([p["policy"] for p in doc["parts"]], ["archival", "efficient"])
        self.assertEqual((doc["policy"], doc["policy_mixed"], doc["policy_reasons"]), ("archival", True, ["score_9_plus"]))

    def test_size_band_only_on_the_efficient_part(self):
        self.title(files=[sfile(1, n, size=900 * MB) for n in (1, 2, 3)] + [sfile(2, n, size=900 * MB) for n in (1, 2, 3)])
        self.media(A, SHOW)
        self.media(B, f"{SHOW} 2nd Season")
        self.parts((A, "auto"), (B, "auto"))
        self.viewer(scores={A: 9.5})
        flags = self.doc()["flags"]
        self.assertEqual([(f["code"], f["detail"]["part"]) for f in flags if f["code"] == "size_above_band"], [("size_above_band", B)])
        self.assertNotIn("size_above_band", {f["code"] for f in self.doc()["parts"][0]["flags"]})

    def test_mixed_groups_are_per_part(self):
        self.two_seasons(g2=OTHER)              # a different group per season is not mixing
        self.assertNotIn("mixed_groups", self.codes())
        self.title(files=[sfile(1, 1), sfile(1, 2, group=OTHER), sfile(2, 1)])
        self.assertEqual([f["detail"]["part"] for f in self.doc()["flags"] if f["code"] == "mixed_groups"], [A])

    def test_seadex_best_differing_on_one_part_only(self):
        self.two_seasons()
        self.seadex(A, [{"group": GROUP}], entry="entry000001")
        self.seadex(B, [{"group": OTHER}], entry="entry000002")
        doc = self.doc()
        self.assertEqual([p["seadex"]["held_matches"] for p in doc["parts"]], ["best", "none"])
        self.assertEqual([p["suggestion"] for p in doc["parts"]], ["none", "none"])   # info only under efficient
        self.assertEqual([(f["code"], f["detail"]["part"]) for f in doc["flags"] if f["code"] == "seadex_best_differs"],
                         [("seadex_best_differs", B)])
        self.assertEqual(doc["seadex"]["held_matches"], "none")
        self.viewer(scores={B: 9.5})
        doc = self.doc()
        self.assertEqual((doc["suggestion"], doc["parts"][1]["suggestion"]), ("upgrade_candidate", "upgrade_candidate"))

    def test_unassigned_episodes_are_flagged(self):
        self.title(files=[sfile(1, 1), sfile(2, 1), ("Misc/" + ep(9), 250 * MB)])
        self.media(A, SHOW)
        self.media(B, f"{SHOW} 2nd Season")
        self.parts((A, "auto"), (B, "auto"))
        flag = self.codes()["part_unassigned"]
        self.assertEqual((flag["severity"], flag["detail"]["video"]), ("info", 1))
        self.assertEqual(self.doc()["unassigned"]["video"], 1)
        flag = self.codes(self.audit(retain=self.idfile(f"{C}\n")))["part_unassigned"]
        self.assertEqual(flag["severity"], "warn")

    def test_retained_parts_and_flag_order(self):
        self.two_seasons()
        report = self.audit(retain=self.idfile(f"{B}\n"))
        doc = self.doc(report)
        self.assertEqual([p["retained"] for p in doc["parts"]], [False, True])
        self.assertTrue(doc["retained"])
        self.assertEqual(report["inputs"]["retain"]["unmatched_ids"], [])
        codes = [f["code"] for f in doc["flags"]]
        self.assertEqual(codes, sorted(codes, key=audit.FLAGS.index))

    def test_stale_derived_parts_read_as_unidentified(self):
        self.two_seasons()
        self.db.execute("DELETE FROM title_parts")
        self.db.execute("INSERT OR REPLACE INTO title_identities(library, title, anilist_id, status, source, candidates,"
                        " updated_at) VALUES ('home',?,?,'resolved','list','[]',1.0)", (KEY, C))
        self.parts((A, "auto"), (B, "auto"), source="relations", seed=A)
        codes = self.codes()
        self.assertEqual((codes["identity_unknown"]["detail"]["status"], codes["parts_pending"]["detail"]["reason"]),
                         ("parts_stale", "parts_stale"))
        self.assertNotIn("parts", self.doc())


if __name__ == "__main__":
    unittest.main()
