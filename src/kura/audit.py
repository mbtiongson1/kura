"""Library audit: one offline report that joins everything Kura knows about each title.

State only. ``run`` reads the manifest inventory, links, title identities, probe results, the
SeaDex mirror and the AniList list snapshot from the private state database, applies the owner's
retain list and policy overrides, and returns (and by default stores) a report of flags and
suggestions. It opens no library file, no network connection and no descriptor; the only write
is the report row in ``audit_reports`` (``store_report=False`` skips it). Everything in it is a
proposal: nothing is fetched, moved or deleted.

Determinism: titles, files, groups and flags are all sorted, so the same state, inputs and clock
produce the same ``report_sha256`` (sha256 over the canonical JSON without ``generated_at`` and
without the hash itself). Stale inputs (SeaDex older than 7 days, AniList list older than 48
hours) are reported in ``inputs`` and never stop the audit.

Calibration notes (encoder settings come from the x265/x264 SEI text, see ``mkvprobe``):

* Settings are a flat option to value mapping; x265 keys are hyphenated (``aq-mode``), x264 keys
  use underscores (``psy_rd``). ``rc`` is ``crf``, ``abr`` or ``2pass``.
* A web stream that is the streaming service's own encode (2-pass or ABR with ``bframes=0``, or
  no settings at all on a file named WEB-DL) is labelled ``untouched_stream`` and is never
  judged by CRF: the bitrate belongs to the service, not to a re-encoder.
* The CRF band (x265, 10-bit, 18 to 24) is informational only and never raises a flag.
* ``writing_app`` is ignored: remuxers rewrite it (``Lavf...``, ``no_variable_data``).
"""
import hashlib
import json
import math
import re
import statistics
import time

from . import idlist, identity, manifest, mkvprobe, names, policy, titleparts
from . import probe as probe_mod
from .manifest_store import ManifestRepo, canonical
from .models import KuraError
from .sources import seadex

MB = 1_000_000
FLAGS = ("mixed_groups", "superseded_version_present", "v2_known_upstream", "low_bpp_8bit",
         "ass_missing_fonts", "seadex_best_differs", "seadex_alt_held", "seadex_no_public_pick",
         "seadex_best_outside_band", "size_above_band", "size_below_band", "identity_unknown",
         "probe_missing", "unsupported_container", "retained", "part_unassigned", "parts_pending")
SEVERITIES = ("info", "warn", "action")
SUGGESTIONS = ("none", "review", "upgrade_candidate")
BPP_LOW, BPP_ACTION = 0.06, 0.035
ANILIST_STALE = 48 * 3600
MAX_LIMIT = 10_000
KEEP_REPORTS = 30
MAX_LIST = 10
CRF_BAND = (18.0, 24.0)
USABLE_STATUSES = ("ok", "partial", "unsupported_container")      # "invalid" says nothing about the file
ASS_PARTIAL = {"attachments_truncated", "budget_exhausted", "truncated", "element_limit"}
SIDECAR_FONT_DIRS = ("fonts", "font")


# ---- small helpers ------------------------------------------------------------------------

def _norm(group) -> str:
    """Comparison key of a release group: lowercase alphanumerics only ('' when unknown)."""
    return re.sub(r"[^0-9a-z]", "", group.casefold()) if isinstance(group, str) else ""


def _mb(size) -> float:
    return round(size / MB, 1)


def _num(value):
    if not isinstance(value, str):
        return None
    try:
        x = float(value)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _lead(value):
    """First number of an ``a:b:c`` option value."""
    return _num(value.split(":", 1)[0]) if isinstance(value, str) else None


