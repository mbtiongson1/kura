"""A hand-written fake of the SeaDex PocketBase API. Synthetic records only; no sockets.

``FakeSeaDex.transport`` has the signature of ``kura.sources.seadex.fetch``: it receives the
URL the client built, checks it, and answers from in-memory records.
"""
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

FAKE_GROUP = "Kura Encodes"
FAKE_TRACKER = "ExampleTracker"


def digest(label: str) -> str:
    """A deterministic 40-hex infohash computed at test time (never a literal)."""
    return hashlib.sha1(label.encode()).hexdigest()


def rid(n: int) -> str:
    return f"rec{n:012d}"[:15]


def stamp(n: int) -> str:
    return f"2025-01-{1 + n // 86400 % 28:02d} {n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}.123Z"


def entry(n: int, al: int, trs=(), **extra) -> dict:
    return {"id": rid(n), "alID": al, "incomplete": False, "notes": "Kura Sample note",
            "theoreticalBest": "", "comparison": "https://compare.example/c/one,https://compare.example/c/two",
            "trs": [rid(t) for t in trs], "updated": stamp(n), **extra}


def torrent(n: int, label=None, *, tracker="Nyaa", group=FAKE_GROUP, best=True, **extra) -> dict:
    label = label or f"torrent-{n}"
    row = {"id": rid(n), "infoHash": digest(label), "releaseGroup": group, "tracker": tracker,
           "url": f"https://nyaa.si/view/{999000000 + n}", "isBest": best, "dualAudio": False,
           "tags": [], "updated": stamp(n), "files": [{"name": f"{label} - 01.mkv", "length": 1000}]}
    row.update(extra)
    return row


def private_torrent(n: int) -> dict:
    return torrent(n, tracker=FAKE_TRACKER, infoHash="<redacted>", url="/torrents.php?id=1&torrentid=2")


class Clock:
    """One fake wall clock; sleeping advances it."""

    def __init__(self, now=1_800_000_000.0):
        self.now, self.slept = now, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeSeaDex:
    def __init__(self, entries=(), torrents=()):
        self.entries = {e["id"]: e for e in entries}
        self.torrents = {t["id"]: t for t in torrents}
        self.urls = []
        self.fail = None            # a KuraError (or any exception) raised by the next call

    def _match(self, record, expression):
        if expression is None:
            return True
        m = re.fullmatch(r'updated>"([^"]+)"', expression)
        if m:
            return record["updated"] > m.group(1)
        ids = re.fullmatch(r'id="[a-z0-9]+"(\|\|id="[a-z0-9]+")*', expression)
        if ids:
            return record["id"] in re.findall(r'id="([a-z0-9]+)"', expression)
        raise AssertionError(f"fake does not understand filter {expression!r}")

    def transport(self, url: str) -> bytes:
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "seadex.moe", url
        match = re.fullmatch(r"/api/collections/(entries|torrents)/records", parts.path)
        assert match, url
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        records = list((self.entries if match.group(1) == "entries" else self.torrents).values())
        records = sorted((r for r in records if self._match(r, query.get("filter"))), key=lambda r: r["id"])
        per, page = int(query["perPage"]), int(query["page"])
        fields = query["fields"].split(",")
        window = records[(page - 1) * per: page * per]
        items = [{k: r[k] for k in fields if k in r} for r in window]
        return json.dumps({"page": page, "perPage": per, "totalItems": len(records),
                           "totalPages": max(1, -(-len(records) // per)) if records else 1,
                           "items": items}).encode()
