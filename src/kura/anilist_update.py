"""Write one entry of the logged-in user's AniList list: progress, status, score (issue #45).

This is Kura's only AniList write. One call changes one entry of the token owner's own list:

1. the target is an AniList media ID, or a title: matched on the viewer's stored list (exact, or
   the one entry holding every word), else exactly on any stored media row, else (opt-in) exactly
   in one public search; a tie or a near miss refuses and lists the candidates;
2. one authenticated read fetches the media and the viewer's current entry;
3. the change is planned from that entry. Progress never goes down without ``allow_decrease`` and
   never past a known episode count; setting progress on a new, planned, paused or dropped entry
   makes it CURRENT, and reaching the final episode makes it COMPLETED, unless a status is given;
4. one ``SaveMediaListEntry`` mutation sends only the fields that change (none: no write);
5. the saved entry is written back into the local snapshot.

Every request goes through :class:`kura.anilist_list.Requester`, so it shares the rate budget and
the pinned transport; the token is read from the private token file and never returned.
"""
import json
import math

from . import anilist_auth, anilist_list
from .anilist import MAX_TITLE, QUERY as SEARCH_QUERY, parse_response
from .identity import list_snapshot
from .models import KuraError
from .releases import normalize

STATUSES = anilist_list.STATUSES
MAX_PROGRESS = 100_000
MAX_CANDIDATES = 10
ENTRY = "id userId mediaId status progress repeat updatedAt score(format:POINT_10_DECIMAL)"
READ_QUERY = ("query($id:Int){Media(id:$id,type:ANIME){%s mediaListEntry{%s}}}"
              % (anilist_list.MEDIA, ENTRY))
# Variable name -> GraphQL type. The mutation declares only the variables it sends, so a field
# that is not being changed is never sent (an explicit null could clear it).
FIELDS = {"progress": "Int", "status": "MediaListStatus", "scoreRaw": "Int"}
ACTIVATE = ("PLANNING", "PAUSED", "DROPPED")


def _bad(message: str, **details):
    return KuraError("invalid_arguments", message, **details)


def check_args(*, anilist_id=None, title=None, progress=None, status=None, score=None, search=False):
    """Validate before any state or network is touched."""
    if (anilist_id is None) == (title is None):
        raise _bad("Give exactly one of --id or --title")
    if search and title is None:
        raise _bad("--search needs --title")
    if anilist_id is not None and not anilist_list.valid_id(anilist_id):
        raise _bad("--id must be a positive AniList media ID")
    if title is not None and (not normalize(title) or len(title) > MAX_TITLE
                              or any(ord(c) < 32 or ord(c) == 127 for c in title)):
        raise _bad("--title must be 1..200 printable characters")
    if progress is None and status is None and score is None:
        raise _bad("Nothing to change: give --progress, --status or --score")
    if progress is not None and not 0 <= progress <= MAX_PROGRESS:
        raise _bad("--progress must be 0..100000")
    if status is not None and status not in STATUSES:
        raise _bad("--status must be one of " + ", ".join(STATUSES))
    if score is not None and (not math.isfinite(score) or not 0 <= score <= 10
                              or abs(score * 10 - round(score * 10)) > 1e-6):
        raise _bad("--score must be 0..10 with at most one decimal (0 clears it)")


# -- target ---------------------------------------------------------------------------------

def _candidates(rows: list[dict], query: str) -> list[dict]:
    """Rows any of whose titles equal the query or contain all of its words (for a refusal)."""
    wanted = normalize(query)
    tokens = set(wanted.split())
    out = []
    for row in rows:
        for title in row["titles"]:
            candidate = normalize(title)
            if candidate == wanted or (tokens and tokens <= set(candidate.split())):
                out.append({"anilist_id": row["id"], "title": title})
                break
    return out[:MAX_CANDIDATES]


def _media_rows(db) -> list[dict]:
    rows = []
    for anilist_id, *names, synonyms in db.execute(
            "SELECT anilist_id,title_romaji,title_english,title_native,synonyms FROM anilist_media "
            "ORDER BY anilist_id"):
        try:
            extra = json.loads(synonyms) if synonyms else []
        except ValueError:
            extra = []
        titles = [t for t in [*names, *(extra if isinstance(extra, list) else [])] if isinstance(t, str) and t]
        if titles:
            rows.append({"id": anilist_id, "titles": titles})
    return rows


