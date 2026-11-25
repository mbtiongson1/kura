"""SeaDex mirror: fake PocketBase pages only. No sockets, no real records, no real clock."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import kura.cli as cli
from kura.models import KuraError
from kura.ratelimit import SharedLimiter
from kura.sources import seadex
from kura.store import Store
from seadexfake import (FAKE_GROUP, FAKE_TRACKER, Clock, FakeSeaDex, digest, entry, private_torrent, rid,
                        stamp, torrent)

AL = 999000001


def world():
    """Entries 1,2 (AniList 999000001/2) with torrents 11,12,13 (13 is private)."""
    return FakeSeaDex(
        entries=[entry(1, AL, trs=[11, 12]), entry(2, AL + 1, trs=[13])],
        torrents=[torrent(11), torrent(12, best=False), private_torrent(13)])


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "kura")
        self.addCleanup(self.store.db.close)
        self.clock = Clock()
        self.fake = world()

    def limiter(self):
        return SharedLimiter(self.store.root, "seadex", interval=1.0, wall=self.clock, sleep=self.clock.sleep)

    def sync(self, **kw):
        kw.setdefault("transport", self.fake.transport)
        return seadex.sync(self.store, limiter=self.limiter(), wall=self.clock, **kw)

    def rows(self, sql, *args):
        return self.store.db.execute(sql, args).fetchall()

    def state(self):
        return self.rows("SELECT status, last_ok, last_full_ok, cursor FROM sync_state")[0]


class Sync(Case):
    def test_three_page_pagination(self):
        fake = FakeSeaDex([entry(n, AL + n) for n in range(1, 6)], [torrent(n + 20) for n in range(1, 6)])
        with patch.object(seadex, "PER_PAGE", 2):
            out = self.sync(transport=fake.transport)
        self.assertEqual(out["requests"], 6)        # 3 pages of entries + 3 of torrents
        self.assertEqual((out["mode"], out["entries"], out["skipped_malformed"]),
                         ("full", {"upserted": 5, "total": 5}, 0))
        self.assertEqual(out["torrents"], {"upserted": 5, "total": 5, "public": 5})
        pages = [parse_qs(urlsplit(u).query)["page"][0] for u in fake.urls]
        self.assertEqual(pages, ["1", "2", "3", "1", "2", "3"])
        self.assertEqual(self.state()[0], "ok")

    def test_incremental_cursor_and_weekly_full(self):
        first = self.sync()
        self.assertEqual(first["mode"], "full")
        self.assertEqual(json.loads(self.state()[3]),
                         {"entries": stamp(2), "torrents": stamp(13)})
        self.clock.now += 3600
        self.fake.entries[rid(2)] = entry(500, AL + 1, trs=[13], id=rid(2), notes="changed")
        self.fake.urls.clear()
        out = self.sync()
        self.assertEqual((out["mode"], out["entries"]["upserted"], out["torrents"]["upserted"]),
                         ("incremental", 1, 0))
        queries = [parse_qs(urlsplit(u).query)["filter"][0] for u in self.fake.urls]
        self.assertEqual(queries, [f'updated>"{stamp(2)}"', f'updated>"{stamp(13)}"'])
        self.assertEqual(self.rows("SELECT notes FROM seadex_entries WHERE id=?", rid(2)), [("changed",)])
        self.assertEqual(out["entries"]["total"], 2)
        self.assertEqual(json.loads(self.state()[3])["entries"], stamp(500))
        self.clock.now += 8 * 86400                 # last full sync is now older than 7 days
        self.assertEqual(self.sync()["mode"], "full")

    def test_full_replace_removes_deleted_upstream(self):
        self.sync()
        del self.fake.entries[rid(2)]
        del self.fake.torrents[rid(12)]
        self.clock.now += 60
        out = self.sync(full=True)
        self.assertEqual((out["mode"], out["entries"]["total"], out["torrents"]["total"]), ("full", 1, 2))
        self.assertEqual(self.rows("SELECT id FROM seadex_entries"), [(rid(1),)])
        self.assertEqual(self.rows("SELECT torrent_id FROM seadex_entry_torrents ORDER BY 1"),
                         [(rid(11),), (rid(12),)])      # the link list is what the entry still says

    def test_private_and_lookalike_rows_are_not_public(self):
        self.fake.torrents = {t["id"]: t for t in [
            private_torrent(13),
            torrent(14, tracker="nyaa"),                                    # case-insensitive: public
            torrent(15, url="http://nyaa.si/view/1"),
            torrent(16, url="https://nyaa.si/view/1/extra"),
            torrent(17, url="https://nyaa.si.invalid/view/1"),
            torrent(18, url="https://nyaa.si/view/١٢"),            # non-ASCII digits
            torrent(19, infoHash="abc"),
            torrent(20, tracker=FAKE_TRACKER)]}
        self.sync()
        got = {r[0]: r[1:] for r in self.rows("SELECT id, public, infohash, nyaa_id FROM seadex_torrents")}
        self.assertEqual(got[rid(14)], (1, digest("torrent-14"), 999000014))
        for n in (13, 15, 16, 17, 18, 19, 20):
            self.assertEqual(got[rid(n)], (0, None, None), n)
        self.assertEqual(self.rows("SELECT tracker FROM seadex_torrents WHERE id=?", rid(13)),
                         [(FAKE_TRACKER,)])
        self.assertNotIn("<redacted>", json.dumps(self.rows("SELECT * FROM seadex_torrents")))

    def test_malformed_items_are_skipped_and_counted(self):
        bad_entries = [entry(30, True), entry(31, 0), entry(32, AL, id="BAD ID"), entry(33, AL, updated="x"),
                       entry(34, AL, incomplete="no"), {**entry(35, AL), "trs": "oops"}, "text", None,
                       {k: v for k, v in entry(36, AL).items() if k != "updated"}]
        bad_torrents = [torrent(40, tracker=""), torrent(41, best="yes"), torrent(42, updated="soon"),
                        torrent(43, id="x" * 33), 7]
        # the fake keys records by id, so feed the raw lists through a custom transport
        pages = {"entries": [entry(1, AL, trs=[11]), *bad_entries], "torrents": [torrent(11), *bad_torrents]}

        def transport(url):
            name = urlsplit(url).path.split("/")[3]
            return json.dumps({"page": 1, "perPage": 500, "totalItems": 0, "totalPages": 1,
                               "items": pages[name]}).encode()
        out = self.sync(transport=transport)
        self.assertEqual((out["entries"]["total"], out["torrents"]["total"]), (1, 1))
        self.assertEqual(out["skipped_malformed"], len(bad_entries) + len(bad_torrents))

    def test_sanitizing(self):
        long_tags = [f"tag{i}" + "x" * 60 for i in range(30)]
        e = entry(1, AL, notes="a\x00b‮c\nd\te" + "z" * 5000, theoreticalBest="\x1b[31mred",
                  comparison=",".join(["http://plain.example/x", "javascript:alert(1)", "https://u:p@h.example/x",
                                       "https://ok.example/a b", *[f"https://ok.example/{i}" for i in range(40)]]),
                  trs=[11])
        t = torrent(11, tags=long_tags, group="Kura\x07 Encodes")
        self.sync(transport=FakeSeaDex([e], [t]).transport)
        notes, best, comparison = self.rows("SELECT notes, theoretical_best, comparison FROM seadex_entries")[0]
        self.assertEqual(notes[:12], "abc d e" + "zzzzz")
        self.assertEqual(len(notes), 4000)
        self.assertNotIn("\x1b", best)
        links = json.loads(comparison)
        self.assertEqual(len(links), 20)
        self.assertTrue(all(u.startswith("https://ok.example/") for u in links))
        tags, group = self.rows("SELECT tags, release_group FROM seadex_torrents")[0]
        tags = json.loads(tags)
        self.assertEqual((len(tags), max(map(len, tags)), group), (20, 40, "Kura Encodes"))

    def test_failure_keeps_old_rows_and_records_failed(self):
        self.sync()
        before = self.rows("SELECT * FROM seadex_entries") + self.rows("SELECT * FROM seadex_torrents")
        self.fake.fail = KuraError("seadex_unavailable", "down", status=503)
        self.clock.now += 60
        with self.assertRaises(KuraError) as caught:
            self.sync(full=True)
        self.assertEqual(caught.exception.code, "seadex_unavailable")
        self.assertTrue(caught.exception.details["kept_previous_data"])
        self.assertEqual(before, self.rows("SELECT * FROM seadex_entries") + self.rows("SELECT * FROM seadex_torrents"))
        self.assertEqual(self.state()[0], "failed")
        self.assertIsNotNone(self.state()[1])       # last_ok from the earlier success is kept

    def test_midway_failure_is_not_a_partial_replace(self):
        calls = []

        def transport(url):
            calls.append(url)
            if "/torrents/" in url:
                raise KuraError("seadex_unavailable", "down")
            return self.fake.transport(url)
        with self.assertRaises(KuraError):
            self.sync(transport=transport)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM seadex_entries"), [(0,)])
        self.assertEqual(self.state()[0], "failed")

    def test_http_429_penalizes_everyone(self):
        self.fake.fail = KuraError("seadex_unavailable", "slow down", status=429, retry_after=120.0)
        with self.assertRaises(KuraError):
            self.sync()
        self.assertGreaterEqual(self.limiter().state()["penalty_in_s"], 119)

    def test_invalid_response_shapes(self):
        for body in (b"not json", b"[]", json.dumps({"items": {}, "totalPages": 1}).encode(),
                     json.dumps({"items": [], "totalPages": 101}).encode(),
                     json.dumps({"items": [], "totalPages": True}).encode(),
                     json.dumps({"items": [{}] * 501, "totalPages": 1}).encode()):
            with self.assertRaises(KuraError) as caught:
                self.sync(transport=lambda url, b=body: b)
            self.assertEqual(caught.exception.code, "seadex_invalid", body[:20])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM seadex_entries"), [(0,)])

    def test_pacing_one_request_per_second(self):
        self.sync()
        self.assertEqual(len(self.fake.urls), 2)
        self.assertEqual([round(s, 3) for s in self.clock.slept], [1.0])
        self.sync(full=True)
        self.assertEqual(len(self.fake.urls), 4)
        self.assertTrue(all(abs(s - 1.0) < 1e-6 for s in self.clock.slept), self.clock.slept)
        self.assertEqual(len(self.clock.slept), 3)

    def test_request_budget_fails_a_listing(self):
        with patch.object(seadex, "PER_PAGE", 1):
            with self.assertRaises(KuraError) as caught:
                self.sync(max_requests=2)
        self.assertEqual(caught.exception.code, "request_budget")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM seadex_entries"), [(0,)])


class Builders(unittest.TestCase):
    def test_filter_injection_is_rejected(self):
        for bad in ('a" || 1=1', "a'b", "UPPER", "", "a" * 33, "a b", 'a"', None, 5, "a||b"):
            with self.assertRaises(KuraError, msg=repr(bad)):
                seadex.record_id(bad)
        for bad in ('2025-01-01 00:00:00Z" || 1=1', "2025-01-01T00:00:00Z", "", None,
                    '2025-01-01 00:00:00.1Z"', "2025-01-01 00:00:00.Z"):
            with self.assertRaises(KuraError, msg=repr(bad)):
                seadex.timestamp(bad)
        for bad in (True, 0, -1, 10**9, "5", "5 || 1=1", 1.0):
            with self.assertRaises(KuraError, msg=repr(bad)):
                seadex.anilist_id(bad)
        for bad in ("a" * 39, "a" * 41, "g" * 40, "a" * 40 + "\n", None):
            with self.assertRaises(KuraError, msg=repr(bad)):
                seadex.infohash(bad)
        with self.assertRaises(KuraError):
            seadex.filter_ids([])
        with self.assertRaises(KuraError):
            seadex.filter_ids([rid(n) for n in range(41)])
        with self.assertRaises(KuraError):
            seadex.filter_ids([rid(1), 'x" || 1=1 || id="y'])
        self.assertEqual(seadex.infohash("A" * 40), "a" * 40)
        self.assertEqual(seadex.filter_ids([rid(1), rid(2)]), f'id="{rid(1)}"||id="{rid(2)}"')

    def test_urls_are_only_collection_records_on_the_exact_host(self):
        ok = seadex.build_url("entries", page=2, per_page=500, fields=seadex.ENTRY_FIELDS,
                              filter_=seadex.filter_updated(stamp(5)))
        u = urlsplit(ok)
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "seadex.moe", "/api/collections/entries/records"))
        self.assertEqual(parse_qs(u.query)["filter"], [f'updated>"{stamp(5)}"'])
        for kw in (dict(collection="users"), dict(collection="../x"), dict(page=0), dict(page=101),
                   dict(page=True), dict(per_page=501), dict(fields=["id,files"]), dict(fields=[]),
                   dict(fields=["id&filter=1"])):
            args = dict(collection="entries", page=1, per_page=10, fields=["id"])
            args.update(kw)
            with self.assertRaises(KuraError, msg=str(kw)):
                seadex.build_url(args.pop("collection"), **args)
        for bad in ("http://seadex.moe/api/collections/entries/records",
                    "https://seadex.moe.invalid/api/collections/entries/records",
                    "https://user@seadex.moe/api/collections/entries/records",
                    "https://seadex.moe:444/api/collections/entries/records",
                    "https://seadex.moe/api/collections/users/records",
                    "https://seadex.moe/api/collections/entries/records/abc",
                    "https://seadex.moe/api/collections/entries/records#x",
                    "https://seadex.moe/api/collections/entries/records?a=b c",
                    "https://seadex.moe/api/admins"):
            with self.assertRaises(KuraError, msg=bad):
                seadex.validate_url(bad)

    def test_files_median_excludes_extras(self):
        def f(name, size):
            return {"name": name, "length": size}
        parsed = seadex.parse_files([
            f("Show/[Kura Encodes] Show - 01.mkv", 300), f("Show/[Kura Encodes] Show - 02.mkv", 100),
            f("Show/[Kura Encodes] Show - 03.mkv", 200),
            f("Show/[Kura Encodes] Show - NCOP1.mkv", 10), f("Show/Show - NCED 2.mkv", 10),
            f("Show/Show - PV 01.mkv", 10), f("Show/Show - CM01.mkv", 10), f("Show/Show Menu.mkv", 10),
            f("Show/Show - Trailer.mkv", 10), f("Show/Show - Preview.mkv", 10),
            f("Show/Extras/Making of.mkv", 10), f("Show/NC/Clean.mkv", 10), f("Show/Bonus/Bits.mkv", 10),
            f("Show/Show - 01.ass", 5), f("Show/cover.jpg", 5),
            f("x" * 300 + ".mkv", 9), {"name": "bad", "length": True}, {"name": "", "length": 1}, "junk"])
        self.assertEqual((parsed["video_files"], parsed["median_video_bytes"]), (3, 200))
        self.assertEqual(parsed["total_bytes"], 710)
        self.assertEqual(len(parsed["files"]), 15)
        self.assertIsNone(seadex.parse_files("x"))
        self.assertIsNone(seadex.parse_files([f("a.txt", 1)])["median_video_bytes"])
        many = seadex.parse_files([f(f"Show - {i}.mkv", 1) for i in range(2500)])
        self.assertEqual((len(many["files"]), many["video_files"]), (2000, 2500))
        self.assertEqual(seadex.parse_files([f("Show - 01.mkv", 1), f("Show - 02.mkv", 2)])["median_video_bytes"], 1)


class Transport(unittest.TestCase):
    URL = "https://seadex.moe/api/collections/entries/records?page=1"

    def opener(self, body=b"{}"):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = body
        opener = Mock()
        opener.open.return_value = response
        return opener

    def public_dns(self):
        return patch("kura.sources.seadex.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.1.1.1", 443))])

    def test_private_dns_refused(self):
        for address in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.0.1", "::1"):
            with patch("kura.sources.seadex.socket.getaddrinfo",
                       return_value=[(2, 1, 6, "", (address, 443))]):
                with self.assertRaises(KuraError) as caught:
                    seadex.fetch(self.URL)
            self.assertEqual(caught.exception.code, "unsafe_url", address)

    def test_oversize_refused_and_hardened_opener(self):
        opener = self.opener(b"x" * (seadex.LIMIT + 1))
        with self.public_dns(), patch("kura.sources.seadex.urllib.request.build_opener",
                                      return_value=opener) as build:
            with self.assertRaises(KuraError) as caught:
                seadex.fetch(self.URL)
        self.assertEqual(caught.exception.code, "seadex_invalid")
        names = [type(h).__name__ for h in build.call_args.args]
        self.assertIn("NoRedirect", names)
        self.assertIn("ProxyHandler", names)
        self.assertEqual(opener.open.call_args.args[0].get_header("User-agent"), seadex.USER_AGENT)
        opener.open.return_value.read.assert_called_with(seadex.LIMIT + 1)

    def test_redirects_are_refused(self):
        from kura.provider import NoRedirect
        with self.assertRaises(KuraError) as caught:
            NoRedirect().redirect_request(Mock(), None, 302, "Found", {}, "https://elsewhere.invalid/")
        self.assertEqual(caught.exception.code, "unsafe_redirect")
        opener = Mock()
        opener.open.side_effect = KuraError("unsafe_redirect", "no")
        with self.public_dns(), patch("kura.sources.seadex.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(KuraError) as seen:
                seadex.fetch(self.URL)
        self.assertEqual(seen.exception.code, "unsafe_redirect")

    def test_network_and_http_errors_are_structured(self):
        with patch("kura.sources.seadex.socket.getaddrinfo", side_effect=OSError("offline")):
            with self.assertRaises(KuraError) as caught:
                seadex.fetch(self.URL)
        self.assertEqual(caught.exception.code, "seadex_unavailable")
        import urllib.error
        err = urllib.error.HTTPError(self.URL, 429, "Too Many", {"Retry-After": "90"}, io.BytesIO(b""))
        self.addCleanup(err.close)
        opener = Mock()
        opener.open.side_effect = err
        with self.public_dns(), patch("kura.sources.seadex.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(KuraError) as seen:
                seadex.fetch(self.URL)
        self.assertEqual((seen.exception.details["status"], seen.exception.details["retry_after"]), (429, 90.0))

    def test_unsafe_url_never_reaches_the_socket(self):
        with patch("kura.sources.seadex.socket.getaddrinfo", side_effect=AssertionError("resolved")):
            with self.assertRaises(KuraError):
                seadex.fetch("https://elsewhere.invalid/api/collections/entries/records")


class Files(Case):
    def identities(self, *ids, library="main"):
        for i, al in enumerate(ids):
            self.store.db.execute(
                "INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at) "
                "VALUES (?,?,?,?,?,?)", (library, f"main/Title {i}", al, "resolved", "list", 1.0))
        self.store.db.execute(
            "INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at) "
            "VALUES (?,?,?,?,?,?)", (library, "main/Unknown", None, "unresolved", "alias", 1.0))
        self.store.db.commit()

    def test_empty_identity_and_list_tables_fetch_nothing(self):
        out = self.sync(files_library="main")
        self.assertEqual((out["files_fetched"], out["files_pending"], out["requests"]), (0, 0, 2))
        self.assertEqual(seadex.library_ids(self.store, "main"), [])

    def test_files_for_library_uses_identities_then_viewer_list(self):
        self.identities(AL)
        db = self.store.db
        db.execute("INSERT INTO anilist_users(user_id, name, is_viewer, updated_at) VALUES (999001,'viewer',1,1.0)")
        db.execute("INSERT INTO anilist_users(user_id, name, is_viewer, updated_at) VALUES (999002,'other',0,1.0)")
        db.execute("INSERT INTO anilist_list_entries(user_id, anilist_id, status, fetched_at) "
                   "VALUES (999001, ?, 'PLANNING', 1.0)", (AL + 1,))
        db.execute("INSERT INTO anilist_list_entries(user_id, anilist_id, status, fetched_at) "
                   "VALUES (999002, ?, 'PLANNING', 1.0)", (AL + 7,))
        db.commit()
        self.assertEqual(seadex.library_ids(self.store, "main"), [AL, AL + 1])
        self.fake.torrents[rid(11)]["files"] = [
            {"name": "Show/[Kura Encodes] Show - 01.mkv", "length": 400},
            {"name": "Show/Show - NCOP.mkv", "length": 20}, {"name": "Show/Fonts/a.ttf", "length": 5}]
        out = self.sync(files_library="main")
        self.assertEqual((out["files_fetched"], out["files_pending"]), (2, 0))   # 11 and 12; 13 is private
        row = self.rows("SELECT total_bytes, video_files, median_video_bytes, files_fetched_at, files "
                        "FROM seadex_torrents WHERE id=?", rid(11))[0]
        self.assertEqual(row[:3], (425, 1, 400))
        self.assertEqual(row[3], 1_800_000_000.0)      # the run's start time
        self.assertEqual(json.loads(row[4])[0], ["Show/[Kura Encodes] Show - 01.mkv", 400])
        self.assertIsNone(self.rows("SELECT files_fetched_at FROM seadex_torrents WHERE id=?", rid(13))[0][0])
        again = self.sync(files_library="main")                      # nothing left to fetch
        self.assertEqual((again["files_fetched"], again["requests"]), (0, 2))

    def test_batches_of_forty_and_budget(self):
        torrents = [torrent(n) for n in range(100, 145)]            # 45 public torrents on one entry
        fake = FakeSeaDex([entry(1, AL, trs=range(100, 145))], torrents)
        self.identities(AL)
        out = self.sync(transport=fake.transport, files_library="main")
        self.assertEqual((out["files_fetched"], out["requests"]), (45, 4))   # 2 listing + 2 file batches
        batch = parse_qs(urlsplit(fake.urls[2]).query)
        self.assertEqual(batch["filter"][0].count('id="'), 40)
        self.assertEqual(batch["fields"], ["id,files"])
        self.assertEqual(batch["perPage"], ["40"])
        # a budget that ends mid-way is not a failure: the rest stays pending
        self.setUp()
        self.identities(AL)
        out = self.sync(transport=fake.transport, files_library="main", max_requests=3)
        self.assertEqual((out["files_fetched"], out["files_pending"], out["requests"]), (40, 5, 3))
        self.assertEqual(self.state()[0], "ok")

    def test_file_details_survive_unchanged_records_only(self):
        self.identities(AL)
        self.sync(files_library="main")
        self.clock.now += 60
        self.sync(full=True)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM seadex_torrents WHERE files_fetched_at IS NOT NULL"), [(2,)])
        self.fake.torrents[rid(11)]["updated"] = stamp(900)
        self.clock.now += 60
        self.sync()                                                   # incremental picks up the change
        got = dict(self.rows("SELECT id, files_fetched_at IS NOT NULL FROM seadex_torrents"))
        self.assertEqual((got[rid(11)], got[rid(12)]), (0, 1))

    def test_files_errors_keep_the_committed_listing(self):
        self.identities(AL)

        def transport(url):
            if "fields=id%2Cfiles" in url:
                raise KuraError("seadex_unavailable", "down")
            return self.fake.transport(url)
        with self.assertRaises(KuraError) as caught:
            self.sync(transport=transport, files_library="main")
        self.assertTrue(caught.exception.details["listing_committed"])
        self.assertEqual(self.state()[0], "ok")

    def test_unknown_or_malformed_file_records_are_skipped(self):
        self.identities(AL)

        def transport(url):
            if "fields=id%2Cfiles" in url:
                items = [{"id": rid(11), "files": [{"name": "A - 01.mkv", "length": 7}]},
                         {"id": rid(999), "files": []}, {"id": rid(12), "files": "nope"}, "x"]
                return json.dumps({"page": 1, "perPage": 40, "totalItems": 4, "totalPages": 1,
                                   "items": items}).encode()
            return self.fake.transport(url)
        out = self.sync(transport=transport, files_library="main")
        self.assertEqual((out["files_fetched"], out["skipped_malformed"], out["files_pending"]), (1, 3, 1))


class Lookup(Case):
    def test_lookup_needs_no_network(self):
        self.sync()
        with patch("kura.sources.seadex.fetch", side_effect=AssertionError("network")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")):
            out = seadex.lookup(self.store, anilist_id_=AL, now=self.clock)
            self.assertEqual(out["anilist_id"], AL)
            self.assertEqual((out["entry"]["id"], out["entry"]["incomplete"]), (rid(1), False))
            self.assertEqual(out["entry"]["comparison"], ["https://compare.example/c/one",
                                                          "https://compare.example/c/two"])
            self.assertEqual([(t["is_best"], t["release_group"], t["public"]) for t in out["torrents"]],
                             [(True, FAKE_GROUP, True), (False, FAKE_GROUP, True)])
            self.assertEqual(out["torrents"][0]["infohash"], digest("torrent-11"))
            self.assertEqual(out["torrents"][0]["nyaa_id"], 999000011)
            self.assertFalse(out["stale"])
            self.assertEqual(out["synced_at"], self.state()[1])
            private = seadex.lookup(self.store, anilist_id_=AL + 1, now=self.clock)["torrents"]
            self.assertEqual(len(private), 1)
            self.assertFalse(private[0]["public"])
            self.assertNotIn("infohash", private[0])
            self.assertNotIn("tracker", private[0])
            self.assertNotIn("redacted", json.dumps(private))
            none = seadex.lookup(self.store, anilist_id_=AL + 50, now=self.clock)
            self.assertEqual((none["entry"], none["torrents"]), (None, []))

    def test_stale_and_empty_state(self):
        empty = seadex.lookup(self.store, anilist_id_=AL, now=self.clock)
        self.assertEqual((empty["entry"], empty["synced_at"], empty["stale"]), (None, None, True))
        self.sync()
        self.clock.now += 7 * 86400 + 1
        self.assertTrue(seadex.lookup(self.store, anilist_id_=AL, now=self.clock)["stale"])
        self.assertTrue(seadex.status(self.store, now=self.clock)["stale"])

    def test_lookup_by_public_infohash_only(self):
        self.sync()
        out = seadex.lookup(self.store, infohash_=digest("torrent-11").upper(), now=self.clock)
        self.assertEqual(out["infohash"], digest("torrent-11"))
        self.assertEqual([m["anilist_id"] for m in out["matches"]], [AL])
        self.assertEqual(seadex.lookup(self.store, infohash_="0" * 40, now=self.clock)["matches"], [])
        with self.assertRaises(KuraError):
            seadex.lookup(self.store, infohash_="<redacted>", now=self.clock)
        with self.assertRaises(KuraError):
            seadex.lookup(self.store, now=self.clock)

    def test_status(self):
        self.assertEqual(seadex.status(self.store, now=self.clock)["status"], "never")
        self.sync()
        out = seadex.status(self.store, now=self.clock)
        self.assertEqual((out["status"], out["entries"], out["torrents"], out["public_torrents"],
                          out["full_sync_due"]), ("ok", 2, 3, 2, False))


def run(*argv, home):
    buffer = io.StringIO()
    with patch.dict(os.environ, {"HOME": home}), patch("sys.argv", ["kura", *argv]), \
         contextlib.redirect_stdout(buffer):
        try:
            cli.main()
        except SystemExit as exited:
            code = exited.code
    return code, json.loads(buffer.getvalue())


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.fake = world()
        patcher = patch("kura.sources.seadex.fetch", side_effect=lambda url: self.fake.transport(url))
        patcher.start()
        self.addCleanup(patcher.stop)
        interval = patch.object(seadex, "INTERVAL", 0.001)
        interval.start()
        self.addCleanup(interval.stop)

    def test_sync_lookup_status_envelopes(self):
        code, out = run("seadex", "sync", home=self.home)
        self.assertEqual((code, out["success"], out["phase"], out["schema_version"]), (0, True, "seadex", 1))
        data = out["data"]
        self.assertEqual(set(data), {"mode", "entries", "torrents", "files_fetched", "skipped_malformed",
                                     "requests", "cursor", "synced_at"})
        self.assertEqual((data["mode"], data["entries"]["total"], data["torrents"]["public"]), ("full", 2, 2))
        with patch("kura.sources.seadex.fetch", side_effect=AssertionError("network")):
            code, out = run("seadex", "lookup", "--anilist-id", str(AL), home=self.home)
            self.assertEqual((code, out["data"]["entry"]["id"]), (0, rid(1)))
            self.assertEqual(set(out["data"]), {"anilist_id", "entry", "torrents", "synced_at", "stale"})
            code, out = run("seadex", "lookup", "--infohash", digest("torrent-11"), home=self.home)
            self.assertEqual(out["data"]["matches"][0]["anilist_id"], AL)
            code, out = run("seadex", "status", home=self.home)
            self.assertEqual((code, out["data"]["status"], out["data"]["entries"]), (0, "ok", 2))

    def test_failure_envelope_and_no_token_like_leak(self):
        self.fake.fail = KuraError("seadex_unavailable", "down", status=503)
        code, out = run("seadex", "sync", home=self.home)
        self.assertEqual((code, out["success"], out["error"]["code"]), (2, False, "seadex_unavailable"))
        self.assertTrue(out["data"]["kept_previous_data"])
        code, out = run("seadex", "status", home=self.home)
        self.assertEqual(out["data"]["status"], "failed")

    def test_argument_validation(self):
        for argv in (("sync", "--max-requests", "0"), ("sync", "--max-requests", "501"),
                     ("lookup",), ("lookup", "--anilist-id", "1", "--infohash", "a" * 40),
                     ("lookup", "--anilist-id", "0"), ("lookup", "--anilist-id", "x"),
                     ("lookup", "--infohash", "zz"), ("sync", "--files-for-library", "")):
            code, out = run("seadex", *argv, home=self.home)
            self.assertEqual((code, out["success"]), (2, False), argv)
            self.assertIn(out["error"]["code"], ("invalid_arguments",), argv)

    def test_files_for_library_through_the_cli(self):
        run("seadex", "sync", home=self.home)
        store = Store(Path(self.home) / ".local" / "share" / "kura")
        store.db.execute("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at) "
                         "VALUES ('main','main/T',?,'pinned','owner',1.0)", (AL,))
        store.db.commit()
        store.db.close()
        code, out = run("seadex", "sync", "--files-for-library", "main", home=self.home)
        self.assertEqual((code, out["data"]["files_fetched"], out["data"]["files_pending"]), (0, 2, 0))
        self.assertEqual(out["data"]["mode"], "incremental")


if __name__ == "__main__":
    unittest.main()
