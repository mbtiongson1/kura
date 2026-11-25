"""Quarantine plan: a *proposal* to move re-acquirable titles aside. Kura never moves anything.

``plan`` reads the manifest in Kura's private state database and writes one JSON document
(also stored with ``ManifestRepo.save_plan``) listing which titles the owner could move to a
quarantine folder and why the rest cannot. Nothing in this module opens, renames, moves or
deletes anything under a library root: it does not even touch the library. The owner's
own infrastructure may execute the plan later, after the owner approved it, and must pass
every precondition recorded in the plan first. Kura never executes, undoes or purges.

A title is proposed only when all of the following hold, otherwise it is *refused* with
fixed reason tokens:
  * its manifest status is ``reacquirable`` (see ``manifest.title_status``)
  * the policy lists were complete, no ``path:`` protection covers it and it was assessed
    under the same policy as the newest build (``policy_changed`` otherwise)
  * the inventory behind it is fresh (``max_inventory_age_h``) and its file evidence is
    complete, on one device and consistent with the stored file rows
  * nothing inside it changed for ``min_quiet_days``
  * the swarm requirement holds: each linked torrent has a recent stored swarm check with
    status ``ok`` and at least ``min_seeders`` seeders, or the caller passed ``skip_swarm``,
    which is recorded in the plan (``swarm_unchecked``) and on every move
  * its root label and title name are plain single path components

``plan`` returns ``{"plan", "plan_id", "stored"}``; ``plan`` is exactly the document that
``plan_hash`` covers and that is stored.

Plan document (``plan_version`` 1). Paths are relative to a root label, never absolute:
  moves   {root, from, to, tree_sha256, file_count, total_bytes, infohashes, verified_at,
           seeders, swarm}
  undo    {root, from, to}: the exact inverse of ``moves``, same order
  refused {title, status, reasons}
``to`` is ``<quarantine_dir>/<batch_id>/<title>`` on the same root, hence the same
filesystem. A plan cannot name its own hash, so ``batch_id`` (first 16 hex of the hash of
everything that decides the moves) names the folder; ``plan_hash`` then covers the whole
document and ``plan_id`` is ``plan_hash[:16]``.
"""
import hashlib
import json
import re
import time
from typing import Callable

from .manifest import show
from .manifest_store import ManifestRepo, canonical
from .models import KuraError

QUARANTINE_DIR = ".kura-quarantine"   # the only name the inventory scanner skips
SUPERSEDED = "superseded"
DAY = 86400
MAX_REFUSED = 200
MIN_QUARANTINE_DAYS = 30   # founder decision: never purge earlier
MAX_TITLES = 10_000
NOTICE = ("Proposal only. Kura never moves, renames, deletes or purges anything: the owner's "
          "own infrastructure may execute this plan after approval, once every precondition "
          "holds.")
PRECONDITIONS = (
    "owner_approval_of_plan_hash",
    "same_device_for_source_and_destination",
    "tree_sha256_matches_fresh_inventory",
    "destination_absent",
    "rename_never_replaces",
    "do_not_follow_symlinks",
    "purge_not_before_respected",
)
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _bad(message: str, **details):
    return KuraError("invalid_arguments", message, **details)


def tree_sha256(files: list[dict]) -> str:
    """Digest of a title's file tree: sorted ``[rel_path, size, mtime_ns]`` triples.

    Device and inode numbers are left out on purpose: they change when the same disk is
    seen from another mount namespace, while path, size and mtime do not.
    """
    rows = []
    for f in files:
        rel, size, mtime = f["rel_path"], f["size"], f["mtime_ns"]
        if not isinstance(rel, str) or not all(type(v) is int for v in (size, mtime)):
            raise _bad("Malformed file entry")
        rows.append([rel, size, mtime])
    return hashlib.sha256(canonical(sorted(rows)).encode("ascii")).hexdigest()


def plan_hash_of(plan: dict) -> str:
    """The hash an executor recomputes: sha256 of the canonical plan minus ``plan_hash``."""
    body = {k: v for k, v in plan.items() if k != "plan_hash"}
    return hashlib.sha256(canonical(body).encode("ascii")).hexdigest()


def _component_ok(value) -> bool:
    return (isinstance(value, str) and value not in ("", ".", "..") and "/" not in value
            and "\\" not in value and "\0" not in value and value == value.strip()
            and not value.startswith(QUARANTINE_DIR))


