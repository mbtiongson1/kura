"""Multi-part titles: which AniList media a file of one library folder belongs to.

One Kura title is one top-level folder. It may hold several AniList media (seasons, cours,
split-cour parts, movies). ``assign`` decides, for each video file, the one part it belongs to
by deterministic rules, or leaves it unassigned with a reason. Pure: no I/O, no state, no
network. Assignment is never stored; every reader recomputes it from the files, the parts and
the media metadata.

Fail-safe by construction: any doubt (a contradiction, a missing count, an ambiguous marker,
numbering that does not land on a season boundary) leaves the file unassigned, and callers
refuse an unassigned episode file under a retain list. Kura never guesses "the third TV entry
is season 3": a season marker must be carried by the part's own titles, or the owner says so.

A part is ``{"anilist_id", "selector", "source", "ordinal", "chain_pos"}``; ``media`` maps an
AniList ID to ``{"titles": [...], "format", "episodes", "status"}`` (every field optional).
"""
import re
import unicodedata

from . import names
from .identity import (MEDIA_EXT, carries_season_strict, explicit_seasons, norm_words,
                       season_markers)

KEYWORD_SELECTORS = ("all", "auto", "rest")
MAX_SELECTOR = 512
MAX_RELPATH = 500
# Formats that are not a season of the main line: matched by file name, never by season marker.
SIDE_FORMATS = ("MOVIE", "OVA", "ONA", "SPECIAL", "TV_SHORT", "MUSIC")
MAX_SAMPLE = 10
_PART_MARKER = re.compile(r"(?i)(?<![a-z])(?:part|cour|cours)[ ._-]*[0-9]")


