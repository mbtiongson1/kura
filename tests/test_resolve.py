"""Candidate resolution: term derivation, ranking, pacing, resume, safety. Offline, synthetic."""
import json
import os
from pathlib import Path
import stat
import unittest
import urllib.parse
from unittest.mock import patch

import torrentgen
from kura import manifest, provider, resolve
from kura.anilist import Identity
from kura.models import KuraError
from test_cli_handover import run
from test_manifest import GOOD, Lab, snapshot

P = torrentgen.payload
FOLDER = "[GroupX] Kura Sample Series [1080p HEVC] (Batch)"
NS = provider.NS


def item(title, nyaa_id, infohash, size="48.8 KiB", seeders=5, category="1_2"):
    return (f"<item><title>{title}</title><link>https://nyaa.si/download/{nyaa_id}.torrent</link>"
            f"<nyaa:seeders>{seeders}</nyaa:seeders><nyaa:infoHash>{infohash}</nyaa:infoHash>"
            f"<nyaa:categoryId>{category}</nyaa:categoryId><nyaa:size>{size}</nyaa:size></item>")


def feed(*items):
    return ('<?xml version="1.0" encoding="utf-8"?><rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa">'
            "<channel>" + "".join(items) + "</channel></rss>").encode()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeNyaa:
    """Stands in for ``provider.fetch``. Records call times on the shared fake clock."""

    def __init__(self, clock, feeds=None, descriptors=None):
        self.clock, self.feeds, self.descriptors = clock, feeds or {}, descriptors or {}
        self.calls, self.errors = [], []     # errors: queue of exceptions for the next calls

    def __call__(self, url, limit, descriptor=False):
        self.calls.append((self.clock.now, url, limit, descriptor))
        self.clock.now += 0.5                  # a request takes time
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        parts = urllib.parse.urlsplit(url)
        if descriptor:
            data = self.descriptors.get(parts.path.rsplit("/", 1)[1].split(".")[0])
            if data is None:
                raise KuraError("provider_unavailable", "gone", status=404)
            return data
        query = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
        return self.feeds.get(query, feed())

    @property
    def searches(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["q"][0]
                for _, u, _, d in self.calls if not d]

    @property
    def fetched(self):
        return [u for _, u, _, d in self.calls if d]


def http_error(status):
    return KuraError("provider_unavailable", "Nyaa request failed; retry later", status=status)


class Terms(unittest.TestCase):
    def test_derivation_table(self):
        table = [
            (FOLDER, ["Kura Sample Series"], ["GroupX"], None),
            ("Kura Sample Series S02 (BD 1080p x265)", ["Kura Sample Series S02", "Kura Sample Series"], [], 2),
            ("Kura.Sample.Series.S01.1080p.BluRay.x264-GroupZ", ["Kura Sample Series S01", "Kura Sample Series"], [], 1),
            ("Kura Sample Series 2nd Season", ["Kura Sample Series S02", "Kura Sample Series"], [], 2),
            ("Kura Sample Series Season 3 [Dual Audio]", ["Kura Sample Series S03", "Kura Sample Series"], [], 3),
            ("[GroupX] Kura Sample Series - S01-S03 (Complete)", ["Kura Sample Series"], ["GroupX"], None),
            ("Kura Sample Movie (2020) [BD 1080p]", ["Kura Sample Movie"], [], None),
            ("Kura_Sample_Series_[GroupX]_[720p].mkv", ["Kura Sample Series"], [], None),
            ("[GroupX] [1080p] Kura Sample Series [ABCD1234]", ["Kura Sample Series"], ["GroupX"], None),
            ("[1080p] Kura Sample Series", ["Kura Sample Series"], [], None),
            ("[abcdef12] Kura Sample Series", ["Kura Sample Series"], [], None),
            ("Kura's Sample: Series!", ["Kuras Sample Series"], [], None),
            ("Kura Sample Series WEB-DL AAC2.0 10bit v2", ["Kura Sample Series"], [], None),
            ("[GroupX]", [], ["GroupX"], None),
            ("", [], [], None),
        ]
        for entry, queries, groups, season in table:
            with self.subTest(entry=entry):
                terms = resolve.derive_terms(entry)
                self.assertEqual((list(terms.queries), list(terms.groups), terms.season),
                                 (queries, groups, season))

    def test_aliases_and_file_groups(self):
        terms = resolve.derive_terms("Kura Sample Series S02",
                                     ["Kura Example Saga", "Third Alias", "Fourth Alias", "kura sample series"])
        self.assertEqual(list(terms.queries), ["Kura Sample Series S02", "Kura Sample Series",
                                               "Kura Example Saga S02", "Kura Example Saga"])
        self.assertEqual(list(terms.names), ["kura sample series", "kura example saga", "third alias"])
        files = ["[GroupY] Kura - 01.mkv", "[GroupY] Kura - 02.mkv", "Kura - 03.mkv"]
        self.assertEqual(list(resolve.derive_terms("Kura Sample Series", (), files).groups), ["GroupY"])
        self.assertEqual(list(resolve.derive_terms("Kura Sample Series", (), ["a.mkv", "b.mkv"]).groups), [])
        long = resolve.derive_terms("word " * 100)
        self.assertLessEqual(len(long.queries[0]), 120)


