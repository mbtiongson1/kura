"""Read-only, symlink-refusing file access for input trees (libraries, torrent dirs).

Every function here opens paths with O_RDONLY and O_NOFOLLOW relative to a directory
file descriptor, so a hostile or racing tree cannot redirect a read through a symlink
or make Kura create, truncate or write anything. Later modules (piece verification,
torrent directory scanning) must reach input files only through this module.

Error codes (all `KuraError`):
  unsafe_path  symlink, non-directory intermediate, non-regular final component,
               or an invalid path component
  not_found    a component does not exist
  io_error     any other OS failure (permissions, I/O)
  short_read   `read_exact` hit end of file early
"""
import errno
import os
from pathlib import Path
import stat
from typing import Iterator

from .models import KuraError
from .safety import no_symlinks

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_NOATIME = getattr(os, "O_NOATIME", 0)  # Linux only; best effort


def _os_error(exc: OSError, what: str) -> KuraError:
    if exc.errno == errno.ENOENT:
        return KuraError("not_found", f"{what} does not exist")
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
        return KuraError("unsafe_path", f"{what} is a symlink or not the expected type")
    return KuraError("io_error", f"{what} could not be opened: {exc.strerror}")


def open_root(path: Path) -> int:
    """Open a library root as a directory fd. The caller closes it.

    The path must be absolute, non-traversing and free of symlinks in every component
    (`safety.no_symlinks`). That check is only a fast, well-worded pre-check: the actual
    open then walks from "/" one component at a time with O_NOFOLLOW|O_DIRECTORY relative
    to the previous fd, so a component swapped for a symlink after the check is refused
    atomically rather than followed.
    """
    path = Path(path)
    no_symlinks(path)
    cursor = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=cursor)
            except OSError as exc:
                raise _os_error(exc, "Root") from exc
            os.close(cursor)
            cursor = nxt
        return cursor
    except BaseException:
        os.close(cursor)
        raise


def _check_parts(parts: tuple[str, ...]):
    if not isinstance(parts, tuple) or not parts:
        raise KuraError("unsafe_path", "A non-empty tuple of path components is required")
    for part in parts:
        if (not isinstance(part, str) or part in ("", ".", "..")
                or "/" in part or "\0" in part):
            raise KuraError("unsafe_path", "Invalid path component")


def open_file_ro(root_fd: int, parts: tuple[str, ...]) -> int:
    """Open the regular file at `parts` below `root_fd`, read-only. The caller closes it.

    Directories are walked one component at a time with O_NOFOLLOW|O_DIRECTORY relative
    to the previous fd. The final component is first examined with lstat (so a FIFO or
    device is never opened), then opened O_RDONLY|O_NOFOLLOW|O_NONBLOCK, with O_NOATIME
    where the platform supports it, and fstat must say regular file with the same
    device/inode as the lstat. Nothing is created.

    Residual risk (cannot be closed without kernel support such as openat2 RESOLVE_*
    or O_PATH tricks): if an attacker with write access to the tree swaps the regular
    file for a device node between the lstat and the open, that node is opened once,
    O_RDONLY|O_NONBLOCK, and rejected immediately after by fstat. No data is read.
    The library trees Kura scans are trusted not to be raced by hostile writers; this
    module defends against accidents and planted symlinks/specials, not live attackers.
    """
    _check_parts(parts)
    shown = "/".join(parts)
    cursor, owned = root_fd, False
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=cursor)
            except OSError as exc:
                raise _os_error(exc, f"Directory in {shown}") from exc
            if owned:
                os.close(cursor)
            cursor, owned = nxt, True
        name = parts[-1]
        try:
            before = os.stat(name, dir_fd=cursor, follow_symlinks=False)
        except OSError as exc:
            raise _os_error(exc, f"File {shown}") from exc
        if not stat.S_ISREG(before.st_mode):
            raise KuraError("unsafe_path", "Expected a regular file", path=shown)
        try:
            try:
                fd = os.open(name, _FILE_FLAGS | _NOATIME, dir_fd=cursor)
            except PermissionError:
                if not _NOATIME:
                    raise
                fd = os.open(name, _FILE_FLAGS, dir_fd=cursor)  # O_NOATIME needs ownership
        except OSError as exc:
            raise _os_error(exc, f"File {shown}") from exc
        try:
            after = os.fstat(fd)
            if (not stat.S_ISREG(after.st_mode)
                    or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)):
                raise KuraError("unsafe_path", "Expected a regular file", path=shown)
        except BaseException:
            os.close(fd)
            raise
        return fd
    finally:
        if owned:
            os.close(cursor)


def read_exact(fd: int, n: int, chunk: int = 1 << 20) -> Iterator[bytes]:
    """Yield chunks totalling exactly `n` bytes from the current offset of `fd`.

    Never reads past `n`. Raises `short_read` if the file ends first (a truncated
    file is a verification fact, not something to paper over).
    """
    if n < 0 or chunk <= 0:
        raise KuraError("invalid_argument", "n must be >= 0 and chunk > 0")
    remaining = n
    while remaining:
        try:
            data = os.read(fd, min(chunk, remaining))
        except OSError as exc:
            raise KuraError("io_error", f"Read failed: {exc.strerror}") from exc
        if not data:
            raise KuraError("short_read", "File ended before the expected length",
                            missing=remaining)
        remaining -= len(data)
        yield data
