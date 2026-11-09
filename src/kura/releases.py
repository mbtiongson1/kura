"""Conservative parsing; intentionally decline fuzzy matches and unsupported naming."""
import re
import unicodedata
from .models import KuraError, Preferences, Release


def normalize(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def query_check(title: str, episode: int):
    if not normalize(title) or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise KuraError("invalid_query", "Title must be 1..200 printable characters")
    if not 1 <= episode <= 1899:
        raise KuraError("invalid_query", "Episode must be 1..1899")


def parse_title(title: str):
    if len(title) > 500 or any(ord(c) < 32 for c in title):
        return None
    if re.search(r"\b(batch|complete|all episodes)\b|\d[-~+]\d|\d\s*[~+]\s*\d", title, re.I):
        return None
    group = re.match(r"^\[([^\]]+)\]\s*", title)
    clean = title[group.end():] if group else title
    # Only a well-delimited single episode; no speculative bracket-number fallback.
    patterns = [r"\s+-\s+(\d{1,4})(?:v\d+)?(?=\s|\[|\(|\.|$)",
                r"\s+S(\d{1,2})E(\d{1,4})(?:v\d+)?(?=\s|\[|\(|\.|$)"]
    matches = [(i, m) for i, p in enumerate(patterns) for m in re.finditer(p, clean, re.I)]
    if len(matches) != 1:
        return None
    kind, match = matches[0]
    series = clean[:match.start()].strip()
    ep = int(match.group(1 if kind == 0 else 2))
    if kind == 1:
        series += " S" + str(int(match.group(1))).zfill(2)
    # Metadata must not conceal a batch/range or another episode marker.
    tail = clean[match.end():]
    for tag in re.findall(r"\[([^\]]*)\]|\(([^)]*)\)", clean):
        text = " ".join(tag).strip()
        if (re.search(r"\b\d{1,4}\s*[-~+]\s*\d{1,4}\b", text) or
                re.search(r"\b\d{1,4}\s*(?:[,;&/+_]|and|to|through)\s*\d{1,4}\b|\b\d{1,3}\s+\d{1,3}\b", text, re.I) or
                re.search(r"(?:S\d{1,2}E|E|EP\.?\s*|Episodes?\s*)\d{1,4}", text, re.I) or
                re.fullmatch(r"\d{1,3}", text)):
            return None
    # Remaining tail must be metadata tags or extension, not a second title/episode.
    tail = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", tail)
    tail = re.sub(r"\.(mkv|mp4|webm|txt)$", "", tail.strip(), flags=re.I).strip()
    if tail or not series or not 1 <= ep <= 1899:
        return None
    resolutions = set(re.findall(r"\b(480p|720p|1080p|2160p)\b", title, re.I))
    resolution = next(iter(resolutions)).lower() if len(resolutions) == 1 else "unknown"
    return series, ep, group.group(1) if group else "unknown", resolution


def eligible(releases: list[Release], title: str, episode: int, p: Preferences) -> list[Release]:
    groups = [g.casefold() for g in p.groups]
    rows = [r for r in releases if
            normalize(r.series) == normalize(title) and r.episode == episode and
            (p.resolution == "any" or r.resolution == p.resolution) and
            (not groups or r.group.casefold() in groups) and
            (p.subtitles == "any" or r.subtitles == p.subtitles) and
            0 < r.size_bytes <= p.max_size_mb * 1024**2 and r.seeders >= p.min_seeders]
    def tier(r):
        return groups.index(r.group.casefold()) if groups else 0
    # Dedupe same infohash; never treat seeder count as identity confidence.
    unique = {r.infohash: r for r in rows}
    return sorted(unique.values(), key=lambda r: (tier(r), -r.seeders, r.id))


def select(rows: list[Release], p: Preferences, release_id: str | None) -> Release:
    if release_id:
        rows = [r for r in rows if r.id == release_id]
    else:
        rows = [r for r in rows if r.trusted]
        if rows and p.groups:
            group = rows[0].group.casefold()
            rows = [r for r in rows if r.group.casefold() == group]
    if not rows:
        raise KuraError("no_match", "No release satisfies title, episode and preferences")
    if len(rows) != 1:
        raise KuraError("ambiguous", "Choose an eligible --release-id from search; no download authorized",
                        candidates=[r.json() for r in rows])
    return rows[0]
