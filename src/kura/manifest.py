"""Reacquisition manifest: which library titles are safely backed by a healthy torrent.

The founder's mental model is a photo app that clears the phone's local copy once the
picture is safely in the cloud. A local copy may only be *proposed* for clearing when it
is on the server (every file's bytes verified against the torrent's piece hashes) AND
the torrent is healthy and re-downloadable (public, unprotected and, in a later stage,
seeded). Kura proposes; the owner purges. Nothing here moves, renames or deletes.

``build`` scans the library roots and a directory of ``.torrent`` descriptors (read-only,
through ``rofs``), matches torrents to titles (``match``), verifies each linked title
(``pieceverify``) and records everything in the private state database through
``ManifestRepo``. ``show`` recomputes every title's status from that database alone.

Storage conventions
  * A title is keyed ``<root label>/<top-level entry>`` in ``library_titles.rel_path``.
    File rows (``library_files``) and link mappings stay relative to their root, so a
    later stage splits the title key at the first ``/`` to get ``(root, title)``.
  * ``library_titles.identity`` is a JSON object: ``anilist`` (alias lookup result or
    null) and ``assessment`` (policy hash and completeness, whether a ``path:`` entry of
    the protected list covers the title, and the scan time). ``show`` needs no policy
    files because the build recorded what it needs.
  * A title that disappears from a root that was scanned is kept, marked
    ``absent_from_scan`` and shown as ``stale``. Verification history is append-only; the
    newest row per link counts.
  * A title under a root label that a *complete* build was not given (labels get retired:
    per-disk labels replaced by share labels) is marked absent in ``library_absent``. An
    absent title is invisible to every command (``show``, ``plan``, probe, audit,
    identity, retain, swarm, resolve) and can never reach a plan. The mark is reversible: a
    build that sees the title again clears it. ``prune`` deletes absent and vanished rows
    from Kura's state (never media). A build that was cut short (read budget spent,
    interrupted, an empty root) marks nothing, and ``--keep-other-roots`` (``mark_absent=False``)
    marks nothing for a deliberate partial rebuild of some roots.

Title status (first rule that applies wins)
  stale          absent from the latest scan, or the evidence behind a verification
                 (size, mtime, device, inode per file) no longer matches the inventory
  protected      a ``path:`` protected entry covers it, or any linked torrent is private,
                 denylisted, protected-listed or has unparsable announce data (or its
                 protection record is unreadable)
  blocked        the inventory could not follow something inside it (symlink, mount, ...)
  unmatched      no torrent is linked (or the title has no files)
  unverified     linked but nothing verified, or the policy lists were not both supplied
  partial        some files verified, but others failed, were not verified, or no torrent
                 covers them (``uncovered_files``)
  reacquirable   every file is covered by a linked torrent whose verification is
                 ``complete`` and verifies it, nothing protected, policy lists supplied

A link the latest build no longer produces is rewritten as ``superseded`` (no mapping): it
keeps protecting but covers nothing. A torrent classified under a policy other than the
title's last assessment makes the title ``unverified`` (``not_reclassified``) until the
next build reprocesses it.

Reasons are short fixed tokens. ``uncovered_files`` means the title has files that no
matched torrent lists (extra files); such a title is never reacquirable.
"""
import contextlib
import os
from pathlib import Path
import re
import time
from typing import Callable, Iterator

from . import rofs
from .core import Core
from .manifest_store import ManifestRepo
from .inventory import scan as scan_inventory
from .match import match_many
from .models import KuraError
from .pieceverify import verify_tree
from .protect import Protection, ProtectionPolicy, classify, magnet
from .torrent import decode
from .torrentmeta import MAX_DESCRIPTOR, TorrentMeta, parse, release_group

STATUSES = ("reacquirable", "protected", "partial", "unverified", "unmatched", "blocked", "stale")
SUPERSEDED = "superseded"   # link strategy: no longer matched; keeps protection only
CONCLUSIVE = ("complete", "partial", "failed")   # verdicts that a re-run would only repeat
MAX_LISTED = 100
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MEDIA_EXT = re.compile(r"\.(mkv|mp4|avi|m4v|webm|mov)\Z", re.I)


