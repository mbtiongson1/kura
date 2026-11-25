# Kura roadmap

Kura is a new successor repository, not a rename, archive or rewrite of MPV AniList Sync.
The old application remains unchanged. Native issue transfer mapping:
[issue-transfer-map.md](issue-transfer-map.md) and [JSON ledger](issue-transfer-map.json).

## Landed
[PR #1](https://github.com/mbtiongson1/kura/pull/1), merged as
`82300917b2ac85d3c09c5db009789c3aa675d99b`, independently Terra-approved:
- Architecture/security baseline, protected main, lightweight CI and security scanning.
- Headless Python/JSON CLI, conservative Nyaa single-episode discovery and explicit aria2 acquisition.
- Local preferences/overrides, safe scoped storage, idempotency, interruption and intentional offline resume.
- Verified files/piece hashes/SHA-256, 54 tests and real authorized native Termux transfer evidence.

## Library handover (read-only, in progress)
Offline pipeline toward proposing which library titles can be cleared because a healthy,
re-downloadable torrent backs them (the owner always performs the purge):
- Landed: descriptor parsing and protection classifier, read-only library inventory,
  state schema v2 with the manifest repository, piece-hash verification of local trees.
- Landed with the manifest CLI: exact-name matching, `kura library inventory`,
  `kura torrent inspect|verify`, `kura manifest build|show`, per-title status
  (`reacquirable`, `partial`, `unverified`, `unmatched`, `protected`, `blocked`, `stale`),
  evidence-based verification caching and rate-limit-aware AniList identity.
- Landed: `kura manifest resolve` fetches candidate descriptors for unmatched titles; the
  normal build then verifies them.
- Next: quarantine plan (same-volume rename proposals, tree hashes, undo list, purge only
  after a waiting period and a fresh swarm check), swarm health scraping, and an explicit
  owner approval for files no torrent lists.

## Library quality (planned)
Builds on the handover pipeline to judge whether the library holds the best available release of
each title. Foundation landed first: state schema v3 (cache tables for the quality data), a
cross-process request limiter shared by every networked service, and a CLI command registry so
later modules add commands without touching the core parser.

Landed: the foundation, the Nyaa request pacing, the AniList login and list sync, the SeaDex
mirror, title identity with retain lists, the Matroska header probe, and the audit
(`kura audit`, [audit.md](audit.md)).

Build order, each step read-only toward the library:
1. Audit (landed): per-title identity (AniList), the owner's list, community best-release data,
   and local file probing, combined into one offline report.
2. Release cards and a `best` view per title.
3. Wanted list: titles the audit shows as missing or weak.
4. Upgrade proposals: owner-approved candidates only.

Later, not in this wave:
- BEP 9 magnet metadata fallback (needs peer connections; descriptor fetch over HTTP stays the default).
- Header peek from peers (needs its own ADR and is opt-in).

## Reconciled tracks

| Kura issue (legacy) | Track | Remaining work |
| --- | --- | --- |
| [#2](https://github.com/mbtiongson1/kura/issues/2) (208) | Agentic core | Acquisition is the first service; account/player/library/season/sync workflows are not implemented. |
| [#3](https://github.com/mbtiongson1/kura/issues/3) (219) | Architecture epic | Baseline established; infrastructure discovery and detailed integration ADRs remain. New repo supersedes rename plans. |
| [#4](https://github.com/mbtiongson1/kura/issues/4) (220) | Audit / migration | Limited reuse audit and greenfield decision landed; broader legacy/infrastructure/security inventory remains. |
| [#5](https://github.com/mbtiongson1/kura/issues/5) (221) | Watch ledger / optional sync | Design only. Acquisition audit is not a cross-device watch ledger or AniList reconciler. |
| [#6](https://github.com/mbtiongson1/kura/issues/6) (222) | Observation / identity | Design only. Exact acquisition matching is not canonical identity or watch observation. |
| [#7](https://github.com/mbtiongson1/kura/issues/7) (223) | Unraid inventory / organizer | Design only; discover actual shares and organizer before changing anything. |
| [#8](https://github.com/mbtiongson1/kura/issues/8) (224) | Followed-series acquisition | One explicit episode landed; subscriptions, scheduling and autonomous budgets remain deferred. |
| [#9](https://github.com/mbtiongson1/kura/issues/9) (225) | Storage lifecycle | No cleanup/organization/archive/reacquisition implementation; receipts are only a foundation. |
| [#10](https://github.com/mbtiongson1/kura/issues/10) (226) | Private playback / distribution | Termux storage only; remote Plex/Tailscale/Android delivery is not implemented. |
| [#11](https://github.com/mbtiongson1/kura/issues/11) (227) | Agent tools / policy | JSON CLI and explicit approval exist; supported Gemini interfaces, remote identity/API and recurring grants need design. |
| [#12](https://github.com/mbtiongson1/kura/issues/12) (228) | Termux torrent MVP | **Completed/closed**, with acceptance evidence after verified merge. |

All ten core/design tracks remain open; the epic is not complete merely because a
baseline architecture exists. GUI/Rust is still an independent future client decision,
not an approved implementation. Supabase, Unraid, Plex, Tailscale and AniList are not
implemented. No additional providers, daemon, seasonal automation, cleanup or seeding
policy has been added. Do not infer authorization for these from this roadmap.
