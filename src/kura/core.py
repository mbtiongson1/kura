"""Headless acquisition service, shared by the CLI and future bounded tool adapters."""
from dataclasses import asdict, replace
import hashlib
from pathlib import Path

from . import downloader
from .models import KuraError, Preferences, Provider
from .releases import eligible, normalize, query_check, select
from .safety import destination, ensure_dir, no_symlinks, regular_file
from .store import Store, validate_prefs
from .torrent import inspect


class Core:
    def __init__(self, store: Store, provider: Provider):
        self.store, self.provider = store, provider

    def preferences(self, overrides: dict) -> Preferences:
        return validate_prefs(replace(self.store.prefs(), **overrides))

    def search(self, title: str, episode: int, overrides: dict):
        query_check(title, episode)
        p = self.preferences(overrides)
        rows = eligible(self.provider.search(title, episode, p.subtitles), title, episode, p)
        return p, rows

    def status(self):
        try:
            with self.store.lock():
                self.store.recover()
        except KuraError as e:
            if e.code != "busy":
                raise
        return {"downloads": self.store.jobs()}

    def download(self, title: str, episode: int, overrides: dict, *,
                 dry_run=False, yes=False, resume=False, release_id=None, confirm=None):
        # Serialize mutations across processes, including the entire foreground transfer.
        with self.store.lock() as lock_fd:
            self.store.recover()
            p, rows = self.search(title, episode, overrides)
            release = select(rows, p, release_id)
            torrent = inspect(self.provider.descriptor(release), release.infohash, p.max_size_mb * 1024**2)
            # A descriptor cannot swap identity while retaining the RSS promise.
            from .releases import parse_title
            parsed = parse_title(torrent.name)
            if (not parsed or normalize(parsed[0]) != normalize(title) or parsed[1] != episode or
                    (p.resolution != "any" and parsed[3] != p.resolution) or
                    (p.groups and parsed[2].casefold() != release.group.casefold())):
                raise KuraError("descriptor_identity", "Torrent filename does not match requested title/episode/resolution")
            base = destination(p.destination)
            directory = base / torrent.infohash
            no_symlinks(directory)
            media_key = normalize(title) + ":" + str(episode)
            existing = next((j for j in self.store.jobs() if
                             j["media_key"] == media_key or j["infohash"] == torrent.infohash), None)
            plan = {"source": "nyaa", "query": title, "episode": episode,
                    "release": release.json(), "preferences": asdict(p),
                    "destination": str(directory), "expected_size_bytes": torrent.length,
                    "existing": existing, "uri_type": "validated_torrent"}
            if dry_run:
                return {"state": "planned", **plan}
            if existing:
                if existing["state"] == "complete":
                    raise KuraError("duplicate", "This episode already has a verified receipt", existing=existing)
                if (not resume or existing["infohash"] != torrent.infohash or
                        existing["destination"] != str(directory)):
                    raise KuraError("duplicate", "Existing partial/failed episode; use --resume with the same release and destination",
                                    existing=existing)
            if not yes and not (confirm and confirm(plan)):
                raise KuraError("confirmation_required", "Review --dry-run, then explicitly confirm or use --yes")
            ensure_dir(directory)
            # Only the selected torrent payload + its aria2 control file may inhabit this job.
            allowed = {torrent.name, torrent.name + ".aria2"}
            for entry in directory.iterdir():
                if entry.name not in allowed or not resume:
                    raise KuraError("destination_not_empty", "Destination contains unexpected/existing data; refusing overwrite")
                regular_file(entry)
            job = {**plan, "infohash": torrent.infohash, "media_key": media_key,
                   "state": "acquiring", "authorization": "explicit_yes" if yes else "interactive",
                   "existing": None}
            self.store.record(job)
            try:
                evidence = downloader.run(torrent, directory, self.store.root, lock_fd=lock_fd)
                job.update(state="complete", evidence=evidence, error=None)
                self.store.record(job)
                return job
            except BaseException as e:
                job.update(state="interrupted" if isinstance(e, (KeyboardInterrupt, SystemExit)) else "failed",
                           error=e.code if isinstance(e, KuraError) else type(e).__name__)
                self.store.record(job)
                raise
