# Roadmap and legacy boundary

Kura is the successor product in a **new repository**, not a rename, archive or rewrite
of MPV AniList Sync. The bootstrap architecture is direction, not implementation of
all the planning tracks. Existing desktop/UI/player behavior remains in the legacy repo.

## Landed scope (pending reviewed feature merge)
- Architecture/security bootstrap, protected main and lightweight CI.
- Headless Python/JSON CLI, Nyaa single-episode discovery and explicit acquisition.
- Persisted preferences, bounded local storage, safe aria2 subprocess, truthful receipts.
- Mock security/failure cases and a real authorized Termux loopback torrent transfer.

## Remaining design tracks
All links below are original issue identities; native GitHub transfer preserves redirects.
The verified destination-number map will be committed after the approved feature merge.

| Original issue | Track | Reconciliation |
| --- | --- | --- |
| [208](https://github.com/mbtiongson1/MPV-anilist-sync/issues/208) | Headless agentic core | Acquisition is the first bounded service, not completion of account/player/library/sync tools. Legacy refactor assumptions need a successor-product scope update. |
| [219](https://github.com/mbtiongson1/MPV-anilist-sync/issues/219) | Architecture epic | Target baseline established; infrastructure discovery and detailed integration ADRs remain. Repo rename is superseded by the authorized greenfield strategy. |
| [220](https://github.com/mbtiongson1/MPV-anilist-sync/issues/220) | Baseline/migration | Code audit and greenfield decision started; broader legacy/security/infrastructure inventory remains. No legacy credentials or archives imported. |
| [221](https://github.com/mbtiongson1/MPV-anilist-sync/issues/221) | Watch ledger / optional Supabase / AniList | Design only, unimplemented. Local acquisition audit is not a cross-device watch ledger. |
| [222](https://github.com/mbtiongson1/MPV-anilist-sync/issues/222) | Observation and identity | Design only; exact acquisition title matching is not canonical media identity or watch observation. |
| [223](https://github.com/mbtiongson1/MPV-anilist-sync/issues/223) | Unraid and organizer inventory | Design only; request actual share layout/organizer before decisions. |
| [224](https://github.com/mbtiongson1/MPV-anilist-sync/issues/224) | Followed-series acquisition | One explicit episode is implemented; subscriptions, scheduling and autonomous budgets are deferred. |
| [225](https://github.com/mbtiongson1/MPV-anilist-sync/issues/225) | Storage lifecycle / reacquisition | No cleanup/organization/archive/reacquisition implementation. Metadata receipts are only a foundation. |
| [226](https://github.com/mbtiongson1/MPV-anilist-sync/issues/226) | Tailscale/Plex/Android delivery | Native Termux storage only; remote playback/distribution is unimplemented. |
| [227](https://github.com/mbtiongson1/MPV-anilist-sync/issues/227) | Bounded agent tools and policy | JSON CLI and explicit approval exist; supported Gemini integration, remote identity, API and scoped recurring grants need design. |
| [228](https://github.com/mbtiongson1/MPV-anilist-sync/issues/228) | Termux torrent MVP | Evaluate closure only after Terra approval, green checks and verified feature merge; controlled test torrent is the authorized acceptance sample. |

The complete open-issue inventory at bootstrap contains these 11 Kura-relevant issues.
No additional legacy-only open issues were found. Re-query before transfer; do not migrate
old pull requests or unrelated closed history. Native transfer preserves descriptions,
history and references; repair bare legacy issue references using verified mappings.
