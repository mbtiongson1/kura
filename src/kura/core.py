"""Headless acquisition service, shared by the CLI and future bounded tool adapters."""
from dataclasses import asdict, replace
import time

from . import downloader
from .models import KuraError, Preferences, Provider, Release
from .releases import eligible, normalize, parse_title, query_check, select
from .safety import destination, ensure_dir, no_symlinks, regular_file
from .store import Store, validate_prefs
from .torrent import inspect

# Alias metadata is a discovery hint, not authority. Cache it to keep lookups quick
# and re-check it well before a title is likely to be renumbered.
ALIAS_TTL = 7 * 24 * 3600
ALIAS_FAILURE_TTL = 600
CACHEABLE = ("resolved", "unresolved", "ambiguous")


class Core:
    def __init__(self, store: Store, provider: Provider, aliases=None, alias_ttl: int = ALIAS_TTL):
        self.store, self.provider, self.aliases, self.alias_ttl = store, provider, aliases, alias_ttl

    def preferences(self, overrides: dict) -> Preferences:
        return validate_prefs(replace(self.store.prefs(), **overrides))

    def alias_status(self, title: str, refresh: bool = False) -> dict:
        """Bounded alias lookup. Never raises for network failure; never authorizes."""
        query_check(title, 1)
        disabled = {"status": "disabled", "titles": [title], "identity": None}
        if self.aliases is None:
            return disabled
        key = normalize(title)
        now = time.time()
        cached = None if refresh else self.store.alias(key)
        if cached is not None:
            ttl = ALIAS_FAILURE_TTL if cached.get("status") == "unavailable" else self.alias_ttl
            if 0 <= now - cached["fetched_at"] < ttl:
                cached.pop("fetched_at", None)
                return cached
        try:
            identity, status = self.aliases.resolve(title)
        except KuraError as e:
            if e.code not in ("alias_unavailable", "alias_invalid", "unsafe_url"):
                raise
            result = {"status": "unavailable", "titles": [title], "identity": None,
                      "reason": e.code}
            self.store.save_alias(key, result, now)
            return result
        result = {"status": status, "identity": identity.json() if identity else None,
                  "titles": list(identity.titles) if identity else [title]}
        self.store.save_alias(key, result, now)
        return result

    def discover(self, titles, episode: int, p: Preferences, release_id=None) -> list[Release]:
        """Query spellings in order, stopping at the first eligible batch.

        Bounded: one provider query per spelling, deduped by infohash, and the
        cheapest spelling usually wins. Selection policy is unchanged.
        """
        rows, found = {}, []
        for title in titles:
            for release in self.provider.search(title, episode, p.subtitles):
                rows.setdefault(release.infohash, release)
            found = eligible(list(rows.values()), titles, episode, p)
            if found and (release_id is None or any(r.id == release_id for r in found)):
                break
        return found

    def search(self, title: str, episode: int, overrides: dict, aliases: bool = True):
        query_check(title, episode)
        p = self.preferences(overrides)
        alias, titles = self._identity_for(title, aliases)
        return p, self.discover(titles, episode, p), alias

    def _identity_for(self, title: str, aliases: bool, recorded: dict | None = None):
        """Alias spellings for one command; resume reuses the recorded set, never network."""
        disabled = {"status": "disabled", "titles": [title], "identity": None}
        if recorded is not None:
            stored = recorded.get("identity")
            if isinstance(stored, dict) and stored.get("status") == "resolved" and stored.get("titles"):
                return stored, list(stored["titles"])
            return disabled, [title]
        if not aliases:
            return disabled, [title]
        alias = self.alias_status(title)
        return alias, (list(alias["titles"]) if alias["status"] == "resolved" else [title])

    def status(self):
        try:
            with self.store.lock():
                self.store.recover()
        except KuraError as e:
            if e.code != "busy":
                raise
        return {"downloads": self.store.jobs()}

    def download(self, title: str, episode: int, overrides: dict, *,
                 dry_run=False, yes=False, resume=False, release_id=None, confirm=None,
                 aliases: bool = True):
        # Serialize mutations across processes, including the entire foreground transfer.
        with self.store.lock() as lock_fd:
            self.store.recover()
            query_check(title, episode)
            p = self.preferences(overrides)
            media_key = normalize(title) + ":" + str(episode)
            jobs = self.store.jobs()
            existing = next((j for j in jobs if j["media_key"] == media_key), None)
            if existing is None and resume:
                # A recorded job may have been started under another alias spelling.
                wanted_key = normalize(title)
                existing = next((j for j in jobs if j.get("episode") == episode and
                                 any(normalize(t) == wanted_key for t in
                                     (j.get("identity") or {}).get("titles") or [])), None)
            alias, titles = self._identity_for(title, aliases, existing if resume else None)
            identity_key = None
            if alias.get("identity"):
                identity_key = "anilist:%d:%d" % (alias["identity"]["anilist_id"], episode)
                existing = existing or next((j for j in jobs if j.get("identity_key") == identity_key), None)
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
                if not eligible([release], titles, episode, replace(p, min_seeders=0)):
                    raise KuraError("resume_preferences", "Recorded release conflicts with current preferences")
                descriptor = self.store.descriptor(release.infohash)
                if descriptor is None:
                    raise KuraError("resume_unavailable", "Recorded job has no validated descriptor; no download started")
            else:
                rows = self.discover(titles, episode, p, release_id)
                release = select(rows, p, release_id)
                descriptor = self.provider.descriptor(release)
            torrent = inspect(descriptor, release.infohash, p.max_size_mb * 1024**2)
            # Approval identity must agree with the payload, independently of preferences.
            wanted = {normalize(t) for t in titles if normalize(t)}
            parsed = parse_title(torrent.name)
            if (not parsed or normalize(parsed[0]) not in wanted or parsed[1] != episode or
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
                    "identity": alias, "identity_key": identity_key,
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
