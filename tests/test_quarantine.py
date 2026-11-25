"""Quarantine plan proposals. Synthetic data only; the plan must never touch the library."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import torrentgen
import kura.cli as cli
from kura import manifest, quarantine
from kura.manifest_store import ManifestRepo
from kura.models import KuraError
from kura.protect import load_policy
from test_manifest import GOOD, Lab, snapshot

DAY = 86400
OLD = int((time.time() - 60 * DAY) * 1e9)


def age(path: Path):
    """Backdate a title (and everything in it) so the quiet period has passed."""
    for p in [path, *path.rglob("*")]:
        os.utime(p, ns=(OLD, OLD), follow_symlinks=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        self.repo = ManifestRepo(self.lab.store.db)

    def add(self, name, files=None, *, old=True, **kw):
        built = self.lab.add(name, files or GOOD, **kw)
        if old:
            age((kw.get("where") or self.lab.lib) / name)
        return built

    def plan(self, **kw):
        """The plan document plus plan_id and stored, flattened for convenience."""
        kw.setdefault("skip_swarm", True)
        result = quarantine.plan(self.lab.store, library="home", **kw)
        return {**result["plan"], "plan_id": result["plan_id"], "stored": result["stored"]}

    def refused(self, doc):
        return {r["title"]: r for r in doc["refused"]}

    def swarm(self, built, **row):
        row.setdefault("checked_at", time.time())
        row.setdefault("status", "ok")
        row.setdefault("seeders", 5)
        self.repo.add_swarm_check({"infohash": built.infohash, **row})


class Eligibility(Base):
    def setUp(self):
        super().setUp()
        self.good = self.add("Kura Good A")
        self.add("Kura Good B")
        self.add("Kura Fresh", old=False)
        self.add("Kura Private", private=True, announce="http://private.example/announce?passkey=SECRETKEY")
        self.add("Kura Extra")
        (self.lab.lib / "Kura Extra" / "notes.txt").write_text("extra")
        age(self.lab.lib / "Kura Extra")
        self.add("Kura Unmatched", torrent=False)
        self.lab.build()

    def test_proposes_only_eligible_titles_and_records_why_not(self):
        doc = self.plan()
        self.assertEqual([m["from"] for m in doc["moves"]], ["Kura Good A", "Kura Good B"])
        refused = self.refused(doc)
        self.assertIn("recently_modified", refused["main/Kura Fresh"]["reasons"])
        self.assertEqual(refused["main/Kura Private"]["status"], "protected")
        self.assertIn("uncovered_files", refused["main/Kura Extra"]["reasons"])
        self.assertEqual(refused["main/Kura Unmatched"]["status"], "unmatched")
        move = doc["moves"][0]
        self.assertEqual(move["root"], "main")
        self.assertEqual(move["file_count"], 2)
        self.assertEqual(move["total_bytes"], 50000)
        self.assertEqual(move["infohashes"], [self.good.infohash])
        self.assertEqual(move["swarm"], "unchecked")
        self.assertIsNone(move["seeders"])
        self.assertEqual(move["to"], f"{quarantine.QUARANTINE_DIR}/{doc['batch_id']}/Kura Good A")
        self.assertEqual(doc["purge_not_before"], doc["created_at"] + 30 * DAY)
        self.assertEqual(doc["quarantine_dir"], ".kura-quarantine")
        self.assertIn("swarm_unchecked", doc["preconditions"])
        self.assertIn("never moves", doc["notice"])

    def test_plan_is_stored_and_hash_verifies(self):
        doc = self.plan()
        self.assertTrue(doc["stored"])
        self.assertEqual(doc["plan_id"], doc["plan_hash"][:16])
        stored = self.repo.plan(doc["plan_hash"])
        self.assertEqual((stored["status"], stored["plan"]["moves"]), ("proposed", doc["moves"]))
        self.assertEqual(quarantine.plan_hash_of(stored["plan"]), doc["plan_hash"])
        tampered = {**stored["plan"], "moves": []}
        self.assertNotEqual(quarantine.plan_hash_of(tampered), doc["plan_hash"])

    def test_returned_document_is_the_hashed_and_stored_one(self):
        result = quarantine.plan(self.lab.store, library="home", skip_swarm=True)
        document = result["plan"]
        self.assertEqual(set(result), {"plan", "plan_id", "stored"})
        self.assertEqual(quarantine.plan_hash_of(document), document["plan_hash"])
        self.assertEqual(self.repo.plan(document["plan_hash"])["plan"], document)
        self.assertEqual(json.loads(json.dumps(document)), document)

    def test_hash_is_deterministic_and_replanning_is_idempotent(self):
        t = int(time.time()) + 7
        a = self.plan(now=lambda: t - 2)
        b = self.plan(now=lambda: t)
        c = self.plan(now=lambda: t)
        self.assertEqual(b["plan_hash"], c["plan_hash"])
        self.assertEqual((b["stored"], c["stored"]), (True, False))
        self.assertEqual(b, {**c, "stored": True})
        self.assertNotEqual(a["plan_hash"], b["plan_hash"])
        self.assertNotEqual(self.plan(now=lambda: t, min_seeders=4)["plan_hash"], b["plan_hash"])
        picked = self.plan(now=lambda: t, titles=["main/Kura Good B", "main/Kura Good A"])
        self.assertEqual([m["from"] for m in picked["moves"]], [m["from"] for m in b["moves"]])

    def test_undo_is_the_exact_inverse(self):
        doc = self.plan()
        self.assertEqual(len(doc["undo"]), len(doc["moves"]))
        for move, undo in zip(doc["moves"], doc["undo"]):
            self.assertEqual(undo, {"root": move["root"], "from": move["to"], "to": move["from"]})
        self.assertEqual(len({m["to"] for m in doc["moves"]}), 2)

    def test_no_absolute_or_traversing_paths(self):
        doc = self.plan()
        for entry in doc["moves"] + doc["undo"]:
            for key in ("from", "to"):
                self.assertFalse(entry[key].startswith("/"))
                self.assertNotIn("..", entry[key].split("/"))
        self.assertNotIn(str(self.lab.tmp), json.dumps(doc))

    def test_title_filter_and_unknown_title(self):
        doc = self.plan(titles=["main/Kura Good B", "main/Nothing Here"])
        self.assertEqual([m["from"] for m in doc["moves"]], ["Kura Good B"])
        self.assertEqual(self.refused(doc)["main/Nothing Here"],
                         {"title": "main/Nothing Here", "status": "unknown",
                          "reasons": ["not_in_manifest"]})

    def test_nothing_eligible_means_nothing_stored(self):
        doc = self.plan(titles=["main/Kura Fresh"])
        self.assertEqual((doc["moves"], doc["undo"], doc["stored"]), ([], [], False))
        self.assertIsNone(self.repo.plan(doc["plan_hash"]))

    def test_unknown_library(self):
        with self.assertRaises(KuraError) as caught:
            quarantine.plan(self.lab.store, library="never-built", skip_swarm=True)
        self.assertEqual(caught.exception.code, "not_found")

    def test_plan_waits_for_no_running_build(self):
        with self.lab.store.lock():
            with self.assertRaises(KuraError) as caught:
                self.plan()
        self.assertEqual(caught.exception.code, "busy")

    def test_library_is_never_modified(self):
        before = snapshot(self.lab.lib)
        boom = AssertionError("library mutation attempted")
        with patch("os.rename", side_effect=boom), patch("os.replace", side_effect=boom), \
             patch("os.unlink", side_effect=boom), patch("os.remove", side_effect=boom), \
             patch("os.rmdir", side_effect=boom), patch("os.mkdir", side_effect=boom), \
             patch("os.link", side_effect=boom), patch("os.symlink", side_effect=boom), \
             patch("shutil.move", side_effect=boom), patch("shutil.rmtree", side_effect=boom):
            doc = self.plan()
        self.assertTrue(doc["moves"])
        self.assertEqual(snapshot(self.lab.lib), before)
        self.assertFalse((self.lab.lib / ".kura-quarantine").exists())


class Refusals(Base):
    def test_status_reasons_stale_blocked_unverified(self):
        self.add("Kura Touched")
        self.add("Kura Linked")
        os.symlink("Kura Linked", self.lab.lib / "Kura Linked" / "loop")
        self.add("Kura Unread", GOOD)
        self.lab.build(max_read_bytes=0)                 # nothing verified at all
        doc = self.plan()
        self.assertEqual(doc["moves"], [])
        for title in ("Kura Touched", "Kura Unread"):
            self.assertEqual(self.refused(doc)[f"main/{title}"]["status"], "unverified")
        self.assertEqual(self.refused(doc)["main/Kura Linked"]["status"], "blocked")
        self.lab.build()
        self.assertEqual([m["from"] for m in self.plan()["moves"]], ["Kura Touched", "Kura Unread"])
        os.utime(self.lab.lib / "Kura Touched" / "ep01.mkv", ns=(OLD + 1, OLD + 1))
        self.lab.build(max_read_bytes=0)                 # evidence now differs, nothing re-read
        doc = self.plan()
        touched = self.refused(doc)["main/Kura Touched"]       # verified bytes, changed file
        self.assertEqual((touched["status"], touched["reasons"]), ("stale", ["evidence_changed"]))
        self.assertEqual([m["from"] for m in doc["moves"]], ["Kura Unread"])

    def test_vanished_title_is_stale(self):
        self.add("Kura Gone")
        self.lab.build()
        shutil.rmtree(self.lab.lib / "Kura Gone")
        self.add("Kura Other")
        self.lab.build()
        refused = self.refused(self.plan())["main/Kura Gone"]
        self.assertEqual((refused["status"], refused["reasons"]), ("stale", ["absent_from_latest_scan"]))

    def test_title_assessed_under_an_older_policy_is_refused(self):
        other = self.lab.tmp / "second"
        other.mkdir()
        self.add("Kura Old Policy", where=other)
        self.add("Kura New Policy")
        both = {"main": self.lab.lib, "second": other}
        kw = dict(roots=both, library="home", torrents_dir=self.lab.torrents)
        manifest.build(self.lab.store, policy=self.lab.policy(), **kw)
        self.assertEqual(len(self.plan()["moves"]), 2)
        self.lab.deny.write_text("# hosts\nblocked.example\nnewly-listed.example\n")
        self.lab.build()                                  # only root "main" is rebuilt
        doc = self.plan()
        self.assertEqual([m["from"] for m in doc["moves"]], ["Kura New Policy"])
        # The manifest already demands re-classification under the new policy ...
        self.assertEqual(self.refused(doc)["second/Kura Old Policy"]["reasons"], ["not_reclassified"])
        # ... and the plan itself re-checks the hash even if title and torrent agree on an old one.
        title = next(t for t in self.repo.titles("home") if t["rel_path"] == "second/Kura Old Policy")
        infohash = self.repo.links(title["id"])[0]["infohash"]
        row, old = self.repo.torrent(infohash), "0" * 64
        self.repo.upsert_torrent({**row, "protection": {**row["protection"], "policy_sha256": old}})
        identity = title["identity"]
        identity["assessment"]["policy_sha256"] = old
        self.lab.store.db.execute("UPDATE library_titles SET identity=? WHERE id=?",
                                  (json.dumps(identity), title["id"]))
        self.lab.store.db.commit()
        self.assertEqual(self.lab.status("Kura New Policy")["status"], "reacquirable")
        doc = self.plan()
        self.assertEqual(self.refused(doc)["second/Kura Old Policy"]["reasons"], ["policy_changed"])
        manifest.build(self.lab.store, policy=self.lab.policy(), **kw)
        self.assertEqual(len(self.plan()["moves"]), 2)

    def test_policy_incomplete_refuses(self):
        self.add("Kura Good")
        self.lab.build(policy=load_policy(self.lab.deny, None))
        refused = self.refused(self.plan())["main/Kura Good"]
        self.assertIn("policy_incomplete", refused["reasons"])
        self.assertEqual(self.plan()["moves"], [])

    def test_path_protected_refuses(self):
        self.add("Kura Walled")
        self.lab.prot.write_text("path:main/Kura Walled\n")
        self.lab.build()
        self.assertEqual(self.refused(self.plan())["main/Kura Walled"]["status"], "protected")

    def test_stale_inventory_refuses(self):
        self.add("Kura Good")
        self.lab.build()
        doc = self.plan(now=lambda: time.time() + 25 * 3600)
        self.assertEqual(self.refused(doc)["main/Kura Good"]["reasons"], ["inventory_stale"])
        self.assertEqual(self.plan(now=lambda: time.time() + 3600)["moves"][0]["from"], "Kura Good")
        self.assertEqual(self.plan(max_inventory_age_h=1000, now=lambda: time.time() + 25 * 3600)
                         ["moves"][0]["from"], "Kura Good")
        self.assertIn("inventory_stale", self.refused(self.plan(now=lambda: time.time() - 3600))
                      ["main/Kura Good"]["reasons"])      # a scan from the future is not fresh

    def test_quiet_period(self):
        self.add("Kura Fresh", old=False)
        self.add("Kura Week", old=False)
        week = int((time.time() - 10 * DAY) * 1e9)
        for p in [self.lab.lib / "Kura Week", *(self.lab.lib / "Kura Week").rglob("*")]:
            os.utime(p, ns=(week, week))
        self.lab.build()
        doc = self.plan()
        self.assertEqual(doc["moves"], [])
        self.assertEqual(self.refused(doc)["main/Kura Fresh"]["reasons"], ["recently_modified"])
        self.assertEqual([m["from"] for m in self.plan(min_quiet_days=7)["moves"]], ["Kura Week"])
        future = int((time.time() + 30 * DAY) * 1e9)
        for p in [self.lab.lib / "Kura Week", *(self.lab.lib / "Kura Week").rglob("*")]:
            os.utime(p, ns=(future, future))
        self.lab.build()
        for days in (14, 0):      # an mtime in the future is never quiet, whatever the threshold
            doc = self.plan(min_quiet_days=days)
            self.assertEqual(self.refused(doc)["main/Kura Week"]["reasons"], ["recently_modified"])

    def test_unsafe_title_names_are_refused(self):
        name = ".kura-quarantine-old"
        self.add(name)
        self.lab.build()
        doc = self.plan()
        self.assertEqual(self.refused(doc)[f"main/{name}"]["reasons"], ["invalid_path"])
        for bad in ("", ".", "..", "a/b", "a\\b", "a\0b", " a", ".kura-quarantine", 7):
            self.assertFalse(quarantine._component_ok(bad), repr(bad))
        self.assertTrue(quarantine._component_ok("Kura Sample Series [1080p].mkv"))

    def test_inconsistent_stored_evidence_refuses(self):
        self.add("Kura Count")
        self.add("Kura Device")
        self.add("Kura Bytes")
        self.lab.build()
        db = self.lab.store.db
        db.execute("UPDATE library_titles SET file_count=3 WHERE rel_path='main/Kura Count'")
        db.execute("UPDATE library_titles SET root_dev=root_dev+1 WHERE rel_path='main/Kura Device'")
        db.execute("UPDATE library_titles SET total_bytes=1 WHERE rel_path='main/Kura Bytes'")
        db.commit()
        refused = self.refused(self.plan())
        self.assertEqual(refused["main/Kura Count"]["reasons"], ["evidence_incomplete"])
        self.assertEqual(refused["main/Kura Device"]["reasons"], ["cross_device"])
        self.assertEqual(refused["main/Kura Bytes"]["reasons"], ["evidence_incomplete"])

    def test_uncovered_and_partial_titles_refused(self):
        self.add("Kura Extra")
        (self.lab.lib / "Kura Extra" / "notes.txt").write_text("extra")
        age(self.lab.lib / "Kura Extra")
        self.add("Kura Broken")
        (self.lab.lib / "Kura Broken" / "ep02.mkv").write_bytes(b"x" * 20000)
        age(self.lab.lib / "Kura Broken")
        self.lab.build()
        refused = self.refused(self.plan())
        self.assertIn("uncovered_files", refused["main/Kura Extra"]["reasons"])
        self.assertEqual(refused["main/Kura Broken"]["status"], "unverified")


class Swarm(Base):
    def setUp(self):
        super().setUp()
        self.good = self.add("Kura Good")
        self.lab.build()

    def reasons(self, **kw):
        doc = self.plan(skip_swarm=False, **kw)
        return doc, self.refused(doc).get("main/Kura Good", {}).get("reasons")

    def test_no_swarm_data_refuses_by_default(self):
        doc, reasons = self.reasons()
        self.assertEqual((doc["moves"], reasons), ([], ["swarm_unchecked"]))

    def test_each_swarm_failure(self):
        now = time.time()
        self.swarm(self.good, status="unavailable", seeders=None, checked_at=now)
        self.assertEqual(self.reasons()[1], ["swarm_unavailable"])
        self.swarm(self.good, seeders=2, checked_at=now + 1)
        self.assertEqual(self.reasons()[1], ["swarm_low_seeders"])
        self.swarm(self.good, seeders=5, checked_at=now + 2)
        self.assertIsNone(self.reasons()[1])
        # The same fresh check is stale a day and a bit later; one from the future never counts.
        self.assertEqual(self.reasons(max_swarm_age_h=1, now=lambda: now + 7200)[1], ["swarm_stale"])
        self.swarm(self.good, seeders=5, checked_at=now + 3 * DAY)
        self.assertEqual(self.reasons(now=lambda: now + 3600)[1], ["swarm_stale"])

    def test_healthy_swarm_passes_and_is_recorded(self):
        self.swarm(self.good, seeders=3)
        doc, reasons = self.reasons()
        self.assertIsNone(reasons)
        self.assertEqual((doc["moves"][0]["seeders"], doc["moves"][0]["swarm"]), (3, "ok"))
        self.assertNotIn("swarm_unchecked", doc["preconditions"])
        self.assertIn("swarm_recheck_at_least_3_seeders_before_move", doc["preconditions"])
        self.assertEqual(self.reasons(min_seeders=4)[1], ["swarm_low_seeders"])
        self.assertFalse(doc["parameters"]["skip_swarm"])

    def test_latest_check_counts(self):
        self.swarm(self.good, seeders=9, checked_at=time.time() - 100)
        self.swarm(self.good, seeders=1, checked_at=time.time() - 10)
        self.assertEqual(self.reasons()[1], ["swarm_low_seeders"])

    def test_every_linked_torrent_needs_a_healthy_swarm(self):
        files = [("ep01.mkv", torrentgen.payload("a", 30000)), ("ep02.mkv", torrentgen.payload("b", 20000))]
        self.swarm(self.good)
        other = torrentgen.build("Kura Good", files[:1], announce="http://tracker.example/announce")
        # A second linked descriptor is simulated directly in the manifest tables.
        row = self.repo.torrent(self.good.infohash)
        row = {**row, "infohash": other.infohash, "info": None, "magnet": None}
        self.repo.upsert_torrent(row)
        title = self.repo.titles("home")[0]
        self.repo.link(title["id"], other.infohash, "root_dir", {})
        doc, reasons = self.reasons()
        self.assertIn("swarm_unchecked", reasons)
        self.assertIn("not_verified", reasons)

    def test_skip_swarm_is_recorded_everywhere(self):
        self.swarm(self.good, seeders=50)
        doc = self.plan(skip_swarm=True)
        self.assertEqual((doc["moves"][0]["swarm"], doc["moves"][0]["seeders"]), ("unchecked", None))
        self.assertIn("swarm_unchecked", doc["preconditions"])
        self.assertTrue(doc["parameters"]["skip_swarm"])
        self.assertEqual(self.repo.plan(doc["plan_hash"])["plan"]["moves"][0]["swarm"], "unchecked")


class Arguments(Base):
    def setUp(self):
        super().setUp()
        self.add("Kura Good")
        self.lab.build()

    def test_bad_quarantine_dir_names(self):
        for bad in ("", ".", "..", "../x", "/abs", "a/b", ".kura-quarantine/x", "quarantine",
                    ".kura-quarantine ", ".kura-quarantine-2", ".KURA-QUARANTINE", None, 3):
            with self.assertRaises(KuraError, msg=repr(bad)) as caught:
                self.plan(quarantine_dir=bad)
            self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_bad_numbers_and_titles(self):
        for kw in ({"min_seeders": 0}, {"min_seeders": -1}, {"min_seeders": True},
                   {"min_seeders": 2.5}, {"min_seeders": "3"}, {"min_quiet_days": -1},
                   {"min_quiet_days": float("nan")}, {"max_inventory_age_h": -1},
                   {"quarantine_days": 0}, {"titles": "main/Kura Good"}, {"titles": [1]},
                   {"max_swarm_age_h": None}):
            with self.assertRaises(KuraError, msg=repr(kw)) as caught:
                self.plan(**kw)
            self.assertEqual(caught.exception.code, "invalid_arguments")
        with self.assertRaises(KuraError):
            quarantine.plan(self.lab.store, library="", skip_swarm=True)

    def test_tree_sha256(self):
        a = {"rel_path": "T/a", "size": 1, "mtime_ns": 2, "dev": 9, "ino": 10}
        b = {"rel_path": "T/b", "size": 3, "mtime_ns": 4, "dev": 9, "ino": 11}
        h = quarantine.tree_sha256([a, b])
        self.assertEqual(h, quarantine.tree_sha256([b, a]))
        self.assertEqual(h, quarantine.tree_sha256([{**a, "dev": 1, "ino": 2}, b]))
        self.assertNotEqual(h, quarantine.tree_sha256([a]))
        self.assertNotEqual(h, quarantine.tree_sha256([{**a, "size": 2}, b]))
        self.assertNotEqual(h, quarantine.tree_sha256([{**a, "mtime_ns": 3}, b]))
        self.assertNotEqual(h, quarantine.tree_sha256([{**a, "rel_path": "T/c"}, b]))
        self.assertRegex(h, r"\A[0-9a-f]{64}\Z")
        with self.assertRaises(KuraError):
            quarantine.tree_sha256([{**a, "size": "1"}])


class CommandLine(Base):
    def setUp(self):
        super().setUp()
        self.add("Kura Good")
        self.add("Kura Fresh", old=False)
        self.home = str(self.lab.tmp / "home")
        os.mkdir(self.home)
        self.cli("manifest", "build", "--library", "home", "--root", f"main={self.lab.lib}",
                 "--torrents", str(self.lab.torrents), "--tracker-denylist", str(self.lab.deny),
                 "--protected", str(self.lab.prot), "--no-anilist")

    def cli(self, *argv):
        buffer = io.StringIO()
        with patch.dict(os.environ, {"HOME": self.home}), patch("sys.argv", ["kura", *argv]), \
             contextlib.redirect_stdout(buffer):
            try:
                cli.main()
            except SystemExit as exited:
                return exited.code, json.loads(buffer.getvalue())

    def test_plan_envelope_and_read_only_library(self):
        before = snapshot(self.lab.lib)
        code, out = self.cli("manifest", "plan", "--library", "home", "--skip-swarm")
        self.assertEqual((code, out["success"], out["phase"]), (0, True, "manifest"))
        data = out["data"]
        self.assertEqual(quarantine.plan_hash_of(data["plan"]), data["plan"]["plan_hash"])
        self.assertEqual([m["from"] for m in data["plan"]["moves"]], ["Kura Good"])
        self.assertTrue(data["stored"])
        self.assertIn("swarm_unchecked", data["plan"]["preconditions"])
        self.assertEqual(snapshot(self.lab.lib), before)

    def test_swarm_is_required_unless_skipped(self):
        code, out = self.cli("manifest", "plan", "--library", "home")
        self.assertEqual((code, out["data"]["plan"]["moves"], out["data"]["stored"]), (0, [], False))
        self.assertIn("swarm_unchecked", out["data"]["plan"]["refused"][0]["reasons"])

    def test_options_and_errors(self):
        code, out = self.cli("manifest", "plan", "--library", "home", "--skip-swarm",
                             "--title", "main/Kura Fresh", "--min-quiet-days", "0")
        self.assertEqual([m["from"] for m in out["data"]["plan"]["moves"]], ["Kura Fresh"])
        for extra, expected in ((["--quarantine-dir", "elsewhere"], "invalid_arguments"),
                                (["--min-seeders", "0"], "invalid_arguments"),
                                (["--library", "never-built"], "not_found")):
            argv = ["manifest", "plan", "--library", "home", "--skip-swarm", *extra]
            code, out = self.cli(*argv)
            self.assertEqual((code, out["error"]["code"]), (2, expected), extra)
        code, out = self.cli("manifest", "plan")
        self.assertEqual((code, out["error"]["code"]), (2, "invalid_arguments"))


if __name__ == "__main__":
    unittest.main()
