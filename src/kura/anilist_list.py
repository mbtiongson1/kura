"""Read-only AniList list, favourites and media sync into Kura's private state.

Every request goes through :meth:`kura.anilist.AniList.query`, so it shares the cross-process
rate budget and the pinned, redirect-free transport. Nothing here writes to AniList. A user's
list and favourites are replaced in one transaction, and only after every chunk or page arrived;
a failure keeps the previous snapshot. The access token is held in a :class:`Requester` and
handed to the transport only for the authenticated list request; it is never returned or logged.
"""
import json
import re
import sqlite3

from . import anilist_auth
from .anilist import GQL_LIMIT, clean
from .manifest_store import PRESENT_IDENTITY_SQL
from .models import KuraError

CHUNK_SIZE = 500
MAX_CHUNKS = 20
FAVOURITES_PER_PAGE = 25            # the documented maximum for Favourites.anime
MAX_FAVOURITE_PAGES = 20
MAX_ENTRIES = 50_000
MEDIA_BATCH = 50
MIN_MEDIA_BATCH = 5
MAX_IDS = 5000
MAX_REQUESTS = 100
STATUSES = ("CURRENT", "PLANNING", "COMPLETED", "DROPPED", "PAUSED", "REPEATING")
# Failures that leave the previous snapshot in place and report it as stale. Anything else
# (a bad token, an unknown or private user, a bad argument) raises: stale data would hide it.
TRANSIENT = ("anilist_rate_limited", "anilist_unavailable", "anilist_budget_exhausted",
             "anilist_invalid_response", "anilist_list_too_large", "anilist_response_too_large",
             "anilist_query_error")
USER_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")

MEDIA = ("id title{romaji english native} synonyms format episodes duration status season seasonYear "
         "averageScore popularity favourites isAdult nextAiringEpisode{episode airingAt}")
VIEWER_QUERY = "{Viewer{id name mediaListOptions{scoreFormat}}}"
LIST_QUERY = ("query($userId:Int,$userName:String,$chunk:Int,$perChunk:Int){"
              "MediaListCollection(userId:$userId,userName:$userName,type:ANIME,chunk:$chunk,perChunk:$perChunk){"
              "hasNextChunk user{id name} lists{isCustomList entries{mediaId status "
              "score(format:POINT_10_DECIMAL) progress repeat updatedAt media{%s}}}}}" % MEDIA)
FAVOURITES_QUERY = ("query($id:Int,$page:Int){User(id:$id){favourites{anime(page:$page,perPage:%d){"
                    "pageInfo{hasNextPage} nodes{id}}}}}" % FAVOURITES_PER_PAGE)
MEDIA_QUERY = "query($ids:[Int],$n:Int){Page(perPage:$n){media(id_in:$ids,type:ANIME){%s}}}" % MEDIA
LIMITS_QUERY = "query{GenreCollection}"

AUTH_MESSAGE = re.compile(r"token|auth|unauthori|expired", re.I)
ENUM = re.compile(r"^[A-Z_]{1,32}$")


def valid_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**9


def _int(value, high=10**9):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < high else None


def _enum(value):
    return value if isinstance(value, str) and ENUM.fullmatch(value) else None


# -- request budget ---------------------------------------------------------------------

