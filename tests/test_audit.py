"""The offline library audit. Synthetic state only: nothing is probed, fetched or opened here
except the one end-to-end test, which probes generated files in a temp dir."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

import mkvgen
from kura import audit, cli_audit, idlist, mkvprobe, probe as probe_mod, rofs
from kura.inventory import scan
from kura.manifest_store import ManifestRepo
from kura.models import KuraError
from kura.store import Store
from test_cli_handover import run

MB = 1_000_000
NOW = 1_800_000_000.0
A, B, C = 999000001, 999000002, 999000003
GROUP, OTHER = "Kura Encodes", "Example Subs"
SHOW = "Kura Sample Series"
KEY = f"main/{SHOW}"
X265 = {"rc": "crf", "crf": "21.2", "aq-mode": "3", "psy-rd": "0.80", "limit-sao": True, "bframes": "4",
        "bitdepth": "10"}
X264_STREAM = {"rc": "2pass", "bitrate": "8000", "bframes": "0", "psy_rd": "0.40:0.00", "deblock": "1:1:1"}


def digest(label):
    return hashlib.sha1(label.encode()).hexdigest()


def ep(n, group=GROUP, ver="", tags="[1080p][x265]"):
    return f"[{group}] {SHOW} - {n:02d}{ver} {tags}.mkv"


def result(*, bits=10, w=1920, h=1080, fps=23.976, kbps=1600, library="x265", settings=X265, subs=("ass",),
           fonts=1, status="ok", container="matroska", warnings=(), source="tags", encoder=True):
    return {"schema": "kura.probe/1", "status": status, "container": container, "bytes_read": 100,
            "duration_s": 1420.0, "muxing_app": "Kura Mux", "writing_app": "Lavf",
            "video": {"track": 1, "codec_id": "V_X", "codec": "hevc", "profile": "Main 10", "bit_depth": bits,
                      "width": w, "height": h, "fps": fps, "bitrate_kbps": kbps, "bitrate_source": source},
            "encoder": ({"library": library, "version": "3.5", "where": "codec_private", "settings": settings}
                        if encoder else None),
            "audio": [{"track": 2, "codec": "aac", "channels": 2, "sample_rate": 48000, "lang": "jpn",
                       "default": True}],
            "subtitles": [{"track": 3, "format": f, "lang": "eng", "name": None, "default": False,
                           "forced": False} for f in subs],
            "attachments": {"count": fonts, "fonts": fonts, "bytes": 1, "names": []},
            "chapters": {"present": True, "count": 3}, "warnings": list(warnings)}


class Lab(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = Store(self.tmp / "state")
        self.addCleanup(self.store.db.close)
        self.db = self.store.db
        self.ids = {}
        self.ino = 0

    # -- state builders ----------------------------------------------------------
    def title(self, name=SHOW, files=(), kind="dir"):
        self.db.commit()
        rows = []
        for fname, size in files:
            self.ino += 1
            rel = f"{name}/{fname}" if kind == "dir" else fname
            rows.append({"rel_path": rel, "size": size, "mtime_ns": 5, "dev": 1, "ino": self.ino})
        self.ids.update(ManifestRepo(self.db).save_inventory("home", 1, [{
            "rel_path": f"main/{name}", "kind": kind, "total_bytes": sum(s for _, s in files),
            "newest_mtime_ns": 5, "files": rows}], 1000.0))
        return self.ids[f"main/{name}"]

    def eps(self, count=3, size=250 * MB, **kw):
        return self.title(files=[(ep(n, **kw), size) for n in range(1, count + 1)])

    def probe(self, fname, res, name=SHOW, size=250 * MB, status=None):
        title = self.ids[f"main/{name}"]
        self.db.execute("INSERT OR REPLACE INTO probe_results VALUES (?,?,?,?,?,?,?,?,?)",
                        (title, f"{name}/{fname}", size, 5, mkvprobe.PROBE_VERSION, status or res["status"],
                         json.dumps(res), 100, NOW))
        self.db.commit()

    def identify(self, anilist_id=A, name=SHOW, status="resolved", source="list", candidates=()):
        self.db.execute("INSERT OR REPLACE INTO title_identities(library, title, anilist_id, status, source,"
                        " candidates, updated_at) VALUES ('home',?,?,?,?,?,1.0)",
                        (f"main/{name}", anilist_id if status in ("resolved", "pinned") else None, status,
                         source, json.dumps(list(candidates))))
        self.db.commit()

    def seadex(self, anilist_id, torrents, *, entry="entry000001", synced=NOW - 60):
        self.db.execute("INSERT OR REPLACE INTO seadex_entries VALUES (?,?,0,'','','[]','2025-01-01 00:00:00.000Z',1.0)",
                        (entry, anilist_id))
        for n, t in enumerate(torrents):
            tid = f"tor{anilist_id % 1000:03d}{n:06d}"
            public = t.get("public", True)
            files = t.get("files")
            self.db.execute(
                "INSERT OR REPLACE INTO seadex_torrents VALUES (?,?,?,?,?,?,?,0,'[]',?,?,?,?,?,'2025-01-01 00:00:00.000Z',1.0)",
                (tid, digest(tid) if public else None, t.get("group", GROUP), "Nyaa" if public else "ExampleTracker",
                 int(public), 1 if public else None, int(t.get("best", True)),
                 json.dumps(files) if files is not None else None, sum(s for _, s in files or []) or None,
                 len(files or []) or None, t.get("median"), 1.0 if files is not None else None))
            self.db.execute("INSERT OR REPLACE INTO seadex_entry_torrents VALUES (?,?)", (entry, tid))
        self.db.execute("INSERT OR REPLACE INTO sync_state(source, last_attempt, last_ok, last_full_ok, status)"
                        " VALUES ('seadex',?,?,?, 'ok')", (synced, synced, synced))
        self.db.commit()

    def viewer(self, *, favourites=(), scores=None, fetched=NOW - 60, name="kura_user", uid=999001):
        self.db.execute("INSERT OR REPLACE INTO anilist_users(user_id, name, is_viewer, list_fetched_at, updated_at)"
                        " VALUES (?,?,1,?,1.0)", (uid, name, fetched))
        for al in favourites:
            self.db.execute("INSERT OR REPLACE INTO anilist_favourites VALUES (?,?,1,1.0)", (uid, al))
        for al, score in (scores or {}).items():
            self.db.execute("INSERT OR REPLACE INTO anilist_list_entries VALUES (?,?,'COMPLETED',?,12,0,1,1.0)",
                            (uid, al, score))
        self.db.commit()

    def idfile(self, text, name="ids.txt", **kw):
        path = self.tmp / name
        path.write_text(text)
        return idlist.parse(path, **kw)

    # -- running -----------------------------------------------------------------
    def audit(self, **kw):
        kw.setdefault("library", "home")
        kw.setdefault("now", lambda: NOW)
        kw.setdefault("store_report", False)
        return audit.run(self.store, **kw)

    def doc(self, report=None, name=SHOW):
        report = report or self.audit()
        return next(t for t in report["titles"] if t["title"] == f"main/{name}")

    def codes(self, report=None, name=SHOW):
        return {f["code"]: f for f in self.doc(report, name)["flags"]}


class Basics(Lab):
    def test_report_shape(self):
        self.eps()
        report = self.audit()
        self.assertEqual(list(report), ["library", "generated_at", "inputs", "counts", "titles",
                                        "titles_truncated", "report_sha256"])
        self.assertEqual(set(report["inputs"]), {"inventory_at", "probe", "seadex", "anilist", "retain", "policy"})
        self.assertEqual(report["inputs"]["policy"]["efficient"], {"target_mb": 250, "min_mb": 120, "max_mb": 450})
        self.assertEqual(report["inputs"]["policy"]["archival"], {"cap": None})
        self.assertEqual(set(report["counts"]), {"titles", "flagged", "by_flag", "by_policy", "by_suggestion"})
        d = self.doc(report)
        self.assertEqual(set(d), {"title", "anilist_id", "identity", "policy", "policy_reasons", "retained", "files",
                                  "groups", "probe", "seadex", "flags", "suggestion"})
        self.assertEqual(d["files"], {"video": 3, "extras": 0, "bytes": 750 * MB,
                                      "per_episode_mb": {"median": 250.0, "min": 250.0, "max": 250.0}})
        self.assertEqual(d["groups"], [{"group": GROUP, "files": 3}])
        self.assertEqual(report["titles_truncated"], 0)

    def test_no_inventory_and_bad_arguments(self):
        with self.assertRaises(KuraError) as e:
            self.audit()
        self.assertEqual(e.exception.code, "no_inventory")
        self.eps()
        for kw in ({"titles": ["main/Nope"]}, {"flags": ["nope"]}, {"limit": 0}, {"limit": True},
                   {"retain": {1: None}}, {"library": ""}):
            with self.subTest(kw=kw), self.assertRaises(KuraError) as e:
                self.audit(**kw)
            self.assertEqual(e.exception.code, "invalid_arguments")

    def test_extras_are_excluded_from_size_and_group_stats(self):
        self.title(files=[(ep(1), 250 * MB), (ep(2), 250 * MB), (f"Extras/[{OTHER}] {SHOW} - NCOP1.mkv", 40 * MB),
                          (f"[{OTHER}] {SHOW} - ED1.mkv", 40 * MB), ("notes.txt", 10)])
        d = self.doc()
        self.assertEqual((d["files"]["video"], d["files"]["extras"], d["files"]["bytes"]), (2, 2, 500 * MB))
        self.assertEqual(d["files"]["per_episode_mb"]["min"], 250.0)
        self.assertEqual(d["groups"], [{"group": GROUP, "files": 2}])
        self.assertNotIn("mixed_groups", self.codes())

    def test_single_file_title(self):
        self.title(name="Kura Loose Movie.mkv", files=[("Kura Loose Movie.mkv", 600 * MB)], kind="file")
        d = self.doc(name="Kura Loose Movie.mkv")
        self.assertEqual(d["files"]["video"], 1)
        self.assertNotIn("size_above_band", self.codes(name="Kura Loose Movie.mkv"))     # a movie, not an episode


class Flags(Lab):
    def test_mixed_groups(self):
        self.title(files=[(ep(1), 250 * MB), (ep(2, group=OTHER), 250 * MB)])
        self.assertEqual(self.codes()["mixed_groups"]["detail"]["groups"], [OTHER, GROUP])
        self.assertEqual(self.codes()["mixed_groups"]["severity"], "warn")
        self.title(files=[(ep(1), 250 * MB), (f"{SHOW} - 02 [720p].mkv", 250 * MB)])      # unknown group does not count
        self.assertNotIn("mixed_groups", self.codes())

    def test_superseded_version_present(self):
        self.title(files=[(ep(1), 250 * MB), (ep(1, ver="v2"), 250 * MB), (ep(2), 250 * MB)])
        flag = self.codes()["superseded_version_present"]
        self.assertEqual((flag["severity"], flag["detail"]["episodes"]), ("action", [1]))
        self.title(files=[(ep(1), 250 * MB), (ep(1, group=OTHER, ver="v2"), 250 * MB)])  # different group
        self.assertNotIn("superseded_version_present", self.codes())
        self.title(files=[(ep(1, ver="v2"), 250 * MB), (ep(2), 250 * MB)])                # only v2 held
        self.assertNotIn("superseded_version_present", self.codes())

    def test_v2_known_upstream(self):
        self.eps(2)
        self.identify()
        self.seadex(A, [{"files": [[ep(1, ver="v2"), 1000], [ep(2), 1000]]}])
        flag = self.codes()["v2_known_upstream"]
        self.assertEqual((flag["severity"], flag["detail"]["episodes"]), ("warn", [1]))

    def test_v2_known_upstream_negatives(self):
        self.eps(2)
        self.identify()
        self.seadex(A, [{"files": [[ep(1, group=OTHER, ver="v2"), 1000]], "group": OTHER}])    # another group
        self.assertNotIn("v2_known_upstream", self.codes())
        self.seadex(A, [{"files": [[ep(1, ver="v2"), 1000]], "public": False}], entry="entry000002")  # private pick
        self.db.execute("DELETE FROM seadex_entry_torrents WHERE entry_id='entry000001'")
        self.assertNotIn("v2_known_upstream", self.codes())
        self.title(files=[(ep(1, ver="v2"), 250 * MB)])                                         # v2 already held
        self.seadex(A, [{"files": [[ep(1, ver="v2"), 1000]]}], entry="entry000003")
        self.assertNotIn("v2_known_upstream", self.codes())

    def test_low_bpp_8bit_severities(self):
        self.eps(1)
        for target, severity in ((0.05, "warn"), (0.03, "action")):
            kbps = round(target * 1920 * 1080 * 23.976 / 1000)
            self.probe(ep(1), result(bits=8, kbps=kbps, settings={"rc": "crf", "crf": "23"}, library="x264"))
            flag = self.codes()["low_bpp_8bit"]
            with self.subTest(bpp=target):
                self.assertEqual(flag["severity"], severity)
                self.assertAlmostEqual(flag["detail"]["min_bpp"], target, places=3)
                self.assertEqual(flag["detail"]["style"], "crf")

    def test_low_bpp_8bit_negatives(self):
        self.eps(1)
        for res in (result(bits=10, kbps=500),                                        # 10-bit is never flagged
                    result(bits=8, kbps=round(0.07 * 1920 * 1080 * 23.976 / 1000)),   # healthy 8-bit
                    result(bits=8, kbps=None)):                                         # unknown bitrate
            self.probe(ep(1), res)
            self.assertNotIn("low_bpp_8bit", self.codes())

    def test_low_bpp_on_an_untouched_stream_is_labelled(self):
        self.eps(1)
        self.probe(ep(1), result(bits=8, kbps=1500, library="x264", settings=X264_STREAM))
        flag = self.codes()["low_bpp_8bit"]
        self.assertEqual(flag["detail"]["style"], "untouched_stream")

    def test_ass_missing_fonts(self):
        self.eps(1)
        self.probe(ep(1), result(fonts=0))
        self.assertEqual(self.codes()["ass_missing_fonts"]["severity"], "warn")
        self.probe(ep(1), result(fonts=2))
        self.assertNotIn("ass_missing_fonts", self.codes())
        self.probe(ep(1), result(fonts=0, subs=("srt",)))
        self.assertNotIn("ass_missing_fonts", self.codes())
        self.probe(ep(1), result(fonts=0, warnings=["attachments_truncated"], status="partial"))
        self.assertNotIn("ass_missing_fonts", self.codes())

    def test_ass_missing_fonts_sidecar_directory(self):
        self.title(files=[(ep(1), 250 * MB), ("Fonts/Kura Sans.ttf", 100)])
        self.probe(ep(1), result(fonts=0))
        self.assertNotIn("ass_missing_fonts", self.codes())

    def test_size_bands_follow_the_efficient_policy(self):
        for size, code in ((500 * MB, "size_above_band"), (100 * MB, "size_below_band")):
            self.eps(3, size=size)
            self.assertIn(code, self.codes())
            self.assertEqual(self.codes()[code]["severity"], "warn")
        self.eps(3, size=450 * MB)
        self.assertFalse({"size_above_band", "size_below_band"} & set(self.codes()))
        self.eps(3, size=120 * MB)
        self.assertFalse({"size_above_band", "size_below_band"} & set(self.codes()))
        self.eps(3, size=900 * MB)
        self.identify()
        self.viewer(favourites=[A])
        self.assertEqual(self.doc()["policy"], "archival")
        self.assertFalse({"size_above_band", "size_below_band"} & set(self.codes()))        # no cap when archival

    def test_identity_unknown(self):
        self.eps()
        self.assertEqual(self.codes()["identity_unknown"]["severity"], "info")
        self.assertEqual(self.codes()["identity_unknown"]["detail"], {"status": "unrecorded"})
        self.assertEqual(self.codes(self.audit(retain=self.idfile(f"{A}\n")))["identity_unknown"]["severity"], "warn")
        self.identify(status="ambiguous", candidates=[A, B])
        self.assertEqual(self.codes()["identity_unknown"]["detail"], {"status": "ambiguous"})
        self.identify(status="not_anime", source="owner")
        self.assertNotIn("identity_unknown", self.codes())
        self.identify()
        self.assertNotIn("identity_unknown", self.codes())
        self.assertEqual(self.doc()["anilist_id"], A)

    def test_probe_missing_and_unsupported_container(self):
        self.eps(2)
        self.assertEqual(self.codes()["probe_missing"]["detail"], {"video_files": 2})
        self.probe(ep(1), {**result(), "status": "unsupported_container", "container": "mp4", "video": None,
                           "encoder": None})
        codes = self.codes()
        self.assertNotIn("probe_missing", codes)
        self.assertEqual(codes["unsupported_container"]["detail"], {"files": 1})
        self.probe(ep(1), result())
        self.assertFalse({"probe_missing", "unsupported_container"} & set(self.codes()))

    def test_probe_rows_that_no_longer_match_the_inventory_are_ignored_and_counted(self):
        self.eps(2)
        self.probe(ep(1), result(), size=1)                                         # file changed since probing
        self.db.execute("UPDATE probe_results SET probe_version=probe_version+1 WHERE rel_path LIKE '%02%'")
        self.probe(ep(2), result())
        self.db.execute("UPDATE probe_results SET probe_version=probe_version+1 WHERE rel_path LIKE '%- 02 %'")
        self.db.commit()
        report = self.audit()
        self.assertIn("probe_missing", self.codes(report))
        self.assertEqual(report["inputs"]["probe"]["changed"], 1)
        self.assertEqual(report["inputs"]["probe"]["files_probed"], 0)

    def test_retained_flag_and_unmatched_ids(self):
        self.eps()
        self.identify()
        retain = self.idfile(f"{A}  # keep\n{B}\n")
        report = self.audit(retain=retain)
        self.assertTrue(self.doc(report)["retained"])
        self.assertEqual(self.codes(report)["retained"], {"code": "retained", "severity": "info",
                                                         "detail": {"reason": "retained"}})
        self.assertEqual(report["inputs"]["retain"], {**retain.summary(), "unmatched_ids": [B]})
        self.assertFalse(self.doc()["retained"])
        self.assertNotIn("retained", self.codes())
        self.assertIsNone(self.audit()["inputs"]["retain"])

    def test_retain_possible_counts_as_retained(self):
        self.eps()
        self.identify(candidates=[B])
        report = self.audit(retain=self.idfile(f"{B}\n"))
        self.assertEqual(self.codes(report)["retained"]["detail"], {"reason": "retain_possible"})
        self.assertEqual(report["inputs"]["retain"]["unmatched_ids"], [B])


class SeaDex(Lab):
    def setUp(self):
        super().setUp()
        self.eps(2)
        self.identify()

    def test_held_best_has_no_flags(self):
        self.seadex(A, [{"group": GROUP, "median": 250 * MB}])
        d = self.doc()
        self.assertEqual(d["seadex"]["held_matches"], "best")
        self.assertEqual(d["seadex"]["best"][0]["release_group"], GROUP)
        self.assertFalse({"seadex_best_differs", "seadex_alt_held", "seadex_no_public_pick"} & set(self.codes()))
        self.assertEqual(d["suggestion"], "review" if any(f["severity"] != "info" for f in d["flags"]) else "none")

    def test_group_names_compare_loosely(self):
        self.seadex(A, [{"group": "KURA-encodes"}])
        self.assertEqual(self.doc()["seadex"]["held_matches"], "best")

    def test_infohash_link_counts_as_held(self):
        self.seadex(A, [{"group": "Someone Else"}])
        self.assertEqual(self.doc()["seadex"]["held_matches"], "none")
        torrent_hash = self.db.execute("SELECT infohash FROM seadex_torrents").fetchone()[0]
        self.db.execute("INSERT INTO manifest_torrents VALUES (?, 'x', NULL, 1, 16384, '[]', 0, '{}', NULL, NULL,"
                        " ?, 'test', 1.0, 1.0)", (torrent_hash, "0" * 64))
        self.db.execute("INSERT INTO manifest_links VALUES (?,?,?,?)", (self.ids[KEY], torrent_hash, "name", "{}"))
        self.db.commit()
        self.assertEqual(self.doc()["seadex"]["held_matches"], "best")

    def test_best_differs_severity_follows_policy(self):
        self.seadex(A, [{"group": OTHER}])
        flag = self.codes()["seadex_best_differs"]
        self.assertEqual((flag["severity"], flag["detail"]["held_matches"]), ("info", "none"))
        self.viewer(favourites=[A])
        self.assertEqual(self.codes()["seadex_best_differs"]["severity"], "warn")

    def test_alt_held(self):
        self.seadex(A, [{"group": OTHER, "best": True}, {"group": GROUP, "best": False}])
        d = self.doc()
        self.assertEqual(d["seadex"]["held_matches"], "alt")
        self.assertEqual([p["release_group"] for p in d["seadex"]["alt"]], [GROUP])
        self.assertTrue({"seadex_alt_held", "seadex_best_differs"} <= set(self.codes()))

    def test_private_only_picks(self):
        self.seadex(A, [{"group": OTHER, "public": False}])
        d = self.doc()
        self.assertEqual(d["seadex"]["best"], [{"release_group": OTHER, "public": False, "dual_audio": False,
                                                "median_video_mb": None, "video_files": None}])
        codes = self.codes()
        self.assertIn("seadex_no_public_pick", codes)
        self.assertNotIn("seadex_best_differs", codes)
        self.assertNotIn("upgrade_candidate", d["suggestion"])

    def test_best_outside_band_only_for_efficient_titles(self):
        self.seadex(A, [{"group": OTHER, "median": 700 * MB}])
        self.assertEqual(self.codes()["seadex_best_outside_band"]["detail"], {"median_mb": 700.0})
        self.viewer(favourites=[A])
        self.assertNotIn("seadex_best_outside_band", self.codes())

    def test_best_inside_band_is_not_outside_and_unknown_sizes_are_silent(self):
        self.seadex(A, [{"group": OTHER, "median": 300 * MB}])
        self.assertNotIn("seadex_best_outside_band", self.codes())
        self.seadex(A, [{"group": OTHER, "median": None}])
        self.assertNotIn("seadex_best_outside_band", self.codes())

    def test_suggestions(self):
        self.seadex(A, [{"group": OTHER, "median": 300 * MB}])                  # efficient, best inside band, differs
        self.assertEqual(self.doc()["suggestion"], "upgrade_candidate")
        self.seadex(A, [{"group": OTHER, "median": 700 * MB}])                  # best too big for the band
        self.assertNotEqual(self.doc()["suggestion"], "upgrade_candidate")
        self.viewer(favourites=[A])                                              # archival: size is irrelevant
        self.assertEqual(self.doc()["suggestion"], "upgrade_candidate")
        self.seadex(A, [{"group": GROUP, "median": 700 * MB}])                   # archival but already holds best
        self.assertNotEqual(self.doc()["suggestion"], "upgrade_candidate")

    def test_unknown_identity_never_consults_seadex(self):
        self.identify(status="ambiguous", candidates=[A])
        self.seadex(A, [{"group": OTHER}])
        d = self.doc()
        self.assertEqual(d["seadex"], {"entry": None, "incomplete": None, "best": [], "alt": [],
                                       "held_matches": "unknown"})
        self.assertFalse({c for c in self.codes() if c.startswith("seadex_")})

    def test_title_without_an_entry(self):
        self.assertEqual(self.doc()["seadex"]["held_matches"], "unknown")
        self.assertFalse({c for c in self.codes() if c.startswith("seadex_")})


class PolicyInAudit(Lab):
    def setUp(self):
        super().setUp()
        self.eps(1)
        self.identify()

    def policy(self, **kw):
        d = self.doc(self.audit(**kw))
        return d["policy"], d["policy_reasons"]

    def test_defaults_without_any_anilist_data(self):
        self.assertEqual(self.policy(), ("efficient", ["default"]))

    def test_favourite_and_score_threshold(self):
        self.viewer(favourites=[A])
        self.assertEqual(self.policy(), ("archival", ["favourite"]))
        self.db.execute("DELETE FROM anilist_favourites")
        self.viewer(scores={A: 9.0})
        self.assertEqual(self.policy(), ("archival", ["score_9_plus"]))
        self.viewer(scores={A: 8.9})
        self.assertEqual(self.policy(), ("efficient", ["default"]))

    def test_overrides_both_ways(self):
        self.viewer(scores={A: 3.0})
        self.assertEqual(self.policy(overrides=self.idfile(f"{A} archival\n", tags=policy_tags())),
                         ("archival", ["override_archival"]))
        self.viewer(favourites=[A], scores={A: 10.0})
        report = self.audit(overrides=self.idfile(f"{A} efficient  # cheap\n", tags=policy_tags()))
        self.assertEqual((self.doc(report)["policy"], self.doc(report)["policy_reasons"]),
                         ("efficient", ["override_efficient"]))
        self.assertEqual(report["inputs"]["policy"]["overrides_ids"], 1)
        self.assertEqual(len(report["inputs"]["policy"]["overrides_sha256"]), 64)

    def test_named_user_selects_another_snapshot(self):
        self.viewer(favourites=[A])
        self.viewer(name="Other_User", uid=999002, scores={A: 9.5}, favourites=[])
        self.db.execute("UPDATE anilist_users SET is_viewer=0 WHERE user_id=999002")
        self.db.commit()
        report = self.audit(user="other_user")
        self.assertEqual(self.doc(report)["policy_reasons"], ["score_9_plus"])
        self.assertEqual(report["inputs"]["anilist"]["user"], "Other_User")
        with self.assertRaises(KuraError) as e:
            self.audit(user="nobody")
        self.assertEqual(e.exception.code, "invalid_arguments")


def policy_tags():
    from kura import policy
    return policy.POLICIES


class Inputs(Lab):
    def test_stale_inputs_are_reported_and_the_audit_still_runs(self):
        self.eps()
        self.viewer(fetched=NOW - 3 * 86400)
        self.seadex(A, [{"group": GROUP}], synced=NOW - 8 * 86400)
        report = self.audit()
        self.assertEqual(report["inputs"]["seadex"], {"synced_at": NOW - 8 * 86400, "stale": True})
        self.assertEqual(report["inputs"]["anilist"], {"user": "kura_user", "fetched_at": NOW - 3 * 86400,
                                                       "stale": True})
        self.assertEqual(report["counts"]["titles"], 1)

    def test_never_synced_is_stale_and_fresh_is_not(self):
        self.eps()
        report = self.audit()
        self.assertEqual(report["inputs"]["seadex"], {"synced_at": None, "stale": True})
        self.assertEqual(report["inputs"]["anilist"], {"user": None, "fetched_at": None, "stale": True})
        self.viewer()
        self.seadex(A, [])
        report = self.audit()
        self.assertFalse(report["inputs"]["seadex"]["stale"])
        self.assertFalse(report["inputs"]["anilist"]["stale"])
        self.assertEqual(report["inputs"]["inventory_at"], 1000.0)

    def test_probe_coverage(self):
        self.eps(4)
        self.probe(ep(1), result())
        report = self.audit()
        self.assertEqual(report["inputs"]["probe"], {"files_probed": 1, "video_files": 4, "coverage": 0.25,
                                                     "changed": 0})


class Probe(Lab):
    def setUp(self):
        super().setUp()
        self.eps(3)

    def test_x265_crf_sample_settings_are_compact(self):
        self.probe(ep(1), result(kbps=1600))
        p = self.doc()["probe"]
        self.assertEqual(p["settings"], {"crf": 21.2, "rc": "crf", "aq-mode": 3, "psy-rd": 0.8, "sao": "limit",
                                         "bit_depth": 10, "bitrate_kbps": 1600})
        self.assertEqual(p["encoder"], {"library": "x265", "version": "3.5", "style": "crf", "crf_band": "within",
                                        "notes": ["crf_within_band"]})
        self.assertEqual((p["container"], p["fonts"], p["chapters"]), ("matroska", 1, {"present": True, "count": 3}))
        self.assertEqual(p["video"]["bit_depth"], 10)
        self.assertEqual(p["subtitles"], [{"format": "ass", "lang": "eng"}])
        self.assertEqual(p["sampled"], 1)

    def test_untouched_two_pass_stream_is_not_judged_by_crf(self):
        self.probe(ep(1), result(bits=8, kbps=8000, library="x264", settings=X264_STREAM))
        p = self.doc()["probe"]
        self.assertEqual(p["encoder"]["style"], "untouched_stream")
        self.assertIsNone(p["encoder"]["crf_band"])
        self.assertEqual(p["encoder"]["notes"], ["untouched_stream_video"])
        self.assertEqual((p["settings"]["rc"], p["settings"]["crf"], p["settings"]["psy-rd"], p["settings"]["sao"]),
                         ("2pass", None, 0.4, None))
        self.assertNotIn("low_bpp_8bit", self.codes())                       # 8 Mbps is a healthy stream

    def test_two_pass_with_bframes_is_plain_bitrate(self):
        self.probe(ep(1), result(library="x264", settings={"rc": "abr", "bitrate": "3000", "bframes": "3"}))
        self.assertEqual(self.doc()["probe"]["encoder"]["style"], "bitrate")
        self.probe(ep(1), result(library="x264", settings={"rc": "abr", "bitrate": "3000", "bframes": "0"}))
        self.assertEqual(self.doc()["probe"]["encoder"]["style"], "untouched_stream")

    def test_no_settings_on_a_web_dl_is_an_untouched_stream(self):
        self.title(files=[(f"{SHOW} - 01 [1080p WEB-DL].mkv", 250 * MB)])
        self.probe(f"{SHOW} - 01 [1080p WEB-DL].mkv", result(encoder=False))
        e = self.doc()["probe"]["encoder"]
        self.assertEqual((e["library"], e["style"]), (None, "untouched_stream"))
        self.assertEqual(e["notes"], ["untouched_stream_video", "no_encoder_settings"])

    def test_no_settings_elsewhere_is_unknown(self):
        self.probe(ep(1), result(encoder=False))
        e = self.doc()["probe"]["encoder"]
        self.assertEqual((e["style"], e["notes"]), ("unknown", ["no_encoder_settings"]))

    def test_crf_band_is_informational(self):
        for crf, band in (("17.9", "below"), ("18", "within"), ("24", "within"), ("24.1", "above")):
            self.probe(ep(1), result(settings={**X265, "crf": crf}))
            with self.subTest(crf=crf):
                self.assertEqual(self.doc()["probe"]["encoder"]["crf_band"], band)
                self.assertFalse([f for f in self.doc()["flags"] if "crf" in f["code"]])
        self.probe(ep(1), result(bits=8, settings={**X265, "crf": "30"}))              # band is x265 10-bit only
        self.assertIsNone(self.doc()["probe"]["encoder"]["crf_band"])

    def test_sao_modes(self):
        for settings, mode in (({"no-sao": True}, "off"), ({"limit-sao": True, "sao": True}, "limit"),
                               ({"selective-sao": "2", "sao": True}, "selective"), ({"sao": True}, "on"),
                               ({"selective-sao": "0"}, None)):
            with self.subTest(settings=settings):
                self.assertEqual(audit.settings_summary("x265", settings)["sao"], mode)
        self.assertIsNone(audit.settings_summary("x264", {"sao": True})["sao"])

    def test_x264_underscored_keys(self):
        s = audit.settings_summary("x264", {"rc": "crf", "crf": "18.0", "psy_rd": "0.40:0.00", "aq": "1:1.00"})
        self.assertEqual((s["crf"], s["psy-rd"], s["aq-mode"]), (18.0, 0.4, 1))
        self.assertEqual(audit.settings_summary("x265", None), {"crf": None, "rc": None, "aq-mode": None,
                                                                "psy-rd": None, "sao": None})

    def test_representative_sample_comes_from_the_biggest_cluster(self):
        self.title(files=[(ep(1), 250 * MB), (ep(2), 250 * MB), (ep(1, group=OTHER, tags="[720p]"), 100 * MB)])
        self.probe(ep(1, group=OTHER, tags="[720p]"), result(w=1280, h=720, kbps=900), size=100 * MB)
        self.probe(ep(1), result(kbps=1600))
        d = self.doc()
        self.assertEqual(d["probe"]["settings"]["bitrate_kbps"], 1600)
        self.assertEqual(d["probe"]["sampled"], 2)
        self.assertEqual({s["group"] for s in d["probe"]["samples"]}, {GROUP, OTHER})

    def test_garbage_probe_result_is_ignored(self):
        title = self.ids[KEY]
        self.db.execute("INSERT INTO probe_results VALUES (?,?,?,?,?,?,?,?,?)",
                        (title, f"{SHOW}/{ep(1)}", 250 * MB, 5, mkvprobe.PROBE_VERSION, "ok", "not json", 1, NOW))
        self.db.commit()
        self.assertIn("probe_missing", self.codes())


class Report(Lab):
    def setUp(self):
        super().setUp()
        self.eps(2)
        self.identify()
        self.seadex(A, [{"group": OTHER, "median": 300 * MB}])
        self.probe(ep(1), result())

    def test_hash_is_deterministic_and_ignores_the_clock_but_not_the_content(self):
        first = self.audit()
        again = self.audit(now=lambda: NOW + 60)
        self.assertEqual(first["report_sha256"], again["report_sha256"])
        self.assertNotEqual(first["generated_at"], again["generated_at"])
        body = {k: v for k, v in first.items() if k not in ("generated_at", "report_sha256")}
        from kura.manifest_store import canonical
        self.assertEqual(first["report_sha256"], hashlib.sha256(canonical(body).encode()).hexdigest())
        self.seadex(A, [{"group": GROUP, "median": 300 * MB}])
        self.assertNotEqual(self.audit()["report_sha256"], first["report_sha256"])

    def test_report_is_stored_unless_disabled(self):
        self.audit()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_reports").fetchone()[0], 0)
        report = self.audit(store_report=True)
        row = self.db.execute("SELECT library, created_at, report_sha256, report FROM audit_reports").fetchone()
        self.assertEqual(row[:3], ("home", NOW, report["report_sha256"]))
        self.assertEqual(json.loads(row[3]), json.loads(json.dumps(report)))

    def test_only_recent_reports_are_kept(self):
        for n in range(audit.KEEP_REPORTS + 5):
            self.audit(store_report=True, now=lambda n=n: NOW + n)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_reports").fetchone()[0], audit.KEEP_REPORTS)
        self.assertEqual(self.db.execute("SELECT MAX(created_at) FROM audit_reports").fetchone()[0],
                         NOW + audit.KEEP_REPORTS + 4)

    def test_report_is_json_serialisable_and_ascii(self):
        text = json.dumps(self.audit(), ensure_ascii=True)
        self.assertIn(KEY, text)

    def test_offline_and_never_opens_the_library(self):
        def boom(*a, **k):
            raise AssertionError("network or library access")
        with patch.object(socket.socket, "connect", boom), patch.object(socket, "create_connection", boom), \
                patch.object(socket, "getaddrinfo", boom), patch.object(rofs, "open_root", boom), \
                patch.object(rofs, "open_file_ro", boom):
            report = self.audit(store_report=True)
        self.assertEqual(report["counts"]["titles"], 1)

    def test_filters_ordering_and_truncation(self):
        self.title(name="Kura Big Show", files=[(f"Kura Big Show - {n:02d}.mkv", 900 * MB) for n in (1, 2)])
        self.title(name="Kura Quiet Show", files=[(f"Kura Quiet Show - {n:02d}.mkv", 250 * MB) for n in (1, 2)])
        self.db.execute("INSERT INTO title_identities(library, title, status, source, updated_at)"
                        " VALUES ('home','main/Kura Quiet Show','not_anime','owner',1.0)")
        self.probe("Kura Quiet Show - 01.mkv", result(), name="Kura Quiet Show")
        self.db.commit()
        report = self.audit()
        self.assertEqual(report["counts"]["titles"], 3)
        self.assertEqual(report["titles"][-1]["title"], "main/Kura Quiet Show")           # quietest last
        only = self.audit(flags=["size_above_band"])
        self.assertEqual([t["title"] for t in only["titles"]], ["main/Kura Big Show"])
        self.assertEqual(only["counts"]["titles"], 3)                                      # counts cover all audited
        cut = self.audit(limit=1)
        self.assertEqual((len(cut["titles"]), cut["titles_truncated"]), (1, 2))
        chosen = self.audit(titles=["main/Kura Big Show"])
        self.assertEqual((chosen["counts"]["titles"], len(chosen["titles"])), (1, 1))
        self.assertEqual(report["counts"]["by_flag"]["size_above_band"], 1)
        self.assertEqual(sum(report["counts"]["by_suggestion"].values()), 3)
        self.assertEqual(report["counts"]["by_policy"], {"archival": 0, "efficient": 3})

    def test_library_is_unchanged_in_state_besides_the_report(self):
        before = {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("library_titles", "library_files", "probe_results", "seadex_torrents",
                            "title_identities", "manifest_links")}
        self.audit(store_report=True)
        after = {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        self.assertEqual(before, after)


class EndToEnd(unittest.TestCase):
    """Generated Matroska files, inventoried and probed for real, then audited."""

    def test_probe_then_audit(self):
        tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lib = tmp / "lib"
        show = lib / SHOW
        show.mkdir(parents=True)
        (show / ep(1)).write_bytes(mkvgen.build(codec="hevc", settings=mkvgen.X265_TEXT, sei="private",
                                                subs=(("S_TEXT/ASS", "eng", None),)))
        (show / ep(2, group=OTHER, tags="[720p]")).write_bytes(mkvgen.build())
        store = Store(tmp / "state")
        self.addCleanup(store.db.close)
        inv = scan(lib, "main")
        ManifestRepo(store.db).save_inventory("home", inv.root_dev, [
            {"rel_path": f"main/{t.rel_path}", "kind": t.kind, "total_bytes": t.total_bytes,
             "newest_mtime_ns": t.newest_mtime_ns, "blocked": list(t.blocked),
             "files": [f.json() for f in t.files]} for t in inv.titles], inv.scanned_at)
        out = probe_mod.probe_library(store, library="home", roots={"main": lib})
        self.assertEqual(out["probed"], 2)
        report = audit.run(store, library="home", now=lambda: NOW, store_report=False)
        d = report["titles"][0]
        self.assertEqual(d["probe"]["sampled"], 2)
        self.assertEqual(d["probe"]["encoder"]["library"] in ("x265", "x264"), True)
        self.assertIn("mixed_groups", {f["code"] for f in d["flags"]})
        self.assertIn("ass_missing_fonts", {f["code"] for f in d["flags"]})
        self.assertEqual({s["group"] for s in d["probe"]["samples"]}, {GROUP, OTHER})


class Cli(Lab):
    def setUp(self):
        super().setUp()
        self.store.db.close()
        self.home = self.tmp / "home"
        self.state = self.home / ".local" / "share" / "kura"      # where the CLI looks for state
        self.state.parent.mkdir(parents=True)
        shutil.copytree(self.tmp / "state", self.state)
        self.store = Store(self.state)
        self.addCleanup(self.store.db.close)
        self.db = self.store.db

    def test_envelope_and_keys(self):
        self.eps()
        self.identify()
        retain = self.tmp / "retain.txt"
        retain.write_text(f"{A}\n")
        over = self.tmp / "over.txt"
        over.write_text(f"{A} archival\n")
        self.db.commit()
        code, env = run("audit", "--library", "home", "--retain", str(retain), "--policy-overrides", str(over),
                        "--limit", "5", "--no-store", home=str(self.home))
        self.assertEqual((code, env["success"], env["phase"]), (0, True, "audit"))
        data = env["data"]
        self.assertEqual(set(data), {"library", "generated_at", "inputs", "counts", "titles", "titles_truncated",
                                     "report_sha256"})
        self.assertEqual(data["titles"][0]["policy"], "archival")
        self.assertTrue(data["titles"][0]["retained"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_reports").fetchone()[0], 0)
        code, env = run("audit", "--library", "home", "--flag", "retained", "--title", KEY, "--user", "nobody",
                        home=str(self.home))
        self.assertEqual((code, env["error"]["code"]), (2, "invalid_arguments"))
        code, env = run("audit", "--library", "home", home=str(self.home))
        self.assertEqual(code, 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_reports").fetchone()[0], 1)

    def test_errors_are_envelopes(self):
        for argv, expected in ((("audit", "--library", "home", "--flag", "nope"), "invalid_arguments"),
                               (("audit", "--library", "home", "--limit", "0"), "invalid_arguments"),
                               (("audit", "--library", "home"), "no_inventory"),
                               (("audit", "--library", "home", "--retain", str(self.tmp / "missing.txt")),
                                "invalid_policy")):
            with self.subTest(argv=argv):
                code, env = run(*argv, home=str(self.home))
                self.assertEqual((code, env["success"], env["error"]["code"]), (2, False, expected))

    def test_overrides_file_needs_tags(self):
        self.eps()
        bad = self.tmp / "bad.txt"
        bad.write_text(f"{A}\n")
        code, env = run("audit", "--library", "home", "--policy-overrides", str(bad), home=str(self.home))
        self.assertEqual(env["error"]["code"], "invalid_policy")


if __name__ == "__main__":
    unittest.main()