def _pick(rows: list[dict], title: str, where: str, *, exact: bool):
    """(anilist_id | None, record). Raises on a tie. ``exact`` refuses word-subset matches."""
    if exact:
        wanted = normalize(title)
        hits = {}
        for row in rows:
            for name in row["titles"]:
                if normalize(name) == wanted:
                    hits.setdefault(row["id"], name)
        if len(hits) > 1:
            raise KuraError("anilist_title_ambiguous", "Several AniList entries match that title; use --id",
                            where=where, candidates=_candidates(rows, title))
        if not hits:
            return None, None
        (anilist_id, matched), = hits.items()
        return anilist_id, {"via": where, "matched": matched}
    anilist_id, record = _pick(rows, title, where, exact=True)
    if anilist_id is not None:
        return anilist_id, record
    candidates = _candidates(rows, title)        # entries holding every word of the title
    if len({c["anilist_id"] for c in candidates}) > 1:
        raise KuraError("anilist_title_ambiguous", "Several AniList entries match that title; use --id",
                        where=where, candidates=candidates)
    if not candidates:
        return None, None
    return candidates[0]["anilist_id"], {"via": where, "matched": candidates[0]["title"]}


def resolve_target(db, req, title: str, *, search: bool):
    """Resolve a title to one AniList ID, never by guessing.

    In order: an exact title (romaji, English, native or a synonym) on the viewer's list; an exact
    title on any stored media row; the one entry on the viewer's list containing every word of the
    title (two such entries refuse; there is no ranking); with ``search``, an exact title in one
    public lookup. A tie at any stage, or no match, refuses with candidates.
    """
    list_rows, media_rows = list_snapshot(db), _media_rows(db)
    for rows, where in ((list_rows, "list"), (media_rows, "media")):
        anilist_id, record = _pick(rows, title, where, exact=True)
        if anilist_id is not None:
            return anilist_id, record
    anilist_id, record = _pick(list_rows, title, "list", exact=False)
    if anilist_id is not None:
        return anilist_id, record
    if not search:
        raise KuraError("anilist_title_unknown",
                        "No stored AniList entry has that exact title; use --id, or --search for one online lookup",
                        candidates=_candidates(media_rows, title))
    data = req.call(SEARCH_QUERY, {"search": title})
    rows = parse_response(json.dumps({"data": data}).encode())
    anilist_id, record = _pick(rows, title, "search", exact=True)
    if anilist_id is None:
        raise KuraError("anilist_title_unknown", "AniList search found no entry with exactly that title; use --id",
                        candidates=[{"anilist_id": r["id"], "title": r["titles"][0]} for r in rows][:MAX_CANDIDATES])
    return anilist_id, record


# -- read, plan, write ----------------------------------------------------------------------

def _entry(item):
    """The viewer's entry as {status, progress, score, repeat, updated_at}, or None."""
    if not isinstance(item, dict):
        return None
    if item.get("status") not in STATUSES:
        raise KuraError("anilist_invalid_response", "AniList returned a list entry with an unknown status")
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 < score <= 10:
        score = None
    progress = item.get("progress")
    return {"status": item["status"], "progress": progress if anilist_list.valid_id(progress) else 0,
            "score": float(score) if score else None,
            "repeat": item.get("repeat") if isinstance(item.get("repeat"), int) else None,
            "updated_at": item.get("updatedAt") if isinstance(item.get("updatedAt"), int) else None,
            "user_id": item.get("userId") if anilist_list.valid_id(item.get("userId")) else None}


def read_current(req, anilist_id: int):
    """(media row, entry | None) with one authenticated request."""
    try:
        data = req.call(READ_QUERY, {"id": anilist_id}, auth=True)
    except KuraError as e:
        if e.code == "anilist_not_found":
            raise KuraError("anilist_media_not_found", "AniList has no anime with that ID",
                            anilist_id=anilist_id) from e
        raise
    item = data.get("Media")
    media = anilist_list.parse_media(item)
    if media is None or media["anilist_id"] != anilist_id:
        raise KuraError("anilist_media_not_found", "AniList has no anime with that ID", anilist_id=anilist_id)
    return media, _entry(item.get("mediaListEntry"))


