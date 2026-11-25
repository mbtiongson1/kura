"""`kura anilist update`: one planned write to the viewer's own AniList entry. No network."""
import unittest

from anilistfake import BASE_ID, VIEWER, entry, media
from kura import anilist_update
from kura.models import KuraError
from test_anilist_list import Lab

IDS = [BASE_ID + n for n in range(1, 6)]


class Update(Lab):
    def setUp(self):
        super().setUp()
        self.login()

    def update(self, **kw):
        return anilist_update.update(self.store, self.ani(), **kw)

    def entry_row(self, identifier):
        return self.rows("SELECT status,progress,score FROM anilist_list_entries WHERE user_id=? AND anilist_id=?",
                         VIEWER["id"], identifier)

    def refused(self, code, **kw):
        with self.assertRaises(KuraError) as caught:
            self.update(**kw)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_progress_on_a_current_entry_reads_then_writes_only_progress(self):
        self.fake.list_entries[IDS[0]] = {"status": "CURRENT", "progress": 7, "score": 0, "repeat": 0}
        out = self.update(anilist_id=IDS[0], progress=8)
        self.assertEqual([c["query"].split("(")[0] for c in self.fake.calls], ["query", "mutation"])
        self.assertEqual(self.fake.saves, [{"mediaId": IDS[0], "progress": 8}])
        save = self.fake.calls[-1]["query"]
        self.assertIn("$progress:Int", save)
        self.assertNotIn("$status", save)
        self.assertNotIn("scoreRaw", save)
        self.assertEqual(self.fake.calls[-1]["token"], self.token)
        self.assertEqual((out["written"], out["before"]["progress"], out["after"]), (True, 7, {
            "status": "CURRENT", "progress": 8, "score": None}))
        self.assertEqual(out["change"], {"progress": 8})
        self.assertEqual(out["requests"], {"made": 2, "budget": 3})
        self.assertTrue(out["snapshot_updated"])
        self.assertEqual(self.entry_row(IDS[0]), [("CURRENT", 8, None)])
        self.assertNotIn(self.token, repr(out))

    def test_new_or_planned_entry_becomes_current(self):
        out = self.update(anilist_id=IDS[1], progress=3)
        self.assertEqual(out["change"], {"progress": 3, "status": "CURRENT"})
        self.assertIsNone(out["before"])
        self.fake.list_entries[IDS[2]] = {"status": "PLANNING", "progress": 0, "score": 0, "repeat": 0}
        self.assertEqual(self.update(anilist_id=IDS[2], progress=1)["after"]["status"], "CURRENT")

    def test_final_episode_of_a_finished_show_completes_it(self):
        self.fake.list_entries[IDS[0]] = {"status": "CURRENT", "progress": 11, "score": 0, "repeat": 0}
        out = self.update(anilist_id=IDS[0], progress=12)
        self.assertEqual(out["after"], {"status": "COMPLETED", "progress": 12, "score": None})

    def test_final_episode_of_an_airing_show_stays_current(self):
        self.fake.media_overrides[IDS[0]] = {**media(IDS[0]), "status": "RELEASING"}
        self.fake.list_entries[IDS[0]] = {"status": "CURRENT", "progress": 11, "score": 0, "repeat": 0}
        self.assertEqual(self.update(anilist_id=IDS[0], progress=12)["change"], {"progress": 12})

    def test_a_rewatch_reaching_the_end_is_left_to_the_owner(self):
        self.fake.list_entries[IDS[0]] = {"status": "REPEATING", "progress": 11, "score": 0, "repeat": 1}
        self.assertEqual(self.update(anilist_id=IDS[0], progress=12)["change"], {"progress": 12})

    def test_a_new_entry_always_gets_an_explicit_status(self):
        self.update(anilist_id=IDS[1], score=7.0)
        self.update(anilist_id=IDS[2], progress=0)
        self.assertEqual(self.fake.saves, [{"mediaId": IDS[1], "status": "PLANNING", "scoreRaw": 70},
                                           {"mediaId": IDS[2], "progress": 0, "status": "PLANNING"}])

    def test_a_token_expiring_soon_is_reported(self):
        self.login(days=10)
        self.assertIn("anilist_token_expires_soon", self.update(anilist_id=IDS[0], progress=1, dry_run=True)["warnings"])

    def test_explicit_status_wins(self):
        self.fake.list_entries[IDS[0]] = {"status": "CURRENT", "progress": 11, "score": 0, "repeat": 0}
        out = self.update(anilist_id=IDS[0], progress=12, status="PAUSED")
        self.assertEqual(out["after"]["status"], "PAUSED")

    def test_lowering_progress_needs_the_flag(self):
        self.fake.list_entries[IDS[0]] = {"status": "CURRENT", "progress": 9, "score": 0, "repeat": 0}
        self.refused("anilist_progress_decrease", anilist_id=IDS[0], progress=8)
        self.assertEqual(self.fake.saves, [])
        self.assertEqual(self.update(anilist_id=IDS[0], progress=8, allow_decrease=True)["after"]["progress"], 8)

    def test_progress_past_the_last_episode_is_refused(self):
        self.refused("anilist_progress_out_of_range", anilist_id=IDS[0], progress=13)
        self.assertEqual(self.fake.saves, [])

    def test_progress_ahead_of_airing_warns(self):
        self.fake.media_overrides[IDS[0]] = {**media(IDS[0]), "status": "RELEASING", "episodes": None,
                                             "nextAiringEpisode": {"episode": 5, "airingAt": 1_900_000_000}}
        self.assertEqual(self.update(anilist_id=IDS[0], progress=6)["warnings"], ["anilist_progress_ahead_of_airing"])

    def test_dry_run_reads_and_never_writes(self):
        out = self.update(anilist_id=IDS[0], progress=4, dry_run=True)
        self.assertEqual((out["written"], out["change"], out["requests"]["made"]),
                         (False, {"progress": 4, "status": "CURRENT"}, 1))
        self.assertEqual(self.fake.count("SaveMediaListEntry"), 0)
        self.assertEqual(self.entry_row(IDS[0]), [])

    def test_nothing_to_change_makes_no_write(self):
        self.fake.list_entries[IDS[0]] = {"status": "CURRENT", "progress": 8, "score": 8.5, "repeat": 0}
        out = self.update(anilist_id=IDS[0], progress=8, score=8.5)
        self.assertEqual((out["written"], out["change"]), (False, {}))
        self.assertEqual(self.fake.count("SaveMediaListEntry"), 0)

    def test_score_is_sent_as_raw_hundredths(self):
        self.fake.list_entries[IDS[0]] = {"status": "COMPLETED", "progress": 12, "score": 0, "repeat": 0}
        out = self.update(anilist_id=IDS[0], score=8.5)
        self.assertEqual(self.fake.saves, [{"mediaId": IDS[0], "scoreRaw": 85}])
        self.assertEqual(out["after"]["score"], 8.5)

    def test_title_resolves_from_the_stored_list(self):
        self.fake.entries = [entry(i, "CURRENT", progress=1) for i in IDS[:3]]
        self.sync()
        self.fake.calls.clear()
        out = self.update(title="kura sample series 2", progress=2)
        self.assertEqual((out["anilist_id"], out["resolved"]["via"]), (IDS[1], "list"))
        self.assertEqual(self.fake.count("media(search"), 0)

    def test_a_tied_title_is_refused_with_candidates_and_no_request(self):
        self.fake.entries = [entry(i, "CURRENT", progress=1) for i in IDS[:3]]
        self.sync()
        self.fake.calls.clear()
        error = self.refused("anilist_title_ambiguous", title="Kura Sample Series", progress=2)
        self.assertEqual({c["anilist_id"] for c in error.details["candidates"]}, set(IDS[:3]))
        self.assertEqual(self.fake.calls, [])

    def test_unknown_title_needs_search_and_search_never_guesses(self):
        self.refused("anilist_title_unknown", title="Kura Sample Series 4", progress=1)
        self.assertEqual(self.fake.calls, [])
        self.fake.search_results = [IDS[3]]
        out = self.update(title="Kura Sample Series 4", progress=1, search=True, dry_run=True)
        self.assertEqual((out["anilist_id"], out["resolved"]["via"]), (IDS[3], "search"))
        self.assertIsNone(self.fake.calls[0]["token"])          # the public search carries no token
        self.store.db.execute("INSERT INTO anilist_media(anilist_id,title_romaji,fetched_at) VALUES(?,?,1)",
                              (IDS[3], "Kura Sample Series 4"))  # stored media, not on the viewer's list
        self.store.db.commit()
        error = self.refused("anilist_title_unknown", title="Kura Sample Series", progress=1)
        self.assertEqual(error.details["candidates"], [{"anilist_id": IDS[3], "title": "Kura Sample Series 4"}])
        self.fake.search_results = [IDS[2], IDS[4]]
        error = self.refused("anilist_title_unknown", title="Kura Sample Series", progress=1, search=True)
        self.assertEqual(len(error.details["candidates"]), 2)
        self.fake.media_overrides[IDS[4]] = {**media(IDS[4]), "synonyms": ["Kura Sample Series 3"]}
        self.refused("anilist_title_ambiguous", title="Kura Sample Series 3", progress=1, search=True)

    def test_an_exact_stored_title_beats_a_partial_list_match(self):
        self.fake.entries = [entry(IDS[0], "CURRENT", progress=1)]
        self.sync()
        self.store.db.execute("INSERT INTO anilist_media(anilist_id,title_romaji,fetched_at) VALUES(?,?,1)",
                              (IDS[4], "Sample Series"))
        self.store.db.commit()
        out = self.update(title="Sample Series", progress=2, dry_run=True)
        self.assertEqual((out["anilist_id"], out["resolved"]["via"]), (IDS[4], "media"))

    def test_a_unique_partial_title_resolves_only_from_the_viewers_list(self):
        self.fake.entries = [entry(IDS[0], "CURRENT", progress=1)]
        self.sync()
        out = self.update(title="Sample Series 1", progress=2, dry_run=True)
        self.assertEqual((out["anilist_id"], out["resolved"]), (IDS[0], {"via": "list", "matched": "Kura Sample Series 1"}))

    def test_unknown_media_id(self):
        self.fake.missing_media.add(IDS[0])
        self.refused("anilist_media_not_found", anilist_id=IDS[0], progress=1)

    def test_login_and_expiry_are_required(self):
        self.login(days=-1)
        self.refused("anilist_token_expired", anilist_id=IDS[0], progress=1)
        (self.root / "anilist-auth.json").unlink()
        self.refused("anilist_not_logged_in", anilist_id=IDS[0], progress=1)
        self.assertEqual(self.fake.calls, [])

    def test_arguments_are_checked_before_anything_else(self):
        for kw in ({"progress": 1}, {"anilist_id": IDS[0], "title": "x", "progress": 1}, {"anilist_id": IDS[0]},
                   {"anilist_id": 0, "progress": 1}, {"anilist_id": IDS[0], "progress": -1},
                   {"anilist_id": IDS[0], "score": 11}, {"anilist_id": IDS[0], "score": 8.55},
                   {"anilist_id": IDS[0], "score": float("nan")}, {"anilist_id": IDS[0], "status": "WATCHING"},
                   {"title": "bad\nname", "progress": 1}, {"title": "x" * 201, "progress": 1},
                   {"anilist_id": IDS[0], "progress": 1, "search": True}):
            with self.subTest(kw=kw):
                self.refused("invalid_arguments", **kw)
        self.assertEqual(self.fake.calls, [])

    def test_mutation_declares_only_what_it_sends(self):
        self.assertEqual(anilist_update.save_mutation({"progress": 1}).count("$"), 4)
        text = anilist_update.save_mutation({"status": "CURRENT", "scoreRaw": 5})
        self.assertIn("$status:MediaListStatus", text)
        self.assertNotIn("progress:$progress", text)


if __name__ == "__main__":
    unittest.main()
