"""AniList list, favourites and media sync: snapshots, budgets, shared pacing. No network."""
import json
from pathlib import Path
import tempfile
import unittest

from anilistfake import BASE_ID, CLIENT_ID, VIEWER, FakeAniList, entry, http_error, make_token, tokens_in
from kura import anilist_auth, anilist_list
from kura.anilist import AniList
from kura.models import KuraError
from kura.ratelimit import SharedLimiter
from kura.store import Store
from test_ratelimit import Wall

IDS = [BASE_ID + n for n in range(1, 8)]


class Lab(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "state"
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.wall = Wall(1_800_000_000.0)
        self.fake = FakeAniList()
        self.token = None

    def ani(self, fake=None, **kw):
        limiter = SharedLimiter(self.root, "anilist", interval=2.0, wall=self.wall, sleep=self.wall.sleep)
        return AniList(max_retries=0, limiter=limiter, gql_transport=fake or self.fake, wall=self.wall,
                       sleep=self.wall.sleep, **kw)

    def login(self, days=200):
        self.token = make_token(self.wall.now + days * 86400)
        anilist_auth.save(self.root, anilist_auth.record_for(self.token, CLIENT_ID, VIEWER, self.wall.now))

    def sync(self, **kw):
        return anilist_list.list_sync(self.store, self.ani(), **kw)

    def rows(self, sql, *args):
        return self.store.db.execute(sql, args).fetchall()


class ListSync(Lab):
    def setUp(self):
        super().setUp()
        self.login()
        self.fake.entries = [entry(i, "COMPLETED" if n % 2 else "CURRENT", score=9.5 if n == 1 else 0.0)
                             for n, i in enumerate(IDS[:5], 1)]

    def test_two_chunk_list_with_favourites(self):
        self.fake.per_chunk = 3
        self.fake.favourites = IDS[:2]
        out = self.sync()
        self.assertEqual(self.fake.count("MediaListCollection"), 2)
        self.assertEqual(self.fake.count("User(id"), 1)
        self.assertEqual((out["entries_total"], out["favourites"], out["scored_9_plus"]), (5, 2, 1))
        self.assertEqual(out["counts"], {"CURRENT": 2, "PLANNING": 0, "COMPLETED": 3, "DROPPED": 0,
                                         "PAUSED": 0, "REPEATING": 0})
        self.assertEqual(out["user"], {"id": VIEWER["id"], "name": VIEWER["name"], "viewer": True})
        self.assertEqual((out["from_cache"], out["stale"], out["error"]), (False, False, None))
        self.assertEqual(out["requests"], {"made": 3, "budget": 10})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_media")[0][0], 5)

    def test_second_call_is_served_from_cache_with_no_requests(self):
        self.sync()
        made = len(self.fake.calls)
        out = self.sync(entries=True)
        self.assertEqual(len(self.fake.calls), made)
        self.assertTrue(out["from_cache"])
        self.assertEqual(out["requests"]["made"], 0)
        self.assertEqual(len(out["entries"]), 5)
        self.assertEqual(out["entries"][0]["title_romaji"], "Kura Sample Series 1")
        self.sync(refresh=True)
        self.assertGreater(len(self.fake.calls), made)

    def test_old_snapshot_is_refetched_after_max_age(self):
        self.sync()
        made = len(self.fake.calls)
        self.wall.now += 13 * 3600
        self.assertFalse(self.sync()["from_cache"])
        self.assertGreater(len(self.fake.calls), made)

    def test_custom_lists_are_deduplicated_and_custom_only_entries_kept(self):
        self.fake.custom_only = [entry(IDS[6], "PLANNING")]
        out = self.sync()
        self.assertEqual(out["entries_total"], 6)
        self.assertEqual(out["counts"]["PLANNING"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_list_entries")[0][0], 6)

    def test_score_zero_is_null(self):
        self.sync()
        scores = dict(self.rows("SELECT anilist_id, score FROM anilist_list_entries"))
        self.assertEqual(scores[IDS[0]], 9.5)
        self.assertIsNone(scores[IDS[1]])

    def test_favourites_are_paginated(self):
        self.fake.favourites = [BASE_ID + 500 + n for n in range(30)]
        out = self.sync()
        self.assertEqual(self.fake.count("User(id"), 2)
        self.assertEqual(out["favourites"], 30)
        self.assertEqual(self.rows("SELECT MAX(position) FROM anilist_favourites")[0][0], 29)

    def test_token_only_travels_on_the_authenticated_list_request(self):
        self.fake.favourites = IDS[:1]
        out = self.sync()
        for call in self.fake.calls:
            self.assertEqual(call["token"], self.token if "MediaListCollection" in call["query"] else None)
        self.assertNotIn(self.token, json.dumps(out))

    def test_viewer_is_cached_in_the_token_file_so_no_viewer_request_is_made(self):
        self.sync()
        self.sync(refresh=True)
        self.assertEqual(self.fake.count("Viewer"), 0)
        self.assertEqual(self.rows("SELECT is_viewer FROM anilist_users")[0][0], 1)

    def test_user_option_sends_no_authorization_and_needs_no_login(self):
        anilist_auth.remove(self.root)
        other = {"id": BASE_ID + 900, "name": "OtherKura"}
        self.fake.viewer = other
        out = self.sync(user_name="otherkura")
        self.assertEqual(tokens_in(self.fake), [None] * len(self.fake.calls))
        self.assertEqual(self.fake.calls[0]["variables"].get("userName"), "otherkura")
        self.assertEqual(out["user"], {"id": other["id"], "name": "OtherKura", "viewer": False})
        self.assertTrue(self.sync(user_name="OTHERKURA")["from_cache"])      # case-insensitive cache hit

    def test_user_option_refuses_the_logged_in_account(self):
        self.sync()
        with self.assertRaises(KuraError) as cm:
            self.sync(user_name=VIEWER["name"], refresh=True)
        self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_user_option_refuses_the_logged_in_account_even_when_cached(self):
        self.sync()
        with self.assertRaises(KuraError):
            self.sync(user_name=VIEWER["name"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_viewer_flag_follows_the_account(self):
        self.sync()
        anilist_list.save_viewer(self.store.db, {"id": BASE_ID + 700, "name": "NewKura", "score_format": None}, 1.0)
        self.assertEqual(self.rows("SELECT user_id FROM anilist_users WHERE is_viewer=1"), [(BASE_ID + 700,)])
        self.assertTrue(self.sync(user_name=VIEWER["name"], refresh=True)["user"]["id"] == VIEWER["id"])
        anilist_list.clear_viewer(self.store.db)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_users WHERE is_viewer=1")[0][0], 0)

    def test_a_list_refresh_invalidates_favourite_freshness_in_the_snapshot_transaction(self):
        self.fake.favourites = IDS[:1]
        self.sync()
        user = {"id": VIEWER["id"], "name": VIEWER["name"]}
        anilist_list.save_snapshot(self.store.db, user=user, is_viewer=True, score_format=None,
                                   entries=[], media=[], now=5.0)
        self.assertIsNone(self.rows("SELECT favourites_fetched_at FROM anilist_users")[0][0])

    def test_authenticated_snapshot_never_answers_a_public_lookup_after_logout(self):
        self.sync()
        anilist_auth.remove(self.root)
        anilist_list.clear_viewer(self.store.db)
        made = len(self.fake.calls)
        out = self.sync(user_name=VIEWER["name"], entries=True)         # fresh cache, but private provenance
        self.assertGreater(len(self.fake.calls), made)
        self.assertIsNone(self.fake.calls[made]["token"])
        self.assertFalse(out["from_cache"])
        self.assertEqual(anilist_list.snapshot_authenticated(self.store.db, VIEWER["id"]), False)
        self.assertTrue(self.sync(user_name=VIEWER["name"])["from_cache"])   # now a public snapshot

    def test_stale_fallback_never_serves_a_snapshot_of_the_other_kind(self):
        self.sync()
        anilist_auth.remove(self.root)
        anilist_list.clear_viewer(self.store.db)
        self.fake.queue = [http_error(500)]
        with self.assertRaises(KuraError) as cm:
            self.sync(user_name=VIEWER["name"], refresh=True)
        self.assertEqual(cm.exception.code, "anilist_query_error")

    def test_a_failed_refresh_keeps_the_snapshot_provenance(self):
        self.sync()
        self.fake.queue = [http_error(500)]
        self.assertTrue(self.sync(refresh=True)["stale"])
        self.assertIs(anilist_list.snapshot_authenticated(self.store.db, VIEWER["id"]), True)
        made = len(self.fake.calls)
        self.assertTrue(self.sync(max_age_h=1000)["from_cache"])      # still servable from cache
        self.assertEqual(len(self.fake.calls), made)

    def test_public_snapshot_does_not_answer_an_authenticated_list(self):
        anilist_auth.remove(self.root)
        self.sync(user_name=VIEWER["name"])
        self.login()
        made = len(self.fake.calls)
        self.sync()
        self.assertEqual(self.fake.calls[made]["token"], self.token)

    def test_partial_failure_keeps_the_old_snapshot_and_reports_stale(self):
        self.fake.per_chunk = 3
        self.sync()
        before = self.rows("SELECT anilist_id, status, score, fetched_at FROM anilist_list_entries ORDER BY 1")
        self.fake.entries = self.fake.entries[:1]               # a changed world would have fewer entries
        self.fake.queue = [(self._ok_chunk_one(), {}), http_error(500)]
        out = self.sync(refresh=True)
        self.assertEqual((out["stale"], out["from_cache"], out["error"]), (True, True, "anilist_query_error"))
        self.assertEqual(out["entries_total"], 5)
        self.assertEqual(self.rows("SELECT anilist_id, status, score, fetched_at FROM anilist_list_entries ORDER BY 1"),
                         before)
        self.assertEqual(self.rows("SELECT status FROM sync_state WHERE source LIKE 'anilist:list:%'"),
                         [("error",)])

    def _ok_chunk_one(self):
        return {"data": {"MediaListCollection": {"hasNextChunk": True, "user": dict(VIEWER), "lists": [
            {"isCustomList": False, "entries": [entry(IDS[0])]}]}}}

    def test_failure_without_a_snapshot_raises_with_request_counts(self):
        self.fake.queue = [http_error(500)]
        with self.assertRaises(KuraError) as cm:
            self.sync()
        self.assertEqual(cm.exception.code, "anilist_query_error")
        self.assertEqual(cm.exception.details["requests"], {"made": 1, "budget": 10})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_list_entries")[0][0], 0)

    def test_401_is_auth_invalid_and_never_stale_or_public(self):
        self.sync()
        for failure in (http_error(401), http_error(400, "Invalid token")):
            self.fake.queue = [failure]
            with self.assertRaises(KuraError) as cm:
                self.sync(refresh=True)
            self.assertEqual(cm.exception.code, "anilist_auth_invalid")
            self.assertNotIn(self.token, json.dumps(cm.exception.details) + cm.exception.message)
        self.assertEqual(self.fake.count("MediaListCollection"), 3)     # one try each, no public retry

    def test_errors_in_a_200_body_are_classified(self):
        for status, message, code in ((404, "Private User", "anilist_list_private"),
                                      (404, "Not Found.", "anilist_not_found")):
            self.fake.queue = [({"data": None, "errors": [{"message": message, "status": status}]}, {})]
            with self.assertRaises(KuraError) as cm:
                self.sync(user_name="nobody", refresh=True)
            self.assertEqual(cm.exception.code, code)

    def test_incomplete_responses_never_replace_a_snapshot(self):
        self.sync()
        before = self.rows("SELECT * FROM anilist_list_entries ORDER BY 1")
        base = {"user": dict(VIEWER), "lists": [], "hasNextChunk": False}
        for broken in ({k: v for k, v in base.items() if k != "lists"},
                       {k: v for k, v in base.items() if k != "hasNextChunk"},
                       {**base, "hasNextChunk": "false"},
                       {**base, "lists": [{"isCustomList": False}]},
                       {**base, "lists": [None]},
                       {**base, "lists": [{"isCustomList": False, "entries": [None]}]}):
            self.fake.queue = [({"data": {"MediaListCollection": broken}}, {})]
            out = self.sync(refresh=True)
            self.assertEqual((out["stale"], out["error"]), (True, "anilist_invalid_response"))
        self.assertEqual(self.rows("SELECT * FROM anilist_list_entries ORDER BY 1"), before)

    def test_incomplete_favourites_keep_the_old_ones_and_warn(self):
        self.fake.favourites = IDS[:2]
        self.sync()
        self.fake.entries = self.fake.entries[:2]
        original = self.fake.respond

        def respond(query, variables):
            if "User(id" in query:
                return {"User": {"favourites": {"anime": {"nodes": []}}}}
            return original(query, variables)
        self.fake.respond = respond
        out = self.sync(refresh=True)
        self.assertEqual(out["warnings"], ["anilist_favourites_failed"])
        self.assertEqual(out["favourites"], 2)

    def test_null_favourite_node_keeps_old_favourites(self):
        self.fake.favourites = IDS[:2]
        self.sync()
        original = self.fake.respond
        self.fake.respond = lambda q, v: ({"User": {"favourites": {"anime": {
            "pageInfo": {"hasNextPage": False}, "nodes": [None]}}}} if "User(id" in q else original(q, v))
        out = self.sync(refresh=True)
        self.assertEqual((out["warnings"], out["favourites"]), (["anilist_favourites_failed"], 2))

    def test_forced_refresh_with_failing_favourites_does_not_leave_old_ones_fresh(self):
        self.fake.favourites = IDS[:2]
        self.sync()
        original = self.fake.respond
        self.fake.respond = lambda q, v: (_ for _ in ()).throw(http_error(500)) if "User(id" in q else original(q, v)
        out = self.sync(refresh=True)
        self.assertEqual((out["warnings"], out["favourites"]), (["anilist_favourites_failed"], 2))
        self.fake.respond = original
        made = len(self.fake.calls)
        self.sync()                                         # list fresh, favourites not: exactly one request
        self.assertEqual(len(self.fake.calls) - made, 1)
        self.assertIn("User(id", self.fake.calls[-1]["query"])

    def test_failed_favourites_are_retried_on_the_next_cached_call(self):
        self.fake.favourites = IDS[:2]
        original = self.fake.respond
        self.fake.respond = lambda q, v: (_ for _ in ()).throw(http_error(500)) if "User(id" in q else original(q, v)
        out = self.sync()
        self.assertEqual((out["warnings"], out["favourites"]), (["anilist_favourites_failed"], 0))
        self.fake.respond = original
        made = len(self.fake.calls)
        out = self.sync()                                   # list is fresh; only favourites are fetched
        self.assertEqual(len(self.fake.calls) - made, 1)
        self.assertEqual((out["favourites"], out["warnings"], out["from_cache"]), (2, [], True))
        self.assertEqual(len(self.fake.calls) - made, 1)
        self.assertEqual(self.sync()["requests"]["made"], 0)

    def test_a_429_inside_a_200_body_is_persisted_for_other_processes(self):
        self.fake.queue = [({"data": None, "errors": [{"message": "Too Many Requests.", "status": 429}]},
                            {"Retry-After": "90", "X-RateLimit-Remaining": "0"})]
        with self.assertRaises(KuraError) as cm:
            self.sync()
        self.assertEqual((cm.exception.code, cm.exception.details["retry_after"]), ("anilist_rate_limited", 90))
        penalty = self.rows("SELECT penalty_until FROM rate_limits WHERE service='anilist'")[0][0]
        self.assertGreater(penalty, self.wall.now + 80)

    def test_budget_stops_cleanly_and_reports_progress(self):
        self.fake.per_chunk = 2
        with self.assertRaises(KuraError) as cm:
            self.sync(max_requests=1)
        self.assertEqual(cm.exception.code, "anilist_budget_exhausted")
        self.assertEqual(cm.exception.details["requests"], {"made": 1, "budget": 1})
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_list_entries")[0][0], 0)

    def test_expired_token_stops_before_any_request(self):
        self.login(days=-1)
        with self.assertRaises(KuraError) as cm:
            self.sync()
        self.assertEqual(cm.exception.code, "anilist_token_expired")
        self.assertEqual(self.fake.calls, [])

    def test_expiring_token_warns(self):
        self.login(days=29)
        self.assertEqual(self.sync()["warnings"], ["anilist_token_expires_soon"])

    def test_not_logged_in(self):
        anilist_auth.remove(self.root)
        with self.assertRaises(KuraError) as cm:
            self.sync()
        self.assertEqual(cm.exception.code, "anilist_not_logged_in")

    def test_argument_validation(self):
        for kw in ({"max_requests": 0}, {"max_requests": 101}, {"max_age_h": -1}, {"max_age_h": float("nan")},
                   {"user_name": "no spaces!"}):
            with self.assertRaises(KuraError) as cm:
                self.sync(**kw)
            self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_hostile_values_are_sanitized_not_stored(self):
        weird = entry(IDS[6], "PAUSED", score=99)
        weird["media"]["title"]["romaji"] = "x" * 500
        weird["media"]["format"] = "tv; DROP"
        self.fake.entries = [weird]
        self.sync()
        self.assertEqual(self.rows("SELECT anilist_id, score FROM anilist_list_entries"), [(IDS[6], None)])
        self.assertEqual(self.rows("SELECT title_romaji, format FROM anilist_media"), [(None, None)])

    def test_an_unreadable_entry_keeps_the_previous_snapshot(self):
        self.sync()
        before = self.rows("SELECT * FROM anilist_list_entries ORDER BY 1")
        for bad in (entry(IDS[5], "WATCHING"), {**entry(IDS[5]), "mediaId": None}):
            self.fake.entries = [bad]
            out = self.sync(refresh=True)
            self.assertEqual((out["stale"], out["error"]), (True, "anilist_invalid_response"))
        self.assertEqual(self.rows("SELECT * FROM anilist_list_entries ORDER BY 1"), before)


class MediaSync(Lab):
    def test_120_ids_take_three_requests(self):
        ids = [BASE_ID + 1000 + n for n in range(120)]
        out = anilist_list.media_sync(self.store, self.ani(), ids=ids)
        self.assertEqual([len(c["variables"]["ids"]) for c in self.fake.calls], [50, 50, 20])
        self.assertEqual((out["requested"], out["fetched"], out["complete"], out["requests"]["made"]),
                         (120, 120, True, 3))
        self.assertEqual(tokens_in(self.fake), [None] * 3)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_media")[0][0], 120)

    def test_400_halves_the_batch_down_to_five(self):
        self.fake.bad_request_over = 12
        ids = [BASE_ID + 1000 + n for n in range(60)]
        out = anilist_list.media_sync(self.store, self.ani(), ids=ids, max_requests=20)
        sizes = [len(c["variables"]["ids"]) for c in self.fake.calls]
        self.assertEqual(sizes[:4], [50, 25, 12, 12])
        self.assertTrue(out["complete"])
        self.assertEqual(out["fetched"], 60)

    def test_persistent_400_stops_at_the_minimum_batch(self):
        self.fake.bad_request_over = 0
        with self.assertRaises(KuraError) as cm:
            anilist_list.media_sync(self.store, self.ani(), ids=[BASE_ID + n for n in range(1, 40)], max_requests=20)
        self.assertEqual(cm.exception.code, "anilist_bad_request")
        self.assertEqual([len(c["variables"]["ids"]) for c in self.fake.calls], [39, 25, 12, 6, 5])

    def test_missing_filter_not_found_and_library_ids(self):
        have, gone, fresh = BASE_ID + 1, BASE_ID + 2, BASE_ID + 3
        self.fake.missing_media = {gone}
        anilist_list.media_sync(self.store, self.ani(), ids=[have])
        self.store.db.execute(
            "INSERT INTO title_identities(library,title,anilist_id,status,source,updated_at) VALUES "
            "('lib','a/One',?,'resolved','alias',0),('lib','a/Two',?,'resolved','alias',0),"
            "('lib','a/Three',?,'resolved','alias',0),('lib','a/Four',NULL,'unresolved','alias',0),"
            "('other','b/Five',?,'resolved','alias',0)", (have, gone, fresh, BASE_ID + 9))
        self.store.db.commit()
        self.fake.calls.clear()
        out = anilist_list.media_sync(self.store, self.ani(), library="lib", missing=True)
        self.assertEqual(self.fake.calls[0]["variables"]["ids"], [gone, fresh])
        self.assertEqual((out["fetched"], out["not_found"]), (1, [gone]))

    def test_partial_progress_is_reported_not_raised(self):
        ids = [BASE_ID + 1000 + n for n in range(120)]
        out = anilist_list.media_sync(self.store, self.ani(), ids=ids, max_requests=2)
        self.assertEqual((out["fetched"], out["remaining"], out["complete"], out["error"]),
                         (100, 20, False, "anilist_budget_exhausted"))

    def test_id_arguments_are_validated(self):
        for kw in ({"ids": [0]}, {"ids": [10**9]}, {}, {"ids": [1], "library": "x"}):
            with self.assertRaises(KuraError) as cm:
                anilist_list.media_sync(self.store, self.ani(), **kw)
            self.assertEqual(cm.exception.code, "invalid_arguments")


class Limits(Lab):
    def test_exactly_one_cheap_request_reports_headers(self):
        self.fake.headers = {"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "88", "X-RateLimit-Reset": "123"}
        out = anilist_list.limits(self.ani())
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(tokens_in(self.fake), [None])
        self.assertEqual((out["limit"], out["remaining"], out["reset"]), (90.0, 88.0, 123.0))
        self.assertEqual(out["rate_limit"]["observed_limit"], 90)
        self.assertEqual(out["requests"], {"made": 1, "budget": 1})


class SharedPacing(Lab):
    def test_two_clients_with_separate_connections_respect_each_other(self):
        self.fake.headers = {}
        a, b = self.ani(), self.ani()
        a.query("{GenreCollection}", {})
        b.query("{GenreCollection}", {})
        a.query("{GenreCollection}", {})
        self.assertEqual(self.wall.slept, [2.0, 2.0])

    def test_429_retry_after_is_persisted_for_other_processes(self):
        self.fake.queue = [http_error(429, **{"Retry-After": "120", "X-RateLimit-Remaining": "0"})]
        with self.assertRaises(KuraError) as cm:
            self.ani().query("{GenreCollection}", {})
        self.assertEqual((cm.exception.code, cm.exception.details["retry_after"]), ("anilist_rate_limited", 120))
        self.assertEqual(len(self.fake.calls), 1)
        other = self.ani()
        with self.assertRaises(KuraError) as cm:
            other.query("{GenreCollection}", {})
        self.assertEqual(cm.exception.code, "anilist_rate_limited")
        self.assertGreater(cm.exception.details["retry_after"], 100)
        self.assertEqual(len(self.fake.calls), 1)               # refused without a request
        self.assertEqual(self.wall.slept, [])
        row = self.rows("SELECT penalty_until FROM rate_limits WHERE service='anilist'")[0][0]
        self.assertAlmostEqual(row, self.wall.now + 120, delta=1)

    def test_advertised_limit_90_sets_interval(self):
        self.ani().query("{GenreCollection}", {})
        interval = self.rows("SELECT interval FROM rate_limits WHERE service='anilist'")[0][0]
        self.assertAlmostEqual(interval, 60 / 90, places=3)

    def test_remaining_zero_waits_for_the_reset(self):
        reset = self.wall.now + 10
        self.fake.queue = [({"data": {"GenreCollection": []}},
                            {"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "0",
                             "X-RateLimit-Reset": str(reset)})]
        ani = self.ani()
        ani.query("{GenreCollection}", {})
        ani.query("{GenreCollection}", {})
        self.assertGreaterEqual(sum(self.wall.slept), 10 - 1)
        self.assertGreaterEqual(self.wall.now, reset)

    def test_a_closed_window_longer_than_max_wait_refuses_without_a_request(self):
        self.fake.queue = [({"data": {"GenreCollection": []}},
                            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(self.wall.now + 500)})]
        ani = self.ani()
        ani.query("{GenreCollection}", {})
        with self.assertRaises(KuraError) as cm:
            ani.query("{GenreCollection}", {})
        self.assertEqual(cm.exception.code, "anilist_rate_limited")
        self.assertEqual(len(self.fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
