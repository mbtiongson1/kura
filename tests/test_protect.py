"""Protection classifier: every hostile or ambiguous case is protected; magnets never leak credentials."""
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from kura.models import KuraError
from kura.protect import (Protection, ProtectionPolicy, classify, classify_bytes, load_policy,
                          magnet, path_protected)
from kura.torrentmeta import parse

import torrentgen as g

HASH = "0123456789abcdef0123456789ABCDEF01234567"


class ProtectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def policy(self, deny="", prot=None):
        return load_policy(self.write("deny.txt", deny) if deny is not None else None,
                           self.write("prot.txt", prot) if prot is not None else None)

    def check(self, policy=None, **kwargs):
        meta = parse(g.build("Kura Sample Series", [("a.mkv", g.payload("a", 100))], **kwargs).data)
        return meta, classify(meta, policy or ProtectionPolicy())

    def test_public_torrent_with_no_policy_is_not_protected(self):
        meta, result = self.check(announce="http://tracker.example/announce")
        self.assertEqual(result, Protection(False, ()))
        self.assertFalse(result.protected)

    def test_private_flag_with_no_announce(self):
        _, result = self.check(private=True)
        self.assertEqual(result, Protection(True, ("private_flag",)))

    def test_hybrid_private_still_protected(self):
        _, result = self.check(private=True, hybrid=True)
        self.assertIn("private_flag", result.reasons)

    def test_denylist_matches_host_and_subdomains_on_dot_boundary(self):
        policy = self.policy("# trackers\ntracker.example\nOther.Test:  # trailing\n")
        for url in ("http://tracker.example/a", "http://TRACKER.EXAMPLE:80/a", "udp://x.tracker.example:1/",
                    "http://a.b.tracker.example/", "http://tracker.example./a", "http://other.test/a"):
            with self.subTest(url=url):
                self.assertEqual(self.check(policy, announce=url)[1].reasons, ("tracker_denylist",))
        for url in ("http://notexample.test/a", "http://nottracker.example/a", "http://tracker.example.evil.test/",
                    "http://example/a"):
            with self.subTest(url=url):
                self.assertNotIn("tracker_denylist", self.check(policy, announce=url)[1].reasons)

    def test_denylist_matches_announce_list_and_idna(self):
        policy = self.policy("bücher.example\n")
        result = self.check(policy, announce="http://fine.example/a",
                            announce_list=[["http://fine.example/a"], ["http://sub.xn--bcher-kva.example/a"]])[1]
        self.assertEqual(result.reasons, ("tracker_denylist",))

    def test_source_tag_denylist_case_insensitive(self):
        policy = self.policy("source:SYN\n")
        self.assertEqual(self.check(policy, source="syn")[1].reasons, ("source_denylist",))
        self.assertEqual(self.check(policy, source=" Syn")[1].reasons, ("source_denylist",))
        self.assertFalse(self.check(policy, source="SYNC")[1].protected)
        self.assertFalse(self.check(policy)[1].protected)

    def test_protected_infohash_normalized(self):
        policy = self.policy("tracker.example", HASH + "\n")
        self.assertEqual(policy.infohashes, frozenset({HASH.lower()}))
        meta = parse(g.build("Kura Sample Series", [("a.mkv", g.payload("a", 100))]).data)
        again = self.policy(None, meta.infohash.upper() + "\n")
        self.assertEqual(classify(meta, again).reasons, ("protected_infohash",))
        self.assertFalse(classify(meta, policy).protected)

    def test_malformed_announce_is_protected(self):
        for extra in ({b"announce-list": [[b"http://ok.example/a"], b"bad"]},
                      {b"announce-list": [[5]]}, {b"announce-list": b"x"},
                      {b"announce": b"not a url"}, {b"announce": b"tracker.example/a"},
                      {b"announce": b"http://a%65.example/a"}, {b"announce": b"http://tracker.example:invalid/announce"},
                      {b"announce": b"http://tracker.example:99999/announce"},
                      {b"announce": b"1http://tracker.example/announce"}, {b"announce": b"http://a.example\\@b.test/"},
                      {b"announce-list": [[b"http://a.example/" + b"x" * 2100]]}):
            with self.subTest(extra=extra):
                meta, result = self.check(root_extra=extra)
                self.assertEqual(result.reasons, ("announce_unparsable",))

    def test_multiple_reasons_in_stable_order(self):
        policy = self.policy("tracker.example\nsource:SYN\n", HASH + "\n")
        meta = parse(g.build("Kura Sample Series", [("a.mkv", g.payload("a", 100))], private=True, source="SYN",
                             announce="http://tracker.example/a", root_extra={b"announce-list": [[5]]}).data)
        policy = ProtectionPolicy(policy.tracker_hosts, policy.source_tags, frozenset({meta.infohash}))
        self.assertEqual(classify(meta, policy).reasons, (
            "private_flag", "tracker_denylist", "source_denylist", "protected_infohash", "announce_unparsable"))

    def test_classify_bytes_fails_closed(self):
        policy = ProtectionPolicy()
        for data in (b"", b"garbage", b"d4:infod4:name1:xee", g.build("x", pure_v2=True).data,
                     g.build("n", [("../a", b"x")]).data, g.build("n.mkv", private=2).data):
            with self.subTest(data=data[:20]):
                meta, result = classify_bytes(data, policy)
                self.assertIsNone(meta)
                self.assertEqual(result, Protection(True, ("unparsable",)))
        meta, result = classify_bytes(g.build("n.mkv").data, policy)
        self.assertIsNotNone(meta)
        self.assertFalse(result.protected)

    def test_classify_bytes_survives_unexpected_exceptions(self):
        from unittest.mock import patch
        with patch("kura.protect.parse", side_effect=RuntimeError("bug")):
            self.assertEqual(classify_bytes(b"x", ProtectionPolicy())[1], Protection(True, ("unparsable",)))

    def test_magnet_for_public_torrent(self):
        meta, result = self.check(announce="http://tracker.example/announce",
                                  announce_list=[["udp://t2.example:1337/announce"], ["https://t3.test/announce"]])
        link = magnet(meta, result)
        parts = urlsplit(link)
        self.assertEqual(parts.scheme, "magnet")
        query = parse_qs(parts.query)
        self.assertEqual(query["xt"], ["urn:btih:" + meta.infohash])
        self.assertEqual(query["dn"], ["Kura Sample Series"])
        self.assertEqual(query["tr"], ["http://tracker.example/announce", "udp://t2.example:1337/announce",
                                       "https://t3.test/announce"])

    def test_magnet_refused_when_protected_or_private(self):
        meta, result = self.check(private=True, announce="http://tracker.example/a")
        self.assertIsNone(magnet(meta, result))
        meta, _ = self.check(announce="http://tracker.example/a")
        self.assertIsNone(magnet(meta, Protection(True, ("tracker_denylist",))))
        # Even a caller passing an "unprotected" verdict cannot extract a private torrent's magnet.
        meta, _ = self.check(private=True)
        self.assertIsNone(magnet(meta, Protection(False, ())))

    def test_passkey_urls_never_in_magnet_or_json(self):
        secret = "deadbeefcafe0123"
        urls = [f"http://tracker.example/announce?passkey={secret}",
                f"http://user:{secret}@tracker.example/announce",
                f"http://tracker.example/{secret}abcdefgh/announce",
                "http://tracker.example/shortsecret/announce", "http://tracker.example/k/announce",
                "http://tracker.example/announce/extra",
                f"http://tracker.example/announce#{secret}",
                f"http://tracker.example/announce?",
                "ftp://tracker.example/announce", "ws://tracker.example/announce",
                "http://tracker.example/announce", "http://tracker.example/announce.php", "udp://tracker.example:80"]
        meta, result = self.check(announce=urls[0], announce_list=[urls[1:]])
        self.assertFalse(result.protected)
        link = magnet(meta, result)
        self.assertNotIn(secret, link)
        self.assertNotIn("passkey", link)
        self.assertEqual(parse_qs(urlsplit(link).query)["tr"], ["http://tracker.example/announce",
                                                                      "http://tracker.example/announce.php",
                                                                      "udp://tracker.example:80"])
        self.assertNotIn(secret, repr(meta.json()))

    def test_policy_line_errors_carry_line_numbers(self):
        cases = [("tracker.example\n\nbad host\n", 3), ("# c\nhttp://tracker.example/\n", 2),
                 ("tracker.example\n*.example.test\n", 2), ("localhost\n", 1), ("a..b.example\n", 1),
                 ("source:\n", 1), ("-bad.example\n", 1), ("tracker.example:80\n", 1)]
        for text, line in cases:
            with self.subTest(text=text):
                with self.assertRaises(KuraError) as caught:
                    load_policy(self.write("d.txt", text), None)
                self.assertEqual(caught.exception.code, "invalid_policy")
                self.assertEqual(caught.exception.details["line"], line)
        for text, line in (("zz\n", 1), (HASH + "\npath:/abs\n", 2), ("path:a/../b\n", 1), ("path:\n", 1),
                           ("path:a//b\n", 1), (HASH[:-1] + "\n", 1), ("magnet:?xt=urn:btih:" + HASH + "\n", 1)):
            with self.subTest(text=text):
                with self.assertRaises(KuraError) as caught:
                    load_policy(None, self.write("p.txt", text))
                self.assertEqual(caught.exception.code, "invalid_policy")
                self.assertEqual(caught.exception.details["line"], line)

    def test_policy_file_limits_and_missing_file(self):
        with self.assertRaises(KuraError) as caught:
            load_policy(self.write("d.txt", "a.example\n" * 1001), None)
        self.assertEqual(caught.exception.code, "invalid_policy")
        self.assertEqual(len(load_policy(self.write("d.txt", "a.example\n" * 1000), None).tracker_hosts), 1)
        with self.assertRaises(KuraError):
            load_policy(self.dir / "missing.txt", None)
        bad = self.dir / "bin.txt"
        bad.write_bytes(b"\xff\xfe")
        with self.assertRaises(KuraError):
            load_policy(bad, None)
        with self.assertRaises(KuraError):
            load_policy(self.dir, None)

    def test_policy_flags_and_hash(self):
        empty = load_policy(None, None)
        self.assertEqual((empty.has_denylist, empty.has_protected_list), (False, False))
        both = self.policy("a.example\n", HASH)
        self.assertEqual((both.has_denylist, both.has_protected_list), (True, True))
        self.assertEqual(len(both.sha256), 64)
        reordered = self.policy("# note\n\nA.example  # x\n", HASH.lower() + "\n")
        self.assertEqual(both.sha256, reordered.sha256)
        self.assertNotEqual(both.sha256, self.policy("b.example\n", HASH).sha256)
        self.assertNotEqual(empty.sha256, self.policy("", None).sha256)  # an empty file is still a list
        self.assertTrue(empty.sha256)

    def test_path_prefixes(self):
        policy = self.policy(None, "path:Kura Sample Series\npath:other/dir/\n")
        self.assertEqual(policy.path_prefixes, ("Kura Sample Series", "other/dir"))
        for rel in ("Kura Sample Series", "Kura Sample Series/ep01.mkv", "other/dir/x"):
            self.assertTrue(path_protected(policy, rel), rel)
        for rel in ("Kura Sample Series 2", "other", "other/directory", "x/Kura Sample Series"):
            self.assertFalse(path_protected(policy, rel), rel)

    def test_no_io_outside_load_policy(self):
        from unittest.mock import patch
        meta, _ = self.check(announce="http://tracker.example/a")
        policy = ProtectionPolicy(("tracker.example",))
        with patch("builtins.open", side_effect=AssertionError("io")), \
                patch("pathlib.Path.open", side_effect=AssertionError("io")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("net")), \
                patch("socket.socket", side_effect=AssertionError("net")):
            classify(meta, policy)
            classify_bytes(g.build("n.mkv").data, policy)
            magnet(meta, Protection(False, ()))


if __name__ == "__main__":
    unittest.main()
