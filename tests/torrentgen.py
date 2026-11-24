"""Synthetic torrent generator for tests. Descriptors and payloads never enter Git.

Builds v1 torrents (single- or multi-file) with 16 KiB pieces, plus the matching file
tree on disk under a caller-supplied temp directory. Everything is deterministic: the
same arguments always produce the same bytes and the same infohash. Only synthetic
names and reserved hosts (``*.example``, ``*.invalid``, ``*.test``) belong here.

Typical use::

    built = torrentgen.build("Kura Sample Series",
                             [("ep01.mkv", payload("a", 40000)), ("ep02.mkv", payload("b", 50000))],
                             pad=True, announce="http://tracker.example/announce")
    parse(built.data)                      # the descriptor bytes
    content = built.write_tree(tmp_root)   # tmp_root/"Kura Sample Series"/ep01.mkv ...

Hostile or odd descriptors are produced with ``info_extra`` / ``root_extra`` (raw bencode
keys, ``None`` deletes the key) and by passing deliberately bad path components; the
builder itself performs no validation so the code under test sees the raw shape.
"""
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path

from kura.torrent import encode

PIECE = 16384


def payload(label: str, size: int) -> bytes:
    """Deterministic, non-repeating bytes: every piece hashes differently."""
    out, counter = bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(f"{label}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def piece_hashes(content: bytes, piece_length: int = PIECE) -> bytes:
    return b"".join(hashlib.sha1(content[i:i + piece_length]).digest()
                    for i in range(0, len(content), piece_length))


@dataclass(frozen=True)
class Entry:
    """One torrent file. ``pad`` entries are BEP 47 padding and never exist on disk."""
    path: tuple[str, ...]
    data: bytes
    pad: bool = False


@dataclass(frozen=True)
class Built:
    data: bytes              # complete .torrent descriptor
    info: dict               # the bencodable info dict (after overrides)
    infohash: str            # sha1 of the canonical info dict
    name: str
    entries: tuple[Entry, ...]
    piece_length: int
    multi_file: bool

    @property
    def content(self) -> bytes:
        """The byte stream the pieces cover (pad files are zeros)."""
        return b"".join(e.data for e in self.entries)

    def write_tree(self, root: Path) -> Path:
        """Create the payload under ``root`` and return its top path (``root/name``).

        Skips pad files. Refuses hostile components so a test cannot escape its temp dir.
        """
        top = Path(root) / self.name
        for e in self.entries:
            if e.pad:
                continue
            parts = e.path if self.multi_file else ()
            for part in parts:
                if part in ("", ".", "..") or "/" in part or "\\" in part or "\0" in part:
                    raise ValueError(f"refusing to write hostile component {part!r}")
            target = top.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(e.data)
        return top


def _path(value) -> tuple[str, ...]:
    return tuple(value.split("/")) if isinstance(value, str) else tuple(value)


def _file_tree(entries: list[Entry], name: str) -> dict:
    tree: dict = {}
    for e in entries:
        if e.pad:
            continue
        node = tree
        for part in e.path:
            node = node.setdefault(part.encode(), {})
        node[b""] = {b"length": len(e.data)}
    return {name.encode(): tree}


def build(name: str = "Kura Sample Series", files=None, *, piece_length: int = PIECE,
          size: int = 40000, pad: bool = False, private=None, announce: str | None = None,
          announce_list: list[list[str]] | None = None, source: str | None = None,
          hybrid: bool = False, pure_v2: bool = False,
          info_extra: dict | None = None, root_extra: dict | None = None) -> Built:
    """Build a v1 torrent.

    ``files=None`` makes a single-file torrent of ``size`` bytes named ``name``.
    Otherwise ``files`` is a list of ``(path, bytes)`` (or a dict) where ``path`` is a
    ``"a/b.mkv"`` string or a tuple of components, and the torrent is multi-file.
    ``pad=True`` inserts BEP 47 pad files so every file starts on a piece boundary.
    ``private`` is written verbatim when not None (so ``private=2`` is expressible).
    ``hybrid`` adds a v2 marker (``meta version`` plus a stub ``file tree``);
    ``pure_v2`` drops the v1 ``pieces``/``files`` data entirely.
    """
    multi = files is not None
    if multi:
        items = list(files.items()) if isinstance(files, dict) else list(files)
        real = [Entry(_path(p), d) for p, d in items]
    else:
        real = [Entry((), payload(name, size))]
    entries: list[Entry] = []
    for i, e in enumerate(real):
        entries.append(e)
        if pad and multi and i < len(real) - 1 and len(e.data) % piece_length:
            fill = piece_length - len(e.data) % piece_length
            entries.append(Entry((".pad", str(fill)), bytes(fill), pad=True))
    stream = b"".join(e.data for e in entries)

    info: dict = {b"name": name.encode(), b"piece length": piece_length,
                  b"pieces": piece_hashes(stream, piece_length)}
    if multi:
        listing = []
        for e in entries:
            item = {b"length": len(e.data), b"path": [c.encode() for c in e.path]}
            if e.pad:
                item[b"attr"] = b"p"
            listing.append(item)
        info[b"files"] = listing
    else:
        info[b"length"] = len(stream)
    if private is not None:
        info[b"private"] = int(private)
    if source is not None:
        info[b"source"] = source.encode()
    if hybrid or pure_v2:
        info[b"meta version"] = 2
        info[b"file tree"] = _file_tree(entries, name)
    if pure_v2:
        for key in (b"pieces", b"files", b"length"):
            info.pop(key, None)
    for key, value in (info_extra or {}).items():
        if value is None:
            info.pop(key, None)
        else:
            info[key] = value

    root: dict = {b"info": info}
    if announce is not None:
        root[b"announce"] = announce.encode()
    if announce_list is not None:
        root[b"announce-list"] = [[u.encode() for u in tier] for tier in announce_list]
    for key, value in (root_extra or {}).items():
        if value is None:
            root.pop(key, None)
        else:
            root[key] = value
    infohash = hashlib.sha1(encode(info)).hexdigest()
    return Built(encode(root), info, infohash, name, tuple(entries), piece_length, multi)


def piece_count(total: int, piece_length: int = PIECE) -> int:
    return math.ceil(total / piece_length)
