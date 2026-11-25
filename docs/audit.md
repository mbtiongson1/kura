# Library audit (`kura audit`)

One offline report that answers, per library title: which AniList entry is it, how does the
owner rate it, what does SeaDex consider the best release, what does the file actually contain,
and is anything worth a second look. It reads only Kura's private state (the manifest
inventory, links, title identities, probe results, the SeaDex mirror and the AniList list
snapshot), opens no library file, uses no network, and writes only one row in `audit_reports`.
Everything in it is a proposal: nothing is fetched, moved or deleted.

```
kura audit --library NAME [--retain FILE] [--policy-overrides FILE] [--user NAME]
           [--title KEY]... [--flag CODE]... [--limit 200] [--no-store]
```

- `--retain FILE`: AniList IDs, one per line, as for `kura manifest plan --retain`
  ([retain.md](retain.md)). Titles on it get the `retained` flag. Only the file's sha256 and ID
  count appear in the report.
- `--policy-overrides FILE`: lines `<AniList ID> archival|efficient  # comment`, same parser
  (`kura.idlist.parse`, tagged form). The file is the only copy; the report records its sha256
  and ID count.
- `--user NAME`: use that AniList user's synced list for favourites and scores. Default: the
  logged-in viewer. A name with no synced list is refused (`invalid_arguments`).
- `--title KEY` audits only those title keys (`<root label>/<entry>`); an unknown key is refused.
- `--flag CODE` lists only titles that carry the flag (`counts` still cover every audited title).
- `--limit N` lists at most N titles, worst severity first, then by title key;
  `titles_truncated` says how many were left out.
- `--no-store` skips the `audit_reports` row. Otherwise the report is stored as canonical JSON
  and the newest 30 per library are kept.

A library with no inventory is refused (`no_inventory`): run `manifest build` first. The audit reads
state in one snapshot and does not take the state lock, so it is not refused while a probe or sync
runs; those writers wait for the read to finish (up to their busy timeout). Only storing the
report takes the lock (`busy` if another mutation runs).

## Policy

A title is `archival` when the owner's override says so; otherwise when its AniList ID is one
of the owner's favourites or their list score is at least 9.0 (`favourite`, `score_9_plus`);
otherwise `efficient`. An override of `efficient` beats the automatic rules. A title without a
resolved or pinned identity is `efficient` (`default`). The reasons are in `policy_reasons`.

Efficient band, per episode (MB = 10^6 bytes): target 250, accepted 120 to 450. Archival has
no size cap. Band flags use the median of the title's episode files (extras excluded) and only
apply to titles with at least two video files or a recognisable episode number, so a lone movie
file is never judged by an episode band.

## Report

```
{"library", "generated_at", "report_sha256",
 "inputs": {"inventory_at",
            "probe": {"files_probed", "video_files", "coverage", "changed"},
            "seadex": {"synced_at", "stale"},
            "anilist": {"user", "fetched_at", "favourites_fetched_at", "stale"},
            "retain": {"sha256", "ids", "unmatched_ids"} | null,
            "policy": {"efficient": {"target_mb", "min_mb", "max_mb"}, "archival": {"cap": null},
                       "overrides_sha256", "overrides_ids"}},
 "counts": {"titles", "flagged", "by_flag": {}, "by_policy": {"archival", "efficient"},
            "by_suggestion": {"none", "review", "upgrade_candidate"}},
 "titles": [{"title", "anilist_id", "identity": {"status", "source"}, "policy", "policy_reasons",
             "retained", "files": {"video", "extras", "bytes", "per_episode_mb": {"median", "min", "max"}},
             "groups": [{"group", "files"}],
             "probe": {"sampled", "container", "video", "encoder", "settings", "audio", "subtitles",
                       "fonts", "chapters", "samples"},
             "seadex": {"entry", "incomplete", "best", "alt", "held_matches"},
             "flags": [{"code", "severity", "detail"}], "suggestion"}],
 "titles_truncated"}
```

`report_sha256` is the SHA-256 of the canonical JSON (sorted keys, compact, ASCII) of the report
without `generated_at` and without the hash itself, so the same state, inputs and clock give the
same hash. `flagged` counts titles with at least one `warn` or `action` flag. `coverage` is
`files_probed / video_files`; the default probe samples one file per release cluster, so it is
low by design.

Inputs that are old are reported and never stop the run: SeaDex is `stale` when its last
successful sync is older than 7 days or missing; the AniList list when its list or its favourites are older than 48 hours or missing. Favourites
that were never fetched are ignored, not read as empty; with no synced list every title falls back
to `efficient`.

### Probe summary

`probe` describes the title's representative sample (the probed file from the title's largest
name cluster); `samples` lists up to eight probed files compactly. `settings` is the compact
view of the encoder settings plus the track facts:

```
{"crf", "rc", "aq-mode", "psy-rd", "sao", "bit_depth", "bitrate_kbps"}
```

`sao` is `off`, `limit`, `selective`, `on` or null (x265 only). `encoder` is
`{"library", "version", "style", "crf_band", "notes"}`:

- `style` is `crf`, `bitrate` (2-pass or ABR with B-frames), `untouched_stream` or `unknown`.
  **Untouched stream video** is a web stream that is the streaming service's own encode: `rc`
  `2pass` or `abr` with `bframes=0`, or no encoder settings at all on a file named WEB-DL. It is
  never judged by CRF.
