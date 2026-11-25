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
from .torrent import decode, encode

# SQL condition on ``title_identities.title`` (two ``library`` parameters): the identity belongs to
# a title that is present in the manifest. A library with no manifest rows at all keeps every
# identity, so identities recorded before any build still count. Absent, vanished-and-pruned
# and deleted titles do not.
PRESENT_IDENTITY_SQL = (
    "(NOT EXISTS (SELECT 1 FROM library_titles WHERE library=?) OR title IN ("
    "SELECT t.rel_path FROM library_titles t LEFT JOIN library_absent a ON a.title_id=t.id"
    " WHERE t.library=? AND a.title_id IS NULL))")
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


_URL_KEYS = (b"announce", b"announce-list", b"url-list", b"httpseeds", b"nodes")


def _check_info(info, infohash: str):
    """The stored info must be the canonical, public info dictionary of this infohash."""
    if not isinstance(info, (bytes, bytearray)):
        raise _invalid("Info must be bytes")
    info = bytes(info)
    if hashlib.sha1(info).hexdigest() != infohash:
        raise _invalid("Info does not match the infohash")
    try:
        decoded = decode(info)
        canonical_form = isinstance(decoded, dict) and encode(decoded) == info
    except (KuraError, ValueError, TypeError):
        canonical_form = False
    if not canonical_form:
        raise _invalid("Info must be a canonical bencoded dictionary")
    if decoded.get(b"private", 0) != 0 or any(k in decoded for k in _URL_KEYS):
        raise _invalid("Info of a private torrent or carrying URLs is never stored")


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
        that need evidence must pass them), optional seen_at (overrides the call's value
        for that title, so a vanished title keeps its true last-seen time). Titles absent
        from `titles` are left untouched, keeping their old seen_at. Saving a title makes
        it present again (any absent mark is cleared). Returns {rel_path: title_id}.
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
                     identity, title.get("seen_at", seen_at), 1 if has_identity else 0))
                title_id = db.execute(
                    "SELECT id FROM library_titles WHERE library=? AND rel_path=?",
                    (library, title["rel_path"])).fetchone()[0]
                ids[title["rel_path"]] = title_id
                db.execute("DELETE FROM library_absent WHERE title_id=?", (title_id,))
                db.execute("DELETE FROM library_files WHERE title_id=?", (title_id,))
                db.executemany(
                    "INSERT INTO library_files VALUES (?,?,?,?,?,?)",
                    [(title_id, f["rel_path"], f["size"], f["mtime_ns"], f["dev"], f["ino"])
                     for f in files])
        return ids

    def titles(self, library: str, include_absent: bool = False) -> list[dict]:
        """Titles of a library ordered by rel_path.

        Titles marked absent (see ``mark_absent``) are left out unless `include_absent`.
        Keys: id, library, rel_path, kind, root_dev, file_count, total_bytes,
        newest_mtime_ns, blocked (list[str]), identity (dict|None), seen_at, absent_at
        (float|None; always None unless `include_absent`).
        """
        rows = self.db.execute(
            "SELECT t.id, t.library, t.rel_path, t.kind, t.root_dev, t.file_count, t.total_bytes,"
            " t.newest_mtime_ns, t.blocked, t.identity, t.seen_at, a.absent_at FROM library_titles t"
            " LEFT JOIN library_absent a ON a.title_id=t.id"
            " WHERE t.library=? AND (? OR a.title_id IS NULL) ORDER BY t.rel_path",
            (library, 1 if include_absent else 0)).fetchall()
        return [{"id": r[0], "library": r[1], "rel_path": r[2], "kind": r[3], "root_dev": r[4],
                 "file_count": r[5], "total_bytes": r[6], "newest_mtime_ns": r[7],
                 "blocked": _load(r[8]), "identity": _load(r[9]), "seen_at": r[10],
                 "absent_at": r[11]}
                for r in rows]

    def mark_absent(self, library: str, scanned_labels, at: float) -> int:
        """Mark every present title whose root label is not in `scanned_labels` as absent.

        The title key is ``<root label>/<entry>``. The mark is reversible (saving the title
        again clears it) and keeps its first timestamp. Returns the number newly marked.
        Callers must only use this after a complete build.
        """
        scanned = set(scanned_labels)
        with self._tx() as db:
            rows = db.execute(
                "SELECT t.id, t.rel_path FROM library_titles t LEFT JOIN library_absent a"
                " ON a.title_id=t.id WHERE t.library=? AND a.title_id IS NULL",
                (library,)).fetchall()
            ids = [i for i, rel in rows if rel.split("/", 1)[0] not in scanned]
            db.executemany("INSERT OR IGNORE INTO library_absent(title_id, absent_at) VALUES (?,?)",
                           [(i, at) for i in ids])
            return len(ids)

    def prunable(self, library: str, cutoff: float | None = None) -> list[dict]:
        """Titles `prune_titles` may delete: absent-marked ones whose mark is at or before
        `cutoff` (None: all of them), and, only when a cutoff is given, titles a scan found
        vanished (``absent_from_scan``) last seen at or before it: a root that was down for one
        build must not cost the history of everything in it. Keys: id, rel_path, state
        ('absent'|'vanished')."""
        out = []
        for r in self.db.execute(
                "SELECT t.id, t.rel_path, t.blocked, t.seen_at, a.absent_at FROM library_titles t"
                " LEFT JOIN library_absent a ON a.title_id=t.id WHERE t.library=? ORDER BY t.rel_path",
                (library,)):
            if r[4] is not None:
                state, when = "absent", r[4]
            elif cutoff is not None and "absent_from_scan" in (_load(r[2]) or []):
                state, when = "vanished", r[3]
            else:
                continue
            if cutoff is None or when <= cutoff:
                out.append({"id": r[0], "rel_path": r[1], "state": state})
        return out

    def prune_titles(self, library: str, titles: list[dict], dry_run: bool = True) -> dict[str, int]:
        """Delete these titles (dicts with id and rel_path, all of `library`) with their files,
        links, verifications, probe results, absent marks and recorded identities, from Kura's
        own state only. Returns the row counts (what was deleted, or with `dry_run` what would
        be). Torrent rows are shared and never deleted. A dry run only reads."""
        ids = list(dict.fromkeys(t["id"] for t in titles))
        keys = list(dict.fromkeys(t["rel_path"] for t in titles))
        plan = (("titles", "library_titles", "id", ids, ""),
                ("files", "library_files", "title_id", ids, ""),
                ("links", "manifest_links", "title_id", ids, ""),
                ("verifications", "verifications", "title_id", ids, ""),
                ("probe_results", "probe_results", "title_id", ids, ""),
                ("marks", "library_absent", "title_id", ids, ""),
                ("identities", "title_identities", "title", keys, " AND library=?"))
        counts = {name: 0 for name, *_ in plan}

        def run(db):
            for name, table, column, values, extra in plan:
                for start in range(0, len(values), 500):
                    chunk = values[start:start + 500]
                    where = f" WHERE {column} IN ({','.join('?' * len(chunk))}){extra}"
                    args = [*chunk, *([library] if extra else [])]
                    if dry_run:
                        counts[name] += db.execute(f"SELECT COUNT(*) FROM {table}{where}", args).fetchone()[0]
                    else:
                        counts[name] += db.execute(f"DELETE FROM {table}{where}", args).rowcount

        if dry_run:
            run(self.db)
        else:
            with self._tx() as db:
                run(db)
        return counts

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
        equals the infohash, decodes canonically (bounded by torrent.decode limits; pass
        None for larger descriptors), is not private and carries no tracker or web-seed
        URL keys. first_seen is kept when the row already exists.
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
