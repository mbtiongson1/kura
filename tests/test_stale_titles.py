"""Absent titles: retired root labels, the build that marks them, and everything that must ignore them.

Synthetic data only. The scenario is the real one in miniature: early builds used per-disk root
labels, later builds used share-level labels over the same files, and the old rows lingered.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import torrentgen
import kura.cli as cli
from kura import audit, identity, idlist, manifest, probe as probe_mod, quarantine
from kura.manifest_store import ManifestRepo
from kura.models import KuraError
from kura.store import MIGRATIONS, SCHEMA_VERSION, Store
from test_manifest import GOOD, Lab, snapshot
from test_manifest_store import V1, tables, version

DAY = 86400
OLD = int((time.time() - 60 * DAY) * 1e9)
NAMES = ("Kura Sample Series", "Kura Extra Show")


class Base(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        # Two old per-disk roots holding one title each, and the share that holds both.
        self.d1, self.d2 = self.lab.tmp / "d1", self.lab.tmp / "d2"
        self.d1.mkdir()
        self.d2.mkdir()
        self.dirs = {"disk1": self.d1, "disk2": self.d2, "anime": self.lab.lib, "main": self.lab.lib}
        for name, disk in zip(NAMES, (self.d1, self.d2)):
            self.lab.add(name, GOOD, where=disk)
            self.lab.add(name, GOOD)
            for where in (disk, self.lab.lib):
                for p in [where / name, *(where / name).rglob("*")]:
                    os.utime(p, ns=(OLD, OLD), follow_symlinks=False)
        self.repo = ManifestRepo(self.lab.store.db)
        self.db = self.lab.store.db

    def build(self, *labels, **kw):
        kw.setdefault("policy", self.lab.policy())
        return manifest.build(self.lab.store, roots={label: self.dirs[label] for label in labels},
                              library="home", torrents_dir=self.lab.torrents, **kw)

    def keys(self, **kw):
        return sorted(t["rel_path"] for t in self.repo.titles("home", **kw))

    def counts(self):
        return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("library_titles", "library_files", "manifest_links", "verifications",
                          "library_absent", "manifest_torrents")}

    def relabel(self):
        """Per-disk labels first, then a share-level label over the same files."""
        self.build("disk1", "disk2")
        return self.build("anime")


class Marking(Base):
    def test_new_labels_mark_old_label_titles_absent(self):
        first = self.build("disk1", "disk2")
        self.assertEqual(first["absent"], {"marked": 0, "complete": True, "reason": None})
        self.assertEqual(len(self.keys()), 2)
        second = self.build("anime")
        self.assertEqual(second["absent"], {"marked": 2, "complete": True, "reason": None})
        self.assertEqual(self.keys(), [f"anime/{n}" for n in sorted(NAMES)])
        self.assertEqual(len(self.keys(include_absent=True)), 4)
        marked = {t["rel_path"]: t["absent_at"] for t in self.repo.titles("home", include_absent=True)}
        self.assertTrue(marked["disk1/Kura Sample Series"] is not None
                        and marked["disk2/Kura Extra Show"] is not None)
        self.assertTrue(all(marked[f"anime/{n}"] is None for n in NAMES))

    def test_the_mark_keeps_its_first_timestamp(self):
        self.relabel()
        before = {t["rel_path"]: t["absent_at"] for t in self.repo.titles("home", include_absent=True)}
        self.build("anime")
        after = {t["rel_path"]: t["absent_at"] for t in self.repo.titles("home", include_absent=True)}
        self.assertEqual(before, after)

    def test_incomplete_builds_mark_nothing(self):
        self.build("disk1", "disk2")
        # Interrupted.
        out = self.build("anime", reverify=True, should_stop=lambda: True)
        self.assertEqual((out["interrupted"], out["absent"]["marked"]), (True, 0))
        # A root with nothing in it (an unmounted share) proves nothing.
        empty = self.lab.tmp / "empty"
        empty.mkdir()
        out = manifest.build(self.lab.store, roots={"anime": self.lab.lib, "gone": empty}, library="home",
                             torrents_dir=self.lab.torrents, policy=self.lab.policy())
        self.assertEqual((out["absent"]["reason"], out["absent"]["marked"]), ("empty_root", 0))
        # A partial rebuild asked for on purpose.
        out = self.build("anime", mark_absent=False)
        self.assertEqual((out["absent"]["reason"], out["absent"]["marked"]), ("disabled", 0))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM library_absent").fetchone()[0], 0)
        self.assertEqual(len(self.keys()), 4)

    def test_a_spent_read_budget_still_marks(self):
        # Verification rotates across nights under a read budget; the inventory is still complete.
        self.build("disk1", "disk2")
        out = self.build("anime", max_read_bytes=0)
        self.assertGreater(out["verification"]["deferred"], 0)
        self.assertEqual(out["absent"], {"marked": 2, "complete": True, "reason": None})

    def test_a_budget_spent_mid_verification_still_marks(self):
        self.build("disk1", "disk2")
        out = self.build("anime", reverify=True, max_read_bytes=100)
        verdicts = {r[0] for r in self.db.execute(
            "SELECT verdict FROM verifications ORDER BY id DESC LIMIT 1")}
        self.assertEqual(verdicts, {"budget_exhausted"})     # the case really is a mid-verification cut
        self.assertEqual((out["absent"]["complete"], out["absent"]["marked"]), (True, 2))

    def test_a_stop_requested_after_the_last_step_marks_nothing(self):
        self.build("disk1")
        for torrent in self.lab.torrents.glob("*.torrent"):
            torrent.unlink()                  # nothing to loop over: the stop is only seen at the end
        out = self.build("anime", should_stop=lambda: True)
        self.assertEqual((out["absent"]["complete"], out["absent"]["marked"]), (False, 0))

    def test_a_failed_build_marks_nothing(self):
        self.build("disk1", "disk2")
        with self.assertRaises(KuraError):
            manifest.build(self.lab.store, roots={"anime": self.lab.tmp / "missing"}, library="home",
                           torrents_dir=self.lab.torrents, policy=self.lab.policy())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM library_absent").fetchone()[0], 0)
        self.assertEqual(len(self.keys()), 2)

    def test_a_title_that_reappears_is_present_again(self):
        self.relabel()
        self.assertEqual(len(self.keys()), 2)
        out = self.build("disk1", "anime")
        self.assertEqual(out["absent"]["marked"], 0)
        self.assertEqual(self.keys(), ["anime/Kura Extra Show", "anime/Kura Sample Series",
                                       "disk1/Kura Sample Series"])
        absent = {r[0] for r in self.db.execute(
            "SELECT t.rel_path FROM library_titles t JOIN library_absent a ON a.title_id=t.id")}
        self.assertEqual(absent, {"disk2/Kura Extra Show"})
        shown = {t["title"] for t in manifest.show(self.lab.store, "home")["titles"]}
        self.assertIn("disk1/Kura Sample Series", shown)
        self.assertNotIn("disk2/Kura Extra Show", shown)

    def test_a_reappearing_title_keeps_its_recorded_alias(self):
        self.build("disk1")
        row = next(t for t in self.repo.titles("home") if t["rel_path"] == "disk1/Kura Sample Series")
        identity_doc = row["identity"]
        identity_doc["anilist"] = {"status": "resolved", "anilist_id": 999000001}
        self.repo.save_inventory("home", row["root_dev"], [{
            "rel_path": row["rel_path"], "kind": row["kind"], "total_bytes": row["total_bytes"],
            "newest_mtime_ns": row["newest_mtime_ns"], "identity": identity_doc,
            "files": self.repo.title_files(row["id"])}], row["seen_at"])
        self.build("anime")
        self.build("disk1")
        again = next(t for t in self.repo.titles("home") if t["rel_path"] == "disk1/Kura Sample Series")
        self.assertEqual(again["identity"]["anilist"]["anilist_id"], 999000001)

    def test_a_title_vanished_from_a_scanned_root_stays_visible_as_stale(self):
        self.build("main")
        shutil.rmtree(self.lab.lib / NAMES[0])
        out = self.build("main")
        self.assertEqual(out["absent"]["marked"], 0)
        row = {t["title"]: t for t in manifest.show(self.lab.store, "home")["titles"]}[f"main/{NAMES[0]}"]
        self.assertEqual(row["status"], "stale")


class Ignored(Base):
    def setUp(self):
        super().setUp()
        self.build("disk1", "disk2")
        self.before = self.counts()
        self.build("anime")
        self.present = [f"anime/{n}" for n in sorted(NAMES)]
        self.absent = ["disk1/Kura Sample Series", "disk2/Kura Extra Show"]

    def test_show(self):
        shown = manifest.show(self.lab.store, "home")
        self.assertEqual(sorted(t["title"] for t in shown["titles"]), self.present)
        self.assertEqual(sum(shown["counts"].values()), 2)
        self.assertEqual(manifest.show(self.lab.store, "home", title=self.absent[0])["titles"], [])

    def test_plan_never_proposes_an_absent_title(self):
        doc = quarantine.plan(self.lab.store, library="home", skip_swarm=True)["plan"]
        self.assertEqual(sorted(f"{m['root']}/{m['from']}" for m in doc["moves"]), self.present)
        self.assertEqual({m["root"] for m in doc["moves"]}, {"anime"})
        self.assertEqual(doc["refused"], [])
        named = quarantine.plan(self.lab.store, library="home", skip_swarm=True, titles=self.absent)["plan"]
        self.assertEqual(named["moves"], [])
        self.assertEqual({r["reasons"][0] for r in named["refused"]}, {"not_in_manifest"})

    def test_plan_cannot_reach_an_absent_title_even_when_it_would_otherwise_pass(self):
        # Before this fix these rows (verified, old, fresh enough) were proposable.
        before = quarantine.plan(self.lab.store, library="home", skip_swarm=True, titles=self.present)["plan"]
        self.assertEqual(len(before["moves"]), 2)
        self.db.execute("DELETE FROM library_absent")
        self.db.commit()
        leaked = quarantine.plan(self.lab.store, library="home", skip_swarm=True)["plan"]
        self.assertEqual(len(leaked["moves"]), 4)       # what the unmarked state would have proposed

    def test_probe_library_has_no_no_root_failures(self):
        out = probe_mod.probe_library(self.lab.store, library="home", roots={"anime": self.lab.lib})
        self.assertEqual(out["sample"]["titles"], 2)
        self.assertNotIn("no_root", out["failed"])
        with self.assertRaises(KuraError):
            probe_mod.probe_library(self.lab.store, library="home", roots={"anime": self.lab.lib},
                                    titles=[self.absent[0]])

    def test_audit(self):
        report = audit.run(self.lab.store, library="home", store_report=False)
        self.assertEqual(report["counts"]["titles"], 2)
        self.assertEqual(sorted(t["title"] for t in report["titles"]), self.present)
        with self.assertRaises(KuraError):
            audit.run(self.lab.store, library="home", store_report=False, titles=[self.absent[0]])

    def test_identity_and_retain(self):
        out = identity.sync(self.lab.store, library="home")
        self.assertEqual(out["titles"], 2)
        with self.assertRaises(KuraError) as ctx:
            identity.sync(self.lab.store, library="home", titles=[self.absent[0]])
        self.assertEqual(ctx.exception.code, "not_found")
        # An identity row recorded for an absent title stays out of `identity show`.
        self.db.execute("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
                        " VALUES ('home', ?, 999000002, 'resolved', 'list', 1.0)", (self.absent[0],))
        self.db.commit()
        shown = identity.show(self.lab.store, "home")
        self.assertNotIn(self.absent[0], [t["title"] for t in shown["titles"]])
        self.assertEqual(shown["unrecorded"] + sum(shown["counts"].values()), 2)
        listed = self.lab.tmp / "retain.txt"
        listed.write_text("999000002\n")
        checked = identity.retain_check(self.lab.store, "home", idlist.parse(listed))
        self.assertEqual(checked["retained_titles"], [])          # the absent title does not count
        self.assertEqual(checked["unmatched_ids"], [999000002])
        self.assertEqual(checked["unidentified_titles"], 2)

    def test_anilist_media_ids_skip_absent_titles(self):
        from kura import anilist_list
        self.db.executemany(
            "INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
            " VALUES ('home', ?, ?, 'resolved', 'list', 1.0)",
            [(self.absent[0], 999000002), (self.present[0], 999000003)])
        self.db.commit()
        self.assertEqual(anilist_list.media_ids(self.db, [], "home", False), [999000003])


class Prune(Base):
    def setUp(self):
        super().setUp()
        self.relabel()
        self.keep = self.counts()

    def test_dry_run_deletes_nothing(self):
        before = self.counts()
        out = manifest.prune(self.lab.store, "home")
        self.assertTrue(out["dry_run"])
        self.assertEqual((out["absent"], out["vanished"]), (2, 0))
        self.assertEqual(out["would_delete"]["titles"], 2)
        self.assertEqual(out["would_delete"]["files"], 4)
        self.assertEqual(out["would_delete"]["links"], 2)
        self.assertEqual(out["would_delete"]["verifications"], 2)
        self.assertEqual(out["by_root"], {"disk1": 1, "disk2": 1})
        self.assertEqual(out["titles_kept"], 2)
        self.assertNotIn("deleted", out)
        self.assertEqual(self.counts(), before)

    def test_yes_deletes_only_absent_rows_and_never_media(self):
        media = snapshot(self.lab.lib)
        torrents = snapshot(self.lab.torrents)
        out = manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual(out["deleted"], {"titles": 2, "files": 4, "links": 2, "verifications": 2,
                                          "probe_results": 0, "marks": 2, "identities": 0})
        after = self.counts()
        self.assertEqual((after["library_titles"], after["library_files"], after["manifest_links"],
                          after["verifications"], after["library_absent"]), (2, 4, 2, 2, 0))
        self.assertEqual(after["manifest_torrents"], self.keep["manifest_torrents"])   # shared rows stay
        self.assertEqual(self.keys(), [f"anime/{n}" for n in sorted(NAMES)])
        self.assertEqual(sorted(t["status"] for t in manifest.show(self.lab.store, "home")["titles"]),
                         ["reacquirable", "reacquirable"])
        self.assertEqual(snapshot(self.lab.lib), media)
        self.assertEqual(snapshot(self.lab.torrents), torrents)
        again = manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual((again["absent"], again["deleted"]["titles"]), (0, 0))

    def test_probe_results_of_pruned_titles_go_too(self):
        victim = next(t for t in self.repo.titles("home", include_absent=True) if t["rel_path"].startswith("disk1/"))
        self.db.execute("INSERT INTO probe_results VALUES (?, 'ep01.mkv', 1, 1, 1, 'ok', '{}', 0, 1.0)", (victim["id"],))
        self.db.commit()
        out = manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual(out["deleted"]["probe_results"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0], 0)

    def test_older_than_days(self):
        now = time.time()
        none = manifest.prune(self.lab.store, "home", older_than_days=1, dry_run=False, now=lambda: now)
        self.assertEqual((none["absent"], none["deleted"]["titles"]), (0, 0))
        self.assertEqual(self.counts()["library_titles"], 4)
        later = manifest.prune(self.lab.store, "home", older_than_days=1, dry_run=False,
                               now=lambda: now + 2 * DAY)
        self.assertEqual(later["deleted"]["titles"], 2)

    def test_vanished_titles_are_prunable_by_age_of_last_sighting(self):
        self.build("anime")
        shutil.rmtree(self.lab.lib / NAMES[0])
        self.build("anime")
        self.assertEqual(self.counts()["library_titles"], 4)
        out = manifest.prune(self.lab.store, "home")
        self.assertEqual((out["absent"], out["vanished"]), (2, 0))      # vanished rows need an age
        out = manifest.prune(self.lab.store, "home", older_than_days=0)
        self.assertEqual((out["absent"], out["vanished"]), (2, 1))
        young = manifest.prune(self.lab.store, "home", older_than_days=1)
        self.assertEqual(young["vanished"], 0)         # seen_at is the last real sighting, not the last build
        self.assertEqual(manifest.prune(self.lab.store, "home", older_than_days=1,
                                        now=lambda: time.time() + 2 * DAY)["vanished"], 1)

    def test_every_table_keyed_by_title_id_is_pruned(self):
        keyed = set()
        for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({name})")}
            if "title_id" in cols:
                keyed.add(name)
        self.assertEqual(keyed, {"library_files", "manifest_links", "verifications", "probe_results",
                                 "library_absent"})
        deleted = manifest.prune(self.lab.store, "home")["would_delete"]
        self.assertEqual(set(deleted), {"titles", "files", "links", "verifications", "probe_results", "marks",
                                  "identities"})

    def test_pruned_titles_identities_stay_out_of_media_and_seadex_ids(self):
        from kura import anilist_list
        from kura.sources import seadex
        rows = [(self.absent_key, 999000002), ("anime/Kura Extra Show", 999000003)]
        self.db.executemany(
            "INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
            " VALUES ('home', ?, ?, 'resolved', 'list', 1.0)", rows)
        self.db.commit()
        for stage in ("absent", "pruned"):
            self.assertEqual(anilist_list.media_ids(self.db, [], "home", False), [999000003], stage)
            self.assertEqual(seadex.library_ids(self.lab.store, "home"), [999000003], stage)
            manifest.prune(self.lab.store, "home", dry_run=False)

    absent_key = "disk1/Kura Sample Series"

    def test_a_fully_pruned_library_leaks_no_identities(self):
        from kura import anilist_list
        from kura.sources import seadex
        self.build("anime")
        self.db.executemany(
            "INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
            " VALUES ('home', ?, ?, 'resolved', 'list', 1.0)",
            [("disk1/Kura Sample Series", 999000002), ("anime/Kura Extra Show", 999000003)])
        self.db.commit()
        self.db.execute("INSERT OR REPLACE INTO library_absent SELECT id, 1.0 FROM library_titles")   # none is present
        self.db.commit()
        out = manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual(self.counts()["library_titles"], 0)
        self.assertEqual(out["deleted"]["identities"], 2)
        self.assertEqual(anilist_list.media_ids(self.db, [], "home", False), [])
        self.assertEqual(seadex.library_ids(self.lab.store, "home"), [])

    def test_a_pin_in_a_fully_pruned_library_never_reaches_media_or_seadex_ids(self):
        from kura import anilist_list
        from kura.sources import seadex
        self.build("anime")
        self.db.execute("INSERT OR REPLACE INTO library_absent SELECT id, 1.0 FROM library_titles")
        self.db.execute("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
                        " VALUES ('home', 'disk1/Kura Sample Series', 999000002, 'pinned', 'owner', 1.0)")
        self.db.commit()
        manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual(self.counts()["library_titles"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM title_identities").fetchone()[0], 0)   # nothing left to pin
        self.assertEqual(anilist_list.media_ids(self.db, [], "home", False), [])
        self.assertEqual(seadex.library_ids(self.lab.store, "home"), [])

    def test_emptying_a_library_clears_leftover_identities_of_earlier_prunes(self):
        from kura import anilist_list
        self.build("anime")
        self.db.execute("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
                        " VALUES ('home', 'disk9/Kura Long Gone', 999000005, 'pinned', 'owner', 1.0)")
        self.db.execute("INSERT OR REPLACE INTO library_absent SELECT id, 1.0 FROM library_titles")
        self.db.commit()
        dry = manifest.prune(self.lab.store, "home")
        self.assertEqual(dry["would_delete"]["identities"], 1)
        manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM title_identities").fetchone()[0], 0)
        self.assertEqual(anilist_list.media_ids(self.db, [], "home", False), [])

    def test_titles_linked_to_a_protected_torrent_are_kept(self):
        victim = next(t for t in self.repo.titles("home", include_absent=True) if t["rel_path"].startswith("disk1/"))
        infohash = self.repo.links(victim["id"])[0]["infohash"]
        row = self.repo.torrent(infohash)
        self.repo.upsert_torrent({**row, "info": None, "magnet": None,
                                  "protection": {**row["protection"], "protected": True}})
        out = manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual((out["kept_protected"], out["deleted"]["titles"]), (1, 1))
        self.assertEqual(out["kept_protected_sample"], [victim["rel_path"]])
        self.assertEqual(self.repo.links(victim["id"])[0]["infohash"], infohash)    # the evidence stays
        self.assertEqual(len(self.repo.titles("home", include_absent=True)), 3)

    def test_unreadable_protection_evidence_keeps_the_title(self):
        self.db.execute("UPDATE manifest_torrents SET protection='{not json'")
        self.db.commit()
        out = manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual((out["kept_protected"], out["deleted"]["titles"]), (2, 0))
        self.assertEqual(sorted(out["kept_protected_sample"]), ["disk1/Kura Sample Series", "disk2/Kura Extra Show"])
        self.assertEqual(len(self.repo.titles("home", include_absent=True)), 4)

    def test_owner_pins_survive_a_prune(self):
        self.db.execute("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
                        " VALUES ('home', 'disk1/Kura Sample Series', 999000002, 'pinned', 'owner', 1.0)")
        self.db.execute("INSERT INTO title_identities(library, title, anilist_id, status, source, updated_at)"
                        " VALUES ('home', 'disk2/Kura Extra Show', 999000004, 'resolved', 'list', 1.0)")
        self.db.commit()
        out = manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual(out["deleted"]["identities"], 1)
        self.assertEqual(self.db.execute("SELECT title FROM title_identities").fetchall(),
                         [("disk1/Kura Sample Series",)])

    def test_prune_dry_run_does_not_take_a_write_transaction(self):
        other = sqlite3.connect(self.lab.state / "state.sqlite3", timeout=0.1)
        other.execute("BEGIN IMMEDIATE")
        try:
            out = manifest.prune(self.lab.store, "home")
        finally:
            other.rollback()
            other.close()
        self.assertTrue(out["dry_run"])

    def test_other_libraries_are_untouched(self):
        row = next(iter(self.repo.titles("home")))
        self.repo.save_inventory("elsewhere", row["root_dev"], [{
            "rel_path": "disk9/Kura Other", "kind": "dir", "total_bytes": 0, "newest_mtime_ns": 1}], 1.0)
        self.db.execute("INSERT INTO library_absent SELECT id, 1.0 FROM library_titles WHERE library='elsewhere'")
        self.db.commit()
        manifest.prune(self.lab.store, "home", dry_run=False)
        self.assertEqual(len(self.repo.titles("elsewhere", include_absent=True)), 1)

    def test_bad_arguments(self):
        for bad in (-1, 10 ** 6, True, "7"):
            with self.assertRaises(KuraError, msg=repr(bad)):
                manifest.prune(self.lab.store, "home", older_than_days=bad)
        with self.assertRaises(KuraError):
            manifest.prune(self.lab.store, "bad label/")


class PruneCli(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        for name in NAMES:
            self.lab.add(name, GOOD)
        self.home = str(self.lab.tmp / "home")
        os.mkdir(self.home)

    def cli(self, *argv):
        buffer = io.StringIO()
        with patch.dict(os.environ, {"HOME": self.home}), patch("sys.argv", ["kura", *argv]), \
             contextlib.redirect_stdout(buffer):
            try:
                cli.main()
            except SystemExit as exited:
                return exited.code, json.loads(buffer.getvalue())

    def build(self, label, *extra):
        return self.cli("manifest", "build", "--library", "home", "--root", f"{label}={self.lab.lib}",
                        "--torrents", str(self.lab.torrents), "--tracker-denylist", str(self.lab.deny),
                        "--protected", str(self.lab.prot), "--no-anilist", *extra)

    def test_default_is_a_dry_run_and_yes_deletes(self):
        self.build("disk1")
        code, out = self.build("anime")
        self.assertEqual((code, out["data"]["absent"]["marked"]), (0, 2))
        code, out = self.cli("manifest", "prune", "--library", "home")
        self.assertEqual((code, out["success"], out["data"]["dry_run"], out["data"]["would_delete"]["titles"]),
                         (0, True, True, 2))
        code, out = self.cli("manifest", "prune", "--library", "home", "--dry-run")
        self.assertEqual((code, out["data"]["dry_run"]), (0, True))
        code, out = self.cli("manifest", "show", "--library", "home")
        self.assertEqual(sum(out["data"]["counts"].values()), 2)
        code, out = self.cli("manifest", "prune", "--library", "home", "--older-than-days", "30", "--yes")
        self.assertEqual((code, out["data"]["dry_run"], out["data"]["deleted"]["titles"]), (0, False, 0))
        code, out = self.cli("manifest", "prune", "--library", "home", "--yes")
        self.assertEqual((code, out["data"]["dry_run"], out["data"]["deleted"]["titles"]), (0, False, 2))
        self.assertEqual(out["data"]["by_root"], {"disk1": 2})

    def test_yes_and_dry_run_conflict_and_bad_values_fail(self):
        code, out = self.cli("manifest", "prune", "--library", "home", "--yes", "--dry-run")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out = self.cli("manifest", "prune", "--library", "home", "--older-than-days", "-3")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))
        code, out = self.cli("manifest", "prune")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))

    def test_keep_other_roots_flag(self):
        self.build("disk1")
        code, out = self.build("anime", "--keep-other-roots")
        self.assertEqual((code, out["data"]["absent"]["reason"], out["data"]["absent"]["marked"]),
                         (0, "disabled", 0))


class Migration(unittest.TestCase):
    def test_v3_database_migrates_to_v4_intact(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve() / "state"
        root.mkdir(mode=0o700)
        path = root / "state.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(V1)
        db.executescript(MIGRATIONS[2])
        db.executescript(MIGRATIONS[3])
        db.execute("UPDATE meta SET version=3")
        db.execute("INSERT INTO library_titles(library, rel_path, kind, root_dev, file_count, total_bytes,"
                   " newest_mtime_ns, seen_at) VALUES ('lib', 'disk1/Kura Sample Series', 'dir', 1, 1, 10, 5, 1.0)")
        db.execute("INSERT INTO library_files VALUES (1, 'ep1.mkv', 10, 5, 1, 7)")
        db.execute("INSERT INTO manifest_links VALUES (1, ?, 'exact', '{}')", ("a" * 40,))
        db.commit()
        db.close()
        self.assertNotIn("library_absent", tables(path))
        store = Store(root)
        try:
            self.assertEqual((version(path), SCHEMA_VERSION), (5, 5))
            self.assertIn("library_absent", tables(path))
            repo = ManifestRepo(store.db)
            titles = repo.titles("lib")
            self.assertEqual([(t["rel_path"], t["file_count"], t["absent_at"]) for t in titles],
                             [("disk1/Kura Sample Series", 1, None)])     # every old title stays present
            self.assertEqual(repo.title_files(titles[0]["id"])[0]["size"], 10)
            self.assertEqual(repo.links(titles[0]["id"])[0]["strategy"], "exact")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM library_absent").fetchone()[0], 0)
        finally:
            store.db.close()

    def test_v4_migration_is_rerunnable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name).resolve() / "state")
        try:
            store.db.executescript(MIGRATIONS[4])
        finally:
            store.db.close()


if __name__ == "__main__":
    unittest.main()
