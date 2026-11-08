# Kura bootstrap and torrent MVP handoff

## Ownership and scope
Lead/sole implementer: Sol (`openai-codex/gpt-6.1-sol`). Independent reviewer:
Terra shared (`openai-codex/gpt-6.1-terra`, high, read-only). No worker implementation.
Repository: https://github.com/mbtiongson1/kura (new, public).
Architecture-only root commit: `25f26cc` (no feature code).
Legacy baseline: `c4f15e1b508691f91c16b8081d336f5483bdf432`; working tree clean,
no application changes. Issue transfer is explicitly deferred until after merge.

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
private vulnerability reporting enabled. CodeQL default setup cannot select Python
until Python reaches the default branch (REST returned 422 while main was docs-only);
enable/verify after the feature merge. The initial secret/alert controls are active.
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
full torrent requirement. **Final revised validation passed: 51 unit tests**, no skips;
real CLI/aria2 transfer **1,408,000 bytes, 86 verified pieces**, SHA-256
`d57edf4cead28f476e667b9c2b1b438df65b513123312cbdb7d7fda60cd7da52`.
The interrupted transfer had 114,688 allocated bytes; explicit resume completed with
Nyaa fetch forbidden and only 68,403,200 mocked free bytes (less than full size + reserve).
The complete evidence replaces the earlier small fixture run in `validation-termux.txt`.
The first new run exposed a process-stat test race when the killed child disappeared;
handled as successful termination. Enlarging the fixture also required updating its
own descriptor inspection budget. Both harness fixes were rerun successfully.
Fresh exact-HEAD Terra approval remains pending.

## Review and outstanding work
Independent Terra review must inspect the latest pushed HEAD, architecture, all source,
fixture harness, tests and this report. Pending approval/green checks: do not merge.
After an approved merge: verify main controls and runtime, native-transfer the 11 open
Kura-relevant legacy issues (208, 219–228), preserve history and repair cross-references,
record mappings and reconcile MVP completion against actual acceptance evidence.
