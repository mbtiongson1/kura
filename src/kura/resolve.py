"""Find candidate torrents for library titles that have none on disk.

Most titles end up ``unmatched`` because no ``.torrent`` was ever kept. ``resolve`` searches
for candidates and fetches their *descriptors* (a few KiB to a few MiB of metadata, never
media) into a Kura-owned directory, ``--torrents-out``. The normal ``manifest build`` then
matches and piece-verifies them: a wrong candidate simply fails verification, and only a
100% verified one makes a title ``reacquirable``. So the search may be generous while
eligibility stays strict, and nothing here ever marks anything eligible.

What it reads and writes
  * Reads only the state database (title keys, file names, sizes). Never the media.
  * Writes only inside ``--torrents-out`` (``<infohash>.torrent`` descriptors, a small
    ``.kura-resolve.json`` attempt ledger and a lock file). The directory must already exist,
    must not be a symlink or look like a library (subdirectory or media inside), and is opened as a directory fd; every write is relative to that
    fd, a temp file then a rename, never replacing a descriptor. The state database is not
    written, except for the existing AniList alias cache.
  * Titles that are not ``unmatched`` (protected, blocked, stale, ...) are never searched, so
    their names are never sent anywhere.

The search is a *verification-purpose* path, separate from the acquisition path. The
acquisition search refuses anything that is not one well-delimited episode; this one wants
batches, so it parses the Nyaa RSS feed itself (``parse_candidates``, with the same XML
safety limits). Both share the hardened transport ``provider.fetch`` (canonical HTTPS origin,
public DNS check, no redirects, no proxies, size cap). Acquisition code is untouched.

Candidate choice
  * Search terms come from the folder name (group, resolution, codec, source and batch tags
    removed; a season marker is kept as one extra variant) plus up to two AniList titles
    (when enabled). At most ``MAX_TERMS`` queries per title, stopping early once enough
    candidates fit.
  * A candidate must fit the local size, within ``tolerance`` (2%) or the rounding of the
    size Nyaa displays if that is larger (``1.2 GiB`` is only known to about 4%), and its
    title must contain every word of some search name. Outside the window it can never
    verify 100%, so it is not fetched.
  * Ranking: closest size first, then release group equal to a group in the folder or its
    file names, then seeders. At most ``max_candidates`` descriptors are fetched per title.
    After the fetch the descriptor's non-padding file count is compared with the local one
    and reported as ``file_count_match`` (informational: a later approval of extra local
    files can still make a smaller torrent useful).
  * A descriptor is discarded, not written, if it is private, unparsable, does not hash to
    the advertised infohash, or any announce URL could carry a credential. With a protection
    policy (optional), a protected infohash is never requested and a protected descriptor
    (denylisted tracker or source) is discarded.

Politeness: one request per ``interval`` seconds (default 2) counted from the end of the
previous one, exponential backoff on HTTP 429/5xx and network errors, a total time budget,
and a stop after repeated failures. Output never contains announce URLs or credentials.

Resume: a title attempted within 7 days is skipped unless ``refresh``. Attempts that
failed for network reasons, or were cut short by the budget, are not recorded and are
simply retried by the next run.
"""
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import provider, rofs
from .core import Core
from .manifest import show
from .manifest_store import ManifestRepo
from .models import KuraError
from .protect import _public_tracker, classify
from .releases import normalize
from .torrentmeta import parse, release_group

