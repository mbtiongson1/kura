"""Automatic discovery of derived (``relations``) parts for multi-part titles.

Offline: reads the manifest, the recorded identities and the relation cache that
``kura anilist relations`` filled; it never uses the network and never opens a library root.
For a media title whose identity Kura resolved itself, the SEQUEL/PREQUEL franchise around the
resolved ID becomes the title's parts, but only when every one of these holds:

* the identity is ``resolved`` (never an owner decision: pins, ``not_anime`` and owner parts
  always win and are left alone);
* every media of the franchise has a fresh, successful relation fetch and cached metadata, so a
  partly crawled franchise never produces parts;
* the TV entries form one unbranched chain from the resolved ID's franchise (a branch, a cycle or
  an unknown format is doubtful), movies/OVAs/specials ride along as side entries;
* ``titleparts.assign`` puts the title's own video files into at least two of those parts, i.e.
  the files really span several related media.

Derived parts are over-protective by design and fail closed: retain planning refuses an episode
file no part claims. A title that stops qualifying keeps its parts, even when its identity moved
(the effective view marks them stale and retain planning refuses the title until discovery
writes a replacement or the owner decides); only parts of a title that is no longer a resolved
media title, or that an owner decision now covers, are removed.
"""
import time

from . import anilist_relations, identity, titleparts
from .manifest_store import ManifestRepo
from .models import KuraError

MAX_COMPONENT = 50
MAX_LISTED = 50
SIDE = titleparts.SIDE_FORMATS
KNOWN_FORMATS = ("TV",) + tuple(SIDE)       # anything else AniList may add later is doubtful


def check_args(library, max_age_d):
    if not isinstance(library, str) or not library or len(library) > 200 or any(ord(c) < 32 for c in library):
        raise KuraError("invalid_arguments", "Invalid --library name")
    if isinstance(max_age_d, bool) or not isinstance(max_age_d, (int, float)) or not 0 <= max_age_d <= 3650:
        raise KuraError("invalid_arguments", "--max-age-d must be between 0 and 3650")


def component(db, seed: int) -> set[int] | None:
    """The SEQUEL/PREQUEL franchise around ``seed`` (None when larger than ``MAX_COMPONENT``)."""
    seen, todo = {seed}, [seed]
    while todo:
        for n in anilist_relations.neighbours(db, todo.pop()):
            if n not in seen:
                seen.add(n)
                todo.append(n)
                if len(seen) > MAX_COMPONENT:
                    return None
    return seen


def chain(db, members: set[int], info: dict) -> tuple[list[int], list[int]] | str:
    """``(main-line IDs in order, side IDs)`` or a reason string. The main line is every non-side
    entry; it must be one path of SEQUEL links with no branch and no cycle."""
    if any(info[i].get("format") not in KNOWN_FORMATS for i in members):
        return "media_unknown"
    tv = {i for i in members if info[i]["format"] not in SIDE}
    succ: dict[int, set[int]] = {i: set() for i in tv}
    marks = ",".join("?" * len(anilist_relations.CHAIN))
    ids = sorted(members)
    for a, b, kind in db.execute(
            "SELECT anilist_id, related_id, relation FROM anilist_relations WHERE relation IN (%s)"
            " AND anilist_id IN (%s)" % (marks, ",".join("?" * len(ids))), (*anilist_relations.CHAIN, *ids)):
        if a in tv and b in tv:
            succ[a if kind == "SEQUEL" else b].add(b if kind == "SEQUEL" else a)
    pred: dict[int, set[int]] = {i: set() for i in tv}
    for a, nexts in succ.items():
        for b in nexts:
            pred[b].add(a)
    heads = [i for i in tv if not pred[i]]
    if len(heads) != 1 or any(len(succ[i]) > 1 or len(pred[i]) > 1 for i in tv):
        return "chain_ambiguous"
    order, here = [], heads[0]
    while here is not None and len(order) <= len(tv):
        order.append(here)
        here = next(iter(succ[here])) if succ[here] else None
    if len(order) != len(tv):
        return "chain_ambiguous"            # a cycle, or entries linked only through side entries
    return order, sorted(members - tv)