# ---- reading input files ---------------------------------------------------------------

def read_file(path: Path, max_bytes: int = MAX_DESCRIPTOR) -> bytes:
    """Read one regular file through ``rofs`` (no symlinks, no writes), at most `max_bytes`."""
    path = Path(os.path.abspath(path))
    root_fd = rofs.open_root(path.parent)
    try:
        fd = rofs.open_file_ro(root_fd, (path.name,))
        try:
            size = os.fstat(fd).st_size
            if size > max_bytes:
                raise KuraError("file_too_large", "File exceeds the size limit", limit=max_bytes)
            return b"".join(rofs.read_exact(fd, size))
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def iter_torrents(directory: Path, *, max_files: int = 20_000, max_bytes: int = 32 << 20
                  ) -> Iterator[tuple[str, bytes | None, str | None]]:
    """Yield ``(file name, bytes, None)`` or ``(file name, None, skip reason)``.

    Only direct children named ``*.torrent`` are considered, in raw-name order. Symlinks,
    non-regular files, oversized files and unreadable ones are skipped with a reason, never
    followed. More than `max_files` candidates raise ``torrent_dir_limit``.
    """
    root_fd = rofs.open_root(Path(os.path.abspath(directory)))
    try:
        try:
            with os.scandir(root_fd) as it:
                candidates = []
                for seen, entry in enumerate(it, 1):
                    if seen > 10 * max_files:
                        raise KuraError("torrent_dir_limit", "Torrent directory is too large")
                    if entry.name.lower().endswith(".torrent"):
                        candidates.append(entry)
                        if len(candidates) > max_files:
                            raise KuraError("torrent_dir_limit",
                                            "More torrent files than the limit", max_files=max_files)
        except OSError as exc:
            raise KuraError("io_error", f"Torrent directory could not be listed: {exc.strerror}") from exc
        candidates.sort(key=lambda e: os.fsencode(e.name))
        for entry in candidates:
            try:
                entry.name.encode("utf-8")
            except UnicodeEncodeError:
                yield os.fsencode(entry.name).decode("utf-8", "backslashreplace"), None, "undecodable_name"
                continue
            try:
                if entry.is_symlink():
                    yield entry.name, None, "symlink"
                    continue
                if not entry.is_file(follow_symlinks=False):
                    yield entry.name, None, "not_regular"
                    continue
                if entry.stat(follow_symlinks=False).st_size > max_bytes:
                    yield entry.name, None, "too_large"
                    continue
                fd = rofs.open_file_ro(root_fd, (entry.name,))
            except KuraError as exc:
                yield entry.name, None, exc.code
                continue
            except OSError:
                yield entry.name, None, "io_error"
                continue
            try:
                size = os.fstat(fd).st_size
                if size > max_bytes:
                    yield entry.name, None, "too_large"
                    continue
                data = b"".join(rofs.read_exact(fd, size))
            except KuraError as exc:
                yield entry.name, None, exc.code
                continue
            finally:
                os.close(fd)
            yield entry.name, data, None
    finally:
        os.close(root_fd)


def scan_torrents(directory: Path, *, max_files: int = 20_000, max_bytes: int = 32 << 20
                  ) -> list[tuple[str, bytes]]:
    """``(file name, bytes)`` of every readable ``*.torrent`` directly inside `directory`."""
    return [(name, data) for name, data, _ in
            iter_torrents(directory, max_files=max_files, max_bytes=max_bytes) if data is not None]


# ---- evidence and status ---------------------------------------------------------------

def _key(entry: dict) -> tuple:
    return (entry["size"], entry["mtime_ns"], entry["dev"], entry["ino"])


def _current(files) -> dict[str, tuple]:
    return {f["rel_path"]: _key(f) for f in files}


