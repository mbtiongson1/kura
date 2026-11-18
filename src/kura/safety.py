"""Filesystem capability boundary. Destinations are relative to two fixed roots."""
import os
from pathlib import Path
import stat
import sys

from .models import KuraError


def no_symlinks(path: Path):
    if not path.is_absolute() or ".." in path.parts:
        raise KuraError("unsafe_path", "An absolute, non-traversing path is required")
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise KuraError("unsafe_path", "Symlink paths are not permitted", path=str(part))
        if part.exists() and not part.is_dir():
            raise KuraError("unsafe_path", "A directory component is not a directory")


def private_root() -> Path:
    return Path.home() / ".local" / "share" / "kura"


def ensure_dir(path: Path):
    no_symlinks(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    no_symlinks(path)


def destination(value: str) -> Path:
    # No arbitrary absolute path or parent traversal, including CLI overrides.
    kind, _, suffix = value.partition("/")
    shared_root = (
        Path.home() / "Downloads" / "Kura"
        if sys.platform == "darwin"
        else Path("/storage/emulated/0/Download/Kura")
    )
    roots = {"private": private_root() / "downloads",
             "shared": shared_root}
    if kind not in roots or any(p in ("", ".", "..") for p in suffix.split("/")) and suffix:
        raise KuraError("unsafe_destination", "Use private[/folder] or shared[/folder]")
    if "\\" in value or any(ord(c) < 32 for c in value) or len(value) > 240:
        raise KuraError("unsafe_destination", "Invalid destination characters")
    path = roots[kind] / suffix
    no_symlinks(path)
    if kind == "shared" and not roots[kind].parent.is_dir():
        hint = ("Create ~/Downloads before using shared storage" if sys.platform == "darwin"
                else "Run termux-setup-storage before using shared storage")
        raise KuraError("storage_permission", hint)
    return path


def regular_file(path: Path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise KuraError("unsafe_path", "Expected a regular file", path=str(path))


def private_file(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")
