"""SeaDex release recommendations: a bounded, read-only mirror in Kura's private state.

SeaDex publishes curated "best release" picks per AniList season ID over an unauthenticated
PocketBase REST API. Everything it returns is untrusted data: it may inform a later audit or
a proposal, but it never authorizes a download, and a record that is not a public Nyaa pick
never exposes an infohash. Rules:

  * Transport: https only, host exactly ``seadex.moe``, only the two collection record
    paths, the query is built by code from validated values (integers, 40-hex infohashes,
    record ids, timestamps), no redirects, no proxy inheritance, DNS must be public, the
    response is bounded and every request goes through the shared ``seadex`` limiter.
  * Sync: a full pull replaces every ``seadex_*`` row in ONE transaction (records deleted
    upstream disappear); an incremental pull upserts what changed since the cursor. A failed
    run keeps the old rows and records ``status=failed``.
  * ``public`` means: tracker is Nyaa (any case), the infohash is 40-hex and the url is
    exactly a Nyaa view link. Anything else stores ``public=0`` and a NULL infohash.
  * Free text is sanitized (control and format characters removed, bounded); a malformed
    record is skipped and counted, never repaired.
"""
import ipaddress
import json
import re
import socket
import statistics
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from ..models import KuraError
from ..provider import NoRedirect
from ..ratelimit import SharedLimiter

HOST = "seadex.moe"
COLLECTIONS = ("entries", "torrents")
USER_AGENT = "Kura/0.1 (+https://github.com/mbtiongson1/kura)"
LIMIT = 8 * 1024 * 1024          # response cap
RUN_LIMIT = 64 * 1024 * 1024     # bytes read across one whole run
INTERVAL = 1.0                   # seconds between request starts
MAX_REQUESTS = 60                # default per run
MAX_REQUESTS_CAP = 500
PER_PAGE = 500
MAX_PAGES = 100                  # per collection
FILES_BATCH = 40
OVERLAP = 300                    # seconds: a cursor never passes (run start - OVERLAP)
FULL_EVERY = 7 * 86400           # seconds; also the staleness threshold
WAIT_BUDGET = 120.0              # longest single wait for the shared limiter
SOURCE = "seadex"

ENTRY_FIELDS = ("id", "alID", "incomplete", "notes", "theoreticalBest", "comparison", "trs", "updated")
TORRENT_FIELDS = ("id", "infoHash", "releaseGroup", "tracker", "url", "isBest", "dualAudio", "tags",
                  "updated")
FILES_FIELDS = ("id", "files")

MAX_TEXT = 4000
MAX_TAGS, MAX_TAG = 20, 40
MAX_COMPARISON, MAX_URL = 20, 300
MAX_TRS = 200
MAX_FILES_STORED = 2000
MAX_FILES_SCANNED = 20000
MAX_NAME = 255
MAX_BYTES = 2**50
MAX_TOTAL = 2**62                # keeps every stored sum inside SQLite's signed 64-bit integers
MAX_LIBRARY = 200

RECORD_ID = re.compile(r"[a-z0-9]{1,32}")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z")
INFOHASH = re.compile(r"[0-9a-fA-F]{40}")
NYAA_VIEW = re.compile(r"https://nyaa\.si/view/([0-9]{1,12})")
FIELD_NAME = re.compile(r"[A-Za-z]{1,32}")

VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".m2ts", ".ts", ".avi", ".webm", ".mov", ".wmv")
EXTRA_NAME = re.compile(r"(?<![A-Za-z0-9])(NCOP|NCED|OP|ED|PV|CM|Preview|Menu|Trailer)"
                        r"(?:[ _.-]?[0-9]{1,3})?(?![A-Za-z0-9])", re.IGNORECASE)
EXTRA_DIR = re.compile(r"(extras?|ncs?|bonus(es)?)", re.IGNORECASE)


# ---- query builders (the only way a URL is made) ------------------------------------

def record_id(value) -> str:
    if not isinstance(value, str) or not RECORD_ID.fullmatch(value):
        raise KuraError("invalid_arguments", "Invalid SeaDex record id")
    return value


def timestamp(value) -> str:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise KuraError("invalid_arguments", "Invalid SeaDex timestamp")
    return value


