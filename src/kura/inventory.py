"""Metadata-only library inventory: names, sizes, mtimes and inode identity.

The scan never opens a file and never follows a symlink. Directories are entered with
O_NOFOLLOW|O_DIRECTORY relative to their parent fd and re-checked with fstat, so a tree
that changes under the scan cannot redirect it. Anything that is not a plain regular
file or directory on the root's device is left alone and recorded in the title's
`blocked` list as "reason:relative/path" (at most MAX_BLOCKED entries, then
"blocked_truncated"). A title with any blocked entry must never be treated as eligible
for anything destructive by later stages.

Reasons: symlink, special_file, mount_crossing, undecodable_name, control_name,
depth_limit, unreadable, changed_during_scan.

Paths in `FileEntry.rel_path` and `Title.rel_path` are POSIX-style and relative to the
scanned root, so `tuple(rel_path.split("/"))` feeds `rofs.open_file_ro` directly.
`newest_mtime_ns` is the newest lstat mtime over every entry seen under the title
(directories and blocked entries included), so any add/remove/rename counts as activity.
"""
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import stat
import time

from .models import KuraError
from .rofs import open_root

MAX_BLOCKED = 50
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _printable(name: str) -> str:
    """Injective, valid-UTF-8 rendering of a name that is not valid UTF-8."""
    return os.fsencode(name).decode("utf-8", "backslashreplace")


@dataclass(frozen=True)
class FileEntry:
    rel_path: str
    size: int
    mtime_ns: int
    dev: int
    ino: int

    def json(self):
        return {"rel_path": self.rel_path, "size": self.size, "mtime_ns": self.mtime_ns,
                "dev": self.dev, "ino": self.ino}


@dataclass(frozen=True)
class Title:
    rel_path: str
    kind: str  # "dir" or "file"
    files: tuple[FileEntry, ...]
    total_bytes: int
    newest_mtime_ns: int
    blocked: tuple[str, ...]

    def json(self, files: bool = False):
        out = {"rel_path": self.rel_path, "kind": self.kind, "file_count": len(self.files),
               "total_bytes": self.total_bytes, "newest_mtime_ns": self.newest_mtime_ns,
               "blocked": list(self.blocked)}
        if files:
            out["files"] = [f.json() for f in self.files]
        return out


@dataclass(frozen=True)
class Inventory:
    label: str
    root_dev: int
    titles: tuple[Title, ...]
    entries: int
    scanned_at: float

    def json(self, files: bool = False):
        return {"label": self.label, "entries": self.entries,
                "titles": [t.json(files) for t in self.titles]}


class _Walk:
    def __init__(self, root_dev, max_entries, max_depth):
        self.root_dev, self.max_entries, self.max_depth = root_dev, max_entries, max_depth
        self.entries = 0
        self.files, self.blocked, self.total, self.newest = [], [], 0, 0

    def count(self):
        self.entries += 1
        if self.entries > self.max_entries:
            raise KuraError("inventory_limit", "Library has more entries than the scan limit",
                            max_entries=self.max_entries)

    def block(self, reason, rel):
        self.blocked.append(f"{reason}:{rel}")

    def seen(self, st):
        self.newest = max(self.newest, st.st_mtime_ns)

    def handle(self, dir_fd, name, rel, depth, st):
        """Classify one entry (already counted). Descends into plain same-device dirs."""
        self.seen(st)
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            return self.block("control_name", rel)
        mode = st.st_mode
        if stat.S_ISLNK(mode):
            return self.block("symlink", rel)
        if st.st_dev != self.root_dev:
            return self.block("mount_crossing", rel)
        if stat.S_ISREG(mode):
            self.files.append(FileEntry(rel, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino))
            self.total += st.st_size
        elif stat.S_ISDIR(mode):
            if depth > self.max_depth:
                return self.block("depth_limit", rel)
            self.descend(dir_fd, name, rel, depth)
        else:
            self.block("special_file", rel)

    def descend(self, parent_fd, name, rel, depth):
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            reason = "unreadable" if exc.errno in (errno.EACCES, errno.EPERM) \
                else "changed_during_scan"
            return self.block(reason, rel)
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_dev != self.root_dev:
                return self.block("mount_crossing" if stat.S_ISDIR(st.st_mode)
                                  else "changed_during_scan", rel)
            self.seen(st)
            self.listing(fd, rel, depth)
        finally:
            os.close(fd)

    def listing(self, fd, rel, depth):
        try:
            with os.scandir(fd) as it:
                items = sorted(it, key=lambda e: os.fsencode(e.name))
        except OSError:
            return self.block("unreadable", rel)
        for entry in items:
            self.count()
            child = f"{rel}/{entry.name}" if rel else entry.name
            try:
                entry.name.encode("utf-8")
            except UnicodeEncodeError:
                self.block("undecodable_name", _printable(child))
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                self.block("changed_during_scan", child)
                continue
            self.handle(fd, entry.name, child, depth + 1, st)


def scan(root: Path, label: str, *, max_entries: int = 500_000, max_depth: int = 12,
         skip: tuple[str, ...] = (".kura-quarantine",)) -> Inventory:
    """Inventory every top-level entry of `root` as a title, metadata only.

    `root` must be absolute and symlink-free (else `unsafe_path`). Top-level entries named
    in `skip` are ignored. More than `max_entries` entries raises `inventory_limit`.
    Directories nested deeper than `max_depth` (root's children are depth 1) are blocked.
    """
    if not _LABEL.match(label or ""):
        raise KuraError("invalid_label", "Label must be 1-64 of letters, digits, . _ -")
    if not 1 <= max_depth <= 64 or max_entries < 1:
        raise KuraError("invalid_argument", "max_depth must be 1-64 and max_entries >= 1")
    root_fd = open_root(root)
    try:
        root_dev = os.fstat(root_fd).st_dev
        probe = _Walk(root_dev, max_entries, max_depth)
        try:
            with os.scandir(root_fd) as it:
                top = sorted(it, key=lambda e: os.fsencode(e.name))
        except OSError as exc:
            raise KuraError("io_error", f"Root could not be listed: {exc.strerror}") from exc
        titles = []
        for entry in top:
            if entry.name in skip:
                continue
            probe.count()
            walk = _Walk(root_dev, max_entries, max_depth)
            walk.entries = probe.entries  # entry budget is shared across titles
            name = entry.name
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                shown = _printable(name)
                walk.block("undecodable_name", shown)
                titles.append(_title(shown, "file", walk))
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                walk.block("changed_during_scan", name)
                titles.append(_title(name, "file", walk))
                continue
            walk.handle(root_fd, name, name, 1, st)
            probe.entries = walk.entries
            titles.append(_title(name, "dir" if stat.S_ISDIR(st.st_mode) else "file", walk))
        return Inventory(label, root_dev, tuple(titles), probe.entries, time.time())
    finally:
        os.close(root_fd)


def _title(rel, kind, walk):
    blocked = sorted(set(walk.blocked))
    if len(blocked) > MAX_BLOCKED:
        blocked = blocked[:MAX_BLOCKED] + ["blocked_truncated"]
    return Title(rel, kind, tuple(sorted(walk.files, key=lambda f: f.rel_path)),
                 walk.total, walk.newest, tuple(blocked))
