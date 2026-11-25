"""HTTP tracker scrape (BEP 48). Synthetic data only; every socket is mocked, nothing goes online."""
import contextlib
import io
import json
import os
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote_to_bytes

import torrentgen
import kura.cli as cli
from kura import quarantine, scrape
from kura.manifest_store import ManifestRepo
from kura.models import KuraError
from kura.protect import classify, load_policy, magnet
from kura.torrentmeta import parse
from test_manifest import GOOD, Lab
from test_quarantine import age

PUBLIC = "8.8.8.8"
H = [bytes([n]) * 20 for n in range(1, 6)]


def answer(entries: dict, *, extra: bytes = b"") -> bytes:
    """A tracker body with the files dictionary in NON-canonical order, as real trackers send."""
    items = b"".join(b"20:" + h + b"d8:completei%de10:incompletei%dee" % (s, l)
                     for h, (s, l) in entries.items())
    return b"d5:filesd" + items + b"e" + extra + b"e"


def queried(target: str) -> list[bytes]:
    return [unquote_to_bytes(kv.split("=", 1)[1]) for kv in target.split("?", 1)[1].split("&")]


class FakeSocket:
    def __init__(self, response: bytes = b""):
        self.sent, self.response, self.closed = b"", response, False

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode="rb", *a, **k):
        return io.BytesIO(self.response)

    def settimeout(self, value):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def http(status=200, body=b"", headers=()):
    head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n"
    return head.encode() + b"".join(h.encode() + b"\r\n" for h in headers) + b"\r\n" + body


@contextlib.contextmanager
def network(addresses, response=b"", connect_error=None):
    """Mock DNS and connect. Yields the list of create_connection calls."""
    calls, sockets = [], []

    def getaddrinfo(host, port, *a, **k):
        calls.append(("dns", host))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a_, port)) for a_ in addresses]

    def connect(address, timeout=None, *a, **k):
        calls.append(("connect", address))
        if connect_error:
            raise connect_error
        sockets.append(FakeSocket(response))
        return sockets[-1]

    with patch("kura.scrape.socket.getaddrinfo", getaddrinfo), \
         patch("kura.scrape.socket.create_connection", connect):
        yield calls, sockets


class Derivation(unittest.TestCase):
    def test_bep48_scrape_url(self):
        self.assertEqual(scrape.scrape_target("http://t.example/announce"),
                         ("http", "t.example", 80, "/scrape"))
        self.assertEqual(scrape.scrape_target("https://T.Example:8443/announce.php"),
                         ("https", "t.example", 8443, "/scrape.php"))
        self.assertEqual(scrape.scrape_target("https://t.example/announce")[2], 443)

    def test_no_scrape_convention_or_credentials_means_no_target(self):
        for url in ("http://t.example/", "http://t.example", "http://t.example/scrape",
                    "udp://t.example:80/announce", "ftp://t.example/announce",
                    "http://t.example/announce?passkey=SECRETKEY", "http://u:p@t.example/announce",
                    "http://t.example/SECRETKEY/announce", "http://t.example/announce#x",
                    "http://t.example:99999/announce", "http://t.example/a/announce", "", None, 5):
            self.assertIsNone(scrape.scrape_target(url), url)

    def test_magnet_trackers_capped_deduplicated_and_credential_free(self):
        trackers = [f"http://t{i}.example/announce" for i in range(5)]
        built = torrentgen.build("Kura Sample Series", announce=trackers[0],
                                 announce_list=[[t] for t in trackers] + [["udp://u.example:1/announce"]])
        meta = parse(built.data)
        link = magnet(meta, classify(meta, load_policy(None, None)))
        found = scrape.targets_from_magnet(link)
        self.assertEqual([t[1] for t in found], ["t0.example", "t1.example", "t2.example"])
        self.assertEqual(scrape.targets_from_magnet(None), [])
        self.assertEqual(scrape.targets_from_magnet("magnet:?xt=urn:btih:" + "a" * 40), [])

    def test_request_target_encodes_raw_bytes(self):
        raw = [bytes(range(20)), b"\xff" * 20]
        target = scrape.request_target("/scrape", raw)
        self.assertTrue(target.startswith("/scrape?info_hash=%00%01%02"))
        self.assertEqual(target.count("info_hash="), 2)
        self.assertEqual(queried(target), raw)