def anilist_id(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 10**9:
        raise KuraError("invalid_arguments", "AniList ID must be an integer in 1..999999999")
    return value


def infohash(value) -> str:
    if not isinstance(value, str) or not INFOHASH.fullmatch(value):
        raise KuraError("invalid_arguments", "Infohash must be 40 hexadecimal characters")
    return value.lower()


def canonical_stamp(value) -> str:
    """Validated timestamp with a fixed nine-digit fraction, so string order is time order.

    Longer fractions are truncated (an earlier cursor only re-reads more, never less)."""
    value = timestamp(value)
    base, _, rest = value[:-1].partition(".")
    return f"{base}.{rest.ljust(9, '0')[:9]}Z"


def filter_updated(since) -> str:
    """Records at or after `since`: the boundary record is fetched again (upserts are idempotent),
    so a record sharing the cursor's timestamp is never missed."""
    return f'updated>="{canonical_stamp(since)}"'


def filter_ids(ids) -> str:
    ids = [record_id(i) for i in ids]
    if not 1 <= len(ids) <= FILES_BATCH:
        raise KuraError("invalid_arguments", "A files request names 1..40 records")
    return "||".join(f'id="{i}"' for i in ids)


def build_url(collection: str, *, page: int, per_page: int, fields, filter_: str | None = None) -> str:
    """The only constructor of request URLs; `filter_` must come from the filter_* helpers."""
    if collection not in COLLECTIONS:
        raise KuraError("invalid_arguments", "Unknown SeaDex collection")
    if (not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= MAX_PAGES or
            not isinstance(per_page, int) or isinstance(per_page, bool) or not 1 <= per_page <= PER_PAGE):
        raise KuraError("invalid_arguments", "Invalid SeaDex paging")
    if not fields or not all(isinstance(f, str) and FIELD_NAME.fullmatch(f) for f in fields):
        raise KuraError("invalid_arguments", "Invalid SeaDex field list")
    query = [("page", str(page)), ("perPage", str(per_page)), ("sort", "id"), ("fields", ",".join(fields))]
    if filter_ is not None:
        query.append(("filter", filter_))
    url = (f"https://{HOST}/api/collections/{collection}/records?"
           + urllib.parse.urlencode(query, quote_via=urllib.parse.quote))
    validate_url(url)
    return url


def validate_url(url: str):
    u = urllib.parse.urlsplit(url)
    parts = u.path.split("/")
    if (u.scheme != "https" or u.netloc != HOST or u.fragment or len(parts) != 5 or
            parts[:3] != ["", "api", "collections"] or parts[3] not in COLLECTIONS or
            parts[4] != "records" or any(ord(c) < 33 or ord(c) > 126 for c in url)):
        raise KuraError("unsafe_url", "Only SeaDex collection record URLs are allowed")


# ---- transport ----------------------------------------------------------------------

def _retry_after(headers) -> float | None:
    try:
        value = float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value == value and 0 <= value < 10**7 else None


def fetch(url: str) -> bytes:
    """One bounded GET. HTTP 429 raises ``seadex_unavailable`` with ``status`` and ``retry_after``."""
    validate_url(url)
    try:
        addresses = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise KuraError("unsafe_url", "SeaDex DNS resolved to a non-public address")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": USER_AGENT})
        with opener.open(request, timeout=20) as response:
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise KuraError("seadex_invalid", "SeaDex response exceeds the size limit")
        return data
    except urllib.error.HTTPError as e:
        details = {"status": e.code}
        if e.code == 429:
            details["retry_after"] = _retry_after(getattr(e, "headers", None))
        raise KuraError("seadex_unavailable", "SeaDex request failed; retry later", **details) from e
    except (OSError, urllib.error.URLError) as e:
        raise KuraError("seadex_unavailable", "SeaDex request failed; retry later") from e


# ---- sanitizing ---------------------------------------------------------------------

def clean_text(value, limit: int) -> str | None:
    """Untrusted text -> single-line text without control/format characters, bounded."""
    if not isinstance(value, str):
        return None
    out = []
    for c in value:
        if c in "\n\r\t":
            out.append(" ")
        elif unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            out.append(c)
    return "".join(out).strip()[:limit]


def _flag(value):
    return value if isinstance(value, bool) else None


def _urls(value) -> list[str]:
    """Comma-separated comparison links -> at most 20 plain https URLs."""
    if not isinstance(value, str):
        return []
    out = []
    for part in value.split(","):
        part = part.strip()
        try:
            u = urllib.parse.urlsplit(part)
            host_ok = bool(u.hostname) and u.username is None and u.password is None
        except ValueError:
            continue
        if (host_ok and u.scheme == "https" and len(part) <= MAX_URL and part.isascii() and
                all(32 < ord(c) < 127 for c in part) and part not in out):
            out.append(part)
        if len(out) == MAX_COMPARISON:
            break
    return out


def parse_entry(item) -> dict | None:
    """One entries record -> row dict, or None when malformed."""
    if not isinstance(item, dict):
        return None
    rid, al, updated = item.get("id"), item.get("alID"), item.get("updated")
    incomplete = _flag(item.get("incomplete"))
    notes, best = clean_text(item.get("notes", ""), MAX_TEXT), clean_text(item.get("theoreticalBest", ""), MAX_TEXT)
    if (not isinstance(rid, str) or not RECORD_ID.fullmatch(rid) or not isinstance(al, int) or
            isinstance(al, bool) or not 0 < al < 10**9 or incomplete is None or notes is None or
            best is None or not isinstance(updated, str) or not TIMESTAMP.fullmatch(updated)):
        return None
    trs = item.get("trs", [])
    if not isinstance(trs, list):
        return None
    links = []
    for t in trs[:MAX_TRS]:
        if isinstance(t, str) and RECORD_ID.fullmatch(t) and t not in links:
            links.append(t)
    return {"id": rid, "anilist_id": al, "incomplete": int(incomplete), "notes": notes,
            "theoreticalBest": best, "comparison": _urls(item.get("comparison", "")),
            "trs": links, "updated": updated}


def parse_torrent(item) -> dict | None:
    """One torrents record -> row dict, or None when malformed."""
    if not isinstance(item, dict):
        return None
    rid, updated = item.get("id"), item.get("updated")
    best, dual = _flag(item.get("isBest")), _flag(item.get("dualAudio"))
    tracker = clean_text(item.get("tracker"), 64)
    group = clean_text(item.get("releaseGroup", ""), 100)
    if (not isinstance(rid, str) or not RECORD_ID.fullmatch(rid) or best is None or dual is None or
            not tracker or group is None or not isinstance(updated, str) or
            not TIMESTAMP.fullmatch(updated)):
        return None
    tags = []
    raw = item.get("tags", [])
    for tag in (raw if isinstance(raw, list) else []):
        tag = clean_text(tag, MAX_TAG)
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) == MAX_TAGS:
            break
    digest, url = item.get("infoHash"), item.get("url")
    view = NYAA_VIEW.fullmatch(url) if isinstance(url, str) else None
    raw_tracker = item.get("tracker")       # the exact value: sanitizing must never create a match
    public = (raw_tracker.lower() == "nyaa" and isinstance(digest, str) and
              bool(INFOHASH.fullmatch(digest)) and view is not None)
    return {"id": rid, "infohash": digest.lower() if public else None, "release_group": group or None,
            "tracker": tracker, "public": int(public), "nyaa_id": int(view.group(1)) if public else None,
            "is_best": int(best), "dual_audio": int(dual), "tags": tags, "updated": updated}


