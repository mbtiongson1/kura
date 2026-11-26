"""AniList relation cache: crawl, bookkeeping, budgets, failures. No network, invented IDs."""
import unittest

from anilistfake import BASE_ID, FakeAniList, http_error, media as media_row
from kura import anilist_relations as rel
from kura.models import KuraError
from test_anilist_list import Lab
from test_quarantine import Base

A, B, C, D = (BASE_ID + 40 + n for n in range(4))


class Relations(Lab):
    def setUp(self):
        super().setUp()
        self.fake.relations = {A: [(B, "SEQUEL"), (D, "ADAPTATION", "MANGA")],
                               B: [(A, "PREQUEL"), (C, "SEQUEL"), (BASE_ID + 90, "SIDE_STORY")],
                               C: [(B, "PREQUEL")]}

    def sync(self, **kw):
        kw.setdefault("ids", [A])
        return rel.relations_sync(self.store, self.ani(), **kw)

    def edges(self):
        return self.rows("SELECT anilist_id, related_id, relation FROM anilist_relations ORDER BY 1,2,3")

    def test_crawls_the_sequel_chain_and_caches_media(self):
        out = self.sync(max_requests=10)
        self.assertEqual((out["fetched"], out["complete"], out["error"], out["remaining"]), (3, True, None, 0))
        self.assertEqual(self.fake.count("relations{"), 3)            # A, then B, then C
        self.assertIn((A, B, "SEQUEL"), self.edges())
        self.assertIn((B, BASE_ID + 90, "SIDE_STORY"), self.edges())
        self.assertFalse(any(e[1] == D for e in self.edges()))        # manga is not a library entry
        self.assertEqual(self.rows("SELECT status FROM anilist_relation_fetches WHERE anilist_id=?", A), [("ok",)])
        ids = {r[0] for r in self.rows("SELECT anilist_id FROM anilist_media")}
        self.assertTrue({B, C, BASE_ID + 90} <= ids)                  # related media metadata is stored
        self.assertTrue(all(c["token"] is None for c in self.fake.calls))   # public, token-less

    def test_cache_hit_makes_no_request(self):
        self.sync()
        before = len(self.fake.calls)
        out = self.sync()
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual((out["fetched"], out["cached"], out["requests"]["made"]), (0, 3, 0))

    def test_stale_and_refresh_fetch_again(self):
        self.sync()
        self.wall.now += 40 * 86400
        self.assertEqual(self.sync(max_age_d=30)["fetched"], 3)
        self.assertEqual(self.sync(max_age_d=30, refresh=True)["fetched"], 3)

    def test_budget_stops_cleanly_and_resumes(self):
        out = self.sync(max_requests=2)
        self.assertEqual((out["complete"], out["error"], out["fetched"]), (False, "anilist_budget_exhausted", 2))
        self.assertEqual(out["remaining"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_relation_fetches")[0][0], 2)
        done = self.sync(max_requests=2)
        self.assertEqual((done["complete"], done["fetched"], done["cached"]), (True, 1, 2))

    def test_unknown_media_is_recorded_not_found_with_no_edges(self):
        self.fake.missing_media = {A}
        out = self.sync()
        self.assertEqual((out["not_found"], out["fetched"], out["complete"]), ([A], 0, True))
        self.assertEqual(self.rows("SELECT status FROM anilist_relation_fetches"), [("not_found",)])
        self.assertEqual(self.edges(), [])

    def test_refetch_replaces_the_media_edges(self):
        self.sync()
        self.fake.relations[A] = [(C, "SEQUEL")]
        self.wall.now += 40 * 86400
        self.sync()
        self.assertEqual([e for e in self.edges() if e[0] == A], [(A, C, "SEQUEL")])

    def test_transient_failure_after_progress_keeps_what_was_cached(self):
        self.sync(ids=[A], max_requests=1)
        self.fake.queue.append(http_error(500))
        out = self.sync(ids=[A, BASE_ID + 95], max_age_d=30)
        self.assertEqual(out["cached"], 1)
        self.assertEqual(out["error"], "anilist_query_error")
        self.assertIn((A, B, "SEQUEL"), self.edges())

    def test_first_request_failure_raises(self):
        self.fake.queue.append(http_error(500))
        with self.assertRaises(KuraError) as caught:
            self.sync()
        self.assertEqual(caught.exception.details["requests"], {"made": 1, "budget": 10})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_relation_fetches")[0][0], 0)

    def test_rate_limit_records_penalty_and_stops(self):
        self.fake.queue.append(http_error(429, "slow down", **{"Retry-After": "30"}))
        with self.assertRaises(KuraError) as caught:
            self.sync()
        self.assertEqual(caught.exception.code, "anilist_rate_limited")
        self.assertEqual(len(self.fake.calls), 1)

    def test_malformed_item_is_not_proof_of_absence(self):
        self.fake.queue.append(({"data": {"Page": {"media": [{"id": A, "relations": "bad"}]}}},
                                {"X-RateLimit-Limit": "90"}))
        with self.assertRaises(KuraError) as caught:
            self.sync()
        self.assertEqual(caught.exception.code, "anilist_invalid_response")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anilist_relation_fetches")[0][0], 0)

    def test_unexpected_id_in_response_is_refused(self):
        self.fake.queue.append(({"data": {"Page": {"media": [{"id": C, "relations": {"edges": []}}]}}}, {}))
        with self.assertRaises(KuraError):
            self.sync()

    def test_a_rejected_batch_is_halved(self):
        calls = []
        real = self.fake.respond

        def respond(query, variables):
            calls.append(len(variables["ids"]))
            if len(variables["ids"]) > 1:
                raise http_error(400, "Query too complex")
            return real(query, variables)
        self.fake.respond = respond
        out = self.sync(ids=[A, B, C, D])
        self.assertTrue(out["complete"])
        self.assertEqual(calls[:4], [4, 4, 2, 1])

    def test_node_bound_truncates(self):
        old = rel.MAX_NODES
        rel.MAX_NODES = 2
        self.addCleanup(setattr, rel, "MAX_NODES", old)
        out = self.sync()
        self.assertEqual((out["truncated"], out["complete"]), (True, False))

    def test_arguments(self):
        for kw in ({"ids": None}, {"ids": [0]}, {"ids": [A], "library": "x"}, {"max_age_d": -1},
                   {"max_requests": 0}):
            with self.subTest(kw=kw), self.assertRaises(KuraError):
                self.sync(**kw)


class Seeds(Base):
    FILES = [("Kura Saga/Kura Saga - 01.mkv", b"x" * 30000)]

    def test_only_resolved_media_titles_seed_the_crawl(self):
        self.add("Kura Saga", [("Kura Saga - 01.mkv", b"a" * 30000)])
        self.add("Pinned Show", [("Pinned Show - 01.mkv", b"b" * 30000)])
        self.add("Ambiguous Show", [("Ambiguous Show - 01.mkv", b"c" * 30000)])
        self.add("Notes", [("readme.txt", b"d" * 100)])
        self.lab.build()
        db = self.lab.store.db
        for title, i, status, source in (("Kura Saga", A, "resolved", "list"), ("Pinned Show", B, "pinned", "owner"),
                                         ("Ambiguous Show", None, "ambiguous", "list"), ("Notes", D, "resolved", "list")):
            db.execute("INSERT INTO title_identities VALUES ('home',?,?,?,?,NULL,NULL,'[]',1.0)",
                       (f"main/{title}", i, status, source))
        db.commit()
        self.assertEqual(rel.seed_ids(self.lab.store, "home"), [A])