INTERVAL = 2.0                  # seconds between two Nyaa requests
TOLERANCE = 0.02                # total size window, relative
MAX_TERMS = 4                   # queries per title
MAX_ALIASES = 2                 # AniList titles added per title
MAX_CANDIDATES = 10
ATTEMPT_TTL = 7 * 24 * 3600
BUDGET_SECONDS = 30 * 60
FEED_LIMIT = 2 * 1024**2
DESCRIPTOR_LIMIT = 8 * 1024**2
LEDGER = ".kura-resolve.json"
LOCK = ".kura-resolve.lock"
LEDGER_LIMIT = 16 * 1024**2
MAX_RETRIES = 2                 # per request, after the first try
BACKOFF_BASE = 4.0
BACKOFF_CAP = 120.0
MAX_CONSECUTIVE_FAILURES = 3    # exhausted requests in a row end the run
MAX_LISTED = 200
MAX_TITLE_KEYS = 100_000
CATEGORIES = ("1_2", "1_3", "1_4")
USER_TERM_LIMIT = 120
MAX_OUT_ENTRIES = 200_000

_EXT = re.compile(r"\.(mkv|mp4|avi|m4v|webm|mov)\Z", re.I)
_SEASONS = (
    re.compile(r"(?i)(?<!\w)S(\d{1,2})(?:\s*-\s*S?(\d{1,2}))?(?!\w)"),
    re.compile(r"(?i)\bSeason\s*(\d{1,2})\b"),
    re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\s+Season\b"),
)
_PAIRS = re.compile(r"(?i)\b(?:dual|multi)[ -]?(?:audio|subs?)\b")
_DROP = re.compile(
    r"(?i)(?:\d{3,4}p|4k|\d{3,4}x\d{3,4}|[xh]\.?26[45]|hevc|avc|av1|10-?bits?|8-?bits?|"
    r"bd(?:rip|remux)?|blu-?ray|web-?dl|web-?rip|web|dvd(?:rip)?|remux|aac(?:\d(?:\.\d)?)?|"
    r"flac|opus|ac-?3|dts|ddp?\d?(?:\.\d)?|batch|complete|uncensored|v\d)")
_NOISE_TAG = re.compile(
    r"(?i)(?:\d{3,4}p|\d{3,4}x\d{3,4}|[0-9a-f]{8}|(?:19|20)\d{2}|\d+-?bits?|v\d|"
    r"[xh]\.?26[45]|hevc|avc|av1|aac|flac|opus|bd|bdrip|bluray|blu-ray|web|webrip|web-dl|dl|dvd|"
    r"dual|audio|multi|subs?|batch|complete|remux|eng|uncensored|ddp?\d?|ac-?3)")
_SIZE = re.compile(r"(\d+(?:\.(\d+))?) (B|KiB|MiB|GiB|TiB)")
_UNIT = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
_ID = re.compile(r"[1-9]\d{0,11}\Z")
_HASH = re.compile(r"[0-9a-f]{40}\Z")


def _bad(message: str, **details):
    return KuraError("invalid_arguments", message, **details)


# ---- search terms ----------------------------------------------------------------------

@dataclass(frozen=True)
class Terms:
    base: str                       # folder name reduced to a title, no season marker
    season: int | None
    queries: tuple[str, ...]        # what is sent to the provider, most specific first
    names: tuple[str, ...]          # normalized titles a candidate must contain
    groups: tuple[str, ...]         # release groups found in the folder name


def _is_noise_tag(tag: str) -> bool:
    parts = [p for p in re.split(r"[\s._,;]+", tag.strip()) if p]
    return not parts or all(_NOISE_TAG.fullmatch(p) for p in parts)


def _flatten(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", text).replace("_", " ")
    if " " not in text.strip():              # scene style: Name.Of.Series.S01.1080p.x264-Group
        text = re.sub(r"-[A-Za-z0-9]+\Z", "", text.strip()).replace(".", " ")
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"['’‘`]", "", text)
    text = re.sub(r"[^\w\s]", " ", text.replace("_", " "))
    text = " ".join(text.split())
    if len(text) > USER_TERM_LIMIT:
        text = text[:USER_TERM_LIMIT].rsplit(" ", 1)[0]
    return text