def is_extra(name: str) -> bool:
    parts = name.split("/")
    if any(EXTRA_DIR.fullmatch(p) for p in parts[:-1]):
        return True
    stem = parts[-1].rsplit(".", 1)[0]
    return bool(EXTRA_NAME.search(stem))


def parse_files(value) -> dict | None:
    """A torrent's ``files`` list -> bounded stored list plus size statistics, or None."""
    if not isinstance(value, list):
        return None
    if len(value) > MAX_FILES_SCANNED:
        return None             # statistics over a truncated list would pass as the whole torrent's
    stored, total, videos = [], 0, []
    for f in value:
        name = clean_text(f.get("name"), MAX_NAME + 1) if isinstance(f, dict) else None
        size = f.get("length") if isinstance(f, dict) else None
        if (not name or len(name) > MAX_NAME or not isinstance(size, int) or isinstance(size, bool) or
                not 0 <= size <= MAX_BYTES):
            return None         # a partial list would store understated totals as complete
        total += size
        if total > MAX_TOTAL:
            return None
        if len(stored) < MAX_FILES_STORED:
            stored.append([name, size])
        if name.lower().endswith(VIDEO_EXT) and not is_extra(name):
            videos.append(size)
    median = int(statistics.median(videos)) if videos else None
    return {"files": stored, "total_bytes": total, "video_files": len(videos), "median_video_bytes": median}