class Parsing(unittest.TestCase):
    def bad(self, data, code="scrape_invalid"):
        with self.assertRaises(KuraError) as caught:
            scrape.parse_scrape(data, H[:1])
        self.assertEqual(caught.exception.code, code)

    def test_non_canonical_order_is_accepted(self):
        body = answer({H[0]: (7, 2), H[1]: (0, 0)}, extra=b"5:flagsd3:min3:ab1e")
        self.assertEqual(scrape.parse_scrape(body, H[:2]), {H[0]: (7, 2), H[1]: (0, 0)})

    def test_unrequested_and_unlisted_hashes_are_ignored(self):
        self.assertEqual(scrape.parse_scrape(answer({H[1]: (9, 9)}), H[:1]), {})

    def test_malformed_bencode(self):
        for data in (b"", b"garbage", b"d5:filesd", b"d5:filesd20:short", b"li1ee", b"i-0e", b"i01e",
                     b"d5:filesdeeXX", b"d5:files99:ee", b"d1:ai1e1:ai2ee", b"di1ei2ee",
                     b"d5:filesd20:" + H[0] + b"d8:completei5e10:incompleteee" + b"e"):
            self.bad(data)          # includes b"li1ee": a list is not a dictionary

    def test_failure_reason_and_missing_files(self):
        self.bad(b"d14:failure reason4:nopee", "scrape_failed")
        self.bad(b"d3:fooi1ee")
        self.bad(b"d5:filesli1eee")

    def test_bad_counters_are_not_trusted(self):
        for entry in (b"d8:completei-1e10:incompletei0ee", b"d8:complete1:510:incompletei0ee",
                      b"d8:completei5ee", b"d8:completei99999999999e10:incompletei0ee", b"i5e"):
            body = b"d5:filesd20:" + H[0] + entry + b"ee"
            self.assertEqual(scrape.parse_scrape(body, H[:1]), {}, entry)

    def test_size_and_depth_limits(self):
        self.bad(b"d" + b"x" * (scrape.LIMIT + 1))
        self.bad(b"l" * 50 + b"e" * 50)
        self.bad(b"l" + b"i1e" * 30_000 + b"e")