def _season(text: str) -> tuple[str, int | None]:
    """Remove season markers; return the single season number, or None (none or a range)."""
    numbers, ranged = set(), False
    for pattern in _SEASONS:
        for found in pattern.finditer(text):
            numbers.add(int(found.group(1)))
            if pattern.groups > 1 and found.group(2):
                ranged = True
        text = pattern.sub(" ", text)
    return text, (next(iter(numbers)) if len(numbers) == 1 and not ranged else None)


def _name(text: str) -> tuple[str, int | None]:
    """A release folder/file name reduced to ``(title, season)``."""
    text = _PAIRS.sub(" ", _flatten(_EXT.sub("", text.strip())))
    text, season = _season(text)
    kept = [t for t in text.split() if not _DROP.fullmatch(t)]
    return _tidy(" ".join(kept)), season


def _clean_alias(text: str) -> str:
    return _tidy(" ".join(unicodedata.normalize("NFKC", text).split()))


def leading_group(name: str) -> str | None:
    """Leading ``[Group]`` tag unless it is a resolution, codec, CRC or similar tag."""
    group = release_group(name.lstrip())
    return None if group is None or _is_noise_tag(group) else group


def derive_terms(entry: str, alias_titles=(), file_names=()) -> Terms:
    """Search terms for one title. `entry` is the folder (or file) name below the root.

    `alias_titles` are AniList spellings (at most ``MAX_ALIASES`` distinct extra ones are
    used); `file_names` are the base names of the title's files, only used to find the
    release group when the folder name has none.
    """
    base, season = _name(entry)
    names = [base] if normalize(base) else []
    for alias in alias_titles:
        cleaned = _clean_alias(alias) if isinstance(alias, str) else ""
        if (normalize(cleaned) and normalize(cleaned) not in {normalize(n) for n in names}
                and len(names) <= MAX_ALIASES):
            names.append(cleaned)
    ordered = []
    for n in names:
        if season:
            ordered.append(f"{n} S{season:02d}")
        ordered.append(n)
    queries, seen = [], set()
    for query in ordered:
        key = normalize(query)
        if key and key not in seen:
            seen.add(key)
            queries.append(query)
    groups = []
    group = leading_group(entry)
    if group:
        groups.append(group)
    counts = Counter(g for g in map(leading_group, file_names) if g)
    wanted = max(1, len(file_names) // 2)
    for tag, n in counts.most_common(2):
        if n >= wanted and tag.casefold() not in {g.casefold() for g in groups}:
            groups.append(tag)
    return Terms(base, season, tuple(queries[:MAX_TERMS]),
                 tuple(dict.fromkeys(normalize(n) for n in names)), tuple(groups))


# ---- provider results ------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    nyaa_id: str
    title: str
    size_bytes: int
    slack: float                    # how far the displayed size can be from the real one
    seeders: int
    infohash: str
    url: str
    group: str | None


def search_url(query: str) -> str:
    """Nyaa RSS search over all anime categories, biggest first (batches before episodes)."""
    return "https://nyaa.si/?" + urllib.parse.urlencode(
        {"page": "rss", "q": query, "c": "1_0", "f": "0", "s": "size", "o": "desc"})


def parse_candidates(data: bytes) -> list[Candidate]:
    """Batch-aware Nyaa RSS parser (verification only). Fails closed per item.

    Keeps the acquisition parser's XML safety rules (UTF-8, size, no DTD or entities, at
    most 100 items, canonical download URL, 40-hex infohash) but, unlike it, accepts
    batches, ranges and seasons: whether a candidate is right is decided by piece hashes.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as e:
        raise KuraError("invalid_feed", "Provider XML must be UTF-8") from e
    if (len(data) > FEED_LIMIT or "\x00" in text or "<!DOCTYPE" in text.upper()
            or "<!ENTITY" in text.upper()):
        raise KuraError("invalid_feed", "Oversized or unsafe XML")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise KuraError("invalid_feed", "Invalid provider XML") from e
    rows = {}
    for item in root.findall("./channel/item")[:100]:
        def val(name):
            return item.findtext(name, default="").strip()
        title, url = val("title"), val("link")
        infohash = val(provider.NS + "infoHash").lower()
        size = _SIZE.fullmatch(val(provider.NS + "size"))
        try:
            provider.validate_url(url, True)
            if (not title or len(title) > 400 or any(ord(c) < 32 or ord(c) == 127 for c in title)
                    or not size or not _HASH.match(infohash)
                    or val(provider.NS + "categoryId") not in CATEGORIES):
                continue
            seeders = int(val(provider.NS + "seeders"))
            nyaa_id = url.rsplit("/", 1)[1].split(".")[0]
            if not 0 <= seeders <= 1_000_000 or not _ID.match(nyaa_id):
                continue
            unit = _UNIT[size[3]]
            decimals = len(size[2] or "")
            count = int(float(size[1]) * unit)
            if count <= 0:
                continue
            rows.setdefault(infohash, Candidate(
                nyaa_id, title, count, 0.5 * unit * 10.0 ** -decimals, seeders, infohash, url,
                release_group(title)))
        except (KuraError, ValueError, OverflowError):
            continue
    return list(rows.values())


def rank(candidates, *, local_bytes: int, names, groups, entry: str | None = None,
         tolerance: float = TOLERANCE) -> list[dict]:
    """Candidates that fit the local title, best first. Each row: candidate + evidence.

    Fit = total size within `tolerance` (or the displayed size's rounding) of `local_bytes`
    and a title containing every word of at least one of `names`. Order: closer size
    (essentially exact first), a title identical to the folder name `entry` (``manifest
    build`` only links a torrent whose name equals the folder name), group match, more
    seeders, then infohash for determinism.
    """
    wanted = [set(n.split()) for n in names if n]
    local_groups = {g.casefold() for g in groups}
    rows = []
    for c in candidates:
        if local_bytes <= 0 or not wanted:
            break
        diff = abs(c.size_bytes - local_bytes)
        if diff > max(tolerance * local_bytes, c.slack):
            continue
        if not any(w <= set(normalize(c.title).split()) for w in wanted):
            continue
        exact = diff <= max(0.002 * local_bytes, c.slack)
        rows.append({"candidate": c, "size_ratio": round(diff / local_bytes, 6), "exact_size": exact,
                     "name_match": entry is not None and c.title == _EXT.sub("", entry),
                     "group_match": bool(c.group and c.group.casefold() in local_groups)})
    rows.sort(key=lambda r: (not r["exact_size"], not r["name_match"], not r["group_match"],
                             -r["candidate"].seeders, r["size_ratio"], r["candidate"].infohash))
    return rows


# ---- pacing ----------------------------------------------------------------------------

def transient(error: KuraError) -> bool:
    """Worth retrying: a network error, HTTP 429 or a 5xx. A 404 or other 4xx is final."""
    status = error.details.get("status")
    return error.code == "provider_unavailable" and (
        status is None or status == 429 or (isinstance(status, int) and status >= 500))


class BudgetExhausted(KuraError):
    def __init__(self):
        super().__init__("budget_exhausted", "Time budget spent; run again to continue")


class Pacer:
    """Every provider request goes through here: spacing, backoff, budget, circuit breaker."""

    def __init__(self, fetcher, *, interval=INTERVAL, budget=BUDGET_SECONDS, max_retries=MAX_RETRIES,
                 backoff_base=BACKOFF_BASE, backoff_cap=BACKOFF_CAP, sleep=time.sleep,
                 clock=time.monotonic):
        self.fetcher, self.interval, self.budget = fetcher, interval, budget
        self.max_retries, self.backoff_base, self.backoff_cap = max_retries, backoff_base, backoff_cap
        self.sleep, self.clock = sleep, clock
        self.started = clock()
        self.next_ok = self.started       # the first request needs no wait
        self.requests = self.retries = self.failures = 0   # failures = exhausted, in a row

    def elapsed(self) -> float:
        return self.clock() - self.started

    def expired(self) -> bool:
        return self.elapsed() >= self.budget

    def _wait(self):
        now = self.clock()
        wait = max(self.next_ok - now, 0.0)
        if (now - self.started) + wait >= self.budget:
            raise BudgetExhausted()
        if wait:
            self.sleep(wait)

    def get(self, url: str, limit: int, descriptor: bool = False) -> bytes:
        attempt = 0
        while True:
            self._wait()
            self.requests += 1
            try:
                data = self.fetcher(url, limit, descriptor)
            except KuraError as e:
                self.next_ok = self.clock() + self.interval
                if not transient(e):
                    raise
                self.next_ok = max(self.next_ok, self.clock() + min(
                    self.backoff_base * 2 ** attempt, self.backoff_cap))
                if attempt >= self.max_retries:
                    self.failures += 1
                    raise
                attempt += 1
                self.retries += 1
                continue
            self.next_ok = self.clock() + self.interval
            self.failures = 0
            return data

    @property
    def tripped(self) -> bool:
        return self.failures >= MAX_CONSECUTIVE_FAILURES


# ---- the output directory --------------------------------------------------------------

class OutDir:
    """``--torrents-out`` as a directory fd: every write is relative to it, nothing else exists.

    Refuses a missing directory, a symlink anywhere in the path, a concurrent resolve and a
    directory that looks like a library (a subdirectory or a media file inside it).
    """

    def __init__(self, path):
        path = Path(os.path.abspath(path))
        self.fd = rofs.open_root(path)         # no symlink components; raises KuraError
        self.lock_fd = None
        try:
            self._refuse_library_like()
            self.lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                   0o600, dir_fd=self.fd)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise KuraError("busy", "Another resolve is using this directory")
        except OSError as e:
            self.close()
            raise KuraError("io_error", f"Output directory is not writable: {e.strerror}") from e
        except BaseException:
            self.close()
            raise

    def _refuse_library_like(self):
        """A flat directory of descriptors never holds subdirectories or media. A library root does."""
        try:
            with os.scandir(self.fd) as it:
                for seen, entry in enumerate(it, 1):
                    if seen > MAX_OUT_ENTRIES:
                        raise KuraError("unsafe_output_dir", "Output directory has too many entries")
                    if entry.is_dir(follow_symlinks=False) or _EXT.search(entry.name):
                        raise KuraError("unsafe_output_dir",
                                        "Output directory looks like a library: it holds a subdirectory or media")
        except OSError as e:
            raise KuraError("io_error", f"Output directory could not be listed: {e.strerror}") from e

    def close(self):
        for name in ("lock_fd", "fd"):
            value = getattr(self, name, None)
            if value is not None:
                try:
                    os.close(value)
                except OSError:
                    pass
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def kind(self, name: str):
        """``stat.S_IFMT`` of a direct child, or None when absent. Never follows symlinks."""
        try:
            return stat.S_IFMT(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return None

    def _put(self, name: str, data: bytes):
        temp = ".tmp-" + secrets.token_hex(8)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except OSError:
                pass
            raise

    def write_descriptor(self, infohash: str, data: bytes) -> str:
        """``written``, or ``present`` when that infohash is already a regular file here."""
        name = infohash + ".torrent"
        kind = self.kind(name)
        if kind == stat.S_IFREG:
            return "present"
        if kind is not None:
            raise KuraError("unsafe_path", "A non-regular entry blocks this descriptor name")
        self._put(name, data)
        return "written"

    def load_ledger(self) -> dict:
        """``{library: {title key: {"at", "outcome", "infohashes"}}}``; unreadable means empty."""
        try:
            fd = os.open(LEDGER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=self.fd)
        except OSError:
            return {}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > LEDGER_LIMIT:
                return {}
            with os.fdopen(fd, "rb", closefd=False) as handle:
                document = json.loads(handle.read(LEDGER_LIMIT + 1).decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        finally:
            os.close(fd)
        out = {}
        attempts = document.get("attempts") if isinstance(document, dict) else None
        for library, titles in (attempts.items() if isinstance(attempts, dict) else ()):
            if not isinstance(titles, dict):
                continue
            for key, row in titles.items():
                if (isinstance(row, dict) and isinstance(row.get("at"), (int, float))
                        and not isinstance(row.get("at"), bool) and isinstance(row.get("outcome"), str)):
                    out.setdefault(library, {})[key] = {
                        "at": float(row["at"]), "outcome": row["outcome"][:40],
                        "infohashes": [h for h in row.get("infohashes") or []
                                       if isinstance(h, str) and _HASH.match(h)][:MAX_CANDIDATES]}
        return out

    def save_ledger(self, ledger: dict):
        document = {"version": 1, "attempts": ledger}
        self._put(LEDGER, json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii"))


# ---- the run ---------------------------------------------------------------------------

def check_args(library, titles, max_titles, max_candidates, budget_seconds, interval=INTERVAL):
    if not isinstance(library, str) or not library:
        raise _bad("A library name is required")
    if titles is not None and (not isinstance(titles, list) or len(titles) > MAX_TITLE_KEYS
                               or not all(isinstance(t, str) and t for t in titles)):
        raise _bad("Titles must be a list of title keys")
    if max_titles is not None and (type(max_titles) is not int or not 1 <= max_titles <= MAX_TITLE_KEYS):
        raise _bad("max_titles must be a whole number from 1 to %d" % MAX_TITLE_KEYS)
    if type(max_candidates) is not int or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise _bad("max_candidates must be a whole number from 1 to %d" % MAX_CANDIDATES)
    for name, value, high in (("budget_seconds", budget_seconds, 24 * 3600), ("interval", interval, 3600)):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or value != value
                or not 0 < value <= high):
            raise _bad(f"{name} must be a positive number of at most {high}")


def _recent(row, now: float, refresh: bool) -> bool:
    if refresh or not row:
        return False
    age = now - row["at"]
    return -300 <= age < ATTEMPT_TTL      # a timestamp far in the future is a skewed clock: retry


def _file_names(files: list[dict]) -> list[str]:
    return [f["rel_path"].rsplit("/", 1)[-1] for f in files if _EXT.search(f["rel_path"])]


def _describe(item: dict, c: Candidate, outcome: str, meta=None, local_files: int | None = None):
    row = {"infohash": c.infohash, "nyaa_id": c.nyaa_id, "name": c.title, "size_bytes": c.size_bytes,
           "seeders": c.seeders, "release_group": c.group, "size_ratio": item["size_ratio"],
           "name_match": item["name_match"], "group_match": item["group_match"],
           "outcome": outcome}
    if meta is not None and local_files is not None:
        row["file_count_match"] = sum(not f.pad for f in meta.files) == local_files
    return row


def _accept(data: bytes, candidate: Candidate, policy=None):
    """``(meta, None)`` for a descriptor worth keeping, else ``(None, reason)``. Fails closed."""
    try:
        meta = parse(data)
    except KuraError as e:
        return None, e.code
    except Exception:
        return None, "unparsable"
    if meta.infohash != candidate.infohash:
        return None, "hash_mismatch"
    if meta.private:
        return None, "private"
    if not meta.announce_valid or not all(_public_tracker(u) for u in meta.announce_urls):
        return None, "announce_not_public"      # may carry a credential: never written
    if policy is not None:
        try:
            if classify(meta, policy).protected:
                return None, "protected"
        except Exception:
            return None, "unclassifiable"
    return meta, None


def _search(pacer: Pacer, terms: Terms, local_bytes: int, groups, entry: str, tolerance: float,
            wanted: int):
    """Query terms in order until `wanted` candidates fit.

    Returns ``(ranked, found, searched, flaky)``: `flaky` counts queries that failed
    for network reasons (worth retrying), not ones that returned an unusable feed.
    """
    seen: dict[str, Candidate] = {}
    searched = flaky = 0
    ranked: list[dict] = []
    for query in terms.queries:
        try:
            found = parse_candidates(pacer.get(search_url(query), FEED_LIMIT))
        except BudgetExhausted:
            raise
        except KuraError as e:
            if e.code == "provider_unavailable" and pacer.tripped:
                raise
            flaky += transient(e)
            continue
        searched += 1
        for c in found:
            seen.setdefault(c.infohash, c)
        ranked = rank(seen.values(), local_bytes=local_bytes, names=terms.names, groups=groups,
                      entry=entry, tolerance=tolerance)
        if len(ranked) >= wanted:
            break
    return ranked, len(seen), searched, flaky


def _process(key, row, files, *, pacer, out, core, max_candidates, tolerance, policy=None):
    """One title. Returns ``(result, record)``; `record` False means retry on the next run."""
    entry = key.split("/", 1)[1] if "/" in key else key
    names = _file_names(files)
    terms = derive_terms(entry, (), names)
    alias = {"status": "disabled"}
    if core is not None and terms.base:
        try:
            alias = core.alias_status(terms.base)
        except KuraError as e:
            alias = {"status": "unavailable", "reason": e.code}
        if alias.get("status") == "resolved":
            terms = derive_terms(entry, alias.get("titles") or (), names)
    result = {"title": key, "anilist": alias.get("status"), "queries": list(terms.queries),
              "candidates": []}
    if not terms.queries:
        return {**result, "outcome": "no_terms"}, True
    ranked, found, searched, flaky = _search(pacer, terms, row["total_bytes"], terms.groups,
                                                 entry, tolerance, max_candidates)
    result["results"] = found
    if searched == 0:
        return {**result, "outcome": "search_failed"}, False
    # Any transient failure means the picture is incomplete: do not record an attempt.
    incomplete = flaky > 0
    if policy is not None:      # a protected infohash is never even requested
        listed = [r for r in ranked if r["candidate"].infohash not in policy.infohashes]
        result["skipped_protected"] = len(ranked) - len(listed)
        ranked = listed
    if not ranked:
        return {**result, "outcome": "no_results" if not found else "no_candidates",
                **({"incomplete": True} if incomplete else {})}, not incomplete
    kept = []
    for item in ranked[:max_candidates]:
        c = item["candidate"]
        if out.kind(c.infohash + ".torrent") == stat.S_IFREG:
            kept.append(c.infohash)
            result["candidates"].append(_describe(item, c, "present"))
            continue
        try:
            data = pacer.get(c.url, DESCRIPTOR_LIMIT, True)
        except BudgetExhausted:
            raise
        except KuraError as e:
            incomplete = incomplete or transient(e)
            result["candidates"].append(_describe(item, c, "fetch_failed:" + e.code))
            if pacer.tripped:
                break
            continue
        meta, reason = _accept(data, c, policy)
        if meta is None:
            result["candidates"].append(_describe(item, c, "discarded:" + reason))
            continue
        try:
            state = out.write_descriptor(c.infohash, data)
        except (OSError, KuraError) as e:
            code = e.code if isinstance(e, KuraError) else "io_error"
            incomplete = True
            result["candidates"].append(_describe(item, c, "write_failed:" + code))
            continue
        kept.append(c.infohash)
        result["candidates"].append(_describe(item, c, state, meta, row["file_count"]))
    result["infohashes"] = kept
    outcome = "written" if kept else "fetch_failed" if incomplete else "discarded"
    if incomplete:
        result["incomplete"] = True
    return {**result, "outcome": outcome}, not incomplete


def resolve(store, *, library: str, torrents_out, max_titles: int | None = None,
            max_candidates: int = 3, titles: list[str] | None = None, refresh: bool = False,
            aliases=None, budget_seconds: float = BUDGET_SECONDS, tolerance: float = TOLERANCE,
            interval: float = INTERVAL, policy=None, fetcher=None, sleep=time.sleep,
            clock=time.monotonic, wall=time.time, should_stop=lambda: False) -> dict:
    """Search for and fetch candidate descriptors for ``unmatched`` titles. See module docstring."""
    check_args(library, titles, max_titles, max_candidates, budget_seconds, interval)
    pacer = Pacer(fetcher or provider.fetch, interval=interval, budget=float(budget_seconds),
                  sleep=sleep, clock=clock)
    with OutDir(torrents_out) as out:
        ledger = out.load_ledger()
        mine = ledger.setdefault(library, {})
        now = wall()
        skipped = {"not_unmatched": 0, "attempted_recently": 0, "empty": 0, "not_in_manifest": 0}
        notes: list[dict] = []
        work: list[tuple[str, dict, list[dict]]] = []
        with store.lock():       # a consistent read; released before any network I/O
            repo = ManifestRepo(store.db)
            shown = {t["title"]: t for t in show(store, library)["titles"]}
            rows = {r["rel_path"]: r for r in repo.titles(library)}
            wanted = sorted(set(titles)) if titles is not None else sorted(shown)
            for key in wanted:
                info, row = shown.get(key), rows.get(key)
                if info is None or row is None:
                    skipped["not_in_manifest"] += 1
                    notes.append({"title": key, "skipped": "not_in_manifest"})
                    continue
                if info["status"] != "unmatched":
                    skipped["not_unmatched"] += 1
                    if titles is not None:
                        notes.append({"title": key, "skipped": "status_" + info["status"]})
                    continue
                if row["total_bytes"] <= 0 or row["file_count"] <= 0:
                    skipped["empty"] += 1
                    continue
                if _recent(mine.get(key), now, refresh):
                    skipped["attempted_recently"] += 1
                    continue
                if max_titles is not None and len(work) >= max_titles:
                    continue
                work.append((key, row, repo.title_files(row["id"])))

        core = Core(store, None, aliases) if aliases is not None else None
        results, outcomes = [], Counter()
        aborted = None
        interrupted = budget_hit = False
        for key, row, files in work:
            if should_stop():
                interrupted = True
                break
            if pacer.expired():
                budget_hit = True
                break
            try:
                result, record = _process(key, row, files, pacer=pacer, out=out, core=core,
                                          max_candidates=max_candidates, tolerance=tolerance,
                                          policy=policy)
            except BudgetExhausted:
                budget_hit = True
                break
            except KuraError as e:
                if e.code != "provider_unavailable":
                    raise
                result, record = {"title": key, "outcome": "search_failed", "candidates": []}, False
            outcomes[result["outcome"]] += 1
            results.append(result)
            if record:
                mine[key] = {"at": wall(), "outcome": result["outcome"],
                             "infohashes": result.get("infohashes", [])}
                out.save_ledger(ledger)
            if pacer.tripped:
                aborted = "provider_unavailable"
                break
        written = sum(c["outcome"] == "written" for r in results for c in r["candidates"])
        return {
            "library": library, "processed": len(results), "pending": len(work) - len(results),
            "outcomes": dict(sorted(outcomes.items())), "descriptors_written": written,
            "descriptors_present": sum(c["outcome"] == "present" for r in results for c in r["candidates"]),
            "skipped": skipped, "budget_exhausted": budget_hit, "interrupted": interrupted,
            "aborted": aborted,
            "requests": {"made": pacer.requests, "retries": pacer.retries,
                         "elapsed_seconds": round(pacer.elapsed(), 1), "interval_seconds": interval},
            "titles": results[:MAX_LISTED], "titles_truncated": max(len(results) - MAX_LISTED, 0),
            "notes": notes[:MAX_LISTED],
            "next": "run: kura manifest build (offline) to match and piece-verify the candidates"}
