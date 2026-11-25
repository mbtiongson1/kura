"""Read-only piece-hash verification of a local file tree against a parsed torrent.

Answers one question per file: do the bytes on disk hash to what the torrent says, so a
copy could be re-acquired from the swarm? Nothing is written, created, renamed or
deleted. Every path is opened through `rofs` (O_RDONLY|O_NOFOLLOW, directory fds), file
data is streamed in bounded chunks (one piece hasher and one chunk in memory, never a
whole file), and the run can be cut short exactly by a byte budget or a stop callback.

How a verdict is reached
  * Each mapped file gets a metadata-only precheck (open, fstat, close). Missing,
    unsafe (symlink, FIFO, directory, symlinked parent), wrong-size and unreadable files
    are decided there and never read.
  * Pieces are then hashed in order across file boundaries. BEP 47 pad files contribute
    zeros and are never opened. A piece that touches a missing/wrong-size/changed file is
    skipped (not failed): the neighbouring files only get `unverified_boundary`.
  * A file is `verified` only when its size matches and every piece overlapping it
    passed. When a file is released (and at the end), its open descriptor is re-fstat'ed
    and the path is re-opened through rofs; any change of inode, size or mtime marks the
    file `changed`, even if its pieces happened to pass.
  * Budget and stop are checked before every chunk read. `bytes_read` never exceeds
    `max_read_bytes`. A piece cut short is neither ok nor failed; it counts as skipped.

Verdicts
  complete          every non-pad file is verified
  failed            not complete and no file with data is verified
  partial           not complete but at least one file with data is verified
  interrupted       `should_stop()` returned true (checked per file precheck and per chunk)
  budget_exhausted  more bytes were needed than `max_read_bytes` allowed
`pieces_ok` counts pieces that hashed correctly when read, even if a contributing file is
later reported `changed`; trust per-file status, not that counter, for eligibility.
"""
from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import time
from typing import Callable

from . import rofs
from .models import KuraError
from .torrentmeta import TorrentMeta

STATUSES = ("verified", "missing", "size_mismatch", "piece_failed", "unverified_boundary",
            "unsafe", "changed", "pad", "unverified")


@dataclass(frozen=True)
class FileVerdict:
    index: int
    path: str                    # torrent path, including the torrent name when multi-file
    local: str | None            # path below the root that was checked, None if unmapped/pad
    expected_size: int
    actual_size: int | None
    status: str
    evidence: dict | None        # size, mtime_ns, dev, ino at precheck
    detail: str | None = None

    def json(self) -> dict:
        return {"index": self.index, "path": self.path, "local": self.local,
                "expected_size": self.expected_size, "actual_size": self.actual_size,
                "status": self.status, "evidence": self.evidence, "detail": self.detail}


@dataclass(frozen=True)
class Verification:
    infohash: str
    verdict: str
    files: tuple[FileVerdict, ...]
    pieces_total: int
    pieces_ok: int
    pieces_failed: int
    pieces_skipped: int
    bytes_read: int
    started_at: float
    finished_at: float

    def json(self) -> dict:
        return {"infohash": self.infohash, "verdict": self.verdict,
                "pieces_total": self.pieces_total, "pieces_ok": self.pieces_ok,
                "pieces_failed": self.pieces_failed, "pieces_skipped": self.pieces_skipped,
                "bytes_read": self.bytes_read, "started_at": self.started_at,
                "finished_at": self.finished_at, "files": [f.json() for f in self.files]}


def default_mapping(meta: TorrentMeta, base: tuple[str, ...] = ()) -> dict[int, tuple[str, ...]]:
    """Map every non-pad torrent file to `base + torrent path` (path includes the name)."""
    return {f.index: tuple(base) + tuple(f.path) for f in meta.files if not f.pad}


def _evidence(st: os.stat_result) -> dict:
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "dev": st.st_dev, "ino": st.st_ino}


def _same(a: dict, st: os.stat_result) -> bool:
    return _evidence(st) == a


