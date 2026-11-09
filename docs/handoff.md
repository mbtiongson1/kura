# Kura bootstrap and torrent MVP handoff

## Final feature result
**MERGED**: [PR #1](https://github.com/mbtiongson1/kura/pull/1), squash commit
`82300917b2ac85d3c09c5db009789c3aa675d99b` (2026-11-09 UTC).
Shared-provider Terra independently approved exact request HEAD
`8c1554b5a8e4625e3290540a0e7df52cae262f14`, findings empty, in round 9.
Approval is recorded in [the PR](https://github.com/mbtiongson1/kura/pull/1#issuecomment-6729347682).
Actual child session events confirmed provider `openai-codex`, model `gpt-6.1-terra`,
effort `high`; no substitute reviewer or fabricated GitHub-account approval.
Sol verified matching remote HEAD, clean checkout, green required `tests` and enforced
main protection, then merged normally with `--match-head-commit` (no admin bypass).

Post-merge exact main passed **54 native Termux tests**, original torrent transfer,
real interruption/offline resume, 86 piece hashes and SHA-256 for 1,408,000 CC0 bytes.
CodeQL Python analysis [42540335118](https://github.com/mbtiongson1/kura/actions/runs/42540335118)
passed; default setup is configured (weekly). Code/secret scanning open alerts: zero
at verification. Main protection was read back again and remains enforced.

Legacy application code remains clean at `c4f15e1b508691f91c16b8081d336f5483bdf432`.
Its existing [CI run 42533897716](https://github.com/mbtiongson1/MPV-anilist-sync/actions/runs/42533897716)
was queried after the Kura merge: Ubuntu/macOS/Windows tests, frontend, typecheck,
version-sync and docker all report success. This is upstream per-commit evidence,
not a claim of rerunning the legacy suite locally or altering the old application.

## Ownership and scope
Lead/sole implementer: Sol (`openai-codex/gpt-6.1-sol`). Independent reviewer:
Terra shared (`openai-codex/gpt-6.1-terra`, high, read-only). No worker implementation.
Repository: https://github.com/mbtiongson1/kura (new, public).
Architecture-only root commit: `25f26cc` (no feature code).
Legacy baseline: `c4f15e1b508691f91c16b8081d336f5483bdf432`; working tree clean,
no application changes. Issue transfer is permitted only after the above approved,
green feature merge and post-merge verification.

## Implementation
- Stdlib-only Python >=3.11 headless services and typed models/provider port.
- Nyaa RSS search, strict single-episode parser, exact normalized title/season,
  group/resolution/category/size/seeder filters and conservative ambiguity refusal.
- Local SQLite preferences, unique media/infohash receipts, transaction audit events
  and cross-process mutation lock. Explicit confirmation and intentional resume.
- Canonical bounded bencode parser, exact infohash and filename identity check,
  single-file-only byte budget, sanitized descriptor (no provider trackers/webseeds).
- Safe foreground aria2 argument array; no inherited config/proxies/hooks, shell,
  RPC, DHT, daemon or completed-torrent seeding. Fixed Nyaa tracker only.
  Linux/Android PR_SET_PDEATHSIG terminates aria2 even on supervisor SIGKILL;
  normal Ctrl-C/SIGTERM/timeout waits for the child before releasing the mutation lock.
  The child inherits the operation lock FD, preventing a SIGKILL/retry overlap while
  the kernel-delivered termination is still being handled.
- Approved private/shared roots, no symlink/traversal paths; per-job hash directory.
- Free-space reserve, timeout/interrupt handling, exact sizes and piece hashes,
  SHA-256 receipt; no descriptor-only or exit-code-only success.
- JSON v1 CLI and reproducible native Termux install/test instructions.

## Legacy reuse decision
Inspected `src/nyaa.py`, `src/parser.py`, `src/settings.py`, old architecture and open
issues. Retained the worthwhile RSS namespace/metadata, episode/batch concepts and
resolution/group/destination preference vocabulary. Reimplemented only the small
needed logic; no copyrighted archive, OAuth, UI, full settings file or web server
copied. Legacy `download_torrent` uses xdg-open and cannot prove bytes downloaded;
that seam is replaced here rather than migrated. No additional providers.

## Repository controls
Immediately after the root commit, `main` protection was configured and read back
through `gh api`: PR required, admin enforcement, zero required GitHub approvals
(solo maintenance), no force-push/deletion, linear history and resolved conversations.
Required strict `tests` check added before feature push. Independent Terra approval
is an operational merge gate, not a fabricated second-account GitHub approval.
Secret scanning, push protection, vulnerability alerts, automated security fixes and
private vulnerability reporting enabled. CodeQL initially returned 422 while main was
docs-only; after Python landed it was enabled for Python and its first analysis passed.
Default setup is configured, query suite default, weekly schedule. Initial secret/alert
controls remained active throughout.
CI is one Python 3.11 unit-test job on PR/main, no matrix or costly packaging.

## Validation evidence
- Native Android 15, aarch64/arm64-v8a, Linux Android kernel; no proot/container/root.
- Python 3.14.6 and aria2 1.37.0 available via Termux.
- Fresh virtualenv editable build/install completed; `kura prefs show` returned JSON
  default preferences successfully. No runtime third-party Python dependencies.
- An initial `--no-build-isolation` install failed because a fresh venv lacks
  setuptools. The documented normal isolated build succeeded; this is not a runtime
  incompatibility and the README warns against that unsupported install shortcut.
- Initial pre-review source: **45 unit tests passed**, including real supervisor-SIGKILL child
  termination, cross-process locks, persistent preferences in fresh processes,
  malicious URLs/redirects/XML/bencode/paths, low disk, timeout/interruption and
  exit-0-without-bytes refusals. `git diff --check` and compileall passed.
- Real CLI/aria2 controlled torrent: search, dry-run, explicit download, status and
  duplicate refusal passed on native Android. **44,000 bytes**, 3 pieces verified,
  SHA-256 `c3b1993690573ada77108391e46f9f60507f80a19d12af35e3816fd795731342`.
  Full sanitized output: [validation-termux.txt](validation-termux.txt). Test payload
  and temporary descriptors/state were removed by the harness after verification.
- Live Nyaa HTTPS metadata search for `Kura Authorized Sample`, episode 3, succeeded
  with zero candidates (expected for an invented fixture name). No public media was
  downloaded. This proves reachability, not availability of an authorized public swarm.
- During development, a harness-only version-report call initially failed after the
  transfer had succeeded; fixed and rerun. The new death guard also exposed Python
  3.14's `android` platform identifier; explicitly supported and rerun with no skips.

## Limitations and deferred work
Public Nyaa swarm availability is not guaranteed. Controlled tests substitute only
Nyaa HTTP fetch and tracker/peer-port while running real aria2 and CLI policy. Subtitle
language is a provider category claim, not stream inspection. Conservative naming and
single-file constraints intentionally reject many legitimate releases. Private app
storage is the baseline; shared Android storage permission/layout is conditional.
Same-UID malicious Termux processes, root and compromised system DNS are outside the
filesystem isolation claim. Android process survival is not guaranteed. A durable
complete receipt is historical evidence, not a promise the user has retained a file.
No GUI/Rust implementation, Supabase, Unraid, Plex, Tailscale, AniList, schedules,
background queue, autonomous cleanup or seeding-policy implementation.

## Independent review loop
Terra round 1 independently reviewed `fa6433c11fdd76803e54a9fcd2b2566ce7c4de53`
and requested five blocking fixes. Executed identity was verified from child session
model/effort events: `openai-codex/gpt-6.1-terra`, high (shared provider).

1. Spaced ranges/episode markers hidden in metadata: reject tags, with release and
   descriptor regression coverage.
2. Resume depending on fresh search: transactionally cache sanitized descriptors;
   explicit resume uses recorded identity, revalidates policy/paths/hash and requires
   new approval, but does not rediscover or gate on stale seed counts.
3. Full-size free-space check on resume: credit only validated payload allocation
   (`min(st_blocks*512, st_size, expected_size)`), never sparse logical length.
4. Descriptor group mismatch without group preferences: always compare selected
   group/resolution identity with the descriptor, independently of preference filters.
5. Contiguous season/episode search mismatch: query supported separated and S02E03
   forms and merge/deduplicate provider results.

New tests cover all five. The real torrent harness now includes an interrupted partial
transfer, offline recorded-job resume and a constrained free-space value below the
full torrent requirement. **Round-1 repair validation passed: 51 unit tests**, no skips;
real CLI/aria2 transfer **1,408,000 bytes, 86 verified pieces**, SHA-256
`d57edf4cead28f476e667b9c2b1b438df65b513123312cbdb7d7fda60cd7da52`.
The interrupted transfer had 114,688 allocated bytes; explicit resume completed with
Nyaa fetch forbidden and only 68,403,200 mocked free bytes (less than full size + reserve).
The complete evidence replaces the earlier small fixture run in `validation-termux.txt`.
The first new run exposed a process-stat test race when the killed child disappeared;
handled as successful termination. Enlarging the fixture also required updating its
own descriptor inspection budget. Both harness fixes were rerun successfully.
Terra round 2 reviewed `2f8a1e171c156c149960c7dcde7831e043393c08` and found
remaining explicit episode-list/compound-marker gaps plus repeated-interrupt cleanup.
Both are repaired: list/compound markers now fail closed; cleanup ignores further
SIGINT/SIGTERM until the child is terminated/reaped and restores handlers afterward.
The operation lock now releases by FD close, never explicit shared-description unlock,
so an inheriting child retains exclusion even if supervision unwinds unexpectedly.
Added a real slow-child/repeated-SIGINT/lock-retention test plus injected-interrupt
coverage. **Final validation: 53 unit tests passed with no skips**, including the real
slow-child repeated-interrupt case; original and offline-resumed 1,408,000-byte torrent
transfers both passed with the same SHA-256/86 pieces. See `validation-termux.txt`.
Terra round 3 reviewed `55279e72381ea751e723d5e3693d662193b5586f` and found
that the leading group bracket escaped metadata checks. The ambiguity scan now covers
**all** brackets in the original title, including the leading tag; added parser, actual
RSS and descriptor regressions for leading lists, compound markers and spaced ranges.
Post-round-3 validation passed: all 53 tests (no skips), real original transfer,
interrupted/offline resumed transfer, live metadata request, and diff hygiene. Latest
results are in `validation-termux.txt`.
Terra round 4 reviewed `c5a5cddce803028c14fc15a878884044e65a4448` and found
version-suffixed range endpoints bypassed a word boundary. Tag checks now normalize
version suffixes and do not require word boundaries for range/list endpoints; Unicode
dash variants are also refused. All tag positions have versioned range/list parser,
RSS and descriptor regressions. Refreshed validation passed: 53 tests, both real torrent
paths and provider metadata request; same 1,408,000-byte SHA-256 receipt.
Terra round 5 reviewed `29b12a8604b5d904939bb828503e1f3e8bab13bd` and found
other Unicode dash/minus variants. All Unicode Pd characters and the mathematical minus
are now normalized. A separator-independent numeric-tag check also rejects unknown
punctuation/prose joining two episode-looking numbers, with narrow dimension/frame-rate/
audio exemptions and positive codec regressions. Parser/RSS/descriptor tests cover the
reported Unicode variants and unknown separators. Final validation passed again:
53 tests without skips, both real torrent paths, live Nyaa metadata and diff hygiene.
The exact evidence is committed in `validation-termux.txt`.
Terra round 6 reviewed `48144cde0e3264194fb371c8a8598413d8bc0fc5` and found
numeric prose adjacency (`03of04`) bypassed word boundaries. The conservative numeric
check now counts every digit run after explicitly stripping supported numeric codec,
resolution, dimension, frame-rate and audio forms, with a narrow standalone CRC case.
Added adjacent-prose regressions in every tag position and positive CRC/codec parser,
RSS and descriptor coverage. Positive codec coverage caught a redundant raw-number
check that rejected AC3/channel/bit-depth metadata; removed in favor of the normalized
numeric gate. Refreshed validation passed: **54 tests, no skips**, real original and
offline-resumed torrents, live metadata and diff hygiene.
Terra round 7 reviewed `3b064b60f2f81a74b7722b7ce3defff22137b5fd` and found
unbounded codec substitutions could concatenate surrounding episode digits (`03x26404`).
Recognized codec/quality forms now require token boundaries and every recognized span
is replaced with whitespace, never concatenated. Remaining unexplained standalone
episode numbers and season markers fail closed too. Added regression cases across
all tag positions and RSS/descriptor paths. Refreshed validation passed: 54 tests
without skips, original and interrupted/offline-resumed torrents, live Nyaa metadata
and diff hygiene.
Terra round 8 reviewed `bedab91c289fbbe7d8d679bbef74a03df8d8496f` and found
adjacent spelled-out season labels (`Season02`). Numeric metadata now fully fails closed:
ANY digit remaining after explicit recognized metadata classification is refused, including
unknown numeric group tags. Supported standalone years/versions/Hi10P are explicit;
spelled-out season labels are also directly rejected. Added Season/Saison/volume cases
and documented the deliberate unsupported-naming limitation. Final validation passed:
54 tests/no skips, both real original and offline-resume torrent paths, live metadata
and diff hygiene. Latest results committed; exact-HEAD independent approval pending.

## Final review, issue transfer and outstanding scope
Round 9 Terra approval was clean for exact HEAD `8c1554b5a8e4625e3290540a0e7df52cae262f14`;
its read-only limits are explicit in the PR approval record. Sol separately verified
runtime, live checks/protection and exact remote HEAD before normal protected merge.

After verified merge, **11** Kura-relevant open issues were native-transferred (not
recreated), with original descriptions/history, native transfer events and old URL
redirects verified. GitHub changed global IDs and briefly returned stale mutation reads;
the verifier stopped, recovered existing moves without replay, and verified all mappings.
First-issue pre-transfer comment-count retention is an explicit ledger limitation.
References were repaired and read back. No PRs or unrelated closed issues were moved.
See [issue-transfer-map.md](issue-transfer-map.md) and [JSON ledger](issue-transfer-map.json).
MVP legacy #228 is now Kura #12, closed only after acceptance reconciliation; the ten
other design/core issues remain open. Legacy source has zero open issues and is not archived.
The later greenfield/Sol mission supersedes old in-place/Flash assumptions without
silently migrating personal configuration. Future tracks are in [roadmap.md](roadmap.md).

No unresolved feature-review blockers remain. Known limitations remain those documented
above: conservative naming, category-level subtitle claims, conditional shared storage,
foreground lifecycle and no guarantee of public swarm availability. Future integrations
and destructive automation are not implemented or authorized by the roadmap.
