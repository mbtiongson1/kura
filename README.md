# Kura

Headless, agent-first personal media workflows, starting on native Android Termux.

Kura succeeds MPV AniList Sync as a **new repository**. The legacy application remains
unchanged and supported by its own repository; this is not a full migration.

## Vision and goals
Composable, typed core services usable by humans, Pi and future supported agent tools.
Safe discovery and authorized acquisition, durable local metadata, explainable actions,
and eventually private playback, watch history and cautious library lifecycle management.

## First MVP
A small Python CLI: Nyaa episode discovery, conservative release selection, saved
preferences and a real foreground aria2c download with verified completion.
No AniList login or always-running web server.

## Roadmap
Architecture/security bootstrap -> protected main -> torrent CLI and Termux evidence ->
independent review -> event/identity and infrastructure design tracks. Optional future
Unraid, Plex, Tailscale, AniList, Supabase synchronization and GUI are **not implemented**.
Rust is only a future GUI candidate, independent of the core language.

Read [ARCHITECTURE.md](ARCHITECTURE.md) and [security policy](.github/SECURITY.md).
Only download material you are legally authorized to acquire.

## Install on native Android Termux
Use a maintained Termux build (for example F-Droid), not proot, Docker or root.

```sh
pkg update
pkg install python aria2 git
# No Python runtime dependencies. pip installs the setuptools build dependency.
git clone https://github.com/mbtiongson1/kura.git
cd kura
python -m venv .venv
. .venv/bin/activate
python -m pip install --no-deps .
kura prefs show
kura prefs set --resolution 1080p
```

Python >=3.11, aria2 >=1.37.0. Do not upgrade Termux's system pip with pip itself.
For an editable development install use `python -m pip install --no-deps -e .`.
All runtime code uses Python's standard library. On a fresh virtualenv, do not use
`--no-build-isolation` unless setuptools has already been installed in it.

## Commands
```sh
kura search "Anime Title" --episode 3 --json
kura download "Anime Title" --episode 3 --dry-run
# Only for a torrent you have permission to acquire:
kura download "Anime Title" --episode 3 --yes
kura downloads status
kura prefs set --resolution 1080p --groups "SubsPlease,Erai-raws" \
  --subtitles english --max-size-mb 1500 --min-seeders 1 --destination private
```

Every command emits one JSON v1 envelope, even without `--json`. Exit 0 means the
operation succeeded (a dry-run is only a plan); exit 2 is a structured refusal/error;
exit 130 is interruption. `--help` is human-readable. `--max-size-mb` is MiB.
The downloader lifecycle guard currently requires Linux/Android; it is not a desktop
Windows/macOS acquisition implementation.
Preferences persist across invocations; search/download accept the same overrides
without changing saved values. `--groups ''` clears the allowed group list.
English/raw subtitle preference means the provider category, **not** inspected subtitle
tracks or a guarantee of language. Nyaa trusted status is not a legal-rights guarantee.

The parser deliberately accepts only `Series - 03` or `Series S02E03` with metadata
tags. Query `Series S02` for the latter. No fuzzy titles, episode ranges, season packs,
multiple files or inferred specials. Unsupported naming is rejected rather than guessed.
Resolution and group constraints are enforced; ranked results prefer the ordered group
list then seeders. Automatic selection needs exactly one trusted candidate in the best
group tier. Seed counts do not resolve identity ambiguity. If refused, inspect search
and explicitly choose an eligible `--release-id 12345`; this can also select an untrusted
release. It does not bypass title, episode, size, filename or path validation.

`--dry-run` fetches/inspects metadata but never starts aria2 or reserves a job.
Without `--yes`, a terminal asks for literal `yes`; noninteractive calls refuse.
`--yes` asserts permission for this one selected torrent, not ongoing automation.
No media is acquired during search. Descriptor retrieval uses Nyaa HTTPS only, rejects
redirects and discards all descriptor trackers/webseeds; aria2 uses one fixed Nyaa tracker.
Torrent peer traffic exposes your IP and uses a temporary BitTorrent listener; **no RPC
endpoint** is enabled. Keep aria2 patched. There is no persistent seeding service;
`--seed-time=0` exits at completion, though BitTorrent may upload pieces while downloading.

## Storage, interruption and status
Default state: `~/.local/share/kura/state.sqlite3` (preferences, receipts, audit events).
Default payloads: `~/.local/share/kura/downloads/<infohash>/<validated-name>`.
No legacy settings, credentials, torrents or media are imported automatically.
Destination capabilities are `private[/subfolder]` and `shared[/subfolder]` only, not
arbitrary paths. Symlinks and traversal are refused. To opt in to shared Android storage:

```sh
termux-setup-storage
kura prefs set --destination shared
# Writes only beneath /storage/emulated/0/Download/Kura (if permission is granted).
```

Android version/storage-provider permissions vary; app-private storage is the tested
baseline. No broad storage grants are required for it. Budget: default 1500 MiB, hard
maximum 10000 MiB per torrent, plus 64 MiB free reserve. Actual descriptor length, not
RSS rounding, is authoritative. One Kura mutation runs at a time. Other applications
can still consume disk; disk-full never becomes a successful receipt.

Foreground operation only. Optionally `termux-wake-lock` during long transfers and
review Android battery optimization; neither guarantees survival. Timeout is 30 minutes
with a 120-second no-progress bound. Ctrl-C/SIGTERM stops the child; SIGKILL/process death
is reported as interrupted on the next invocation. A Linux/Android parent-death signal
also terminates the aria2 child when the supervisor dies. Partial data is preserved. Retry with
`--resume --yes` and the same release/destination. Never delete files merely to retry.
A completed receipt blocks duplicate acquisition of the same title/episode or infohash.
No automatic cleanup, move, purge or reacquisition policy exists. Completion requires
aria2 exit 0, exact file size, no partial-control file, every torrent piece hash, and a
recorded SHA-256. `downloads status` reports durable receipts, not an active RPC queue.

## Verify without copyrighted media
```sh
python -m unittest discover -s tests -v
python tests/termux_e2e.py
```
The opt-in second command uses **original CC0 test bytes**, a loopback-only HTTP tracker
and a temporary local aria2 seeder. It exercises the real CLI search/dry-run/approval/
aria2/verification/status/duplicate path. Only Nyaa HTTP fetch and tracker/peer-port are
substituted in the harness; no production CLI test bypass exists. It neither downloads
public copyrighted media nor proves public swarm availability. Generated descriptors,
media and state are temporary and not committed. Unit tests need no network or aria2.
See [handoff](docs/handoff.md) for runtime evidence and limitations.