def evidence_current(vfiles: list[dict], current: dict[str, tuple]) -> bool:
    """True when every file a verification looked at is still exactly as it was.

    A file the verification saw must have the same size/mtime/device/inode; a file it
    found missing or unsafe must still be absent from the inventory.
    """
    for f in vfiles:
        local = f.get("local")
        if local is None:
            continue
        evidence, now = f.get("evidence"), current.get(local)
        if evidence is None:
            if now is not None:
                return False
        elif now is None or now != _key(evidence):
            return False
    return True


def _mapping_matches(vfiles: list[dict], mapping: dict[int, tuple[str, ...]]) -> bool:
    recorded = {f["index"]: f["local"] for f in vfiles if f.get("local") is not None}
    return recorded == {i: "/".join(parts) for i, parts in mapping.items()}


def _cacheable(verification: dict | None, mapping, current) -> bool:
    if not verification or verification["verdict"] not in CONCLUSIVE:
        return False
    vfiles = verification["files"]
    if any(f.get("status") in ("unverified", "changed") for f in vfiles):
        return False
    return _mapping_matches(vfiles, mapping) and evidence_current(vfiles, current)


def _link_paths(link: dict) -> set[str]:
    return {"/".join(parts) for parts in link["mapping"].values()}


def title_status(title: dict, files: list[dict], links: list[dict],
                 verifications: dict[str, dict | None], torrents: dict[str, dict]) -> dict:
    """Status of one title from stored facts only.

    ``title`` is a ``ManifestRepo.titles`` row, ``files`` its ``title_files`` rows,
    ``links`` its ``links``, ``verifications`` the latest verification per infohash (or
    None) and ``torrents`` the ``torrent`` rows by infohash. Returns ``status``,
    ``reasons`` and ``coverage`` (files / covered / verified / uncovered counts).
    """
    blocked = [b for b in title.get("blocked") or []]
    assessment = ((title.get("identity") or {}).get("assessment")) or {}
    paths = {f["rel_path"] for f in files}
    coverage = {"files": len(paths), "covered": 0, "verified": 0, "uncovered": 0}

    def result(status, reasons):
        return {"status": status, "reasons": sorted(set(reasons)), "coverage": coverage}

    if "absent_from_scan" in blocked:
        return result("stale", ["absent_from_latest_scan"])

    protected = []
    if assessment.get("path_protected"):
        protected.append("path_protected")
    for link in links:
        row = torrents.get(link["infohash"])
        protection = row.get("protection") if row else None
        if (not isinstance(protection, dict) or not isinstance(protection.get("protected"), bool)
                or not isinstance(row.get("private"), bool)):
            protected.append("protection_unknown")
        elif protection["protected"] or row["private"]:
            protected.append("torrent_protected")
            protected.extend(str(r) for r in protection.get("reasons") or [])
    if protected:
        return result("protected", protected)

    if blocked:
        return result("blocked", ["blocked:" + b.split(":", 1)[0] for b in blocked])
    active = [link for link in links if link["strategy"] != SUPERSEDED]
    if not files:
        return result("unmatched", ["no_files"])
    if not active:
        return result("unmatched", ["no_torrent_linked"])
    for link in active:   # a classification made under another policy proves nothing now
        if torrents[link["infohash"]]["protection"].get("policy_sha256") != assessment.get("policy_sha256"):
            return result("unverified", ["not_reclassified"])

    current = _current(files)
    best: dict[str, str] = {}      # mapped path -> "verified" only via a complete verification
    covered: set[str] = set()
    reasons: list[str] = []
    any_verified = False
    for link in active:
        covered |= _link_paths(link)
        verification = verifications.get(link["infohash"])
        if verification is None:
            reasons.append("not_verified")
            continue
        if not evidence_current(verification["files"], current):
            return result("stale", ["evidence_changed"])
        verdict = verification["verdict"]
        if verdict in ("interrupted", "budget_exhausted"):
            reasons.append("verification_incomplete")
        for f in verification["files"]:
            local, state = f.get("local"), f.get("status")
            if local is None or best.get(local) == "verified":
                continue
            if state == "verified":
                any_verified = True
                # Only a torrent whose every file verified proves its data is re-downloadable.
                state = "verified" if verdict == "complete" else "torrent_incomplete"
            best[local] = state
    covered &= paths
    verified = {p for p in covered if best.get(p) == "verified"}
    uncovered = paths - covered
    coverage.update(covered=len(covered), verified=len(verified), uncovered=len(uncovered))
    if uncovered:
        reasons.append("uncovered_files")
    for p in covered - verified:
        reasons.append(best.get(p) or "not_verified")
    if not uncovered and verified == paths:
        if assessment.get("policy_complete") is not True:
            return result("unverified", ["policy_incomplete"])
        return result("reacquirable", [])
    return result("partial" if any_verified else "unverified", reasons)


