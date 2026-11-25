"""Swarm health by HTTP(S) tracker scrape (BEP 48). Network only; never touches the library.

``scrape`` reads the manifest in Kura's private state database, asks the *public* trackers
of the re-acquirable titles' torrents how many seeders they report, and appends the answers
with ``ManifestRepo.add_swarm_check`` for ``quarantine.plan`` to read. Rules:

  * Only public, unprotected torrents are ever scraped. A private or protected torrent gets
    no request and no row. Trackers come from the stored magnet link, which already carries
    only credential-free ``/announce`` and ``/announce.php`` URLs; each is re-validated here
    and a scrape URL is derived by BEP 48 (``announce`` -> ``scrape`` in the last path
    component). UDP trackers (BEP 15) are a documented follow-up and are skipped.
  * At most ``MAX_TRACKERS`` trackers per torrent and ``BATCH`` infohashes per request.
    The first tracker that lists a torrent decides its numbers.
  * Transport hardening: canonical http/https scheme, no redirects, no proxy inheritance,
    DNS answers must all be globally routable and the connection is pinned to a validated
    address (no second resolution), a total deadline and a bounded read. Responses are
    parsed by a bounded bencode reader.
  * Failures never fail the run: the torrent is recorded as ``unavailable`` with a fixed
    reason token. Full announce URLs are never logged, stored in a row or returned, only
    tracker hosts.
"""
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import parse_qsl, quote_from_bytes, urlsplit

from .manifest import show
from .manifest_store import ManifestRepo
from .models import KuraError
from .protect import _public_tracker

TIMEOUT = 5.0                # seconds, also the total deadline of one request
LIMIT = 256 * 1024           # response cap
MAX_TRACKERS = 3             # per torrent
BATCH = 74                   # infohashes per request (BEP 48 URLs stay well under 8 KiB)
MAX_REQUESTS = 100           # per run
HOST_INTERVAL = 1.0          # minimum seconds between two requests to one host
MAX_COUNT = 2**31 - 1
MAX_RESULTS_SHOWN = 500
USER_AGENT = "Kura/0.1 (+https://github.com/mbtiongson1/kura)"


# ---- scrape URL derivation ---------------------------------------------------

def scrape_target(announce: str):
    """``(scheme, host, port, path)`` of the BEP 48 scrape endpoint, or None.

    None unless the URL is a credential-free http(s) announce URL on a conventional path
    (same rule as ``protect.magnet``) whose last path component starts with ``announce``.
    """
    if not isinstance(announce, str) or not _public_tracker(announce):
        return None
    parts = urlsplit(announce)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    head, _, last = parts.path.rpartition("/")
    if not last.startswith("announce"):
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or "%" in host:
        return None
    return scheme, host, port, f"{head}/scrape{last[len('announce'):]}"


def targets_from_magnet(magnet) -> list[tuple]:
    """Up to MAX_TRACKERS distinct scrape targets, in magnet order."""
    if not isinstance(magnet, str):
        return []
    try:
        pairs = parse_qsl(urlsplit(magnet).query, keep_blank_values=True)
    except ValueError:
        return []
    found = []
    for key, value in pairs:
        target = scrape_target(value) if key == "tr" else None
        if target and target not in found:
            found.append(target)
            if len(found) == MAX_TRACKERS:
                break
    return found


def request_target(path: str, infohashes: list[bytes]) -> str:
    """Request target with one percent-encoded ``info_hash`` per raw 20-byte hash."""
    return path + "?" + "&".join("info_hash=" + quote_from_bytes(h, safe="") for h in infohashes)


# ---- bounded bencode ---------------------------------------------------------

