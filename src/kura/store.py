"""Local transactional preferences, idempotency receipts and state-transition audit."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from .models import KuraError, Preferences
from .safety import destination, ensure_dir, no_symlinks, regular_file


def validate_prefs(p: Preferences) -> Preferences:
    if p.resolution not in ("480p", "720p", "1080p", "2160p", "any"):
        raise KuraError("invalid_preferences", "Unsupported resolution")
    if p.subtitles not in ("english", "raw", "any"):
        raise KuraError("invalid_preferences", "Subtitles must be english, raw or any")
    if not 1 <= p.max_size_mb <= 10000 or not 0 <= p.min_seeders <= 1000000:
        raise KuraError("invalid_preferences", "Size must be 1..10000 MiB; seeders 0..1000000")
    if len(p.groups) > 20 or any(not g or len(g) > 80 or any(ord(c) < 32 for c in g) for g in p.groups):
        raise KuraError("invalid_preferences", "Invalid release group list")
    destination(p.destination)
    return p


SCHEMA_VERSION = 4

# Each migration is idempotent DDL (IF NOT EXISTS) so a concurrent opener that
# queued behind the writer re-runs it harmlessly; the version bump is guarded.
MIGRATIONS = {
    2: """
        CREATE TABLE IF NOT EXISTS library_titles (
            id INTEGER PRIMARY KEY, library TEXT NOT NULL, rel_path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('dir','file')), root_dev INTEGER NOT NULL,
            file_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL,
            newest_mtime_ns INTEGER NOT NULL, blocked TEXT NOT NULL DEFAULT '[]',
            identity TEXT, seen_at REAL NOT NULL, UNIQUE(library, rel_path));
        CREATE TABLE IF NOT EXISTS library_files (
            title_id INTEGER NOT NULL, rel_path TEXT NOT NULL, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, dev INTEGER NOT NULL, ino INTEGER NOT NULL,
            PRIMARY KEY(title_id, rel_path));
        CREATE TABLE IF NOT EXISTS manifest_torrents (
            infohash TEXT PRIMARY KEY CHECK(length(infohash)=40),
            name TEXT NOT NULL, release_group TEXT, total_bytes INTEGER NOT NULL,
            piece_length INTEGER NOT NULL, files TEXT NOT NULL, private INTEGER NOT NULL,
            protection TEXT NOT NULL, magnet TEXT, info BLOB, descriptor_sha256 TEXT NOT NULL,
            source TEXT NOT NULL, first_seen REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS manifest_links (
            title_id INTEGER NOT NULL, infohash TEXT NOT NULL, strategy TEXT NOT NULL,
            mapping TEXT NOT NULL, PRIMARY KEY(title_id, infohash));
        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY, title_id INTEGER NOT NULL, infohash TEXT NOT NULL,
            verdict TEXT NOT NULL, files TEXT NOT NULL, pieces_total INTEGER NOT NULL,
            pieces_ok INTEGER NOT NULL, bytes_read INTEGER NOT NULL,
            started_at REAL NOT NULL, finished_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS verifications_link
            ON verifications(title_id, infohash, finished_at);
        CREATE TABLE IF NOT EXISTS swarm_checks (
            id INTEGER PRIMARY KEY, infohash TEXT NOT NULL, checked_at REAL NOT NULL,
            seeders INTEGER, leechers INTEGER, status TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quarantine_plans (
            plan_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, plan TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed');
    """,
    # Library quality foundation (wave 0): shared limiter, SeaDex/AniList caches,
    # title identities, probe cache, audit reports. Policy files and the AniList
    # token are files, never rows.
    3: """
        CREATE TABLE IF NOT EXISTS rate_limits (
          service TEXT PRIMARY KEY CHECK(length(service) BETWEEN 1 AND 32),   -- 'anilist','nyaa','seadex'
          next_ok REAL NOT NULL DEFAULT 0,          -- wall-clock epoch: earliest start of the next request
          interval REAL NOT NULL,                   -- seconds between request starts
          penalty_until REAL NOT NULL DEFAULT 0,    -- from 429 Retry-After / X-RateLimit-Reset
          observed_limit INTEGER, remaining INTEGER, reset_at REAL,
          updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_state (
          source TEXT PRIMARY KEY,                  -- 'seadex', 'anilist:list:<user_id>', 'anilist:favourites:<user_id>'
          cursor TEXT, last_attempt REAL, last_ok REAL, last_full_ok REAL,
          status TEXT NOT NULL DEFAULT 'never', detail TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS seadex_entries (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 32),
          anilist_id INTEGER NOT NULL CHECK(anilist_id > 0),
          incomplete INTEGER NOT NULL CHECK(incomplete IN (0,1)),
          notes TEXT NOT NULL DEFAULT '', theoretical_best TEXT NOT NULL DEFAULT '',
          comparison TEXT NOT NULL DEFAULT '[]',    -- JSON list of https URLs (bounded)
          updated TEXT NOT NULL, synced_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS seadex_entries_anilist ON seadex_entries(anilist_id);
        CREATE TABLE IF NOT EXISTS seadex_torrents (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 32),
          infohash TEXT CHECK(infohash IS NULL OR length(infohash)=40),   -- NULL unless 40-hex
          release_group TEXT, tracker TEXT NOT NULL,                       -- tracker value is data, verbatim, bounded
          public INTEGER NOT NULL CHECK(public IN (0,1)),                  -- see C: Nyaa view URL + 40-hex only
          nyaa_id INTEGER, is_best INTEGER NOT NULL CHECK(is_best IN (0,1)),
          dual_audio INTEGER NOT NULL CHECK(dual_audio IN (0,1)), tags TEXT NOT NULL DEFAULT '[]',
          files TEXT, total_bytes INTEGER, video_files INTEGER, median_video_bytes INTEGER, files_fetched_at REAL,
          updated TEXT NOT NULL, synced_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS seadex_torrents_infohash ON seadex_torrents(infohash);
        CREATE TABLE IF NOT EXISTS seadex_entry_torrents (
          entry_id TEXT NOT NULL, torrent_id TEXT NOT NULL, PRIMARY KEY(entry_id, torrent_id));
        CREATE TABLE IF NOT EXISTS anilist_users (
          user_id INTEGER PRIMARY KEY CHECK(user_id > 0), name TEXT NOT NULL,
          is_viewer INTEGER NOT NULL DEFAULT 0 CHECK(is_viewer IN (0,1)), score_format TEXT,
          list_fetched_at REAL, favourites_fetched_at REAL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS anilist_media (
          anilist_id INTEGER PRIMARY KEY CHECK(anilist_id > 0),
          title_romaji TEXT, title_english TEXT, title_native TEXT, synonyms TEXT NOT NULL DEFAULT '[]',
          format TEXT, episodes INTEGER, duration INTEGER, status TEXT, season TEXT, season_year INTEGER,
          average_score INTEGER, popularity INTEGER, favourites INTEGER, is_adult INTEGER,
          next_airing_episode INTEGER, next_airing_at INTEGER, fetched_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS anilist_list_entries (
          user_id INTEGER NOT NULL, anilist_id INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('CURRENT','PLANNING','COMPLETED','DROPPED','PAUSED','REPEATING')),
          score REAL CHECK(score IS NULL OR (score > 0 AND score <= 10)),   -- POINT_10_DECIMAL; 0 => NULL
          progress INTEGER, repeat INTEGER, updated_at INTEGER, fetched_at REAL NOT NULL,
          PRIMARY KEY(user_id, anilist_id));
        CREATE TABLE IF NOT EXISTS anilist_favourites (
          user_id INTEGER NOT NULL, anilist_id INTEGER NOT NULL, position INTEGER, fetched_at REAL NOT NULL,
          PRIMARY KEY(user_id, anilist_id));
        CREATE TABLE IF NOT EXISTS title_identities (
          library TEXT NOT NULL, title TEXT NOT NULL,                 -- title key "<root label>/<entry>"
          anilist_id INTEGER CHECK(anilist_id IS NULL OR anilist_id > 0),
          status TEXT NOT NULL CHECK(status IN ('resolved','pinned','ambiguous','unresolved','unavailable','not_anime')),
          source TEXT NOT NULL CHECK(source IN ('list','alias','owner')),
          query TEXT, matched TEXT, candidates TEXT NOT NULL DEFAULT '[]',   -- JSON list of candidate AniList IDs
          updated_at REAL NOT NULL, PRIMARY KEY(library, title),
          CHECK((status IN ('resolved','pinned')) = (anilist_id IS NOT NULL)));
        CREATE TABLE IF NOT EXISTS probe_results (
          title_id INTEGER NOT NULL, rel_path TEXT NOT NULL,          -- same keys as library_files
          size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,           -- cache validity (dev/ino deliberately excluded)
          probe_version INTEGER NOT NULL, status TEXT NOT NULL,       -- ok|partial|unsupported_container|invalid|unsafe|changed|io_error
          result TEXT NOT NULL, bytes_read INTEGER NOT NULL, probed_at REAL NOT NULL,
          PRIMARY KEY(title_id, rel_path));
        CREATE TABLE IF NOT EXISTS audit_reports (
          id INTEGER PRIMARY KEY, library TEXT NOT NULL, created_at REAL NOT NULL,
          report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64), report TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS audit_reports_library ON audit_reports(library, created_at);
    """,
    # Absent titles: rows that a complete build no longer saw (for example titles recorded
    # under root labels that were retired). A side table rather than ALTER TABLE so the
    # migration stays idempotent DDL like the others; a row here hides the title everywhere,
    # deleting the row (a build that sees the title again) brings it back.
    4: """
        CREATE TABLE IF NOT EXISTS library_absent (
            title_id INTEGER PRIMARY KEY, absent_at REAL NOT NULL);
    """,
}


class Store:
    def __init__(self, root: Path):
        ensure_dir(root)
        os.chmod(root, 0o700)
        self.root = root
        path = root / "state.sqlite3"
        if path.exists() or path.is_symlink():
            regular_file(path)
        self.db = sqlite3.connect(path, timeout=5)
        os.chmod(path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (version INTEGER NOT NULL);
            INSERT INTO meta SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM meta);
            CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                infohash TEXT PRIMARY KEY, media_key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS descriptors (
                infohash TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, infohash TEXT NOT NULL,
                state TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched_at REAL NOT NULL);
        """)
        self.db.commit()
        self._migrate()

    def _version(self) -> int:
        return self.db.execute("SELECT version FROM meta").fetchone()[0]

    def _migrate(self):
        """Bring the database to SCHEMA_VERSION, one atomic step per version.

        A newer or unknown version is refused so an older build never writes to a
        schema it does not understand. Concurrent openers serialise on BEGIN
        IMMEDIATE; the loser re-runs idempotent DDL and its guarded UPDATE is a no-op.
        """
        version = self._version()
        if not 1 <= version <= SCHEMA_VERSION:
            raise KuraError("state_version", "Unsupported local database version")
        for step in range(version + 1, SCHEMA_VERSION + 1):
            try:
                self.db.executescript(
                    f"BEGIN IMMEDIATE;{MIGRATIONS[step]}"
                    f"UPDATE meta SET version={step} WHERE version={step - 1};COMMIT;")
            except sqlite3.Error:
                if self.db.in_transaction:
                    self.db.rollback()
                raise KuraError("state_version", "Local database migration failed")
        if self._version() != SCHEMA_VERSION:
            raise KuraError("state_version", "Unsupported local database version")

    @contextmanager
    def lock(self):
        path = self.root / "operation.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise KuraError("busy", "Another Kura mutation is running")
            # Closing our FD releases the lock only after any inheriting child
            # also closes it. Explicit LOCK_UN would unlock that child\'s shared
            # open-file description if supervision were interrupted during cleanup.
            yield f.fileno()

    def prefs(self) -> Preferences:
        row = self.db.execute("SELECT value FROM preferences WHERE id=1").fetchone()
        data = json.loads(row[0]) if row else {}
        data["groups"] = tuple(data.get("groups", ()))
        return validate_prefs(Preferences(**data))

    def save_prefs(self, p: Preferences):
        validate_prefs(p)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES (1,?)",
                            (json.dumps(asdict(p)),))

    def jobs(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT value FROM jobs ORDER BY rowid")]

    def alias(self, key: str) -> dict | None:
        """Cached alias resolution or None. Cache misses and errors are never fatal."""
        try:
            row = self.db.execute("SELECT value, fetched_at FROM aliases WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        try:
            return {**json.loads(row[0]), "fetched_at": row[1]}
        except (TypeError, ValueError):
            return None

    def save_alias(self, key: str, value: dict, fetched_at: float):
        try:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?)",
                                (key, json.dumps(value), fetched_at))
        except sqlite3.Error:
            pass  # A cache write must never fail a read-only discovery command.

    def descriptor(self, infohash: str) -> bytes | None:
        row = self.db.execute("SELECT value FROM descriptors WHERE infohash=?", (infohash,)).fetchone()
        return row[0] if row else None

    def record(self, job: dict, descriptor: bytes | None = None):
        job["updated_at"] = time.time()
        with self.db:
            if descriptor is not None:
                self.db.execute("INSERT OR REPLACE INTO descriptors VALUES (?,?)",
                                (job["infohash"], descriptor))
            self.db.execute("INSERT INTO jobs VALUES (?,?,?) ON CONFLICT(infohash) DO UPDATE SET value=excluded.value",
                            (job["infohash"], job["media_key"], json.dumps(job)))
            self.db.execute("INSERT INTO events(at,infohash,state,detail) VALUES (?,?,?,?)",
                            (job["updated_at"], job["infohash"], job["state"],
                             json.dumps({"error": job.get("error"), "authorization": job.get("authorization")})))

    def recover(self):
        for job in self.jobs():
            if job["state"] == "acquiring":
                job.update(state="interrupted", error="Previous process ended without a verified receipt")
                self.record(job)