- `crf_band` (informational, never a flag) places an x265 10-bit CRF against the efficient
  18 to 24 range: `below`, `within`, `above`; null otherwise.
- `notes`: `untouched_stream_video`, `crf_below_band`, `crf_within_band`, `crf_above_band`,
  `no_encoder_settings` (not given to an untouched stream).

x265 option keys are hyphenated and x264's use underscores (`psy-rd` versus `psy_rd`); the
summary reads both. `writing_app` is not used: remuxers rewrite it.

Bits per pixel is `bitrate / (width x height x fps)`, from the probe's video bitrate and is null when
any input is missing. The probe keeps the track's `BPS` tag only when it is plausible for the file;
a tag copied from a source stream is replaced by file size over duration (less the audio tracks'
plausible rates), see [probe.md](probe.md). Such an estimate still includes subtitles and attachments,
so it can only overstate bits per pixel slightly. The `low_bpp_8bit` flag reports `bitrate_source`
(`tags` or `size_over_duration`) beside `bitrate_kbps`. Results probed by an older `PROBE_VERSION`
are ignored until `kura probe` runs again, so a stale tag from version 1 never feeds the audit.

## Flags

Severity is `info`, `warn` or `action`. A title's `suggestion` is `upgrade_candidate` (see below),
else `review` when it has any `warn` or `action` flag, else `none`.

| code | severity | when |
| --- | --- | --- |
| `mixed_groups` | warn | two or more known release groups across episode files (extras excluded) |
| `superseded_version_present` | action | the same group, season and episode held as v1 and as vN (season from `SxxExx` or a `Season N` directory) |
| `v2_known_upstream` | warn | a public SeaDex file list has `vN` of an episode (same group) for which a v1 is held |
| `low_bpp_8bit` | warn, action below 0.035; info when every low file is an untouched stream | a sampled 8-bit file has bits per pixel below 0.06; `detail.style` says whether it is an untouched stream |
| `ass_missing_fonts` | warn | an ASS track with no attached fonts and no `Fonts/` directory in the title (skipped when the attachment scan was cut short) |
| `seadex_best_differs` | warn archival, info efficient | a public SeaDex best exists and what is held is comparable (`held_matches` `none` or `alt`) and not a best |
| `seadex_alt_held` | info | the held group or infohash is a SeaDex alternative |
| `seadex_no_public_pick` | info | a SeaDex entry exists but no best pick is public (private trackers) |
| `seadex_best_outside_band` | info | efficient title; every public best with a known size is outside 120 to 450 MB |
| `size_above_band`, `size_below_band` | warn | efficient title with a resolved or pinned identity; median episode size outside the band (an unidentified title might be archival, so it is not judged) |
| `identity_unknown` | info, warn under `--retain` | no resolved or pinned identity (not for `not_anime`) |
| `probe_missing` | info | the title has video files and no usable probe result |
| `unsupported_container` | info | a sampled file is not Matroska |
| `retained` | info | on the retain list (`detail.reason` is `retained` or `retain_possible`) |

`seadex.held_matches` is `best`, `alt`, `none` or `unknown` (no entry, unknown identity, or
nothing held to compare). Held means the release groups parsed from the episode file names
(compared case- and punctuation-insensitively) plus the infohashes and groups of manifest links.
SeaDex flags are only produced for a resolved or pinned identity. A probe row counts only while
its status is `ok`, `partial` or `unsupported_container` (an `invalid` row says nothing), its size and mtime still equal the inventory and its probe version is current; others add to
`inputs.probe.changed` and are ignored.

`upgrade_candidate`: archival and `seadex_best_differs`; or efficient with a public SeaDex best
whose median episode size is inside the band and that is not held (`held_matches` `none` or `alt`;
an `unknown` comparison never proposes anything). It is a proposal for
the owner to look at, nothing more.

## Runner contract

The audit is a state-only step for a runner: it needs no media and no network, so a runner should
run it in an offline container with the policy files staged read-only. Absent titles (marked
`absent_from_scan` by `manifest build`) and superseded manifest links are ignored.

```
audit  ->  kura audit --library LIBRARY --retain /policy/retain.txt [--policy-overrides /policy/policy-overrides.txt]
```

Steps by class:

- media steps (network none, roots read-only, state read-write, refused during a parity check):
  `probe [--sample all] [--max-read-mib N]` runs `kura probe library --library LIBRARY --root LABEL=/library/LABEL ...`.
- networked steps (state read-write, no media): `seadex-sync [--full]`
  (`kura seadex sync [--full] --files-for-library LIBRARY`), `anilist-login` (the token is read by
  the host with `read -rs` and piped to `docker run -i ... kura anilist login --token-stdin`;
  never argv or `-e`), `anilist-url`, `anilist-whoami [--check]`, `anilist-list [--max-age-h 12]`,
  `anilist-media` (`kura anilist media --library LIBRARY --missing`), `identify`
  (`kura identity sync --library LIBRARY --max-lookups 60`) and `resolve` (5 s pacing is internal).
- state-only steps (network none, policy staged read-only): `audit`, `retain-check`
  (`kura retain check --library LIBRARY --retain /policy/retain.txt`) and `plan [--skip-swarm]`,
  which adds `--retain /policy/retain.txt` and refuses when the file is missing.

Suggested nightly order: build, probe, seadex-sync (incremental daily, full weekly), anilist-list,
anilist-media, identify, resolve, swarm, audit, plan. Back up `state.sqlite3` before the first
schema v3 image.