# ---- client -------------------------------------------------------------------------

class Client:
    """Paged, paced, budgeted reads. Never retries: a failed run is simply run again."""

    def __init__(self, limiter: SharedLimiter, *, max_requests=MAX_REQUESTS, transport=None, wall=time.time):
        self.limiter, self.max_requests, self.wall = limiter, max_requests, wall
        self.transport = transport or fetch
        self.requests = 0
        self.received = 0

    def exhausted(self) -> bool:
        return self.requests >= self.max_requests

    def get(self, url: str) -> dict:
        if self.exhausted():
            raise KuraError("request_budget", "Request budget exhausted", requests=self.requests)
        validate_url(url)
        try:
            self.limiter.acquire(WAIT_BUDGET)
        except KuraError as e:
            if e.code == "rate_limited":
                raise KuraError("rate_limited", "SeaDex rate limit window is still open",
                                retry_after=e.details.get("retry_after")) from e
            raise KuraError("seadex_unavailable", "Rate-limit state is unavailable; retry later") from e
        self.requests += 1
        try:
            data = self.transport(url)
        except KuraError as e:
            if e.details.get("status") == 429:
                delay = e.details.get("retry_after")
                delay = 60.0 if not isinstance(delay, (int, float)) else min(max(delay, 1.0), 3600.0)
                try:
                    self.limiter.penalize(self.wall() + delay)
                except KuraError:
                    pass
            raise
        finally:
            try:
                self.limiter.done()
            except KuraError:
                pass            # acquire already reserved the slot; never mask the outcome
        return self._parse(data)

    def _parse(self, data: bytes) -> dict:
        if not isinstance(data, bytes) or len(data) > LIMIT:
            raise KuraError("seadex_invalid", "SeaDex response is not bounded bytes")
        self.received += len(data)
        if self.received > RUN_LIMIT:
            raise KuraError("seadex_invalid", "SeaDex responses exceed the per-run size limit")
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as e:
            raise KuraError("seadex_invalid", "SeaDex response must be bounded UTF-8 JSON") from e
        items = doc.get("items") if isinstance(doc, dict) else None
        pages = doc.get("totalPages") if isinstance(doc, dict) else None
        total = doc.get("totalItems") if isinstance(doc, dict) else None
        if (not isinstance(items, list) or not isinstance(pages, int) or isinstance(pages, bool) or
                not 0 <= pages <= MAX_PAGES or not isinstance(total, int) or isinstance(total, bool) or
                not 0 <= total <= MAX_PAGES * PER_PAGE):
            raise KuraError("seadex_invalid", "SeaDex response has an unexpected shape or too many pages")
        return {"items": items, "pages": pages, "total": total}

    def pages(self, collection: str, fields, parser, skipped, filter_=None) -> dict:
        """Parsed rows (by id) of a collection or filtered subset, at most MAX_PAGES pages.

        Each page is parsed and dropped before the next one is read, so memory holds rows only."""
        rows, page, seen, total = {}, 1, 0, None
        while True:
            body = self.get(build_url(collection, page=page, per_page=PER_PAGE, fields=fields,
                                      filter_=filter_))
            if len(body["items"]) > PER_PAGE:
                raise KuraError("seadex_invalid", "SeaDex page exceeds the requested size")
            if total is not None and body["total"] != total:
                raise KuraError("seadex_unstable", "SeaDex changed while it was being read; run it again")
            total, seen = body["total"], seen + len(body["items"])
            for item in body["items"]:
                row = parser(item)
                if row is None:
                    skipped[0] += 1
                elif row["id"] in rows:     # offset paging repeated a record: something moved
                    raise KuraError("seadex_unstable", "SeaDex changed while it was being read; run it again")
                else:
                    rows[row["id"]] = row
            if page >= body["pages"]:
                if seen != total:       # offset paging skips records when earlier ones vanish mid-read
                    raise KuraError("seadex_unstable", "SeaDex changed while it was being read; run it again")
                return rows
            page += 1