def _json(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bad(message: str, **details):
    return KuraError("invalid_arguments", message, **details)


def check_args(library, titles, flags, limit, retain, overrides):
    if not isinstance(library, str) or not library:
        raise _bad("A library label is required")
    if titles is not None and (not isinstance(titles, (list, tuple))
                               or not all(isinstance(t, str) and t for t in titles)):
        raise _bad("titles must be non-empty title keys")
    if flags is not None and (not isinstance(flags, (list, tuple)) or any(f not in FLAGS for f in flags)):
        raise _bad("Unknown flag code", allowed=list(FLAGS))
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise _bad(f"limit must be 1..{MAX_LIMIT}")
    for value in (retain, overrides):
        if value is not None and not isinstance(value, idlist.IdList):
            raise _bad("retain and overrides must be parsed ID lists")


# ---- encoder settings ---------------------------------------------------------------------

def settings_summary(library, settings) -> dict:
    """The few options worth reading per title: crf, rc, aq-mode, psy-rd and the SAO mode."""
    s = settings if isinstance(settings, dict) else {}

    def pick(*keys):
        return next((s[k] for k in keys if isinstance(s.get(k), str)), None)

    sao = None
    if library == "x265":
        if "no-sao" in s:
            sao = "off"
        elif "limit-sao" in s:
            sao = "limit"
        elif (_num(s.get("selective-sao")) or 0) > 0:
            sao = "selective"
        elif "sao" in s:
            sao = "on"
    aq = _lead(pick("aq-mode", "aq"))
    return {"crf": _num(pick("crf")), "rc": pick("rc"),
            "aq-mode": int(aq) if aq is not None and aq == int(aq) else aq,
            "psy-rd": _lead(pick("psy-rd", "psy_rd")), "sao": sao}


def encoding_style(settings, source) -> str:
    """``crf``, ``bitrate``, ``untouched_stream`` or ``unknown`` for one file."""
    if isinstance(settings, dict) and settings:
        rc, crf, bframes = settings.get("rc"), settings.get("crf"), settings.get("bframes")
        if rc in ("2pass", "abr") or (rc is None and crf is None and "bitrate" in settings):
            return "untouched_stream" if bframes == "0" else "bitrate"
        if rc == "crf" or (rc is None and crf is not None):
            return "crf"
        return "unknown"
    return "untouched_stream" if source == "WEB-DL" else "unknown"


def crf_band(library, bit_depth, style, crf):
    """Informational only: where an x265 10-bit CRF sits against the efficient 18 to 24 range."""
    if library != "x265" or bit_depth != 10 or style != "crf" or crf is None:
        return None
    return "below" if crf < CRF_BAND[0] else "above" if crf > CRF_BAND[1] else "within"


def bits_per_pixel(video):
    if not isinstance(video, dict):
        return None
    kbps, w, h, fps = (video.get(k) for k in ("bitrate_kbps", "width", "height", "fps"))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in (kbps, w, h, fps)):
        return None
    return round(kbps * 1000 / (w * h * fps), 4)


def _sample(path, result) -> dict:
    """Everything one probed file contributes, in report shape."""
    video = result.get("video") if isinstance(result.get("video"), dict) else None
    enc = result.get("encoder") if isinstance(result.get("encoder"), dict) else None
    parsed = names.parse(path)
    library = enc.get("library") if enc else None
    settings = enc.get("settings") if enc else None
    style = encoding_style(settings, parsed["source"]) if video else None
    summary = settings_summary(library, settings)
    depth = video.get("bit_depth") if video else None
    band = crf_band(library, depth, style, summary["crf"])
    notes = []
    if style == "untouched_stream":
        notes.append("untouched_stream_video")
    if band:
        notes.append("crf_" + band + "_band")
    if video and not enc and style != "untouched_stream":
        notes.append("no_encoder_settings")
    subs = [s for s in (result.get("subtitles") or []) if isinstance(s, dict)]
    attach = result.get("attachments") if isinstance(result.get("attachments"), dict) else {}
    warnings = set(result.get("warnings") or [])
    return {
        "file": path.rsplit("/", 1)[-1], "group": parsed["group"], "status": result.get("status"),
        "container": result.get("container"), "bpp": bits_per_pixel(video),
        "video": ({k: video.get(k) for k in ("codec", "profile", "bit_depth", "width", "height", "fps",
                                             "bitrate_kbps", "bitrate_source")} if video else None),
        "encoder": ({"library": library, "version": enc.get("version") if enc else None, "style": style,
                     "crf_band": band, "notes": notes} if video else None),
        "settings": ({**summary, "bit_depth": depth, "bitrate_kbps": video.get("bitrate_kbps")}
                     if video else None),
        "audio": [{"codec": a.get("codec"), "channels": a.get("channels"), "lang": a.get("lang")}
                  for a in (result.get("audio") or [])[:8] if isinstance(a, dict)],
        "subtitles": [{"format": s.get("format"), "lang": s.get("lang")} for s in subs[:12]],
        "fonts": attach.get("fonts") if isinstance(attach.get("fonts"), int) else 0,
        "chapters": result.get("chapters") if isinstance(result.get("chapters"), dict) else None,
        "has_ass": any(s.get("format") == "ass" for s in subs),
        "complete_attachments": not (warnings & ASS_PARTIAL),
    }


# ---- per-title pieces ---------------------------------------------------------------------

