"""Multi-file torrent metadata: bounded, canonical, traversal-proof, never leaks announce URLs."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from kura.models import KuraError
from kura.torrent import decode, encode
from kura.torrentmeta import parse, release_group, sanitized_info, tracker_host

import torrentgen as g

FILES = [("Season 1/ep01.mkv", g.payload("a", 40000)),
         ("Season 1/ep02.mkv", g.payload("b", 50000)),
         ("extras/notes.txt", g.payload("c", 700))]


class ParseCase(unittest.TestCase):
    def reject(self, data, code=None):
        with self.assertRaises(KuraError) as caught:
            parse(data)
        if code:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_multifile_round_trip_and_independent_infohash(self):
        built = g.build("Kura Sample Series", FILES, announce="http://tracker.example/announce")
        meta = parse(built.data)
        raw = decode(built.data)[b"info"]
        self.assertEqual(meta.infohash, hashlib.sha1(encode(raw)).hexdigest())
        self.assertEqual(meta.infohash, built.infohash)
        self.assertTrue(meta.multi_file)
        self.assertEqual(meta.total_length, 90700)
        self.assertEqual([f.path for f in meta.files], [
            ("Kura Sample Series", "Season 1", "ep01.mkv"),
            ("Kura Sample Series", "Season 1", "ep02.mkv"),
            ("Kura Sample Series", "extras", "notes.txt")])
        self.assertEqual([f.offset for f in meta.files], [0, 40000, 90000])
        self.assertEqual([f.length for f in meta.files], [40000, 50000, 700])
        self.assertFalse(any(f.pad for f in meta.files))
        self.assertEqual(meta.descriptor_sha256, hashlib.sha256(built.data).hexdigest())
        self.assertFalse(meta.private)
        self.assertTrue(meta.announce_valid)

    def test_single_file(self):
        meta = parse(g.build("Kura Sample Series.mkv", size=20000).data)
        self.assertFalse(meta.multi_file)
        self.assertEqual(meta.files[0].path, ("Kura Sample Series.mkv",))
        self.assertEqual(meta.total_length, 20000)

    def test_zero_length_files_and_empty_torrent(self):
        meta = parse(g.build("Kura Sample Series", [("a.txt", b""), ("b.txt", g.payload("b", 10))]).data)
        self.assertEqual([f.length for f in meta.files], [0, 10])
        self.assertEqual(parse(g.build("Kura Sample Series", [("a.txt", b"")]).data).total_length, 0)

    def test_path_utf8_preferred(self):
        files = [{b"length": 10, b"path": [b"legacy.txt"], b"path.utf-8": ["\u30c6\u30b9\u30c8.txt".encode()]}]
        data = g.build("Kura Sample Series", [("a.txt", g.payload("a", 10))], info_extra={b"files": files}).data
        self.assertEqual(parse(data).files[0].path[-1], "\u30c6\u30b9\u30c8.txt")

    def test_pad_files_marked(self):
        built = g.build("Kura Sample Series", FILES, pad=True)
        meta = parse(built.data)
        pads = [f for f in meta.files if f.pad]
        self.assertEqual(len(pads), 2)
        self.assertTrue(all(f.offset % built.piece_length == 0 for f in meta.files if not f.pad))
        self.assertEqual(sum(1 for f in meta.files if not f.pad), 3)
        self.assertEqual(meta.json()["pad_files"], 2)
        self.assertEqual(meta.json()["file_count"], 3)

    def test_pad_files_may_repeat_but_real_duplicates_may_not(self):
        same = [("a.bin", g.payload("a", 16000)), ("b.bin", g.payload("b", 16000)), ("c.bin", g.payload("c", 100))]
        meta = parse(g.build("Kura Sample Series", same, pad=True).data)
        self.assertEqual(len([f for f in meta.files if f.pad]), 2)
        self.reject(g.build("Kura Sample Series", [("a.bin", b"x"), ("a.bin", b"y")]).data, "unsafe_torrent")

    def test_file_and_directory_conflict_rejected(self):
        self.reject(g.build("Kura Sample Series", [("a", b"x"), ("a/b", b"y")]).data, "unsafe_torrent")
        self.reject(g.build("Kura Sample Series", [("a/b", b"y"), ("a", b"x")]).data, "unsafe_torrent")

    def test_hostile_paths_rejected(self):
        for bad in (("..", "x"), ("a", ".."), (".", "x"), ("", "x"), ("a/b",), ("a\\b",), ("a\0b",),
                    ("/etc", "passwd"), ("a\nb",), ("a\x7f",), ("x" * 256,)):
            data = g.build("Kura Sample Series", [(bad, b"payload")]).data
            with self.subTest(path=bad):
                self.reject(data, "unsafe_torrent")

    def test_hostile_name_rejected(self):
        for bad in ("..", ".", "a/b", "a\\b", "a\0b", "x" * 256):
            with self.subTest(name=bad):
                self.reject(g.build(bad, [("a.txt", b"x")]).data, "unsafe_torrent")
        self.reject(g.build("ok", [("a.txt", b"x")], info_extra={b"name": b""}).data, "unsafe_torrent")
        self.reject(g.build("ok", [("a.txt", b"x")], info_extra={b"name": b"\xff\xfe"}).data, "unsafe_torrent")

    def test_bad_path_shapes_rejected(self):
        for files in ([{b"length": 1, b"path": []}], [{b"length": 1, b"path": b"a.txt"}],
                      [{b"length": 1}], [{b"length": -1, b"path": [b"a"]}],
                      [{b"length": b"1", b"path": [b"a"]}], [], [b"a"], [{b"length": 1, b"path": [5]}],
                      [{b"length": 1, b"path": [b"\xff"]}]):
            data = g.build("Kura Sample Series", [("a", b"x")], info_extra={b"files": files}).data
            with self.subTest(files=files):
                self.reject(data)

    def test_length_and_files_together_rejected(self):
        self.reject(g.build("Kura Sample Series", [("a", b"x")], info_extra={b"length": 1}).data, "unsafe_torrent")
        self.reject(g.build("n", size=10, info_extra={b"length": None}).data)

    def test_symlink_entries_unsupported(self):
        files = [{b"length": 1, b"path": [b"a"], b"attr": b"l", b"symlink path": [b"b"]}]
        self.reject(g.build("Kura Sample Series", [("a", b"x")], info_extra={b"files": files}).data,
                    "unsupported_torrent")
        self.reject(g.build("n.mkv", size=10, info_extra={b"symlink path": [b"x"]}).data, "unsupported_torrent")

    def test_hybrid_uses_v1_data(self):
        built = g.build("Kura Sample Series", FILES, hybrid=True)
        meta = parse(built.data)
        self.assertTrue(meta.hybrid)
        self.assertEqual(meta.infohash, built.infohash)
        self.assertFalse(parse(g.build("Kura Sample Series", FILES).data).hybrid)

    def test_pure_v2_rejected(self):
        self.reject(g.build("Kura Sample Series", FILES, pure_v2=True).data, "unsupported_torrent")
        self.reject(g.build("Kura Sample Series", FILES, info_extra={b"pieces": None}).data, "unsafe_torrent")

    def test_piece_count_and_length_validated(self):
        built = g.build("Kura Sample Series", FILES)
        self.reject(g.build("Kura Sample Series", FILES,
                            info_extra={b"pieces": built.info[b"pieces"][:-20]}).data, "unsafe_torrent")
        self.reject(g.build("Kura Sample Series", FILES,
                            info_extra={b"pieces": built.info[b"pieces"] + b"\0" * 20}).data, "unsafe_torrent")
        self.reject(g.build("Kura Sample Series", FILES,
                            info_extra={b"pieces": built.info[b"pieces"] + b"\0"}).data, "unsafe_torrent")
        for length in (0, 16383, 64 * 1024**2 + 1, -1):
            with self.subTest(length=length):
                self.reject(g.build("Kura Sample Series", FILES, info_extra={b"piece length": length}).data,
                            "unsafe_torrent")
        self.reject(g.build("Kura Sample Series", FILES, info_extra={b"piece length": b"16384"}).data)

    def test_private_flag(self):
        self.assertTrue(parse(g.build("n.mkv", private=True).data).private)
        self.assertFalse(parse(g.build("n.mkv", private=0).data).private)
        self.reject(g.build("n.mkv", private=2).data, "unsafe_torrent")
        self.reject(g.build("n.mkv", info_extra={b"private": b"1"}).data, "unsafe_torrent")

    def test_source_tag(self):
        self.assertEqual(parse(g.build("n.mkv", source="SYN").data).source, "SYN")
        self.assertIsNone(parse(g.build("n.mkv").data).source)
        self.reject(g.build("n.mkv", info_extra={b"source": b"\xff"}).data, "unsafe_torrent")
        self.reject(g.build("n.mkv", info_extra={b"source": 5}).data, "unsafe_torrent")

    def test_noncanonical_rejected(self):
        built = g.build("n.mkv", size=100)
        info = built.info
        swapped = (b"d4:infod4:name" + str(len(info[b"name"])).encode() + b":" + info[b"name"] +
                   b"6:lengthi100e12:piece lengthi16384e6:pieces20:" + info[b"pieces"] + b"ee")
        self.reject(swapped, "invalid_torrent")
        self.reject(built.data + b"x", "invalid_torrent")
        self.reject(b"", "invalid_torrent")
        self.reject(b"not bencode", "invalid_torrent")
        self.reject(encode([b"info"]), "invalid_torrent")
        self.reject(encode({b"info": b"x"}), "invalid_torrent")

    def test_limits(self):
        big = g.build("Kura Sample Series", [("a", g.payload("a", 100))],
                      root_extra={b"comment": b"x" * (33 * 1024**2)}).data
        self.reject(big, "invalid_torrent")
        # Acquisition limits are untouched: 4 MiB, depth 12, 50k nodes by default.
        mid = g.build("Kura Sample Series", [("a", g.payload("a", 100))],
                      root_extra={b"comment": b"x" * (5 * 1024**2)}).data
        self.assertEqual(len(parse(mid).files), 1)
        with self.assertRaises(KuraError):
            decode(mid)
        deep = b"l" * 20 + b"e" * 20
        with self.assertRaises(KuraError):
            decode(deep)
        self.assertEqual(decode(deep, max_depth=32), [[[[[[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]]]]])
        nested = g.build("Kura Sample Series", [("a", b"x")], root_extra={b"x": [[[[[[[[[[[[[[[[[[[[[[b"y"]]]]]]]]]]]]]]]]]]]]]]})
        self.assertEqual(len(parse(nested.data).files), 1)
        too_deep = g.build("Kura Sample Series", [("a", b"x")], root_extra={b"x": eval("[" * 40 + "]" * 40)})
        self.reject(too_deep.data, "invalid_torrent")
        wide = g.build("Kura Sample Series", [("a", b"x")], root_extra={b"x": [1] * 500_001})
        self.reject(wide.data, "invalid_torrent")
        with self.assertRaises(KuraError):
            decode(encode([1] * 60000))
        self.assertEqual(len(decode(encode([1] * 60000), max_nodes=100000)), 60000)

    def test_many_files(self):
        files = [(f"d{i // 100}/f{i}.bin", b"") for i in range(5000)]
        meta = parse(g.build("Kura Sample Series", files).data)
        self.assertEqual(len(meta.files), 5000)

    def test_announce_collection(self):
        meta = parse(g.build("n.mkv", announce="http://a.example/announce",
                             announce_list=[["http://a.example/announce", "udp://b.example:80"],
                                            ["https://c.example/x"]]).data)
        self.assertEqual(meta.announce_urls, ("http://a.example/announce", "udp://b.example:80",
                                              "https://c.example/x"))
        self.assertTrue(meta.announce_valid)
        self.assertEqual(parse(g.build("n.mkv").data).announce_urls, ())

    def test_malformed_announce_flagged_not_fatal(self):
        for extra in ({b"announce-list": b"x"}, {b"announce-list": [b"x"]}, {b"announce-list": [[5]]},
                      {b"announce": 5}, {b"announce": b"http://a.example/\xff"},
                      {b"announce": b"http://a.example/ x"}, {b"announce": b"x" * 2049},
                      {b"announce-list": [[b"http://a%d.example/" % i for i in range(201)]]}):
            with self.subTest(extra=extra):
                meta = parse(g.build("n.mkv", root_extra=extra).data)
                self.assertFalse(meta.announce_valid)
        many = parse(g.build("n.mkv", root_extra={b"announce-list": [[b"http://a%d.example/" % i for i in range(201)]]}).data)
        self.assertEqual(len(many.announce_urls), 200)

    def test_json_never_contains_urls_or_passkeys(self):
        built = g.build("Kura Sample Series", FILES,
                        announce="http://user:pw@tracker.example/abcdefghijklmnopqrstuvwx/announce?passkey=deadbeefcafe",
                        announce_list=[["udp://other.test:1337/announce?passkey=feedface"]])
        meta = parse(built.data)
        text = json.dumps(meta.json())
        for secret in ("passkey", "deadbeef", "feedface", "user", "pw@", "abcdefghijklmnopqrstuvwx", "http", "udp:"):
            self.assertNotIn(secret, text)
        self.assertEqual(meta.json()["tracker_hosts"], ["other.test", "tracker.example"])
        self.assertNotIn("passkey", repr(meta))

    def test_sanitized_info_drops_urls_and_keeps_infohash(self):
        built = g.build("Kura Sample Series", FILES, announce="http://tracker.example/a?passkey=deadbeefcafe",
                        root_extra={b"url-list": [b"http://web.example/"], b"comment": b"hi"})
        clean = sanitized_info(built.data)
        self.assertEqual(clean, encode({b"info": built.info}))
        self.assertEqual(sanitized_info(parse(built.data)), clean)
        self.assertNotIn(b"passkey", clean)
        self.assertNotIn(b"announce", clean)
        self.assertEqual(parse(clean).infohash, built.infohash)

    def test_tracker_host_normalization(self):
        self.assertEqual(tracker_host("http://Tracker.Example:8080/announce"), "tracker.example")
        self.assertEqual(tracker_host("udp://tracker.example./"), "tracker.example")
        self.assertEqual(tracker_host("http://bücher.example/a"), "xn--bcher-kva.example")
        self.assertEqual(tracker_host("http://[::1]:80/a"), "::1")
        for bad in ("tracker.example/announce", "", "http://", "http://a..example/", "http://a%65.example/",
                    "http://good.example\\@bad.test/", "http://a.example/ x", "http://a b.example/",
                    "http://[bad/", "http://t.example:invalid/a", "http://t.example:99999/a",
                    "1http://t.example/a", "://t.example/a"):
            with self.subTest(url=bad):
                self.assertIsNone(tracker_host(bad))

    def test_release_group(self):
        self.assertEqual(release_group("[Kura] Kura Sample Series - 01"), "Kura")
        self.assertEqual(release_group("[ Spaced ] x"), "Spaced")
        self.assertIsNone(release_group("Kura Sample Series [Kura]"))
        self.assertIsNone(release_group("[] x"))
        self.assertIsNone(release_group("[" + "a" * 81 + "] x"))
        self.assertEqual(release_group("[" + "a" * 80 + "] x"), "a" * 80)
        self.assertIsNone(release_group("[a\x00b] x"))

    def test_write_tree_matches_descriptor(self):
        built = g.build("Kura Sample Series", FILES, pad=True)
        with tempfile.TemporaryDirectory() as tmp:
            top = built.write_tree(Path(tmp))
            for f in parse(built.data).files:
                if f.pad:
                    continue
                self.assertEqual((Path(tmp).joinpath(*f.path)).stat().st_size, f.length)
            self.assertEqual(top, Path(tmp) / "Kura Sample Series")
            self.assertFalse((top / ".pad").exists())


if __name__ == "__main__":
    unittest.main()
