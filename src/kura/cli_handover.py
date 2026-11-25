"""CLI for the library handover pipeline: inventory -> inspect -> verify -> manifest.

Three commands are *stateless*: ``library inventory``, ``torrent inspect`` and
``torrent verify`` never open the state database, never touch HOME and write nothing, so
they run in a container with a read-only root filesystem and a read-only library mount.
Only the ``manifest`` commands (``build``, ``show``, ``plan``, ``swarm``) open Kura's private state, lazily, after
their arguments validated. Every command returns data for the v1 envelope that
``cli.main`` prints; this module never prints.
"""
import os
from pathlib import Path

from . import manifest, quarantine, scrape
from .anilist import AniList
from .inventory import scan
from .models import KuraError
from .pieceverify import default_mapping, verify_tree
from .protect import classify, load_policy, magnet
from .torrentmeta import parse

MAX_FILES_SHOWN = 500


def register(sub, parser_class):
    """Add the ``library``, ``torrent`` and ``manifest`` command groups to the CLI."""
    def leaf(group, name):
        q = group.add_parser(name)
        q.add_argument("--json", action="store_true", help="JSON is the default for every command")
        return q

    library = sub.add_parser("library").add_subparsers(dest="operation", required=True,
                                                       parser_class=parser_class)
    q = leaf(library, "inventory")
    q.add_argument("--root", action="append", required=True, metavar="LABEL=PATH",
                   help="Library root to scan (repeatable); read-only, metadata only")
    q.add_argument("--files", action="store_true", help="Include every file entry")

    torrent = sub.add_parser("torrent").add_subparsers(dest="operation", required=True,
                                                       parser_class=parser_class)
    q = leaf(torrent, "inspect")
    q.add_argument("file")
    q.add_argument("--tracker-denylist")
    q.add_argument("--protected")
    q = leaf(torrent, "verify")
    q.add_argument("file")
    q.add_argument("--root", required=True, help="Library root holding the files")
    q.add_argument("--base", help="Directory below the root that contains the torrent's top path")
    q.add_argument("--max-read-gib", type=float)

    manifest_ = sub.add_parser("manifest").add_subparsers(dest="operation", required=True,
                                                          parser_class=parser_class)
    q = leaf(manifest_, "build")
    q.add_argument("--library", required=True)
    q.add_argument("--root", action="append", required=True, metavar="LABEL=PATH")
    q.add_argument("--torrents", required=True, help="Directory of .torrent files (read-only)")
    q.add_argument("--tracker-denylist", required=True)
    q.add_argument("--protected", required=True)
    q.add_argument("--max-read-gib", type=float)
    q.add_argument("--reverify", action="store_true", help="Ignore cached verifications")
    q.add_argument("--no-anilist", action="store_true",
                   help="Skip AniList identity lookups (on by default, rate-limited and cached)")
    q = leaf(manifest_, "show")
    q.add_argument("--library", required=True)
    q.add_argument("--status", choices=manifest.STATUSES)
    q.add_argument("--title", help="Exact title key, <root label>/<entry>")
    q = leaf(manifest_, "plan")
    q.add_argument("--library", required=True)
    q.add_argument("--title", action="append", metavar="KEY",
                   help="Only consider this title key (repeatable); default all titles")
    q.add_argument("--min-seeders", type=int, default=3)
    q.add_argument("--skip-swarm", action="store_true",
                   help="Propose without swarm data; recorded as swarm_unchecked in the plan")
    q.add_argument("--quarantine-dir", default=quarantine.QUARANTINE_DIR)
    q.add_argument("--max-inventory-age-h", type=float, default=24)
    q.add_argument("--min-quiet-days", type=float, default=14)
    q = leaf(manifest_, "swarm")
    q.add_argument("--library", required=True)
    q.add_argument("--title", action="append", metavar="KEY",
                   help="Only scrape this title key (repeatable); default every reacquirable title")
    q.add_argument("--max-requests", type=int, default=scrape.MAX_REQUESTS,
                   help="Upper bound on tracker requests in this run")


def _abs(value: str) -> Path:
    return Path(os.path.abspath(value))