def _files(repo, title) -> list[dict]:
    prefix = title["rel_path"].partition("/")[2] + "/"
    out = []
    for f in repo.title_files(title["id"]):
        rel = f["rel_path"]
        local = rel[len(prefix):] if title["kind"] == "dir" and rel.startswith(prefix) else rel.rsplit("/", 1)[-1]
        parsed = names.parse(local)
        out.append({"rel": rel, "local": local, "size": f["size"], "mtime_ns": f["mtime_ns"],
                    "video": local.lower().endswith(probe_mod.VIDEO_EXT) and not local.lower().endswith(".mka"),
                    "n": parsed})
    return out


def _stats(files):
    videos = [f for f in files if f["video"] and not f["n"]["extra"]]
    sizes = [f["size"] for f in videos]
    per = ({"median": _mb(statistics.median(sizes)), "min": _mb(min(sizes)), "max": _mb(max(sizes))}
           if sizes else {"median": None, "min": None, "max": None})
    doc = {"video": len(videos), "extras": sum(1 for f in files if f["video"] and f["n"]["extra"]),
           "bytes": sum(sizes), "per_episode_mb": per}
    groups: dict[str, list] = {}
    for f in videos:
        g = f["n"]["group"]
        groups.setdefault(_norm(g), [g, 0])[1] += 1
    listed = sorted(groups.values(), key=lambda v: (-v[1], v[0] or ""))
    return videos, doc, [{"group": g, "files": n} for g, n in listed]


def _probe_rows(db, title, files):
    """``(usable rows by path, changed count)``: rows that still describe the inventoried file."""
    by_path = {f["rel"]: f for f in files}
    usable, changed = {}, 0
    for path, size, mtime, version, status, text in db.execute(
            "SELECT rel_path, size, mtime_ns, probe_version, status, result FROM probe_results"
            " WHERE title_id=? ORDER BY rel_path", (title["id"],)):
        f = by_path.get(path)
        if f is None or status == "changed" or (f["size"], f["mtime_ns"]) != (size, mtime):
            changed += 1
            continue
        result = _json(text)
        if status in USABLE_STATUSES and version == mkvprobe.PROBE_VERSION and result is not None:
            usable[path] = (status, result)
    return usable, changed


def _representative(samples, video_files):
    """The sample from the biggest signature cluster of the title (ties: first path)."""
    sizes: dict = {}
    for f in video_files:
        sig = probe_mod.signature(f["rel"])
        sizes[sig] = sizes.get(sig, 0) + 1
    return min(samples, key=lambda s: (-sizes.get(probe_mod.signature(s["path"]), 0), s["path"]))


def _held(db, repo, title, videos):
    groups = {_norm(f["n"]["group"]): f["n"]["group"] for f in videos if f["n"]["group"]}
    groups.pop("", None)
    hashes = {link["infohash"] for link in repo.links(title["id"]) if link["strategy"] != manifest.SUPERSEDED}
    for infohash in hashes:
        row = db.execute("SELECT release_group FROM manifest_torrents WHERE infohash=?", (infohash,)).fetchone()
        if row and _norm(row[0]):
            groups.setdefault(_norm(row[0]), row[0])
    return set(groups), hashes


def _seadex_rows(db, anilist_id):
    entries = db.execute("SELECT id, incomplete FROM seadex_entries WHERE anilist_id=? ORDER BY updated DESC, id",
                         (anilist_id,)).fetchall()
    if not entries:
        return None, []
    rows = db.execute(
        "SELECT DISTINCT t.id, t.release_group, t.infohash, t.public, t.is_best, t.dual_audio, t.files,"
        " t.median_video_bytes, t.video_files FROM seadex_entries e"
        " JOIN seadex_entry_torrents x ON x.entry_id=e.id JOIN seadex_torrents t ON t.id=x.torrent_id"
        " WHERE e.anilist_id=? ORDER BY t.is_best DESC, t.release_group, t.id", (anilist_id,)).fetchall()
    torrents = [{"id": r[0], "group": r[1], "infohash": r[2], "public": bool(r[3]), "best": bool(r[4]),
                 "dual": bool(r[5]), "files": r[6], "median": r[7], "videos": r[8]} for r in rows]
    return entries[0], torrents


def _pick(t) -> dict:
    out = {"release_group": t["group"], "public": t["public"], "dual_audio": t["dual"],
           "median_video_mb": _mb(t["median"]) if t["median"] is not None else None,
           "video_files": t["videos"]}
    if t["public"]:
        out["infohash"] = t["infohash"]
    return out


