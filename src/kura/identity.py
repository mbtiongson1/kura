"""Title identity: which AniList entry is each library title? Needed so a retain list can bind.

``sync`` maps every manifest title (key ``<root label>/<entry>``) to an AniList ID and records
the outcome in ``title_identities``. Two passes, cheapest first:

  1. offline, against the owner's list snapshot (``anilist_media`` titles and synonyms joined
     with ``anilist_list_entries``): the folder name reduced by ``resolve.derive_terms`` must
     equal a title or be a whole-word subset of one (``anilist.choose`` semantics, ties are
     ``ambiguous`` with the tied IDs recorded);
  2. alias fallback (network, shared rate limiter, bounded by ``max_lookups``) through the
     same cached ``Core.alias_status`` the rest of Kura uses.

Season rule: a folder with a season marker 2 or higher is accepted only by an entry whose
matched title carries that season ("Season N", "Nth Season", "SN" or a trailing "N"); a
folder marked season 1 never binds to an entry titled for a later season; a folder spanning
several seasons (or season 0) is ``ambiguous``. Everything undecidable stays unresolved or
ambiguous, never guessed: a wrong binding is the one way a retained title could be quarantined.

Owner rows (``pinned`` and ``not_anime``, source ``owner``) are never overwritten by a sync;
only ``pin`` changes them. A resolved row is kept as it is until ``refresh`` asks to revalidate
it. A refresh first drops every resolved row the offline pass cannot reconfirm, then tries the
network within the budget, so whatever it cannot reconfirm (no match, failed lookup, budget
spent, ``--offline``) ends up weaker than ``resolved``, never stale. The module reads and
writes only Kura's private state database and never touches a library.
"""
from contextlib import contextmanager
import json
import re
import time

from .anilist import clean
from .core import Core
from .manifest_store import ManifestRepo
from .models import KuraError
from .releases import normalize
from . import resolve

STATUSES = ("resolved", "pinned", "ambiguous", "unresolved", "unavailable", "not_anime")
IDENTIFIED = ("resolved", "pinned")
MAX_LOOKUPS = 1000
MAX_LISTED = 500
MAX_ID = 10**9

_SEASON_WORDS = (
    lambda n: re.compile(rf"\bseason 0*{n}\b"),
    lambda n: re.compile(rf"\b0*{n}(?:st|nd|rd|th) season\b"),
    lambda n: re.compile(rf"\bs0*{n}\b"),
)
_LATER = (re.compile(r"\bseason 0*([2-9]|[1-9][0-9])\b"),
          re.compile(r"\b0*([2-9]|[1-9][0-9])(?:st|nd|rd|th) season\b"),
          re.compile(r"\bs0*([2-9]|[1-9][0-9])\b"))


def _bad(message: str, **details):
    return KuraError("invalid_arguments", message, **details)