class Requester:
    """Counts requests against a per-command budget and turns responses into data or KuraError."""

    def __init__(self, ani, *, max_requests: int, token: str | None = None):
        self.ani, self.budget, self.made = ani, max_requests, 0
        self._token = token
        self.last_headers: dict = {}

    def info(self) -> dict:
        return {"made": self.made, "budget": self.budget}

    def call(self, query: str, variables: dict, *, auth: bool = False, limit: int = GQL_LIMIT) -> dict:
        if self.made >= self.budget:
            raise KuraError("anilist_budget_exhausted", "The request budget for this command is used up",
                            made=self.made, budget=self.budget)
        if auth and self._token is None:
            raise KuraError("anilist_not_logged_in", "Not logged in: run kura anilist login")
        self.made += 1
        try:
            body, self.last_headers = self.ani.query(query, variables, limit=limit,
                                                     token=self._token if auth else None)
        except KuraError as e:
            if e.code != "anilist_http":
                raise
            raise self._failure(e.details.get("status"), e.details.get("reason"), auth) from e
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as e:
            raise KuraError("anilist_invalid_response", "AniList returned a malformed response") from e
        if not isinstance(document, dict):
            raise KuraError("anilist_invalid_response", "AniList returned a malformed response")
        errors = document.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
            if first.get("status") == 429:      # a rate limit reported in a 200 body binds every process too
                raise KuraError("anilist_rate_limited", "AniList rate limit reached; retry later",
                                retry_after=self.ani.penalize(self.last_headers))
            raise self._failure(first.get("status"), clean(first.get("message")), auth)
        data = document.get("data")
        if not isinstance(data, dict):
            raise KuraError("anilist_invalid_response", "AniList response has no data")
        return data

    @staticmethod
    def _failure(status, message, auth) -> KuraError:
        message = message if isinstance(message, str) else ""
        if auth and (status == 401 or (status == 400 and AUTH_MESSAGE.search(message))):
            return KuraError("anilist_auth_invalid", "AniList rejected the token; log in again", status=status)
        if status == 429:
            return KuraError("anilist_rate_limited", "AniList rate limit reached; retry later", retry_after=60)
        if "private" in message.lower():
            return KuraError("anilist_list_private", "That AniList user's list is private", status=status)
        if status == 404:
            return KuraError("anilist_not_found", "AniList does not know that user", status=status)
        if status == 400:
            return KuraError("anilist_bad_request", "AniList rejected the query", status=status)
        return KuraError("anilist_query_error", "AniList reported an error",
                         **({"status": status} if isinstance(status, int) else {}))


# -- parsing (all of it untrusted) ----------------------------------------------------------

def parse_media(item):
    """One Media object -> row dict, or None when it has no valid id."""
    if not isinstance(item, dict) or not valid_id(item.get("id")):
        return None
    title = item.get("title") if isinstance(item.get("title"), dict) else {}
    synonyms = [s for s in (clean(v) for v in (item.get("synonyms") if isinstance(item.get("synonyms"), list) else [])[:20]) if s]
    airing = item.get("nextAiringEpisode") if isinstance(item.get("nextAiringEpisode"), dict) else {}
    adult = item.get("isAdult")
    return {"anilist_id": item["id"], "title_romaji": clean(title.get("romaji")),
            "title_english": clean(title.get("english")), "title_native": clean(title.get("native")),
            "synonyms": json.dumps(synonyms, ensure_ascii=True), "format": _enum(item.get("format")),
            "episodes": _int(item.get("episodes")), "duration": _int(item.get("duration")),
            "status": _enum(item.get("status")), "season": _enum(item.get("season")),
            "season_year": _int(item.get("seasonYear"), 10_000), "average_score": _int(item.get("averageScore"), 101),
            "popularity": _int(item.get("popularity")), "favourites": _int(item.get("favourites")),
            "is_adult": int(adult) if isinstance(adult, bool) else None,
            "next_airing_episode": _int(airing.get("episode")),
            "next_airing_at": _int(airing.get("airingAt"), 10**11)}


def parse_entry(item):
    """One list entry -> (row, media row | None), or None when unusable."""
    if not isinstance(item, dict) or not valid_id(item.get("mediaId")) or item.get("status") not in STATUSES:
        return None
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 < score <= 10:
        score = None                    # 0 means unscored
    row = {"anilist_id": item["mediaId"], "status": item["status"], "score": float(score) if score else None,
           "progress": _int(item.get("progress"), 10**6), "repeat": _int(item.get("repeat"), 10**6),
           "updated_at": _int(item.get("updatedAt"), 10**11)}
    media = parse_media(item.get("media"))
    return row, media if media and media["anilist_id"] == row["anilist_id"] else None


def viewer(req: Requester) -> dict:
    """The authenticated user: {id, name, score_format}."""
    data = req.call(VIEWER_QUERY, {}, auth=True)
    me = data.get("Viewer")
    name = clean(me.get("name")) if isinstance(me, dict) else None
    if not isinstance(me, dict) or not valid_id(me.get("id")) or not name:
        raise KuraError("anilist_invalid_response", "AniList returned no viewer")
    options = me.get("mediaListOptions") if isinstance(me.get("mediaListOptions"), dict) else {}
    return {"id": me["id"], "name": name, "score_format": _enum(options.get("scoreFormat"))}