class Transport(unittest.TestCase):
    def get(self, **kw):
        return scrape.fetch("http", "t.example", 80, "/scrape?info_hash=%01", **kw)

    def test_success_is_pinned_to_the_validated_address(self):
        body = answer({H[0]: (4, 1)})
        with network([PUBLIC], http(200, body)) as (calls, sockets), \
             patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid:3128", "http_proxy": "http://proxy.invalid:3128"}):
            self.assertEqual(self.get(), body)
        self.assertEqual(calls, [("dns", "t.example"), ("connect", (PUBLIC, 80))])
        request = sockets[0].sent.decode()
        self.assertTrue(request.startswith("GET /scrape?info_hash=%01 HTTP/1.1\r\n"))
        self.assertIn("Host: t.example\r\n", request)
        self.assertNotIn("Authorization", request)
        self.assertTrue(sockets[0].closed)

    def test_private_loopback_and_special_addresses_are_refused_before_connecting(self):
        for address in ("10.0.0.5", "127.0.0.1", "172.16.9.9", "169.254.1.1", "0.0.0.0",
                        "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.1.2.3",
                        "224.0.0.1", "198.18.0.1", "192.0.2.7"):
            with network([address]) as (calls, _), self.assertRaises(KuraError) as caught:
                self.get()
            self.assertEqual(caught.exception.code, "unsafe_url", address)
            self.assertFalse([c for c in calls if c[0] == "connect"], address)

    def test_one_private_answer_among_public_ones_refuses_the_host(self):
        with network([PUBLIC, "10.0.0.5"]) as (calls, _), self.assertRaises(KuraError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "unsafe_url")
        self.assertEqual([c for c in calls if c[0] == "connect"], [])

    def test_dns_and_connect_failures_are_unavailable(self):
        with patch("kura.scrape.socket.getaddrinfo", side_effect=socket.gaierror("x")), \
             self.assertRaises(KuraError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "scrape_unavailable")
        with network([PUBLIC], connect_error=ConnectionRefusedError()), \
             self.assertRaises(KuraError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "scrape_unavailable")

    def test_redirects_are_refused_not_followed(self):
        for status in (301, 302, 307, 308):
            response = http(status, b"", ["Location: http://10.0.0.5/scrape"])
            with network([PUBLIC], response) as (calls, _), self.assertRaises(KuraError) as caught:
                self.get()
            self.assertEqual(caught.exception.code, "unsafe_redirect")
            self.assertEqual(len([c for c in calls if c[0] == "connect"]), 1)

    def test_error_status_and_oversize_bodies(self):
        with network([PUBLIC], http(503)), self.assertRaises(KuraError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "scrape_unavailable")
        big = http(200, b"x" * (scrape.LIMIT + 10))
        with network([PUBLIC], big), self.assertRaises(KuraError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "scrape_invalid")
        lying = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"x" * (scrape.LIMIT + 10)
        with network([PUBLIC], lying), self.assertRaises(KuraError) as caught:
            self.get()                      # no Content-Length: the read itself is bounded
        self.assertEqual(caught.exception.code, "scrape_invalid")

    def test_garbage_http_is_unavailable(self):
        with network([PUBLIC], b"\x00\x01 not http"), self.assertRaises(KuraError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "scrape_unavailable")

    def test_only_http_and_https_and_sane_ports(self):
        for scheme, port in (("ftp", 21), ("udp", 80), ("http", 0), ("http", 70000)):
            with self.assertRaises(KuraError) as caught:
                scrape.fetch(scheme, "t.example", port, "/scrape?info_hash=%01")
            self.assertEqual(caught.exception.code, "unsafe_url")

    def test_https_verifies_the_hostname_over_the_pinned_socket(self):
        body = answer({H[0]: (1, 1)})
        context = Mock()
        with network([PUBLIC], http(200, body)) as (calls, sockets), \
             patch("kura.scrape.ssl.create_default_context", return_value=context):
            context.wrap_socket.side_effect = lambda raw, server_hostname: raw
            self.assertEqual(scrape.fetch("https", "t.example", 8443, "/scrape?info_hash=%01"), body)
        self.assertEqual(calls[-1], ("connect", (PUBLIC, 8443)))
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "t.example")

    def test_a_stalled_tracker_is_cut_off_at_the_deadline(self):
        gate = threading.Event()

        class Stuck(FakeSocket):
            def makefile(self, *a, **k):
                class Stream:
                    def readline(self, *a):
                        gate.wait(3)
                        raise OSError("shut down")

                    def close(self):
                        pass

                    def flush(self):
                        pass
                return Stream()

            def shutdown(self, how):
                gate.set()

        with patch("kura.scrape.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, 1, 6, "", (PUBLIC, 80))]), \
             patch("kura.scrape.socket.create_connection", return_value=Stuck()):
            started = time.monotonic()
            with self.assertRaises(KuraError) as caught:
                self.get(timeout=0.2)
        self.assertEqual(caught.exception.code, "scrape_unavailable")
        self.assertLess(time.monotonic() - started, 2.0)


    def test_slow_drip_body_with_connection_close_is_cut_off(self):
        dead = threading.Event()
        head = io.BytesIO(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")

        class Drip(FakeSocket):
            def makefile(self, *a, **k):
                class Stream:
                    def readline(self, *a):
                        return head.readline(*a)

                    def read(self, n=-1):
                        if dead.is_set():
                            raise OSError("shut down")
                        time.sleep(0.03)       # always inside any per-recv timeout
                        return b"x"

                    def flush(self):
                        pass

                    def close(self):
                        pass
                return Stream()

            def shutdown(self, how):
                dead.set()

        with patch("kura.scrape.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, 1, 6, "", (PUBLIC, 80))]), \
             patch("kura.scrape.socket.create_connection", return_value=Drip()):
            started = time.monotonic()
            with self.assertRaises(KuraError) as caught:
                self.get(timeout=0.3)
        self.assertEqual(caught.exception.code, "scrape_unavailable")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_stalled_dns_is_abandoned_at_the_deadline(self):
        gate = threading.Event()

        def slow(*a, **k):
            gate.wait(3)
            return [(socket.AF_INET, 1, 6, "", (PUBLIC, 80))]

        with patch("kura.scrape.socket.getaddrinfo", slow), \
             patch("kura.scrape.socket.create_connection", side_effect=AssertionError("connected")):
            started = time.monotonic()
            with self.assertRaises(KuraError) as caught:
                self.get(timeout=0.2)
            gate.set()
        self.assertEqual(caught.exception.code, "scrape_unavailable")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_many_dead_addresses_share_one_budget(self):
        timeouts = []

        def connect(address, timeout=None, *a, **k):
            timeouts.append(timeout)
            time.sleep(0.12)
            raise TimeoutError("dead")

        many = [f"8.8.{n}.8" for n in range(1, 9)]
        with patch("kura.scrape.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, 1, 6, "", (a, 80)) for a in many]), \
             patch("kura.scrape.socket.create_connection", connect):
            started = time.monotonic()
            with self.assertRaises(KuraError) as caught:
                self.get(timeout=0.3)
        self.assertEqual(caught.exception.code, "scrape_unavailable")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLess(len(timeouts), len(many))
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))
        self.assertLessEqual(max(timeouts), 0.3)

    def test_https_handshake_socket_is_reachable_by_the_deadline_timer(self):
        raw = FakeSocket()
        context = Mock()
        seen = []

        def handshake(sock, server_hostname):
            seen.append(sock)
            raise OSError("handshake stalled")

        context.wrap_socket.side_effect = handshake
        with patch("kura.scrape.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, 1, 6, "", (PUBLIC, 443))]), \
             patch("kura.scrape.socket.create_connection", return_value=raw), \
             patch("kura.scrape.ssl.create_default_context", return_value=context):
            with self.assertRaises(KuraError) as caught:
                scrape.fetch("https", "t.example", 443, "/scrape?info_hash=%01")
        self.assertEqual(caught.exception.code, "scrape_unavailable")
        self.assertEqual(seen, [raw])
        self.assertTrue(raw.closed)


class Query(unittest.TestCase):
    """The tracker rounds, batching and budgets, below the database layer."""

    def run_query(self, work, fetcher, **kw):
        outcome, trail = {}, {h: [] for h in work}
        kw.setdefault("max_requests", 100)
        requests = scrape._query(work, outcome, trail, fetcher=fetcher, sleep=kw.pop("sleep", lambda s: None),
                                 clock=kw.pop("clock", lambda: 1000.0), **kw)
        return requests, outcome, trail

    def test_requests_are_batched_at_74_infohashes(self):
        hashes = [f"{n:040x}" for n in range(150)]
        target = ("http", "t.example", 80, "/scrape")
        sizes = []

        def fetcher(scheme, host, port, request):
            raws = queried(request)
            sizes.append(len(raws))
            return answer({r: (3, 0) for r in raws})

        requests, outcome, _ = self.run_query({h: [target] for h in hashes}, fetcher)
        self.assertEqual((requests, sizes), (3, [74, 74, 2]))
        self.assertEqual(len(outcome), 150)
        self.assertLessEqual(max(sizes), scrape.BATCH)

    def test_first_tracker_that_lists_a_torrent_wins_and_later_ones_are_not_asked(self):
        a, b, c = [("http", f"{n}.example", 80, "/scrape") for n in "abc"]
        seen = []

        def fetcher(scheme, host, port, request):
            seen.append(host)
            return {"a.example": answer({}), "b.example": answer({bytes.fromhex("11" * 20): (6, 2)})}[host]

        h = "11" * 20
        requests, outcome, trail = self.run_query({h: [a, b, c]}, fetcher)
        self.assertEqual(seen, ["a.example", "b.example"])
        self.assertEqual(outcome[h], {"seeders": 6, "leechers": 2, "host": "b.example"})
        self.assertEqual(trail[h], [{"host": "a.example", "error": "not_listed"}])

    def test_at_most_three_trackers_are_ever_contacted_per_torrent(self):
        targets = [("http", f"{n}.example", 80, "/scrape") for n in "abcde"]
        seen = []

        def fetcher(scheme, host, port, request):
            seen.append(host)
            raise KuraError("scrape_unavailable", "down")

        h = "22" * 20
        _, outcome, trail = self.run_query({h: targets}, fetcher)
        self.assertEqual(seen, ["a.example", "b.example", "c.example"])
        self.assertNotIn(h, outcome)
        self.assertEqual([t["error"] for t in trail[h]], ["scrape_unavailable"] * 3)

    def test_a_failed_tracker_is_not_retried_for_later_batches(self):
        target = ("http", "t.example", 80, "/scrape")
        calls = []

        def fetcher(scheme, host, port, request):
            calls.append(1)
            raise KuraError("unsafe_redirect", "no")

        hashes = [f"{n:040x}" for n in range(100)]
        requests, outcome, trail = self.run_query({h: [target] for h in hashes}, fetcher)
        self.assertEqual((requests, len(calls)), (1, 1))
        self.assertEqual(trail[hashes[-1]], [{"host": "t.example", "error": "tracker_failed"}])

    def test_request_budget_stops_the_run_and_marks_the_rest(self):
        targets = [("http", f"{n}.example", 80, "/scrape") for n in "abc"]
        work = {f"{n:040x}": [targets[n]] for n in range(3)}
        calls = []

        def fetcher(scheme, host, port, request):
            calls.append(host)
            return answer({r: (5, 5) for r in queried(request)})

        requests, outcome, _ = self.run_query(work, fetcher, max_requests=2)
        self.assertEqual((requests, calls), (2, ["a.example", "b.example"]))
        self.assertEqual(outcome[f"{2:040x}"], {"reason": "request_budget"})

    def test_requests_to_one_host_are_paced(self):
        target = ("http", "t.example", 80, "/scrape")
        sleeps, now = [], [1000.0]

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        fetcher = Mock(return_value=answer({}))
        hashes = [f"{n:040x}" for n in range(80)]
        self.run_query({h: [target] for h in hashes}, fetcher, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(sleeps, [scrape.HOST_INTERVAL])


class Library(unittest.TestCase):
    """scrape() against a real manifest database built from a synthetic library."""

    def setUp(self):
        self.lab = Lab(self)
        self.repo = ManifestRepo(self.lab.store.db)
        self.a = self.add("Kura A", announce="http://one.example/announce",
                          announce_list=[["http://one.example/announce"], ["http://two.example/announce"]])
        self.b = self.add("Kura B", announce="http://one.example/announce",
                          announce_list=[["http://one.example/announce"], ["http://two.example/announce"]])
        self.down = self.add("Kura Down", announce="http://down.example/announce")
        self.bare = self.add("Kura Bare")
        self.private = self.add("Kura Private", private=True,
                                announce="http://private.example/announce?passkey=SECRETKEY")
        self.denied = self.add("Kura Denied", announce="http://blocked.example/announce")
        self.lab.build()
        self.table = {"one.example": {self.a.infohash: (5, 1)},
                      "two.example": {self.b.infohash: (2, 0)}}
        self.requests = []

    def add(self, name, **kw):
        built = self.lab.add(name, GOOD, **kw)
        age(self.lab.lib / name)
        return built

    def fetcher(self, scheme, host, port, target):
        self.requests.append((scheme, host, port, target))
        if host == "down.example":
            raise KuraError("scrape_unavailable", "down")
        table = self.table.get(host, {})
        return answer({bytes.fromhex(h): table[h] for h in (r.hex() for r in queried(target))
                       if h in table})

    def scrape(self, **kw):
        return scrape.scrape(self.lab.store, library="home", fetcher=self.fetcher,
                             sleep=lambda s: None, **kw)

    def test_results_are_recorded_per_public_torrent(self):
        out = self.scrape()
        self.assertEqual(out["counts"], {"ok": 2, "unavailable": 2})
        self.assertEqual(out["skipped"], {"titles_not_reacquirable": 2, "torrents_protected": 0})
        a, b = self.repo.latest_swarm(self.a.infohash), self.repo.latest_swarm(self.b.infohash)
        self.assertEqual((a["status"], a["seeders"], a["leechers"], a["detail"]["tracker"]),
                         ("ok", 5, 1, "one.example"))
        self.assertEqual((b["status"], b["seeders"], b["detail"]["tracker"]), ("ok", 2, "two.example"))
        down = self.repo.latest_swarm(self.down.infohash)
        self.assertEqual((down["status"], down["seeders"], down["detail"]["reason"]),
                         ("unavailable", None, "tracker_failed"))
        bare = self.repo.latest_swarm(self.bare.infohash)
        self.assertEqual((bare["status"], bare["detail"]["reason"]), ("unavailable", "no_tracker"))
        self.assertEqual(out["requests"], len(self.requests))
        self.assertEqual({r[1] for r in self.requests}, {"one.example", "two.example", "down.example"})
        self.assertTrue(all(r[0] == "http" and r[3].startswith("/scrape?info_hash=") for r in self.requests))

    def test_protected_and_private_torrents_are_never_scraped_and_get_no_row(self):
        out = self.scrape()
        for built in (self.private, self.denied):
            self.assertIsNone(self.repo.latest_swarm(built.infohash))
            self.assertNotIn(built.infohash, {r["infohash"] for r in out["results"]})
        self.assertFalse({"private.example", "blocked.example"} & {r[1] for r in self.requests})
        self.assertNotIn("SECRETKEY", json.dumps(out) + repr(self.requests))
        raw = {h for r in self.requests for h in queried(r[3])}
        self.assertFalse({bytes.fromhex(self.private.infohash), bytes.fromhex(self.denied.infohash)} & raw)

    def test_a_protected_torrent_that_slips_through_selection_is_still_skipped(self):
        with patch("kura.scrape._candidates",
                   return_value=([self.private.infohash, self.denied.infohash],
                                 {"titles_not_reacquirable": 0, "torrents_protected": 0})):
            out = self.scrape()
        self.assertEqual((self.requests, out["counts"], out["skipped"]["torrents_protected"]),
                         ([], {"ok": 0, "unavailable": 0}, 2))
        self.assertIsNone(self.repo.latest_swarm(self.private.infohash))
        for row in ({"private": True, "protection": {"protected": False}},
                    {"private": False, "protection": {"protected": True}},
                    {"private": False, "protection": {}}, {"private": False, "protection": None}):
            self.assertFalse(scrape._eligible(row))
        self.assertFalse(scrape._eligible(None))

    def test_no_url_is_stored_or_returned_only_hosts(self):
        out = self.scrape()
        text = json.dumps(out) + json.dumps(self.lab.store.db.execute(
            "SELECT detail FROM swarm_checks").fetchall())
        self.assertNotIn("://", text)
        self.assertNotIn("/announce", text)
        self.assertNotIn("/scrape", text)
        self.assertIn("one.example", text)

    def test_feeds_the_quarantine_plan_three_seeders_in_two_out(self):
        self.table["one.example"][self.b.infohash] = (2, 0)
        self.scrape()
        result = quarantine.plan(self.lab.store, library="home", titles=["main/Kura A", "main/Kura B"])
        plan = result["plan"]
        self.assertEqual([m["from"] for m in plan["moves"]], ["Kura A"])
        self.assertEqual((plan["moves"][0]["seeders"], plan["moves"][0]["swarm"]), (5, "ok"))
        self.assertEqual(plan["refused"], [{"title": "main/Kura B", "status": "reacquirable",
                                            "reasons": ["swarm_low_seeders"]}])
        exactly_three = scrape.scrape(self.lab.store, library="home", titles=["main/Kura B"],
                                      fetcher=lambda *a: answer({bytes.fromhex(self.b.infohash): (3, 0)}),
                                      sleep=lambda s: None)
        self.assertEqual(exactly_three["counts"]["ok"], 1)
        plan = quarantine.plan(self.lab.store, library="home", titles=["main/Kura B"])["plan"]
        self.assertEqual([m["from"] for m in plan["moves"]], ["Kura B"])

    def test_unavailable_scrape_keeps_the_plan_refusing_the_title(self):
        self.scrape()
        plan = quarantine.plan(self.lab.store, library="home", titles=["main/Kura Down", "main/Kura Bare"])["plan"]
        self.assertEqual(plan["moves"], [])
        self.assertEqual({r["title"]: r["reasons"] for r in plan["refused"]},
                         {"main/Kura Down": ["swarm_unavailable"], "main/Kura Bare": ["swarm_unavailable"]})

    def test_title_filter_and_unknown_library(self):
        out = self.scrape(titles=["main/Kura A"])
        self.assertEqual(out["counts"], {"ok": 1, "unavailable": 0})
        self.assertIsNone(self.repo.latest_swarm(self.b.infohash))
        with self.assertRaises(KuraError) as caught:
            scrape.scrape(self.lab.store, library="never-built", fetcher=self.fetcher)
        self.assertEqual(caught.exception.code, "not_found")

    def test_malformed_and_hostile_tracker_answers_are_recorded_unavailable(self):
        for body in (b"garbage", b"d14:failure reason1:xe", b"d5:filesli1eee"):
            out = scrape.scrape(self.lab.store, library="home", titles=["main/Kura Down"],
                                fetcher=lambda *a, body=body: body, sleep=lambda s: None)
            self.assertEqual(out["counts"], {"ok": 0, "unavailable": 1})
        row = self.repo.latest_swarm(self.down.infohash)
        self.assertEqual((row["status"], row["seeders"]), ("unavailable", None))

    def test_a_transport_that_raises_something_else_is_a_bug_not_swallowed(self):
        def boom(*a):
            raise RuntimeError("bug")
        with self.assertRaises(RuntimeError):
            scrape.scrape(self.lab.store, library="home", fetcher=boom, sleep=lambda s: None)

    def test_interrupt_keeps_what_was_already_answered(self):
        def fetcher(scheme, host, port, target):
            if host == "two.example":
                raise KeyboardInterrupt
            return self.fetcher(scheme, host, port, target)

        with self.assertRaises(KeyboardInterrupt):
            scrape.scrape(self.lab.store, library="home", fetcher=fetcher, sleep=lambda s: None)
        self.assertEqual(self.repo.latest_swarm(self.a.infohash)["seeders"], 5)
        self.assertIsNone(self.repo.latest_swarm(self.b.infohash))

    def test_state_lock_is_held_for_the_whole_run(self):
        seen = []

        def fetcher(scheme, host, port, target):
            with self.assertRaises(KuraError) as caught:
                with self.lab.store.lock():
                    pass
            seen.append(caught.exception.code)
            return self.fetcher(scheme, host, port, target)

        scrape.scrape(self.lab.store, library="home", fetcher=fetcher, sleep=lambda s: None)
        self.assertTrue(seen and set(seen) == {"busy"})
        with self.lab.store.lock():         # released afterwards
            pass

    def test_a_torrent_protected_during_the_run_cannot_be_reclassified_underneath_it(self):
        """The lock refuses a concurrent build (which is what reclassifies torrents)."""
        with self.lab.store.lock():
            with self.assertRaises(KuraError) as caught:
                self.scrape()
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(self.requests, [])
        self.assertIsNone(self.repo.latest_swarm(self.a.infohash))

    def test_argument_validation(self):
        for kw in ({"max_requests": 0}, {"max_requests": True}, {"max_requests": 10_001},
                   {"titles": "main/Kura A"}, {"library": ""}):
            args = {"library": "home", "titles": None, "max_requests": 5, **kw}
            with self.assertRaises(KuraError) as caught:
                scrape.check_args(**args)
            self.assertEqual(caught.exception.code, "invalid_arguments", kw)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        self.built = self.lab.add("Kura Good", GOOD, announce="http://one.example/announce")
        age(self.lab.lib / "Kura Good")
        self.home = str(self.lab.tmp / "home")
        os.mkdir(self.home)
        self.cli("manifest", "build", "--library", "home", "--root", f"main={self.lab.lib}",
                 "--torrents", str(self.lab.torrents), "--tracker-denylist", str(self.lab.deny),
                 "--protected", str(self.lab.prot), "--no-anilist")

    def cli(self, *argv):
        buffer = io.StringIO()
        with patch.dict(os.environ, {"HOME": self.home}), patch("sys.argv", ["kura", *argv]), \
             contextlib.redirect_stdout(buffer):
            try:
                cli.main()
            except SystemExit as exited:
                return exited.code, json.loads(buffer.getvalue())

    def test_envelope_then_plan_without_skip_swarm(self):
        body = answer({bytes.fromhex(self.built.infohash): (8, 3)})
        with network([PUBLIC], http(200, body)) as (calls, sockets):
            code, out = self.cli("manifest", "swarm", "--library", "home")
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "manifest"))
        self.assertEqual(out["data"]["counts"], {"ok": 1, "unavailable": 0})
        self.assertEqual(out["data"]["results"][0]["tracker"], "one.example")
        self.assertEqual(calls, [("dns", "one.example"), ("connect", (PUBLIC, 80))])
        code, out = self.cli("manifest", "plan", "--library", "home")
        self.assertEqual([m["from"] for m in out["data"]["plan"]["moves"]], ["Kura Good"])
        self.assertEqual(out["data"]["plan"]["moves"][0]["seeders"], 8)

    def test_private_dns_is_recorded_unavailable_not_fatal(self):
        with network(["10.0.0.5"]) as (calls, _):
            code, out = self.cli("manifest", "swarm", "--library", "home")
        self.assertEqual((code, out["data"]["counts"]), (0, {"ok": 0, "unavailable": 1}))
        self.assertEqual(out["data"]["results"][0]["reason"], "tracker_failed")
        self.assertFalse([c for c in calls if c[0] == "connect"])

    def test_errors(self):
        for argv, expected in ((["--library", "home", "--max-requests", "0"], "invalid_arguments"),
                               (["--library", "never-built"], "not_found")):
            code, out = self.cli("manifest", "swarm", *argv)
            self.assertEqual((code, out["error"]["code"]), (2, expected), argv)
        code, out = self.cli("manifest", "swarm")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))


if __name__ == "__main__":
    unittest.main()
