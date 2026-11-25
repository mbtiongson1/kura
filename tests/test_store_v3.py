"""State schema v3 (library quality foundation) and the CLI command registry. Synthetic data only."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import kura.cli as cli
from kura.models import KuraError
from kura.store import MIGRATIONS, SCHEMA_VERSION, Store

from test_manifest_store import V1, NEW_TABLES, tables, version

V3_TABLES = {"rate_limits", "sync_state", "seadex_entries", "seadex_torrents", "seadex_entry_torrents",
             "anilist_users", "anilist_media", "anilist_list_entries", "anilist_favourites",
             "title_identities", "probe_results", "audit_reports"}


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "state"
        self.db_path = self.root / "state.sqlite3"

    def build_v2(self):
        self.root.mkdir(mode=0o700)
        db = sqlite3.connect(self.db_path)
        db.executescript(V1)
        db.executescript(MIGRATIONS[2])
        db.execute("UPDATE meta SET version=2")
        db.execute("INSERT INTO preferences VALUES (1, ?)", (json.dumps({"min_seeders": 7}),))
        db.execute("INSERT INTO library_titles(library, rel_path, kind, root_dev, file_count, total_bytes, "
                   "newest_mtime_ns, seen_at) VALUES ('lib', 'main/Kura Sample Series', 'dir', 1, 2, 30, 5, 1.0)")
        db.execute("INSERT INTO library_files VALUES (1, 'ep1.mkv', 10, 5, 1, 7)")
        db.execute("INSERT INTO manifest_torrents VALUES (?, 'Kura Sample Series', NULL, 30, 16384, '[]', 0, "
                   "'public', NULL, NULL, ?, 'test', 1.0, 1.0)", ("a" * 40, "d" * 64))
        db.execute("INSERT INTO manifest_links VALUES (1, ?, 'exact', '{}')", ("a" * 40,))
        db.commit()
        db.close()


class SchemaV3(Case):
    def test_target_version(self):
        self.assertEqual(SCHEMA_VERSION, 3)
        self.assertEqual(sorted(MIGRATIONS), [2, 3])

    def test_fresh_database_is_v3(self):
        store = Store(self.root)
        store.db.close()
        self.assertEqual(version(self.db_path), 3)
        self.assertTrue((V3_TABLES | NEW_TABLES) <= tables(self.db_path))

    def test_v2_database_migrates_intact(self):
        self.build_v2()
        self.assertFalse(V3_TABLES & tables(self.db_path))
        store = Store(self.root)
        try:
            self.assertEqual(version(self.db_path), 3)
            self.assertTrue(V3_TABLES <= tables(self.db_path))
            self.assertEqual(store.prefs().min_seeders, 7)
            self.assertEqual(store.db.execute("SELECT library, rel_path, file_count FROM library_titles").fetchall(),
                             [("lib", "main/Kura Sample Series", 2)])
            self.assertEqual(store.db.execute("SELECT rel_path, size FROM library_files").fetchall(), [("ep1.mkv", 10)])
            self.assertEqual(store.db.execute("SELECT infohash, name FROM manifest_torrents").fetchall(),
                             [("a" * 40, "Kura Sample Series")])
            self.assertEqual(store.db.execute("SELECT strategy FROM manifest_links").fetchall(), [("exact",)])
        finally:
            store.db.close()

    def test_reopen_is_idempotent(self):
        self.build_v2()
        Store(self.root).db.close()
        store = Store(self.root)
        store.db.execute("INSERT INTO rate_limits(service, interval, updated_at) VALUES ('anilist', 2.0, 1.0)")
        store.db.commit()
        store.db.close()
        store = Store(self.root)
        try:
            self.assertEqual(store.db.execute("SELECT service FROM rate_limits").fetchall(), [("anilist",)])
        finally:
            store.db.close()

    def test_v3_migration_is_rerunnable(self):
        store = Store(self.root)
        try:
            store.db.executescript(MIGRATIONS[3])
        finally:
            store.db.close()

    def test_newer_version_is_refused_and_untouched(self):
        Store(self.root).db.close()
        db = sqlite3.connect(self.db_path)
        db.execute("UPDATE meta SET version=?", (SCHEMA_VERSION + 1,))
        db.commit()
        db.close()
        with self.assertRaises(KuraError) as ctx:
            Store(self.root)
        self.assertEqual(ctx.exception.code, "state_version")
        self.assertEqual(version(self.db_path), SCHEMA_VERSION + 1)

    def test_failed_v3_step_rolls_back_and_keeps_v2(self):
        self.build_v2()
        broken = dict(MIGRATIONS)
        broken[3] = "CREATE TABLE half_done (a INTEGER); CREATE TABLE half_done (a INTEGER);"
        with patch("kura.store.MIGRATIONS", broken):
            with self.assertRaises(KuraError):
                Store(self.root)
        self.assertEqual(version(self.db_path), 2)
        self.assertNotIn("half_done", tables(self.db_path))
        self.assertFalse(V3_TABLES & tables(self.db_path))
        Store(self.root).db.close()
        self.assertEqual(version(self.db_path), 3)

    def test_two_stores_open_at_once(self):
        for scenario in ("fresh", "v2"):
            with self.subTest(scenario=scenario):
                self.setUp()
                if scenario == "v2":
                    self.build_v2()
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
                self.assertEqual((errors, results), ([], [3] * 4))

    def test_table_constraints_hold(self):
        store = Store(self.root)
        db = store.db
        try:
            def bad(sql, *params):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql, params)
            bad("INSERT INTO seadex_entries VALUES ('e', 0, 0, '', '', '[]', 'u', 1.0)")
            bad("INSERT INTO seadex_torrents(id, infohash, tracker, public, is_best, dual_audio, updated, synced_at) "
                "VALUES ('t', 'abc', 'ExampleTracker', 0, 0, 0, 'u', 1.0)")
            bad("INSERT INTO anilist_list_entries VALUES (1, 999000001, 'CURRENT', 0, 1, 0, 1, 1.0)")
            bad("INSERT INTO anilist_list_entries VALUES (1, 999000001, 'BOGUS', 5, 1, 0, 1, 1.0)")
            bad("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at) "
                "VALUES ('l', 't', NULL, 'resolved', 'list', 1.0)")
            bad("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at) "
                "VALUES ('l', 't', 999000001, 'ambiguous', 'list', 1.0)")
            bad("INSERT INTO rate_limits(service, interval, updated_at) VALUES ('', 1.0, 1.0)")
            bad("INSERT INTO audit_reports(library, created_at, report_sha256, report) VALUES ('l', 1.0, 'x', '{}')")
            db.execute("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at) "
                       "VALUES ('l', 'main/Kura Sample Series', 999000001, 'resolved', 'list', 1.0)")
            db.execute("INSERT INTO title_identities(library, title, status, source, updated_at) "
                       "VALUES ('l', 'main/Other', 'ambiguous', 'alias', 1.0)")
        finally:
            db.close()


class Registry(Case):
    STUBS = ("cli_seadex", "cli_probe", "cli_identity", "cli_audit")

    def test_registry_order_and_contract(self):
        self.assertEqual([m.__name__.rsplit(".", 1)[1] for m in cli.COMMAND_MODULES],
                         ["cli_handover", "cli_anilist", *self.STUBS])
        for module in cli.COMMAND_MODULES:
            self.assertTrue(callable(module.register) and callable(module.dispatch))

    def test_stubs_register_nothing_and_handle_nothing(self):
        for name in self.STUBS:
            module = getattr(cli, name)
            with self.subTest(module=name):
                p = cli.Parser(prog="kura")
                sub = p.add_subparsers(dest="command", parser_class=cli.Parser)
                module.register(sub, cli.Parser)
                self.assertEqual(sub.choices, {})
                self.assertEqual(module.dispatch(argparse.Namespace(command="search"), lambda: 1 / 0), (False, None))

    def test_command_set_is_unchanged(self):
        sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(sorted(sub.choices),
                         ["aliases", "anilist", "download", "downloads", "library", "manifest", "prefs", "search", "torrent"])

    def test_dispatch_falls_through_in_order(self):
        calls = []

        def fake(name, handled):
            class M:
                @staticmethod
                def register(sub, parser_class):
                    pass

                @staticmethod
                def dispatch(args, open_store):
                    calls.append(name)
                    return (handled, name) if handled else (False, None)
            return M

        with patch.object(cli, "COMMAND_MODULES", (fake("a", False), fake("b", True), fake("c", True))):
            self.assertEqual(cli.execute(argparse.Namespace(command="x")), "b")
        self.assertEqual(calls, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
