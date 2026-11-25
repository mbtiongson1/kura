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
torrent ``ambiguous`` and it is not linked at all. Matching never reads file bytes; the
piece verification that follows is what makes a link mean anything. Names are compared
exactly (no case folding, no fuzzy matching).

When none of those finds a place (torrent and folder names differ arbitrarily), the
``content`` fallback maps a torrent to a title by what its files look like. Each non-pad
torrent file is matched to a file of the title with the same basename and the same size;
failing that, a file of at least 64 MiB whose size is unique both in the title and among
the torrent's unclaimed files is matched by size alone. A title qualifies when the mapped
files cover at least 90% of the torrent's bytes, and the title with the most covered bytes
wins; an exact tie between titles is ``ambiguous``. The mapping is per file, so folder
layout and names may differ freely, and a torrent file that found no counterpart is simply
absent from the mapping (verification then reports it missing, so the title cannot become
reacquirable). A local file is never claimed by two torrent files. Everything is built from
one pass over the inventories (size indexes), not one scan per torrent. Content matching
only ever proposes a link: a decoy with the same sizes and different bytes fails piece
verification.

A mapping sends every non-pad torrent file index to path components below the library
root, ready for ``pieceverify.verify_tree``.
"""
from dataclasses import dataclass

from .inventory import Inventory
from .pieceverify import default_mapping
from .torrentmeta import TorrentMeta

PRIORITY = {"root_dir": 0, "nested_dir": 1, "file_name": 2}
CONTENT = "content"
MIN_COVERAGE = (9, 10)                 # matched bytes / torrent bytes, as a fraction
SIZE_ONLY_MIN = 64 * 1024 * 1024       # files this large may match on size alone


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


def _pair(tfiles: list, locals_: list[tuple[str, ...]]) -> dict[int, tuple[str, ...]]:
    """Pair torrent files and title files that share a (basename, size) signature.

    One-to-one needs no guessing. Otherwise each torrent file takes the title file that
    shares its longest trailing path (indexed once, so cost is linear in the files); a tie
    at that length pairs nothing.
    """
    if len(tfiles) == 1 and len(locals_) == 1:
        return {tfiles[0].index: locals_[0]}
    index: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for parts in locals_:
        for j in range(1, len(parts) + 1):
            index.setdefault(parts[-j:], []).append(parts)
    depth = max(len(parts) for parts in locals_)
    out = {}
    for f in tfiles:
        for j in range(min(len(f.path), depth), 0, -1):
            found = index.get(f.path[-j:])
            if found:
                if len(found) == 1:
                    out[f.index] = found[0]
                break
    return out


class _ContentIndex:
    """Size indexes over every inventory file, built in one pass.

    A signature shared by more than MAX_BUCKET files is not distinctive (it names nothing
    in particular) and is dropped, which also bounds the work any one torrent can cause.
    """

    MAX_BUCKET = 256

    def __init__(self, inventories: list[Inventory]):
        # (basename, size) -> [(title key, root-relative parts)]
        self.by_name_size: dict[tuple[str, int], list[tuple[tuple[str, str], tuple[str, ...]]]] = {}
        self.big_by_size: dict[int, list[tuple[tuple[str, str], tuple[str, ...]]]] = {}
        for inv in inventories:
            for title in inv.titles:
                key = (inv.label, title.rel_path)
                for entry in title.files:
                    parts = tuple(entry.rel_path.split("/"))
                    item = (key, parts)
                    self.by_name_size.setdefault((parts[-1], entry.size), []).append(item)
                    if entry.size >= SIZE_ONLY_MIN:
                        self.big_by_size.setdefault(entry.size, []).append(item)
        for table in (self.by_name_size, self.big_by_size):
            for sig in [s for s, items in table.items() if len(items) > self.MAX_BUCKET]:
                del table[sig]

    def best(self, meta: TorrentMeta):
        """``("match", title key, mapping)``, ``("ambiguous",)`` or ``None`` for one torrent."""
        files = [f for f in meta.files if not f.pad]
        total = sum(f.length for f in files)
        if total <= 0:
            return None
        by_index = {f.index: f for f in files}
        tsig: dict[tuple[str, int], list] = {}
        big: dict[int, list] = {}
        for f in files:
            tsig.setdefault((f.path[-1], f.length), []).append(f)
            if f.length >= SIZE_ONLY_MIN:
                big.setdefault(f.length, []).append(f)
        named: dict[tuple[str, str], dict[tuple[str, int], list[tuple[str, ...]]]] = {}
        for sig in tsig:
            for key, parts in self.by_name_size.get(sig, ()):
                named.setdefault(key, {}).setdefault(sig, []).append(parts)
        sized: dict[tuple[str, str], dict[int, list[tuple[str, ...]]]] = {}
        for size in big:
            for key, parts in self.big_by_size.get(size, ()):
                sized.setdefault(key, {}).setdefault(size, []).append(parts)
        need = total * MIN_COVERAGE[0]
        results: list[tuple[int, tuple[str, str], dict[int, tuple[str, ...]]]] = []
        for key in {*named, *sized}:
            reachable = {f.index for sig in named.get(key, ()) for f in tsig[sig]}
            reachable.update(f.index for size in sized.get(key, ()) for f in big[size])
            if sum(by_index[i].length for i in reachable) * MIN_COVERAGE[1] < need:
                continue                       # cannot reach the coverage threshold
            mapping = self._map_title(tsig, named.get(key, {}), sized.get(key, {}), big)
            matched = sum(by_index[i].length for i in mapping)
            if matched * MIN_COVERAGE[1] >= need:
                results.append((matched, key, mapping))
        if not results:
            return None
        results.sort(key=lambda r: r[0], reverse=True)
        if len(results) > 1 and results[1][0] == results[0][0]:
            return ("ambiguous",)
        return ("match", results[0][1], results[0][2])

    @staticmethod
    def _map_title(tsig, named, sized, big) -> dict[int, tuple[str, ...]]:
        chosen: dict[int, tuple[str, ...]] = {}
        for sig, locals_ in named.items():
            chosen.update(_pair(tsig[sig], locals_))
        claims: dict[tuple[str, ...], list[int]] = {}
        for index, parts in chosen.items():
            claims.setdefault(parts, []).append(index)
        mapping = {}
        for parts, indexes in claims.items():
            if len(indexes) == 1:                  # two torrent files never share a local file
                mapping[indexes[0]] = parts
        used = set(claims)                         # contested files stay off limits too
        for size, locals_ in sized.items():
            free = [f for f in big[size] if f.index not in mapping and f.index not in chosen]
            if len(locals_) == 1 and len(free) == 1 and locals_[0] not in used:
                mapping[free[0].index] = locals_[0]
        return mapping


def match_many(inventories: list[Inventory], metas: list[TorrentMeta]
               ) -> tuple[list[Match], list[dict]]:
    """Match against several library roots at once; a torrent found in two roots is ambiguous."""
    indexes = [_Index(inv) for inv in inventories]
    content: _ContentIndex | None = None
    matches: list[Match] = []
    unmatched: list[dict] = []
    for meta in metas:
        places = []
        for index in indexes:
            for base, (strategy, title) in index.candidates(meta).items():
                places.append((index.label, title, base, strategy))
        if not places:
            if content is None:
                content = _ContentIndex(inventories)
            found = content.best(meta)
            if found is None:
                unmatched.append({"infohash": meta.infohash, "name": meta.name, "reason": "no_match"})
            elif found[0] == "ambiguous":
                unmatched.append({"infohash": meta.infohash, "name": meta.name, "reason": "ambiguous"})
            else:
                (root, title), mapping = found[1], found[2]
                matches.append(Match(meta.infohash, title, mapping, CONTENT, root))
        elif len(places) > 1:
            unmatched.append({"infohash": meta.infohash, "name": meta.name, "reason": "ambiguous"})
        else:
            root, title, base, strategy = places[0]
            matches.append(Match(meta.infohash, title, default_mapping(meta, base), strategy, root))
    return matches, unmatched


def match(inv: Inventory, metas: list[TorrentMeta]) -> tuple[list[Match], list[dict]]:
    """Match against one inventory. Returns (matches, unmatched rows with a reason)."""
    return match_many([inv], metas)
