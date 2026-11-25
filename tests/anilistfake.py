"""Synthetic AniList for tests: a recording GraphQL transport with a tiny scripted world.

Everything here is invented: IDs start at 999000001, the fake client ID is 999001, and tokens are
built at test time (JWT-shaped, base64url, with an `exp`), never stored as literals.
"""
import base64
import json
import re
import secrets

from kura.models import KuraError

BASE_ID = 999000001
CLIENT_ID = 999001
VIEWER = {"id": 999000101, "name": "KuraTester"}


def make_token(exp=None):
    """A JWT-shaped fake token. With `exp` the payload carries it; without, it is opaque."""
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    if exp is None:
        return secrets.token_urlsafe(48)
    return ".".join((part({"alg": "none"}), part({"exp": int(exp), "jti": secrets.token_hex(6)}),
                     secrets.token_urlsafe(24)))


def media(identifier):
    return {"id": identifier, "title": {"romaji": f"Kura Sample Series {identifier - BASE_ID}",
                                        "english": None, "native": None},
            "synonyms": [], "format": "TV", "episodes": 12, "duration": 24, "status": "FINISHED",
            "season": "FALL", "seasonYear": 2001, "averageScore": 80, "popularity": 100,
            "favourites": 5, "isAdult": False, "nextAiringEpisode": None}


def entry(identifier, status="COMPLETED", score=0.0, progress=12, repeat=0):
    return {"mediaId": identifier, "status": status, "score": score, "progress": progress,
            "repeat": repeat, "updatedAt": 1_700_000_000 + identifier - BASE_ID, "media": media(identifier)}


def http_error(status, reason=None, **headers):
    return KuraError("anilist_http", "AniList rejected the request", status=status, reason=reason, **headers)


class FakeAniList:
    """Callable with the transport signature ``(query, variables, token, limit) -> (body, headers)``.

    `calls` records every request (query text, variables and the token it carried). `queue` holds
    one-shot overrides: a KuraError is raised, a (dict, headers) pair is returned as the body.
    """

    def __init__(self, *, per_chunk=500, headers=None):
        self.calls, self.queue = [], []
        self.per_chunk = per_chunk
        self.viewer = dict(VIEWER)
        self.entries: list[dict] = []           # list entries served in status lists
        self.custom_only: list[dict] = []       # entries visible only through a custom list
        self.favourites: list[int] = []
        self.media_overrides: dict[int, dict] = {}
        self.missing_media: set[int] = set()
        self.bad_request_over = None            # media batches larger than this get HTTP 400
        self.headers = headers or {"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "89"}

    def __call__(self, query, variables, token=None, limit=None):
        self.calls.append({"query": query, "variables": dict(variables), "token": token})
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, KuraError):
                raise item
            body, headers = item
            return json.dumps(body).encode(), headers
        return json.dumps({"data": self.respond(query, variables)}).encode(), dict(self.headers)

    def count(self, needle):
        return sum(needle in c["query"] for c in self.calls)

    def respond(self, query, variables):
        if "Viewer" in query:
            return {"Viewer": {**self.viewer, "mediaListOptions": {"scoreFormat": "POINT_10_DECIMAL"}}}
        if "MediaListCollection" in query:
            chunk = variables["chunk"]
            window = self.entries[(chunk - 1) * self.per_chunk: chunk * self.per_chunk]
            by_status: dict[str, list] = {}
            for item in window:
                by_status.setdefault(item["status"], []).append(item)
            lists = [{"isCustomList": False, "entries": rows} for rows in by_status.values()]
            if chunk == 1:
                lists.append({"isCustomList": True, "entries": self.entries[:2] + self.custom_only})
            return {"MediaListCollection": {
                "hasNextChunk": chunk * self.per_chunk < len(self.entries), "user": dict(self.viewer),
                "lists": lists}}
        if "User(id" in query:
            page = variables["page"]
            ids = self.favourites[(page - 1) * 25: page * 25]
            return {"User": {"favourites": {"anime": {
                "pageInfo": {"hasNextPage": page * 25 < len(self.favourites)},
                "nodes": [{"id": i} for i in ids]}}}}
        if "id_in" in query:
            ids = variables["ids"]
            if self.bad_request_over is not None and len(ids) > self.bad_request_over:
                raise http_error(400, "Query too complex")
            return {"Page": {"media": [self.media_overrides.get(i, media(i)) for i in ids
                                       if i not in self.missing_media]}}
        if "GenreCollection" in query:
            return {"GenreCollection": ["Drama"]}
        raise AssertionError("unexpected query")


def tokens_in(fake):
    return [c["token"] for c in fake.calls]


def contains(text, secret):
    return re.search(re.escape(secret), text) is not None