def check_args(library, titles, quarantine_dir, min_seeders, max_inventory_age_h,
                min_quiet_days, quarantine_days, max_swarm_age_h):
    if not isinstance(library, str) or not library:
        raise _bad("A library name is required")
    if quarantine_dir != QUARANTINE_DIR:
        # Any other name would be scanned as a title by the next inventory.
        raise _bad("Quarantine directory must be exactly " + QUARANTINE_DIR)
    if titles is not None:
        if (not isinstance(titles, list) or len(titles) > MAX_TITLES
                or not all(isinstance(t, str) for t in titles)):
            raise _bad("Titles must be a list of title keys")
    for name, value, low in (("min_seeders", min_seeders, 1), ("min_quiet_days", min_quiet_days, 0),
                             ("max_inventory_age_h", max_inventory_age_h, 0),
                             ("quarantine_days", quarantine_days, MIN_QUARANTINE_DAYS),
                             ("max_swarm_age_h", max_swarm_age_h, 0)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
                or value < low or value > 10**6:
            raise _bad(f"{name} must be a number of at least {low}")
    if not isinstance(min_seeders, int):
        raise _bad("min_seeders must be a whole number")


def _swarm(repo: ManifestRepo, infohashes: list[str], *, now: float, min_seeders: int,
           max_age_h: float) -> tuple[list[str], int | None]:
    """Reasons a swarm requirement fails (empty when met) and the lowest seeder count."""
    reasons, lowest = [], None
    for infohash in infohashes:
        check = repo.latest_swarm(infohash)
        if check is None:
            reasons.append("swarm_unchecked")
        elif check["status"] != "ok" or not isinstance(check["seeders"], int):
            reasons.append("swarm_unavailable")
        elif now - check["checked_at"] > max_age_h * 3600 or check["checked_at"] > now + 300:
            reasons.append("swarm_stale")
        elif check["seeders"] < min_seeders:
            reasons.append("swarm_low_seeders")
        else:
            lowest = check["seeders"] if lowest is None else min(lowest, check["seeders"])
    return sorted(set(reasons)), lowest


def plan(store, *, library: str, titles: list[str] | None = None,
         quarantine_dir: str = QUARANTINE_DIR, min_seeders: int = 3, skip_swarm: bool = False,
         max_inventory_age_h: float = 24, min_quiet_days: float = 14,
         quarantine_days: float = 30, max_swarm_age_h: float = 24,
         now: Callable[[], float] = time.time) -> dict:
    """Propose moves for the eligible titles. Moves nothing; stores the plan when non-empty.

    Returns ``{"plan": <the exact hashed document>, "plan_id", "stored"}``; metadata sits
    outside ``plan`` so ``plan_hash_of(result["plan"]) == result["plan"]["plan_hash"]``.
    """
    check_args(library, titles, quarantine_dir, min_seeders, max_inventory_age_h,
                min_quiet_days, quarantine_days, max_swarm_age_h)
    moment = now()
    created_at = int(moment)
    with store.lock():      # never read a manifest that a build is rewriting
        repo = ManifestRepo(store.db)
        manifest = show(store, library)
        shown = {t["title"]: t for t in manifest["titles"]}
        rows = {r["rel_path"]: r for r in repo.titles(library)}
        wanted = sorted(set(titles)) if titles is not None else sorted(rows)
        moves, refused = [], []
        for key in wanted:
            row, info = rows.get(key), shown.get(key)
            if row is None or info is None:
                refused.append({"title": key, "status": "unknown", "reasons": ["not_in_manifest"]})
                continue
            reasons = _refusals(repo, row, info, moment=moment, policy=manifest["policy_sha256"], min_seeders=min_seeders,
                                skip_swarm=skip_swarm, max_inventory_age_h=max_inventory_age_h,
                                min_quiet_days=min_quiet_days, max_swarm_age_h=max_swarm_age_h)
            if isinstance(reasons, list):
                refused.append({"title": key, "status": info["status"], "reasons": reasons})
            else:
                moves.append(reasons)
        body = _document(library, moves, refused, created_at, quarantine_dir, skip_swarm,
                         {"min_seeders": min_seeders, "min_quiet_days": min_quiet_days,
                          "max_inventory_age_h": max_inventory_age_h,
                          "max_swarm_age_h": max_swarm_age_h, "skip_swarm": bool(skip_swarm)},
                         quarantine_days, manifest["policy_sha256"])
        document = {**body, "plan_hash": plan_hash_of(body)}
        stored = False
        if moves:
            stored = repo.save_plan(document["plan_hash"], canonical(document), created_at)
    return {"plan": document, "plan_id": document["plan_hash"][:16], "stored": stored}


def _refusals(repo, row, info, *, moment, policy, min_seeders, skip_swarm, max_inventory_age_h,
              min_quiet_days, max_swarm_age_h):
    """A list of refusal reasons, or the move dict (without ``to``) when eligible."""
    if info["status"] != "reacquirable":
        return list(info["reasons"]) or ["status_" + info["status"]]
    reasons = []
    root, sep, name = row["rel_path"].partition("/")
    if not sep or not _LABEL.match(root) or not _component_ok(name):
        reasons.append("invalid_path")
    assessment = (row.get("identity") or {}).get("assessment") or {}
    if assessment.get("policy_complete") is not True:
        reasons.append("policy_incomplete")
    if assessment.get("policy_sha256") != policy:
        reasons.append("policy_changed")    # assessed under another policy than the plan claims
    if assessment.get("path_protected") is not False:
        reasons.append("path_protected")
    seen_at = row.get("seen_at")
    if (not isinstance(seen_at, (int, float)) or seen_at > moment + 300
            or moment - seen_at > max_inventory_age_h * 3600):
        reasons.append("inventory_stale")
    files = repo.title_files(row["id"])
    if (not files or row["blocked"] or len(files) != row["file_count"]
            or sum(f["size"] for f in files) != row["total_bytes"]):
        reasons.append("evidence_incomplete")
    if any(f["dev"] != row["root_dev"] for f in files):
        reasons.append("cross_device")
    newest = max([f["mtime_ns"] for f in files] + [row["newest_mtime_ns"]]) if files else 0
    if moment * 1e9 - newest < min_quiet_days * DAY * 1_000_000_000:
        reasons.append("recently_modified")     # also true for an mtime in the future
    links = [link for link in repo.links(row["id"]) if link["strategy"] != SUPERSEDED]
    infohashes = sorted(link["infohash"] for link in links)
    verified = [repo.latest_verification(row["id"], h) for h in infohashes]
    if not infohashes or any(v is None for v in verified):
        reasons.append("not_verified")
    seeders = None
    if not skip_swarm:
        swarm_reasons, seeders = _swarm(repo, infohashes, now=moment,
                                        min_seeders=min_seeders, max_age_h=max_swarm_age_h)
        reasons.extend(swarm_reasons)
    if reasons:
        return sorted(set(reasons))
    return {"root": root, "from": name, "tree_sha256": tree_sha256(files),
            "file_count": len(files), "total_bytes": row["total_bytes"],
            "infohashes": infohashes, "verified_at": min(v["finished_at"] for v in verified),
            "seeders": seeders, "swarm": "unchecked" if skip_swarm else "ok"}


def _document(library, moves, refused, created_at, quarantine_dir, skip_swarm, parameters,
              quarantine_days, policy_sha256) -> dict:
    refused_total = len(refused)
    preconditions = list(PRECONDITIONS)
    preconditions.append("swarm_unchecked_recheck_before_move" if skip_swarm
                         else f"swarm_recheck_at_least_{parameters['min_seeders']}_seeders_before_move")
    if skip_swarm:
        preconditions.append("swarm_unchecked")
    body = {
        "plan_version": 1,
        "notice": NOTICE,
        "created_at": created_at,
        "purge_not_before": created_at + int(quarantine_days * DAY),
        "library": library,
        "policy_sha256": policy_sha256,
        "quarantine_dir": quarantine_dir,
        "parameters": parameters,
        "moves": moves,
        "undo": [],
        "refused": refused[:MAX_REFUSED],
        "refused_total": refused_total,
        "preconditions": preconditions,
    }
    batch = hashlib.sha256(canonical({**body, "moves": moves}).encode("ascii")).hexdigest()[:16]
    body["batch_id"] = batch
    for move in moves:
        move["to"] = f"{quarantine_dir}/{batch}/{move['from']}"
    body["undo"] = [{"root": m["root"], "from": m["to"], "to": m["from"]} for m in moves]
    return body