class Parsing(unittest.TestCase):
    H = "a" * 40

    def test_batches_are_accepted_unlike_acquisition(self):
        data = feed(item(FOLDER, 11, self.H), item("Kura Sample Series - 01-12 (Batch)", 12, "b" * 40),
                    item("Kura Sample Series S01E05", 13, "c" * 40, category="1_3"))
        rows = resolve.parse_candidates(data)
        self.assertEqual([r.nyaa_id for r in rows], ["11", "12", "13"])
        self.assertEqual(rows[0].group, "GroupX")
        self.assertEqual(provider.parse_rss(data), [])          # the acquisition parser still declines

    def test_unsafe_or_malformed_items_are_dropped(self):
        bad = [
            item("t", 11, "A" * 39),                                         # short infohash
            item("t", 11, self.H, category="1_1"),                           # other category
            item("t", 11, self.H, size="12 parsecs"),
            item("t", 11, self.H, size="0 B"),
            item("t", 11, self.H, seeders=-1),
            item("t\tx", 11, self.H),
            item("t", 11, self.H).replace("https://nyaa.si/download/11.torrent", "https://evil.example/download/11.torrent"),
            item("t", 11, self.H).replace("download/11.torrent", "download/11.torrent?passkey=1"),
        ]
        self.assertEqual(resolve.parse_candidates(feed(*bad)), [])
        for data in (b"<!DOCTYPE x><rss/>", b"\xff\xfe", b"<rss", b"<rss>\x00</rss>",
                     b"<!ENTITY a 'b'>"):
            with self.assertRaises(KuraError):
                resolve.parse_candidates(data)

    def test_displayed_size_rounding_is_known(self):
        row = resolve.parse_candidates(feed(item("t", 11, self.H, size="1.2 GiB")))[0]
        self.assertEqual(row.size_bytes, int(1.2 * 1024**3))
        self.assertAlmostEqual(row.slack, 0.05 * 1024**3)
        whole = resolve.parse_candidates(feed(item("t", 11, self.H, size="700 MiB")))[0]
        self.assertAlmostEqual(whole.slack, 0.5 * 1024**2)

    def test_duplicate_infohash_and_item_cap(self):
        many = [item(f"Kura {i}", i + 1, f"{i:040x}") for i in range(150)]
        self.assertEqual(len(resolve.parse_candidates(feed(*many))), 100)
        two = feed(item("one", 11, self.H), item("two", 12, self.H))
        self.assertEqual([r.title for r in resolve.parse_candidates(two)], ["one"])