# ---- state --------------------------------------------------------------------------

def check_args(files_library, max_requests):
    if (not isinstance(max_requests, int) or isinstance(max_requests, bool) or
            not 1 <= max_requests <= MAX_REQUESTS_CAP):
        raise KuraError("invalid_arguments", f"--max-requests must be 1..{MAX_REQUESTS_CAP}")
    if files_library is not None and (
            not isinstance(files_library, str) or not 1 <= len(files_library) <= MAX_LIBRARY or
            not files_library.isprintable()):
        raise KuraError("invalid_arguments", "--files-for-library must be 1..200 printable characters")


def _state(store) -> dict:
    row = store.db.execute("SELECT cursor, last_attempt, last_ok, last_full_ok, status, detail "
                           "FROM sync_state WHERE source=?", (SOURCE,)).fetchone()
    if row is None:
        return {"cursor": None, "last_attempt": None, "last_ok": None, "last_full_ok": None,
                "status": "never", "detail": {}}
    try:
        detail = json.loads(row[5])
    except ValueError:
        detail = {}
    return {"cursor": row[0], "last_attempt": row[1], "last_ok": row[2], "last_full_ok": row[3],
            "status": row[4], "detail": detail if isinstance(detail, dict) else {}}


def _cursor(raw) -> dict:
    """Stored cursor -> {"entries": ts, "torrents": ts}; anything unusable means "no cursor"."""
    try:
        doc = json.loads(raw) if raw else None
    except ValueError:
        return {}
    if not isinstance(doc, dict):
        return {}
    return {k: doc[k] for k in COLLECTIONS if isinstance(doc.get(k), str) and TIMESTAMP.fullmatch(doc[k])}


def _write(store, work):
    """Run `work(db)` as one transaction under the Kura mutation lock."""
    db = store.db
    db.commit()
    with store.lock():
        try:
            db.execute("BEGIN IMMEDIATE")
            result = work(db)
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise


def _state_now(db):
    return db.execute("SELECT cursor, last_ok FROM sync_state WHERE source=?", (SOURCE,)).fetchone() or (None, None)


def _save_state(db, now, **fields):
    row = db.execute("SELECT cursor, last_ok, last_full_ok FROM sync_state WHERE source=?",
                     (SOURCE,)).fetchone() or (None, None, None)
    values = {"cursor": row[0], "last_ok": row[1], "last_full_ok": row[2], "detail": "{}", **fields}
    db.execute(
        "INSERT INTO sync_state(source, cursor, last_attempt, last_ok, last_full_ok, status, detail) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, "
        "last_attempt=excluded.last_attempt, last_ok=excluded.last_ok, last_full_ok=excluded.last_full_ok, "
        "status=excluded.status, detail=excluded.detail",
        (SOURCE, values["cursor"], now, values["last_ok"], values["last_full_ok"],
         values["status"], values["detail"]))


def _insert_entry(db, e, now):
    db.execute("INSERT OR REPLACE INTO seadex_entries(id, anilist_id, incomplete, notes, theoretical_best, "
               "comparison, updated, synced_at) VALUES (?,?,?,?,?,?,?,?)",
               (e["id"], e["anilist_id"], e["incomplete"], e["notes"], e["theoreticalBest"],
                json.dumps(e["comparison"]), e["updated"], now))
    db.execute("DELETE FROM seadex_entry_torrents WHERE entry_id=?", (e["id"],))
    db.executemany("INSERT OR IGNORE INTO seadex_entry_torrents(entry_id, torrent_id) VALUES (?,?)",
                   [(e["id"], t) for t in e["trs"]])


