"""CLI for ``kura probe``: encoder and track facts from the headers of Matroska files.

``probe file`` is stateless: no state database, no network, one file opened read-only through
``rofs``. ``probe library`` reads the inventory recorded by ``manifest build`` and writes only
Kura's private state. Neither command prints; both return data for the v1 envelope.
"""
import os
from pathlib import Path

from . import mkvprobe, probe, rofs
from .models import KuraError

DEFAULT_MAX_READ_KIB = 2048
MAX_READ_KIB = 16384


def register(sub, parser_class):
    group = sub.add_parser("probe").add_subparsers(dest="operation", required=True,
                                                   parser_class=parser_class)
    q = group.add_parser("file")
    q.add_argument("path", help="Absolute path of one file; read-only, no state, no network")
    q.add_argument("--max-read-kib", type=int, default=DEFAULT_MAX_READ_KIB,
                   help="Byte budget for this file (1..%d KiB)" % MAX_READ_KIB)
    q.add_argument("--json", action="store_true", help="JSON is the default for every command")
    q = group.add_parser("library")
    q.add_argument("--library", required=True)
    q.add_argument("--root", action="append", required=True, metavar="LABEL=PATH",
                   help="Library root (repeatable); read-only")
    q.add_argument("--sample", choices=probe.SAMPLES, default="groups")
    q.add_argument("--title", action="append", metavar="KEY",
                   help="Only probe this title key, <root label>/<entry> (repeatable)")
    q.add_argument("--max-files", type=int, default=2000)
    q.add_argument("--max-read-mib", type=int, default=256, help="Total byte budget for the run")
    q.add_argument("--reprobe", action="store_true", help="Ignore cached probe results")
    q.add_argument("--json", action="store_true", help="JSON is the default for every command")


def _roots(values):
    roots = {}
    for value in values:
        label, sep, path = value.partition("=")
        if not sep or not label or not path:
            raise KuraError("invalid_arguments", "--root expects LABEL=PATH")
        if label in roots:
            raise KuraError("invalid_arguments", "Duplicate root label", label=label)
        roots[label] = Path(os.path.abspath(path))
    return roots


def _file(args):
    if not 1 <= args.max_read_kib <= MAX_READ_KIB:
        raise KuraError("invalid_arguments", "--max-read-kib must be 1..%d" % MAX_READ_KIB)
    if not os.path.isabs(args.path) or "\0" in args.path:
        raise KuraError("invalid_arguments", "An absolute file path is required")
    parent, name = os.path.split(args.path)
    if not name or name in (".", ".."):
        raise KuraError("invalid_arguments", "An absolute file path is required")
    root = rofs.open_root(Path(parent))
    try:
        fd = rofs.open_file_ro(root, (name,))
    finally:
        os.close(root)
    try:
        size = os.fstat(fd).st_size

        def read(offset, n):
            try:
                return os.pread(fd, n, offset)
            except OSError as exc:
                raise KuraError("io_error", f"Read failed: {exc.strerror}") from exc

        return mkvprobe.probe(read, size, max_bytes=args.max_read_kib * 1024)
    finally:
        os.close(fd)


def _library(args, open_store):
    if not 1 <= args.max_read_mib <= 1 << 20:
        raise KuraError("invalid_arguments", "--max-read-mib must be 1..%d" % (1 << 20))
    roots = _roots(args.root)
    probe.check_args(args.library, args.sample, args.title, args.max_files,
                     args.max_read_mib << 20, 1)
    store = open_store()
    try:
        return probe.probe_library(store, library=args.library, roots=roots, sample=args.sample,
                                   titles=args.title, max_files=args.max_files,
                                   max_read_bytes=args.max_read_mib << 20, reprobe=args.reprobe)
    finally:
        store.db.close()


def dispatch(args, open_store):
    """Return ``(True, data)`` for a ``probe`` command, else ``(False, None)``."""
    if args.command != "probe":
        return False, None
    if args.operation == "file":
        return True, _file(args)
    return True, _library(args, open_store)
