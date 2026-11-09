"""Thin JSON CLI. Library services never print or prompt."""
import argparse
from dataclasses import asdict
import json
import os
import signal
import sqlite3
import sys

from .core import Core
from .models import KuraError
from .provider import Nyaa
from .safety import private_root
from .store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise KuraError("invalid_arguments", message)


def options(parser):
    parser.add_argument("--resolution", choices=["480p", "720p", "1080p", "2160p", "any"])
    parser.add_argument("--groups", help="Ordered comma-separated allowed release groups; empty clears")
    parser.add_argument("--subtitles", choices=["english", "raw", "any"])
    parser.add_argument("--max-size-mb", type=int, help="Maximum MiB, 1..10000")
    parser.add_argument("--min-seeders", type=int)
    parser.add_argument("--destination", help="private[/folder] or shared[/folder]")


def parser():
    p = Parser(prog="kura", description="Authorized single-episode acquisition on Termux")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("search", "download"):
        q = sub.add_parser(name)
        q.add_argument("title")
        q.add_argument("--episode", type=int, required=True)
        q.add_argument("--json", action="store_true", help="JSON is the default for every command")
        options(q)
        if name == "download":
            approval = q.add_mutually_exclusive_group()
            approval.add_argument("--dry-run", action="store_true")
            approval.add_argument("--yes", action="store_true", help="Confirm authorization for this single torrent")
            q.add_argument("--resume", action="store_true", help="Explicitly retry an interrupted/failed acquisition")
            q.add_argument("--release-id", help="Disambiguate using an eligible Nyaa release ID")
    q = sub.add_parser("downloads")
    q.add_argument("operation", choices=["status"])
    q.add_argument("--json", action="store_true")
    prefs = sub.add_parser("prefs").add_subparsers(dest="operation", required=True, parser_class=Parser)
    q = prefs.add_parser("show")
    q.add_argument("--json", action="store_true")
    q = prefs.add_parser("set")
    q.add_argument("--json", action="store_true")
    options(q)
    return p


def confirm(plan):
    if not sys.stdin.isatty():
        return False
    print(json.dumps(plan, indent=2), file=sys.stderr)
    print("Only acquire content you are authorized to download. Type yes to download:", file=sys.stderr)
    return input().strip() == "yes"


def execute(args):
    store = Store(private_root())
    try:
        core = Core(store, Nyaa())
        overrides = {key: getattr(args, key) for key in asdict(store.prefs())
                     if getattr(args, key, None) is not None}
        if "groups" in overrides:
            overrides["groups"] = tuple(x.strip() for x in overrides["groups"].split(",") if x.strip())
        if args.command == "prefs":
            if args.operation == "set":
                with store.lock():
                    store.save_prefs(core.preferences(overrides))
            return asdict(store.prefs())
        if args.command == "downloads":
            return core.status()
        if args.command == "search":
            p, rows = core.search(args.title, args.episode, overrides)
            return {"query": args.title, "episode": args.episode, "preferences": asdict(p),
                    "candidates": [r.json() for r in rows]}
        return core.download(args.title, args.episode, overrides, dry_run=args.dry_run,
                             yes=args.yes, resume=args.resume, release_id=args.release_id, confirm=confirm)
    finally:
        store.db.close()


def main():
    os.umask(0o077)
    # SIGTERM follows the same cleanup/receipt path as Ctrl-C; SIGKILL is recovered on next invocation.
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    phase = "arguments"
    try:
        args = parser().parse_args()
        phase = args.command
        data = execute(args)
        result, code = {"schema_version": 1, "success": True, "phase": phase,
                        "data": data, "error": None}, 0
    except KuraError as e:
        result, code = {"schema_version": 1, "success": False, "phase": phase, "data": e.details,
                        "error": {"code": e.code, "message": e.message}}, 2
    except (KeyboardInterrupt, EOFError):
        result, code = {"schema_version": 1, "success": False, "phase": phase, "data": None,
                        "error": {"code": "interrupted", "message": "Interrupted; no completion claimed"}}, 130
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as e:
        result, code = {"schema_version": 1, "success": False, "phase": phase, "data": None,
                        "error": {"code": "local_error", "message": "Local state or filesystem error; inspect permissions/state without deleting partial data"}}, 2
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(code)
