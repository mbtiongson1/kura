"""`kura anilist login/whoami/logout`: the token stays secret, the token file stays private."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import patch

from anilistfake import CLIENT_ID, VIEWER, FakeAniList, http_error, make_token
import kura.cli as cli
from kura import anilist_auth
from kura.models import KuraError
from kura.ratelimit import SharedLimiter
from test_ratelimit import Wall

DAY = 86400


class TTY(io.StringIO):
    def isatty(self):
        return True


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        self.root = self.home / ".local" / "share" / "kura"
        self.fake = FakeAniList()
        self.wall = Wall(time.time())
        self.secrets: list[str] = []
        self.outputs: list[str] = []

    def run_cli(self, *argv, stdin="", tty=False, client_id=True, getpass_value=None, extra_env=None):
        """Run the CLI in-process. Returns (exit code, envelope, stderr text)."""
        out, err = io.StringIO(), io.StringIO()
        stream = TTY(stdin) if tty else io.StringIO(stdin)
        env = {"HOME": str(self.home)}
        env.pop("KURA_ANILIST_CLIENT_ID", None)
        if client_id:
            env["KURA_ANILIST_CLIENT_ID"] = str(CLIENT_ID)
        env.update(extra_env or {})
        wall = self.wall

        def limiter(root, service, interval):
            return SharedLimiter(root, service, interval=interval, wall=wall, sleep=wall.sleep)

        patches = [patch.dict(os.environ, env), patch("sys.argv", ["kura", *argv]), patch("sys.stdin", stream),
                   patch("kura.anilist.graphql_fetch", self.fake), patch("kura.cli_anilist.SharedLimiter", limiter)]
        if getpass_value is not None:
            patches.append(patch("kura.cli_anilist.getpass.getpass", return_value=getpass_value))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if "KURA_ANILIST_CLIENT_ID" not in env:
                os.environ.pop("KURA_ANILIST_CLIENT_ID", None)
            code = 0
            try:
                cli.main()
            except SystemExit as exited:
                code = exited.code
        for text in (out.getvalue(), err.getvalue(), " ".join(argv)):
            self.outputs.append(text)
        self.assert_no_secrets()
        return code, json.loads(out.getvalue()), err.getvalue()

    def assert_no_secrets(self):
        for secret in self.secrets:
            for text in self.outputs:
                self.assertNotIn(secret, text)

    def token(self, exp_days=100):
        token = make_token(time.time() + exp_days * DAY if exp_days is not None else None)
        self.secrets.append(token)
        return token

    def login(self, token=None, **kw):
        token = token or self.token()
        return token, self.run_cli("anilist", "login", "--token-stdin", stdin=token + "\n", **kw)


class PrintUrl(Cli):
    def test_print_url_is_stateless_and_exact(self):
        with patch("kura.cli.Store", side_effect=AssertionError("state touched")):
            code, out, _ = self.run_cli("anilist", "login", "--print-url")
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "anilist"))
        self.assertEqual(out["data"]["authorize_url"],
                         f"https://anilist.co/api/v2/oauth/authorize?client_id={CLIENT_ID}&response_type=token")
        self.assertEqual(set(out["data"]), {"authorize_url", "next"})
        self.assertIn("--token-stdin", out["data"]["next"])
        self.assertEqual(list(self.home.iterdir()), [])         # nothing was created
        self.assertEqual(self.fake.calls, [])

    def test_client_id_precedence_and_validation(self):
        code, out, _ = self.run_cli("anilist", "login", "--print-url", "--client-id", "999002")
        self.assertIn("client_id=999002&", out["data"]["authorize_url"])
        code, out, _ = self.run_cli("anilist", "login", "--print-url", client_id=False)
        self.assertIn(f"client_id={anilist_auth.DEFAULT_CLIENT_ID}&", out["data"]["authorize_url"])
        self.assertEqual(anilist_auth.DEFAULT_CLIENT_ID, 58022)              # the registered client
        with patch.object(anilist_auth, "DEFAULT_CLIENT_ID", None):
            code, out, _ = self.run_cli("anilist", "login", "--print-url", client_id=False)
            self.assertEqual((code, out["error"]["code"]), (2, "anilist_client_missing"))
        with patch.object(anilist_auth, "DEFAULT_CLIENT_ID", 999003):
            code, out, _ = self.run_cli("anilist", "login", "--print-url", client_id=False)
            self.assertIn("client_id=999003&", out["data"]["authorize_url"])
            code, out, _ = self.run_cli("anilist", "login", "--print-url")      # env beats the constant
            self.assertIn(f"client_id={CLIENT_ID}&", out["data"]["authorize_url"])
        code, out, _ = self.run_cli("anilist", "login", "--print-url", extra_env={"KURA_ANILIST_CLIENT_ID": "abc"})
        self.assertEqual(out["error"]["code"], "anilist_client_missing")


class Login(Cli):
    def test_stdin_login_stores_a_private_token_and_shows_only_a_fingerprint(self):
        token, (code, out, err) = self.login()
        self.assertEqual((code, out["success"]), (0, True))
        data = out["data"]
        self.assertEqual(set(data), {"viewer", "issued_at", "expires_at", "expires_source", "stored",
                                     "token_fingerprint", "warnings"})
        self.assertEqual((data["viewer"], data["stored"], data["expires_source"], data["warnings"]),
                         (VIEWER, True, "jwt_exp", []))
        self.assertRegex(data["token_fingerprint"], r"^sha256:[0-9a-f]{8}$")
        self.assertEqual(data["token_fingerprint"], anilist_auth.fingerprint(token))
        self.assertEqual(err, "")
        path = self.root / "anilist-auth.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        stored = json.loads(path.read_text())
        self.assertEqual((stored["token"], stored["client_id"], stored["viewer"], stored["version"]),
                         (token, CLIENT_ID, VIEWER, 1))
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])
        # exactly one request, authenticated, and nothing but the Viewer query
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.calls[0]["token"], token)
        self.assertIn("Viewer", self.fake.calls[0]["query"])
        self.assertNotIn(token, " ".join(os.environ.values()))

    def test_tty_login_uses_the_hidden_prompt_and_prints_the_url_to_stderr_only(self):
        token = self.token()
        code, out, err = self.run_cli("anilist", "login", tty=True, getpass_value=token)
        self.assertEqual((code, out["success"]), (0, True))
        self.assertIn(f"client_id={CLIENT_ID}", err)
        self.assertEqual(json.loads((self.root / "anilist-auth.json").read_text())["token"], token)

    def test_a_terminal_that_cannot_hide_input_aborts_instead_of_echoing(self):
        import getpass
        import warnings

        def echoing(prompt="", stream=None):
            warnings.warn("echo", getpass.GetPassWarning)
            return "echoed-" + "x" * 30
        with patch("kura.cli_anilist.getpass.getpass", echoing):
            code, out, _ = self.run_cli("anilist", "login", tty=True)
        self.assertEqual((code, out["error"]["code"]), (2, "anilist_login_needs_tty"))
        self.assertFalse((self.root / "anilist-auth.json").exists())
        self.assertEqual(self.fake.calls, [])

    def test_login_without_a_terminal_or_with_stdin_on_a_terminal_is_refused(self):
        code, out, _ = self.run_cli("anilist", "login")
        self.assertEqual(out["error"]["code"], "anilist_login_needs_tty")
        code, out, _ = self.run_cli("anilist", "login", "--token-stdin", tty=True)
        self.assertEqual(out["error"]["code"], "invalid_arguments")
        self.assertEqual(self.fake.calls, [])

    def test_garbage_input_is_rejected_without_being_echoed(self):
        for garbage in ("not a token with spaces", "", "a" * 9000, "short"):
            if len(garbage.strip()) > 5:
                self.secrets.append(garbage.strip())
            code, out, _ = self.run_cli("anilist", "login", "--token-stdin", stdin=garbage + "\n")
            self.assertEqual((code, out["error"]["code"]), (2, "anilist_token_invalid"))
        self.assertFalse((self.root / "anilist-auth.json").exists())
        self.assertEqual(self.fake.calls, [])

    def test_rejected_token_stores_nothing_and_is_not_echoed(self):
        self.fake.queue = [http_error(400, "Invalid token")]
        token, (code, out, _) = self.login()
        self.assertEqual((code, out["error"]["code"]), (2, "anilist_auth_invalid"))
        self.assertFalse((self.root / "anilist-auth.json").exists())

    def test_server_error_stores_nothing(self):
        self.fake.queue = [http_error(500)]
        token, (code, out, _) = self.login()
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "anilist-auth.json").exists())

    def test_expiry_warning_at_29_days_and_none_at_40(self):
        _, (code, out, _) = self.login(self.token(29))
        self.assertEqual(out["data"]["warnings"], ["anilist_token_expires_soon"])
        _, (code, out, _) = self.login(self.token(40))
        self.assertEqual(out["data"]["warnings"], [])

    def test_opaque_token_expires_after_365_days(self):
        _, (code, out, _) = self.login(self.token(None))
        self.assertEqual(out["data"]["expires_source"], "issued_plus_365d")
        self.assertAlmostEqual(out["data"]["expires_at"] - out["data"]["issued_at"], 365 * DAY, delta=1)

    def test_an_already_expired_token_is_refused_before_any_request(self):
        _, (code, out, _) = self.login(self.token(-1))
        self.assertEqual(out["error"]["code"], "anilist_token_expired")
        self.assertEqual(self.fake.calls, [])

    def test_symlinked_token_path_is_refused(self):
        self.root.mkdir(parents=True, mode=0o700)
        elsewhere = self.home / "elsewhere"
        elsewhere.write_text("do not touch")
        (self.root / "anilist-auth.json").symlink_to(elsewhere)
        _, (code, out, _) = self.login()
        self.assertEqual((code, out["error"]["code"]), (2, "unsafe_path"))
        self.assertEqual(elsewhere.read_text(), "do not touch")
        with self.assertRaises(KuraError):
            anilist_auth.load(self.root)

    def test_symlinked_state_directory_is_refused(self):
        real = self.home / "real"
        real.mkdir(mode=0o700)
        self.root.parent.mkdir(parents=True)
        self.root.symlink_to(real)
        _, (code, out, _) = self.login()
        self.assertEqual((code, out["error"]["code"]), (2, "unsafe_path"))
        self.assertEqual(list(real.iterdir()), [])

    def test_symlinked_parent_directory_is_refused_on_load(self):
        token, _ = self.login()
        moved = self.home / "moved"
        (self.home / ".local").rename(moved)
        (self.home / ".local").symlink_to(moved)
        for command in (("anilist", "whoami"), ("anilist", "list"), ("anilist", "logout")):
            code, out, _ = self.run_cli(*command)
            self.assertEqual((code, out["error"]["code"]), (2, "unsafe_path"))

    def test_loose_permissions_are_refused_on_use(self):
        token, _ = self.login()
        path = self.root / "anilist-auth.json"
        os.chmod(path, 0o644)
        code, out, _ = self.run_cli("anilist", "list")
        self.assertEqual(out["error"]["code"], "anilist_token_insecure")
        os.chmod(path, 0o600)
        os.chmod(self.root, 0o755)
        code, out, _ = self.run_cli("anilist", "whoami")
        self.assertEqual(out["error"]["code"], "anilist_token_insecure")

    def test_malformed_token_file_is_refused(self):
        self.root.mkdir(parents=True, mode=0o700)
        path = self.root / "anilist-auth.json"
        path.write_text("{not json")
        os.chmod(path, 0o600)
        code, out, _ = self.run_cli("anilist", "whoami")
        self.assertEqual(out["error"]["code"], "anilist_token_invalid")

    def test_no_token_option_exists_anywhere(self):
        def options(parser):
            for action in parser._actions:
                yield from action.option_strings
                if isinstance(action, argparse._SubParsersAction):
                    for sub in action.choices.values():
                        yield from options(sub)
        names = set(options(cli.parser()))
        self.assertIn("--token-stdin", names)
        self.assertEqual({n for n in names if n.startswith("--token")}, {"--token-stdin"})
        for argv in (("--token", "x"), ("--token=abc",), ("--tok=abc",)):
            code, out, _ = self.run_cli("anilist", "login", *argv)
            self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))


class Transport(unittest.TestCase):
    """graphql_fetch itself: where the bearer header goes, and what failures reveal."""

    class Opener:
        def __init__(self, result):
            self.requests, self.result = [], result

        def open(self, request, timeout=None):
            self.requests.append(request)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class Response(io.BytesIO):
        headers = {"X-RateLimit-Limit": "90", "Set-Cookie": "dropped"}

        def __enter__(self):
            return self

    def fetch(self, result, token):
        import kura.anilist as anilist
        opener = self.Opener(result)
        with patch.object(anilist.urllib.request, "build_opener", return_value=opener), \
                patch.object(anilist, "_check_public_dns"):
            try:
                return anilist.graphql_fetch("{Viewer{id}}", {}, token), opener
            except KuraError as e:
                return e, opener

    def test_bearer_goes_only_to_the_pinned_endpoint_and_only_when_given(self):
        token = make_token(time.time() + DAY * 90)
        (body, headers), opener = self.fetch(self.Response(b"{}"), token)
        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://graphql.anilist.co/")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + token)
        self.assertEqual(headers, {"X-RateLimit-Limit": "90"})
        (_, _), opener = self.fetch(self.Response(b"{}"), None)
        self.assertIsNone(opener.requests[0].get_header("Authorization"))

    def test_malformed_token_is_refused_before_any_request(self):
        for bad in ("two words here", "line\nbreak" + "x" * 20, "short", ""):
            error, opener = self.fetch(self.Response(b"{}"), bad)
            self.assertEqual(error.code, "anilist_token_invalid")
            self.assertEqual(opener.requests, [])
            self.assertNotIn("break", error.message + json.dumps(error.details))

    def test_http_failure_carries_status_and_reason_but_never_the_token(self):
        import urllib.error
        token = make_token(time.time() + DAY * 90)
        body = io.BytesIO(json.dumps({"errors": [{"message": "Invalid token", "status": 400}]}).encode())
        failure = urllib.error.HTTPError("https://graphql.anilist.co/", 400, "Bad Request", {"Retry-After": "7"}, body)
        error, _ = self.fetch(failure, token)
        self.assertEqual((error.code, error.details["status"], error.details["reason"]),
                         ("anilist_http", 400, "Invalid token"))
        self.assertEqual(error.details["Retry-After"], "7")
        self.assertNotIn(token, json.dumps(error.details) + error.message)

    def test_an_error_message_that_echoes_the_token_is_dropped(self):
        import urllib.error
        token = make_token(time.time() + DAY * 90)
        body = io.BytesIO(json.dumps({"errors": [{"message": "bad " + token}]}).encode())
        failure = urllib.error.HTTPError("https://graphql.anilist.co/", 400, "Bad Request", {}, body)
        error, _ = self.fetch(failure, token)
        self.assertEqual((error.details["status"], error.details["reason"]), (400, None))
        self.assertNotIn(token, json.dumps(error.details) + error.message)

    def test_oversized_response_is_refused(self):
        import kura.anilist as anilist
        opener = self.Opener(self.Response(b"x" * 20))
        with patch.object(anilist.urllib.request, "build_opener", return_value=opener), \
                patch.object(anilist, "_check_public_dns"):
            with self.assertRaises(KuraError) as cm:
                anilist.graphql_fetch("{Viewer{id}}", {}, None, 10)
        self.assertEqual(cm.exception.code, "anilist_response_too_large")


class Pinned(unittest.TestCase):
    def test_connection_goes_to_the_validated_address_with_hostname_verification(self):
        import kura.anilist as anilist
        sock, wrapped = object(), object()
        context = unittest.mock.Mock()
        context.wrap_socket.return_value = wrapped
        with patch.object(anilist.socket, "create_connection", return_value=sock) as connect:
            conn = anilist._PinnedHTTPS("graphql.anilist.co", addresses=("203.0.113.7",), timeout=30, context=context)
            conn.connect()
        connect.assert_called_once_with(("203.0.113.7", 443), 30)
        context.wrap_socket.assert_called_once_with(sock, server_hostname="graphql.anilist.co")
        self.assertIs(conn.sock, wrapped)

    def test_connection_falls_through_to_the_next_validated_address(self):
        import kura.anilist as anilist
        sock, context = object(), unittest.mock.Mock()
        with patch.object(anilist.socket, "create_connection", side_effect=[OSError("no route"), sock]) as connect:
            conn = anilist._PinnedHTTPS("graphql.anilist.co", addresses=("2001:db8::1", "203.0.113.7"),
                                        timeout=30, context=context)
            conn.connect()
        self.assertEqual([c.args[0] for c in connect.call_args_list],
                         [("2001:db8::1", 443), ("203.0.113.7", 443)])
        with patch.object(anilist.socket, "create_connection", side_effect=OSError("down")):
            conn = anilist._PinnedHTTPS("graphql.anilist.co", addresses=("203.0.113.7",), timeout=30, context=context)
            with self.assertRaises(OSError):
                conn.connect()

    def test_fetch_uses_the_address_it_validated(self):
        import kura.anilist as anilist
        with patch.object(anilist, "_check_public_dns", return_value=("203.0.113.7",)), \
                patch.object(anilist, "_pinned_opener") as pinned:
            pinned.return_value.open.return_value = Transport.Response(b"{}")
            anilist.graphql_fetch("{Viewer{id}}", {}, make_token(time.time() + DAY * 90))
        pinned.assert_called_once_with(("203.0.113.7",))


class WhoamiLogout(Cli):
    def test_whoami_is_offline_and_never_shows_the_token(self):
        token, _ = self.login()
        self.fake.calls.clear()
        code, out, _ = self.run_cli("anilist", "whoami")
        self.assertEqual(self.fake.calls, [])
        data = out["data"]
        self.assertEqual((data["logged_in"], data["checked"], data["viewer"], data["warnings"]),
                         (True, False, VIEWER, []))
        self.assertEqual(data["token_fingerprint"], anilist_auth.fingerprint(token))
        self.assertAlmostEqual(data["expires_in_days"], 100, delta=0.1)

    def test_whoami_when_logged_out(self):
        code, out, _ = self.run_cli("anilist", "whoami")
        self.assertEqual((code, out["data"]), (0, {"logged_in": False, "checked": False, "warnings": []}))

    def test_whoami_check_makes_one_request_and_records_verification(self):
        token, _ = self.login()
        self.fake.calls.clear()
        code, out, _ = self.run_cli("anilist", "whoami", "--check")
        self.assertEqual((len(self.fake.calls), out["data"]["checked"]), (1, True))
        self.assertEqual(self.fake.calls[0]["token"], token)
        self.fake.queue = [http_error(401)]
        code, out, _ = self.run_cli("anilist", "whoami", "--check")
        self.assertEqual((code, out["error"]["code"]), (2, "anilist_auth_invalid"))

    def test_logout_removes_the_file_and_reminds_about_revocation(self):
        self.login()
        code, out, _ = self.run_cli("anilist", "logout")
        self.assertEqual((code, out["data"]["removed"]), (0, True))
        self.assertIn("revoke", out["data"]["reminder"].lower())
        self.assertFalse((self.root / "anilist-auth.json").exists())
        code, out, _ = self.run_cli("anilist", "logout")
        self.assertEqual(out["data"]["removed"], False)

    def test_logout_clears_the_viewer_flag(self):
        self.login()
        self.run_cli("anilist", "list")
        self.run_cli("anilist", "logout")
        import sqlite3
        db = sqlite3.connect(self.root / "state.sqlite3")
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM anilist_users WHERE is_viewer=1").fetchone()[0], 0)

    def test_logout_refuses_a_symlink(self):
        self.login()
        path = self.root / "anilist-auth.json"
        path.unlink()
        path.symlink_to(self.home)
        code, out, _ = self.run_cli("anilist", "logout")
        self.assertEqual(out["error"]["code"], "unsafe_path")
        self.assertTrue(self.home.exists())


class EndToEnd(Cli):
    def test_list_media_and_limits_through_the_cli_never_leak_the_token(self):
        token, _ = self.login()
        from anilistfake import BASE_ID, entry
        self.fake.entries = [entry(BASE_ID + n, score=9.0 if n == 1 else 0.0) for n in (1, 2, 3)]
        self.fake.favourites = [BASE_ID + 1]
        self.fake.calls.clear()
        code, out, _ = self.run_cli("anilist", "list")
        data = out["data"]
        self.assertEqual((code, out["phase"]), (0, "anilist"))
        self.assertEqual(set(data), {"user", "fetched_at", "from_cache", "stale", "error", "counts",
                                     "entries_total", "favourites", "scored_9_plus", "requests",
                                     "rate_limit", "warnings"})
        self.assertEqual((data["entries_total"], data["favourites"], data["scored_9_plus"],
                          data["requests"]), (3, 1, 1, {"made": 2, "budget": 10}))
        self.assertEqual(data["rate_limit"]["service"], "anilist")
        made = len(self.fake.calls)
        code, out, _ = self.run_cli("anilist", "list", "--entries")
        self.assertEqual((len(self.fake.calls), out["data"]["from_cache"], len(out["data"]["entries"])),
                         (made, True, 3))
        code, out, _ = self.run_cli("anilist", "media", "--id", str(BASE_ID + 7), "--id", str(BASE_ID + 8))
        self.assertEqual((out["data"]["fetched"], out["data"]["requests"]["made"]), (2, 1))
        code, out, _ = self.run_cli("anilist", "limits")
        self.assertEqual((out["data"]["limit"], out["data"]["requests"]), (90.0, {"made": 1, "budget": 1}))
        # failures too
        self.fake.queue = [http_error(401)]
        code, out, _ = self.run_cli("anilist", "list", "--refresh")
        self.assertEqual(out["error"]["code"], "anilist_auth_invalid")
        self.assertNotIn(token, json.dumps(self.fake.calls[0]["variables"]))

    def test_update_through_the_cli_writes_once_and_never_leaks_the_token(self):
        token, _ = self.login()
        from anilistfake import BASE_ID
        self.fake.list_entries[BASE_ID + 1] = {"status": "CURRENT", "progress": 7, "score": 0, "repeat": 0}
        self.fake.calls.clear()
        code, out, _ = self.run_cli("anilist", "update", "--id", str(BASE_ID + 1), "--progress", "8", "--dry-run")
        self.assertEqual((code, out["data"]["written"], out["data"]["change"]), (0, False, {"progress": 8}))
        code, out, _ = self.run_cli("anilist", "update", "--id", str(BASE_ID + 1), "--progress", "8")
        self.assertEqual((code, out["data"]["written"], out["data"]["after"]["progress"]), (0, True, 8))
        self.assertEqual(self.fake.saves, [{"mediaId": BASE_ID + 1, "progress": 8}])
        self.assertEqual([c["token"] for c in self.fake.calls], [token] * 3)
        code, out, _ = self.run_cli("anilist", "update", "--id", str(BASE_ID + 1), "--progress", "7")
        self.assertEqual((code, out["error"]["code"]), (2, "anilist_progress_decrease"))
        code, out, _ = self.run_cli("anilist", "update", "--progress", "1")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out, _ = self.run_cli("anilist", "update", "--id", str(BASE_ID + 1))
        self.assertEqual(out["error"]["code"], "invalid_arguments")

    def test_list_user_needs_no_login(self):
        code, out, _ = self.run_cli("anilist", "list", "--user", "KuraTester")
        self.assertEqual(code, 0)
        self.assertEqual([c["token"] for c in self.fake.calls], [None] * len(self.fake.calls))
        self.assertFalse(self.root.joinpath("anilist-auth.json").exists())

    def test_media_needs_exactly_one_selector(self):
        code, out, _ = self.run_cli("anilist", "media")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out, _ = self.run_cli("anilist", "media", "--id", "1", "--library", "x")
        self.assertEqual(out["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