# ---- build -----------------------------------------------------------------------------

def _check_label(value, what: str):
    if not isinstance(value, str) or not _LABEL.match(value):
        raise KuraError("invalid_label", f"{what} must be 1-64 of letters, digits, . _ -")


def _path_protected(policy: ProtectionPolicy, label: str, title) -> bool:
    """Does a ``path:`` protected entry cover the title (as a label-prefixed or plain path)?"""
    for prefix in policy.path_prefixes:
        want = tuple(prefix.split("/"))
        for form in ((title.rel_path,), (label, title.rel_path)):
            n = min(len(want), len(form))
            if want[:n] != form[:n]:
                continue
            if len(want) <= len(form):
                return True                    # the title sits at or under the prefix
            for entry in title.files:          # the prefix points inside the title
                parts = tuple(entry.rel_path.split("/"))
                parts = parts if len(form) == 1 else (label, *parts)
                if parts[:len(want)] == want:
                    return True
    return False


def _search_name(rel: str) -> str:
    """A release folder or file name reduced to a plausible series title for AniList."""
    name = _MEDIA_EXT.sub("", rel)
    name = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)
    name = " ".join(name.replace("_", " ").split()).strip(" -.")
    return name[:200]


def _info_for_storage(meta: TorrentMeta, protected: bool) -> bytes | None:
    if protected or meta.private:
        return None
    try:
        decode(meta.info_bytes)     # the store applies acquisition limits; huge packs keep no info
    except KuraError:
        return None
    return meta.info_bytes


def _torrent_row(meta: TorrentMeta, protection, policy: ProtectionPolicy) -> dict:
    protected = protection.protected or meta.private
    return {
        "infohash": meta.infohash, "name": meta.name, "release_group": release_group(meta.name),
        "total_bytes": meta.total_length, "piece_length": meta.piece_length,
        "files": [{"index": f.index, "path": "/".join(f.path), "length": f.length, "pad": f.pad}
                  for f in meta.files],
        "private": meta.private,
        "protection": {"protected": protected, "reasons": list(protection.reasons),
                       "policy_sha256": policy.sha256},
        "magnet": None if protected else magnet(meta, protection),
        "info": _info_for_storage(meta, protected),
        "descriptor_sha256": meta.descriptor_sha256, "source": "torrents_dir"}


def _title_row(key: str, title, identity: dict) -> dict:
    return {"rel_path": key, "kind": title.kind, "total_bytes": title.total_bytes,
            "newest_mtime_ns": title.newest_mtime_ns, "file_count": len(title.files),
            "blocked": list(title.blocked), "files": [f.json() for f in title.files],
            "identity": identity}


def _protected_elsewhere(repo: ManifestRepo, db, library: str, infohash: str) -> bool:
    row = repo.torrent(infohash)
    if not row or not isinstance(row["protection"], dict) or not row["protection"].get("protected"):
        return False
    return db.execute(
        "SELECT 1 FROM manifest_links l JOIN library_titles t ON t.id=l.title_id"
        " WHERE l.infohash=? AND t.library<>? LIMIT 1", (infohash, library)).fetchone() is not None


def _identity(anilist: dict | None, assessment: dict) -> dict:
    return {"anilist": anilist, "assessment": assessment}


MAX_TORRENT_DIRS = 16