def fetch_list(req: Requester, *, user_id: int | None, user_name: str | None, auth: bool):
    """Every chunk of one user's anime list -> (user, entries, media rows). All or nothing."""
    entries: dict[int, dict] = {}
    media: dict[int, dict] = {}
    user = None
    for chunk in range(1, MAX_CHUNKS + 1):
        variables = {"chunk": chunk, "perChunk": CHUNK_SIZE}
        # The first chunk may be looked up by name; later ones pin the owner it returned.
        variables.update({"userId": user_id} if user_id is not None else {"userName": user_name})
        collection = req.call(LIST_QUERY, variables, auth=auth).get("MediaListCollection")
        if not isinstance(collection, dict):
            raise KuraError("anilist_invalid_response", "AniList returned no list")
        owner = collection.get("user") if isinstance(collection.get("user"), dict) else {}
        owner_name = clean(owner.get("name"))
        if not valid_id(owner.get("id")) or not owner_name or (user_id is not None and owner["id"] != user_id):
            raise KuraError("anilist_invalid_response", "AniList returned a list for an unexpected user")
        if user is not None and owner["id"] != user["id"]:
            raise KuraError("anilist_invalid_response", "AniList returned lists for different users")
        user = {"id": owner["id"], "name": owner_name}
        user_id = owner["id"]
        lists, more = collection.get("lists"), collection.get("hasNextChunk")
        if not isinstance(lists, list) or not isinstance(more, bool):    # never replace a snapshot with a partial one
            raise KuraError("anilist_invalid_response", "AniList returned an incomplete list")
        # Status lists first. An entry the owner hid from them is only in a custom list, and the same
        # entry (same mediaId, same status) can appear in several lists, so keep the first of each.
        if not all(isinstance(g, dict) and isinstance(g.get("entries"), list) and
                   all(isinstance(i, dict) for i in g["entries"]) for g in lists):
            raise KuraError("anilist_invalid_response", "AniList returned an incomplete list")
        for group in sorted(lists, key=lambda g: g.get("isCustomList") is True):
            for item in group["entries"]:
                parsed = parse_entry(item)
                if parsed is None:          # never replace a snapshot with a response we could not fully read
                    raise KuraError("anilist_invalid_response", "AniList returned an unreadable list entry")
                if parsed[0]["anilist_id"] in entries:
                    continue
                entries[parsed[0]["anilist_id"]] = parsed[0]
                if parsed[1]:
                    media[parsed[1]["anilist_id"]] = parsed[1]
        if len(entries) > MAX_ENTRIES:
            raise KuraError("anilist_list_too_large", "The list is larger than Kura will store")
        if not more:
            return user, list(entries.values()), list(media.values())
    raise KuraError("anilist_list_too_large", "The list has more chunks than Kura will fetch")


def fetch_favourites(req: Requester, user_id: int) -> list[int]:
    ids: list[int] = []
    for page in range(1, MAX_FAVOURITE_PAGES + 1):
        data = req.call(FAVOURITES_QUERY, {"id": user_id, "page": page})
        owner = data.get("User")
        favourites = owner.get("favourites") if isinstance(owner, dict) else None
        anime = favourites.get("anime") if isinstance(favourites, dict) else None
        if not isinstance(anime, dict):
            raise KuraError("anilist_invalid_response", "AniList returned no favourites")
        nodes, info = anime.get("nodes"), anime.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
            raise KuraError("anilist_invalid_response", "AniList returned incomplete favourites")
        if not all(isinstance(n, dict) and valid_id(n.get("id")) for n in nodes):
            raise KuraError("anilist_invalid_response", "AniList returned incomplete favourites")
        ids.extend(n["id"] for n in nodes if n["id"] not in ids)
        if not info["hasNextPage"]:
            return ids
    raise KuraError("anilist_list_too_large", "More favourites pages than Kura will fetch")


# -- storage --------------------------------------------------------------------------------

MEDIA_COLUMNS = ("anilist_id", "title_romaji", "title_english", "title_native", "synonyms", "format", "episodes",
                 "duration", "status", "season", "season_year", "average_score", "popularity", "favourites",
                 "is_adult", "next_airing_episode", "next_airing_at")


def upsert_media(db, rows, now: float):
    names = ",".join(MEDIA_COLUMNS)
    update = ",".join(f"{c}=excluded.{c}" for c in MEDIA_COLUMNS[1:])
    db.executemany(
        f"INSERT INTO anilist_media({names},fetched_at) VALUES({','.join('?' * len(MEDIA_COLUMNS))},?) "
        f"ON CONFLICT(anilist_id) DO UPDATE SET {update},fetched_at=excluded.fetched_at",
        [tuple(r[c] for c in MEDIA_COLUMNS) + (now,) for r in rows])