class Ranking(unittest.TestCase):
    LOCAL = 10 * 1024**3

    def cand(self, title, size, nyaa_id=1, seeders=5, group=None, slack=0.0, infohash=None):
        return resolve.Candidate(str(nyaa_id), title, int(size), slack, seeders,
                                 infohash or f"{nyaa_id:040x}", "https://nyaa.si/download/1.torrent", group)

    def rank(self, rows, **kw):
        kw.setdefault("names", ("kura sample series",))
        kw.setdefault("groups", ())
        return resolve.rank(rows, local_bytes=self.LOCAL, **kw)

    def test_size_window_and_relevance(self):
        rows = [self.cand("Kura Sample Series", self.LOCAL * 1.019, 1),
                self.cand("Kura Sample Series", self.LOCAL * 1.021, 2),
                self.cand("Kura Sample Series", self.LOCAL * 0.5, 3),
                self.cand("Other Show", self.LOCAL, 4),
                self.cand("Kura Sample Series Extra Cut", self.LOCAL, 5)]
        out = self.rank(rows)
        self.assertEqual([r["candidate"].nyaa_id for r in out], ["5", "1"])
        self.assertEqual(self.rank(rows, names=()), [])
        self.assertEqual(resolve.rank(rows, local_bytes=0, names=("kura",), groups=()), [])

    def test_displayed_rounding_widens_the_window(self):
        local = int(1.2 * 1024**3) - 50 * 1024**2          # the real size is 4% below "1.2 GiB"
        shown = resolve.parse_candidates(feed(item("Kura Sample Series", 11, "a" * 40, size="1.2 GiB")))
        self.assertEqual(len(resolve.rank(shown, local_bytes=local, names=("kura sample series",), groups=())), 1)
        self.assertEqual(resolve.rank(shown, local_bytes=local - 200 * 1024**2,
                                      names=("kura sample series",), groups=()), [])

    def test_order_size_name_group_seeders(self):
        entry = "Kura Sample Series"
        rows = [self.cand("Kura Sample Series", self.LOCAL * 1.015, 1, seeders=900, group="GroupX"),   # close, not exact
                self.cand("[GroupX] Kura Sample Series (Batch)", self.LOCAL, 2, seeders=1, group="GroupX"),
                self.cand("[GroupY] Kura Sample Series (Batch)", self.LOCAL, 3, seeders=50, group="GroupY"),
                self.cand("[GroupZ] Kura Sample Series (Batch)", self.LOCAL, 4, seeders=60, group="GroupZ"),
                self.cand("Kura Sample Series", self.LOCAL, 5, seeders=0)]
        out = self.rank(rows, groups=("GroupX",), entry=entry)
        self.assertEqual([r["candidate"].nyaa_id for r in out], ["5", "2", "4", "3", "1"])
        self.assertTrue(out[0]["name_match"] and out[1]["group_match"])
        again = self.rank(list(reversed(rows)), groups=("groupx",), entry=entry)
        self.assertEqual([r["candidate"].nyaa_id for r in again], ["5", "2", "4", "3", "1"])


class Pacing(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def pacer(self, fetcher, **kw):
        return resolve.Pacer(fetcher, sleep=self.clock.sleep, clock=self.clock, **kw)

    def test_requests_are_spaced_from_the_end_of_the_previous_one(self):
        nyaa = FakeNyaa(self.clock)
        pacer = self.pacer(nyaa)
        for _ in range(4):
            pacer.get("https://nyaa.si/?q=x", 10)
        times = [t for t, *_ in nyaa.calls]
        self.assertEqual(times[0], 1000.0)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, 2.0 + 0.5)       # 0.5 s request + 2 s interval
        self.assertEqual(pacer.requests, 4)

    def test_backoff_on_429_and_5xx_then_success(self):
        nyaa = FakeNyaa(self.clock)
        nyaa.errors = [http_error(429), http_error(503)]
        pacer = self.pacer(nyaa)
        pacer.get("https://nyaa.si/?q=x", 10)
        times = [t for t, *_ in nyaa.calls]
        self.assertEqual(len(times), 3)
        self.assertGreaterEqual(times[1] - times[0], 4.0)               # first backoff
        self.assertGreaterEqual(times[2] - times[1], 8.0)               # doubled
        self.assertEqual((pacer.retries, pacer.failures), (2, 0))

    def test_network_error_is_retried_but_client_errors_are_not(self):
        nyaa = FakeNyaa(self.clock)
        nyaa.errors = [KuraError("provider_unavailable", "x"), None]
        self.pacer(nyaa).get("u", 10)
        self.assertEqual(len(nyaa.calls), 2)
        nyaa = FakeNyaa(self.clock)
        nyaa.errors = [http_error(404), None]
        pacer = self.pacer(nyaa)
        with self.assertRaises(KuraError):
            pacer.get("u", 10)
        self.assertEqual((len(nyaa.calls), pacer.failures), (1, 0))
        nyaa.errors = [KuraError("unsafe_redirect", "no")]
        with self.assertRaises(KuraError):
            pacer.get("u", 10)
        self.assertEqual(len(nyaa.calls), 2)

    def test_exhausted_retries_back_off_and_trip_the_breaker(self):
        nyaa = FakeNyaa(self.clock)
        nyaa.errors = [http_error(429)] * 20
        pacer = self.pacer(nyaa)
        for _ in range(3):
            with self.assertRaises(KuraError):
                pacer.get("u", 10)
        self.assertEqual(len(nyaa.calls), 9)                            # 1 try + 2 retries, three times
        self.assertTrue(pacer.tripped)
        self.assertGreaterEqual(self.clock.now - 1000.0, 3 * (4 + 8) + 8)

    def test_budget_stops_before_a_request_that_would_overrun(self):
        nyaa = FakeNyaa(self.clock)
        pacer = self.pacer(nyaa, budget=5.0)
        pacer.get("u", 10)                      # t=1000, ends 1000.5, next at 1002.5
        pacer.get("u", 10)                      # t=1002.5 -> 1003.0, next at 1005.0 (= budget edge)
        with self.assertRaises(resolve.BudgetExhausted):
            pacer.get("u", 10)
        self.assertEqual(len(nyaa.calls), 2)


