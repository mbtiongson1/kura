"""AniList relation cache: which media continue which (SEQUEL/PREQUEL), for multi-part titles.

Public, token-less and read-only. Every request goes through the same ``Requester`` as the media
sync, so it shares the cross-process rate budget and the per-command request budget. A fetch
replaces the cached edges of the media it covers and records ``anilist_relation_fetches``; a
failed or partial fetch changes nothing for the media it did not cover. Each media entry reached
through a relation edge also lands in ``anilist_media`` (title, format, episode count, status),
which is what ``titleparts`` needs to place files.

The crawl follows only ``SEQUEL``/``PREQUEL`` edges, breadth first from the library's resolved
identities, bounded by ``MAX_NODES`` and the request budget. Discovery reads the cache offline
and refuses any franchise whose closure is not fully fetched, so a partial crawl is safe.
"""
from . import anilist_list, identity
from .anilist_list import Requester, parse_media, upsert_media, valid_id
from .manifest_store import ManifestRepo
from .models import KuraError

CHAIN = ("SEQUEL", "PREQUEL")
BATCH = 10                  # relations carry nested media: keep each request small
MAX_EDGES = 100             # per media; more is treated as an unreadable response
MAX_NODES = 300             # media visited by one command
DAY = 86400.0

RELATIONS_QUERY = ("query($ids:[Int],$n:Int){Page(perPage:$n){media(id_in:$ids,type:ANIME){id "
                   "relations{edges{relationType node{id type %s}}}}}}"
                   % anilist_list.MEDIA.replace("id title", "title", 1))


def parse_item(item, wanted):
    """One Media object -> ``(id, [(related_id, relation)], [media rows])`` or None when unreadable."""
    if not isinstance(item, dict) or not valid_id(item.get("id")) or item["id"] not in wanted:
        return None
    relations = item.get("relations")
    edges = relations.get("edges") if isinstance(relations, dict) else None
    if not isinstance(edges, list) or len(edges) > MAX_EDGES:
        return None
    found, rows = set(), {}
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        kind = edge.get("relationType") if isinstance(edge, dict) else None
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            return None                                 # a malformed edge makes the whole item unreadable
        if node["type"] != "ANIME":
            continue                                    # manga and light novels are not part of a library
        if not anilist_list._enum(kind) or not valid_id(node.get("id")) or node["id"] == item["id"]:
            return None
        found.add((node["id"], kind))
        row = parse_media(node)
        if row is not None:
            rows[row["anilist_id"]] = row
    return item["id"], sorted(found), list(rows.values())


def check_args(ids, library, max_age_d, max_requests):
    anilist_list.check_budget(max_requests)
    if (ids is None) == (library is None):
        raise KuraError("invalid_arguments", "Give --id N (repeatable) or --library NAME")
    if ids is not None and (not ids or not all(valid_id(i) for i in ids)):
        raise KuraError("invalid_arguments", "--id must be a positive AniList ID")
    if library is not None and (not library or len(library) > 200 or any(ord(c) < 32 for c in library)):
        raise KuraError("invalid_arguments", "Invalid --library name")
    if not 0 <= max_age_d <= 3650:
        raise KuraError("invalid_arguments", "--max-age-d must be between 0 and 3650")


def seed_ids(store, library: str) -> list[int]:
    """The library's media titles that discovery may derive parts for: resolved by Kura (never an
    owner decision), so their AniList ID is a seed of the franchise."""
    titles = ManifestRepo(store.db).titles(library)
    media = identity._media_titles(store, titles)
    rows = identity.load(store.db, library)
    return sorted({r["anilist_id"] for k, r in rows.items()
                   if k in media and r["status"] == "resolved" and r["source"] != "owner"
                   and valid_id(r["anilist_id"])})


def fetches(db) -> dict[int, tuple[float, str]]:
    return {i: (at, st) for i, at, st in
            db.execute("SELECT anilist_id, fetched_at, status FROM anilist_relation_fetches")}


def neighbours(db, anilist_id: int) -> set[int]:
    """Media linked to ``anilist_id`` by a SEQUEL/PREQUEL edge in either direction."""
    marks = ",".join("?" * len(CHAIN))
    out = {r[0] for r in db.execute(
        f"SELECT related_id FROM anilist_relations WHERE anilist_id=? AND relation IN ({marks})",
        (anilist_id, *CHAIN))}
    out |= {r[0] for r in db.execute(
        f"SELECT anilist_id FROM anilist_relations WHERE related_id=? AND relation IN ({marks})",
        (anilist_id, *CHAIN))}
    return out


