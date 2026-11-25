"""AniList alias resolution: public, read-only metadata, no credentials.

Alias spellings are untrusted provider data. They may widen what Nyaa is queried
for, but they never authorize a download, relax identity checks or choose a
release on their own. Failures are structured and never grant extra trust.
"""
from dataclasses import dataclass
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import KuraError
from .provider import NoRedirect
from .ratelimit import SharedLimiter
from .releases import normalize

ENDPOINT = "https://graphql.anilist.co/"
HOST = "graphql.anilist.co"
LIMIT = 512 * 1024
MAX_MEDIA = 10
MAX_TITLE = 200

QUERY = ("query($search:String!){Page(perPage:%d){media(search:$search,type:ANIME){"
         "id title{romaji english native} synonyms}}}" % MAX_MEDIA)


@dataclass(frozen=True)
class Identity:
    anilist_id: int
    titles: tuple[str, ...]
    matched: str

    def json(self):
        return {"anilist_id": self.anilist_id, "matched": self.matched,
                "titles": list(self.titles)}


def validate_endpoint(url: str):
    u = urllib.parse.urlsplit(url)
    if (u.scheme != "https" or u.netloc != HOST or u.path != "/" or
            u.query or u.fragment or any(ord(c) < 33 for c in url)):
        raise KuraError("unsafe_url", "Only the canonical HTTPS AniList endpoint is allowed")


