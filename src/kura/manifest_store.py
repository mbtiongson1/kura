"""Repository for the library manifest tables (state schema v2).

Plain dicts, lists and ints in and out: no dataclasses from other modules. JSON
columns (files, protection, mapping, blocked, plan, detail, identity) are stored as
canonical JSON text and decoded on the way out. Every method is one transaction.

This database holds the owner's real library data, so it lives only in Kura's private
state directory (the Store's 0700 root, 0600 file) and never in the repository.
Magnet links and sanitized info dictionaries are accepted only for public, unprotected
torrents; anything else is refused here as defence in depth.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlsplit

from .models import KuraError

_INFOHASH = re.compile(r"[0-9a-f]{40}\Z")
_PLAN_HASH = re.compile(r"[0-9a-f]{64}\Z")


def canonical(value) -> str:
    """Canonical JSON text: sorted keys, compact separators, ASCII only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _invalid(message: str):
    return KuraError("invalid_manifest", message)


def _need(row: dict, *keys: str):
    if not isinstance(row, dict):
        raise _invalid("Expected a mapping")
    missing = [k for k in keys if k not in row]
    if missing:
        raise _invalid("Missing field(s): " + ", ".join(missing))


def _infohash(value) -> str:
    if not isinstance(value, str) or not _INFOHASH.match(value):
        raise _invalid("Infohash must be 40 lowercase hex characters")
    return value


def _check_magnet(magnet, infohash: str):
    """Accept only a bare btih magnet with dn and credential-free http/https/udp trackers."""
    try:
        parts = urlsplit(magnet)
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise _invalid("Magnet is not parseable")
    if parts.scheme != "magnet" or parts.netloc or parts.path or parts.fragment:
        raise _invalid("Magnet is not a plain magnet link")
    xt = [v for k, v in pairs if k == "xt"]
    if xt != ["urn:btih:" + infohash] or any(k not in ("xt", "dn", "tr") for k, _ in pairs):
        raise _invalid("Magnet must carry only this infohash, a name and trackers")
    for k, v in pairs:
        if k != "tr":
            continue
        try:
            t = urlsplit(v)
            _ = t.port
        except ValueError:
            raise _invalid("Magnet tracker is not parseable")
        if (t.scheme not in ("http", "https", "udp") or not t.hostname or t.query or t.fragment
                or "@" in t.netloc or t.username or t.password):
            raise _invalid("Magnet tracker must be a credential-free http, https or udp URL")


def _check_info(info, infohash: str):
    """The stored info must be exactly the hashed info dictionary, so it holds no URLs."""
    if (not isinstance(info, (bytes, bytearray)) or not info.startswith(b"d")
            or hashlib.sha1(info).hexdigest() != infohash):
        raise _invalid("Info must be the bencoded info dictionary matching the infohash")


def _load(text):
    return json.loads(text) if text is not None else None


