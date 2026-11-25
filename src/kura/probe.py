"""Library probing: run ``mkvprobe`` / ``mp4probe`` over the video files recorded by ``manifest build``.

Offline and media-only. Files come from the ``library_files`` rows of an earlier inventory;
every one is opened through ``rofs`` (O_RDONLY|O_NOFOLLOW, one root descriptor per root) and
read with ``os.pread`` on that descriptor only. Nothing is created, renamed or deleted under a
root; results go into ``probe_results`` in Kura's private state. The whole run holds
``store.lock()`` so it never overlaps another mutation.

Sampling
  groups (default)  per title, video files are clustered by a name signature (release group,
                    resolution tokens, codec tokens); the first file (sorted) of each
                    cluster is probed, at most ``MAX_CLUSTERS`` clusters per title
  all               every video file, resumable under the byte budget

Cache: a row is a hit (no bytes read; the file is still opened and its size, mtime, device and
inode compared with the inventory, so a swapped root cannot serve stale results) when the inventory's size and mtime and ``PROBE_VERSION`` equal the
stored ones, the stored status describes the content (ok, partial, unsupported_container,
invalid), the probe was not cut by the per-file byte limit (or was cut at an allowance no smaller than
the current one; the stored result then carries an ``allowance`` key) and ``reprobe`` is off. The cache is as fresh as the inventory: re-run ``manifest build`` after files change. Rows describing an access problem
(unsafe, changed, io_error) are recorded but never count as hits, so they are retried.

A title is only probed (or served from the cache) when its root opens safely and sits on the
device recorded by the inventory (``root_mismatch`` otherwise).

``max_files`` bounds the files probed in one run. Cache hits and files that cannot be probed
(changed, unsafe, io_error) do not use it up, so repeated runs walk through a selection larger
than the cap and a stuck file cannot starve the files behind it. Files left over stay pending.

``reprobe`` ignores the cache from the start of the selection each run, so it cannot continue where
a capped run stopped: for a library larger than the caps, reprobe per title or raise the caps.

Budget: ``max_read_bytes`` is a hard cap on bytes read in one run. A file gets
``min(per_file_bytes, what is left)`` (``per_file_bytes`` is itself capped at ``max_read_bytes``,
so every run can finish at least one file); if the smaller allowance cuts a probe short, that result
is discarded (not cached) and the file stays pending for the next run.
"""
import json
import os
import re
import time

from . import mkvprobe, mp4probe, rofs
from .manifest_store import ManifestRepo, canonical
from .models import KuraError

VIDEO_EXT = (".mkv", ".mka", ".webm", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".wmv", ".flv")
MAX_CLUSTERS = 4
MAX_FILES = 100_000
MAX_PER_FILE = 16 << 20
SNIFF_BYTES = 16
EBML_MAGIC = b"\x1a\x45\xdf\xa3"
CONTENT_STATUSES = ("ok", "partial", "unsupported_container", "invalid")
SAMPLES = ("groups", "all")

_GROUP_LEAD = re.compile(r"^\[([^\]]+)\]")
_GROUP_TRAIL = re.compile(r"-([A-Za-z][A-Za-z0-9]*)$")
_RES = re.compile(r"(?<![0-9a-z])((?:480|576|720|1080|2160|4320)[pi]|4k|\d{3,4}x\d{3,4})(?![0-9a-z])", re.I)
_CODEC = re.compile(r"(?<![0-9a-z])(x264|x265|h\.?264|h\.?265|hevc|avc|av1|vp9|10[ -]?bits?|8[ -]?bits?|"
                    r"hi10p?|flac|aac|ac-?3|e-?ac-?3|dts|opus)(?![0-9a-z])", re.I)


class _Sniffed:
    """The file reader shared by the sniff and the parser: one counter for every byte received.

    The first bytes are fetched once; a read inside them is answered from that copy, and one that
    runs past them asks the file only for the part beyond. So no byte is requested twice and
    ``received`` is exactly what the file was asked for.
    """

    def __init__(self, read, head):
        self._read, self.head, self.received = read, head, len(head)

    def __call__(self, offset, length):
        end, have = offset + length, len(self.head)
        if offset < 0 or length <= 0 or offset >= have:
            data = bytes(self._read(offset, length))
            self.received += len(data)
            return data
        if end <= have:
            return self.head[offset:end]
        tail = bytes(self._read(have, end - have))
        self.received += len(tail)
        return self.head[offset:] + tail


