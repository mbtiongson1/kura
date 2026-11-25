"""Tolerant release-name tokenizer, for scoring only.

``parse(name)`` pulls the usual scene and fansub tags out of a file name: release group,
episode, version, season, resolution, codec, bit depth, source, CRC and an extras flag. Every field
that cannot be read is ``None``. It is deliberately forgiving and therefore sometimes wrong:
its output feeds audit statistics and flags, and must never decide which torrent or title a
file is (acquisition identity stays exact, see ``match`` and ``manifest``). ``series`` is the one
other consumer: ``identity`` reads it as a second opinion on a folder name, and only when every
video file of the title agrees.

Pure: no I/O, no state, no network.
"""
import re

from .sources.seadex import is_extra

_EXT = re.compile(r"\.(?:mkv|mp4|m4v|avi|webm|ts|m2ts|mov|wmv|flv|ass|ssa|srt|sub|idx|sup)$", re.I)
_LEAD = re.compile(r"^\s*\[([^\[\]]{1,80})\]")
_TRAIL = re.compile(r"-([A-Za-z][A-Za-z0-9]{1,30})$")
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_RES = re.compile(r"(?<![0-9a-z])(?:(\d{3,4})[pi]|(\d{3,4})x(\d{3,4})|(4k))(?![0-9a-z])", re.I)
_CRC = re.compile(r"\[([0-9A-Fa-f]{8})\]")
_SEASON = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})E\d{1,4}(?![0-9])", re.I)
_TAGLIKE = re.compile(
    r"(?:\d{3,4}[pi]|\d{3,4}x\d{3,4}|4k|x26[45]|h\.?26[45]|hevc|avc|av1|vp9|flac|aac\d*|opus|ac-?3|dts|"
    r"hi10p?|10-?bits?|8-?bits?|bd|bdrip|blu-?ray|remux|web|webrip|web-?dl|dl|rip|tv|hdtv|dvd|"
    r"multi-?subs?|ddp?\d?|truehd|atmos|h|ray|audio|subs?|batch|[0-9a-f]{8})", re.I)
_VER = r"(?:v(\d{1,2}))?"
DASH_EPISODE = re.compile(r"\s-\s(\d{1,3})" + _VER + r"(?![0-9A-Za-z]|-[0-9])")    # "Show - 05"
_EPISODES = (
    re.compile(r"(?<![A-Za-z0-9])S\d{1,2}E(\d{1,4})" + _VER + r"(?![A-Za-z0-9])", re.I),
    re.compile(r"(?<![A-Za-z0-9])EP?\.?\s?(\d{1,4})" + _VER + r"(?![A-Za-z0-9])", re.I),
    DASH_EPISODE,
    re.compile(r"(?<![A-Za-z0-9.#])#?(\d{2,3})v(\d{1,2})(?![A-Za-z0-9])", re.I),
    re.compile(r"#(\d{1,4})" + _VER + r"(?![A-Za-z0-9])"),
)
_NOISE = re.compile(r"(?<![0-9a-z])(?:\d{3,4}[pi]|\d{3,4}x\d{3,4}|x26[45]|h\.?26[45]|10-?bits?|8-?bits?|hi10p?)"
                    r"(?![0-9a-z])", re.I)
_CODECS = (("hevc", re.compile(r"(?<![0-9a-z])(?:x265|h\.?265|hevc)(?![0-9a-z])", re.I)),
           ("avc", re.compile(r"(?<![0-9a-z])(?:x264|h\.?264|avc)(?![0-9a-z])", re.I)),
           ("av1", re.compile(r"(?<![0-9a-z])av1(?![0-9a-z])", re.I)),
           ("vp9", re.compile(r"(?<![0-9a-z])vp9(?![0-9a-z])", re.I)))
_DEPTH = ((10, re.compile(r"(?<![0-9a-z])(?:hi10p?|10[ ._-]?bits?)(?![0-9a-z])", re.I)),
          (8, re.compile(r"(?<![0-9a-z])8[ ._-]?bits?(?![0-9a-z])", re.I)))