_FILE_COLUMNS = "files, total_bytes, video_files, median_video_bytes, files_fetched_at"


def _insert_torrent(db, t, now, carried):
    """Insert/replace one torrent. File details survive only while `updated` is unchanged."""
    keep = carried.get(t["id"])
    keep = keep[1:] if keep and keep[0] == t["updated"] else (None,) * 5
    db.execute("INSERT OR REPLACE INTO seadex_torrents(id, infohash, release_group, tracker, public, nyaa_id, "
               "is_best, dual_audio, tags, files, total_bytes, video_files, median_video_bytes, "
               "files_fetched_at, updated, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (t["id"], t["infohash"], t["release_group"], t["tracker"], t["public"], t["nyaa_id"],
                t["is_best"], t["dual_audio"], json.dumps(t["tags"]), *keep, t["updated"], now))


def watermark(now: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - OVERLAP)) + ".000000000Z"


def _newest(rows, previous, now):
    """Newest `updated` seen, held back to the run's start minus an overlap.

    Pages are read by id while records change, and an untrusted record may claim any date;
    the hold-back means a record that changed mid-run, or one dated in the future, can
    never push the cursor past what the next incremental run will still re-read."""
    stamps = [canonical_stamp(r["updated"]) for r in rows.values()]
    newest = max(stamps + ([canonical_stamp(previous)] if previous else []), default=None)
    return min(newest, watermark(now)) if newest else None


def _count(db, sql):
    return db.execute(sql).fetchone()[0]


def _listing(store, client, full: bool, now: float, skipped) -> dict:
    """Fetch entries and torrents (full or incremental), then write them in one transaction."""
    state = _state(store)
    cursor = _cursor(state["cursor"])
    if not full:
        if len(cursor) != len(COLLECTIONS):
            full = True
    since = {} if full else cursor
    entries = client.pages("entries", ENTRY_FIELDS, parse_entry, skipped,
                           filter_updated(since["entries"]) if since else None)
    torrents = client.pages("torrents", TORRENT_FIELDS, parse_torrent, skipped,
                            filter_updated(since["torrents"]) if since else None)
    # A full pull derives the cursor from what it just read, never from the old one.
    new_cursor = {"entries": _newest(entries, None if full else cursor.get("entries"), now),
                  "torrents": _newest(torrents, None if full else cursor.get("torrents"), now)}
    if None in new_cursor.values():
        new_cursor = {}         # nothing seen yet: stay without a cursor, so the next run is full

    def write(db):
        if _state_now(db) != (state["cursor"], state["last_ok"]):
            raise KuraError("sync_conflict", "Another SeaDex sync finished meanwhile; run it again")
        carried = {r[0]: r[1:] for r in db.execute(f"SELECT id, updated, {_FILE_COLUMNS} FROM seadex_torrents")}
        if full:
            for table in ("seadex_entry_torrents", "seadex_entries", "seadex_torrents"):
                db.execute(f"DELETE FROM {table}")
        for t in torrents.values():
            _insert_torrent(db, t, now, carried)
        for e in entries.values():
            _insert_entry(db, e, now)
        _save_state(db, now, cursor=json.dumps(new_cursor) if new_cursor else None, last_ok=now,
                    last_full_ok=now if full else state["last_full_ok"], status="ok",
                    detail=json.dumps({"mode": "full" if full else "incremental"}))
        return {"entries": {"upserted": len(entries), "total": _count(db, "SELECT COUNT(*) FROM seadex_entries")},
                "torrents": {"upserted": len(torrents),
                             "total": _count(db, "SELECT COUNT(*) FROM seadex_torrents"),
                             "public": _count(db, "SELECT COUNT(*) FROM seadex_torrents WHERE public=1")}}

    result = _write(store, write)
    result["mode"], result["cursor"] = ("full" if full else "incremental"), new_cursor or None
    return result