class _Run:
    """Mutable state for one verification; kept private so the public result is frozen."""

    def __init__(self, meta, root_fd, mapping, max_read_bytes, should_stop, chunk_size):
        self.meta, self.root_fd, self.mapping = meta, root_fd, mapping
        self.budget_left = max_read_bytes
        self.should_stop, self.chunk = should_stop, chunk_size
        n = len(meta.files)
        self.status: list[str | None] = [None] * n   # decided-before/while-reading statuses
        self.detail: list[str | None] = [None] * n
        self.evidence: list[dict | None] = [None] * n
        self.actual: list[int | None] = [None] * n
        self.ok = [0] * n
        self.failed = [0] * n
        self.boundary = [0] * n
        self.bytes_read = 0
        self.stop_reason: str | None = None          # "interrupted" | "budget_exhausted"
        self.cur: tuple[int, int] | None = None      # (file index, fd)
        self.pos = 0
        self.zeros = bytes(chunk_size)

    # -- precheck -------------------------------------------------------------
    def precheck(self):
        for f in self.meta.files:
            if f.pad:
                self.status[f.index] = "pad"
                continue
            if self.stop_reason is None and self.should_stop():
                self.stop_reason = "interrupted"
            if self.stop_reason is not None:
                self.status[f.index] = "unverified"
                self.detail[f.index] = "not checked: stopped"
                continue
            parts = self.mapping.get(f.index)
            if parts is None:
                self.status[f.index] = "missing"
                self.detail[f.index] = "no local path mapped"
                continue
            try:
                fd = rofs.open_file_ro(self.root_fd, tuple(parts))
            except KuraError as exc:
                self.status[f.index] = {"not_found": "missing", "unsafe_path": "unsafe"}.get(
                    exc.code, "unverified")
                self.detail[f.index] = exc.code
                continue
            try:
                st = os.fstat(fd)
            finally:
                os.close(fd)
            self.evidence[f.index] = _evidence(st)
            self.actual[f.index] = st.st_size
            if st.st_size != f.length:
                self.status[f.index] = "size_mismatch"

    # -- change detection -----------------------------------------------------
    def release(self):
        """Close the current descriptor and check the file did not change underneath us."""
        if self.cur is None:
            return
        index, fd = self.cur
        self.cur = None
        try:
            changed = not _same(self.evidence[index], os.fstat(fd))
        except OSError:
            changed = True
        finally:
            os.close(fd)
        if not changed:
            try:
                again = rofs.open_file_ro(self.root_fd, tuple(self.mapping[index]))
            except KuraError:
                changed = True
            else:
                try:
                    changed = not _same(self.evidence[index], os.fstat(again))
                except OSError:
                    changed = True
                finally:
                    os.close(again)
        if changed:
            self._mark_changed(index, "file changed while it was being read")

    def _mark_changed(self, index, why):
        if self.status[index] is None:
            self.status[index] = "changed"
            self.detail[index] = why

    def _open(self, index) -> bool:
        if self.cur is not None and self.cur[0] == index:
            return True
        self.release()
        if self.status[index] is not None:       # already known bad
            return False
        try:
            fd = rofs.open_file_ro(self.root_fd, tuple(self.mapping[index]))
        except KuraError:
            self._mark_changed(index, "file disappeared or became unsafe while verifying")
            return False
        try:
            same = _same(self.evidence[index], os.fstat(fd))
        except OSError:
            same = False
        if not same:
            os.close(fd)
            self._mark_changed(index, "file changed between precheck and read")
            return False
        self.cur, self.pos = (index, fd), 0
        return True

    # -- reading ----------------------------------------------------------------
    def _read_segment(self, index, offset, length, hasher) -> str:
        """Feed `length` bytes of file `index` from `offset` into hasher.

        Returns "ok", "bad" (file turned bad; piece unusable) or "stop".
        """
        if not self._open(index):
            return "bad"
        fd = self.cur[1]
        if self.pos != offset:
            try:
                os.lseek(fd, offset, os.SEEK_SET)
            except OSError:
                self._mark_changed(index, "seek failed")
                return "bad"
            self.pos = offset
        remaining = length
        while remaining:
            if self.should_stop():
                self.stop_reason = "interrupted"
                return "stop"
            n = min(self.chunk, remaining)
            if self.budget_left is not None:
                if self.budget_left <= 0:
                    self.stop_reason = "budget_exhausted"
                    return "stop"
                n = min(n, self.budget_left)
            try:
                data = b"".join(rofs.read_exact(fd, n, n))
            except KuraError as exc:
                if exc.code == "short_read":
                    self._mark_changed(index, "file ended early while reading")
                else:
                    if self.status[index] is None:
                        self.status[index] = "unverified"
                        self.detail[index] = exc.code
                # position unknown after a failed read
                return "bad"
            self.pos += n
            self.bytes_read += n
            if self.budget_left is not None:
                self.budget_left -= n
            hasher.update(data)
            remaining -= n
        return "ok"

    def _feed_zeros(self, hasher, length):
        view = memoryview(self.zeros)
        while length:
            n = min(len(view), length)
            hasher.update(view[:n])
            length -= n

    # -- main loop --------------------------------------------------------------
    def hash_pieces(self):
        meta = self.meta
        pl, total = meta.piece_length, meta.total_length
        files = meta.files
        starts = [f.offset for f in files]
        n_pieces = len(meta.pieces) // 20
        self.pieces_ok = self.pieces_failed = 0
        for p in range(n_pieces):
            start, end = p * pl, min((p + 1) * pl, total)
            j = max(bisect_right(starts, start) - 1, 0)
            segs = []
            while j < len(files) and files[j].offset < end:
                f = files[j]
                lo, hi = max(start, f.offset), min(end, f.offset + f.length)
                if hi > lo:
                    segs.append((f, lo - f.offset, hi - lo))
                j += 1
            real = [f for f, _, _ in segs if not f.pad]
            if any(self.status[f.index] is not None for f in real):
                self._skip(real)
                continue
            hasher = hashlib.sha1()
            outcome = "ok"
            for f, off, length in segs:
                if f.pad:
                    self._feed_zeros(hasher, length)
                    continue
                outcome = self._read_segment(f.index, off, length, hasher)
                if outcome != "ok":
                    break
            if outcome == "stop":
                return
            if outcome == "bad":
                self._skip(real)
                continue
            if hasher.digest() == meta.pieces[20 * p:20 * p + 20]:
                self.pieces_ok += 1
                for f in real:
                    self.ok[f.index] += 1
            else:
                self.pieces_failed += 1
                for f in real:
                    self.failed[f.index] += 1

    def _skip(self, real):
        for f in real:
            self.boundary[f.index] += 1

    # -- results ----------------------------------------------------------------
    def verdicts(self) -> tuple[FileVerdict, ...]:
        out = []
        pl = self.meta.piece_length
        for f in self.meta.files:
            i = f.index
            status, detail = self.status[i], self.detail[i]
            if status is None:   # prechecked fine; decided by what the pieces said
                needed = 0 if f.length == 0 else (f.offset + f.length - 1) // pl - f.offset // pl + 1
                if self.failed[i]:
                    status = "piece_failed"
                elif self.ok[i] == needed:
                    status = "verified"
                elif self.boundary[i]:
                    status = "unverified_boundary"
                else:
                    status = "unverified"
            out.append(FileVerdict(
                index=i, path="/".join(f.path),
                local=None if f.pad or i not in self.mapping else "/".join(self.mapping[i]),
                expected_size=f.length, actual_size=self.actual[i], status=status,
                evidence=self.evidence[i], detail=detail))
        return tuple(out)