def _torrent_dirs(torrents_dir) -> list[Path]:
    """One directory or a sequence of them, absolute, in order, without repeats."""
    given = [torrents_dir] if isinstance(torrents_dir, (str, os.PathLike)) else list(torrents_dir)
    if not given:
        raise KuraError("invalid_argument", "At least one torrent directory is required")
    if len(given) > MAX_TORRENT_DIRS:
        raise KuraError("invalid_argument", "Too many torrent directories", limit=MAX_TORRENT_DIRS)
    return list(dict.fromkeys(Path(os.path.abspath(d)) for d in given))


def build(store, *, roots: dict[str, Path], library: str, torrents_dir,
          policy: ProtectionPolicy, max_read_bytes: int | None = None, reverify: bool = False,
          aliases=None, should_stop: Callable[[], bool] = lambda: False,
          mark_absent: bool = True) -> dict:
    """Scan, match, verify and record. See the module docstring for what is stored.

    Holds the store's mutation lock throughout. Each torrent is committed on its own (its
    row, its links, then each verification as it finishes), so an interrupt or a read
    budget keeps everything finished so far. `max_read_bytes` is a budget for the whole
    build: once spent, remaining links are left unverified (and reported as deferred).
    A verification whose files still match its recorded evidence is reused unless
    `reverify`. `aliases` is an AniList-style resolver or None (no lookups).
    `torrents_dir` is one directory or a sequence of them (scanned in order, repeats
    ignored); a torrent present in several is one torrent (same infohash), with protection
    merged as for any duplicate. After a complete build, titles under root labels that were not
    given are marked absent unless `mark_absent` is False (for a deliberate partial rebuild).
    """
    _check_label(library, "Library")
    if not isinstance(roots, dict) or not roots:
        raise KuraError("invalid_root", "At least one root is required")
    for label, path in roots.items():
        _check_label(label, "Root label")
        if not Path(path).is_absolute():
            raise KuraError("invalid_root", "Root paths must be absolute")
    if max_read_bytes is not None and max_read_bytes < 0:
        raise KuraError("invalid_argument", "max_read_bytes must be >= 0")
    dirs = _torrent_dirs(torrents_dir)
    repo = ManifestRepo(store.db)
    complete = policy.has_denylist and policy.has_protected_list

    with store.lock():
        # 1. Descriptors: parse, classify (fail closed), dedupe by infohash.
        parsed: dict[str, tuple[TorrentMeta, object]] = {}
        skipped, scanned, duplicates = [], 0, 0
        for name, data, reason in (item for d in dirs for item in iter_torrents(d)):
            scanned += 1
            if data is not None:
                try:
                    meta = parse(data)
                except KuraError as exc:
                    reason = exc.code
                except Exception:
                    reason = "unparsable"
            if reason is not None:
                skipped.append({"file": name, "reason": reason})
                continue
            try:
                protection = classify(meta, policy)
            except Exception:
                skipped.append({"file": name, "reason": "unclassifiable"})
                continue
            if meta.infohash in parsed:
                # Announce data is outside the infohash: another copy of the same torrent can
                # carry a denylisted tracker. Keep the first descriptor, merge protection.
                duplicates += 1
                first, kept = parsed[meta.infohash]
                protection = Protection(
                    kept.protected or protection.protected,
                    tuple(dict.fromkeys((*kept.reasons, *protection.reasons))))
                parsed[meta.infohash] = (first, protection)
            else:
                parsed[meta.infohash] = (meta, protection)

        # Record every torrent's current protection before anything else can expose an old
        # link: an interrupted build must never leave a stale "public" classification live.
        for meta, protection in parsed.values():
            if not (protection.protected or meta.private) and _protected_elsewhere(
                    repo, store.db, library, meta.infohash):
                # The record is shared by infohash across libraries. Another library already
                # holds this torrent under a protected descriptor: never silently clear it.
                protection = Protection(True, (*protection.reasons, "conflicting_descriptor"))
            repo.upsert_torrent(_torrent_row(meta, protection, policy))

        # 2. Inventory every root (metadata only) and match.
        invs = {label: scan_inventory(Path(path), label) for label, path in roots.items()}
        matches, unmatched = match_many(list(invs.values()), [m for m, _ in parsed.values()])

        # 3. Persist inventories, including titles that vanished from a root.
        # Absent titles are included so a title that comes back keeps its recorded identity and
        # its stale links are superseded; they are never rewritten as "vanished" below.
        existing = {t["rel_path"]: t for t in repo.titles(library, include_absent=True)}
        # A link that this scan no longer produces (torrent removed, now ambiguous, title
        # renamed) stops counting toward coverage; it still counts toward protection. Done
        # before the new inventory is published so an interrupt can only lose eligibility.
        current = {(f"{m.root}/{m.title_rel}", m.infohash) for m in matches}
        for key, old in existing.items():
            if key.split("/", 1)[0] not in invs:     # a root not scanned this time is untouched
                continue
            for link in repo.links(old["id"]):
                if (key, link["infohash"]) not in current and link["strategy"] != SUPERSEDED:
                    repo.link(old["id"], link["infohash"], SUPERSEDED, {})
        ids: dict[str, int] = {}
        files_by_key: dict[str, dict[str, tuple]] = {}
        assessments: dict[str, dict] = {}
        for label, inv in invs.items():
            rows, seen = [], set()
            for title in inv.titles:
                key = f"{label}/{title.rel_path}"
                seen.add(key)
                files_by_key[key] = _current(f.json() for f in title.files)
                assessment = {"policy_sha256": policy.sha256, "policy_complete": complete,
                              "path_protected": _path_protected(policy, label, title),
                              "built_at": inv.scanned_at}
                kept = ((existing.get(key) or {}).get("identity") or {}).get("anilist")
                assessments[key] = assessment
                rows.append(_title_row(key, title, _identity(kept, assessment)))
            for key, old in existing.items():
                if key.startswith(label + "/") and key not in seen and old["absent_at"] is None:
                    rows.append({"rel_path": key, "kind": old["kind"], "total_bytes": 0,
                                 "newest_mtime_ns": old["newest_mtime_ns"], "file_count": 0,
                                 "blocked": ["absent_from_scan"], "files": [],
                                 "seen_at": old["seen_at"]})   # last seen stays the truth
            ids.update(repo.save_inventory(library, inv.root_dev, rows, inv.scanned_at))

        # 4. Per torrent: link it, verify each linked title (committed as each finishes).
        by_torrent: dict[str, list] = {}
        for m in matches:
            by_torrent.setdefault(m.infohash, []).append(m)
        left = max_read_bytes
        stats = {"run": 0, "cached": 0, "deferred": 0, "skipped_protected": 0, "bytes_read": 0}
        interrupted = False
        cut_short = False
        for infohash, (meta, protection) in parsed.items():
            if should_stop():
                interrupted = True
                break
            for m in by_torrent.get(infohash, []):
                repo.link(ids[f"{m.root}/{m.title_rel}"], infohash, m.strategy,
                          {str(i): list(p) for i, p in m.mapping.items()})
            if protection.protected or meta.private:
                stats["skipped_protected"] += len(by_torrent.get(infohash, []))
                continue
            for m in by_torrent.get(infohash, []):
                key = f"{m.root}/{m.title_rel}"
                title_id = ids[key]
                if not reverify and _cacheable(repo.latest_verification(title_id, infohash),
                                               m.mapping, files_by_key[key]):
                    stats["cached"] += 1
                    continue
                if left is not None and left <= 0:
                    stats["deferred"] += 1
                    continue
                v = verify_tree(meta, Path(roots[m.root]), m.mapping,
                                max_read_bytes=left, should_stop=should_stop)
                stats["run"] += 1
                cut_short = cut_short or v.verdict in ("interrupted", "budget_exhausted")
                stats["bytes_read"] += v.bytes_read
                if left is not None:
                    left -= v.bytes_read
                repo.add_verification({
                    "title_id": title_id, "infohash": infohash, "verdict": v.verdict,
                    "files": [f.json() for f in v.files], "pieces_total": v.pieces_total,
                    "pieces_ok": v.pieces_ok, "bytes_read": v.bytes_read,
                    "started_at": v.started_at, "finished_at": v.finished_at})
                if v.verdict == "interrupted":
                    interrupted = True
                    break
            if interrupted:
                break

        # 5. Optional alias lookups for linked titles; never fails the build.
        anilist = {"enabled": aliases is not None, "resolved": 0, "unavailable": 0, "unresolved": 0}
        if aliases is not None and not interrupted:
            core = Core(store, None, aliases)
            for label, inv in invs.items():
                for title in inv.titles:
                    key = f"{label}/{title.rel_path}"
                    if not repo.links(ids[key]):
                        continue
                    query = _search_name(title.rel_path)
                    if not query:
                        continue
                    try:
                        found = core.alias_status(query)
                    except KuraError as exc:
                        found = {"status": "unavailable", "identity": None, "reason": exc.code}
                    identity = found.get("identity") or {}
                    note = {"status": found["status"], "query": query,
                            "anilist_id": identity.get("anilist_id"), "matched": identity.get("matched")}
                    anilist["resolved" if note["anilist_id"] else
                            "unavailable" if found["status"] == "unavailable" else "unresolved"] += 1
                    repo.save_inventory(library, inv.root_dev, [
                        _title_row(key, title, _identity(note, assessments[key]))], inv.scanned_at)

        # 6. Titles under root labels this build was not given are absent, but only when the
        # whole build ran to the end: a cut or failed run proves nothing about what is gone.
        absent = {"marked": 0, "complete": False, "reason": None}
        if not mark_absent:
            absent["reason"] = "disabled"
        elif interrupted or cut_short or stats["deferred"] or should_stop():
            absent["reason"] = "incomplete_build"
        elif any(not inv.titles for inv in invs.values()):
            absent["reason"] = "empty_root"       # an unmounted share looks exactly like this
        else:
            absent.update(complete=True, marked=repo.mark_absent(library, invs, time.time()))

    shown = show(store, library)
    protected_count = sum(1 for m, p in parsed.values() if p.protected or m.private)
    return {
        "library": library, "policy_sha256": policy.sha256, "policy_complete": complete,
        "roots": {label: {"titles": len(inv.titles), "entries": inv.entries}
                  for label, inv in invs.items()},
        "matched_by": {k: sum(1 for m in matches if m.strategy == k)
                       for k in sorted({m.strategy for m in matches})},
        "torrents": {"dirs": len(dirs), "scanned": scanned, "parsed": len(parsed), "protected": protected_count,
                     "duplicates": duplicates, "skipped": skipped[:MAX_LISTED],
                     "skipped_count": len(skipped)},
        "matched": len(matches), "unmatched": unmatched[:MAX_LISTED],
        "unmatched_count": len(unmatched), "verification": stats,
        "anilist": anilist, "absent": absent, "interrupted": interrupted, "counts": shown["counts"]}


