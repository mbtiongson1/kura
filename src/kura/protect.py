"""Protection classifier: decides which torrents must never be treated as re-acquirable.

Fail closed. A private flag, a denylisted tracker host or source tag, a protected
infohash, malformed announce data or an unparsable descriptor all mean *protected*.
Private-tracker announce URLs embed credentials, so a magnet link is only produced for
unprotected torrents and only from announce URLs that carry no query, userinfo or
credential-looking path. Pure functions; the only I/O is ``load_policy`` reading its two
files.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from .models import KuraError
from .torrentmeta import TorrentMeta, parse, tracker_host

MAX_POLICY_LINES = 1000
MAX_POLICY_BYTES = 1024**2
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_INFOHASH = re.compile(r"[0-9a-fA-F]{40}")
_SECRET_SEGMENT = re.compile(r"[0-9A-Za-z_-]{20,}")


@dataclass(frozen=True)
class ProtectionPolicy:
    tracker_hosts: tuple[str, ...] = ()
    source_tags: tuple[str, ...] = ()
    infohashes: frozenset[str] = frozenset()
    path_prefixes: tuple[str, ...] = ()
    has_denylist: bool = False
    has_protected_list: bool = False
    sha256: str = ""


@dataclass(frozen=True)
class Protection:
    protected: bool
    reasons: tuple[str, ...]

    def json(self) -> dict:
        return {"protected": self.protected, "reasons": list(self.reasons)}


def _policy_error(line: int | None, message: str):
    details = {"line": line} if line is not None else {}
    raise KuraError("invalid_policy", message, **details)


def _read_lines(path: Path) -> list[tuple[int, str]]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_POLICY_BYTES + 1)
    except OSError as e:
        raise KuraError("invalid_policy", "Policy file is unreadable") from e
    if len(raw) > MAX_POLICY_BYTES:
        _policy_error(None, "Policy file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _policy_error(None, "Policy file is not UTF-8")
    lines = text.splitlines()
    if len(lines) > MAX_POLICY_LINES:
        _policy_error(None, f"Policy file exceeds {MAX_POLICY_LINES} lines")
    out = []
    for number, line in enumerate(lines, 1):
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
        if line:
            out.append((number, line))
    return out


def _policy_host(text: str, number: int) -> str:
    host = text[:-1] if text.endswith(":") else text
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        _policy_error(number, "Invalid host")
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        _policy_error(number, "Invalid host (need a full domain such as tracker.example)")
    return ascii_host


def _path_prefix(text: str, number: int) -> str:
    parts = [p for p in text.split("/")]
    if parts and parts[-1] == "":
        parts.pop()
    if (not parts or text.startswith("/") or any(
            p in ("", ".", "..") or "\\" in p or any(ord(c) < 32 or ord(c) == 127 for c in p)
            for p in parts)):
        _policy_error(number, "Invalid path prefix (relative, no traversal)")
    return "/".join(parts)


def load_policy(denylist: Path | None, protected: Path | None) -> ProtectionPolicy:
    hosts, tags, hashes, prefixes = set(), set(), set(), set()
    if denylist is not None:
        for number, line in _read_lines(denylist):
            if line.lower().startswith("source:"):
                tag = line[len("source:"):].strip()
                if not tag or len(tag) > 256 or any(ord(c) < 32 for c in tag):
                    _policy_error(number, "Invalid source tag")
                tags.add(tag.casefold())
            else:
                hosts.add(_policy_host(line, number))
    if protected is not None:
        for number, line in _read_lines(protected):
            if line.lower().startswith("path:"):
                prefixes.add(_path_prefix(line[len("path:"):].strip(), number))
            elif _INFOHASH.fullmatch(line):
                hashes.add(line.lower())
            else:
                _policy_error(number, "Expected a 40-hex infohash or a path: prefix")
    digest = hashlib.sha256(json.dumps({
        "tracker_hosts": sorted(hosts), "source_tags": sorted(tags),
        "infohashes": sorted(hashes), "path_prefixes": sorted(prefixes),
        "has_denylist": denylist is not None, "has_protected_list": protected is not None,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ProtectionPolicy(tuple(sorted(hosts)), tuple(sorted(tags)), frozenset(hashes),
                            tuple(sorted(prefixes)), denylist is not None, protected is not None,
                            digest)


def path_protected(policy: ProtectionPolicy, rel_path: str) -> bool:
    """True when a library-relative path equals or sits under a protected ``path:`` prefix."""
    parts = tuple(p for p in rel_path.split("/") if p)
    for prefix in policy.path_prefixes:
        want = tuple(prefix.split("/"))
        if parts[:len(want)] == want:
            return True
    return False


def _host_denied(host: str, policy: ProtectionPolicy) -> bool:
    return any(host == h or host.endswith("." + h) for h in policy.tracker_hosts)


def classify(meta: TorrentMeta, policy: ProtectionPolicy) -> Protection:
    reasons = []
    if meta.private:
        reasons.append("private_flag")
    hosts = [tracker_host(u) for u in meta.announce_urls]
    if any(h and _host_denied(h, policy) for h in hosts):
        reasons.append("tracker_denylist")
    if meta.source is not None and meta.source.strip().casefold() in policy.source_tags:
        reasons.append("source_denylist")
    if meta.infohash in policy.infohashes:
        reasons.append("protected_infohash")
    if not meta.announce_valid or any(h is None for h in hosts):
        reasons.append("announce_unparsable")
    return Protection(bool(reasons), tuple(reasons))


def classify_bytes(data: bytes, policy: ProtectionPolicy) -> tuple[TorrentMeta | None, Protection]:
    try:
        meta = parse(data)
        return meta, classify(meta, policy)
    except Exception:  # fail closed on anything, including a bug
        return None, Protection(True, ("unparsable",))


def _public_tracker(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https", "udp") or tracker_host(url) is None:
        return False
    if "?" in url or "#" in url or "@" in parts.netloc or parts.username or parts.password:
        return False
    # Credentials are sometimes embedded in the path (/<key>/announce).
    return not any(_SECRET_SEGMENT.fullmatch(seg) for seg in parts.path.split("/"))


def magnet(meta: TorrentMeta, protection: Protection) -> str | None:
    """Credential-free magnet link, or None for anything protected or private."""
    if protection.protected or meta.private:
        return None
    link = f"magnet:?xt=urn:btih:{meta.infohash}&dn={quote(meta.name, safe='')}"
    for url in meta.announce_urls:
        if _public_tracker(url):
            link += "&tr=" + quote(url, safe="")
    return link
