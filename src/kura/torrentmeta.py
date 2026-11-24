"""Read-only, bounded metadata for single- and multi-file v1 (and hybrid) torrents.

This is for cataloguing existing libraries, not for acquisition: ``torrent.inspect`` stays
the strict single-file gate. Nothing here touches the network or the filesystem. Announce
URLs may embed private-tracker credentials, so the JSON form exposes hosts only and the
raw URLs are carried solely so ``protect.magnet`` can rebuild a public, credential-free
magnet.
"""
from dataclasses import dataclass, field
import hashlib
import math
import re
from urllib.parse import urlsplit
import unicodedata

from .models import KuraError
from .torrent import decode, encode

MAX_DESCRIPTOR = 32 * 1024**2
MAX_DEPTH = 32
MAX_NODES = 500_000
MIN_PIECE, MAX_PIECE = 16 * 1024, 64 * 1024**2
MAX_FILES = 100_000
MAX_FILE_LENGTH = 2**50
MAX_PATH_DEPTH = 64
MAX_ANNOUNCE, MAX_URL_BYTES = 200, 2048
MAX_SOURCE_BYTES = 256

_HOST = re.compile(r"[a-z0-9._-]+|[0-9a-f:.]+")


@dataclass(frozen=True)
class TorrentFile:
    index: int
    path: tuple[str, ...]   # includes the torrent name for multi-file torrents
    length: int
    offset: int             # byte offset in the piece stream
    pad: bool               # BEP 47 padding: not a real file


@dataclass(frozen=True)
class TorrentMeta:
    infohash: str
    name: str
    multi_file: bool
    piece_length: int
    pieces: bytes = field(repr=False)
    files: tuple[TorrentFile, ...]
    total_length: int
    private: bool
    announce_urls: tuple[str, ...] = field(repr=False)  # may embed credentials
    source: str | None
    hybrid: bool
    descriptor_sha256: str
    # Additions beyond the minimal field set; defaulted so callers may construct without them.
    announce_valid: bool = True   # False: some announce data was malformed, oversized or truncated
    info_bytes: bytes = field(default=b"", repr=False, compare=False)  # canonical bencoded info dict

    def json(self) -> dict:
        """Hosts only; announce URLs (which can carry passkeys) are never emitted."""
        hosts = sorted({h for h in map(tracker_host, self.announce_urls) if h})
        return {
            "infohash": self.infohash, "name": self.name, "multi_file": self.multi_file,
            "piece_length": self.piece_length, "piece_count": len(self.pieces) // 20,
            "total_length": self.total_length, "file_count": sum(not f.pad for f in self.files),
            "pad_files": sum(f.pad for f in self.files), "private": self.private,
            "tracker_hosts": hosts, "announce_valid": self.announce_valid,
            "source": self.source, "hybrid": self.hybrid,
            "descriptor_sha256": self.descriptor_sha256,
            "files": [{"index": f.index, "path": "/".join(f.path), "length": f.length,
                       "pad": f.pad} for f in self.files],
        }


def tracker_host(url: str) -> str | None:
    """Lowercase ASCII (IDNA) host of an announce URL, or None if it is not trustworthy.

    Anything a stricter or looser URL parser could read differently (backslashes,
    whitespace, control bytes, percent-escapes in the host) yields None, which callers
    treat as unparsable and therefore protected.
    """
    if not isinstance(url, str) or "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", parts.scheme):
            return None
        parts.port  # raises ValueError on a non-numeric or out-of-range port
        host = parts.hostname
        if not host:
            return None
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        return None
    return host if host and _HOST.fullmatch(host) else None


def release_group(name: str) -> str | None:
    """Leading ``[Group]`` tag of a release name, at most 80 printable characters."""
    match = re.match(r"\[([^\[\]]{1,80})\]", name)
    if not match:
        return None
    group = match.group(1).strip()
    return group if group and group.isprintable() else None


def _bad(message: str, code: str = "unsafe_torrent"):
    raise KuraError(code, message)


def _component(raw, what: str) -> str:
    if type(raw) is not bytes or not raw or len(raw) > 255:
        _bad(f"Invalid {what}: wrong type or length")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _bad(f"Invalid {what}: not UTF-8")
    if (text in (".", "..") or any(c in text for c in "/\\\0") or
            any(unicodedata.category(c) == "Cc" for c in text)):
        _bad(f"Unsafe {what}")
    return text


def _announce(root: dict) -> tuple[tuple[str, ...], bool]:
    urls: list[str] = []
    valid = True

    def add(raw) -> bool:
        nonlocal valid
        if len(urls) >= MAX_ANNOUNCE:
            valid = False
            return False
        if type(raw) is not bytes or not raw or len(raw) > MAX_URL_BYTES:
            valid = False
            return True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            valid = False
            return True
        if any(ord(c) <= 32 or ord(c) == 127 for c in text):
            valid = False
            return True
        urls.append(text)
        return True

    if b"announce" in root:
        add(root[b"announce"])
    if b"announce-list" in root:
        tiers = root[b"announce-list"]
        if not isinstance(tiers, list):
            valid = False
        else:
            for tier in tiers:
                if not isinstance(tier, list):
                    valid = False
                    continue
                if not all(add(u) for u in tier):
                    break
    return tuple(dict.fromkeys(urls)), valid