def _sync_state(db, source: str, now: float, status: str, detail: dict, ok: bool):
    db.execute(
        "INSERT INTO sync_state(source,last_attempt,last_ok,last_full_ok,status,detail) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt, status=excluded.status, "
        "detail=excluded.detail, last_ok=COALESCE(excluded.last_ok,last_ok), "
        "last_full_ok=COALESCE(excluded.last_full_ok,last_full_ok)",
        (source, now, now if ok else None, now if ok else None, status, json.dumps(detail)))


def save_snapshot(db, *, user: dict, is_viewer: bool, score_format, entries, media, now: float):
    """Replace one user's list in a single transaction."""
    existing = db.execute("SELECT is_viewer FROM anilist_users WHERE user_id=?", (user["id"],)).fetchone()
    if existing and existing[0] and not is_viewer:
        raise KuraError("invalid_arguments", "That is the logged-in account; omit --user to sync it")
    with db:
        db.execute(
            "INSERT INTO anilist_users(user_id,name,is_viewer,score_format,list_fetched_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, "
            "is_viewer=MAX(is_viewer,excluded.is_viewer), score_format=COALESCE(excluded.score_format,score_format), "
            "list_fetched_at=excluded.list_fetched_at, favourites_fetched_at=NULL, "    # restored by save_favourites
            "updated_at=excluded.updated_at",
            (user["id"], user["name"], int(is_viewer), score_format, now, now))
        db.execute("DELETE FROM anilist_list_entries WHERE user_id=?", (user["id"],))
        db.executemany(
            "INSERT INTO anilist_list_entries(user_id,anilist_id,status,score,progress,repeat,updated_at,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(user["id"], e["anilist_id"], e["status"], e["score"], e["progress"], e["repeat"],
              e["updated_at"], now) for e in entries])
        upsert_media(db, media, now)
        _sync_state(db, f"anilist:list:{user['id']}", now, "ok",
                    {"entries": len(entries), "authenticated": is_viewer}, True)


def save_favourites(db, user_id: int, ids: list[int], now: float):
    with db:
        db.execute("DELETE FROM anilist_favourites WHERE user_id=?", (user_id,))
        db.executemany("INSERT INTO anilist_favourites(user_id,anilist_id,position,fetched_at) VALUES(?,?,?,?)",
                       [(user_id, i, n, now) for n, i in enumerate(ids)])
        db.execute("UPDATE anilist_users SET favourites_fetched_at=? WHERE user_id=?", (now, user_id))
        _sync_state(db, f"anilist:favourites:{user_id}", now, "ok", {"favourites": len(ids)}, True)


def record_failure(db, user_id, code: str, now: float):
    if user_id is None:
        return
    detail = {"error": code}
    provenance = snapshot_authenticated(db, user_id)    # describes the stored rows, not this attempt
    if provenance is not None:
        detail["authenticated"] = provenance
    with db:
        _sync_state(db, f"anilist:list:{user_id}", now, "error", detail, False)


def snapshot_authenticated(db, user_id) -> bool | None:
    """Whether the stored list was fetched with the token (it may then hold private entries)."""
    row = db.execute("SELECT detail FROM sync_state WHERE source=?", (f"anilist:list:{user_id}",)).fetchone()
    try:
        value = json.loads(row[0]).get("authenticated") if row else None
    except (ValueError, AttributeError, TypeError):
        return None
    return value if isinstance(value, bool) else None


def clear_viewer(db):
    with db:
        db.execute("UPDATE anilist_users SET is_viewer=0")


def save_viewer(db, viewer_row: dict, now: float):
    """Make `viewer_row` the one logged-in account."""
    with db:
        db.execute("UPDATE anilist_users SET is_viewer=0 WHERE user_id!=?", (viewer_row["id"],))
        db.execute(
            "INSERT INTO anilist_users(user_id,name,is_viewer,score_format,updated_at) VALUES(?,?,1,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, is_viewer=1, "
            "score_format=COALESCE(excluded.score_format,score_format), updated_at=excluded.updated_at",
            (viewer_row["id"], viewer_row["name"], viewer_row["score_format"], now))