def library_ids(store, library: str) -> list[int]:
    """AniList IDs for a library: its resolved identities first, then the viewer's list snapshot.

    Both tables may be empty (identity pass or list sync not run yet); that yields no ids.
    """
    db = store.db
    ids = [r[0] for r in db.execute(
        "SELECT DISTINCT anilist_id FROM title_identities WHERE library=? AND anilist_id IS NOT NULL "
        "AND status IN ('resolved','pinned') ORDER BY anilist_id", (library,))]
    ids += [r[0] for r in db.execute(
        "SELECT DISTINCT e.anilist_id FROM anilist_list_entries e JOIN anilist_users u "
        "ON u.user_id=e.user_id WHERE u.is_viewer=1 ORDER BY e.anilist_id")]
    return list(dict.fromkeys(ids))


def _pending_torrents(store, ids) -> dict[str, str]:
    """Public torrents (id -> `updated` seen) of the entries for `ids` without stored file lists."""
    out = {}
    for i in ids:
        for tid, updated in store.db.execute(
                "SELECT DISTINCT t.id, t.updated FROM seadex_entries e JOIN seadex_entry_torrents x ON x.entry_id=e.id "
                "JOIN seadex_torrents t ON t.id=x.torrent_id WHERE e.anilist_id=? AND t.public=1 "
                "AND t.files_fetched_at IS NULL ORDER BY t.id", (i,)):
            out.setdefault(tid, updated)
    return out


def _files(store, client, library: str, now: float, skipped) -> tuple[int, int]:
    """Fetch file lists in batches until done or the request budget ends. -> (fetched, pending)."""
    versions = _pending_torrents(store, library_ids(store, library))
    pending = list(versions)
    fetched = 0
    for start in range(0, len(pending), FILES_BATCH):
        if client.exhausted():
            break
        batch = pending[start:start + FILES_BATCH]
        body = client.get(build_url("torrents", page=1, per_page=FILES_BATCH, fields=FILES_FIELDS,
                                    filter_=filter_ids(batch)))
        rows = []
        for item in body["items"][:FILES_BATCH]:
            parsed = parse_files(item.get("files")) if isinstance(item, dict) else None
            rid = item.get("id") if isinstance(item, dict) else None
            if parsed is None or rid not in batch:
                skipped[0] += 1
                continue
            rows.append((json.dumps(parsed["files"]), parsed["total_bytes"], parsed["video_files"],
                         parsed["median_video_bytes"], now, rid, versions[rid]))

        def write(db):
            # Only the record version that was selected: a concurrent sync may have replaced it.
            return db.executemany(
                "UPDATE seadex_torrents SET files=?, total_bytes=?, video_files=?, median_video_bytes=?, "
                "files_fetched_at=? WHERE id=? AND updated=? AND public=1", rows).rowcount
        fetched += _write(store, write)
    return fetched, len(_pending_torrents(store, library_ids(store, library)))


def sync(store, *, full=False, files_library=None, max_requests=MAX_REQUESTS, limiter=None,
         transport=None, wall=time.time) -> dict:
    """Mirror SeaDex into state. Needs network (unless every step is skipped); writes only state."""
    check_args(files_library, max_requests)
    limiter = limiter or SharedLimiter(store.root, SOURCE, interval=INTERVAL, wall=wall)
    client = Client(limiter, max_requests=max_requests, transport=transport, wall=wall)
    now = wall()
    state = _state(store)
    due = state["last_full_ok"] is None or now - state["last_full_ok"] > FULL_EVERY
    skipped = [0]
    try:
        result = _listing(store, client, full or due, now, skipped)
    except KuraError as e:
        if e.code == "sync_conflict":
            raise
        def failed(db):
            if _state_now(db) != (state["cursor"], state["last_ok"]):
                return              # another run finished meanwhile; its result stands
            _save_state(db, now, status="failed", detail=json.dumps({"error": e.code}))
        try:
            _write(store, failed)
        except (KuraError, OSError):
            pass
        raise KuraError(e.code, e.message, **{**e.details, "requests": client.requests,
                                              "kept_previous_data": True}) from e
    fetched, pending = 0, 0
    if files_library is not None:
        try:
            fetched, pending = _files(store, client, files_library, now, skipped)
        except KuraError as e:      # the listing above is already committed and stays "ok"
            raise KuraError(e.code, e.message, **{**e.details, "requests": client.requests,
                                                  "listing_committed": True}) from e
    out = {"mode": result["mode"], "entries": result["entries"], "torrents": result["torrents"],
           "files_fetched": fetched, "skipped_malformed": skipped[0], "requests": client.requests,
           "cursor": result["cursor"], "synced_at": now}
    if files_library is not None:
        out["files_pending"] = pending
    return out


