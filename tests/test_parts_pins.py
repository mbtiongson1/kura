"""Owner pins with parts: pins file, `identity pin --part`, show, parts view, prune, media IDs.
Synthetic data only."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import kura.cli as cli
from kura import anilist_list, identity, titleparts
from kura.idlist import parse, parse_pins
from kura.models import KuraError
import torrentgen
from test_manifest import Lab

P = torrentgen.payload
S1, S2, S3, X = 999000031, 999000032, 999000033, 999000099
FILES = [("Season 1/Kura Saga - 01.mkv", P("a", 30000)), ("Season 2/Kura Saga - 01.mkv", P("b", 30000))]
KEY = "main/Kura Saga"


class Base(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)
        self.lab.add("Kura Saga", FILES)
        self.lab.add("Kura Solo")
        self.lab.build()
        self.store = self.lab.store
        self.db = self.store.db
        for i, title in ((S1, "Kura Saga"), (S2, "Kura Saga 2nd Season"), (S3, "Kura Saga Movie")):
            self.db.execute("INSERT INTO anilist_media(anilist_id, title_romaji, format, episodes, status, fetched_at)"
                            " VALUES (?,?,'TV',1,'FINISHED',1.0)", (i, title))
        self.db.commit()

    def pins(self, text):
        path = self.lab.tmp / "pins.tsv"
        path.write_text(text)
        return parse_pins(path)

    def apply(self, text, **kw):
        return identity.apply_pins(self.store, "home", self.pins(text), **kw)

    def parts(self, title=KEY):
        return [(i, sel, o) for i, sel, o in self.db.execute(
            "SELECT anilist_id, selector, ordinal FROM title_parts WHERE library='home' AND title=?"
            " AND source='owner' ORDER BY ordinal, anilist_id", (title,))]

    def net(self, title=KEY):
        return self.db.execute("SELECT anilist_id, status, source, matched, candidates FROM title_identities"
                               " WHERE library='home' AND title=?", (title,)).fetchone()


class ApplyPins(Base):
    TEXT = f"{S1}@auto\t{KEY}\n{S2}@auto\t{KEY}\n"

    def test_writes_parts_and_the_safety_net_row(self):
        out = self.apply(self.TEXT)
        self.assertEqual(out["applied"], {"pinned": 0, "not_anime": 0, "parts": 2})
        self.assertEqual((out["unchanged_parts"], out["kept_parts"], out["replaced_parts"]), (0, [], []))
        self.assertEqual(self.parts(), [(S1, "auto", 1), (S2, "auto", 2)])
        self.assertEqual(self.net(), (S1, "pinned", "owner", "parts:2", json.dumps([S2])))
        # a reader that knows nothing about parts still fails closed for part two
        self.assertEqual(identity.retain_reasons(KEY, identity.load(self.db, "home"), parse_ids(S2)),
                         ["retain_possible"])

    def test_reapplying_is_a_no_op(self):
        self.apply(self.TEXT, now=lambda: 5.0)
        out = self.apply(self.TEXT, now=lambda: 9.0)
        self.assertEqual((out["applied"]["parts"], out["unchanged_parts"]), (0, 2))

    def test_dropping_a_line_keeps_the_part(self):
        self.apply(self.TEXT)
        out = self.apply(f"{S1}@auto\t{KEY}\n")
        self.assertEqual((out["kept_parts"], self.parts()), ([KEY], [(S1, "auto", 1), (S2, "auto", 2)]))

    def test_a_plain_line_replaces_the_parts(self):
        self.apply(self.TEXT)
        out = self.apply(f"{S1}\t{KEY}\n")
        self.assertEqual((out["replaced_parts"], self.parts()), ([KEY], []))
        self.assertEqual(self.net(), (S1, "pinned", "owner", None, "[]"))

    def test_missing_titles_are_reported_and_all_cannot_mix(self):
        out = self.apply(f"{S1}@auto\tmain/Not Here\n")
        self.assertEqual(out["missing"], ["main/Not Here"])
        self.apply(f"{S1}@dir:Season 1\t{KEY}\n")
        with self.assertRaises(KuraError):
            self.apply(f"{S2}@all\t{KEY}\n")
        self.assertEqual(self.parts(), [(S1, "dir:Season 1", 1)])      # the failed file changed nothing

    def test_sync_never_touches_owner_parts(self):
        self.apply(self.TEXT)
        identity.sync(self.store, library="home", aliases=None, refresh=True)
        self.assertEqual(self.parts(), [(S1, "auto", 1), (S2, "auto", 2)])
        self.assertEqual(self.net()[:3], (S1, "pinned", "owner"))


def parse_ids(*ids):
    import tempfile, pathlib
    path = pathlib.Path(tempfile.mkdtemp()) / "r.txt"
    path.write_text("".join(f"{i}\n" for i in ids))
    return parse(path)


class PinCommand(Base):
    def pin(self, *a, **kw):
        return identity.pin(self.store, "home", KEY, *a, **kw)

    def test_add_replace_and_clear_one_part(self):
        self.pin(S1, part="dir:Season 1")
        out = self.pin(S2, part="dir:Season 2")
        self.assertEqual(out["parts"], [{"anilist_id": S1, "selector": "dir:Season 1"},
                                        {"anilist_id": S2, "selector": "dir:Season 2"}])
        self.assertEqual(self.net(), (S1, "pinned", "owner", "parts:2", json.dumps([S2])))
        self.pin(S3, part="dir:Season 2")                          # replaces that selector's part, same ordinal
        self.assertEqual(self.parts(), [(S1, "dir:Season 1", 1), (S3, "dir:Season 2", 2)])
        self.assertEqual(self.pin(clear=True, part="dir:Season 1")["status"], "cleared")
        self.assertEqual(self.net(), (S3, "pinned", "owner", "parts:1", "[]"))
        self.pin(clear=True, part="dir:Season 2")                   # the last removal drops the safety net
        self.assertEqual((self.parts(), self.net()), ([], None))

    def test_auto_parts_accumulate_by_id(self):
        self.pin(S1, part="auto")
        self.pin(S2, part="auto")
        self.pin(S2, part="auto")
        self.assertEqual(self.parts(), [(S1, "auto", 1), (S2, "auto", 2)])

    def test_clear_removes_every_part_and_the_row(self):
        self.pin(S1, part="auto")
        self.pin(S2, part="auto")
        self.assertEqual(self.pin(clear=True)["status"], "cleared")
        self.assertEqual((self.parts(), self.net()), ([], None))
        self.assertEqual(self.pin(clear=True)["status"], "none")

    def test_a_plain_pin_replaces_parts_and_a_part_needs_no_plain_pin(self):
        self.pin(S1, part="auto")
        self.pin(S2)
        self.assertEqual((self.parts(), self.net()[:3]), ([], (S2, "pinned", "owner")))
        with self.assertRaises(KuraError):
            self.pin(S1, part="auto")           # clear the plain decision first

    def test_refusals(self):
        for kw in ({"anilist_id": S1, "part": "guess"}, {"not_anime": True, "part": "auto"},
                   {"anilist_id": S1, "part": "dir:../x"}):
            with self.subTest(kw=kw), self.assertRaises(KuraError):
                self.pin(**kw)
        with self.assertRaises(KuraError):
            identity.pin(self.store, "home", "main/Nope", S1, part="auto")
        self.pin(S1, part="all")
        with self.assertRaises(KuraError):
            self.pin(S2, part="auto")


class Views(Base):
    def setUp(self):
        super().setUp()
        self.apply = lambda text: identity.apply_pins(self.store, "home", self.pins(text))
        self.apply(f"{S1}@dir:Season 1\t{KEY}\n{S2}@dir:Season 2\t{KEY}\n{S3}\tmain/Kura Solo\n")

    def test_show_lists_the_parts(self):
        row = next(t for t in identity.show(self.store, "home")["titles"] if t["title"] == KEY)
        self.assertEqual(row["mode"], "owner")
        self.assertEqual([(p["anilist_id"], p["selector"]) for p in row["parts"]],
                         [(S1, "dir:Season 1"), (S2, "dir:Season 2")])
        solo = next(t for t in identity.show(self.store, "home")["non_media"] if t["title"] == "main/Kura Solo")
        self.assertNotIn("parts", solo)

    def test_parts_view_gives_the_assignment(self):
        out = titleparts.show_assignment(self.store, "home", KEY)
        self.assertEqual((out["mode"], out["status"], out["complete"]), ("owner", "parted", True))
        self.assertEqual({f["local"]: (f["part"], f["rule"]) for f in out["files"]},
                         {"Season 1/Kura Saga - 01.mkv": (S1, "owner_dir"),
                          "Season 2/Kura Saga - 01.mkv": (S2, "owner_dir")})
        single = titleparts.show_assignment(self.store, "home", "main/Kura Solo")
        self.assertEqual((single["mode"], single["files"]), ("single", []))
        with self.assertRaises(KuraError):
            titleparts.show_assignment(self.store, "home", "main/Nope")

    def test_part_ids_get_media_metadata_requests(self):
        self.db.execute("DELETE FROM anilist_media")
        self.db.commit()
        self.assertEqual(anilist_list.media_ids(self.db, [], "home", False), [S1, S2, S3])
        self.assertEqual(anilist_list.media_ids(self.db, [], "home", True), [S1, S2, S3])

    def test_prune_keeps_owner_parts_until_the_library_empties(self):
        self.db.execute("INSERT INTO title_parts VALUES ('home',?,?,'auto','relations',1,1,?,1.0)", ("main/Kura Solo", S3, S3))
        self.db.commit()
        from kura.manifest_store import ManifestRepo
        repo = ManifestRepo(self.db)
        every = repo.titles("home")
        out = repo.prune_titles("home", every, dry_run=False, empties_library=False)
        self.assertEqual(out["parts"], 1)                               # the derived row only
        self.assertEqual(len(self.parts()), 2)
        repo.prune_titles("home", every, dry_run=False, empties_library=True)
        self.assertEqual(self.parts(), [])


class CommandLine(Base):
    def cli(self, *argv):
        buffer = io.StringIO()
        with patch("kura.cli.private_root", return_value=self.lab.state), patch("sys.argv", ["kura", *argv]), \
             contextlib.redirect_stdout(buffer):
            try:
                cli.main()
            except SystemExit as exited:
                return exited.code, json.loads(buffer.getvalue())

    def test_pin_part_and_parts_view(self):
        code, out = self.cli("identity", "pin", "--library", "home", "--title", KEY, "--anilist-id", str(S1),
                             "--part", "dir:Season 1")
        self.assertEqual((code, out["data"]["part"]), (0, "dir:Season 1"))
        code, out = self.cli("identity", "pin", "--library", "home", "--title", KEY, "--anilist-id", str(S2),
                             "--part", "rest")
        self.assertEqual(code, 0)
        code, out = self.cli("identity", "parts", "--library", "home", "--title", KEY)
        self.assertEqual((code, out["data"]["complete"]), (0, True))
        code, out = self.cli("identity", "pin", "--library", "home", "--title", KEY, "--anilist-id", str(S2),
                             "--part", "bogus")
        self.assertEqual((code, out["success"]), (2, False))


if __name__ == "__main__":
    unittest.main()