def _files(info: dict, name: str) -> tuple[bool, list[tuple[tuple[str, ...], int, bool]]]:
    """Return (multi_file, [(path, length, pad)]) from a validated-shape info dict."""
    if b"files" in info:
        if b"length" in info:
            _bad("Both length and files present")
        listing = info[b"files"]
        if not isinstance(listing, list) or not listing or len(listing) > MAX_FILES:
            _bad("Invalid file list")
        out, seen, prefixes = [], set(), set()
        for item in listing:
            if not isinstance(item, dict):
                _bad("Invalid file entry")
            length = item.get(b"length")
            if type(length) is not int or not 0 <= length <= MAX_FILE_LENGTH:
                _bad("Invalid file length")
            attr = item.get(b"attr", b"")
            if type(attr) is not bytes:
                _bad("Invalid file attributes")
            if b"l" in attr or b"symlink path" in item:
                _bad("Symlink entries are not supported", "unsupported_torrent")
            pad = b"p" in attr
            raw = item[b"path.utf-8"] if b"path.utf-8" in item else item.get(b"path")
            if (not isinstance(raw, list) or not raw or len(raw) > MAX_PATH_DEPTH):
                _bad("Invalid file path")
            path = (name, *(_component(c, "path component") for c in raw))
            if not pad:
                if path in seen or path in prefixes:
                    _bad("Duplicate or conflicting file path")
                for i in range(2, len(path)):
                    if path[:i] in seen:
                        _bad("Duplicate or conflicting file path")
                seen.add(path)
                prefixes.update(path[:i] for i in range(2, len(path)))
            out.append((path, length, pad))
        return True, out
    length = info.get(b"length")
    if type(length) is not int or not 0 <= length <= MAX_FILE_LENGTH:
        _bad("Invalid file length")
    attr = info.get(b"attr", b"")
    if type(attr) is not bytes:
        _bad("Invalid file attributes")
    if b"l" in attr or b"symlink path" in info:
        _bad("Symlink entries are not supported", "unsupported_torrent")
    return False, [((name,), length, False)]


def parse(data: bytes) -> TorrentMeta:
    """Parse a v1 or hybrid descriptor. Raises KuraError; never returns a half-trusted result."""
    root = decode(data, max_bytes=MAX_DESCRIPTOR, max_depth=MAX_DEPTH, max_nodes=MAX_NODES)
    info = root.get(b"info") if isinstance(root, dict) else None
    if not isinstance(info, dict):
        _bad("Descriptor has no info dictionary", "invalid_torrent")
    v2_marker = info.get(b"meta version") == 2 or b"file tree" in info
    if b"pieces" not in info:
        if v2_marker:
            _bad("BitTorrent v2-only torrents are not supported", "unsupported_torrent")
        _bad("Missing piece hashes")
    name = _component(info[b"name.utf-8"] if b"name.utf-8" in info else info.get(b"name"), "name")
    multi, listing = _files(info, name)

    piece_length, pieces = info.get(b"piece length"), info[b"pieces"]
    if type(piece_length) is not int or not MIN_PIECE <= piece_length <= MAX_PIECE:
        _bad("Invalid piece length")
    total = sum(length for _, length, _ in listing)
    if type(pieces) is not bytes or len(pieces) != 20 * math.ceil(total / piece_length):
        _bad("Piece hashes do not match the total length")

    private = info.get(b"private", 0)
    if type(private) is not int or private not in (0, 1):
        _bad("Invalid private flag")
    source = None
    if b"source" in info:
        raw = info[b"source"]
        if type(raw) is not bytes or len(raw) > MAX_SOURCE_BYTES:
            _bad("Invalid source tag")
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            _bad("Invalid source tag")
        if any(unicodedata.category(c) == "Cc" for c in source):
            _bad("Invalid source tag")

    files, offset = [], 0
    for index, (path, length, pad) in enumerate(listing):
        files.append(TorrentFile(index, path, length, offset, pad))
        offset += length
    canonical = encode(info)
    urls, announce_ok = _announce(root)
    return TorrentMeta(
        infohash=hashlib.sha1(canonical).hexdigest(), name=name, multi_file=multi,
        piece_length=piece_length, pieces=pieces, files=tuple(files), total_length=total,
        private=bool(private), announce_urls=urls, source=source, hybrid=v2_marker,
        descriptor_sha256=hashlib.sha256(data).hexdigest(),
        announce_valid=announce_ok, info_bytes=canonical)


def sanitized_info(meta_or_data) -> bytes:
    """Canonical ``{info}`` bencode with trackers, webseeds and every other URL dropped."""
    meta = meta_or_data if isinstance(meta_or_data, TorrentMeta) else parse(meta_or_data)
    return b"d4:info" + meta.info_bytes + b"e"
