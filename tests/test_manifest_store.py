"""State schema v2 migration and the manifest repository. Synthetic data only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from kura.manifest_store import ManifestRepo
from kura.models import KuraError, Preferences
from kura.store import MIGRATIONS, SCHEMA_VERSION, Store

V1 = """
    CREATE TABLE meta (version INTEGER NOT NULL);
    INSERT INTO meta VALUES (1);
    CREATE TABLE preferences (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE jobs (infohash TEXT PRIMARY KEY, media_key TEXT UNIQUE NOT NULL, value TEXT NOT NULL);
    CREATE TABLE descriptors (infohash TEXT PRIMARY KEY, value BLOB NOT NULL);
    CREATE TABLE events (id INTEGER PRIMARY KEY, at REAL NOT NULL, infohash TEXT NOT NULL,
        state TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE aliases (key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched_at REAL NOT NULL);
"""
NEW_TABLES = {"library_titles", "library_files", "manifest_torrents", "manifest_links",
              "verifications", "swarm_checks", "quarantine_plans"}
H1, H2 = "a" * 40, "b" * 40


def tables(path: Path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def version(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT version FROM meta").fetchone()[0]
    finally:
        db.close()


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "state"
        self.db_path = self.root / "state.sqlite3"
        paths = patch("kura.safety.private_root", return_value=self.root)
        paths.start()
        self.addCleanup(paths.stop)

    def build_v1(self):
        self.root.mkdir(mode=0o700)
        db = sqlite3.connect(self.db_path)
        db.executescript(V1)
        db.execute("INSERT INTO preferences VALUES (1, ?)", (json.dumps({"min_seeders": 7}),))
        db.execute("INSERT INTO jobs VALUES ('h', 'k', '{\"state\": \"done\"}')")
        db.execute("INSERT INTO descriptors VALUES ('h', ?)", (b"\x00blob",))
        db.execute("INSERT INTO events(at, infohash, state, detail) VALUES (1.5, 'h', 'done', '{}')")
        db.execute("INSERT INTO aliases VALUES ('k', '{\"id\": 1}', 2.5)")
        db.commit()
        db.close()


class Migration(TempCase):
    def test_fresh_database_is_v2(self):
        store = Store(self.root)
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(store.db.execute("SELECT version FROM meta").fetchone()[0], 2)
        self.assertTrue(NEW_TABLES <= tables(self.db_path))
        self.assertEqual(oct(self.db_path.stat().st_mode & 0o777), "0o600")

    def test_v1_migrates_with_data_intact(self):
        self.build_v1()
        store = Store(self.root)
        self.assertEqual(version(self.db_path), 2)
        self.assertTrue(NEW_TABLES <= tables(self.db_path))
        self.assertEqual(store.prefs().min_seeders, 7)
        self.assertEqual(store.jobs(), [{"state": "done"}])
        self.assertEqual(store.descriptor("h"), b"\x00blob")
        self.assertEqual(store.db.execute("SELECT at, state FROM events").fetchall(), [(1.5, "done")])
        self.assertEqual(store.alias("k"), {"id": 1, "fetched_at": 2.5})

    def test_reopen_is_idempotent(self):
        self.build_v1()
        Store(self.root)
        store = Store(self.root)
        repo = ManifestRepo(store.db)
        repo.save_inventory("lib", 1, [title("A")], 1.0)
        store = Store(self.root)
        self.assertEqual(version(self.db_path), 2)
        self.assertEqual(len(ManifestRepo(store.db).titles("lib")), 1)

    def test_newer_or_invalid_version_refused(self):
        for bad in (3, 0):
            with self.subTest(version=bad):
                self.build_v1() if not self.root.exists() else None
                db = sqlite3.connect(self.db_path)
                db.execute("UPDATE meta SET version=?", (bad,))
                db.commit()
                db.close()
                with self.assertRaises(KuraError) as ctx:
                    Store(self.root)
                self.assertEqual(ctx.exception.code, "state_version")
                self.assertEqual(version(self.db_path), bad)

    def test_failed_migration_rolls_back_atomically(self):
        self.build_v1()
        broken = {2: "CREATE TABLE half_done (a INTEGER); CREATE TABLE half_done (a INTEGER);"}
        with patch("kura.store.MIGRATIONS", broken):
            with self.assertRaises(KuraError) as ctx:
                Store(self.root)
        self.assertEqual(ctx.exception.code, "state_version")
        self.assertEqual(version(self.db_path), 1)
        self.assertNotIn("half_done", tables(self.db_path))
        self.assertFalse(NEW_TABLES & tables(self.db_path))
        # A later healthy open still migrates the same file.
        Store(self.root)
        self.assertEqual(version(self.db_path), 2)

    def test_migrations_cover_every_version(self):
        self.assertEqual(sorted(MIGRATIONS), list(range(2, SCHEMA_VERSION + 1)))

    def test_concurrent_opens_both_work(self):
        for scenario in ("fresh", "v1"):
            with self.subTest(scenario=scenario):
                self.root = Path(self._tmp.name).resolve() / scenario
                self.db_path = self.root / "state.sqlite3"
                if scenario == "v1":
                    self.build_v1()
                barrier = threading.Barrier(4)
                results, errors = [], []

                def open_one():
                    try:
                        barrier.wait(5)
                        store = Store(self.root)
                        results.append(store.db.execute("SELECT version FROM meta").fetchone()[0])
                        store.db.close()
                    except Exception as exc:  # pragma: no cover - failure path
                        errors.append(repr(exc))

                threads = [threading.Thread(target=open_one) for _ in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
                self.assertEqual(errors, [])
                self.assertEqual(results, [2] * 4)
                self.assertEqual(version(self.db_path), 2)


def title(rel="Kura Sample Series", files=None, **extra):
    files = files if files is not None else [
        {"rel_path": "ep01.mkv", "size": 10, "mtime_ns": 5, "dev": 1, "ino": 100},
        {"rel_path": "ep02.mkv", "size": 20, "mtime_ns": 6, "dev": 1, "ino": 101}]
    return {"rel_path": rel, "kind": "dir", "total_bytes": sum(f["size"] for f in files),
            "newest_mtime_ns": 6, "blocked": [], "files": files, **extra}


def torrent(infohash=H1, **extra):
    row = {"infohash": infohash, "name": "Kura Sample Series", "release_group": "Kura",
           "total_bytes": 30, "piece_length": 16384,
           "files": [{"index": 0, "path": ["Kura Sample Series", "ep01.mkv"], "length": 10,
                      "offset": 0, "pad": False}],
           "private": False, "protection": {"protected": False, "reasons": [], "policy_sha256": "0" * 64},
           "magnet": "magnet:?xt=urn:btih:" + infohash, "info": b"d4:name1:xe",
           "descriptor_sha256": "c" * 64, "source": "synthetic", "first_seen": 1.0, "updated_at": 2.0}
    row.update(extra)
    return row


class Repo(TempCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root)
        self.repo = ManifestRepo(self.store.db)

    def test_inventory_round_trip_and_reseen(self):
        ids = self.repo.save_inventory("lib", 7, [title("A"), title("B", kind="dir")], 10.0)
        self.assertEqual(set(ids), {"A", "B"})
        rows = self.repo.titles("lib")
        self.assertEqual([r["rel_path"] for r in rows], ["A", "B"])
        a = rows[0]
        self.assertEqual((a["id"], a["kind"], a["root_dev"], a["file_count"], a["total_bytes"],
                          a["newest_mtime_ns"], a["blocked"], a["identity"], a["seen_at"]),
                         (ids["A"], "dir", 7, 2, 30, 6, [], None, 10.0))
        self.assertEqual(self.repo.titles("other"), [])
        self.assertEqual(self.repo.title_files(ids["A"])[0],
                         {"rel_path": "ep01.mkv", "size": 10, "mtime_ns": 5, "dev": 1, "ino": 100})

        # Re-save only A: ids stable, files replaced, B keeps rows and its old seen_at.
        newer = title("A", files=[{"rel_path": "ep03.mkv", "size": 1, "mtime_ns": 9, "dev": 1, "ino": 7}],
                      blocked=["symlink"], identity={"id": 42})
        ids2 = self.repo.save_inventory("lib", 7, [newer], 20.0)
        self.assertEqual(ids2["A"], ids["A"])
        self.assertEqual([f["rel_path"] for f in self.repo.title_files(ids["A"])], ["ep03.mkv"])
        self.assertEqual(len(self.repo.title_files(ids["B"])), 2)
        by = {r["rel_path"]: r for r in self.repo.titles("lib")}
        self.assertEqual((by["A"]["seen_at"], by["A"]["blocked"], by["A"]["identity"]),
                         (20.0, ["symlink"], {"id": 42}))
        self.assertEqual(by["B"]["seen_at"], 10.0)

        # Omitted identity keeps it; explicit None clears it.
        self.repo.save_inventory("lib", 7, [title("A")], 30.0)
        self.assertEqual({r["rel_path"]: r for r in self.repo.titles("lib")}["A"]["identity"], {"id": 42})
        self.repo.save_inventory("lib", 7, [title("A", identity=None)], 31.0)
        self.assertIsNone({r["rel_path"]: r for r in self.repo.titles("lib")}["A"]["identity"])

    def test_inventory_is_atomic_and_validates(self):
        with self.assertRaises(KuraError):
            self.repo.save_inventory("lib", 1, [title("A"), {"rel_path": "bad"}], 1.0)
        self.assertEqual(self.repo.titles("lib"), [])
        with self.assertRaises(KuraError) as ctx:
            self.repo.save_inventory("lib", 1, [title("A", kind="symlink")], 1.0)
        self.assertEqual(ctx.exception.code, "invalid_manifest")
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.repo.titles("lib"), [])

    def test_torrent_round_trip_and_upsert(self):
        self.assertIsNone(self.repo.torrent(H1))
        self.repo.upsert_torrent(torrent())
        got = self.repo.torrent(H1)
        self.assertEqual(got["name"], "Kura Sample Series")
        self.assertIs(got["private"], False)
        self.assertEqual(got["info"], b"d4:name1:xe")
        self.assertEqual(got["files"][0]["path"], ["Kura Sample Series", "ep01.mkv"])
        self.assertEqual(got["protection"]["protected"], False)
        self.assertEqual((got["first_seen"], got["updated_at"]), (1.0, 2.0))
        self.repo.upsert_torrent(torrent(name="Renamed", first_seen=99.0, updated_at=3.0))
        got = self.repo.torrent(H1)
        self.assertEqual((got["name"], got["first_seen"], got["updated_at"]), ("Renamed", 1.0, 3.0))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM manifest_torrents").fetchone()[0], 1)

    def test_protected_or_private_torrent_never_keeps_magnet_or_info(self):
        prot = {"protected": True, "reasons": ["private_flag"], "policy_sha256": "0" * 64}
        for extra in ({"protection": prot}, {"private": True}):
            with self.subTest(extra=sorted(extra)):
                with self.assertRaises(KuraError) as ctx:
                    self.repo.upsert_torrent(torrent(**extra))
                self.assertEqual(ctx.exception.code, "invalid_manifest")
        self.repo.upsert_torrent(torrent(protection=prot, private=True, magnet=None, info=None))
        got = self.repo.torrent(H1)
        self.assertEqual((got["magnet"], got["info"], got["private"]), (None, None, True))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM manifest_torrents").fetchone()[0], 1)

    def test_torrent_validation(self):
        for bad in (torrent(infohash="A" * 40), torrent(infohash="a" * 39),
                    torrent(protection={"reasons": []}), torrent(info="text")):
            with self.assertRaises(KuraError):
                self.repo.upsert_torrent(bad)
        missing = torrent()
        del missing["name"]
        with self.assertRaises(KuraError):
            self.repo.upsert_torrent(missing)

    def test_links(self):
        ids = self.repo.save_inventory("lib", 1, [title("A")], 1.0)
        self.repo.upsert_torrent(torrent(H1))
        self.repo.upsert_torrent(torrent(H2))
        self.assertEqual(self.repo.links(ids["A"]), [])
        self.repo.link(ids["A"], H2, "root_dir", {0: ["Kura Sample Series", "ep01.mkv"]})
        self.repo.link(ids["A"], H1, "file_name", {"0": ["x"]})
        self.repo.link(ids["A"], H1, "nested_dir", {"0": ["y"]})  # replaces
        links = self.repo.links(ids["A"])
        self.assertEqual([(l["infohash"], l["strategy"]) for l in links],
                         [(H1, "nested_dir"), (H2, "root_dir")])
        self.assertEqual(links[0]["mapping"], {"0": ["y"]})
        self.assertEqual(links[1]["mapping"], {"0": ["Kura Sample Series", "ep01.mkv"]})
        with self.assertRaises(KuraError):
            self.repo.link(9999, H1, "root_dir", {})
        with self.assertRaises(KuraError):
            self.repo.link(ids["A"], "c" * 40, "root_dir", {})
        with self.assertRaises(KuraError):
            self.repo.link(ids["A"], H1, "root_dir", [])

    def test_verifications_are_append_only_history(self):
        def row(verdict, finished, **kw):
            return {"title_id": 1, "infohash": H1, "verdict": verdict,
                    "files": [{"path": "ep01.mkv", "status": "verified", "size": 10,
                               "mtime_ns": 5, "dev": 1, "ino": 100}],
                    "pieces_total": 4, "pieces_ok": 4, "bytes_read": 64,
                    "started_at": finished - 1, "finished_at": finished, **kw}
        self.assertIsNone(self.repo.latest_verification(1, H1))
        first = self.repo.add_verification(row("partial", 10.0))
        second = self.repo.add_verification(row("complete", 20.0))
        older = self.repo.add_verification(row("failed", 5.0))
        self.assertEqual(len({first, second, older}), 3)
        latest = self.repo.latest_verification(1, H1)
        self.assertEqual((latest["id"], latest["verdict"], latest["bytes_read"]), (second, "complete", 64))
        self.assertEqual(latest["files"][0]["ino"], 100)
        tie = self.repo.add_verification(row("interrupted", 20.0))
        self.assertEqual(self.repo.latest_verification(1, H1)["id"], tie)
        self.assertIsNone(self.repo.latest_verification(2, H1))
        self.assertIsNone(self.repo.latest_verification(1, H2))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM verifications").fetchone()[0], 4)
        with self.assertRaises(KuraError):
            self.repo.add_verification({"title_id": 1})

    def test_swarm_checks(self):
        self.assertIsNone(self.repo.latest_swarm(H1))
        self.repo.add_swarm_check({"infohash": H1, "checked_at": 5.0, "seeders": 9, "leechers": 1,
                                   "status": "ok", "detail": {"host": "tracker.example"}})
        newer = self.repo.add_swarm_check({"infohash": H1, "checked_at": 6.0, "status": "unavailable"})
        got = self.repo.latest_swarm(H1)
        self.assertEqual((got["id"], got["seeders"], got["leechers"], got["status"], got["detail"]),
                         (newer, None, None, "unavailable", {}))
        self.assertIsNone(self.repo.latest_swarm(H2))
        with self.assertRaises(KuraError):
            self.repo.add_swarm_check({"infohash": "nope", "checked_at": 1, "status": "ok"})

    def test_plans_are_idempotent(self):
        h = "d" * 64
        text = json.dumps({"plan_version": 1, "moves": []})
        self.assertIsNone(self.repo.plan(h))
        self.assertTrue(self.repo.save_plan(h, text, 1.0))
        self.assertFalse(self.repo.save_plan(h, json.dumps({"other": True}), 2.0))
        got = self.repo.plan(h)
        self.assertEqual(got, {"plan_hash": h, "created_at": 1.0,
                               "plan": {"plan_version": 1, "moves": []}, "status": "proposed"})
        for bad_hash, bad_text in (("short", text), (h, "not json"), (h, "[]")):
            with self.assertRaises(KuraError):
                self.repo.save_plan(bad_hash, bad_text, 1.0)

    def test_sql_metacharacters_are_data(self):
        evil = "x'); DROP TABLE library_titles;--"
        ids = self.repo.save_inventory(evil, 1, [title(evil)], 1.0)
        self.assertEqual(self.repo.titles(evil)[0]["rel_path"], evil)
        self.assertEqual(self.repo.links(ids[evil]), [])
        self.assertIn("library_titles", tables(self.db_path))

    def test_each_write_is_committed(self):
        self.repo.save_inventory("lib", 1, [title("A")], 1.0)
        self.assertFalse(self.store.db.in_transaction)
        other = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM library_titles").fetchone()[0], 1)
        finally:
            other.close()

    def test_v1_features_still_work_on_v2(self):
        self.store.save_prefs(Preferences())
        self.store.save_alias("k", {"id": 1}, 1.0)
        self.assertEqual(self.store.alias("k")["id"], 1)


if __name__ == "__main__":
    unittest.main()
