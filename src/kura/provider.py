"""Nyaa RSS only. Strict HTTPS origin/path, no redirects or proxy inheritance."""
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .models import KuraError, Release
from .releases import parse_title

NS = "{https://nyaa.si/xmlns/nyaa}"


def validate_url(url: str, descriptor: bool = False):
    u = urllib.parse.urlsplit(url)
    path = r"/download/[1-9]\d*\.torrent" if descriptor else r"/"
    if (u.scheme != "https" or u.netloc != "nyaa.si" or
            not re.fullmatch(path, u.path) or u.fragment or
            (descriptor and u.query) or any(ord(c) < 33 for c in url)):
        raise KuraError("unsafe_url", "Only canonical HTTPS Nyaa URLs are allowed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise KuraError("unsafe_redirect", "Provider redirects are not allowed")


def fetch(url: str, limit: int, descriptor: bool = False) -> bytes:
    validate_url(url, descriptor)
    # Defense in depth; TLS and exact hostname remain required. DNS rebinding by a
    # compromised local resolver is outside the local same-UID threat boundary.
    try:
        addresses = socket.getaddrinfo("nyaa.si", 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise KuraError("unsafe_url", "Provider DNS resolved to a non-public address")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        req = urllib.request.Request(url, headers={"User-Agent": "Kura/0.1 (+https://github.com/mbtiongson1/kura)"})
        with opener.open(req, timeout=15) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise KuraError("metadata_limit", "Provider response exceeds metadata limit")
        return data
    except (OSError, urllib.error.URLError) as e:
        raise KuraError("provider_unavailable", "Nyaa request failed; retry later") from e


def parse_rss(data: bytes) -> list[Release]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as e:
        raise KuraError("invalid_feed", "Provider XML must be UTF-8") from e
    if (len(data) > 2 * 1024**2 or "\x00" in text or
            "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper()):
        raise KuraError("invalid_feed", "Oversized or unsafe XML")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise KuraError("invalid_feed", "Invalid provider XML") from e
    rows = []
    for item in root.findall("./channel/item")[:100]:
        def val(name):
            return item.findtext(name, default="").strip()
        title = val("title")
        parsed = parse_title(title)
        url, ih = val("link"), val(NS + "infoHash").lower()
        size = re.fullmatch(r"(\d+(?:\.\d+)?) (B|KiB|MiB|GiB)", val(NS + "size"))
        category = val(NS + "categoryId")
        try:
            validate_url(url, True)
            if not parsed or not size or not re.fullmatch(r"[0-9a-f]{40}", ih):
                continue
            if category not in ("1_2", "1_4"):
                continue
            seeders = int(val(NS + "seeders"))
            if not 0 <= seeders <= 1000000:
                continue
            count = int(float(size[1]) * {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[size[2]])
            series, ep, group, resolution = parsed
            rows.append(Release(url.split("/")[-1].split(".")[0], title, series, ep, group,
                                resolution, "english" if category == "1_2" else "raw",
                                count, seeders, ih, url, val(NS + "trusted") == "Yes"))
        except (KuraError, ValueError, OverflowError):
            continue
    return rows


class Nyaa:
    def search(self, title: str, episode: int, subtitles: str) -> list[Release]:
        category = {"english": "1_2", "raw": "1_4", "any": "1_0"}[subtitles]
        queries = [f"{title} {episode:02d}"]
        season = re.fullmatch(r"(.+) S(\d{2})", title, re.I)
        if season:
            queries.append(f"{season[1]} S{season[2]}E{episode:02d}")
        rows = {}
        for text in queries:
            query = urllib.parse.urlencode({"page": "rss", "q": text, "c": category, "f": "0"})
            for release in parse_rss(fetch("https://nyaa.si/?" + query, 2 * 1024**2)):
                rows[release.infohash] = release
        return list(rows.values())

    def descriptor(self, release: Release) -> bytes:
        return fetch(release.url, 4 * 1024**2, True)