def plan(current, media: dict, *, progress=None, status=None, score=None, allow_decrease=False):
    """The fields to send ({} when nothing changes) and any warnings. Pure."""
    episodes = media.get("episodes")
    have = current or {"status": None, "progress": 0, "score": None}
    warnings = []
    if progress is not None:
        if episodes and progress > episodes:
            raise KuraError("anilist_progress_out_of_range", "Progress is past the last episode",
                            progress=progress, episodes=episodes)
        if progress < have["progress"] and not allow_decrease:
            raise KuraError("anilist_progress_decrease", "That would lower progress; pass --allow-decrease",
                            progress=progress, current=have["progress"])
        aired = media.get("next_airing_episode")
        if aired and progress > aired - 1:
            warnings.append("anilist_progress_ahead_of_airing")
    target = status
    if target is None and progress is not None and progress > 0:
        if (episodes and progress == episodes and media.get("status") == "FINISHED"
                and have["status"] != "REPEATING"):     # a rewatch is the owner's to close (repeat count)
            target = "COMPLETED"
        elif have["status"] is None or have["status"] in ACTIVATE:
            target = "CURRENT"
    send = {}
    if progress is not None and (current is None or progress != have["progress"]):
        send["progress"] = progress
    if target is not None and target != have["status"]:
        send["status"] = target
    if score is not None and (have["score"] or 0) != score:
        send["scoreRaw"] = round(score * 10)
    if current is None:
        send["status"] = target or "PLANNING"   # a new entry always gets an explicit status
    return send, warnings


def save_mutation(fields: dict) -> str:
    names = [n for n in FIELDS if n in fields]
    declared = "".join(f",${n}:{FIELDS[n]}" for n in names)
    passed = "".join(f",{n}:${n}" for n in names)
    return ("mutation($mediaId:Int%s){SaveMediaListEntry(mediaId:$mediaId%s){%s}}"
            % (declared, passed, ENTRY))


def write_back(db, viewer_id, anilist_id: int, media: dict, entry: dict, now: float):
    """Put the saved entry into the local snapshot (only for the account it belongs to)."""
    if not anilist_list.valid_id(viewer_id) or entry.get("user_id") not in (None, viewer_id):
        return False
    with db:
        db.execute(
            "INSERT INTO anilist_list_entries(user_id,anilist_id,status,score,progress,repeat,updated_at,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,anilist_id) DO UPDATE SET status=excluded.status, "
            "score=excluded.score, progress=excluded.progress, repeat=excluded.repeat, "
            "updated_at=excluded.updated_at, fetched_at=excluded.fetched_at",
            (viewer_id, anilist_id, entry["status"], entry["score"], entry["progress"], entry["repeat"],
             entry["updated_at"], now))
        anilist_list.upsert_media(db, [media], now)
    return True


def _title(media: dict):
    return media.get("title_english") or media.get("title_romaji") or media.get("title_native")


def update(store, ani, *, anilist_id=None, title=None, search=False, progress=None, status=None, score=None,
           allow_decrease=False, dry_run=False) -> dict:
    """Plan and (unless ``dry_run``) save one change to the viewer's AniList entry."""
    check_args(anilist_id=anilist_id, title=title, progress=progress, status=status, score=score, search=search)
    now = ani.wall()
    record = anilist_auth.load(store.root)
    left, token_warnings = anilist_auth.expiry_state(record["expires_at"], now)
    if left <= 0:
        raise KuraError("anilist_token_expired", "The stored token has expired; log in again")
    req = anilist_list.Requester(ani, max_requests=3, token=record["token"])
    resolved = None
    if anilist_id is None:
        anilist_id, resolved = resolve_target(store.db, req, title, search=search)
    media, current = read_current(req, anilist_id)
    send, warnings = plan(current, media, progress=progress, status=status, score=score,
                          allow_decrease=allow_decrease)
    warnings = [w for w in token_warnings if w != "anilist_token_expired"] + warnings
    viewer = record.get("viewer") if isinstance(record.get("viewer"), dict) else {}
    out = {"anilist_id": anilist_id, "title": _title(media), "episodes": media.get("episodes"),
           "resolved": resolved, "before": None if current is None else
           {k: current[k] for k in ("status", "progress", "score")},
           "change": {("score" if k == "scoreRaw" else k): (v / 10 if k == "scoreRaw" else v)
                      for k, v in send.items()},
           "dry_run": dry_run, "written": False, "after": None, "snapshot_updated": False,
           "warnings": warnings}
    if dry_run or not send:
        out["requests"] = req.info()
        return out
    data = req.call(save_mutation(send), {"mediaId": anilist_id, **send}, auth=True)
    saved = _entry(data.get("SaveMediaListEntry"))
    if saved is None:
        raise KuraError("anilist_invalid_response", "AniList did not return the saved entry")
    if saved["user_id"] is not None and anilist_list.valid_id(viewer.get("id")) and saved["user_id"] != viewer["id"]:
        warnings.append("anilist_viewer_mismatch")      # the token's account changed since login
    out.update(written=True, after={k: saved[k] for k in ("status", "progress", "score")},
               snapshot_updated=write_back(store.db, viewer.get("id"), anilist_id, media, saved, now),
               requests=req.info())
    return out
