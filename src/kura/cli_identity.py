"""Command module for ``kura identity`` and ``kura retain``: title identities and the retain list.

``identity sync`` is the only command here that may use the network (AniList alias fallback,
rate-limited and bounded; ``--offline`` never does). Everything reads and writes only Kura's
private state database; nothing opens a library root.
"""
from pathlib import Path

from . import identity, idlist, partsdiscover, titleparts
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
    q.add_argument("--part", metavar="SELECTOR",
                   help="Make the ID one part of a multi-part title (all, auto, rest, season:N, ep:A-B, "
                        "dir:PATH, file:PATH); with --clear remove that one part")
    q = leaf(group, "pins")
    q.add_argument("--from", dest="pins_file", required=True, metavar="FILE",
                   help="Pins file: <AniList ID[@selector] or not-anime><TAB><title key> per line")
    q = leaf(group, "show")
    q.add_argument("--status", choices=identity.STATUSES)
    q = leaf(group, "parts")
    q.add_argument("--title", required=True, metavar="KEY",
                   help="Show the parts of this title and which part each video file belongs to")

    q = leaf(group, "discover")
    q.add_argument("--max-age-d", type=float, default=90.0,
                   help="Refuse a franchise whose relations were fetched longer ago than this (days)")
    q.add_argument("--dry-run", action="store_true", help="Report what would be written; change nothing")

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
        if args.operation == "pins":
            pins = idlist.parse_pins(Path(args.pins_file).absolute())   # refuse a bad file before opening state
            store = open_store()
            try:
                return True, identity.apply_pins(store, args.library, pins)
            finally:
                store.db.close()
        store = open_store()
        try:
            if args.operation == "pin":
                return True, identity.pin(store, args.library, args.title, args.anilist_id,
                                          not_anime=args.not_anime, clear=args.clear, part=args.part)
            if args.operation == "discover":
                partsdiscover.check_args(args.library, args.max_age_d)
                return True, partsdiscover.discover(store, args.library, max_age_d=args.max_age_d,
                                                    dry_run=args.dry_run)
            if args.operation == "parts":
                return True, titleparts.show_assignment(store, args.library, args.title)
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
