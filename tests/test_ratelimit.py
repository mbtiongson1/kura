"""Shared cross-process limiter and its AniList wiring. Offline, synthetic, fake clocks."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from kura.anilist import AniList
from kura.models import KuraError
from kura.ratelimit import SharedLimiter
from kura.store import Store

BODY = json.dumps({"data": {"Page": {"media": [{
    "id": 999000001, "title": {"romaji": "Kura Sample Series", "english": None, "native": None},
    "synonyms": []}]}}}).encode()


class Wall:
    """One fake wall clock shared by every limiter in a test; sleeping advances it."""

    def __init__(self, now=1_000_000.0):
        self.now, self.slept = now, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "state"
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.wall = Wall()

    def limiter(self, service="anilist", interval=2.0, **kw):
        return SharedLimiter(self.root, service, interval=interval, wall=self.wall, sleep=self.wall.sleep, **kw)

    def row(self, service="anilist"):
        return self.store.db.execute("SELECT next_ok, interval, penalty_until, observed_limit, remaining, reset_at "
                                     "FROM rate_limits WHERE service=?", (service,)).fetchone()


class Spacing(Base):
    def test_two_instances_share_one_budget(self):
        a, b = self.limiter(), self.limiter()
        self.assertEqual(a.acquire(10), 0.0)
        self.assertEqual(b.acquire(10), 2.0)            # second connection sees the first one's slot
        self.assertEqual(a.acquire(10), 2.0)
        self.assertEqual(self.wall.slept, [2.0, 2.0])

    def test_services_are_independent(self):
        self.assertEqual(self.limiter("anilist").acquire(10), 0.0)
        self.assertEqual(self.limiter("seadex", interval=5.0).acquire(10), 0.0)
        self.assertEqual(self.limiter("seadex", interval=5.0).acquire(10), 5.0)

    def test_idle_time_is_not_banked(self):
        lim = self.limiter()
        lim.acquire(10)
        self.wall.now += 100
        self.assertEqual(lim.acquire(10), 0.0)

    def test_done_counts_the_interval_from_the_request_end(self):
        lim = self.limiter()
        lim.acquire(10)
        self.wall.now += 5.0                            # the request itself took 5 s
        lim.done()
        self.assertEqual(self.limiter().acquire(10), 2.0)

    def test_budget_refusal_reserves_nothing(self):
        lim = self.limiter()
        lim.acquire(10)
        before = self.row()
        with self.assertRaises(KuraError) as caught:
            self.limiter().acquire(1.0)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(caught.exception.details["retry_after"], 2.0)
        self.assertEqual(self.row()[0], before[0])
        self.assertEqual(self.wall.slept, [])
        self.assertEqual(self.limiter().acquire(2.0), 2.0)   # a sufficient budget still works

    def test_negative_or_nan_budget_is_refused(self):
        for bad in (-1, float("nan")):
            with self.assertRaises(KuraError) as caught:
                self.limiter().acquire(bad)
            self.assertEqual(caught.exception.code, "invalid_argument")


class Penalty(Base):
    def test_penalty_recorded_during_the_sleep_is_waited_out(self):
        self.limiter().acquire(10)
        other = self.limiter()
        sleeps = []

        def sleeper(seconds):
            sleeps.append(seconds)
            self.wall.now += seconds
            if len(sleeps) == 1:                        # another process hits a 429 while we sleep
                other.penalize(self.wall.now + 20)

        lim = SharedLimiter(self.root, "anilist", interval=2.0, wall=self.wall, sleep=sleeper)
        self.assertEqual(lim.acquire(60), 22.0)
        self.assertEqual(sleeps, [2.0, 20.0])

    def test_penalty_during_the_sleep_beyond_budget_refuses(self):
        self.limiter().acquire(10)
        other = self.limiter()

        def sleeper(seconds):
            self.wall.now += seconds
            other.penalize(self.wall.now + 500)

        lim = SharedLimiter(self.root, "anilist", interval=2.0, wall=self.wall, sleep=sleeper)
        with self.assertRaises(KuraError) as caught:
            lim.acquire(10)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(caught.exception.details["retry_after"], 500.0)

    def test_penalty_persists_across_instances(self):
        self.limiter().penalize(self.wall.now + 30)
        self.assertEqual(self.limiter().acquire(60), 30.0)
        self.assertEqual(self.limiter().state()["penalty_in_s"], 0.0)

    def test_penalty_is_refused_inside_a_small_budget_and_never_shrinks(self):
        lim = self.limiter()
        lim.penalize(self.wall.now + 30)
        lim.penalize(self.wall.now + 10)                # an earlier deadline cannot shorten it
        with self.assertRaises(KuraError) as caught:
            self.limiter().acquire(5)
        self.assertEqual(caught.exception.details["retry_after"], 30.0)

    def test_penalty_is_capped(self):
        self.limiter(max_penalty=100).penalize(self.wall.now + 10**9)
        self.assertEqual(self.limiter(max_penalty=100).acquire(1000), 100.0)

    def test_absurd_stored_future_is_clamped(self):
        self.limiter().acquire(1)
        self.store.db.execute("UPDATE rate_limits SET next_ok=?, penalty_until=?",
                              (self.wall.now + 10**12, self.wall.now + 10**12))
        self.store.db.commit()
        with self.assertRaises(KuraError) as caught:
            self.limiter(max_penalty=60).acquire(10)
        self.assertEqual(caught.exception.details["retry_after"], 60.0)
        self.assertEqual(self.limiter(max_penalty=60).acquire(100), 60.0)

    def test_garbage_stored_values_are_treated_as_zero(self):
        self.limiter().acquire(1)
        self.store.db.execute("UPDATE rate_limits SET next_ok=-5, penalty_until=-1")
        self.store.db.commit()
        self.assertEqual(self.limiter().acquire(1), 0.0)

    def test_bad_arguments(self):
        for kw in ({"interval": 0}, {"interval": -1}, {"interval": float("inf")},
                   {"interval": 1, "max_penalty": 0}):
            with self.assertRaises(KuraError):
                SharedLimiter(self.root, "anilist", **kw)
        for service in ("", "x" * 33, "bad\nname", "café"):
            with self.assertRaises(KuraError):
                SharedLimiter(self.root, service, interval=1)
        with self.assertRaises(KuraError):
            self.limiter().penalize(float("nan"))
        with self.assertRaises(KuraError):
            self.limiter().observe(limit="90")


class Observe(Base):
    def test_advertised_limit_replaces_the_interval_for_everyone(self):
        self.limiter().observe(limit=90, remaining=80, reset=self.wall.now + 30, interval=60 / 90)
        state = self.limiter().state()
        self.assertEqual((state["observed_limit"], state["remaining"]), (90, 80))
        self.assertAlmostEqual(state["interval"], 60 / 90)
        lim = self.limiter()
        lim.acquire(10)
        self.assertAlmostEqual(self.limiter().acquire(10), 60 / 90)

    def test_without_an_observation_configuration_wins(self):
        self.limiter(interval=2.0).acquire(1)
        self.assertEqual(self.limiter(interval=5.0).state()["interval"], 5.0)

    def test_interval_without_limit_is_ignored(self):
        self.limiter().observe(interval=0.1)
        self.assertEqual(self.limiter().state()["interval"], 2.0)

    def test_a_slower_advertised_interval_pushes_out_the_reserved_slot(self):
        lim = self.limiter()
        lim.acquire(10)                                  # next_ok = now + 2
        lim.observe(limit=10, interval=6.0)              # slower limit than assumed
        self.assertEqual(self.limiter().acquire(10), 6.0)

    def test_state_shape(self):
        lim = self.limiter()
        lim.acquire(10)
        self.assertEqual(lim.state(), {"service": "anilist", "next_ok_in_s": 2.0, "interval": 2.0,
                                       "observed_limit": None, "remaining": None, "penalty_in_s": 0.0})


class Isolation(Base):
    def test_open_caller_transaction_is_untouched(self):
        db = self.store.db
        db.execute("BEGIN")                                      # open caller transaction, no lock held yet
        self.assertTrue(db.in_transaction)
        lim = self.limiter()
        lim.acquire(10)
        lim.penalize(self.wall.now + 1)
        lim.done()
        self.assertTrue(db.in_transaction)                       # still the caller's, not committed by us
        db.execute("INSERT INTO events(at, infohash, state, detail) VALUES (1, 'h', 's', '{}')")
        db.rollback()
        self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.assertIsNotNone(self.row())                         # limiter state survived the rollback

    def test_missing_or_broken_state_fails_closed(self):
        lim = SharedLimiter(self.root / "nowhere", "anilist", interval=1, wall=self.wall, sleep=self.wall.sleep)
        with self.assertRaises(KuraError) as caught:
            lim.acquire(10)
        self.assertEqual(caught.exception.code, "limiter_unavailable")
        self.store.db.execute("DROP TABLE rate_limits")
        self.store.db.commit()
        with self.assertRaises(KuraError) as caught:
            self.limiter().acquire(10)
        self.assertEqual(caught.exception.code, "limiter_unavailable")
        self.assertEqual(self.wall.slept, [])

    def test_symlinked_database_is_refused(self):
        other = self.root / "other"
        (self.root / "state.sqlite3").rename(other)
        (self.root / "state.sqlite3").symlink_to(other)
        with self.assertRaises(KuraError):
            self.limiter().acquire(1)


class AniListWiring(Base):
    def client(self, transport, **kw):
        return AniList(transport=transport, limiter=self.limiter(), sleep=self.wall.sleep, wall=self.wall, **kw)

    def test_default_behaviour_has_no_limiter(self):
        self.assertIsNone(AniList().limiter)

    def test_pacing_is_shared_between_two_clients(self):
        transport = Mock(return_value=(BODY, {}))
        first, second = self.client(transport), self.client(transport)
        self.assertEqual(first.resolve("Kura Sample Series")[1], "resolved")
        self.assertEqual(second.resolve("Kura Sample Series")[1], "resolved")
        self.assertEqual(self.wall.slept, [2.0])

    def test_429_penalises_every_process_and_gives_up_in_budget(self):
        limited = KuraError("alias_unavailable", "limit", status=429, **{"Retry-After": "600"})
        first = self.client(Mock(side_effect=limited), max_wait=30)
        with self.assertRaises(KuraError) as caught:
            first.resolve("Kura Sample Series")
        self.assertEqual(caught.exception.code, "alias_unavailable")
        other_transport = Mock(return_value=(BODY, {}))
        with self.assertRaises(KuraError) as caught:
            self.client(other_transport, max_wait=30).resolve("Kura Sample Series")
        self.assertEqual((caught.exception.code, caught.exception.details.get("status")),
                         ("alias_unavailable", 429))
        other_transport.assert_not_called()                      # a second process makes no request

    def test_retry_after_within_budget_is_waited_then_succeeds(self):
        limited = KuraError("alias_unavailable", "limit", status=429, **{"Retry-After": "7"})
        transport = Mock(side_effect=[limited, (BODY, {})])
        self.assertEqual(self.client(transport).resolve("Kura Sample Series")[1], "resolved")
        self.assertEqual(self.wall.slept, [7.0])

    def test_advertised_headers_are_stored_and_capped_at_90(self):
        headers = {"X-RateLimit-Limit": "500", "X-RateLimit-Remaining": "400",
                   "X-RateLimit-Reset": str(int(self.wall.now) + 60)}
        self.client(Mock(return_value=(BODY, headers))).resolve("Kura Sample Series")
        state = self.limiter().state()
        self.assertEqual((state["observed_limit"], state["remaining"]), (500, 400))
        self.assertAlmostEqual(state["interval"], 60 / 90)

    def test_exhausted_window_blocks_the_next_process(self):
        headers = {"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "0",
                   "X-RateLimit-Reset": str(int(self.wall.now) + 40)}
        self.client(Mock(return_value=(BODY, headers))).resolve("Kura Sample Series")
        self.assertGreaterEqual(self.limiter().state()["penalty_in_s"], 39.0)

    def test_broken_limiter_state_means_no_request(self):
        self.store.db.execute("DROP TABLE rate_limits")
        self.store.db.commit()
        transport = Mock(return_value=(BODY, {}))
        with self.assertRaises(KuraError) as caught:
            self.client(transport).resolve("Kura Sample Series")
        self.assertEqual(caught.exception.code, "alias_unavailable")
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
