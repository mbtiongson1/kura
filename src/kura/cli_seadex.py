"""CLI for the SeaDex mirror: ``kura seadex sync | lookup | status``.

``sync`` needs network (SeaDex only) and writes only Kura's private state. ``lookup`` and
``status`` read state and never touch the network. Every command returns data for the v1
envelope that ``cli.main`` prints; this module never prints.
"""
from .models import KuraError
from .sources import seadex


def register(sub, parser_class):
    group = sub.add_parser("seadex").add_subparsers(dest="operation", required=True,
                                                    parser_class=parser_class)

    def leaf(name):
        q = group.add_parser(name)
        q.add_argument("--json", action="store_true", help="JSON is the default for every command")
        return q

    q = leaf("sync")
    q.add_argument("--full", action="store_true",
                   help="Replace the whole mirror (automatic when the last full sync is older than 7 days)")
    q.add_argument("--files-for-library", metavar="NAME",
                   help="Also fetch file lists of public picks for this library's titles and the viewer's list")
    q.add_argument("--max-requests", type=int, default=seadex.MAX_REQUESTS,
                   help="Request budget for the whole run (default %(default)s)")
    q = leaf("lookup")
    which = q.add_mutually_exclusive_group(required=True)
    which.add_argument("--anilist-id", type=int)
    which.add_argument("--infohash")
    leaf("status")


def dispatch(args, open_store):
    """Return ``(True, data)`` when `args` is a seadex command, else ``(False, None)``."""
    if args.command != "seadex":
        return False, None
    if args.operation == "sync":
        seadex.check_args(args.files_for_library, args.max_requests)
    elif args.operation == "lookup":
        if args.anilist_id is not None:
            seadex.anilist_id(args.anilist_id)
        else:
            seadex.infohash(args.infohash)
    store = open_store()
    try:
        if args.operation == "sync":
            return True, seadex.sync(store, full=args.full, files_library=args.files_for_library,
                                     max_requests=args.max_requests)
        if args.operation == "lookup":
            return True, seadex.lookup(store, anilist_id_=args.anilist_id, infohash_=args.infohash)
        if args.operation == "status":
            return True, seadex.status(store)
        raise KuraError("invalid_arguments", "Unknown seadex operation")
    finally:
        store.db.close()