def _save(db, found: dict, absent: list[int], rows: dict, now: float):
    with db:
        for anilist_id, edges in found.items():
            db.execute("DELETE FROM anilist_relations WHERE anilist_id=?", (anilist_id,))
            db.executemany("INSERT OR IGNORE INTO anilist_relations(anilist_id, related_id, relation)"
                           " VALUES (?,?,?)", [(anilist_id, rid, kind) for rid, kind in edges])
            db.execute("INSERT OR REPLACE INTO anilist_relation_fetches(anilist_id, fetched_at, status)"
                       " VALUES (?,?,'ok')", (anilist_id, now))
        for anilist_id in absent:
            db.execute("DELETE FROM anilist_relations WHERE anilist_id=?", (anilist_id,))
            db.execute("INSERT OR REPLACE INTO anilist_relation_fetches(anilist_id, fetched_at, status)"
                       " VALUES (?,?,'not_found')", (anilist_id, now))
        if rows:
            upsert_media(db, list(rows.values()), now)


def relations_sync(store, ani, *, ids=None, library=None, max_age_d=30.0, refresh=False,
                   max_requests=10) -> dict:
    """Fetch and cache the SEQUEL/PREQUEL closure of the given IDs (or of the library's resolved
    identities). Entries fetched within ``max_age_d`` days are served from the cache; ``refresh``
    fetches them again. Stops cleanly when the budget, the node bound or AniList says so."""
    check_args(ids, library, max_age_d, max_requests)
    db, now = store.db, ani.wall()
    floor = now if refresh else now - max_age_d * DAY       # a fetch at or after this is fresh

    def fresh(i):
        known = fetches(db).get(i)
        return known is not None and known[0] >= floor

    seeds = sorted(set(ids)) if ids is not None else seed_ids(store, library)
    total = len(seeds)
    seeds.sort(key=fresh)                   # stale first: repeated runs make progress through a large set
    truncated = len(seeds) > MAX_NODES
    seeds = seeds[:MAX_NODES]
    req = Requester(ani, max_requests=max_requests)
    seen, frontier = set(seeds), sorted(seeds)
    fetched = cached = 0
    absent: list[int] = []
    error, size = None, BATCH
    while frontier and error is None:
        pending = [i for i in frontier if not fresh(i)]
        cached += len(frontier) - len(pending)
        while pending:
            batch = pending[:size]
            try:
                data = req.call(RELATIONS_QUERY, {"ids": batch, "n": len(batch)})
            except KuraError as e:
                if e.code == "anilist_bad_request" and size > 1:
                    size = max(1, size // 2)
                    continue
                if not (fetched or absent or cached):
                    e.details["requests"] = req.info()
                    raise
                error = e.code
                break
            page = data.get("Page")
            items = page.get("media") if isinstance(page, dict) else None
            parsed = [parse_item(i, set(batch)) for i in items] if isinstance(items, list) else None
            if parsed is None or None in parsed or len({p[0] for p in parsed}) != len(parsed):
                error = "anilist_invalid_response"      # unreadable items are not proof of absence
                break
            got = {p[0]: p[1] for p in parsed}
            rows = {r["anilist_id"]: r for p in parsed for r in p[2]}
            gone = [i for i in batch if i not in got]
            _save(db, got, gone, rows, now)
            fetched += len(got)
            absent += gone
            pending = pending[len(batch):]
        if error is not None:
            break
        known, nxt = fetches(db), []
        for i in frontier:
            if known.get(i, (0, ""))[1] != "ok":
                continue
            for n in sorted(neighbours(db, i)):
                if n in seen:
                    continue
                if len(seen) >= MAX_NODES:
                    truncated = True
                    continue
                seen.add(n)
                nxt.append(n)
        frontier = nxt
    if error is not None and not (fetched or absent or cached):
        raise KuraError(error, "AniList returned unreadable relation data", requests=req.info())
    remaining = sum(1 for i in frontier if not fresh(i)) if error else 0
    return {"seeds": total, "visited": len(seen), "fetched": fetched, "cached": cached,
            "not_found": sorted(absent)[:100], "remaining": remaining,
            "complete": error is None and not truncated, "truncated": truncated, "error": error,
            "requests": req.info(), "rate_limit": anilist_list._limiter_state(ani), "warnings": []}