def bdecode(data: bytes, *, max_depth: int = 6, max_nodes: int = 20_000):
    """Lenient bounded bencode reader for tracker responses.

    Trackers often emit dictionaries in non-canonical key order, so unlike
    ``torrent.decode`` this accepts any order. Everything else is strict; any problem
    raises ``KuraError("scrape_invalid")``.
    """
    pos, nodes = 0, 0

    def number(end_byte: bytes, limit: int) -> int:
        nonlocal pos
        end = data.index(end_byte, pos)
        raw = data[pos:end]
        if (len(raw) > limit or not raw.lstrip(b"-").isdigit() or raw.count(b"-") > 1
                or raw.startswith(b"-0") or (raw.startswith(b"0") and raw != b"0")):
            raise ValueError("bad number")
        pos = end + 1
        return int(raw)

    def parse(depth: int):
        nonlocal pos, nodes
        nodes += 1
        if depth > max_depth or nodes > max_nodes or pos >= len(data):
            raise ValueError("limit")
        first = data[pos:pos + 1]
        if first == b"i":
            pos += 1
            return number(b"e", 19)
        if first in (b"d", b"l"):
            pos += 1
            out = {} if first == b"d" else []
            while data[pos:pos + 1] != b"e":
                if first == b"d":
                    key = parse(depth + 1)
                    if not isinstance(key, bytes) or key in out:
                        raise ValueError("bad key")
                    out[key] = parse(depth + 1)
                else:
                    out.append(parse(depth + 1))
            pos += 1
            return out
        if first.isdigit():
            size = number(b":", 8)
            if pos + size > len(data):
                raise ValueError("truncated")
            value = data[pos:pos + size]
            pos += size
            return value
        raise ValueError("bad token")

    try:
        value = parse(0)
        if pos != len(data):
            raise ValueError("trailing data")
        return value
    except (ValueError, IndexError, RecursionError) as e:
        raise KuraError("scrape_invalid", "Tracker response is not valid bencode") from e


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def parse_scrape(data: bytes, wanted: list[bytes]) -> dict[bytes, tuple[int, int]]:
    """``{raw infohash: (seeders, leechers)}`` for the requested hashes the tracker listed.

    A tracker ``failure reason`` is an error. Entries for hashes nobody asked about and
    entries without sane non-negative ``complete``/``incomplete`` counters are ignored.
    """
    if len(data) > LIMIT:
        raise KuraError("scrape_invalid", "Tracker response exceeds the size limit")
    document = bdecode(data)
    if not isinstance(document, dict):
        raise KuraError("scrape_invalid", "Tracker response is not a dictionary")
    if b"failure reason" in document:
        raise KuraError("scrape_failed", "Tracker reported a failure")
    files = document.get(b"files")
    if not isinstance(files, dict):
        raise KuraError("scrape_invalid", "Tracker response has no files dictionary")
    out = {}
    for infohash in wanted:
        entry = files.get(infohash)
        if not isinstance(entry, dict):
            continue
        seeders, leechers = _count(entry.get(b"complete")), _count(entry.get(b"incomplete"))
        if seeders is not None and leechers is not None:
            out[infohash] = (seeders, leechers)
    return out


# ---- transport -----------------------------------------------------------------

