"""Bounded canonical bencode, single-file torrent inspection and piece verification."""
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re

from .models import KuraError
from .safety import regular_file


def encode(value) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(encode(k) + encode(value[k]) for k in sorted(value)) + b"e"
    raise ValueError("Unsupported bencode type")


def decode(data: bytes):
    if not data or len(data) > 4 * 1024**2:
        raise KuraError("invalid_torrent", "Torrent descriptor is empty or too large")
    pos, nodes = 0, 0

    def parse(depth=0):
        nonlocal pos, nodes
        nodes += 1
        if depth > 12 or nodes > 50000 or pos >= len(data):
            raise ValueError("Complexity limit")
        first = data[pos:pos+1]
        if first == b"i":
            end = data.index(b"e", pos)
            raw = data[pos+1:end]
            if not re.fullmatch(rb"0|-?[1-9]\d{0,18}", raw):
                raise ValueError("Invalid integer")
            pos = end + 1
            return int(raw)
        if first in (b"d", b"l"):
            pos += 1
            result = {} if first == b"d" else []
            previous = None
            while data[pos:pos+1] != b"e":
                if first == b"d":
                    key = parse(depth+1)
                    if not isinstance(key, bytes) or previous is not None and key <= previous:
                        raise ValueError("Noncanonical keys")
                    previous = key
                    result[key] = parse(depth+1)
                else:
                    result.append(parse(depth+1))
            pos += 1
            return result
        end = data.index(b":", pos)
        raw = data[pos:end]
        if not re.fullmatch(rb"0|[1-9]\d{0,7}", raw):
            raise ValueError("Invalid byte length")
        size = int(raw)
        pos = end + 1
        if pos + size > len(data):
            raise ValueError("Truncated string")
        value = data[pos:pos+size]
        pos += size
        return value
    try:
        value = parse()
        if pos != len(data):
            raise ValueError("Trailing data")
        return value
    except (ValueError, IndexError, RecursionError) as e:
        raise KuraError("invalid_torrent", "Malformed or noncanonical torrent") from e


@dataclass(frozen=True)
class Torrent:
    infohash: str
    name: str
    length: int
    piece_length: int
    pieces: bytes
    sanitized: bytes


def inspect(data: bytes, expected_hash: str, max_bytes: int) -> Torrent:
    root = decode(data)
    info = root.get(b"info") if isinstance(root, dict) else None
    allowed = {b"name", b"length", b"piece length", b"pieces", b"private", b"source"}
    if not isinstance(info, dict) or not set(info) <= allowed:
        raise KuraError("unsupported_torrent", "MVP requires one regular file, not batches, paths or extended metadata")
    ih = hashlib.sha1(encode(info)).hexdigest()
    if ih != expected_hash:
        raise KuraError("hash_mismatch", "Descriptor does not match the selected release infohash")
    try:
        name = info[b"name"].decode("utf-8")
        length, piece_length, pieces = info[b"length"], info[b"piece length"], info[b"pieces"]
        if (not name or len(name.encode()) > 240 or name[0] in ".-" or
                any(c in name for c in '/\\:') or any(ord(c) < 32 or ord(c) == 127 for c in name) or
                not re.search(r"\.(mkv|mp4|webm|txt)$", name, re.I)):
            raise ValueError("Unsafe filename")
        if type(length) is not int or not 0 < length <= max_bytes:
            raise ValueError("File exceeds byte budget")
        if type(piece_length) is not int or not 16384 <= piece_length <= 16 * 1024**2:
            raise ValueError("Invalid piece length")
        if not isinstance(pieces, bytes) or len(pieces) != 20 * math.ceil(length / piece_length):
            raise ValueError("Invalid piece hashes")
        if b"private" in info and info[b"private"] not in (0, 1):
            raise ValueError("Invalid private flag")
    except (KeyError, AttributeError, TypeError, UnicodeError, ValueError) as e:
        raise KuraError("unsafe_torrent", "Invalid filename, file size or piece metadata") from e
    # Preserve exact canonical infohash; discard ALL untrusted trackers/webseeds/URLs.
    return Torrent(ih, name, length, piece_length, pieces, encode({b"info": info}))


def verify(torrent: Torrent, directory: Path) -> dict:
    path = directory / torrent.name
    try:
        regular_file(path)
        if path.stat().st_size != torrent.length or (directory / (torrent.name + ".aria2")).exists():
            raise KuraError("verification_failed", "File size incomplete or aria2 control file remains")
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for start in range(0, len(torrent.pieces), 20):
                chunk = f.read(torrent.piece_length)
                digest.update(chunk)
                if hashlib.sha1(chunk).digest() != torrent.pieces[start:start+20]:
                    raise KuraError("verification_failed", "Downloaded piece hash mismatch")
            if f.read(1):
                raise KuraError("verification_failed", "Unexpected trailing bytes")
        return {"path": str(path), "size_bytes": torrent.length, "sha256": digest.hexdigest(),
                "pieces_verified": len(torrent.pieces) // 20}
    except OSError as e:
        raise KuraError("verification_failed", "Expected downloaded file is missing or unreadable") from e