# ---- prune -----------------------------------------------------------------------------

def prune(store, library: str, *, older_than_days: float | None = None, dry_run: bool = True,
          now: Callable[[], float] = time.time) -> dict:
    """Delete absent and vanished titles of `library` from Kura's state (never from disk).

    Candidates are titles marked absent (``absent_at`` at least `older_than_days` ago; None
    means all) and, only when `older_than_days` is given, titles a scan found vanished (last
    seen at least that long ago). Their file, link, verification, probe-result, absent-mark and
    identity rows go with them (the identities, so a fully pruned library cannot leak them into
    AniList or SeaDex lookups; owner pins from ``identity pin`` are never deleted). The dry run
    is advisory: it reads without a snapshot. A title whose links point at a protected or private torrent is
    kept (``kept_protected``). Torrent rows, plans and reports stay. With `dry_run` (the
    default) it only counts.
    Nothing here opens a library root.
    """
    _check_label(library, "Library")
    if older_than_days is not None and not (
            isinstance(older_than_days, (int, float)) and not isinstance(older_than_days, bool)
            and 0 <= older_than_days <= 36500):
        raise KuraError("invalid_arguments", "--older-than-days must be between 0 and 36500")
    cutoff = None if older_than_days is None else now() - older_than_days * 86400
    with (contextlib.nullcontext() if dry_run else store.lock()):   # a preview never blocks a build
        repo = ManifestRepo(store.db)
        total = len(repo.titles(library, include_absent=True))
        candidates, kept_protected = [], []
        for c in repo.prunable(library, cutoff):
            if _protection_evidence(repo, c["id"]):
                kept_protected.append(c["rel_path"])   # its links prove a torrent is protected elsewhere
            else:
                candidates.append(c)
        counts = repo.prune_titles(library, candidates, dry_run=dry_run)
    by_root: dict[str, int] = {}
    for c in candidates:
        label = c["rel_path"].split("/", 1)[0]
        by_root[label] = by_root.get(label, 0) + 1
    return {"library": library, "dry_run": dry_run, "older_than_days": older_than_days,
            "absent": sum(1 for c in candidates if c["state"] == "absent"),
            "vanished": sum(1 for c in candidates if c["state"] == "vanished"),
            "deleted" if not dry_run else "would_delete": counts,
            "kept_protected": len(kept_protected), "kept_protected_sample": kept_protected[:20],
            "by_root": dict(sorted(by_root.items())),
            "sample": [c["rel_path"] for c in candidates[:20]],
            "titles_kept": total - len(candidates)}