def _held_matches(torrents, held_groups, held_hashes) -> str:
    if not torrents or not (held_groups or held_hashes):
        return "unknown"

    def hit(t):
        return (t["public"] and t["infohash"] in held_hashes) or (_norm(t["group"]) in held_groups
                                                                 and _norm(t["group"]) != "")
    if any(hit(t) for t in torrents if t["best"]):
        return "best"
    return "alt" if any(hit(t) for t in torrents if not t["best"]) else "none"


def _upstream_revisions(torrents, videos):
    """Episodes of held v1 files for which a public SeaDex file list holds a newer version."""
    held = {}
    for f in videos:
        n = f["n"]
        if n["episode"] is not None and n["group"] and (n["version"] or 1) < 2:
            held.setdefault((_norm(n["group"]), _season(f), n["episode"]), True)
    if not held:
        return []
    found = set()
    for t in torrents:
        listed = None
        try:
            listed = json.loads(t["files"]) if t["public"] and t["files"] else None
        except ValueError:
            pass
        for item in listed if isinstance(listed, list) else []:
            if not (isinstance(item, list) and item and isinstance(item[0], str)):
                continue
            name = item[0]
            if not name.lower().endswith(probe_mod.VIDEO_EXT):
                continue
            n = names.parse(name)
            group = _norm(n["group"]) or _norm(t["group"])
            if n["extra"] or n["episode"] is None or (n["version"] or 1) < 2:
                continue
            # A name without a season matches any season, on either side.
            season = _season({"n": n, "local": name})
            if any(h[0] == group and h[2] == n["episode"] and (None in (h[1], season) or h[1] == season)
                   for h in held):
                found.add(n["episode"])
    return sorted(found)


_SEASON_DIR = re.compile(r"(?:season|s)[ ._-]?0*(\d{1,2})", re.I)


def _season(f):
    """Season of a file: from SxxExx in its name, else a 'Season N' / 'SN' directory, else None."""
    if f["n"]["season"] is not None:
        return f["n"]["season"]
    for part in reversed(f["local"].split("/")[:-1]):
        m = _SEASON_DIR.fullmatch(part.strip())
        if m:
            return int(m.group(1))
    return None


def _superseded(videos):
    held = [(_norm(f["n"]["group"]), _season(f), f["n"]["episode"], f["n"]["version"] or 1)
            for f in videos if f["n"]["episode"] is not None]
    found = set()
    for g, s, ep, v in held:
        if v < 2:
            continue
        # A missing season on either side matches any season (same rule as upstream revisions).
        if any(g2 == g and ep2 == ep and v2 < 2 and (None in (s, s2) or s == s2) for g2, s2, ep2, v2 in held):
            found.add(ep)
    return sorted(found)


# ---- one title ----------------------------------------------------------------------------

class _Flags:
    def __init__(self):
        self.items = []

    def add(self, code, severity, **detail):
        self.items.append({"code": code, "severity": severity, "detail": detail})

    def done(self):
        return sorted(self.items, key=lambda f: FLAGS.index(f["code"]))


def _seadex(ctx, title, anilist_id, pol, videos, flags):
    """SeaDex comparison for one AniList entry against the files in ``videos``: ``(summary, upgrade)``;
    adds its flags to ``flags``. ``anilist_id`` None (unidentified) gives the empty summary."""
    db, repo = ctx["db"], ctx["repo"]
    known = anilist_id is not None
    sd = {"entry": None, "incomplete": None, "best": [], "alt": [], "held_matches": "unknown"}
    upgrade = False
    if known:
        entry, torrents = _seadex_rows(db, anilist_id)
        if entry is not None:
            held_groups, held_hashes = _held(db, repo, title, videos)
            matches = _held_matches(torrents, held_groups, held_hashes)
            best = [t for t in torrents if t["best"]]
            public_best = [t for t in best if t["public"]]
            sd = {"entry": entry[0], "incomplete": bool(entry[1]), "best": [_pick(t) for t in best[:MAX_LIST]],
                  "alt": [_pick(t) for t in torrents if not t["best"]][:MAX_LIST], "held_matches": matches}
            if not public_best:
                flags.add("seadex_no_public_pick", "info", best=len(best))
            if matches == "alt":
                flags.add("seadex_alt_held", "info")
            differs = bool(public_best) and matches in ("none", "alt")
            if differs:
                flags.add("seadex_best_differs", "warn" if pol == "archival" else "info",
                          held_matches=matches, best_groups=sorted({t["group"] or "" for t in public_best})[:MAX_LIST])
            if pol == "archival":
                upgrade = bool(differs)
            else:
                sized = [t for t in public_best if t["median"] is not None]
                inside = [t for t in sized if policy.band_position(t["median"] / MB) == "within"]
                if sized and not inside:
                    flags.add("seadex_best_outside_band", "info",
                              median_mb=sorted(_mb(t["median"]) for t in sized)[0])
                upgrade = bool(inside) and matches in ("none", "alt")
            revised = _upstream_revisions(torrents, videos)
            if revised:
                flags.add("v2_known_upstream", "warn", episodes=revised[:20], count=len(revised))

    return sd, upgrade