def probe_container(read, size, *, max_bytes=2 << 20):
    """Probe one file with the parser its first bytes call for.

    ``1A 45 DF A3`` is Matroska/WebM; an ISO BMFF box type (``ftyp``, ``moov``, ``mdat``,
    ``free``, ``skip``, ``wide``) at offset 4 is MP4. Anything else goes to ``mkvprobe``, which
    reports it ``unsupported_container`` (or ``invalid`` when too short). The extension is never
    consulted. The sniff is charged to `max_bytes` (the parser gets what is left), and the
    document's ``bytes_read`` is the total actually received from the file, sniff included.
    """
    n = min(SNIFF_BYTES, size) if isinstance(size, int) and size > 0 else 0
    if not isinstance(max_bytes, int) or n == 0 or max_bytes < n:
        return mkvprobe.probe(read, size, max_bytes=max_bytes)
    shared = _Sniffed(read, bytes(read(0, n))[:n])
    parser = mp4probe if shared.head[:4] != EBML_MAGIC and shared.head[4:8] in mp4probe.SNIFF_BOXES else mkvprobe
    result = parser.probe(shared, size, max_bytes=max_bytes - n)
    result["bytes_read"] = shared.received
    return result


def signature(name: str) -> tuple:
    """Name signature of a video file: release group, resolution tokens, codec tokens."""
    stem = name.rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    m = _GROUP_LEAD.match(stem) or _GROUP_TRAIL.search(stem)
    group = m.group(1).casefold() if m else ""
    res = tuple(sorted({t.casefold() for t in _RES.findall(stem)}))
    codec = tuple(sorted({re.sub(r"[ .-]", "", t.casefold()) for t in _CODEC.findall(stem)}))
    return group, res, codec


def check_args(library, sample, titles, max_files, max_read_bytes, per_file_bytes):
    if not isinstance(library, str) or not library:
        raise KuraError("invalid_arguments", "A library label is required")
    if sample not in SAMPLES:
        raise KuraError("invalid_arguments", "sample must be groups or all")
    if titles is not None and (not isinstance(titles, (list, tuple))
                               or not all(isinstance(t, str) and t for t in titles)):
        raise KuraError("invalid_arguments", "titles must be non-empty title keys")
    if not isinstance(max_files, int) or not 1 <= max_files <= MAX_FILES:
        raise KuraError("invalid_arguments", f"max_files must be 1..{MAX_FILES}")
    if not isinstance(max_read_bytes, int) or max_read_bytes < 1:
        raise KuraError("invalid_arguments", "max_read_bytes must be positive")
    if not isinstance(per_file_bytes, int) or not 1 <= per_file_bytes <= MAX_PER_FILE:
        raise KuraError("invalid_arguments", f"per_file_bytes must be 1..{MAX_PER_FILE}")


def check_state_dir(state_root, roots):
    """Refuse a state directory equal to, inside or above a library root: probing must never
    put its database or lock file where the library lives."""
    state = os.path.realpath(os.path.abspath(state_root))
    for root in roots.values():
        base = os.path.realpath(os.path.abspath(root))
        common = os.path.commonpath([state, base])
        if common in (state, base):                     # equal, inside or above (works for "/" too)
            raise KuraError("unsafe_state_dir", "The state directory overlaps a library root")


def _select(repo, library, titles, sample):
    """Deterministic ``[(title, file)]`` in (title key, file path) order, plus the title count."""
    rows = repo.titles(library)
    if titles:
        known = {t["rel_path"] for t in rows}
        for key in titles:
            if key not in known:
                raise KuraError("invalid_arguments", "Unknown title key", title=key)
        wanted = set(titles)
        rows = [t for t in rows if t["rel_path"] in wanted]
    picked, used = [], 0
    for title in rows:
        videos = [f for f in repo.title_files(title["id"]) if f["rel_path"].lower().endswith(VIDEO_EXT)]
        if sample == "groups":
            first = {}
            for f in videos:
                first.setdefault(signature(f["rel_path"]), f)
            videos = sorted(first.values(), key=lambda f: f["rel_path"])[:MAX_CLUSTERS]
        if videos:
            used += 1
        picked.extend((title, f) for f in videos)
    return picked, used


def _cached(db, title_id, f, per_file_bytes):
    row = db.execute("SELECT size, mtime_ns, probe_version, status, result FROM probe_results"
                     " WHERE title_id=? AND rel_path=?", (title_id, f["rel_path"])).fetchone()
    if (row is None or row[3] not in CONTENT_STATUSES or row[2] != mkvprobe.PROBE_VERSION
            or row[0] != f["size"] or row[1] != f["mtime_ns"]):
        return False
    try:
        stored = json.loads(row[4])
        if "budget_exhausted" not in stored.get("warnings", []):
            return True
        # Cut by the per-file byte limit: valid only for an allowance no larger than the one used.
        return per_file_bytes <= stored["allowance"]
    except (ValueError, AttributeError, KeyError, TypeError):
        return False


def _store(db, title_id, f, status, result, bytes_read):
    with db:
        db.execute(
            "INSERT INTO probe_results(title_id, rel_path, size, mtime_ns, probe_version, status,"
            " result, bytes_read, probed_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(title_id, rel_path) DO UPDATE SET size=excluded.size,"
            " mtime_ns=excluded.mtime_ns, probe_version=excluded.probe_version,"
            " status=excluded.status, result=excluded.result, bytes_read=excluded.bytes_read,"
            " probed_at=excluded.probed_at",
            (title_id, f["rel_path"], f["size"], f["mtime_ns"], mkvprobe.PROBE_VERSION, status,
             canonical(result) if result is not None else "{}", bytes_read, time.time()))