def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int) -> list[str]:
    """All addresses of `host`, or ``unsafe_url`` if any is not globally routable."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise KuraError("scrape_unavailable", "Tracker host did not resolve") from e
    addresses = []
    for info in infos:
        address = info[4][0]
        if not isinstance(address, str) or not _public(address):
            raise KuraError("unsafe_url", "Tracker resolved to a non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise KuraError("scrape_unavailable", "Tracker host did not resolve")
    return addresses


def _pin(timeout: float, addresses: list[str], port: int):
    """Open a socket to the first reachable validated address (no second DNS lookup)."""
    last = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout)
        except OSError as e:
            last = e
    raise KuraError("scrape_unavailable", "Tracker connection failed") from last


class _Plain(http.client.HTTPConnection):
    def __init__(self, host, port, addresses, timeout):
        super().__init__(host, port, timeout=timeout)
        self._addresses = addresses

    def connect(self):
        self.sock = _pin(self.timeout, self._addresses, self.port)


class _Secure(http.client.HTTPSConnection):
    def __init__(self, host, port, addresses, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._addresses, self._ctx = addresses, context

    def connect(self):
        raw = _pin(self.timeout, self._addresses, self.port)
        try:
            self.sock = self._ctx.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch(scheme: str, host: str, port: int, target: str, *, timeout: float = TIMEOUT,
          limit: int = LIMIT) -> bytes:
    """One bounded GET. Returns the body of a 200 response; every failure is a KuraError.

    Never follows a redirect, never reads proxy settings, never resolves a host twice and
    never lets one request run past `timeout` seconds in total.
    """
    if scheme not in ("http", "https") or not 0 < port < 65536:
        raise KuraError("unsafe_url", "Only http and https trackers are scraped")
    addresses = resolve_public(host, port)
    conn = (_Secure(host, port, addresses, timeout, ssl.create_default_context())
            if scheme == "https" else _Plain(host, port, addresses, timeout))

    def expire():     # unblock a stalled read: the socket timeout only bounds each recv
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        conn.request("GET", target, headers={
            "User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close"})
        response = conn.getresponse()
        if 300 <= response.status < 400:
            raise KuraError("unsafe_redirect", "Tracker redirects are not allowed")
        if response.status != 200:
            raise KuraError("scrape_unavailable", "Tracker answered with an error status",
                            status=response.status)
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            raise KuraError("scrape_invalid", "Tracker response exceeds the size limit")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise KuraError("scrape_invalid", "Tracker response exceeds the size limit")
        return data
    except KuraError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise KuraError("scrape_unavailable", "Tracker request failed") from e
    finally:
        timer.cancel()
        conn.close()


# ---- the run ---------------------------------------------------------------------

def _eligible(row) -> bool:
    """Public and unprotected torrents only; anything uncertain is skipped, never scraped."""
    protection = row.get("protection") if row else None
    return bool(row and row["private"] is False and isinstance(protection, dict)
                and protection.get("protected") is False)


def check_args(library, titles, max_requests):
    if not isinstance(library, str) or not library:
        raise KuraError("invalid_arguments", "A library name is required")
    if titles is not None and (not isinstance(titles, list) or len(titles) > 10_000
                               or not all(isinstance(t, str) for t in titles)):
        raise KuraError("invalid_arguments", "Titles must be a list of title keys")
    if type(max_requests) is not int or not 1 <= max_requests <= 10_000:
        raise KuraError("invalid_arguments", "max_requests must be a whole number from 1 to 10000")


def _candidates(store, repo, library, titles) -> tuple[list[str], dict]:
    """Infohashes of the re-acquirable titles (deduplicated, sorted) and skip counts."""
    manifest = show(store, library)
    wanted = None if titles is None else set(titles)
    hashes, skipped = set(), {"titles_not_reacquirable": 0, "torrents_protected": 0}
    rows = {r["rel_path"]: r for r in repo.titles(library)}
    for item in manifest["titles"]:
        if wanted is not None and item["title"] not in wanted:
            continue
        if item["status"] != "reacquirable":
            skipped["titles_not_reacquirable"] += 1
            continue
        for link in repo.links(rows[item["title"]]["id"]):
            if link["strategy"] != "superseded":
                hashes.add(link["infohash"])
    return sorted(hashes), skipped


def _query(work, outcome, trail, *, max_requests, fetcher, sleep, clock):
    """Fill `outcome` by asking each torrent's trackers in order. Returns the request count."""
    failed: set[tuple] = set()          # targets that already failed in this run
    last_hit: dict[str, float] = {}
    requests = 0
    for round_ in range(MAX_TRACKERS):
        groups: dict[tuple, list[str]] = {}
        for infohash, targets in work.items():
            if infohash not in outcome and round_ < len(targets):
                groups.setdefault(targets[round_], []).append(infohash)
        for target in sorted(groups):
            scheme, host, port, path = target
            for start in range(0, len(groups[target]), BATCH):
                batch = groups[target][start:start + BATCH]
                if requests >= max_requests:
                    for h in batch:
                        outcome[h] = {"reason": "request_budget"}
                    continue
                if target in failed:
                    for h in batch:
                        trail[h].append({"host": host, "error": "tracker_failed"})
                    continue
                wait = last_hit.get(host, -HOST_INTERVAL) + HOST_INTERVAL - clock()
                if wait > 0:
                    sleep(wait)
                last_hit[host] = clock()
                requests += 1
                raws = [bytes.fromhex(h) for h in batch]
                try:
                    found = parse_scrape(
                        fetcher(scheme, host, port, request_target(path, raws)), raws)
                except KuraError as e:
                    failed.add(target)
                    for h in batch:
                        trail[h].append({"host": host, "error": e.code})
                    continue
                for h, raw in zip(batch, raws):
                    if raw in found:
                        outcome[h] = {"seeders": found[raw][0], "leechers": found[raw][1],
                                      "host": host}
                    else:
                        trail[h].append({"host": host, "error": "not_listed"})
    return requests