@contextmanager
def _tx(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


# ---- title and season parsing ----------------------------------------------------------

def folder_terms(key: str) -> tuple[str, int | None, bool]:
    """``(base title, season, undecidable)`` for a title key. Undecidable means a season range,
    several season markers or season 0: the folder cannot bind to one AniList entry."""
    entry = key.partition("/")[2] or key
    base = resolve.derive_terms(entry).base
    text = resolve._PAIRS.sub(" ", resolve._flatten(resolve._EXT.sub("", entry.strip())))
    numbers, ranged = set(), False
    for pattern in resolve._SEASONS:
        for found in pattern.finditer(text):
            numbers.add(int(found.group(1)))
            if pattern.groups > 1 and found.group(2):
                ranged = True
    season = next(iter(numbers)) if len(numbers) == 1 else None
    return base, season, ranged or len(numbers) > 1 or season == 0


def carries_season(title: str, season: int) -> bool:
    """True when an AniList title names season ``season`` (``2`` also matches a trailing 2)."""
    text = normalize(title)
    if any(make(season).search(text) for make in _SEASON_WORDS):
        return True
    tokens = text.split()
    return len(tokens) > 1 and tokens[-1] == str(season)


def names_later_season(title: str, base: str = "") -> bool:
    """True when a title names season 2 or later: an explicit marker, or a trailing number
    (unless the folder title itself ends in that same number, as in a title like "... 100")."""
    text = normalize(title)
    if any(p.search(text) for p in _LATER):
        return True
    tokens, own = text.split(), normalize(base).split()
    return (len(tokens) > 1 and tokens[-1].isdecimal() and 2 <= int(tokens[-1]) <= 99
            and tokens[-1] != (own[-1] if own else None))


def _title_ok(title: str, season: int | None, base: str = "") -> bool:
    if season is not None and season >= 2:
        return carries_season(title, season)
    if season == 1:
        return not names_later_season(title, base)
    return True


# ---- matching --------------------------------------------------------------------------

def list_snapshot(db) -> list[dict]:
    """``[{"id", "titles"}]`` for the owner's list (the viewer's, else any synced list)."""
    viewers = [r[0] for r in db.execute("SELECT user_id FROM anilist_users WHERE is_viewer=1")]
    sql = ("SELECT DISTINCT m.anilist_id, m.title_romaji, m.title_english, m.title_native, m.synonyms"
           " FROM anilist_media m JOIN anilist_list_entries e ON e.anilist_id = m.anilist_id")
    params: tuple = ()
    if viewers:
        sql += " WHERE e.user_id IN (%s)" % ",".join("?" * len(viewers))
        params = tuple(viewers)
    rows = []
    for anilist_id, *names, synonyms in db.execute(sql + " ORDER BY m.anilist_id", params):
        try:
            extra = json.loads(synonyms) if synonyms else []
        except ValueError:
            extra = []
        titles, seen = [], set()
        for value in [*names, *(extra if isinstance(extra, list) else [])]:
            text, key = clean(value), normalize(value) if isinstance(value, str) else ""
            if text and key and key not in seen:
                seen.add(key)
                titles.append(text)
        if titles:
            rows.append({"id": anilist_id, "titles": titles})
    return rows


def match(rows: list[dict], base: str, season: int | None) -> dict:
    """Match a folder title against ``rows`` with ``anilist.choose`` semantics plus the season rule."""
    wanted = normalize(base)
    tokens = set(wanted.split())
    scored = []
    for row in rows:
        best = None
        for title in row["titles"]:
            if not _title_ok(title, season, base):
                continue
            candidate = normalize(title)
            words = set(candidate.split())
            if candidate == wanted:
                key = (0, 0)
            elif tokens and tokens <= words:
                key = (1, len(words - tokens))
            else:
                continue
            if best is None or key < best[0]:
                best = (key, title)
        if best:
            scored.append((best[0], row["id"], best[1]))
    if not wanted or not scored:
        return {"status": "unresolved", "anilist_id": None, "matched": None, "candidates": []}
    top = min(key for key, _, _ in scored)
    tied = sorted({i for key, i, _ in scored if key == top})
    if len(tied) > 1:
        return {"status": "ambiguous", "anilist_id": None, "matched": None, "candidates": tied}
    _, chosen, matched = next(s for s in scored if s[1] == tied[0])
    # Weaker matches stay plausible (an unmarked folder may hold several seasons), so retain
    # planning must see them even when the winner is an exact match.
    others = sorted({i for _, i, _ in scored if i != chosen})
    return {"status": "resolved", "anilist_id": chosen, "matched": matched, "candidates": others}


# ---- storage ---------------------------------------------------------------------------

def load(db, library: str) -> dict[str, dict]:
    """Recorded identities by title key. A row whose candidate list cannot be read is degraded
    to ``unresolved`` in memory so every consumer fails closed."""
    out = {}
    for (title, anilist_id, status, source, query, matched, candidates,
         updated_at) in db.execute(
            "SELECT title, anilist_id, status, source, query, matched, candidates, updated_at"
            " FROM title_identities WHERE library=? ORDER BY title", (library,)):
        try:
            listed = json.loads(candidates)
            ok = isinstance(listed, list) and all(type(i) is int for i in listed)
        except ValueError:
            listed, ok = [], False
        if not ok:
            listed, status = [], ("unresolved" if status in IDENTIFIED else status)
        out[title] = {"anilist_id": anilist_id, "status": status, "source": source, "query": query,
                      "matched": matched, "candidates": listed, "updated_at": updated_at}
    return out


def _store(db, library: str, records: dict[str, dict], now: float):
    """Upsert records; an owner row is never replaced (checked again inside the transaction)."""
    if not records:
        return
    with _tx(db):
        for title, r in sorted(records.items()):
            db.execute(
                "INSERT INTO title_identities(library, title, anilist_id, status, source, query, matched,"
                " candidates, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(library, title) DO UPDATE SET anilist_id=excluded.anilist_id,"
                " status=excluded.status, source=excluded.source, query=excluded.query,"
                " matched=excluded.matched, candidates=excluded.candidates,"
                " updated_at=excluded.updated_at WHERE title_identities.source <> 'owner'",
                (library, title, r["anilist_id"], r["status"], r["source"], r.get("query"),
                 r.get("matched"), json.dumps(sorted(r["candidates"])), now))


def _record(found: dict, source: str, query: str | None) -> dict:
    return {**found, "source": source, "query": query[:200] if query else None}


def _same(old: dict | None, new: dict) -> bool:
    return old is not None and all(
        old[k] == new[k] for k in ("anilist_id", "status", "source", "matched", "candidates"))


class _Counting:
    """Counts the network lookups an AniList resolver actually makes (cache hits are free)."""

    def __init__(self, inner):
        self.inner, self.made = inner, 0

    def resolve(self, title):
        self.made += 1
        return self.inner.resolve(title)


def _alias(core, counter, base: str, season: int | None, refresh: bool, budget: int):
    """Alias fallback for one title: ``(record|None, stop, out_of_budget)``. The season variant
    is asked first; a result that does not carry the folder's season is never accepted."""
    queries = [f"{base} Season {season}", base] if season is not None and season >= 2 else [base]
    rejected = None
    for query in queries:
        if counter.made >= budget:
            return None, False, True
        try:
            result = core.alias_status(query, refresh=refresh)
        except KuraError as e:
            if e.code != "invalid_query":
                raise
            return _record({"status": "unresolved", "anilist_id": None, "matched": None,
                            "candidates": []}, "alias", None), False, False
        status = result["status"]
        if status == "unavailable":
            return _record({"status": "unavailable", "anilist_id": None, "matched": None,
                            "candidates": []}, "alias", query), True, False
        if status == "ambiguous":
            return _record({"status": "ambiguous", "anilist_id": None, "matched": None,
                            "candidates": []}, "alias", query), False, False
        identity = result.get("identity") if status == "resolved" else None
        if identity:
            if _title_ok(identity["matched"], season, base):
                return _record({"status": "resolved", "anilist_id": identity["anilist_id"],
                                "matched": clean(identity["matched"]), "candidates": []},
                               "alias", query), False, False
            rejected = rejected or (identity["anilist_id"], query)
    found = {"status": "unresolved", "anilist_id": None, "matched": None,
             "candidates": [rejected[0]] if rejected else []}
    return _record(found, "alias", queries[-1]), False, False


def check_sync_args(library, titles, max_lookups):
    if not isinstance(library, str) or not library:
        raise _bad("A library name is required")
    if titles is not None and (not isinstance(titles, list) or not all(isinstance(t, str) for t in titles)):
        raise _bad("Titles must be a list of title keys")
    if isinstance(max_lookups, bool) or not isinstance(max_lookups, int) or not 0 <= max_lookups <= MAX_LOOKUPS:
        raise _bad(f"max_lookups must be a whole number from 0 to {MAX_LOOKUPS}")


def _manifest_titles(store, library: str) -> list[str]:
    keys = [r["rel_path"] for r in ManifestRepo(store.db).titles(library)]
    if not keys:
        raise KuraError("not_found", "No manifest for that library; run manifest build first")
    return keys


def _counts(rows: dict[str, dict], wanted: list[str]) -> dict:
    counts = {s: 0 for s in STATUSES}
    by_source = {"list": 0, "alias": 0}
    for key in wanted:
        row = rows.get(key)
        if row is None:
            continue
        counts[row["status"]] += 1
        if row["status"] == "resolved":
            by_source[row["source"]] += 1
    return counts, by_source


def sync(store, *, library: str, titles: list[str] | None = None, aliases=None,
         max_lookups: int = 60, refresh: bool = False, now=time.time) -> dict:
    """Identify titles. ``aliases`` is an AniList resolver or None (offline only)."""
    check_sync_args(library, titles, max_lookups)
    manifest = _manifest_titles(store, library)
    wanted = sorted(set(titles)) if titles is not None else manifest
    missing = sorted(set(wanted) - set(manifest))
    if missing:
        raise KuraError("not_found", "Title not in the manifest", titles=missing[:20])
    existing = load(store.db, library)
    snapshot = list_snapshot(store.db)
    offline, need = {}, []
    for key in wanted:
        current = existing.get(key)
        if current is not None and current["source"] == "owner":
            continue
        base, season, undecidable = folder_terms(key)
        found = ({"status": "ambiguous", "anilist_id": None, "matched": None, "candidates": []}
                 if undecidable else match(snapshot, base, season))
        known = current is not None and current["status"] == "resolved"
        if found["status"] == "unresolved":
            if known and not refresh:
                continue                    # keep the earlier identity; no new lookup
            need.append((key, base, season))    # a refresh drops what it cannot reconfirm
        record = _record(found, "list", base)
        if not _same(current, record):
            offline[key] = record
    moment = now()
    _store(store.db, library, offline, moment)
    existing.update(offline)
    counter = _Counting(aliases) if aliases is not None else None
    pending = 0
    if counter is not None:
        core = Core(store, None, counter)
        found_now, stop = {}, False
        try:
            for key, base, season in need:
                if stop:
                    pending += 1
                    continue
                if not normalize(base):
                    continue
                record, stop, exhausted = _alias(core, counter, base, season, refresh, max_lookups)
                if exhausted:
                    stop = True
                    pending += 1
                    continue
                if not _same(existing.get(key), record):
                    found_now[key] = record
        finally:
            _store(store.db, library, found_now, now())
            existing.update(found_now)
    rows = load(store.db, library)
    counts, by_source = _counts(rows, wanted)
    return {"library": library, "titles": len(wanted),
            "resolved": by_source, "pinned": counts["pinned"], "ambiguous": counts["ambiguous"],
            "unresolved": counts["unresolved"], "unavailable": counts["unavailable"],
            "not_anime": counts["not_anime"],
            "lookups": {"made": counter.made if counter else 0, "budget": max_lookups if counter else 0},
            "pending": pending}


def pin(store, library: str, title: str, anilist_id: int | None = None, *,
        not_anime: bool = False, clear: bool = False, now=time.time) -> dict:
    """Owner decision for one title: bind it to an AniList ID, mark it not anime, or clear it."""
    if not isinstance(library, str) or not library or not isinstance(title, str) or not title:
        raise _bad("A library and a title key are required")
    if (anilist_id is not None) + bool(not_anime) + bool(clear) != 1:
        raise _bad("Give exactly one of an AniList ID, not-anime or clear")
    if anilist_id is not None and (isinstance(anilist_id, bool) or not isinstance(anilist_id, int)
                                   or not 0 < anilist_id < MAX_ID):
        raise _bad("AniList ID must be a positive whole number below 1000000000")
    with store.lock():
        if clear:
            with _tx(store.db):
                cur = store.db.execute(
                    "DELETE FROM title_identities WHERE library=? AND title=? AND source='owner'",
                    (library, title))
            return {"library": library, "title": title, "status": "cleared" if cur.rowcount else "none",
                    "anilist_id": None}
        if title not in _manifest_titles(store, library):
            raise KuraError("not_found", "Title not in the manifest", title=title)
        status = "pinned" if anilist_id is not None else "not_anime"
        with _tx(store.db):
            store.db.execute(
                "INSERT INTO title_identities(library, title, anilist_id, status, source, query, matched,"
                " candidates, updated_at) VALUES (?,?,?,?,'owner',NULL,NULL,'[]',?)"
                " ON CONFLICT(library, title) DO UPDATE SET anilist_id=excluded.anilist_id,"
                " status=excluded.status, source='owner', query=NULL, matched=NULL, candidates='[]',"
                " updated_at=excluded.updated_at", (library, title, anilist_id, status, now()))
    return {"library": library, "title": title, "status": status, "anilist_id": anilist_id}


def show(store, library: str, status: str | None = None) -> dict:
    """Recorded identities, newest state; read-only. ``unrecorded`` counts manifest titles with no row."""
    if status is not None and status not in STATUSES:
        raise _bad("Unknown status filter", allowed=list(STATUSES))
    rows = load(store.db, library)
    keys = [r["rel_path"] for r in ManifestRepo(store.db).titles(library)]
    counts = {s: 0 for s in STATUSES}
    for key in keys:
        if key in rows:
            counts[rows[key]["status"]] += 1
    listed = [{"title": k, **v} for k, v in sorted(rows.items()) if status is None or v["status"] == status]
    out = {"library": library, "counts": {k: v for k, v in counts.items() if v},
           "unrecorded": sum(1 for k in keys if k not in rows), "titles": listed[:MAX_LISTED]}
    if len(listed) > MAX_LISTED:
        out["titles_truncated"] = len(listed) - MAX_LISTED
    return out


def retain_reasons(key: str, identities: dict[str, dict], retain) -> list[str]:
    """Why a title must not be proposed for quarantine under a retain list (empty when it may be).

    Fail closed: a title without a resolved or pinned identity is refused ``identity_unknown``.
    """
    row = identities.get(key)
    if (row is None or row["status"] not in IDENTIFIED or type(row["anilist_id"]) is not int
            or not isinstance(row.get("candidates"), list)):
        return ["identity_unknown"]
    reasons = []
    if row["anilist_id"] in retain:
        reasons.append("retained")
    if any(c in retain for c in row["candidates"]):
        reasons.append("retain_possible")
    return reasons


def retain_check(store, library: str, retain) -> dict:
    """What a retain list would protect in this library. Read-only."""
    keys = _manifest_titles(store, library)
    rows = load(store.db, library)
    retained, possible, unidentified, bound = [], [], 0, set()
    for key in keys:
        row = rows.get(key)
        if row is None or row["status"] not in IDENTIFIED:
            unidentified += 1
        elif row["anilist_id"] in retain:
            bound.add(row["anilist_id"])
            retained.append({"title": key, "anilist_id": row["anilist_id"], "source": row["source"]})
        hits = sorted(c for c in (row["candidates"] if row else []) if c in retain)
        if hits and not (row["status"] in IDENTIFIED and row["anilist_id"] in retain):
            possible.append({"title": key, "candidates": hits})
    return {"library": library, "retain": retain.summary(), "retained_titles": retained,
            "unmatched_ids": sorted(set(retain.ids) - bound), "possible": possible,
            "unidentified_titles": unidentified}