def _band_flags(flags, known, pol, file_doc, videos):
    """Size-band flags of an efficient-policy entry, from the median size of its episode files."""
    median = file_doc["per_episode_mb"]["median"]
    episodic = file_doc["video"] >= 2 or any(f["n"]["episode"] is not None for f in videos)
    position = policy.band_position(median) if episodic else None
    if known and pol == "efficient" and position == "above":
        flags.add("size_above_band", "warn", median_mb=median, max_mb=policy.EFFICIENT["max_mb"])
    if known and pol == "efficient" and position == "below":
        flags.add("size_below_band", "warn", median_mb=median, min_mb=policy.EFFICIENT["min_mb"])


def _parts(ctx, title, files, view, flags):
    """Audit each part of a multi-part title on its own files. Returns ``(extra title keys, policy,
    policy reasons, upgrade, seadex summary)`` and adds the parts' flags (``detail.part``) to ``flags``.

    The policy of the title is archival when any part is; ``policy_mixed`` says the parts differ.
    Held SeaDex releases are attributed by group name; a part whose files carry no group name is not
    compared. The title-level ``held_matches`` is the worst over the parts that have a SeaDex entry and
    files (best < alt < none), or ``unknown`` when there is none."""
    db = ctx["db"]
    parts = view["parts"]
    media = titleparts.media_info(db, [p["anilist_id"] for p in parts])
    found = titleparts.assign([f["local"] for f in files], parts, media)
    docs, pols, reasons, upgrade, matches = [], [], [], False, []
    for part in parts:
        pid = part["anilist_id"]
        mine = [f for f in files if found["files"].get(f["local"], {}).get("part") == pid]
        videos, file_doc, groups = _stats(mine)
        pol, why = policy.classify(pid, favourites=ctx["favourites"], score=ctx["scores"].get(pid),
                                   overrides=ctx["overrides"].ids if ctx["overrides"] else None)
        pflags = _Flags()
        known_groups = [g for g in groups if _norm(g["group"])]
        if len(known_groups) >= 2:
            pflags.add("mixed_groups", "warn", groups=[g["group"] for g in known_groups[:MAX_LIST]])
        _band_flags(pflags, True, pol, file_doc, videos)
        sd = {"entry": None, "incomplete": None, "best": [], "alt": [], "held_matches": "unknown"}
        part_upgrade = False
        stale = _superseded(videos)
        if stale:
            pflags.add("superseded_version_present", "action", episodes=stale[:20], count=len(stale))
        if any(f["n"]["group"] for f in videos):       # without a group name the comparison would rest on shared links
            sd, part_upgrade = _seadex(ctx, title, pid, pol, videos, pflags)
        items = pflags.done()
        info = media.get(pid) or {}
        numbers = sorted({f["n"]["episode"] for f in videos if f["n"]["episode"] is not None})
        docs.append({"anilist_id": pid, "title": (info.get("titles") or [None])[0], "format": info.get("format"),
                     "selector": part["selector"], "source": part["source"], "chain_pos": part["chain_pos"],
                     "rule_counts": _tally(found["files"], {f["local"] for f in mine}),
                     "files": file_doc, "groups": groups,
                     "episodes": {"held": len(numbers), "expected": info.get("episodes"),
                                  "local_range": [numbers[0], numbers[-1]] if numbers else None},
                     "policy": pol, "policy_reasons": why,
                     "retained": ctx["retain"] is not None and pid in ctx["retain"],
                     "seadex": sd, "flags": items,
                     "suggestion": ("upgrade_candidate" if part_upgrade else
                                    "review" if any(f["severity"] != "info" for f in items) else "none")})
        for item in items:
            flags.add(item["code"], item["severity"], **{**item["detail"], "part": pid})
        pols.append(pol)
        reasons.append((pol, why))
        upgrade = upgrade or part_upgrade
        if sd["entry"] is not None:
            matches.append(sd["held_matches"])
    if not found["complete"]:
        flags.add("part_unassigned", "warn" if ctx["retain"] is not None else "info",
                  video=found["unassigned_video"], reasons=found["reasons"], sample=found["unassigned_sample"])
    rank = {"best": 0, "alt": 1, "none": 2, "unknown": 3}
    worst = max(matches, key=lambda m: rank.get(m, 3)) if matches else "unknown"
    rollup = {"entry": None, "incomplete": None, "best": [], "alt": [], "held_matches": worst}
    pol = "archival" if "archival" in pols else "efficient"
    why = sorted({r for p, rs in reasons for r in rs if p == pol})        # the reasons of the parts that decide
    return ({"policy_mixed": len(set(pols)) > 1, "parts": docs,
             "unassigned": {"video": found["unassigned_video"], "reasons": found["reasons"],
                            "sample": found["unassigned_sample"]}},
            pol, why, upgrade, rollup)


