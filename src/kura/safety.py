"""Filesystem capability boundary. Destinations are relative to two fixed roots."""
import os
from pathlib import Path
import stat

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
    roots = {"private": private_root() / "downloads",
             "shared": Path("/storage/emulated/0/Download/Kura")}
    if kind not in roots or any(p in ("", ".", "..") for p in suffix.split("/")) and suffix:
        raise KuraError("unsafe_destination", "Use private[/folder] or shared[/folder]")
    if "\\" in value or any(ord(c) < 32 for c in value) or len(value) > 240:
        raise KuraError("unsafe_destination", "Invalid destination characters")
    path = roots[kind] / suffix
    no_symlinks(path)
    if kind == "shared" and not roots[kind].parent.is_dir():
        raise KuraError("storage_permission", "Run termux-setup-storage before using shared storage")
    return path


def regular_file(path: Path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise KuraError("unsafe_path", "Expected a regular file", path=str(path))


def private_file(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")
