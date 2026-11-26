"""State schema v5 (multi-part titles): migration from a v4 database, idempotence, constraints.
Synthetic data only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from kura.models import KuraError
import kura.store as store_module
from kura.store import MIGRATIONS, SCHEMA_VERSION, Store

V5_TABLES = {"title_parts", "anilist_relations", "anilist_relation_fetches"}


def tables(path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def version(path) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT version FROM meta").fetchone()[0]
    finally:
        db.close()


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "state"
        self.db_path = self.root / "state.sqlite3"

    def build_v4(self):
        """A database exactly as a v4 build left it, with rows in every kind of table."""
        self.root.mkdir(mode=0o700)
        db = sqlite3.connect(self.db_path)
        db.executescript("""
            CREATE TABLE meta (version INTEGER NOT NULL);
            INSERT INTO meta VALUES (1);
            CREATE TABLE preferences (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE jobs (infohash TEXT PRIMARY KEY, media_key TEXT UNIQUE NOT NULL, value TEXT NOT NULL);
            CREATE TABLE descriptors (infohash TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY, at REAL NOT NULL, infohash TEXT NOT NULL,
                state TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE aliases (key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched_at REAL NOT NULL);
        """)
        for step in (2, 3, 4):
            db.executescript(MIGRATIONS[step])
        db.execute("UPDATE meta SET version=4")
        db.execute("INSERT INTO preferences VALUES (1, ?)", (json.dumps({"min_seeders": 7}),))
        db.execute("INSERT INTO library_titles(library, rel_path, kind, root_dev, file_count, total_bytes, "
                   "newest_mtime_ns, seen_at) VALUES ('lib', 'main/Example Show', 'dir', 1, 2, 30, 5, 1.0)")
        db.execute("INSERT INTO library_files VALUES (1, 'ep1.mkv', 10, 5, 1, 7)")
        db.execute("INSERT INTO library_absent VALUES (1, 2.0)")
        db.execute("INSERT INTO title_identities VALUES ('lib', 'main/Example Show', 20464, 'pinned', 'owner',"
                   " NULL, NULL, '[1,2]', 3.0)")
        db.execute("INSERT INTO anilist_media(anilist_id, title_romaji, synonyms, fetched_at)"
                   " VALUES (20464, 'Example Show', '[]', 4.0)")
        db.commit()
        db.close()


class SchemaV5(Case):
    def test_target_version(self):
        self.assertEqual(SCHEMA_VERSION, 5)
        self.assertEqual(sorted(MIGRATIONS), [2, 3, 4, 5])

    def test_fresh_database_has_the_part_tables(self):
        Store(self.root).db.close()
        self.assertEqual(version(self.db_path), 5)
        self.assertTrue(V5_TABLES <= tables(self.db_path))

    def test_v4_migrates_with_every_row_intact(self):
        self.build_v4()
        self.assertFalse(V5_TABLES & tables(self.db_path))
        store = Store(self.root)
        try:
            self.assertEqual(version(self.db_path), 5)
            self.assertEqual(store.prefs().min_seeders, 7)
            q = lambda sql: store.db.execute(sql).fetchall()
            self.assertEqual(q("SELECT rel_path, file_count FROM library_titles"), [("main/Example Show", 2)])
            self.assertEqual(q("SELECT title_id FROM library_absent"), [(1,)])
            self.assertEqual(q("SELECT title, anilist_id, status, source, candidates FROM title_identities"),
                             [("main/Example Show", 20464, "pinned", "owner", "[1,2]")])
            self.assertEqual(q("SELECT anilist_id FROM anilist_media"), [(20464,)])
            for table in V5_TABLES:     # migration copies and invents nothing
                self.assertEqual(q(f"SELECT COUNT(*) FROM {table}"), [(0,)])
        finally:
            store.db.close()

    def test_reopen_and_rerunning_the_step_are_harmless(self):
        self.build_v4()
        Store(self.root).db.close()
        store = Store(self.root)
        try:
            store.db.execute("INSERT INTO title_parts VALUES ('lib','t',20464,'auto','owner',1,NULL,NULL,1.0)")
            store.db.commit()
            store.db.executescript(MIGRATIONS[5])      # what a concurrent opener that lost the race does
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM title_parts").fetchone()[0], 1)
        finally:
            store.db.close()

    def test_concurrent_openers_of_a_v4_database(self):
        self.build_v4()
        results, errors, barrier = [], [], threading.Barrier(4)

        def opener():
            try:
                barrier.wait()
                store = Store(self.root)
                results.append(store.db.execute("SELECT version FROM meta").fetchone()[0])
                store.db.close()
            except Exception as e:      # collected and asserted below
                errors.append(e)
        threads = [threading.Thread(target=opener) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((errors, results), ([], [5] * 4))

    def test_newer_version_is_refused_and_left_untouched(self):
        self.build_v4()
        db = sqlite3.connect(self.db_path)
        db.execute("UPDATE meta SET version=6")
        db.commit()
        db.close()
        with self.assertRaises(KuraError) as cm:
            Store(self.root)
        self.assertEqual(cm.exception.code, "state_version")
        self.assertEqual(version(self.db_path), 6)
        self.assertFalse(V5_TABLES & tables(self.db_path))

    def test_failed_step_rolls_back_to_v4(self):
        self.build_v4()
        with patch.dict(store_module.MIGRATIONS, {5: MIGRATIONS[5] + "CREATE TABLE title_parts (x);"}):
            with self.assertRaises(KuraError) as cm:        # the second CREATE is not IF NOT EXISTS
                Store(self.root)
        self.assertEqual(cm.exception.code, "state_version")
        self.assertEqual(version(self.db_path), 4)
        self.assertFalse(V5_TABLES & tables(self.db_path))
        Store(self.root).db.close()                         # the real migration then succeeds
        self.assertEqual(version(self.db_path), 5)

    def test_constraints_hold(self):
        store = Store(self.root)
        db = store.db
        try:
            ok = ("lib", "t", 20464, "all", "owner", 1, None, None, 1.0)
            db.execute("INSERT INTO title_parts VALUES (?,?,?,?,?,?,?,?,?)", ok)
            bad = [ok[:2] + (0,) + ok[3:], ok[:2] + (1_000_000_000,) + ok[3:], ok[:3] + ("",) + ok[4:],
                   ok[:3] + ("x" * 513,) + ok[4:], ok[:4] + ("guess",) + ok[5:], ok[:5] + (0,) + ok[6:],
                   ok[:6] + (0, None, 1.0), ok[:7] + (0, 1.0), ok]        # last: duplicate key
            for row in bad:
                with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO title_parts VALUES (?,?,?,?,?,?,?,?,?)", row)
            db.execute("INSERT INTO title_parts VALUES ('lib','t',20464,'auto','relations',2,NULL,20464,1.0)")
            for sql in ("INSERT INTO anilist_relations VALUES (0, 1, 'SEQUEL')",
                        "INSERT INTO anilist_relations VALUES (1, 2, '')",
                        "INSERT INTO anilist_relation_fetches VALUES (1, 1.0, 'maybe')"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
