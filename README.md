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
kura aliases "some alias"
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

### Alias resolution (AniList)
Titles are often known by an alias rather than the romanized Nyaa series name.
`kura aliases "dark item"` performs one bounded, unauthenticated AniList GraphQL query
and returns the candidate spellings it matched. `search` and `download` use the same
lookup before querying Nyaa, so the alias resolves without hand-guessing:

```sh
kura aliases "dark item"
# {"status": "resolved", "identity": {"anilist_id": 186742,
#  "matched": "A Certain Dark Item", "titles": ["A Certain Dark Item",
#  "Toaru Anbu no ITEM", ...]}}
kura search "dark item" --episode 7
kura download "dark item" --episode 7 --yes
```

Alias metadata is untrusted discovery input only. It never authorizes a download,
never relaxes preferences, and never picks a release: resolution must be an exact title
or a whole-word subset match, and equal-best candidates from different AniList entries
are refused as `ambiguous` rather than guessed. Results are cached for 7 days in the
private state database (network failures for 10 minutes) and `--refresh` re-queries.
Network failure degrades to `status: unavailable` and the literal title, so search still
works offline from cache. `--no-alias` disables resolution for deterministic/offline use.
Unknown or unsupported romanizations still need `--release-id` or `--groups`.
A resolved identity also keys duplicate receipts, so a later request under another alias
cannot re-acquire the same episode.
English/raw subtitle preference means the provider category, **not** inspected subtitle
tracks or a guarantee of language. Nyaa trusted status is not a legal-rights guarantee.

The parser deliberately accepts only `Series - 03` or `Series S02E03` with metadata
tags. Query `Series S02` for the latter. No fuzzy titles, episode ranges, season packs,
multiple files or inferred specials. Unsupported naming is rejected rather than guessed.
Numeric metadata is fail-closed: only explicit codec/quality/audio/dimension/frame-rate,
version, standalone year and CRC formats are recognized. Unknown numeric tags, including
numeric release-group labels, are unsupported rather than interpreted as safe metadata.
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

## Library handover: reacquisition manifest (read-only)
Kura can answer one question about an existing media library: which titles could be
cleared from local storage because they are safely backed by a healthy torrent? The model
is a photo app that clears the local copy of a picture once it is safely in the cloud. A
copy may only be *proposed* for clearing when it is **on the server** (every file's bytes
verified, piece by piece, against a torrent descriptor) AND its **torrent is healthy and
re-downloadable** (public and unprotected; swarm health is a later stage). **Kura
proposes; the owner purges.** Nothing in this pipeline moves, renames, deletes or writes
inside a library or torrent directory: library and descriptor paths are only opened
read-only, never through a symlink.

```sh
kura library inventory --root media=/path/to/library [--files]
kura torrent inspect release.torrent --tracker-denylist deny.txt --protected protected.txt
kura torrent verify release.torrent --root /path/to/library [--base subdir] [--max-read-gib 50]
kura manifest build --library home --root media=/path/to/library --torrents /path/to/torrents \
  --tracker-denylist deny.txt --protected protected.txt [--max-read-gib 200] [--reverify] [--no-anilist]
kura manifest show --library home [--status reacquirable] [--title media/Some\ Title]
```

`library inventory`, `torrent inspect` and `torrent verify` are **stateless**: they never
open the state database or touch `HOME`, so they run in a throwaway container with a
read-only root filesystem and a read-only library mount (inventory and verify need no
network). Only `manifest build` and `manifest show` use the private state database.

`manifest build` scans every `--root` (metadata only) and the `.torrent` files directly
inside `--torrents`, matches each torrent to a library title by exact name (the title
equals the torrent name, the torrent sits directly inside a title folder, or a
single-file torrent's file name is unique; any other situation is `ambiguous` and left
unlinked), verifies each linked title against the piece hashes, and records the result
per torrent as it goes, so an interrupt or `--max-read-gib` budget keeps what finished.
A verification is reused while every file's size, mtime, device and inode still match;
touch a file, or pass `--reverify`, and it is read again. Private torrents are never
read. AniList identity lookup is on by default for linked titles and can be switched off
with `--no-anilist`; it is paced to at most 30 requests per minute, honours `429` /
`Retry-After` and the rate-limit headers, is cached for 7 days, and degrades to
`unavailable` rather than failing a build.