def probe_library(store, *, library, roots, sample="groups", titles=None, max_files=2000,
                  max_read_bytes=256 << 20, per_file_bytes=2 << 20, reprobe=False, should_stop=None):
    """Probe sampled video files of `library`. See the module docstring for the rules.

    `roots` maps a root label to its absolute directory (the label is the first component of
    a title key). Returns the counts document of ``kura probe library``.
    """
    check_args(library, sample, titles, max_files, max_read_bytes, per_file_bytes)
    check_state_dir(store.root, roots)
    repo = ManifestRepo(store.db)
    per_file_bytes = min(per_file_bytes, max_read_bytes)       # a run budget below the allowance is the allowance
    with store.lock():
        selected, title_count = _select(repo, library, titles, sample)
        stats = {"probed": 0, "cached": 0, "changed": 0, "bytes_read": 0}
        failed: dict[str, int] = {}
        handled = worked = 0            # worked: files actually probed this run (bounded by max_files)
        exhausted = False
        root_fds: dict[str, int | KuraError] = {}

        def fail(code):
            failed[code] = failed.get(code, 0) + 1

        try:
            for title, f in selected:
                if should_stop is not None and should_stop():
                    break
                label = title["rel_path"].partition("/")[0]
                if label not in roots:                      # a hit needs its root named and openable too
                    fail("no_root")
                    handled += 1
                    continue
                if label not in root_fds:
                    try:
                        root_fds[label] = rofs.open_root(roots[label])
                    except KuraError as exc:
                        root_fds[label] = exc
                root = root_fds[label]
                if isinstance(root, KuraError):
                    fail(root.code)
                    handled += 1
                    continue
                if os.fstat(root).st_dev != title["root_dev"]:
                    fail("root_mismatch")                   # not the filesystem this title was inventoried on
                    handled += 1
                    continue
                hit = not reprobe and _cached(store.db, title["id"], f, per_file_bytes)
                allowance = 0
                if not hit:
                    if worked >= max_files:
                        break
                    remaining = max_read_bytes - stats["bytes_read"]
                    if remaining <= 0:
                        exhausted = True
                        break
                    allowance = min(per_file_bytes, remaining)
                # Even a cache hit opens the file (no read) to prove it is the inventoried one here.
                outcome = _probe_file(root, f, allowance, per_file_bytes, check_only=hit)
                if outcome[0] == "cached":
                    stats["cached"] += 1
                    handled += 1
                    continue
                if outcome[0] == "budget":
                    stats["bytes_read"] += outcome[1]
                    exhausted = True
                    break
                status, result, used = outcome
                if result is not None and "budget_exhausted" in result["warnings"]:
                    result = {**result, "allowance": per_file_bytes}      # so a larger one retries it
                stats["bytes_read"] += used
                _store(store.db, title["id"], f, status, result, used)
                handled += 1
                if status == "changed":
                    stats["changed"] += 1
                elif status in CONTENT_STATUSES:
                    stats["probed"] += 1
                    worked += 1
                else:
                    fail(status)
        finally:
            for fd in root_fds.values():
                if isinstance(fd, int):
                    os.close(fd)
        return {"library": library,
                "sample": {"strategy": sample, "titles": title_count, "files_selected": len(selected)},
                **stats, "failed": failed, "budget_exhausted": exhausted,
                "pending": len(selected) - handled}


def _probe_file(root_fd, f, allowance, full, check_only=False):
    """``(status, result|None, bytes_read)``; ``("cached",)`` when `check_only` and the file is
    exactly the inventoried one; ``("budget", bytes_read)`` for a discarded probe."""
    try:
        fd = rofs.open_file_ro(root_fd, tuple(f["rel_path"].split("/")))
    except KuraError as exc:
        status = {"not_found": "changed", "unsafe_path": "unsafe"}.get(exc.code, "io_error")
        return status, None, 0
    used = 0

    def read(offset, n):
        nonlocal used
        data = os.pread(fd, n, offset)
        used += len(data)
        return data

    try:
        st = os.fstat(fd)
        if ((st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
                != (f["size"], f["mtime_ns"], f["dev"], f["ino"])):
            return "changed", None, 0           # not the file (or the version) the inventory saw
        if check_only:
            return ("cached",)
        try:
            result = probe_container(read, st.st_size, max_bytes=allowance)
        except OSError:
            return "io_error", None, used            # what was read before the failure still counts
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return "changed", None, used             # it moved under the reads: the result is mixed
    finally:
        os.close(fd)
    if allowance < full and "budget_exhausted" in result["warnings"]:
        return "budget", result["bytes_read"]      # cut by the run budget, not by the file: discard
    return result["status"], result, result["bytes_read"]