def scrape(store, *, library: str, titles: list[str] | None = None,
           max_requests: int = MAX_REQUESTS, fetcher=None, now=time.time,
           sleep=time.sleep, clock=time.monotonic) -> dict:
    """Scrape the swarm of every re-acquirable title's public torrents; record the results.

    `fetcher(scheme, host, port, target)` returns response bytes or raises ``KuraError``
    (injectable for offline tests). Nothing here raises for a tracker problem. Rows are
    appended even when the run is interrupted, for every torrent already answered.
    """
    check_args(library, titles, max_requests)
    fetcher = fetcher or fetch     # late-bound so tests can replace the transport
    with store.lock():      # a consistent read of the manifest; released before any network I/O
        repo = ManifestRepo(store.db)
        hashes, skipped = _candidates(store, repo, library, titles)
        work = {}           # infohash -> scrape targets of a public, unprotected torrent
        for infohash in hashes:
            row = repo.torrent(infohash)
            if _eligible(row):
                work[infohash] = targets_from_magnet(row.get("magnet"))
            else:
                skipped["torrents_protected"] += 1   # no request and no row
    outcome = {h: {"reason": "no_tracker"} for h, targets in work.items() if not targets}
    trail: dict[str, list] = {h: [] for h in work}   # infohash -> [{host, error}]
    requests = 0
    finished = False
    try:
        requests = _query(work, outcome, trail, max_requests=max_requests, fetcher=fetcher,
                          sleep=sleep, clock=clock)
        finished = True
    finally:
        if finished:
            for h in work:
                if h not in outcome:
                    broken = any(a["error"] != "not_listed" for a in trail[h])
                    outcome[h] = {"reason": "tracker_failed" if broken else "not_listed"}
        checked_at = now()
        counts = {"ok": 0, "unavailable": 0}
        shown = []
        for infohash in sorted(outcome):
            result = outcome[infohash]
            detail = {"source": "scrape", "trackers": len(work[infohash])}
            if "seeders" in result:
                status, seeders, leechers = "ok", result["seeders"], result["leechers"]
                detail["tracker"] = result["host"]
            else:
                status, seeders, leechers = "unavailable", None, None
                detail.update(reason=result["reason"], attempts=trail[infohash][:MAX_TRACKERS])
            repo.add_swarm_check({"infohash": infohash, "checked_at": checked_at, "status": status,
                                  "seeders": seeders, "leechers": leechers, "detail": detail})
            counts[status] += 1
            if len(shown) < MAX_RESULTS_SHOWN:
                shown.append({"infohash": infohash, "status": status, "seeders": seeders,
                              "leechers": leechers, "tracker": detail.get("tracker"),
                              "reason": detail.get("reason")})
    return {"library": library, "checked_at": checked_at, "requests": requests,
            "counts": counts, "skipped": skipped,
            "results": shown, "results_truncated": max(len(outcome) - len(shown), 0)}