def read_snapshot(db, user_id: int, *, with_entries: bool) -> dict:
    user = db.execute("SELECT user_id,name,is_viewer,list_fetched_at FROM anilist_users WHERE user_id=?",
                      (user_id,)).fetchone()
    counts = {s: 0 for s in STATUSES}
    for status, n in db.execute("SELECT status,COUNT(*) FROM anilist_list_entries WHERE user_id=? GROUP BY status",
                                (user_id,)):
        counts[status] = n
    nine = db.execute("SELECT COUNT(*) FROM anilist_list_entries WHERE user_id=? AND score>=9", (user_id,)).fetchone()[0]
    favourites = db.execute("SELECT COUNT(*) FROM anilist_favourites WHERE user_id=?", (user_id,)).fetchone()[0]
    data = {"user": {"id": user[0], "name": user[1], "viewer": bool(user[2])}, "fetched_at": user[3],
            "counts": counts, "entries_total": sum(counts.values()), "favourites": favourites,
            "scored_9_plus": nine}
    if with_entries:
        rows = db.execute(
            "SELECT e.anilist_id,e.status,e.score,e.progress,e.repeat,e.updated_at,m.title_romaji,m.title_english,"
            "m.format,m.episodes FROM anilist_list_entries e LEFT JOIN anilist_media m USING(anilist_id) "
            "WHERE e.user_id=? ORDER BY e.anilist_id", (user_id,)).fetchall()
        data["entries"] = [dict(zip(("anilist_id", "status", "score", "progress", "repeat", "updated_at",
                                     "title_romaji", "title_english", "format", "episodes"), r)) for r in rows]
    return data


def _limiter_state(ani):
    try:
        return ani.limiter.state() if ani.limiter is not None else None
    except KuraError:
        return None


# -- commands -------------------------------------------------------------------------------

def check_list_args(user, max_age_h, max_requests):
    if user is not None and not USER_NAME.fullmatch(user):
        raise KuraError("invalid_arguments", "--user must be an AniList user name")
    if not 0 <= max_age_h < 24 * 365:
        raise KuraError("invalid_arguments", "--max-age-h must be between 0 and 8760")
    check_budget(max_requests)


def check_budget(max_requests):
    if not 1 <= max_requests <= MAX_REQUESTS:
        raise KuraError("invalid_arguments", "--max-requests must be between 1 and %d" % MAX_REQUESTS)


def list_sync(store, ani, *, user_name=None, max_age_h=12.0, refresh=False, entries=False, max_requests=10) -> dict:
    """Serve a fresh cached snapshot, or fetch one. Returns the `anilist list` envelope data."""
    check_list_args(user_name, max_age_h, max_requests)
    db, now = store.db, ani.wall()
    warnings: list[str] = []
    token = None
    if user_name is None:
        record = anilist_auth.load(store.root)
        days, warnings = anilist_auth.expiry_state(record["expires_at"], now)
        if days <= 0:
            raise KuraError("anilist_token_expired", "The AniList token has expired; run kura anilist login")
        token, user_id = record["token"], record["viewer"]["id"]
        row = db.execute("SELECT list_fetched_at,favourites_fetched_at FROM anilist_users WHERE user_id=?",
                         (user_id,)).fetchone()
    else:
        row = db.execute("SELECT list_fetched_at,favourites_fetched_at,user_id,is_viewer FROM anilist_users "
                         "WHERE name=? COLLATE NOCASE ORDER BY list_fetched_at DESC", (user_name,)).fetchone()
        if row and row[3]:      # that snapshot came from the authenticated account and may hold private entries
            raise KuraError("invalid_arguments", "That is the logged-in account; omit --user to sync it")
        user_id = row[2] if row else None
    fetched, fav_fetched = (row[0], row[1]) if row else (None, None)
    req = Requester(ani, max_requests=max_requests, token=token)

    def fresh(stamp):
        return stamp is not None and not refresh and now - stamp < max_age_h * 3600

    # A snapshot is only served to the kind of request that produced it: an authenticated one may
    # hold private entries and must never answer a public --user lookup, and vice versa.
    matches = user_id is not None and snapshot_authenticated(db, user_id) is (token is not None)

    error, fetched_now = None, False
    if not (fresh(fetched) and matches):
        try:
            # Public refreshes resolve the requested name afresh: a cached ID may belong to a renamed account.
            user, rows, media = fetch_list(req, user_id=user_id if token is not None else None,
                                           user_name=user_name, auth=token is not None)
            user_id = user["id"]
            save_snapshot(db, user=user, is_viewer=token is not None, score_format=None,
                          entries=rows, media=media, now=now)
            fav_fetched, fetched_now = None, True
        except KuraError as e:
            e.details.setdefault("requests", req.info())
            if e.code not in TRANSIENT or fetched is None or not matches:
                raise
            record_failure(db, user_id, e.code, now)
            error = e.code
    if error is None and not fresh(fav_fetched):
        try:        # favourites have their own freshness: a failed fetch is retried by the next call
            save_favourites(db, user_id, fetch_favourites(req, user_id), now)
        except KuraError:
            warnings.append("anilist_favourites_failed")
            with db:        # the old rows stay, but they no longer count as fresh: the next call retries
                db.execute("UPDATE anilist_users SET favourites_fetched_at=NULL WHERE user_id=?", (user_id,))
    data = read_snapshot(db, user_id, with_entries=entries)
    return {**data, "from_cache": not fetched_now, "stale": error is not None,
            "error": error, "requests": req.info(), "warnings": warnings, "rate_limit": _limiter_state(ani)}