class Fixture:
    """A library with one unmatched batch folder, a built manifest and a fake Nyaa."""

    def setUp(self):
        self.lab = Lab(self)
        self.clock = FakeClock()
        self.wall = [1_700_000_000.0]
        self.right = self.lab.add(FOLDER, GOOD, torrent=False, announce="http://tracker.example/announce")
        self.decoy = torrentgen.build(FOLDER, [("ep01.mkv", P("x", 30000)), ("ep02.mkv", P("y", 20000))],
                                      announce="http://tracker.example/announce")
        self.out = self.lab.tmp / "out"
        self.out.mkdir()
        self.lab.build()                      # empty torrents dir: the title is "unmatched"
        self.nyaa = FakeNyaa(self.clock)

    def feed_for(self, *items):
        self.nyaa.feeds["Kura Sample Series"] = feed(*items)

    def resolve(self, **kw):
        kw.setdefault("budget_seconds", 3600.0)
        return resolve.resolve(self.lab.store, library="home", torrents_out=self.out, fetcher=self.nyaa,
                               sleep=self.clock.sleep, clock=self.clock, wall=lambda: self.wall[0], **kw)

    def names(self):
        return sorted(p.name for p in self.out.iterdir())


class EndToEnd(Fixture, unittest.TestCase):
    def test_resolve_then_build_verifies_the_right_candidate_and_rejects_the_decoy(self):
        total = len(self.right.content)
        self.assertEqual(total, 50000)
        self.feed_for(item(FOLDER, 11, self.right.infohash, seeders=3),
                      item("[GroupY] Kura Sample Series [1080p] (Batch)", 12, self.decoy.infohash, seeders=99),
                      item("Kura Sample Series S01E01", 13, "d" * 40, size="9.0 MiB"))
        self.nyaa.descriptors = {"11": self.right.data, "12": self.decoy.data}
        result = self.resolve()
        self.assertEqual((result["processed"], result["outcomes"], result["descriptors_written"]),
                         (1, {"written": 1}, 2))
        self.assertEqual(self.names(), sorted([self.right.infohash + ".torrent", self.decoy.infohash + ".torrent",
                                               ".kura-resolve.json", ".kura-resolve.lock"]))
        first = result["titles"][0]["candidates"]
        self.assertEqual([c["infohash"] for c in first], [self.right.infohash, self.decoy.infohash])
        self.assertTrue(first[0]["name_match"] and first[0]["file_count_match"])
        self.assertTrue(result["next"].startswith("run: kura manifest build"))
        self.assertEqual(len(self.nyaa.fetched), 2)       # the unrelated single episode was never fetched
        self.assertLessEqual(self.lab.status(FOLDER)["bytes"], total)
        self.assertEqual(self.lab.status(FOLDER)["status"], "unmatched")   # resolve alone proves nothing

        built = manifest.build(self.lab.store, roots={"main": self.lab.lib}, library="home",
                               torrents_dir=self.out, policy=self.lab.policy())
        self.assertEqual(built["torrents"]["parsed"], 2)
        row = self.lab.status(FOLDER)
        self.assertEqual(row["status"], "reacquirable")
        verdicts = {t["infohash"]: t["verdict"] for t in row["torrents"]}
        self.assertEqual(verdicts, {self.right.infohash: "complete", self.decoy.infohash: "failed"})

    def test_decoy_alone_never_makes_a_title_reacquirable(self):
        self.feed_for(item(FOLDER, 12, self.decoy.infohash))
        self.nyaa.descriptors = {"12": self.decoy.data}
        self.resolve()
        manifest.build(self.lab.store, roots={"main": self.lab.lib}, library="home",
                       torrents_dir=self.out, policy=self.lab.policy())
        self.assertNotEqual(self.lab.status(FOLDER)["status"], "reacquirable")

    def test_only_unmatched_titles_are_searched(self):
        done = self.lab.add("Kura Done Series", GOOD, announce="http://tracker.example/announce")
        self.lab.build()
        self.assertEqual(self.lab.status("Kura Done Series")["status"], "reacquirable")
        result = self.resolve()
        self.assertEqual(self.nyaa.searches, ["Kura Sample Series"])
        self.assertEqual(result["skipped"]["not_unmatched"], 1)
        self.assertEqual(done.infohash in "".join(self.names()), False)
        named = self.resolve(titles=["main/Kura Done Series", "main/Nope"], refresh=True)
        self.assertEqual(named["processed"], 0)
        self.assertEqual({n["skipped"] for n in named["notes"]}, {"status_reacquirable", "not_in_manifest"})
        self.assertEqual(self.nyaa.searches, ["Kura Sample Series"])      # still nothing new

    def test_discards_private_unhashable_and_credential_bearing_descriptors(self):
        private = torrentgen.build(FOLDER, GOOD, private=True,
                                   announce="http://tracker.example/announce?passkey=SECRETKEY")
        keyed = torrentgen.build(FOLDER, [("ep01.mkv", P("k", 30000)), ("ep02.mkv", P("l", 20000))],
                                 announce="http://tracker.example/SECRETKEY/announce")
        other = torrentgen.build("Another Name", GOOD)
        self.feed_for(item("A", 21, private.infohash), item("B", 22, keyed.infohash),
                      item("C", 23, "e" * 40), item("D", 24, "f" * 40))
        for n in ("A", "B", "C", "D"):                       # make every title relevant
            self.nyaa.feeds["Kura Sample Series"] = self.nyaa.feeds["Kura Sample Series"].replace(
                f"<title>{n}</title>".encode(), b"<title>Kura Sample Series Batch</title>")
        self.nyaa.descriptors = {"21": private.data, "22": keyed.data, "23": other.data, "24": b"not bencode"}
        result = self.resolve(max_candidates=4)
        outcomes = sorted(c["outcome"] for c in result["titles"][0]["candidates"])
        self.assertEqual(outcomes, ["discarded:announce_not_public", "discarded:hash_mismatch",
                                    "discarded:invalid_torrent", "discarded:private"])
        self.assertEqual(result["outcomes"], {"discarded": 1})
        self.assertEqual(self.names(), [".kura-resolve.json", ".kura-resolve.lock"])
        text = json.dumps(result)
        for secret in ("SECRETKEY", "passkey", "tracker.example", "http://", "udp://"):
            self.assertNotIn(secret, text)

    def test_max_candidates_bounds_descriptor_fetches(self):
        items, descriptors = [], {}
        for i in range(5):
            built = torrentgen.build(FOLDER, [("ep01.mkv", P(f"m{i}", 30000)), ("ep02.mkv", P(f"n{i}", 20000))],
                                     announce="http://tracker.example/announce")
            items.append(item(FOLDER, 30 + i, built.infohash, seeders=i))
            descriptors[str(30 + i)] = built.data
        self.feed_for(*items)
        self.nyaa.descriptors = descriptors
        result = self.resolve(max_candidates=2)
        self.assertEqual(len(self.nyaa.fetched), 2)
        self.assertEqual(result["descriptors_written"], 2)
        # same title, so the most-seeded two win
        self.assertEqual([c["seeders"] for c in result["titles"][0]["candidates"]], [4, 3])
        self.assertEqual(self.nyaa.searches, ["Kura Sample Series"])      # enough fit: no second query

    def test_no_results_and_no_candidates_are_recorded(self):
        result = self.resolve()
        self.assertEqual(result["outcomes"], {"no_results": 1})
        self.assertEqual(len(self.nyaa.searches), 1)
        self.feed_for(item("Kura Sample Series", 11, self.right.infohash, size="900 MiB"))
        self.assertEqual(self.resolve(refresh=True)["outcomes"], {"no_candidates": 1})

    def test_anilist_titles_widen_the_search_and_are_cached(self):
        calls = []

        class Aliases:
            def resolve(self, title):
                calls.append(title)
                return Identity(7, ("Kura Example Saga", "Kura Sample Series"), "Kura Sample Series"), "resolved"

        self.nyaa.feeds["Kura Example Saga"] = feed(item(FOLDER.replace("Sample Series", "Example Saga"), 11,
                                                         self.right.infohash))
        self.nyaa.descriptors = {"11": self.right.data}
        result = self.resolve(aliases=Aliases())
        self.assertEqual(self.nyaa.searches, ["Kura Sample Series", "Kura Example Saga"])
        self.assertEqual(result["titles"][0]["anilist"], "resolved")
        self.assertEqual(result["descriptors_written"], 1)
        self.resolve(aliases=Aliases(), refresh=True)
        self.assertEqual(calls, ["Kura Sample Series"])                    # second lookup came from the cache

    def test_anilist_failure_degrades_to_the_folder_name(self):
        class Down:
            def resolve(self, title):
                raise KuraError("alias_unavailable", "down")

        result = self.resolve(aliases=Down())
        self.assertEqual(result["titles"][0]["anilist"], "unavailable")
        self.assertEqual(self.nyaa.searches, ["Kura Sample Series"])


