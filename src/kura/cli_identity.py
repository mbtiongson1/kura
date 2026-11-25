"""Command module for ``kura identity`` and ``kura retain``: title identities and the retain list.

``identity sync`` is the only command here that may use the network (AniList alias fallback,
rate-limited and bounded; ``--offline`` never does). Everything reads and writes only Kura's
private state database; nothing opens a library root.
"""
from pathlib import Path

from . import identity, idlist
from .anilist import AniList
from .ratelimit import SharedLimiter


def register(sub, parser_class):
    """Add the ``identity`` and ``retain`` command groups."""
    def leaf(group, name):
        q = group.add_parser(name)
        q.add_argument("--json", action="store_true", help="JSON is the default for every command")
        q.add_argument("--library", required=True)
        return q

    group = sub.add_parser("identity").add_subparsers(dest="operation", required=True,
                                                      parser_class=parser_class)
    q = leaf(group, "sync")
    q.add_argument("--max-lookups", type=int, default=60,
                   help="Upper bound on AniList network lookups (0..%d)" % identity.MAX_LOOKUPS)
    q.add_argument("--offline", action="store_true",
                   help="Match against the synced AniList list only; no network")
    q.add_argument("--refresh", action="store_true", help="Look resolved titles up again")
    q.add_argument("--title", action="append", metavar="KEY",
                   help="Only this title key, <root label>/<entry> (repeatable); default all")
    q = leaf(group, "pin")
    q.add_argument("--title", required=True, metavar="KEY")
    what = q.add_mutually_exclusive_group(required=True)
    what.add_argument("--anilist-id", type=int, help="Bind the title to this AniList ID")
    what.add_argument("--not-anime", action="store_true", help="Record that the title is not anime")
    what.add_argument("--clear", action="store_true", help="Remove the owner decision")
    q = leaf(group, "show")
    q.add_argument("--status", choices=identity.STATUSES)

    retain = sub.add_parser("retain").add_subparsers(dest="operation", required=True,
                                                     parser_class=parser_class)
    q = leaf(retain, "check")
    q.add_argument("--retain", required=True, metavar="FILE", help="AniList ID list (one per line)")


def _sync(args, open_store) -> dict:
    identity.check_sync_args(args.library, args.title, args.max_lookups)
    store = open_store()
    try:
        aliases = None if args.offline else AniList(
            limiter=SharedLimiter(store.root, "anilist", interval=2.0))
        return identity.sync(store, library=args.library, titles=args.title, aliases=aliases,
                             max_lookups=args.max_lookups, refresh=args.refresh)
    finally:
        store.db.close()


def dispatch(args, open_store):
    """Return ``(True, data)`` for an identity or retain command, else ``(False, None)``."""
    if args.command == "identity":
        if args.operation == "sync":
            return True, _sync(args, open_store)
        store = open_store()
        try:
            if args.operation == "pin":
                return True, identity.pin(store, args.library, args.title, args.anilist_id,
                                          not_anime=args.not_anime, clear=args.clear)
            return True, identity.show(store, args.library, args.status)
        finally:
            store.db.close()
    if args.command == "retain":
        retain = idlist.parse(Path(args.retain).absolute())
        store = open_store()
        try:
            return True, identity.retain_check(store, args.library, retain)
        finally:
            store.db.close()
    return False, None