def _protection_evidence(repo: ManifestRepo, title_id: int) -> bool:
    """Does any link of the title point at a protected, private or unreadable torrent?

    ``_protected_elsewhere`` uses the links of other libraries as the evidence that stops a
    public copy from clearing a protection, so prune never deletes those links (fail closed).
    """
    try:
        for link in repo.links(title_id):
            row = repo.torrent(link["infohash"])
            protection = row and row.get("protection")
            if (not isinstance(protection, dict) or not isinstance(protection.get("protected"), bool)
                    or protection["protected"] or row["private"]):
                return True
    except Exception:       # corrupt rows prove nothing: keep the title
        return True
    return False


# ---- show ------------------------------------------------------------------------------

def show(store, library: str, status: str | None = None, title: str | None = None) -> dict:
    """Recompute every title's status from the database. Read-only."""
    if status is not None and status not in STATUSES:
        raise KuraError("invalid_arguments", "Unknown status filter", allowed=list(STATUSES))
    repo = ManifestRepo(store.db)
    rows = repo.titles(library)
    if not rows:
        raise KuraError("not_found", "No manifest for that library; run manifest build first")
    torrents: dict[str, dict] = {}
    counts = {s: 0 for s in STATUSES}
    out, policy_hash, latest = [], None, -1.0
    for row in rows:
        files = repo.title_files(row["id"])
        links = repo.links(row["id"])
        verifications = {link["infohash"]: repo.latest_verification(row["id"], link["infohash"])
                         for link in links}
        for link in links:
            if link["infohash"] not in torrents:
                torrents[link["infohash"]] = repo.torrent(link["infohash"])
        assessment = ((row.get("identity") or {}).get("assessment")) or {}
        if assessment.get("built_at", -1.0) > latest:
            latest, policy_hash = assessment["built_at"], assessment.get("policy_sha256")
        result = title_status(row, files, links, verifications, torrents)
        counts[result["status"]] += 1
        if (status and result["status"] != status) or (title and row["rel_path"] != title):
            continue
        anilist = (row.get("identity") or {}).get("anilist") or {}
        entries = []
        for link in links:
            t, v = torrents.get(link["infohash"]), verifications.get(link["infohash"])
            entries.append({
                "infohash": link["infohash"], "release_group": t and t["release_group"],
                "magnet": t and t["magnet"],
                "protected": True if t is None else bool(
                    not isinstance(t["protection"], dict) or t["protection"].get("protected")
                    or t["private"]),
                "verdict": v and v["verdict"], "verified_at": v and v["finished_at"]})
        out.append({"title": row["rel_path"], "status": result["status"],
                    "reasons": result["reasons"], "coverage": result["coverage"],
                    "anilist_id": anilist.get("anilist_id"), "files": row["file_count"],
                    "bytes": row["total_bytes"], "torrents": entries})
    return {"library": library, "policy_sha256": policy_hash,
            "counts": {k: v for k, v in counts.items() if v}, "titles": out}