class Resume(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.feed_for(item(FOLDER, 11, self.right.infohash))
        self.nyaa.descriptors = {"11": self.right.data}

    def test_attempted_titles_are_skipped_for_seven_days_unless_refreshed(self):
        self.assertEqual(self.resolve()["outcomes"], {"written": 1})
        made = len(self.nyaa.calls)
        again = self.resolve()
        self.assertEqual((again["processed"], again["skipped"]["attempted_recently"]), (0, 1))
        self.assertEqual(len(self.nyaa.calls), made)
        self.wall[0] += 6.9 * 24 * 3600
        self.assertEqual(self.resolve()["processed"], 0)
        refreshed = self.resolve(refresh=True)
        self.assertEqual((refreshed["processed"], refreshed["descriptors_present"]), (1, 1))
        self.assertGreater(len(self.nyaa.calls), made)
        self.wall[0] += 8 * 24 * 3600
        made = len(self.nyaa.calls)
        self.assertEqual(self.resolve()["processed"], 1)
        self.assertGreater(len(self.nyaa.calls), made)

    def test_future_timestamp_does_not_block_forever(self):
        self.resolve()
        ledger = json.loads((self.out / ".kura-resolve.json").read_text())
        ledger["attempts"]["home"]["main/" + FOLDER]["at"] = self.wall[0] + 10**7
        (self.out / ".kura-resolve.json").write_text(json.dumps(ledger))
        self.assertEqual(self.resolve()["processed"], 1)

    def test_budget_leaves_unfinished_titles_for_the_next_run(self):
        self.lab.add("Kura Second Series", GOOD, torrent=False)
        self.lab.build()
        self.nyaa.feeds["Kura Second Series"] = feed()
        result = self.resolve(budget_seconds=5.0)
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["pending"], 1)
        done = json.loads((self.out / ".kura-resolve.json").read_text())["attempts"]["home"]
        self.assertEqual(len(done), result["processed"])
        rest = self.resolve()
        self.assertEqual(rest["processed"], 2 - result["processed"])
        self.assertFalse(rest["budget_exhausted"])
        again = self.resolve()
        self.assertEqual(again["processed"], 0)
        self.assertEqual(again["skipped"]["attempted_recently"], 2)

    def test_max_titles_and_title_filter(self):
        self.lab.add("Kura Second Series", GOOD, torrent=False)
        self.lab.build()
        self.nyaa.feeds["Kura Second Series"] = feed()
        one = self.resolve(max_titles=1)
        self.assertEqual((one["processed"], one["pending"]), (1, 0))
        only = self.resolve(titles=["main/Kura Second Series"], refresh=True)
        self.assertEqual([t["title"] for t in only["titles"]], ["main/Kura Second Series"])

    def test_network_failures_are_not_recorded_and_stop_the_run(self):
        self.lab.add("Kura Second Series", GOOD, torrent=False)
        self.lab.add("Kura Third Series", GOOD, torrent=False)
        self.lab.add("Kura Fourth Series", GOOD, torrent=False)
        self.lab.build()
        self.nyaa.errors = [http_error(503)] * 100
        result = self.resolve()
        self.assertEqual(result["aborted"], "provider_unavailable")
        self.assertEqual(result["outcomes"], {"search_failed": 3})
        self.assertEqual(result["requests"]["made"], 9)                    # three exhausted requests
        self.assertEqual(result["pending"], 1)
        self.assertFalse((self.out / ".kura-resolve.json").exists())
        self.nyaa.errors = []
        self.assertEqual(self.resolve()["processed"], 4)                   # all still pending: retried

    def test_corrupt_ledger_is_treated_as_empty(self):
        (self.out / ".kura-resolve.json").write_text("{not json")
        self.assertEqual(self.resolve()["processed"], 1)
        self.assertIsInstance(json.loads((self.out / ".kura-resolve.json").read_text()), dict)

    def test_search_pacing_in_a_real_run(self):
        self.resolve()
        times = [t for t, *_ in self.nyaa.calls]
        self.assertGreaterEqual(len(times), 2)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, 2.5)