def media_ids(db, ids, library, missing) -> list[int]:
    if library is not None:
        found = [r[0] for r in db.execute(
            "SELECT DISTINCT anilist_id FROM title_identities WHERE library=? AND anilist_id IS NOT NULL "
            "AND " + PRESENT_IDENTITY_SQL + " ORDER BY anilist_id", (library, library, library))]
    else:
        found = sorted(set(ids))
    if len(found) > MAX_IDS:
        raise KuraError("invalid_arguments", "Too many AniList IDs (limit %d)" % MAX_IDS)
    if missing:
        have = {r[0] for r in db.execute("SELECT anilist_id FROM anilist_media")}
        found = [i for i in found if i not in have]
    return found


def media_sync(store, ani, *, ids=None, library=None, missing=False, max_requests=10) -> dict:
    """Fetch public media metadata in batches of 50, halving a batch AniList rejects (minimum 5)."""
    check_budget(max_requests)
    if (ids is None) == (library is None):
        raise KuraError("invalid_arguments", "Give --id N (repeatable) or --library NAME")
    if ids is not None and not all(valid_id(i) for i in ids):
        raise KuraError("invalid_arguments", "--id must be a positive AniList ID")
    if library is not None and (not library or len(library) > 200 or any(ord(c) < 32 for c in library)):
        raise KuraError("invalid_arguments", "Invalid --library name")
    db, now = store.db, ani.wall()
    wanted = media_ids(db, ids, library, missing)
    req = Requester(ani, max_requests=max_requests)
    fetched, absent, error, size, pending = 0, [], None, MEDIA_BATCH, list(wanted)
    while pending:
        batch = pending[:size]
        try:
            data = req.call(MEDIA_QUERY, {"ids": batch, "n": len(batch)})
        except KuraError as e:
            if e.code == "anilist_bad_request" and size > MIN_MEDIA_BATCH:
                size = max(MIN_MEDIA_BATCH, size // 2)
                continue
            if fetched == 0 and not absent:
                e.details["requests"] = req.info()
                raise
            error = e.code
            break
        page = data.get("Page")
        items = page.get("media") if isinstance(page, dict) else None
        if not isinstance(items, list):
            error = "anilist_invalid_response"
            break
        rows = [parse_media(i) for i in items]
        if any(m is None or m["anilist_id"] not in batch for m in rows):
            error = "anilist_invalid_response"      # unreadable or unexpected items are not proof of absence
            break
        with db:
            upsert_media(db, rows, now)
        got = {r["anilist_id"] for r in rows}
        fetched += len(got)
        absent += [i for i in batch if i not in got]
        pending = pending[len(batch):]
    if error is not None and fetched == 0 and not absent:
        raise KuraError(error, "AniList returned unreadable media data", requests=req.info())
    return {"requested": len(wanted), "fetched": fetched, "not_found": absent[:100],
            "remaining": len(pending), "complete": error is None, "error": error,
            "requests": req.info(), "rate_limit": _limiter_state(ani), "warnings": []}


def limits(ani) -> dict:
    """Exactly one cheap public request; reports what AniList says about the budget."""
    req = Requester(ani, max_requests=1)
    req.call(LIMITS_QUERY, {})
    headers = req.last_headers
    def number(name):
        try:
            return float(headers[name])
        except (KeyError, ValueError):
            return None
    return {"limit": number("X-RateLimit-Limit"), "remaining": number("X-RateLimit-Remaining"),
            "reset": number("X-RateLimit-Reset"), "requests": req.info(),
            "rate_limit": _limiter_state(ani), "warnings": []}