# ---- read side (state only, no network) ---------------------------------------------

def _json_list(raw) -> list:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _stale(synced_at, now) -> bool:
    return synced_at is None or now - synced_at > FULL_EVERY


def _torrent_json(row) -> dict:
    (_tid, group, public, best, dual, tags, digest, nyaa_id, total, videos, median) = row
    out = {"release_group": group, "public": bool(public), "is_best": bool(best),
           "dual_audio": bool(dual), "tags": _json_list(tags)}
    if public:
        out["infohash"] = digest
    out.update({"nyaa_id": nyaa_id, "total_bytes": total, "video_files": videos,
                "median_video_bytes": median})
    return out


def _lookup_anilist(db, al: int) -> dict:
    entries = db.execute("SELECT id, incomplete, notes, comparison, updated FROM seadex_entries "
                         "WHERE anilist_id=? ORDER BY updated DESC, id", (al,)).fetchall()
    torrents = db.execute(
        "SELECT DISTINCT t.id, t.release_group, t.public, t.is_best, t.dual_audio, t.tags, t.infohash, "
        "t.nyaa_id, t.total_bytes, t.video_files, t.median_video_bytes FROM seadex_entries e "
        "JOIN seadex_entry_torrents x ON x.entry_id=e.id JOIN seadex_torrents t ON t.id=x.torrent_id "
        "WHERE e.anilist_id=? ORDER BY t.is_best DESC, t.release_group, t.id", (al,)).fetchall()
    entry = None
    if entries:
        eid, incomplete, notes, comparison, updated = entries[0]
        entry = {"id": eid, "incomplete": bool(incomplete), "notes": notes,
                 "comparison": _json_list(comparison), "updated": updated}
    return {"anilist_id": al, "entry": entry, "torrents": [_torrent_json(r) for r in torrents]}


def lookup(store, *, anilist_id_=None, infohash_=None, now=time.time) -> dict:
    """Stored picks for an AniList ID, or the entries listing a public infohash."""
    if (anilist_id_ is None) == (infohash_ is None):
        raise KuraError("invalid_arguments", "Give exactly one of --anilist-id and --infohash")
    synced = _state(store)["last_ok"]
    stale = _stale(synced, now())
    if anilist_id_ is not None:
        return {**_lookup_anilist(store.db, anilist_id(anilist_id_)), "synced_at": synced, "stale": stale}
    digest = infohash(infohash_)
    ids = [r[0] for r in store.db.execute(
        "SELECT DISTINCT e.anilist_id FROM seadex_torrents t JOIN seadex_entry_torrents x ON x.torrent_id=t.id "
        "JOIN seadex_entries e ON e.id=x.entry_id WHERE t.public=1 AND t.infohash=? ORDER BY e.anilist_id",
        (digest,))]
    return {"infohash": digest, "matches": [_lookup_anilist(store.db, i) for i in ids],
            "synced_at": synced, "stale": stale}


def status(store, now=time.time) -> dict:
    state, db = _state(store), store.db
    return {"source": SOURCE, "status": state["status"], "last_attempt": state["last_attempt"],
            "last_ok": state["last_ok"], "last_full_ok": state["last_full_ok"],
            "cursor": _cursor(state["cursor"]) or None, "detail": state["detail"],
            "entries": _count(db, "SELECT COUNT(*) FROM seadex_entries"),
            "torrents": _count(db, "SELECT COUNT(*) FROM seadex_torrents"),
            "public_torrents": _count(db, "SELECT COUNT(*) FROM seadex_torrents WHERE public=1"),
            "torrents_with_files": _count(db, "SELECT COUNT(*) FROM seadex_torrents "
                                              "WHERE files_fetched_at IS NOT NULL"),
            "stale": _stale(state["last_ok"], now()),
            "full_sync_due": state["last_full_ok"] is None or now() - state["last_full_ok"] > FULL_EVERY}