def _tally(assigned, mine):
    out: dict[str, int] = {}
    for path in sorted(mine):
        rule = assigned[path]["rule"]
        out[rule] = out.get(rule, 0) + 1
    return out


def _audit_title(ctx, title):
    db, repo = ctx["db"], ctx["repo"]
    key = title["rel_path"]
    files = _files(repo, title)
    videos, file_doc, groups = _stats(files)
    ident = ctx["identities"].get(key)
    view = ctx["views"].get(key)
    multi = view is not None and view["mode"] != "single"        # a title with stored parts (see titleparts)
    parted = multi and view["status"] == "parted"
    known = (parted or (not multi and ident is not None and ident["status"] in identity.IDENTIFIED
                        and type(ident["anilist_id"]) is int))
    anilist_id = (view["anilist_id"] if parted else ident["anilist_id"]) if known else None
    pol, reasons = policy.classify(anilist_id, favourites=ctx["favourites"], score=ctx["scores"].get(anilist_id),
                                   overrides=ctx["overrides"].ids if ctx["overrides"] else None)
    flags = _Flags()

    # identity and retain
    status = view["status"] if multi else ident["status"] if ident else "unrecorded"
    media = identity.has_media(title["kind"], key.partition("/")[2], [f["local"] for f in files])
    if not known and status != "not_anime" and media:      # sidecars and the like are not titles
        flags.add("identity_unknown", "warn" if ctx["retain"] is not None else "info", status=status)
    if multi and not parted:
        flags.add("parts_pending", "info", reason=status)
    retained = False
    if ctx["retain"] is not None:
        if multi:
            why = titleparts.retain_reasons(db, key, ctx["views"], ctx["retain"], lambda: [f["local"] for f in files])
        else:
            why = identity.retain_reasons(key, ctx["identities"], ctx["retain"])
        retained = any(r in ("retained", "retain_possible") for r in why)
        if retained:
            flags.add("retained", "info", reason="retained" if "retained" in why else "retain_possible")

    # names: groups, versions, size
    known_groups = [g for g in groups if _norm(g["group"])]
    if len(known_groups) >= 2 and not parted:         # a parted title is judged part by part
        flags.add("mixed_groups", "warn", groups=[g["group"] for g in known_groups[:MAX_LIST]])
    stale_eps = _superseded(videos) if not parted else []        # per part for a parted title
    if stale_eps:
        flags.add("superseded_version_present", "action", episodes=stale_eps[:20], count=len(stale_eps))
    if not parted:
        _band_flags(flags, known, pol, file_doc, videos)

    # probe
    rows, changed = _probe_rows(db, title, files)
    counted = {f["rel"] for f in videos}              # episode videos only: no extras, no audio sidecars
    rows = {path: row for path, row in rows.items() if path in counted}
    samples = []
    for path, (st, result) in sorted(rows.items()):
        samples.append({"path": path, **_sample(path, result)} | {"_status": st})
    unsupported = [s for s in samples if s["_status"] == "unsupported_container"]
    if file_doc["video"] and not rows:
        flags.add("probe_missing", "info", video_files=file_doc["video"])
    if unsupported:
        flags.add("unsupported_container", "info", files=len(unsupported))
    media = [s for s in samples if s["video"]]
    sidecar = any(part.casefold() in SIDECAR_FONT_DIRS for f in files for part in f["local"].split("/")[:-1])
    low = [s for s in media if s["video"]["bit_depth"] == 8 and s["bpp"] is not None and s["bpp"] < BPP_LOW]
    if low:
        reencodes = [s for s in low if s["encoder"]["style"] != "untouched_stream"]
        worst = min(reencodes or low, key=lambda s: (s["bpp"], s["path"]))
        style = worst["encoder"]["style"]
        # The bitrate of an untouched stream belongs to the service, not to a re-encoder: informational.
        severity = "info" if not reencodes else "action" if worst["bpp"] < BPP_ACTION else "warn"
        flags.add("low_bpp_8bit", severity, files=len(low), min_bpp=worst["bpp"],
                  bitrate_kbps=worst["video"]["bitrate_kbps"],
                  bitrate_source=worst["video"]["bitrate_source"], style=style)
    nofonts = [s for s in samples if s["has_ass"] and s["fonts"] == 0 and s["complete_attachments"]]
    if nofonts and not sidecar:
        flags.add("ass_missing_fonts", "warn", files=len(nofonts))

    probe_doc = {"sampled": len(rows), "container": None, "video": None, "encoder": None, "settings": None,
                 "audio": [], "subtitles": [], "fonts": 0, "chapters": None, "samples": []}
    if samples:
        rep = _representative(media or samples, videos)
        probe_doc.update({"container": rep["container"], "video": rep["video"], "encoder": rep["encoder"],
                          "settings": rep["settings"], "audio": rep["audio"], "subtitles": rep["subtitles"],
                          "fonts": rep["fonts"], "chapters": rep["chapters"]})
        probe_doc["samples"] = [{"file": s["file"], "group": s["group"], "bpp": s["bpp"],
                                 "settings": s["settings"],
                                 "style": s["encoder"]["style"] if s["encoder"] else None}
                                for s in samples[:probe_mod.MAX_CLUSTERS * 2]]

    extra_doc = {}
    if parted:
        extra_doc, pol, reasons, upgrade, sd = _parts(ctx, title, files, view, flags)
    else:
        sd, upgrade = _seadex(ctx, title, anilist_id if known else None, pol, videos, flags)

    items = flags.done()
    suggestion = ("upgrade_candidate" if upgrade
                  else "review" if any(f["severity"] != "info" for f in items) else "none")
    doc = {"title": key, "anilist_id": anilist_id,
           "identity": {"status": status, "source": ident["source"] if ident else None,
                        **({"mode": view["mode"]} if multi else {})},
           "policy": pol, "policy_reasons": reasons, "retained": retained, "files": file_doc,
           "groups": groups, "probe": probe_doc, "seadex": sd, "flags": items, "suggestion": suggestion,
           **extra_doc}
    return doc, {"probed": len(rows), "videos": sum(1 for f in files if f["video"]), "changed": changed}


