"""Headless acquisition service, shared by the CLI and future bounded tool adapters."""
from dataclasses import asdict, replace
from . import downloader
from .models import KuraError, Preferences, Provider, Release
from .releases import eligible, normalize, parse_title, query_check, select
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
            query_check(title, episode)
            p = self.preferences(overrides)
            media_key = normalize(title) + ":" + str(episode)
            jobs = self.store.jobs()
            existing = next((j for j in jobs if j["media_key"] == media_key), None)
            if existing and not dry_run:
                if existing["state"] == "complete":
                    raise KuraError("duplicate", "This episode already has a verified receipt", existing=existing)
                if not resume:
                    raise KuraError("duplicate", "A recorded job exists; explicitly use --resume", existing=existing)
            if resume or (existing and dry_run):
                if not existing:
                    raise KuraError("no_resume", "No recorded job exists for this title and episode")
                release = Release(**existing["release"])
                if release_id and release_id != release.id:
                    raise KuraError("resume_mismatch", "Resume must use the recorded release")
                # No fresh discovery/seed-count gate: this exact job was already selected.
                # Current quality/group/category/size policy and authorization still apply.
                if not eligible([release], title, episode, replace(p, min_seeders=0)):
                    raise KuraError("resume_preferences", "Recorded release conflicts with current preferences")
                descriptor = self.store.descriptor(release.infohash)
                if descriptor is None:
                    raise KuraError("resume_unavailable", "Recorded job has no validated descriptor; no download started")
            else:
                rows = eligible(self.provider.search(title, episode, p.subtitles), title, episode, p)
                release = select(rows, p, release_id)
                descriptor = self.provider.descriptor(release)
            torrent = inspect(descriptor, release.infohash, p.max_size_mb * 1024**2)
            # Approval identity must agree with the payload, independently of preferences.
            parsed = parse_title(torrent.name)
            if (not parsed or normalize(parsed[0]) != normalize(title) or parsed[1] != episode or
                    parsed[2].casefold() != release.group.casefold() or
                    parsed[3] != release.resolution or
                    (p.resolution != "any" and parsed[3] != p.resolution)):
                raise KuraError("descriptor_identity", "Torrent filename disagrees with selected title/episode/group/resolution")
            base = destination(p.destination)
            directory = base / torrent.infohash
            no_symlinks(directory)
            existing = existing or next((j for j in jobs if j["infohash"] == torrent.infohash), None)
            if resume and existing["destination"] != str(directory):
                raise KuraError("resume_mismatch", "Resume must use the recorded destination")
            plan = {"source": "nyaa", "query": title, "episode": episode,
                    "release": release.json(), "preferences": asdict(p),
                    "destination": str(directory), "expected_size_bytes": torrent.length,
                    "existing": existing, "uri_type": "validated_torrent", "resume": resume,
                    "seeders_are_snapshot": bool(existing)}
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
            self.store.record(job, torrent.sanitized)
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
