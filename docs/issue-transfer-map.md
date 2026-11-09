# Verified native issue transfers

Transferred **11 open Kura-relevant issues** from `mbtiongson1/MPV-anilist-sync`
to `mbtiongson1/kura` on 2026-11-09 UTC, **after** PR #1 was approved, merged
(`82300917b2ac85d3c09c5db009789c3aa675d99b`) and verified on native Termux.
No legacy-only open issues were found. No pull requests or closed history were moved.

| Legacy issue | Kura issue | Track | Reconciled state |
| --- | --- | --- | --- |
| [208](https://github.com/mbtiongson1/MPV-anilist-sync/issues/208) | [2](https://github.com/mbtiongson1/kura/issues/2) | Agentic core | Open: acquisition first; other workflows remain |
| [219](https://github.com/mbtiongson1/MPV-anilist-sync/issues/219) | [3](https://github.com/mbtiongson1/kura/issues/3) | Architecture epic | Open: baseline landed, integration design remains |
| [220](https://github.com/mbtiongson1/MPV-anilist-sync/issues/220) | [4](https://github.com/mbtiongson1/kura/issues/4) | Baseline / migration | Open: greenfield supersedes rename; broader audit remains |
| [221](https://github.com/mbtiongson1/MPV-anilist-sync/issues/221) | [5](https://github.com/mbtiongson1/kura/issues/5) | Event ledger / optional sync | Open, unimplemented |
| [222](https://github.com/mbtiongson1/MPV-anilist-sync/issues/222) | [6](https://github.com/mbtiongson1/kura/issues/6) | Observation / identity | Open, unimplemented |
| [223](https://github.com/mbtiongson1/MPV-anilist-sync/issues/223) | [7](https://github.com/mbtiongson1/kura/issues/7) | Unraid inventory | Open, unimplemented |
| [224](https://github.com/mbtiongson1/MPV-anilist-sync/issues/224) | [8](https://github.com/mbtiongson1/kura/issues/8) | Followed-series acquisition | Open: explicit one-episode path only |
| [225](https://github.com/mbtiongson1/MPV-anilist-sync/issues/225) | [9](https://github.com/mbtiongson1/kura/issues/9) | Storage lifecycle / reacquisition | Open, unimplemented |
| [226](https://github.com/mbtiongson1/MPV-anilist-sync/issues/226) | [10](https://github.com/mbtiongson1/kura/issues/10) | Private playback / distribution | Open, unimplemented |
| [227](https://github.com/mbtiongson1/MPV-anilist-sync/issues/227) | [11](https://github.com/mbtiongson1/kura/issues/11) | Bounded tools / policy | Open: local JSON/approval foundation only |
| [228](https://github.com/mbtiongson1/MPV-anilist-sync/issues/228) | [12](https://github.com/mbtiongson1/kura/issues/12) | Termux torrent MVP | **Closed, completed**, after acceptance reconciliation |

## Verification and preservation
Used `gh issue transfer`, never issue recreation. Each old URL was verified to redirect
to the mapped issue; each destination has a native `transferred` timeline event.
Descriptions were compared after canonicalizing GitHub's automatic reference rewrites.
For subsequent transfers, creation date, author and comment count were compared before/
after. Bare/qualified legacy references and full issue URLs were repaired and read back;
untransferred references remain qualified to the legacy repository.

GitHub rotated global issue/node IDs, and mutation reads were briefly eventually
consistent. The verifier stopped rather than misreport success, recovered existing
moves through redirects without replay, and used bounded consistency retries. The first
issue's pre-transfer comment-count snapshot was not retained; its native event, description,
history and redirect were verified. This limitation is explicit in the ledger.

Machine-readable audit: [issue-transfer-map.json](issue-transfer-map.json), with old/new
IDs, verification flags, description/reference hashes and reconciled states. Hashes are
dated snapshots, not claims that issue bodies can never change. The MVP acceptance
checkbox update has a separate final hash.

## Roadmap reconciliation
MVP #12 was closed only after [acceptance evidence](https://github.com/mbtiongson1/kura/issues/12#issuecomment-6729494566):
54 native Termux tests, actual authorized transfer and offline partial resume, exact
Terra approval, green protected merge, post-merge rerun, CodeQL, and unchanged legacy
baseline with successful existing CI. The later greenfield/Sol mission supersedes the
original issue's in-place/Flash assumptions; no legacy credentials/settings were silently
imported. Other tracks remain open; [roadmap](roadmap.md) and
[epic reconciliation](https://github.com/mbtiongson1/kura/issues/3#issuecomment-6729495908)
distinguish landed work from future design.

Final transfer inventory: **0** open issues remain in the legacy repo; **10** transferred
design/core issues remain open in Kura, plus the completed MVP. The legacy application
repository remains **unarchived**, with application code **unchanged**.