# ---- inputs -------------------------------------------------------------------------------

def _anilist(db, user, moment):
    if user is not None:
        row = db.execute("SELECT user_id, name, list_fetched_at, favourites_fetched_at FROM anilist_users WHERE lower(name)=lower(?)"
                         " ORDER BY user_id LIMIT 1", (user,)).fetchone()
        if row is None:
            raise _bad("That AniList user has no synced list; run kura anilist list first")
    else:
        row = db.execute("SELECT user_id, name, list_fetched_at, favourites_fetched_at FROM anilist_users WHERE is_viewer=1"
                         " ORDER BY user_id LIMIT 1").fetchone()
    if row is None:
        return {"user": None, "fetched_at": None, "favourites_fetched_at": None, "stale": True}, set(), {}
    uid, name, fetched, fav_fetched = row
    # Favourites decide the policy: a set that was never (or no longer) fetched is not trusted.
    favourites = ({r[0] for r in db.execute("SELECT anilist_id FROM anilist_favourites WHERE user_id=?", (uid,))}
                  if fav_fetched is not None else set())
    scores = {r[0]: r[1] for r in db.execute(
        "SELECT anilist_id, score FROM anilist_list_entries WHERE user_id=? AND score IS NOT NULL", (uid,))}
    stale = any(t is None or moment - t > ANILIST_STALE for t in (fetched, fav_fetched))
    return {"user": name, "fetched_at": fetched, "favourites_fetched_at": fav_fetched, "stale": stale}, favourites, scores


def _retain_doc(retain, identities, titles, views=None):
    if retain is None:
        return None
    bound = {identities[t["rel_path"]]["anilist_id"] for t in titles
             if t["rel_path"] in identities and identities[t["rel_path"]]["status"] in identity.IDENTIFIED}
    for t in titles:      # every part of a trusted multi-part title is bound
        view = (views or {}).get(t["rel_path"])
        if view and view["mode"] != "single" and view["status"] == "parted":
            bound |= {p["anilist_id"] for p in view["parts"]}
    return {**retain.summary(), "unmatched_ids": sorted(set(retain.ids) - bound)}


def _digest(report) -> str:
    body = {k: v for k, v in report.items() if k not in ("generated_at", "report_sha256")}
    return hashlib.sha256(canonical(body).encode("ascii")).hexdigest()


