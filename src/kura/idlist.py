"""AniList ID list files: the retain list and the policy overrides file.

A policy file is plain text, like ``protected.txt``: one positive AniList ID below 10^9 per
non-comment line, ``#`` starts a comment, blank lines are ignored. The file is the only
copy of the owner's policy (it never enters the state database); plans and reports record
just its sha256 and the number of IDs. Anything unexpected is refused with
``invalid_policy`` and the line number, so a typo can never silently shrink a retain list.

  retain list:        ``999000123   # free-text comment``
  with tags (``tags=("archival", "efficient")``): ``999000123 archival  # comment``

Duplicates merge; a duplicate with a different tag is refused. Reading is bounded (1 MiB,
10,000 lines) and uses a no-follow, regular-file open.

A pins file (``parse_pins``) holds the owner's title identity decisions, one per line:
``<AniList ID or not-anime><TAB><title key>``. The key is everything after the first tab, taken
verbatim up to the newline (spaces, tabs, trailing whitespace and ``#`` included; a trailing CR
is dropped), so only whole-line comments are allowed. A title listed twice is refused, even
with the same value.

A pins line may also name one part of a multi-part title: ``<ID>@<selector><TAB><title key>`` (selectors
in ``titleparts.canonical_selector``: ``all``, ``auto``, ``rest``, ``season:N``, ``ep:A-B``,
``dir:PATH``, ``file:PATH``). A title may then repeat, but only with selector lines: a plain line plus a
selector line, a repeated selector (``auto`` repeats with different IDs: those IDs are the parts), ``all`` beside other selectors, a second ``rest`` and
``not-anime@...`` are all refused. A file without selector lines keeps the digest it always had.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .models import KuraError
from .titleparts import canonical_selector

MAX_LINES = 10_000
MAX_BYTES = 1024**2
MAX_ID = 10**9
_ID = re.compile(r"[1-9][0-9]{0,8}", re.ASCII)
_COMMENT = re.compile(r"(?:^|\s)#")


@dataclass(frozen=True)
class IdList:
    ids: dict[int, str | None]
    sha256: str

    def __contains__(self, anilist_id) -> bool:
        return anilist_id in self.ids

    def summary(self) -> dict:
        """What a plan or report may record about the list: its digest and size."""
        return {"sha256": self.sha256, "ids": len(self.ids)}


def _error(line: int | None, message: str):
    details = {"line": line} if line is not None else {}
    raise KuraError("invalid_policy", message, **details)


def _read(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)   # a FIFO must not hang
    except OSError as e:
        raise KuraError("invalid_policy", "ID list file is unreadable") from e
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = None
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise KuraError("invalid_policy", "ID list file must be a regular file")
            return handle.read(MAX_BYTES + 1)
    except OSError as e:
        raise KuraError("invalid_policy", "ID list file is unreadable") from e
    finally:
        if fd is not None:
            os.close(fd)


def parse(path: Path, *, tags: tuple[str, ...] = ()) -> IdList:
    """Parse an ID list file. With ``tags`` every line needs exactly one of them after the ID."""
    raw = _read(Path(path))
    if len(raw) > MAX_BYTES:
        _error(None, "ID list file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _error(None, "ID list file is not UTF-8")
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        _error(None, f"ID list file exceeds {MAX_LINES} lines")
    ids: dict[int, str | None] = {}
    for number, line in enumerate(lines, 1):
        line = _COMMENT.split(line, maxsplit=1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if not _ID.fullmatch(parts[0]):
            _error(number, "Expected a positive AniList ID below 1000000000")
        anilist_id = int(parts[0])
        if tags:
            if len(parts) != 2 or parts[1] not in tags:
                _error(number, "Expected an ID followed by one of: " + ", ".join(tags))
            tag = parts[1]
        else:
            if len(parts) != 1:
                _error(number, "Expected one AniList ID per line")
            tag = None
        if anilist_id in ids and ids[anilist_id] != tag:
            _error(number, "Conflicting entries for the same ID")
        ids[anilist_id] = tag
    body = sorted(ids.items()) if tags else sorted(ids)
    digest = hashlib.sha256(json.dumps(body, separators=(",", ":")).encode("ascii")).hexdigest()
    return IdList(ids, digest)


NOT_ANIME = "not-anime"


@dataclass(frozen=True)
class Pins:
    entries: dict[str, int | None]      # title key -> AniList ID, or None for not-anime
    sha256: str
    parts: dict[str, list[tuple[int, str]]] = field(default_factory=dict)   # title key -> [(ID, selector)] in line order

    def summary(self) -> dict:
        return {"sha256": self.sha256, "pins": len(self.entries) + len(self.parts)}


def parse_pins(path: Path) -> Pins:
    """Parse a pins file (see the module docstring). Any bad line refuses the whole file."""
    raw = _read(Path(path))
    if len(raw) > MAX_BYTES:
        _error(None, "Pins file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _error(None, "Pins file is not UTF-8")
    lines = text.split("\n")            # only \n ends a line: a key may hold any other character
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_LINES:
        _error(None, f"Pins file exceeds {MAX_LINES} lines")
    entries: dict[str, int | None] = {}
    parts: dict[str, list[tuple[int, str]]] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line = line.removesuffix("\r")    # CRLF files work; a key ending in CR cannot be written
        value, sep, title = line.partition("\t")
        value = value.strip()
        if not sep or not title:
            _error(number, "Expected <AniList ID or not-anime>, a tab, then the title key")
        value, at, selector = value.partition("@")
        if value == NOT_ANIME:
            if at:
                _error(number, "A not-anime line has no selector")
            target = None
        elif _ID.fullmatch(value):
            target = int(value)
        else:
            _error(number, "Expected a positive AniList ID below 1000000000 or not-anime")
        if at:
            try:
                selector = canonical_selector(selector)
            except ValueError as e:
                _error(number, str(e))
            if title in entries:
                _error(number, "A title with a plain line cannot also have selector lines")
            listed = parts.setdefault(title, [])
            if any(sel == selector and (selector != "auto" or i == target) for i, sel in listed):
                _error(number, "The same title and selector are listed twice")
            if "all" in (selector, *(sel for _, sel in listed)) and listed:
                _error(number, "The selector all cannot be combined with other selectors")
            listed.append((target, selector))
            continue
        if title in entries or title in parts:
            _error(number, "The same title is listed twice")
        entries[title] = target
    if not parts:
        body = sorted(entries.items(), key=lambda kv: kv[0])
    else:
        body = sorted([*([t, v] for t, v in entries.items()), *([t, [[i, s] for i, s in listed]]
                                                                 for t, listed in parts.items())])
    digest = hashlib.sha256(json.dumps(body, separators=(",", ":"), ensure_ascii=False)
                            .encode("utf-8")).hexdigest()
    return Pins(entries, digest, parts)
