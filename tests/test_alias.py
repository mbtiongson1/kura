"""Alias resolution: bounded metadata, conservative matching, cached and offline-testable."""
from dataclasses import replace
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kura.anilist import ENDPOINT, Identity, AniList, choose, parse_response, request, validate_endpoint
from kura.core import Core
from kura.models import KuraError
from kura.releases import eligible
from kura.store import Store
import kura.cli as cli

import helpers

FIXTURE = json.dumps({"data": {"Page": {"media": [{
    "id": 186742, "format": "TV",
    "title": {"romaji": "Toaru Anbu no ITEM", "english": "A Certain Dark Item",
              "native": "\u3068\u3042\u308b\u6697\u90e8\u5c11\u5973"},
    "synonyms": ["A Certain Dark Side's Item", "Toaru Anbu no ITEM"],
}]}}}).encode()

NOT_FOUND = json.dumps({"data": {"Page": {"media": []}}}).encode()


def media(identifier, *titles):
    return {"id": identifier,
            "titles": [t for t in titles]}


class FakeAliases:
    """Stand-in resolver: no network, call counting, optional failure."""

    def __init__(self, identity=None, status="resolved", error=None):
        self.identity, self.status, self.error, self.calls = identity, status, error, []

    def resolve(self, title):
        self.calls.append(title)
        if self.error:
            raise self.error
        return self.identity, self.status


class AliasCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = patch("kura.safety.private_root", return_value=self.root / "state")
        self.paths.start()
        self.store = Store(self.root / "state")
        self.row, self.data = helpers.sample()
        self.identity = Identity(186742, ("Kura Sample", "A Certain Dark Sample"), "Kura Sample")

    def tearDown(self):
        self.store.db.close()
        self.paths.stop()
        self.tmp.cleanup()

    def provider(self, mapping):
        descriptor = self.data

        class Mapped:
            def search(self, title, episode, subtitles):
                return list(mapping.get(title, []))

            def descriptor(self, release):
                return descriptor
        return Mapped()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(KuraError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class ParseTests(unittest.TestCase):
    def test_fixture_titles_deduplicate_and_order(self):
        rows = parse_response(FIXTURE)
        self.assertEqual([r["id"] for r in rows], [186742])
        self.assertEqual(rows[0]["titles"][:3],
                         ["Toaru Anbu no ITEM", "A Certain Dark Item", "\u3068\u3042\u308b\u6697\u90e8\u5c11\u5973"])
        self.assertEqual(len(rows[0]["titles"]), 4)  # repeated synonym dropped

    def test_hostile_or_malformed_responses(self):
        for data in (b"", b"[]", b"null", b'{"errors":[{"message":"x"}]}',
                     b'{"data":null}', b'{"data":{"Page":{}}}',
                     b'{"data":{"Page":{"media":{}}}}', b"\xff\xfe"):
            with self.assertRaises(KuraError) as caught:
                parse_response(data)
            self.assertEqual(caught.exception.code, "alias_invalid")

    def test_unusable_media_rows_are_skipped_not_trusted(self):
        for item in ({"id": 0}, {"id": True}, {"id": 5}, "x", None,
                     {"id": 5, "title": {"romaji": ""}, "synonyms": []}):
            self.assertEqual(parse_response(json.dumps(
                {"data": {"Page": {"media": [item]}}}).encode()), [])

    def test_untrusted_field_types_and_bounds(self):
        data = json.dumps({"data": {"Page": {"media": [{
            "id": 7, "title": {"romaji": "  Spaced   Title ", "english": 5},
            "synonyms": ["a" * 500, "ok synonym", None, 3]}]}}}).encode()
        rows = parse_response(data)
        self.assertEqual(rows[0]["titles"], ["Spaced Title", "ok synonym"])
        with self.assertRaises(KuraError):
            parse_response(b"x" * (1024 * 1024))


class MatchTests(unittest.TestCase):
    def test_exact_beats_subset(self):
        rows = [media(1, "Dark Item"), media(2, "A Certain Dark Item")]
        identity, status = choose(rows, "dark item")
        self.assertEqual((identity.anilist_id, status), (1, "resolved"))
        self.assertEqual(identity.titles[0], "Dark Item")

    def test_partial_alias_resolves_to_full_title_first(self):
        identity, status = choose(parse_response(FIXTURE), "dark item")
        self.assertEqual(status, "resolved")
        self.assertEqual(identity.matched, "A Certain Dark Item")
        self.assertEqual(identity.titles[0], "A Certain Dark Item")
        self.assertIn("Toaru Anbu no ITEM", identity.titles)

    def test_equal_best_matches_are_refused(self):
        rows = [media(1, "A Certain Dark Item"), media(2, "A Certain Dark Item")]
        self.assertEqual(choose(rows, "dark item"), (None, "ambiguous"))

    def test_no_plausible_match_is_unresolved(self):
        self.assertEqual(choose(parse_response(FIXTURE), "totally different"), (None, "unresolved"))
        self.assertEqual(choose([], "dark item"), (None, "unresolved"))
        # Word-subset only: a prefix of a word is not a match.
        self.assertEqual(choose([media(1, "Darkness Itemized")], "dark item"), (None, "unresolved"))

    def test_resolver_validates_query(self):
        with self.assertRaises(KuraError) as caught:
            AniList().resolve("bad\ntitle")
        self.assertEqual(caught.exception.code, "invalid_query")


class TransportTests(unittest.TestCase):
    def test_endpoint_allowlist(self):
        validate_endpoint(ENDPOINT)
        for url in ("http://graphql.anilist.co/", "https://evil.test/",
                    "https://graphql.anilist.co/graphql", "https://graphql.anilist.co/?x=1",
                    "https://graphql.anilist.co/#x", "https://user@graphql.anilist.co/"):
            with self.assertRaises(KuraError):
                validate_endpoint(url)

    def test_private_dns_refused(self):
        with patch("kura.anilist.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(KuraError) as caught:
                request("dark item")
        self.assertEqual(caught.exception.code, "unsafe_url")

    def test_offline_and_http_errors_are_structured(self):
        for side_effect, code in ((OSError("offline"), "alias_unavailable"),):
            with patch("kura.anilist.socket.getaddrinfo", side_effect=side_effect):
                with self.assertRaises(KuraError) as caught:
                    request("dark item")
            self.assertEqual(caught.exception.code, code)

    def test_bounded_response_and_no_redirect_handler(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x" * (512 * 1024 + 1)
        opener = Mock()
        opener.open.return_value = response
        with patch("kura.anilist.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("1.1.1.1", 443))]), \
             patch("kura.anilist.urllib.request.build_opener", return_value=opener) as build:
            with self.assertRaises(KuraError) as caught:
                request("dark item")
        self.assertEqual(caught.exception.code, "alias_invalid")
        handlers = build.call_args.args
        self.assertTrue(any(type(h).__name__ == "NoRedirect" for h in handlers))

    def test_request_posts_json_without_credentials(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = FIXTURE
        opener = Mock()
        opener.open.return_value = response
        with patch("kura.anilist.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("1.1.1.1", 443))]), \
             patch("kura.anilist.urllib.request.build_opener", return_value=opener):
            self.assertEqual(request("dark item"), FIXTURE)
        sent = opener.open.call_args.args[0]
        self.assertEqual(sent.get_method(), "POST")
        self.assertNotIn("Cookie", sent.headers)
        self.assertNotIn("Authorization", sent.headers)
        self.assertEqual(json.loads(sent.data)["variables"], {"search": "dark item"})


class CoreAliasTests(AliasCase):
    def test_alias_widens_discovery_and_preserves_episode_identity(self):
        self.core = Core(self.store, self.provider({"Kura Sample": [self.row], "Other": []}),
                         FakeAliases(self.identity))
        core = self.core
        p, rows, alias = core.search("dark sample", 3, {})
        self.assertEqual(alias["status"], "resolved")
        self.assertEqual([r.id for r in rows], [self.row.id])
        self.assertEqual(eligible(rows, alias["titles"], 3, p), rows)
        # The alias does not match a different episode.
        self.assertEqual(core.search("dark sample", 4, {})[1], [])

    def test_discovery_stops_at_first_eligible_spelling(self):
        calls = []
        benchmark = self.row
        descriptor = self.data

        class Counting:
            def search(self, title, episode, subtitles):
                calls.append(title)
                return [benchmark] if title == "Kura Sample" else []

            def descriptor(self, release):
                return descriptor

        core = Core(self.store, Counting(), FakeAliases(self.identity))
        self.assertEqual(len(core.search("dark sample", 3, {})[1]), 1)
        self.assertEqual(calls, ["Kura Sample"])

    def test_disabled_alias_keeps_literal_behaviour(self):
        core = Core(self.store, self.provider({"dark sample": []}), None)
        p, rows, alias = core.search("dark sample", 3, {})
        self.assertEqual((rows, alias["status"]), ([], "disabled"))

    def test_unavailable_alias_degrades_without_failing_search(self):
        aliases = FakeAliases(error=KuraError("alias_unavailable", "offline"))
        core = Core(self.store, self.provider({"dark sample": []}), aliases)
        p, rows, alias = core.search("dark sample", 3, {})
        self.assertEqual((rows, alias["status"], alias["reason"]),
                         ([], "unavailable", "alias_unavailable"))

    def test_resolution_is_cached_and_refreshable(self):
        aliases = FakeAliases(self.identity)
        core = Core(self.store, self.provider({"Kura Sample": [self.row]}), aliases)
        core.search("dark sample", 3, {})
        core.search("dark sample", 3, {})
        self.assertEqual(len(aliases.calls), 1)
        self.assertEqual(core.alias_status("dark sample", refresh=True)["status"], "resolved")
        self.assertEqual(len(aliases.calls), 2)

    def test_cache_entry_expires(self):
        aliases = FakeAliases(self.identity)
        core = Core(self.store, self.provider({"Kura Sample": [self.row]}), aliases, alias_ttl=0)
        core.search("dark sample", 3, {})
        core.search("dark sample", 3, {})
        self.assertEqual(len(aliases.calls), 2)

    def test_ambiguous_alias_does_not_select_anything(self):
        core = Core(self.store, self.provider({"Kura Sample": [self.row]}),
                    FakeAliases(None, "ambiguous"))
        p, rows, alias = core.search("dark sample", 3, {})
        self.assertEqual((rows, alias["status"], alias["identity"]), ([], "ambiguous", None))

    def test_alias_survives_into_download_identity_and_receipt(self):
        core = Core(self.store, self.provider({"Kura Sample": [self.row]}), FakeAliases(self.identity))
        with patch("kura.downloader.run", return_value={"verified": True}) as mocked:
            job = core.download("dark sample", 3, {}, yes=True)
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["identity"]["status"], "resolved")
        self.assertEqual(job["identity_key"], "anilist:186742:3")
        self.assertEqual(job["media_key"], "dark sample:3")
        mocked.assert_called_once()
        # The canonical title now collides with the alias receipt.
        self.error("duplicate", core.download, "A Certain Dark Sample", 3, {}, yes=True)
        self.error("duplicate", core.download, "Kura Sample", 3, {}, yes=True)

    def test_download_without_alias_does_not_match_the_aliased_series(self):
        core = Core(self.store, self.provider({"Kura Sample": [self.row]}), None)
        self.error("no_match", core.download, "dark sample", 3, {}, dry_run=True)

    def test_resume_uses_recorded_identity_without_resolver(self):
        aliases = FakeAliases(self.identity)
        core = Core(self.store, self.provider({"Kura Sample": [self.row]}), aliases)
        with patch("kura.downloader.run", side_effect=KuraError("download_failed", "fixture")):
            self.error("download_failed", core.download, "dark sample", 3, {}, yes=True)
        calls = len(aliases.calls)
        with patch("kura.downloader.run", return_value={"verified": True}):
            # Resume under the canonical spelling still resolves to the recorded identity.
            job = core.download("Kura Sample", 3, {}, resume=True, yes=True)
        self.assertEqual(job["state"], "complete")
        self.assertEqual(len(aliases.calls), calls)


class CliAliasTests(unittest.TestCase):
    def test_aliases_command_emits_resolved_identity(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"HOME": root}), \
             patch("kura.cli.AniList") as resolver, \
             patch("sys.argv", ["kura", "aliases", "dark item"]):
            resolver.return_value.resolve.return_value = (
                Identity(186742, ("A Certain Dark Item",), "A Certain Dark Item"), "resolved")
            buffer = io.StringIO()
            with redirect_stdout(buffer), self.assertRaises(SystemExit) as exited:
                cli.main()
        self.assertEqual(exited.exception.code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["data"]["status"], "resolved")
        self.assertEqual(payload["data"]["identity"]["anilist_id"], 186742)

    def test_no_alias_flag_skips_resolution(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"HOME": root}), \
             patch("kura.cli.AniList") as resolver, \
             patch("kura.cli.Nyaa") as provider, \
             patch("sys.argv", ["kura", "search", "dark item", "--episode", "7", "--no-alias"]):
            provider.return_value.search.return_value = []
            buffer = io.StringIO()
            with redirect_stdout(buffer), self.assertRaises(SystemExit) as exited:
                cli.main()
        self.assertEqual(exited.exception.code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["data"]["identity"]["status"], "disabled")
        resolver.return_value.resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
