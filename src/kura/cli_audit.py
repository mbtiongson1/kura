"""Command module for ``kura audit``: the offline library quality report.

State only: no library root is opened and nothing touches the network. The retain list and the
policy overrides are read from owner-supplied files (see ``idlist``); the report is returned in
the v1 envelope and, unless ``--no-store``, kept in the private ``audit_reports`` table.
"""
from pathlib import Path

from . import audit, idlist, policy


def register(sub, parser_class):
    q = sub.add_parser("audit")
    q.add_argument("--json", action="store_true", help="JSON is the default for every command")
    q.add_argument("--library", required=True)
    q.add_argument("--retain", metavar="FILE", help="AniList ID list: titles that are never proposed for removal")
    q.add_argument("--policy-overrides", metavar="FILE", help="Lines '<AniList ID> archival|efficient'")
    q.add_argument("--user", metavar="NAME", help="AniList user whose synced list decides the policy "
                                                  "(default: the logged-in viewer)")
    q.add_argument("--title", action="append", metavar="KEY",
                   help="Only audit this title key, <root label>/<entry> (repeatable)")
    q.add_argument("--flag", action="append", choices=audit.FLAGS, metavar="CODE",
                   help="Only list titles carrying this flag (repeatable): " + ", ".join(audit.FLAGS))
    q.add_argument("--limit", type=int, default=200, help="Most titles listed, worst first (1..%d)" % audit.MAX_LIMIT)
    q.add_argument("--no-store", action="store_true", help="Do not keep the report in state")


def dispatch(args, open_store):
    """Return ``(True, report)`` for ``audit``, else ``(False, None)``."""
    if args.command != "audit":
        return False, None
    audit.check_args(args.library, args.title, args.flag, args.limit, None, None)
    retain = idlist.parse(Path(args.retain).absolute()) if args.retain else None
    overrides = (idlist.parse(Path(args.policy_overrides).absolute(), tags=policy.POLICIES)
                 if args.policy_overrides else None)
    store = open_store()
    try:
        return True, audit.run(store, library=args.library, retain=retain, overrides=overrides, user=args.user,
                               titles=args.title, flags=args.flag, limit=args.limit,
                               store_report=not args.no_store)
    finally:
        store.db.close()