def _relpath(text: str) -> str:
    value = unicodedata.normalize("NFC", text)
    parts = value.split("/")
    if (not value or len(value) > MAX_RELPATH or value.startswith("/")
            or any(p in ("", ".", "..") for p in parts) or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("selector path must be a relative, '/'-separated path without empty, '.' or '..' parts")
    return value


def canonical_selector(text: str) -> str:
    """The canonical form of a part selector, or ValueError. Shared by the pins parser, the
    database and the CLI so every writer stores the same text.

    ``all`` (whole title), ``auto`` (Kura assigns by the rules), ``rest`` (every file no other
    selector claims), ``season:N`` (0-99), ``ep:A-B`` (1 <= A <= B <= 9999), ``dir:<relpath>``,
    ``file:<relpath>``."""
    if not isinstance(text, str):
        raise ValueError("selector must be text")
    text = text.strip()
    if text in KEYWORD_SELECTORS:
        return text
    kind, sep, value = text.partition(":")
    if sep and kind == "season" and re.fullmatch(r"[0-9]{1,2}", value):
        return f"season:{int(value)}"
    if sep and kind == "ep":
        m = re.fullmatch(r"([0-9]{1,4})-([0-9]{1,4})", value)
        if m and 1 <= int(m[1]) <= int(m[2]) <= 9999:
            return f"ep:{int(m[1])}-{int(m[2])}"
    elif sep and kind in ("dir", "file"):
        return f"{kind}:{_relpath(value)}"
    raise ValueError("selector must be all, auto, rest, season:N, ep:A-B, dir:PATH or file:PATH")


def _kind(selector: str) -> str:
    return selector.partition(":")[0]


def _titles(media: dict, anilist_id: int) -> list[str]:
    return list((media.get(anilist_id) or {}).get("titles") or [])


def _tv_like(part: dict, media: dict) -> bool:
    return (media.get(part["anilist_id"]) or {}).get("format") not in SIDE_FORMATS


def season_part(season: int, parts: list[dict], media: dict) -> tuple[list[int], str | None]:
    """``(part IDs, reason)`` for a season number among ``parts``: one ID when decisive, several
    when more than one part carries it (a split cour), none with a reason otherwise. Never
    positional: a part carries a season only through its own titles or (season 1) the absence of
    any later-season marker on the first main-line entry."""
    tv = [p for p in parts if _tv_like(p, media)]
    if season >= 2:
        ids = sorted(p["anilist_id"] for p in tv
                     if any(carries_season_strict(t, season) for t in _titles(media, p["anilist_id"])))
    elif season == 1:
        first = [p for p in tv if p.get("chain_pos") == 1]
        pool = first or tv
        ids = sorted(p["anilist_id"] for p in pool
                     if not any(explicit_seasons(t) - {1} or any(carries_season_strict(t, n) for n in range(2, 30))
                                for t in _titles(media, p["anilist_id"])))
    else:
        return [], "season_unmatched"
    return ids, (None if ids else "season_unmatched")


def _order(parts: list[dict]) -> list[dict]:
    return sorted(parts, key=lambda p: (p.get("chain_pos") is None, p.get("chain_pos") or 0,
                                        p.get("ordinal", 0), p["anilist_id"]))


def cumulative(episodes: dict[str, tuple[int, int | None]], parts: list[dict], media: dict
               ) -> tuple[dict[str, tuple[int, int]], str | None]:
    """Map absolute episode numbers onto consecutive parts. ``episodes`` is ``{path: (episode,
    version)}`` for the non-extra videos in scope; ``parts`` are tried in main-line order.

    Returns ``({path: (part ID, episode within the part)}, None)`` or ``({}, reason)``. All of
    these must hold, else nothing in scope is assigned: numbers are unique per episode (v1/v2 of
    one episode collapse), contiguous from 1 to M, the parts form a finished run with known
    episode counts, and M lands exactly on a cumulative boundary."""
    if not episodes:
        return {}, "no_marker"
    by_number: dict[int, list[tuple[str, int | None]]] = {}
    for path, (number, version) in episodes.items():
        by_number.setdefault(number, []).append((path, version))
    for copies in by_number.values():
        versions = [v for _, v in copies]
        if len(copies) > 1 and (len(set(versions)) != len(versions)):
            return {}, "absolute_mismatch"          # the same number twice: two seasons numbered alike
    top = max(by_number)
    if sorted(by_number) != list(range(1, top + 1)):
        return {}, "absolute_mismatch"
    run = [p for p in _order(parts) if _tv_like(p, media)]
    if not run:
        return {}, "absolute_mismatch"
    bounds, total = [], 0
    for index, part in enumerate(run):
        info = media.get(part["anilist_id"]) or {}
        count = info.get("episodes")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return {}, "count_unknown"
        if info.get("status") not in ("FINISHED", "RELEASING") or (
                info.get("status") == "RELEASING" and index != len(run) - 1):
            return {}, "absolute_mismatch"
        total += count
        bounds.append((part["anilist_id"], total - count, total))
        if top == total:
            break
    else:
        return {}, "absolute_mismatch"          # ran out of parts before the numbering ended on a boundary
    out = {}
    for number, copies in by_number.items():
        part_id, offset, _ = next(b for b in bounds if number <= b[2])
        for path, _version in copies:
            out[path] = (part_id, number - offset)
    return out, None


def _stem_words(path: str) -> set[str]:
    return set(norm_words(re.sub(r"\.[A-Za-z0-9]{2,4}$", "", path.rsplit("/", 1)[-1])).split())


def _name_part(words: set[str], parts: list[dict], media: dict) -> tuple[int | None, str]:
    """The one part whose title (or synonym) words are all present in ``words``, preferring the
    longest title; a tie between different parts is ambiguous."""
    best: dict[int, int] = {}
    for part in parts:
        for title in _titles(media, part["anilist_id"]):
            tokens = set(norm_words(title).split())
            if tokens and tokens <= words:
                best[part["anilist_id"]] = max(best.get(part["anilist_id"], 0), len(tokens))
    if not best:
        return None, "no_match"
    top = max(best.values())
    winners = sorted(i for i, n in best.items() if n == top)
    return (winners[0], "") if len(winners) == 1 else (None, "name_ambiguous")


def _dir_name_part(component: str, parts: list[dict], media: dict) -> int | None:
    """A directory that names exactly one part: its words equal one title's words, or are
    contained in one part's titles only. A folder that every part contains (the franchise name)
    decides nothing."""
    wanted = set(norm_words(component).split())
    if not wanted:
        return None
    hits = set()
    for part in parts:
        if any(wanted <= set(norm_words(t).split()) for t in _titles(media, part["anilist_id"])):
            hits.add(part["anilist_id"])
    return next(iter(hits)) if len(hits) == 1 else None


def _owner_match(selector: str, path: str, season, episode, doubtful=False) -> tuple | None:
    """Specificity key when an owner selector claims ``path``, else None. file > deeper dir >
    shallower dir > season > ep > rest. ``doubtful`` (contradictory season markers in the path)
    leaves only the path selectors (file, dir) in force."""
    kind, _, value = selector.partition(":")
    if kind == "file":
        return (5, 0) if path == value else None
    if kind == "dir":
        return (4, value.count("/") + 1) if path.startswith(value + "/") else None
    if doubtful:
        return None
    if kind == "season":
        return (3, 0) if season is not None and season == int(value) else None
    if kind == "ep":
        low, high = (int(x) for x in value.split("-"))
        return (2, 0) if episode is not None and low <= episode <= high else None
    return (1, 0) if selector == "rest" else None


def assign(files: list[str], parts: list[dict], media: dict) -> dict:
    """Assign each video file (local paths relative to the title) to a part.

    Returns ``{"files": {path: {"part": id|None, "rule": str, "reason": str|None}},
    "assigned_video", "unassigned_video", "unassigned_sample", "reasons", "complete"}``.
    Non-video files are ignored. ``complete`` is true when no non-extra video is unassigned;
    unassigned extras (a root-level NCOP) never block it. Deterministic under file order."""
    paths = sorted({f for f in files if f.lower().endswith(MEDIA_EXT)})
    parts = _order(parts)
    result: dict[str, dict] = {}

    def put(path, part, rule, reason=None):
        result[path] = {"part": part, "rule": rule, "reason": reason}

    if len(parts) == 1 and parts[0]["selector"] in ("all", "auto", "rest"):
        for path in paths:
            put(path, parts[0]["anilist_id"], "single")
        return _summary(result, paths)
    info = {p: names.parse(p) for p in paths}
    auto = [p for p in parts if p["selector"] in ("auto", "all") or p["source"] == "relations"]
    if len(parts) == 1:
        auto = []
    explicit = [p for p in parts if p not in auto]
    pending = []
    for path in paths:
        parsed = info[path]
        season, doubtful = season_markers(path.split("/"), episode_marker=True)
        if explicit:
            hits = []
            for part in explicit:
                key = _owner_match(part["selector"], path, season, None if parsed["extra"] else parsed["episode"],
                                   doubtful)
                if key:
                    hits.append((key, part["anilist_id"]))
            if hits:
                top = max(k for k, _ in hits)
                ids = sorted({i for k, i in hits if k == top})
                if len(ids) == 1:
                    put(path, ids[0], "owner_" + {5: "file", 4: "dir", 3: "season", 2: "ep", 1: "rest"}[top[0]])
                else:
                    put(path, None, "owner", "selector_conflict")
                continue
        if not auto:
            put(path, None, "owner", "no_selector")
            continue
        pending.append(path)
    _by_rules(pending, info, auto, media, result, put)
    return _summary(result, paths, info)


def _marker_part(components, auto, media):
    """``(part id | ids, rule, None)`` from the season markers of every path component, or
    ``(None, rule, reason)`` when they are ambiguous or contradict each other, or
    ``(None, None, None)`` when the path carries no marker. The deepest directory marker names
    the rule; every component is checked, so an outer directory cannot be overridden silently."""
    last = len(components) - 1
    found = []
    for depth, component in enumerate(components):
        season, bad = season_markers([component], episode_marker=depth == last)
        if bad:
            return None, "dir_season" if depth < last else "file_season", "dir_ambiguous"
        if season is not None:
            found.append((depth, season))
    if not found:
        return None, None, None
    if len({s for _, s in found}) > 1:
        return None, "marker", "marker_conflict"
    depth, season = found[-1]
    rule = "dir_season" if depth < last else "file_season"
    ids, reason = season_part(season, auto, media)
    if reason:
        return None, rule, reason
    return (ids[0], rule, None) if len(ids) == 1 else (ids, rule, None)


def _by_rules(pending, info, auto, media, result, put):
    undecided, grouped = [], {}
    for path in pending:
        parsed, comps = info[path], path.split("/")
        found, rule, reason = _marker_part(comps, auto, media)
        if reason:
            put(path, None, rule, reason)
            continue
        named = None
        for component in reversed(comps[:-1]):              # a directory that names exactly one part
            season, bad = season_markers([component])
            if season is None and not bad and not names.parse(component + "/x.mkv")["extra"]:
                named = _dir_name_part(component, auto, media)
                if named:
                    break
        if named and found is not None:
            if named == found or (isinstance(found, list) and named in found):
                found = named
            else:
                put(path, None, "marker", "marker_conflict")      # name and season evidence disagree
                continue
        elif named:
            found, rule = named, "dir_name"
        if isinstance(found, list):
            grouped.setdefault(tuple(found), []).append(path)       # a split cour: decide by numbering
        elif found is not None:
            put(path, found, rule)
        elif parsed["extra"]:
            put(path, None, "extra", "no_marker")
        else:
            undecided.append(path)
    for ids, members in sorted(grouped.items()):
        done, reason = cumulative({p: (info[p]["episode"], info[p]["version"]) for p in members
                                   if info[p]["episode"] is not None and not info[p]["extra"]},
                                  [p for p in auto if p["anilist_id"] in ids], media)
        for path in members:
            if _PART_MARKER.search(path):
                put(path, None, "cumulative", "part_marker")      # "Part 2" restarts numbering: not read here
            elif path in done:
                put(path, done[path][0], "cumulative")
            else:
                put(path, None, "cumulative", reason or "no_marker")
    numbered = {p: (info[p]["episode"], info[p]["version"]) for p in undecided
                if info[p]["episode"] is not None and not info[p]["extra"]}
    decided_numbered = [p for p in info if p in result and result[p]["part"] is not None
                        and info[p]["episode"] is not None and not info[p]["extra"]]
    done, reason = cumulative(numbered, auto, media) if numbered and not decided_numbered else ({}, None)
    if numbered and decided_numbered:
        reason = "absolute_mismatch"        # numbering is only trusted when it is the sole evidence
    for path in numbered:
        if path in done:
            put(path, done[path][0], "cumulative")
        else:
            put(path, None, "cumulative", reason)
    undecided = [p for p in undecided if p not in numbered]
    for path in undecided:
        parsed = info[path]
        if parsed["episode"] is None:
            side = [p for p in auto if not _tv_like(p, media)] + [p for p in auto if _tv_like(p, media)]
            chosen, why = _name_part(_stem_words(path), side, media)
            if chosen is not None:
                put(path, chosen, "title_match")
            else:
                put(path, None, "title_match", why)
        else:
            put(path, None, "no_marker", "no_marker")


def _summary(result, paths, info=None):
    unassigned = [p for p in paths if result[p]["part"] is None]
    blocking = [p for p in unassigned if not (info and info[p]["extra"])]
    reasons: dict[str, int] = {}
    for path in blocking:
        reason = result[path]["reason"] or "unassigned"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {"files": {p: result[p] for p in paths}, "assigned_video": len(paths) - len(unassigned),
            "unassigned_video": len(blocking), "unassigned_sample": blocking[:MAX_SAMPLE],
            "reasons": dict(sorted(reasons.items())), "complete": not blocking}


# ---- stored parts, the effective view and retain decisions ------------------------------

def load_parts(db, library: str) -> dict[str, list[dict]]:
    """Stored parts by title key, in display order. A malformed row is skipped here and the
    title then reads as ``parts_stale`` or single, never as a part set that was not recorded."""
    out: dict[str, list[dict]] = {}
    for title, anilist_id, selector, source, ordinal, chain_pos, seed_id in db.execute(
            "SELECT title, anilist_id, selector, source, ordinal, chain_pos, seed_id FROM title_parts"
            " WHERE library=? ORDER BY title, ordinal, anilist_id", (library,)):
        out.setdefault(title, []).append({"anilist_id": anilist_id, "selector": selector, "source": source,
                                          "ordinal": ordinal, "chain_pos": chain_pos, "seed_id": seed_id})
    return out


def media_info(db, ids) -> dict[int, dict]:
    """``{id: {"titles", "format", "episodes", "status"}}`` from the cached AniList media rows."""
    import json
    ids = sorted(set(ids))
    out = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for (i, romaji, english, native, synonyms, fmt, episodes, status) in db.execute(
                "SELECT anilist_id, title_romaji, title_english, title_native, synonyms, format, episodes, status"
                f" FROM anilist_media WHERE anilist_id IN ({','.join('?' * len(chunk))})", chunk):
            try:
                extra = [s for s in json.loads(synonyms) if isinstance(s, str)]
            except ValueError:
                extra = []
            out[i] = {"titles": [t for t in (romaji, english, native, *extra) if t], "format": fmt,
                      "episodes": episodes, "status": status}
    return out


def effective(db, library: str) -> dict[str, dict]:
    """The identity of each title as retain planning must read it, by title key.

    Each view has ``mode`` (``single``, ``owner`` or ``auto``), ``status`` (the identity row's
    status; ``parted`` for a multi-part title that can be trusted; ``parts_stale`` when
    derived parts no longer match the recorded identity), ``anilist_id``, ``candidates``,
    ``parts`` and ``row`` (the recorded identity row, or None).

    * ``single``: no part rows; the identity row, unchanged.
    * ``owner``: owner part rows are the whole part set, whatever else is recorded. The owner
      stated it, so no candidates are consulted.
    * ``auto``: parts derived from AniList relations. Trusted only while the identity row has
      not moved to a different resolved ID than the one the parts were derived from.
    """
    from . import identity
    rows = identity.load(db, library)
    stored = load_parts(db, library)
    views = {}
    for key in sorted(set(rows) | set(stored)):
        row, parts = rows.get(key), stored.get(key, [])
        owner = [p for p in parts if p["source"] == "owner"]
        derived = [p for p in parts if p["source"] == "relations"]
        if owner:
            views[key] = {"mode": "owner", "status": "parted", "anilist_id": owner[0]["anilist_id"],
                          "candidates": [], "parts": owner, "row": row}
        elif derived:
            seeds = {p["seed_id"] for p in derived}
            moved = (row is not None and row["status"] in identity.IDENTIFIED
                     and seeds != {row["anilist_id"]})
            views[key] = {"mode": "auto", "status": "parts_stale" if moved or row is None else "parted",
                          "anilist_id": derived[0]["anilist_id"],
                          "candidates": list(row["candidates"]) if row else [], "parts": derived, "row": row}
        else:
            views[key] = {"mode": "single", "status": row["status"], "anilist_id": row["anilist_id"],
                          "candidates": row["candidates"], "parts": [], "row": row}
    return views


def retain_reasons(db, key: str, views: dict[str, dict], retain, files) -> list[str]:
    """Why a title must not be proposed under a retain list (empty when it may be).

    ``files`` is a callable returning the title's local file paths, read only for multi-part
    titles. Single-part titles go through ``identity.retain_reasons`` unchanged. A parted title
    is refused ``identity_unknown`` unless trusted; ``retained`` when a part that owns a file
    (auto) or any declared part (owner) is listed; ``retain_possible`` for a listed candidate
    or (auto) a listed relative that owns no file; and ``parts_incomplete`` when an episode
    file belongs to no part, because then the owner of that file is unknown."""
    from . import identity
    view = views.get(key)
    if view is None or view["mode"] == "single":
        return identity.retain_reasons(key, {key: view["row"]} if view and view["row"] else {}, retain)
    if view["status"] != "parted":
        return ["identity_unknown"]
    result = assign(files(), view["parts"], media_info(db, [p["anilist_id"] for p in view["parts"]]))
    declared = {p["anilist_id"] for p in view["parts"]}
    owning = {v["part"] for v in result["files"].values() if v["part"] is not None}
    listed = lambda ids: any(i in retain for i in ids)
    reasons = []
    if listed(declared if view["mode"] == "owner" else owning):
        reasons.append("retained")
    if listed(view["candidates"]) or (view["mode"] == "auto" and listed(declared - owning)):
        reasons.append("retain_possible")
    if not result["complete"]:
        reasons.append("parts_incomplete")
    return reasons