def _evaluate(db, repo, title, row, fetched, now, max_age_d):
    """``(parts, summary)`` when ``title`` qualifies, else ``(None, reason)``."""
    seed = row["anilist_id"]
    members = component(db, seed)
    if members is None:
        return None, "too_large"
    if len(members) < 2:
        return None, "no_relations"
    floor = now - max_age_d * anilist_relations.DAY
    for i in members:
        got = fetched.get(i)
        if got is None or got[1] != "ok":
            return None, "relations_incomplete"
        if got[0] < floor:
            return None, "relations_stale"
    info = titleparts.media_info(db, members)
    if any(i not in info or not info[i]["titles"] for i in members):
        return None, "media_unknown"
    found = chain(db, members, info)
    if isinstance(found, str):
        return None, found
    main, side = found
    if seed not in main:
        return None, "seed_not_in_chain"
    parts = [{"anilist_id": i, "selector": "auto", "source": "relations", "ordinal": n,
              "chain_pos": n if n <= len(main) else None, "seed_id": seed}
             for n, i in enumerate(main + side, 1)]
    result = titleparts.assign(identity._local_files(repo, title), parts, info)
    owning = {v["part"] for v in result["files"].values() if v["part"] is not None}
    if len(owning) < 2:
        return None, "files_do_not_span"
    return parts, {"parts": [p["anilist_id"] for p in parts], "owning": len(owning),
                   "assigned_video": result["assigned_video"], "unassigned_video": result["unassigned_video"]}


def discover(store, library: str, *, max_age_d: float = 90.0, dry_run: bool = False, now=time.time) -> dict:
    """Derive and store ``relations`` parts for the library's titles (or only report, ``dry_run``)."""
    check_args(library, max_age_d)
    with store.lock():
        db = store.db
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        try:
            out = _run(db, store, library, max_age_d, dry_run, now())
            (db.rollback if dry_run else db.commit)()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
    return out


def _run(db, store, library, max_age_d, dry_run, moment):
    repo = ManifestRepo(db)
    rows_by_key = {r["rel_path"]: r for r in repo.titles(library)}
    if not rows_by_key:
        raise KuraError("not_found", "No manifest for that library; run manifest build first")
    media = identity._media_titles(store, list(rows_by_key.values()))
    rows = identity.load(db, library)
    stored = titleparts.load_parts(db, library)
    fetched = anilist_relations.fetches(db)
    skipped: dict[str, int] = {}
    listed: dict[str, list[str]] = {}
    found, written, unchanged, removed, stale = [], [], 0, [], []

    def skip(key, reason):
        skipped[reason] = skipped.get(reason, 0) + 1
        if reason not in ("no_relations", "files_do_not_span", "unidentified", "owner_decision", "not_anime"):
            listed.setdefault(reason, []).append(key)

    for key in sorted(set(rows_by_key) & (media | set(stored))):
        parts = stored.get(key, [])
        derived = [p for p in parts if p["source"] == "relations"]
        row = rows.get(key)
        if any(p["source"] == "owner" for p in parts):
            skip(key, "owner_parts")
            continue
        reason = None
        if key not in media:
            reason = "non_media"
        elif row is None:
            reason = "unidentified"
        elif row["status"] == "not_anime":
            reason = "not_anime"
        elif row["source"] == "owner":
            reason = "owner_decision"
        elif row["status"] != "resolved" or type(row["anilist_id"]) is not int:
            reason = "unidentified"
        moved = reason is None and bool(derived) and {p["seed_id"] for p in derived} != {row["anilist_id"]}
        if reason is not None:
            if reason != "non_media":
                skip(key, reason)
            if derived:
                removed.append(key)
            continue
        got, summary = _evaluate(db, repo, rows_by_key[key], row, fetched, moment, max_age_d)
        if got is None:
            skip(key, summary)
            if moved:
                stale.append(key)       # kept, and refused by retain planning, until a replacement qualifies
            continue
        found.append({"title": key, **summary})
        same = [(p["anilist_id"], p["ordinal"], p["chain_pos"], p["seed_id"]) for p in derived] == [
            (p["anilist_id"], p["ordinal"], p["chain_pos"], p["seed_id"]) for p in got]
        if same:
            unchanged += 1
            continue
        written.append(key)
        if not dry_run:
            db.execute("DELETE FROM title_parts WHERE library=? AND title=? AND source='relations'", (library, key))
            db.executemany(
                "INSERT INTO title_parts(library, title, anilist_id, selector, source, ordinal, chain_pos, seed_id,"
                " updated_at) VALUES (?,?,?,?,'relations',?,?,?,?)",
                [(library, key, p["anilist_id"], p["selector"], p["ordinal"], p["chain_pos"], p["seed_id"], moment)
                 for p in got])
    if not dry_run:
        for key in sorted(set(removed)):
            if key not in written:
                db.execute("DELETE FROM title_parts WHERE library=? AND title=? AND source='relations'",
                           (library, key))
    return {"library": library, "dry_run": dry_run, "titles": len(media & set(rows_by_key)),
            "discovered": len(found), "written": sorted(written), "unchanged": unchanged,
            "removed": sorted(set(removed) - set(written)), "stale": sorted(stale), "skipped": dict(sorted(skipped.items())),
            "skipped_titles": {r: sorted(k)[:MAX_LISTED] for r, k in sorted(listed.items())},
            "found": found[:MAX_LISTED], "warnings": []}