def _rank(doc):
    return max((SEVERITIES.index(f["severity"]) for f in doc["flags"]), default=-1)


def run(store, *, library, retain=None, overrides=None, user=None, titles=None, flags=None, limit=200,
        store_report=True, now=time.time) -> dict:
    """Audit `library` from state alone and return the report document. See the module docstring."""
    check_args(library, titles, flags, limit, retain, overrides)
    moment = now()
    db = store.db
    began = not db.in_transaction
    if began:
        db.execute("BEGIN")             # one read snapshot (writers wait on it, up to their busy timeout); the state lock is only needed to store the report
    try:
        repo = ManifestRepo(db)
        every = repo.titles(library)
        if not every:
            raise KuraError("no_inventory", "No inventory for this library; run manifest build first")
        present = [t for t in every if "absent_from_scan" not in (t["blocked"] or [])]   # others are gone from disk
        chosen = present
        if titles:
            known = {t["rel_path"] for t in every}
            for key in titles:
                if key not in known or key not in {t["rel_path"] for t in present}:
                    raise _bad("Unknown title key (or no longer present in the library)", title=key)
            wanted = set(titles)
            chosen = [t for t in chosen if t["rel_path"] in wanted]
        identities = identity.load(db, library)
        views = titleparts.effective(db, library)
        anilist_doc, favourites, scores = _anilist(db, user, moment)
        sd_status = seadex.status(store, now=lambda: moment)
        ctx = {"db": db, "repo": repo, "identities": identities, "views": views, "favourites": favourites, "scores": scores,
               "retain": retain, "overrides": overrides}
        docs, probed, videos, changed = [], 0, 0, 0
        for title in chosen:
            doc, stats = _audit_title(ctx, title)
            docs.append(doc)
            probed += stats["probed"]
            videos += stats["videos"]
            changed += stats["changed"]
        by_flag: dict[str, int] = {}
        for doc in docs:
            for code in {f["code"] for f in doc["flags"]}:       # titles per code, also for parted titles
                by_flag[code] = by_flag.get(code, 0) + 1
        counts = {"titles": len(docs),
                  "flagged": sum(1 for d in docs if any(f["severity"] != "info" for f in d["flags"])),
                  "by_flag": dict(sorted(by_flag.items())),
                  "by_policy": {p: sum(1 for d in docs if d["policy"] == p) for p in policy.POLICIES},
                  "by_suggestion": {s: sum(1 for d in docs if d["suggestion"] == s) for s in SUGGESTIONS}}
        parted = [d for d in docs if "parts" in d]
        if parted:
            counts["parts"] = {"titles": len(parted), "parts": sum(len(d["parts"]) for d in parted),
                               "by_policy": {p: sum(1 for d in parted for x in d["parts"] if x["policy"] == p)
                                             for p in policy.POLICIES},
                               "unassigned_video": sum(d["unassigned"]["video"] for d in parted)}
        shown = [d for d in docs if not flags or any(f["code"] in flags for f in d["flags"])]
        shown.sort(key=lambda d: (-_rank(d), d["title"]))
        report = {
            "library": library, "generated_at": moment,
            "inputs": {
                "inventory_at": max(t["seen_at"] for t in every),
                "probe": {"files_probed": probed, "video_files": videos,
                          "coverage": round(probed / videos, 4) if videos else None, "changed": changed},
                "seadex": {"synced_at": sd_status["last_ok"], "stale": sd_status["stale"]},
                "anilist": anilist_doc,
                "retain": _retain_doc(retain, identities, present, views),
                "policy": {"efficient": dict(policy.EFFICIENT), "archival": dict(policy.ARCHIVAL),
                           "overrides_sha256": overrides.sha256 if overrides else None,
                           "overrides_ids": len(overrides.ids) if overrides else None}},
            "counts": counts, "titles": shown[:limit], "titles_truncated": max(0, len(shown) - limit)}
        report["report_sha256"] = _digest(report)
    finally:
        if began and db.in_transaction:
            db.rollback()
    if store_report:
        with store.lock():
            with db:
                db.execute("INSERT INTO audit_reports(library, created_at, report_sha256, report) VALUES (?,?,?,?)",
                           (library, moment, report["report_sha256"], canonical(report)))
                db.execute("DELETE FROM audit_reports WHERE library=? AND id NOT IN (SELECT id FROM audit_reports"
                           " WHERE library=? ORDER BY id DESC LIMIT ?)", (library, library, KEEP_REPORTS))
    return report
