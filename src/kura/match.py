"""Match parsed torrents to library titles by name. Pure: no filesystem, no network.

A torrent is linked to a title only when exactly one place in the inventory could hold
it. Three strategies, tried together and compared:

  root_dir   a top-level library entry is named exactly like the torrent
  nested_dir the torrent's name is a direct child of a top-level directory
             (``Title/<torrent name>``), as a directory for a multi-file torrent or as a
             file for a single-file torrent
  file_name  single-file torrents only: a file with that exact name sits anywhere below a
             top-level directory

Several distinct places (two titles, two roots, a loose copy and a nested one) make the
torrent ``ambiguous`` and it is not linked at all. Matching never inspects content; the
piece verification that follows is what makes a link mean anything. Names are compared
exactly (no case folding, no fuzzy matching).

A mapping sends every non-pad torrent file index to path components below the library
root, ready for ``pieceverify.verify_tree``.
"""
from dataclasses import dataclass

from .inventory import Inventory
from .pieceverify import default_mapping
from .torrentmeta import TorrentMeta

PRIORITY = {"root_dir": 0, "nested_dir": 1, "file_name": 2}


@dataclass(frozen=True)
class Match:
    infohash: str
    title_rel: str                       # top-level entry, relative to its root
    mapping: dict[int, tuple[str, ...]]  # torrent file index -> parts below the root
    strategy: str
    root: str = ""                       # inventory label


class _Index:
    """Name lookups over one inventory, built once and shared by every torrent."""

    def __init__(self, inv: Inventory):
        self.label = inv.label
        self.titles = {t.rel_path: t for t in inv.titles}
        self.file_children: dict[str, set[str]] = {}   # child name -> titles holding it as a file
        self.dir_children: dict[str, set[str]] = {}    # child name -> titles holding it as a dir
        self.by_basename: dict[str, set[tuple[str, ...]]] = {}  # basename -> parent parts
        for title in inv.titles:
            for entry in title.files:
                parts = tuple(entry.rel_path.split("/"))
                if len(parts) == 2:
                    self.file_children.setdefault(parts[1], set()).add(title.rel_path)
                elif len(parts) > 2:
                    self.dir_children.setdefault(parts[1], set()).add(title.rel_path)
                if len(parts) >= 2:
                    self.by_basename.setdefault(parts[-1], set()).add(parts[:-1])

    def candidates(self, meta: TorrentMeta) -> dict[tuple[str, ...], tuple[str, str]]:
        """{base parts: (strategy, title)} for every place this torrent could live."""
        found: dict[tuple[str, ...], tuple[str, str]] = {}

        def add(base, strategy, title):
            if base not in found or PRIORITY[strategy] < PRIORITY[found[base][0]]:
                found[base] = (strategy, title)

        name = meta.name
        title = self.titles.get(name)
        if title is not None and title.kind == ("dir" if meta.multi_file else "file"):
            add((), "root_dir", name)
        holders = self.dir_children if meta.multi_file else self.file_children
        for top in holders.get(name, ()):
            add((top,), "nested_dir", top)
        if not meta.multi_file:
            for parent in self.by_basename.get(name, ()):
                add(parent, "file_name", parent[0])
        return found


def match_many(inventories: list[Inventory], metas: list[TorrentMeta]
               ) -> tuple[list[Match], list[dict]]:
    """Match against several library roots at once; a torrent found in two roots is ambiguous."""
    indexes = [_Index(inv) for inv in inventories]
    matches: list[Match] = []
    unmatched: list[dict] = []
    for meta in metas:
        places = []
        for index in indexes:
            for base, (strategy, title) in index.candidates(meta).items():
                places.append((index.label, title, base, strategy))
        if not places:
            unmatched.append({"infohash": meta.infohash, "name": meta.name, "reason": "no_match"})
        elif len(places) > 1:
            unmatched.append({"infohash": meta.infohash, "name": meta.name, "reason": "ambiguous"})
        else:
            root, title, base, strategy = places[0]
            matches.append(Match(meta.infohash, title, default_mapping(meta, base), strategy, root))
    return matches, unmatched


def match(inv: Inventory, metas: list[TorrentMeta]) -> tuple[list[Match], list[dict]]:
    """Match against one inventory. Returns (matches, unmatched rows with a reason)."""
    return match_many([inv], metas)