def verify_tree(meta: TorrentMeta, root: Path, mapping: dict[int, tuple[str, ...]] | None = None,
                *, max_read_bytes: int | None = None,
                should_stop: Callable[[], bool] = lambda: False,
                chunk_size: int = 1 << 20) -> Verification:
    """Verify the files under `root` against `meta`. Strictly read-only.

    `mapping` maps torrent file index to path components below `root`
    (default: `default_mapping(meta)`); a non-pad file with no entry is `missing`.
    Raises KuraError for an unsafe/missing root or invalid arguments; per-file problems
    are reported in the result, never raised.
    """
    if chunk_size <= 0:
        raise KuraError("invalid_argument", "chunk_size must be positive")
    if max_read_bytes is not None and max_read_bytes < 0:
        raise KuraError("invalid_argument", "max_read_bytes must be >= 0")
    mapping = default_mapping(meta) if mapping is None else dict(mapping)
    started = time.time()
    root_fd = rofs.open_root(Path(root))
    try:
        run = _Run(meta, root_fd, mapping, max_read_bytes, should_stop, chunk_size)
        run.pieces_ok = run.pieces_failed = 0
        try:
            run.precheck()
            if run.stop_reason is None:
                run.hash_pieces()
        finally:
            run.release()
    finally:
        os.close(root_fd)
    files = run.verdicts()
    total_pieces = len(meta.pieces) // 20
    skipped = total_pieces - run.pieces_ok - run.pieces_failed
    data_files = [f for f in files if f.expected_size > 0]
    if run.stop_reason:
        verdict = run.stop_reason
    elif all(f.status in ("verified", "pad") for f in files):
        verdict = "complete"
    elif any(f.status == "verified" for f in data_files):
        verdict = "partial"
    else:
        verdict = "failed"
    return Verification(infohash=meta.infohash, verdict=verdict, files=files,
                        pieces_total=total_pieces, pieces_ok=run.pieces_ok,
                        pieces_failed=run.pieces_failed, pieces_skipped=skipped,
                        bytes_read=run.bytes_read, started_at=started, finished_at=time.time())