Titles are keyed `<root label>/<top-level entry>`. `manifest show` recomputes each title's
status from the database alone:

| status | meaning |
| --- | --- |
| `reacquirable` | every file is listed by a linked torrent whose files all verified (a `complete` verification), nothing protected, both policy lists supplied |
| `partial` | some files verified; others failed, are unverified, or no torrent lists them (`uncovered_files`) |
| `unverified` | linked but nothing verified (not yet read, budget spent, mismatching bytes, or policy lists missing) |
| `unmatched` | no torrent linked |
| `protected` | a `path:` entry covers it, or a linked torrent is private, on the tracker/source denylist, on the protected list or has unparsable announce data |
| `blocked` | the inventory could not follow something inside it (symlink, mount crossing, special file) |
| `stale` | the title vanished from the latest scan, or its files changed since they were verified |

Fail closed: anything unparsable or unrecognised is protected or unverified, never
reacquirable. A title containing **any file that no matched torrent lists** (an extra
file) is never reacquirable until the owner explicitly approves those exact files in a
later step. Announce URLs can embed private-tracker credentials, so they are never stored
or printed; a magnet link is produced only for public, unprotected torrents and only from
credential-free trackers. `manifest build` needs both policy files; an absent file means
the status cannot reach `reacquirable`. Policy files: `#` comments; denylist lines are
tracker hosts (matching subdomains) or `source:TAG`; protected-list lines are 40-hex
infohashes or `path:relative/prefix`. Descriptors that cannot be parsed are reported in
`skipped` and never linked. Several descriptors of one infohash are merged conservatively
(protected if any copy is). A link the latest scan no longer produces (torrent removed,
now ambiguous) stops counting toward coverage but keeps protecting; a torrent whose
protection was classified under a different policy than the title's last assessment is
`unverified` until the next build reprocesses it.

## Storage, interruption and status
Default state: `~/.local/share/kura/state.sqlite3` (preferences, receipts, audit events
and validated sanitized descriptors for explicit offline resume). This is private data,
not an export to Git or a public torrent archive.
The state database is schema version 2 (library manifest tables). Kura migrates an existing
version 1 database on first open; older Kura builds refuse a version 2 database, so keep one
build per state directory after upgrading.
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
maximum 10000 MiB per torrent, plus 64 MiB free reserve. On explicit resume, only
already allocated blocks within the validated partial payload earn disk-space credit;
sparse logical lengths do not. Actual descriptor length, not RSS rounding, is authoritative. One Kura mutation runs at a time. Other applications
can still consume disk; disk-full never becomes a successful receipt.

Foreground operation only. During child termination/reaping, further Ctrl-C/SIGTERM
is temporarily ignored so retries cannot overlap a still-exiting downloader.
Optionally `termux-wake-lock` during long transfers and
review Android battery optimization; neither guarantees survival. Timeout is 30 minutes
with a 120-second no-progress bound. Ctrl-C/SIGTERM stops the child; SIGKILL/process death
is reported as interrupted on the next invocation. A Linux/Android parent-death signal
also terminates the aria2 child when the supervisor dies. Partial data is preserved. Retry with
`--resume --yes` and the same release/destination. Resume uses the recorded sanitized
descriptor and release identity, not a fresh provider search: disappearing releases,
changing seed counts or newly ambiguous search results cannot select a different torrent.
Current quality/group/category/byte limits still apply; the recorded seeder count is a
snapshot, not a fresh availability claim. Changed defaults may require explicit overrides
to match the original destination. Never delete files merely to retry.
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
and a temporary local aria2 seeder. It also interrupts a real partial transfer and resumes
without Nyaa access under a mocked remaining-space limit smaller than the full torrent.
It exercises the real CLI search/dry-run/approval/
aria2/verification/status/duplicate path. Only Nyaa HTTP fetch and tracker/peer-port are
substituted in the harness; no production CLI test bypass exists. It neither downloads
public copyrighted media nor proves public swarm availability. Generated descriptors,
media and state are temporary and not committed. Unit tests need no network or aria2.
See [handoff](docs/handoff.md) for runtime evidence and limitations.