class Boundaries(Fixture, unittest.TestCase):
    def test_nothing_is_written_outside_the_output_directory(self):
        self.feed_for(item(FOLDER, 11, self.right.infohash))
        self.nyaa.descriptors = {"11": self.right.data}
        before = {name: snapshot(path) for name, path in
                  (("lib", self.lab.lib), ("torrents", self.lab.torrents))}
        real_open, writes = os.open, []
        writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def watch(path, flags, mode=0o777, *, dir_fd=None):
            if flags & writing:
                writes.append((str(path), dir_fd))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        renames = []
        real_rename, real_unlink = os.rename, os.unlink
        with patch("kura.resolve.os.open", side_effect=watch), \
             patch("kura.resolve.os.rename", side_effect=lambda *a, **k: renames.append(a) or real_rename(*a, **k)), \
             patch("kura.resolve.os.unlink", side_effect=lambda *a, **k: real_unlink(*a, **k)):
            self.resolve()
        self.assertTrue(writes)
        for path, dir_fd in writes:
            if dir_fd is None:                       # the state store's own mutation lock
                self.assertEqual(path, str(self.lab.state / "operation.lock"))
                continue
            self.assertNotIn("/", path)
        for src, dst in ((r[0], r[1]) for r in renames):
            self.assertNotIn("/", src + dst)
        self.assertEqual(before, {name: snapshot(path) for name, path in
                                  (("lib", self.lab.lib), ("torrents", self.lab.torrents))})
        for entry in self.out.iterdir():
            self.assertFalse(entry.name.startswith(".tmp-"))
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)

    def test_output_directory_must_exist_and_not_be_a_symlink(self):
        with self.assertRaises(KuraError) as caught:
            resolve.resolve(self.lab.store, library="home", torrents_out=self.lab.tmp / "missing",
                            fetcher=self.nyaa)
        self.assertEqual(caught.exception.code, "not_found")
        self.assertFalse((self.lab.tmp / "missing").exists())
        link = self.lab.tmp / "link"
        link.symlink_to(self.out)
        with self.assertRaises(KuraError) as caught:
            resolve.resolve(self.lab.store, library="home", torrents_out=link, fetcher=self.nyaa)
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual(self.nyaa.calls, [])

    def test_concurrent_resolve_is_refused(self):
        with resolve.OutDir(self.out):
            with self.assertRaises(KuraError) as caught:
                resolve.OutDir(self.out)
            self.assertEqual(caught.exception.code, "busy")
        with resolve.OutDir(self.out):
            pass

    def test_existing_descriptor_is_never_replaced_and_odd_entries_block(self):
        existing = self.out / (self.right.infohash + ".torrent")
        existing.write_bytes(b"keep me")
        os.utime(existing, (1, 1))
        with resolve.OutDir(self.out) as out:
            self.assertEqual(out.write_descriptor(self.right.infohash, self.right.data), "present")
            self.assertEqual(existing.read_bytes(), b"keep me")
            (self.out / (self.decoy.infohash + ".torrent")).symlink_to(existing)
            with self.assertRaises(KuraError):
                out.write_descriptor(self.decoy.infohash, self.decoy.data)
            self.assertTrue((self.out / (self.decoy.infohash + ".torrent")).is_symlink())
            self.assertEqual(out.write_descriptor("1" * 40, b"x"), "written")

    def test_failed_write_leaves_no_temp_file(self):
        with resolve.OutDir(self.out) as out, patch("kura.resolve.os.rename", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                out.write_descriptor("2" * 40, b"x")
        self.assertEqual(self.names(), [".kura-resolve.lock"])

    def test_argument_validation(self):
        for kw in ({"max_candidates": 0}, {"max_candidates": 11}, {"max_titles": 0},
                   {"budget_seconds": 0}, {"budget_seconds": float("nan")}, {"titles": "x"}, {"interval": -1}):
            with self.subTest(kw=kw), self.assertRaises(KuraError) as caught:
                self.resolve(**kw)
            self.assertEqual(caught.exception.code, "invalid_arguments")
        with self.assertRaises(KuraError) as caught:
            resolve.resolve(self.lab.store, library="nope", torrents_out=self.out, fetcher=self.nyaa)
        self.assertEqual(caught.exception.code, "not_found")

    def test_default_transport_is_the_hardened_provider_fetch(self):
        with patch("kura.provider.fetch", side_effect=lambda *a: feed()) as fetch:
            resolve.resolve(self.lab.store, library="home", torrents_out=self.out,
                            sleep=self.clock.sleep, clock=self.clock)
        url, limit = fetch.call_args_list[0][0][:2]
        self.assertTrue(url.startswith("https://nyaa.si/?"))
        provider.validate_url(url)
        self.assertEqual(fetch.call_args_list[0][0][2], False)


class Cli(Fixture, unittest.TestCase):
    def test_resolve_command_envelope(self):
        self.feed_for(item(FOLDER, 11, self.right.infohash))
        self.nyaa.descriptors = {"11": self.right.data}
        real = resolve.resolve

        def paced(store, **kw):         # same call the CLI makes, on the fake network and clock
            return real(store, fetcher=self.nyaa, sleep=self.clock.sleep, clock=self.clock, **kw)

        with patch("kura.cli.private_root", return_value=self.lab.state), \
             patch("kura.cli_handover.resolve.resolve", side_effect=paced):
            code, out = run("manifest", "resolve", "--library", "home", "--torrents-out", str(self.out),
                            "--no-anilist", "--max-candidates", "2", "--budget-minutes", "10")
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "manifest"))
        self.assertEqual((out["data"]["outcomes"], out["data"]["descriptors_written"]), ({"written": 1}, 1))
        self.assertTrue((self.out / (self.right.infohash + ".torrent")).exists())

    def test_anilist_default_on_and_flag_off(self):
        with patch("kura.cli.private_root", return_value=self.lab.state), \
             patch("kura.cli_handover.AniList") as aniList, \
             patch("kura.cli_handover.resolve.resolve", return_value={"processed": 0}) as call:
            run("manifest", "resolve", "--library", "home", "--torrents-out", str(self.out))
            self.assertIsNotNone(call.call_args.kwargs["aliases"])
            aniList.assert_called_once()
            self.assertEqual(call.call_args.kwargs["budget_seconds"], 30 * 60)
            aniList.reset_mock()
            run("manifest", "resolve", "--library", "home", "--torrents-out", str(self.out), "--no-anilist")
            self.assertIsNone(call.call_args.kwargs["aliases"])
            aniList.assert_not_called()

    def test_errors(self):
        base = ["manifest", "resolve", "--library", "home", "--torrents-out", str(self.out)]
        with patch("kura.cli.private_root", return_value=self.lab.state):
            code, out = run(*base, "--max-candidates", "0")
            self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
            code, out = run(*base[:-1], str(self.lab.tmp / "missing"), "--no-anilist")
            self.assertEqual((code, out["error"]["code"]), (2, "not_found"))
            code, out = run("manifest", "resolve", "--library", "home")
            self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
            code, out = run(*base, "--budget-minutes", "nan")
            self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))


if __name__ == "__main__":
    unittest.main()