class ManifestRepo:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @contextmanager
    def _tx(self):
        """One write transaction. BEGIN IMMEDIATE takes the write lock up front."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException as exc:
            # Also covers a failed commit (lock timeout): never leave a transaction open.
            if self.db.in_transaction:
                self.db.rollback()
            if isinstance(exc, sqlite3.IntegrityError):
                raise _invalid("Row violates a manifest constraint") from exc
            raise

    # ---- library inventory -------------------------------------------------

    def save_inventory(self, library: str, root_dev: int, titles: list[dict],
                       seen_at: float) -> dict[str, int]:
        """Upsert titles of one library and replace their file rows.

        Each title dict: rel_path (str), kind ('dir'|'file'), total_bytes,
        newest_mtime_ns (ints), optional file_count (defaults to len(files)), optional
        blocked (list[str], default []), optional identity (JSON-able dict or None;
        omitted keeps the stored identity), optional files (list of dicts with
        rel_path, size, mtime_ns, dev, ino; omitted means no file rows, so callers
        that need evidence must pass them). Titles absent from `titles` are left
        untouched, keeping their old seen_at. Returns {rel_path: title_id}.
        """
        if not isinstance(library, str) or not library:
            raise _invalid("Library label is required")
        ids: dict[str, int] = {}
        with self._tx() as db:
            for title in titles:
                _need(title, "rel_path", "kind", "total_bytes", "newest_mtime_ns")
                files = title.get("files") or []
                for f in files:
                    _need(f, "rel_path", "size", "mtime_ns", "dev", "ino")
                has_identity = "identity" in title
                identity = canonical(title["identity"]) if title.get("identity") is not None else None
                db.execute(
                    "INSERT INTO library_titles(library, rel_path, kind, root_dev, file_count,"
                    " total_bytes, newest_mtime_ns, blocked, identity, seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(library, rel_path) DO UPDATE SET kind=excluded.kind,"
                    " root_dev=excluded.root_dev, file_count=excluded.file_count,"
                    " total_bytes=excluded.total_bytes, newest_mtime_ns=excluded.newest_mtime_ns,"
                    " blocked=excluded.blocked, seen_at=excluded.seen_at,"
                    " identity=CASE WHEN ? THEN excluded.identity ELSE library_titles.identity END",
                    (library, title["rel_path"], title["kind"], root_dev,
                     title.get("file_count", len(files)), title["total_bytes"],
                     title["newest_mtime_ns"], canonical(list(title.get("blocked") or [])),
                     identity, seen_at, 1 if has_identity else 0))
                title_id = db.execute(
                    "SELECT id FROM library_titles WHERE library=? AND rel_path=?",
                    (library, title["rel_path"])).fetchone()[0]
                ids[title["rel_path"]] = title_id
                db.execute("DELETE FROM library_files WHERE title_id=?", (title_id,))
                db.executemany(
                    "INSERT INTO library_files VALUES (?,?,?,?,?,?)",
                    [(title_id, f["rel_path"], f["size"], f["mtime_ns"], f["dev"], f["ino"])
                     for f in files])
        return ids

    def titles(self, library: str) -> list[dict]:
        """Titles of a library ordered by rel_path.

        Keys: id, library, rel_path, kind, root_dev, file_count, total_bytes,
        newest_mtime_ns, blocked (list[str]), identity (dict|None), seen_at.
        """
        rows = self.db.execute(
            "SELECT id, library, rel_path, kind, root_dev, file_count, total_bytes,"
            " newest_mtime_ns, blocked, identity, seen_at FROM library_titles"
            " WHERE library=? ORDER BY rel_path", (library,)).fetchall()
        return [{"id": r[0], "library": r[1], "rel_path": r[2], "kind": r[3], "root_dev": r[4],
                 "file_count": r[5], "total_bytes": r[6], "newest_mtime_ns": r[7],
                 "blocked": _load(r[8]), "identity": _load(r[9]), "seen_at": r[10]}
                for r in rows]

    def title_files(self, title_id: int) -> list[dict]:
        """File evidence of one title ordered by rel_path.

        Keys: rel_path, size, mtime_ns, dev, ino.
        """
        return [{"rel_path": r[0], "size": r[1], "mtime_ns": r[2], "dev": r[3], "ino": r[4]}
                for r in self.db.execute(
                    "SELECT rel_path, size, mtime_ns, dev, ino FROM library_files"
                    " WHERE title_id=? ORDER BY rel_path", (title_id,))]

    # ---- torrents and links ------------------------------------------------

    def upsert_torrent(self, row: dict):
        """Insert or update one torrent keyed by infohash.

        Row keys: infohash (40 lowercase hex), name, total_bytes, piece_length,
        files (JSON-able list), private (bool), protection (dict with a boolean
        'protected', plus reasons and policy_sha256), descriptor_sha256, source (str);
        optional release_group, magnet, info (bytes), first_seen, updated_at (default
        now). magnet and info must be None for private or protected torrents (else
        invalid_manifest). A magnet must be 'magnet:?xt=urn:btih:<infohash>' plus
        optional dn and tr parameters, each tr an http/https/udp URL without query,
        fragment or userinfo; info must be the bencoded info dictionary whose SHA-1
        equals the infohash. first_seen is kept when the row already exists.
        """
        _need(row, "infohash", "name", "total_bytes", "piece_length", "files", "private",
              "protection", "descriptor_sha256", "source")
        infohash = _infohash(row["infohash"])
        protection = row["protection"]
        if not isinstance(protection, dict) or not isinstance(protection.get("protected"), bool):
            raise _invalid("Protection must be a mapping with a boolean 'protected'")
        magnet, info = row.get("magnet"), row.get("info")
        if (protection["protected"] or row["private"]) and (magnet is not None or info is not None):
            raise _invalid("Magnet and info are stored only for public, unprotected torrents")
        if magnet is not None:
            _check_magnet(magnet, infohash)
        if info is not None:
            _check_info(info, infohash)
        now = time.time()
        with self._tx() as db:
            db.execute(
                "INSERT INTO manifest_torrents(infohash, name, release_group, total_bytes,"
                " piece_length, files, private, protection, magnet, info, descriptor_sha256,"
                " source, first_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(infohash) DO UPDATE SET name=excluded.name,"
                " release_group=excluded.release_group, total_bytes=excluded.total_bytes,"
                " piece_length=excluded.piece_length, files=excluded.files,"
                " private=excluded.private, protection=excluded.protection,"
                " magnet=excluded.magnet, info=excluded.info,"
                " descriptor_sha256=excluded.descriptor_sha256, source=excluded.source,"
                " updated_at=excluded.updated_at",
                (infohash, row["name"], row.get("release_group"), row["total_bytes"],
                 row["piece_length"], canonical(row["files"]), 1 if row["private"] else 0,
                 canonical(protection), magnet, None if info is None else bytes(info),
                 row["descriptor_sha256"], row["source"],
                 row.get("first_seen", now), row.get("updated_at", now)))

    def torrent(self, infohash: str) -> dict | None:
        """One torrent or None.

        Keys: infohash, name, release_group, total_bytes, piece_length, files (list),
        private (bool), protection (dict), magnet, info (bytes|None), descriptor_sha256,
        source, first_seen, updated_at.
        """
        r = self.db.execute(
            "SELECT infohash, name, release_group, total_bytes, piece_length, files, private,"
            " protection, magnet, info, descriptor_sha256, source, first_seen, updated_at"
            " FROM manifest_torrents WHERE infohash=?", (infohash,)).fetchone()
        if not r:
            return None
        return {"infohash": r[0], "name": r[1], "release_group": r[2], "total_bytes": r[3],
                "piece_length": r[4], "files": _load(r[5]), "private": bool(r[6]),
                "protection": _load(r[7]), "magnet": r[8],
                "info": None if r[9] is None else bytes(r[9]),
                "descriptor_sha256": r[10], "source": r[11],
                "first_seen": r[12], "updated_at": r[13]}

    def link(self, title_id: int, infohash: str, strategy: str, mapping: dict):
        """Associate a title with a torrent (replacing any earlier link for the pair).

        Both the title and the torrent must already exist. `mapping` is a JSON-able
        dict (torrent file index -> list of path parts); JSON turns integer keys into
        strings on the way back out.
        """
        _infohash(infohash)
        if not isinstance(mapping, dict) or not isinstance(strategy, str) or not strategy:
            raise _invalid("Link needs a strategy and a mapping")
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM library_titles WHERE id=?", (title_id,)).fetchone():
                raise _invalid("Unknown title")
            if not db.execute("SELECT 1 FROM manifest_torrents WHERE infohash=?",
                              (infohash,)).fetchone():
                raise _invalid("Unknown torrent")
            db.execute("INSERT OR REPLACE INTO manifest_links VALUES (?,?,?,?)",
                       (title_id, infohash, strategy, canonical(mapping)))

    def links(self, title_id: int) -> list[dict]:
        """Links of a title ordered by infohash. Keys: title_id, infohash, strategy,
        mapping (dict, string keys)."""
        return [{"title_id": r[0], "infohash": r[1], "strategy": r[2], "mapping": _load(r[3])}
                for r in self.db.execute(
                    "SELECT title_id, infohash, strategy, mapping FROM manifest_links"
                    " WHERE title_id=? ORDER BY infohash", (title_id,))]

    # ---- verification history ---------------------------------------------

    def add_verification(self, row: dict) -> int:
        """Append one verification (history is never rewritten). Returns its id.

        Row keys: title_id, infohash, verdict, files (JSON-able list; each entry should
        carry size, mtime_ns, dev, ino as evidence), pieces_total, pieces_ok,
        bytes_read, started_at, finished_at.
        """
        _need(row, "title_id", "infohash", "verdict", "files", "pieces_total", "pieces_ok",
              "bytes_read", "started_at", "finished_at")
        _infohash(row["infohash"])
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO verifications(title_id, infohash, verdict, files, pieces_total,"
                " pieces_ok, bytes_read, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["title_id"], row["infohash"], row["verdict"], canonical(row["files"]),
                 row["pieces_total"], row["pieces_ok"], row["bytes_read"],
                 row["started_at"], row["finished_at"]))
            return cur.lastrowid

    def latest_verification(self, title_id: int, infohash: str) -> dict | None:
        """Most recent verification by finished_at (ties: highest id) or None.

        Keys: id, title_id, infohash, verdict, files (list), pieces_total, pieces_ok,
        bytes_read, started_at, finished_at.
        """
        r = self.db.execute(
            "SELECT id, title_id, infohash, verdict, files, pieces_total, pieces_ok,"
            " bytes_read, started_at, finished_at FROM verifications"
            " WHERE title_id=? AND infohash=? ORDER BY finished_at DESC, id DESC LIMIT 1",
            (title_id, infohash)).fetchone()
        if not r:
            return None
        return {"id": r[0], "title_id": r[1], "infohash": r[2], "verdict": r[3],
                "files": _load(r[4]), "pieces_total": r[5], "pieces_ok": r[6],
                "bytes_read": r[7], "started_at": r[8], "finished_at": r[9]}

    # ---- swarm checks ------------------------------------------------------

    def add_swarm_check(self, row: dict) -> int:
        """Append one swarm observation. Returns its id.

        Row keys: infohash, checked_at, status (str); optional seeders, leechers
        (int|None), detail (JSON-able, default {}).
        """
        _need(row, "infohash", "checked_at", "status")
        _infohash(row["infohash"])
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO swarm_checks(infohash, checked_at, seeders, leechers, status, detail)"
                " VALUES (?,?,?,?,?,?)",
                (row["infohash"], row["checked_at"], row.get("seeders"), row.get("leechers"),
                 row["status"], canonical(row.get("detail") or {})))
            return cur.lastrowid

    def latest_swarm(self, infohash: str) -> dict | None:
        """Most recent swarm check by checked_at (ties: highest id) or None.

        Keys: id, infohash, checked_at, seeders, leechers, status, detail (dict).
        """
        r = self.db.execute(
            "SELECT id, infohash, checked_at, seeders, leechers, status, detail"
            " FROM swarm_checks WHERE infohash=? ORDER BY checked_at DESC, id DESC LIMIT 1",
            (infohash,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "infohash": r[1], "checked_at": r[2], "seeders": r[3],
                "leechers": r[4], "status": r[5], "detail": _load(r[6])}

    # ---- quarantine plans --------------------------------------------------

    def save_plan(self, plan_hash: str, plan_json: str, created_at: float) -> bool:
        """Store a plan under its sha256 (64 lowercase hex). `plan_json` is JSON text.

        Idempotent: returns True when inserted, False when the hash already exists
        (the stored plan and its status are left untouched).
        """
        if not isinstance(plan_hash, str) or not _PLAN_HASH.match(plan_hash):
            raise _invalid("Plan hash must be 64 lowercase hex characters")
        try:
            if not isinstance(json.loads(plan_json), dict):
                raise ValueError
        except (TypeError, ValueError):
            raise _invalid("Plan must be a JSON object text")
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO quarantine_plans(plan_hash, created_at, plan)"
                " VALUES (?,?,?)", (plan_hash, created_at, plan_json))
            return cur.rowcount == 1

    def plan(self, plan_hash: str) -> dict | None:
        """One stored plan or None. Keys: plan_hash, created_at, plan (dict), status."""
        r = self.db.execute(
            "SELECT plan_hash, created_at, plan, status FROM quarantine_plans"
            " WHERE plan_hash=?", (plan_hash,)).fetchone()
        if not r:
            return None
        return {"plan_hash": r[0], "created_at": r[1], "plan": _load(r[2]), "status": r[3]}
