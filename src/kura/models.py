"""Typed domain values; no I/O."""
from dataclasses import asdict, dataclass
from typing import Protocol


class KuraError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


@dataclass(frozen=True)
class Preferences:
    resolution: str = "1080p"
    groups: tuple[str, ...] = ()
    subtitles: str = "english"
    max_size_mb: int = 1500
    min_seeders: int = 1
    destination: str = "private"


@dataclass(frozen=True)
class Release:
    id: str
    title: str
    series: str
    episode: int
    group: str
    resolution: str
    subtitles: str
    size_bytes: int
    seeders: int
    infohash: str
    url: str
    trusted: bool

    def json(self):
        return asdict(self)


class Provider(Protocol):
    def search(self, title: str, episode: int, subtitles: str) -> list[Release]: ...
    def descriptor(self, release: Release) -> bytes: ...