def clean(value):
    """Untrusted title -> single-line bounded text, or None."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > MAX_TITLE or any(ord(c) < 32 for c in text):
        return None
    return text


def parse_response(data: bytes) -> list[dict]:
    if len(data) > LIMIT:
        raise KuraError("alias_invalid", "AniList response exceeds the metadata limit")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as e:
        raise KuraError("alias_invalid", "AniList response must be bounded UTF-8 JSON") from e
    if not isinstance(document, dict):
        raise KuraError("alias_invalid", "AniList response must be a JSON object")
    if document.get("errors"):
        raise KuraError("alias_invalid", "AniList reported a query error")
    payload = document.get("data")
    page = payload.get("Page") if isinstance(payload, dict) else None
    media = page.get("media") if isinstance(page, dict) else None
    if not isinstance(media, list):
        raise KuraError("alias_invalid", "AniList response has no media list")
    rows = []
    for item in media[:MAX_MEDIA]:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if not isinstance(identifier, int) or isinstance(identifier, bool) or not 0 < identifier < 10**9:
            continue
        title = item.get("title") if isinstance(item.get("title"), dict) else {}
        synonyms = item.get("synonyms") if isinstance(item.get("synonyms"), list) else []
        candidates = [clean(title.get(key)) for key in ("romaji", "english", "native")]
        candidates += [clean(value) for value in synonyms[:MAX_MEDIA]]
        titles, seen = [], set()
        for value in candidates:
            if not value:
                continue
            key = normalize(value)
            if not key or key in seen:
                continue
            seen.add(key)
            titles.append(value)
        if titles:
            rows.append({"id": identifier, "titles": titles})
    return rows


def choose(rows: list[dict], query: str):
    """Return (Identity|None, status). Never guesses between equal best matches."""
    wanted = normalize(query)
    tokens = set(wanted.split())
    best = None
    for row in rows:
        scored = []
        for title in row["titles"]:
            candidate = normalize(title)
            candidate_tokens = set(candidate.split())
            if candidate == wanted:
                scored.append((0, 0, title))
            elif tokens and tokens <= candidate_tokens:
                scored.append((1, len(candidate_tokens - tokens), title))
        if not scored:
            continue
        scored.sort(key=lambda item: (item[0], item[1]))
        key, identifier, matched = scored[0][:2], row["id"], scored[0][2]
        if best is None or key < best[0]:
            best = (key, identifier, matched, row["titles"])
        elif key == best[0] and identifier != best[1]:
            return None, "ambiguous"
    if best is None:
        return None, "unresolved"
    ordered = [best[2]] + [t for t in best[3] if normalize(t) != normalize(best[2])]
    return Identity(best[1], tuple(ordered), best[2]), "resolved"


RATE_HEADERS = ("Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")


def _headers(source) -> dict:
    """Only the rate-limit headers, as plain strings; anything else is dropped."""
    out = {}
    for name in RATE_HEADERS:
        try:
            value = source.get(name)
        except (AttributeError, TypeError):
            value = None
        if isinstance(value, str):
            out[name] = value.strip()
    return out


def fetch(title: str) -> tuple[bytes, dict]:
    """One bounded request. Returns (body, rate-limit headers). HTTP 429 carries Retry-After."""
    validate_endpoint(ENDPOINT)
    try:
        addresses = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise KuraError("unsafe_url", "AniList DNS resolved to a non-public address")
        body = json.dumps({"query": QUERY, "variables": {"search": title}}).encode()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request_ = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Kura/0.1 (+https://github.com/mbtiongson1/kura)"})
        with opener.open(request_, timeout=15) as response:
            data = response.read(LIMIT + 1)
            headers = _headers(getattr(response, "headers", None))
        if len(data) > LIMIT:
            raise KuraError("alias_invalid", "AniList response exceeds the metadata limit")
        return data, headers
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise KuraError("alias_unavailable", "AniList rate limit reached; retry later",
                            status=429, **_headers(e.headers)) from e
        raise KuraError("alias_unavailable", "AniList rejected the query; retry later") from e
    except (OSError, urllib.error.URLError) as e:
        raise KuraError("alias_unavailable", "AniList request failed; retry later") from e


def request(title: str) -> bytes:
    return fetch(title)[0]


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and number not in (float("inf"), float("-inf")) else default


class AniList:
    """Alias resolver port. One bounded request per uncached query, paced.

    Defaults assume AniList's degraded limit of 30 requests per minute until its
    ``X-RateLimit-Limit`` header says otherwise (higher or lower, capped at 90). A 429 honours ``Retry-After``
    and ``X-RateLimit-Remaining`` / ``Reset`` are respected, but the total wait for one
    lookup never exceeds ``max_wait`` seconds: past that the resolver raises
    ``alias_unavailable`` immediately (and keeps doing so, without any request, until the
    advertised window passes) so a large library can never hammer the service or stall.

    With a ``limiter`` the pacing, penalties and advertised limits live in the shared
    database, so concurrent Kura processes stay inside one budget; without one (the
    default) pacing is per process and in memory.
    """

    def __init__(self, *, per_minute: int = 30, max_wait: float = 30.0, max_retries: int = 2,
                 transport=None, sleep=time.sleep, clock=time.monotonic, wall=time.time,
                 limiter: SharedLimiter | None = None):
        if per_minute < 1 or max_wait < 0 or max_retries < 0:
            raise KuraError("invalid_argument", "Invalid AniList pacing parameters")
        self.per_minute, self.max_wait, self.max_retries = per_minute, max_wait, max_retries
        self.transport, self.sleep, self.clock, self.wall = transport or fetch, sleep, clock, wall
        self.limiter = limiter
        self.interval = 60.0 / per_minute
        self.next_ok = 0.0      # earliest monotonic time for the next request
        self.last_start = None  # when the previous request was released

    def _pace(self, budget: float) -> float:
        """Sleep until the next request is allowed; refuse if that exceeds `budget` seconds."""
        if self.limiter is not None:
            try:
                return self.limiter.acquire(max(budget, 0.0))
            except KuraError as e:
                if e.code == "rate_limited":
                    raise KuraError("alias_unavailable", "AniList rate limit window is still open",
                                    status=429, rate_limited=True) from e
                raise KuraError("alias_unavailable", "AniList rate-limit state is unavailable; retry later") from e
        wait = max(self.next_ok - self.clock(), 0.0)
        if wait > budget:
            raise KuraError("alias_unavailable", "AniList rate limit window is still open",
                            status=429, rate_limited=True)
        if wait:
            self.sleep(wait)
        self.last_start = self.clock()
        self.next_ok = self.last_start + self.interval
        return wait

    def _shared(self, call, *args, **kwargs):
        """A limiter write that must not fail silently: no shared state, no further requests."""
        try:
            return call(*args, **kwargs)
        except KuraError as e:
            raise KuraError("alias_unavailable", "AniList rate-limit state is unavailable; retry later") from e

    def _penalize(self, headers: dict):
        delay = _number(headers.get("Retry-After"))
        if delay is None:
            reset = _number(headers.get("X-RateLimit-Reset"))
            delay = reset - self.wall() if reset is not None else 60.0
        delay = min(max(delay, 1.0), 3600.0)
        if self.limiter is not None:
            self._shared(self.limiter.penalize, self.wall() + delay)
            return
        self.next_ok = max(self.next_ok, self.clock() + delay)

    def _observe(self, headers: dict):
        limit = _number(headers.get("X-RateLimit-Limit"))
        if self.limiter is not None:
            remaining = _number(headers.get("X-RateLimit-Remaining"))
            reset = _number(headers.get("X-RateLimit-Reset"))
            usable = limit if limit and limit > 0 else None
            if usable is not None or remaining is not None or reset is not None:
                self._shared(self.limiter.observe, limit=usable, remaining=remaining, reset=reset,
                             interval=60.0 / min(usable, 90) if usable is not None else None)
            if remaining is not None and remaining <= 1:
                delay = reset - self.wall() if reset is not None else 60.0
                self._shared(self.limiter.penalize, self.wall() + min(max(delay, 0.0), 3600.0))
            return
        if limit and limit > 0:
            self.interval = 60.0 / min(limit, 90)   # an advertised limit binds in both directions
            if self.last_start is not None:
                self.next_ok = max(self.next_ok, self.last_start + self.interval)
        remaining = _number(headers.get("X-RateLimit-Remaining"))
        if remaining is not None and remaining <= 1:
            reset = _number(headers.get("X-RateLimit-Reset"))
            delay = reset - self.wall() if reset is not None else 60.0
            self.next_ok = max(self.next_ok, self.clock() + min(max(delay, 0.0), 3600.0))

    def _send(self, title: str):
        try:
            return self.transport(title)
        finally:
            if self.limiter is not None:
                try:
                    self.limiter.done()     # spacing counts from the end of the request too
                except KuraError:
                    pass                    # acquire already reserved a slot; never mask the outcome

    def resolve(self, title: str):
        if not normalize(title) or len(title) > MAX_TITLE or any(ord(c) < 32 for c in title):
            raise KuraError("invalid_query", "Title must be 1..200 printable characters")
        throttled, spent = 0, 0.0
        while True:
            spent += self._pace(self.max_wait - spent)   # max_wait bounds the whole lookup
            try:
                data, headers = self._send(title)
            except KuraError as e:
                if e.details.get("status") != 429:
                    raise
                self._penalize({k: v for k, v in e.details.items() if isinstance(v, str)})
                throttled += 1
                if throttled > self.max_retries:
                    raise
                continue
            self._observe(headers)
            return choose(parse_response(data), title)
