"""`kura anilist`: login (PIN flow), whoami, logout, list, media, limits.

Read-only against AniList. There is deliberately no `--token` option: the access token is typed
into a hidden prompt or piped on stdin, stored in a private file, and never printed.
`login --print-url` is stateless (it never opens Kura's state).
"""
import getpass
import sys
import time
import warnings

from . import anilist_auth, anilist_list
from .anilist import AniList
from .models import KuraError
from .ratelimit import SharedLimiter
from .safety import private_root


def register(sub, parser_class):
    """Add the ``anilist`` command group."""
    group = sub.add_parser("anilist").add_subparsers(dest="operation", required=True, parser_class=parser_class)

    def leaf(name):
        q = group.add_parser(name, allow_abbrev=False)
        q.add_argument("--json", action="store_true", help="JSON is the default for every command")
        return q

    q = leaf("login")
    q.add_argument("--client-id", type=int, help="AniList API client ID (public); or KURA_ANILIST_CLIENT_ID")
    mode = q.add_mutually_exclusive_group()
    mode.add_argument("--print-url", action="store_true", help="Only print the authorize URL; touches no state")
    mode.add_argument("--token-stdin", action="store_true", help="Read the token (one line, <= 8 KiB) from stdin")
    q = leaf("whoami")
    q.add_argument("--check", action="store_true", help="Verify the token online (one request)")
    leaf("logout")
    q = leaf("list")
    q.add_argument("--user", help="Another AniList user's public list (sent without credentials)")
    q.add_argument("--max-age-h", type=float, default=12.0, help="Serve the stored snapshot if younger than this")
    q.add_argument("--refresh", action="store_true", help="Fetch again even if the snapshot is fresh")
    q.add_argument("--entries", action="store_true", help="Include every list entry")
    q.add_argument("--max-requests", type=int, default=10)
    q = leaf("media")
    what = q.add_mutually_exclusive_group(required=True)
    what.add_argument("--id", type=int, action="append", metavar="N", help="AniList media ID (repeatable)")
    what.add_argument("--library", help="Media IDs of this library's resolved identities")
    q.add_argument("--missing", action="store_true", help="Only IDs not already stored")
    q.add_argument("--max-requests", type=int, default=10)
    leaf("limits")


def _ani(store) -> AniList:
    # No automatic retries: a 429 records the shared penalty and the command stops.
    return AniList(max_retries=0, limiter=SharedLimiter(store.root, "anilist", interval=2.0))


def _read_hidden() -> str:
    if not sys.stdin.isatty():
        raise KuraError("anilist_login_needs_tty",
                        "No terminal: pipe the token with `kura anilist login --token-stdin`")
    print("Open this URL, approve Kura, then paste the token shown on the page:", file=sys.stderr)
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)      # never fall back to echoed input
        try:
            typed = getpass.getpass("AniList token (hidden): ", stream=sys.stderr)
        except getpass.GetPassWarning:
            raise KuraError("anilist_login_needs_tty",
                            "This terminal cannot hide input: pipe the token with --token-stdin") from None
    return anilist_auth.clean_token(typed)


def _login(args, open_store) -> dict:
    client_id = anilist_auth.resolve_client_id(args.client_id)
    url = anilist_auth.authorize_url(client_id)
    if args.print_url:
        return {"authorize_url": url,
                "next": "Open the URL, approve, copy the token from the AniList page, then run "
                        "`kura anilist login --token-stdin` and paste it (never as an argument)."}
    if args.token_stdin:
        if sys.stdin.isatty():
            raise KuraError("invalid_arguments", "--token-stdin needs piped input; omit it for a hidden prompt")
        token = anilist_auth.read_stdin_token(sys.stdin)
    else:
        if sys.stdin.isatty():
            print(url, file=sys.stderr)
        token = _read_hidden()
    store = open_store()
    try:
        ani = _ani(store)
        now = ani.wall()
        record = anilist_auth.record_for(token, client_id, {"id": 1, "name": "-"}, now)
        if anilist_auth.expiry_state(record["expires_at"], now)[0] <= 0:
            raise KuraError("anilist_token_expired", "That token has already expired")
        req = anilist_list.Requester(ani, max_requests=1, token=token)
        viewer = anilist_list.viewer(req)
        record["viewer"] = {"id": viewer["id"], "name": viewer["name"]}
        anilist_auth.save(store.root, record)
        anilist_list.save_viewer(store.db, viewer, now)
        shown = anilist_auth.public(record, now)
        return {"viewer": shown["viewer"], "issued_at": shown["issued_at"], "expires_at": shown["expires_at"],
                "expires_source": shown["expires_source"], "stored": True,
                "token_fingerprint": shown["token_fingerprint"], "warnings": shown["warnings"]}
    finally:
        store.db.close()


def _whoami(args, open_store) -> dict:
    root = private_root()
    try:
        record = anilist_auth.load(root)
    except KuraError as e:
        if e.code == "anilist_not_logged_in":
            return {"logged_in": False, "checked": False, "warnings": []}
        raise
    checked = False
    now = time.time()
    shown = anilist_auth.public(record, now)
    if args.check and "anilist_token_expired" not in shown["warnings"]:
        store = open_store()
        try:
            req = anilist_list.Requester(_ani(store), max_requests=1, token=record["token"])
            viewer = anilist_list.viewer(req)
            record["viewer"] = {"id": viewer["id"], "name": viewer["name"]}
            record["verified_at"] = now
            anilist_auth.save(store.root, record)
            anilist_list.save_viewer(store.db, viewer, now)
            shown, checked = anilist_auth.public(record, now), True
        finally:
            store.db.close()
    return {"logged_in": True, "checked": checked, **shown}


def _logout(args, open_store) -> dict:
    removed = anilist_auth.remove(private_root())
    if removed:
        store = open_store()
        try:
            anilist_list.clear_viewer(store.db)     # stored snapshots stay, but none is "the" account any more
        finally:
            store.db.close()
    return {"removed": removed,
            "reminder": "Also revoke Kura in your AniList settings (Apps) so the token stops working."}


def _list(args, open_store) -> dict:
    anilist_list.check_list_args(args.user, args.max_age_h, args.max_requests)
    store = open_store()
    try:
        return anilist_list.list_sync(store, _ani(store), user_name=args.user, max_age_h=args.max_age_h,
                                      refresh=args.refresh, entries=args.entries, max_requests=args.max_requests)
    finally:
        store.db.close()


def _media(args, open_store) -> dict:
    anilist_list.check_budget(args.max_requests)
    store = open_store()
    try:
        return anilist_list.media_sync(store, _ani(store), ids=args.id, library=args.library,
                                       missing=args.missing, max_requests=args.max_requests)
    finally:
        store.db.close()


def _limits(args, open_store) -> dict:
    store = open_store()
    try:
        return anilist_list.limits(_ani(store))
    finally:
        store.db.close()


def dispatch(args, open_store):
    """Return ``(True, data)`` for a command this module owns, else ``(False, None)``."""
    if args.command != "anilist":
        return False, None
    if args.operation == "login":
        return True, _login(args, open_store)
    if args.operation == "whoami":
        return True, _whoami(args, open_store)
    if args.operation == "logout":
        return True, _logout(args, open_store)
    if args.operation == "list":
        return True, _list(args, open_store)
    if args.operation == "media":
        return True, _media(args, open_store)
    return True, _limits(args, open_store)
