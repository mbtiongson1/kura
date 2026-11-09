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
        """)
        if self.db.execute("SELECT version FROM meta").fetchone()[0] != 1:
            raise KuraError("state_version", "Unsupported local database version")
        self.db.commit()

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