_SOURCES = (
    ("Remux", re.compile(r"(?<![0-9a-z])(?:(?:bd|blu-?ray)[ ._-]?)?remux(?![0-9a-z])", re.I)),
    ("WEB-DL", re.compile(r"(?<![0-9a-z])web[ ._-]?dl(?![0-9a-z])", re.I)),
    ("WEBRip", re.compile(r"(?<![0-9a-z])web[ ._-]?rip(?![0-9a-z])", re.I)),
    ("BDRip", re.compile(r"(?<![0-9a-z])(?:bd|br|blu-?ray)[ ._-]?rip(?![0-9a-z])", re.I)),
    ("BD", re.compile(r"(?<![0-9a-z])(?:bd|blu-?ray|bdmv)(?![0-9a-z])", re.I)),
    ("WEB", re.compile(r"(?<![0-9a-z])web(?![0-9a-z])", re.I)),
    ("TV", re.compile(r"(?<![0-9a-z])(?:hdtv|tv[ ._-]?rip)(?![0-9a-z])|[\[(]tv[\])]", re.I)),
    ("DVD", re.compile(r"(?<![0-9a-z])dvd(?:[ ._-]?rip)?(?![0-9a-z])", re.I)),
)
FIELDS = ("group", "episode", "version", "season", "resolution", "codec", "bit_depth", "source", "crc", "extra")


def _group(stem: str):
    lead = _LEAD.match(stem)
    if lead and not _TAGLIKE.fullmatch(lead.group(1).strip()):
        return lead.group(1).strip()
    trail = _TRAIL.search(stem)
    if trail and not _TAGLIKE.fullmatch(trail.group(1)):
        # Scene style "...x264-Group": only when a release tag precedes the hyphen, so hyphenated
        # words ("Anti-Hero", "Blu-ray") are never read as groups.
        tokens = [t for t in re.split(r"[\s._\[\]()-]+", stem[:trail.start()]) if t]
        # The word before the hyphen must be a release tag; a number ("H.264", "DDP5.1") looks one back.
        i = len(tokens)
        while i and tokens[i - 1].isdigit() and len(tokens) - i < 3:
            i -= 1
        if i and _TAGLIKE.fullmatch(tokens[i - 1]):
            return trail.group(1)
    return None


def _resolution(stem: str):
    m = _RES.search(stem)
    if not m:
        return None
    if m.group(4):
        return "2160p"
    if m.group(1):
        return m.group(1) + ("i" if m.group(0)[-1].lower() == "i" else "p")
    return m.group(3) + "p"


def _episode(stem: str):
    text = _NOISE.sub(" ", _BRACKETS.sub(" ", stem))
    for pattern in _EPISODES:
        m = pattern.search(text)
        if m:
            version = int(m.group(2)) if m.group(2) else None
            return int(m.group(1)), version
    return None, None


def series(name: str) -> str | None:
    """The text before the episode marker of a release name (path allowed), or None when no
    episode marker is found. Same episode patterns as ``parse``; season tokens and the scene
    style dots stay in the text for the caller (``resolve._name`` reduces it to a title)."""
    stem = _EXT.sub("", name.replace("\\", "/").rsplit("/", 1)[-1])
    text = _NOISE.sub(" ", _BRACKETS.sub(" ", stem))
    for pattern in _EPISODES:
        m = pattern.search(text)
        if m:
            head = text[:m.start()].strip(" \t-")
            return head or None
    return None


def parse(name: str) -> dict:
    """Tokens of one file name or path (the last component is the name); unknown fields are None."""
    path = name.replace("\\", "/")
    base = path.rsplit("/", 1)[-1]
    stem = _EXT.sub("", base)
    episode, version = _episode(stem)
    crc = _CRC.findall(stem)
    codec = next((label for label, pattern in _CODECS if pattern.search(stem)), None)
    depth = next((bits for bits, pattern in _DEPTH if pattern.search(stem)), None)
    source = next((label for label, pattern in _SOURCES if pattern.search(stem)), None)
    season = _SEASON.search(stem)
    return {"group": _group(stem), "episode": episode, "version": version,
            "season": int(season.group(1)) if season else None,
            "resolution": _resolution(stem), "codec": codec, "bit_depth": depth, "source": source,
            "crc": crc[-1].upper() if crc else None, "extra": is_extra(path)}