def _roots(values: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        label, sep, path = value.partition("=")
        if not sep or not label or not path:
            raise KuraError("invalid_arguments", "--root expects LABEL=PATH")
        if label in roots:
            raise KuraError("invalid_arguments", "Duplicate root label", label=label)
        roots[label] = _abs(path)
    return roots


def _budget(gib):
    if gib is None:
        return None
    if not 0 < gib < 2**30:
        raise KuraError("invalid_arguments", "--max-read-gib must be positive")
    return int(gib * 1024**3)


def _parse(path: str):
    data = manifest.read_file(_abs(path))
    try:
        return parse(data)
    except KuraError:
        raise
    except Exception as exc:
        raise KuraError("invalid_torrent", "Descriptor could not be parsed") from exc


def _trim(doc: dict, key: str) -> dict:
    items = doc[key]
    if len(items) > MAX_FILES_SHOWN:
        doc = {**doc, key: items[:MAX_FILES_SHOWN], key + "_truncated": len(items) - MAX_FILES_SHOWN}
    return doc


def _inventory(args) -> dict:
    roots = _roots(args.root)
    return {"roots": [scan(path, label).json(files=args.files) for label, path in roots.items()]}


def _inspect(args) -> dict:
    meta = _parse(args.file)
    policy = load_policy(_abs(args.tracker_denylist) if args.tracker_denylist else None,
                         _abs(args.protected) if args.protected else None)
    protection = classify(meta, policy)
    return {"torrent": _trim(meta.json(), "files"), "protection": protection.json(),
            "magnet": magnet(meta, protection),
            "policy": {"sha256": policy.sha256, "has_denylist": policy.has_denylist,
                       "has_protected_list": policy.has_protected_list}}


def _verify(args) -> dict:
    meta = _parse(args.file)
    base = ()
    if args.base:
        base = tuple(args.base.split("/"))
        if any(p in ("", ".", "..") or "\0" in p for p in base):
            raise KuraError("invalid_arguments", "--base must be a relative path without traversal")
    result = verify_tree(meta, _abs(args.root), default_mapping(meta, base),
                         max_read_bytes=_budget(args.max_read_gib))
    return {"torrent": {"infohash": meta.infohash, "name": meta.name,
                        "file_count": sum(not f.pad for f in meta.files)},
            "verification": _trim(result.json(), "files")}


def _plan(args, open_store) -> dict:
    """Write a quarantine *proposal*; nothing is moved, renamed or deleted, ever."""
    options = dict(library=args.library, titles=args.title, quarantine_dir=args.quarantine_dir,
                   min_seeders=args.min_seeders, skip_swarm=args.skip_swarm,
                   max_inventory_age_h=args.max_inventory_age_h, min_quiet_days=args.min_quiet_days)
    checked = {k: v for k, v in options.items() if k != "skip_swarm"}
    quarantine.check_args(**checked, quarantine_days=30, max_swarm_age_h=24)
    store = open_store()
    try:
        return quarantine.plan(store, **options)
    finally:
        store.db.close()


def _swarm(args, open_store) -> dict:
    """HTTP tracker scrape of public, unprotected torrents. Needs network; touches no library."""
    scrape.check_args(args.library, args.title, args.max_requests)
    store = open_store()
    try:
        return scrape.scrape(store, library=args.library, titles=args.title,
                             max_requests=args.max_requests)
    finally:
        store.db.close()


def _manifest(args, open_store) -> dict:
    if args.operation == "show":
        store = open_store()
        try:
            return manifest.show(store, args.library, args.status, args.title)
        finally:
            store.db.close()
    if args.operation == "plan":
        return _plan(args, open_store)
    if args.operation == "swarm":
        return _swarm(args, open_store)
    roots = _roots(args.root)
    policy = load_policy(_abs(args.tracker_denylist), _abs(args.protected))
    budget = _budget(args.max_read_gib)
    aliases = None if args.no_anilist else AniList()
    store = open_store()
    try:
        return manifest.build(store, roots=roots, library=args.library,
                              torrents_dir=_abs(args.torrents), policy=policy,
                              max_read_bytes=budget, reverify=args.reverify, aliases=aliases)
    finally:
        store.db.close()


def dispatch(args, open_store):
    """Return ``(True, data)`` when `args` is a handover command, else ``(False, None)``.

    `open_store` is only called for ``manifest`` commands, so the stateless commands work
    without a writable state directory.
    """
    if args.command == "library":
        return True, _inventory(args)
    if args.command == "torrent":
        return True, (_inspect(args) if args.operation == "inspect" else _verify(args))
    if args.command == "manifest":
        return True, _manifest(args, open_store)
    return False, None
