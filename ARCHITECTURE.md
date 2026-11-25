# Kura target architecture

Status: approved bootstrap direction; capabilities below are a roadmap unless explicitly
marked MVP. Kura is a new product, not a rename or wholesale migration of
[MPV AniList Sync](https://github.com/mbtiongson1/MPV-anilist-sync).

## Principles and deployment
One headless core, multiple thin clients. Deterministic services enforce policy;
agents express intent, never supply executable commands. Begin as one Python process
with local SQLite and files on native Android Termux. No mandatory server, account,
cloud, queue broker, containers, microservices or distributed control plane.
Python is an MVP decision for portability, not a requirement for future clients.

## Module boundaries
- **Domain/models**: typed queries, release identity, preferences, proposals, receipts,
  lifecycle transitions and structured errors. No network, UI or subprocesses.
- **Core/services**: discovery, candidate validation/ranking, acquisition planning,
  authorization, idempotency and completion verification. Dependency-injected ports.
- **Provider adapters**: search and bounded descriptor retrieval. MVP Nyaa RSS only.
  Results are untrusted data; a provider cannot authorize a download or write paths.
- **Identity/alias adapter**: optional AniList GraphQL lookup (one bounded, unauthenticated
  request, no credentials, redirects or proxy inheritance) maps a user alias to bounded
  candidate spellings. It is discovery-only: it widens which titles Nyaa is queried for,
  never preferences, release choice or approval. Matching is exact or whole-word subset;
  equal-best candidates are refused. Results are cached with a TTL in local state.
- **AniList OAuth adapter** (approved 2026-11-25; reads, plus one owner-invoked write since #45;
  `anilist_auth.py`, `anilist_list.py`, `anilist_update.py`, `cli_anilist.py`): the user authorizes through AniList's PIN page and gives Kura the access
  token on a hidden prompt or stdin (`kura anilist login --token-stdin`); there is no token
  argument, option or environment variable. The token lives only in `anilist-auth.json` in the
  private state directory (dir 0700, file 0600, written atomically, symlinks refused, loose
  modes refused on use) and is sent only as `Authorization: Bearer` to the pinned
  `graphql.anilist.co` endpoint (same transport as above: no redirects, no proxy, public DNS),
  and only on the requests that need it (the user's own list, and an update's read and save). It never appears in output,
  errors, logs or the database; commands show a short sha256 fingerprint. Tokens last about a
  year with no refresh: expiry is read from the JWT `exp` claim (else issue date plus 365
  days), warned about 30 days ahead and refused after. The adapter reads lists, favourites and
  media metadata into private state; each user's snapshot is replaced in one transaction after
  every chunk arrived and a failure keeps the previous one. Every request shares the
  cross-process AniList rate budget. The one write is `kura anilist update` (issue #45): a user
  command that changes one entry of the token owner's list (progress, status, score) after an
  authenticated read, resolving titles without guessing, never lowering progress unless asked,
  sending only the changed fields and writing the saved entry back into the snapshot. There is no
  automatic write and no outbox yet.
- **Library handover** (read-only): `inventory`/`rofs` read a library as metadata only,
  `torrentmeta`/`protect` parse descriptors and classify protection, `match` links torrents
  to titles (by name, else by file name and size; piece verification is the proof), `pieceverify` hashes local files against the piece list, and `manifest`
  records and classifies the result (`manifest_store` is its repository). `cli_handover`
  is the thin command layer; its inventory/inspect/verify commands never open state.
  None of it writes to a library or descriptor directory.
- **Library audit** (read-only, offline; `audit.py`, `names.py`, `policy.py`, `cli_audit.py`):
  joins state tables written by earlier steps (inventory, links, title identities, probe results,
  the SeaDex mirror, the AniList list snapshot) with the owner's retain list and policy overrides
  into one report of flags and suggestions. It imports no network client and opens no library
  root; the media-reading step is `probe`, the networked steps are `seadex`, `anilist` and
  `identity`. `names.py` is a tolerant tokenizer used only for scoring, never for acquisition
  identity; `policy.py` holds the archival/efficient rules and the efficient size band. The only
  write is a row in `audit_reports`; every output is a proposal.
- **Downloader adapter**: local aria2c argument-array subprocess, no shell or RPC.
  Validated torrent metadata supplies exact expected paths, size and piece hashes.
- **Local repository**: versioned preferences and acquisition/audit records in SQLite;
  media/temporary descriptors outside source checkout in private application storage.
- **Interfaces**: argparse CLI with versioned JSON envelopes first; future typed
  local API/tool adapters must call these same services, not duplicate policy.

Dependency direction: interfaces -> services -> models/ports; adapters implement ports.
Do not introduce a plugin framework until a second real integration needs it.

## MVP flow
Title + single episode -> preferences + overrides -> Nyaa discovery -> conservative
parse/filter/rank -> fail closed on missing identity, batches, wrong episode/season,
or ambiguity -> bounded torrent metadata inspection -> dry-run proposal -> explicit
approval -> disk/path checks + idempotency reservation -> foreground aria2 ->
verify exit, exact files, sizes and piece hashes -> durable truthful receipt.
No seasonal automation, daemon or autonomous cleanup. AniList access is public
alias lookup, the approved OAuth list reader above, and the user-invoked `anilist update`;
nothing writes to AniList on its own.
RSS hints never replace authoritative descriptor lengths/hashes.
Interrupted jobs retain partial files; retries require explicit authorization.

## Typed CLI/API/agent contracts
MVP commands: search, download (dry-run/confirmation), downloads status, prefs show/set.
Use a stable v1 JSON envelope: schema_version, success, phase, data, error.
Errors have machine codes and actionable messages; nonzero exit means no claimed success.
Typed Python dataclasses and a Provider protocol form the in-process API.
Future tools: search_releases, propose_acquisition, approve_acquisition, inspect_jobs;
later observation, inventory, organize preview, archive preview and reconcile tools.
Tool schemas must bind title/episode, destination scope, byte budget and actor.
Pi may invoke bounded CLI arguments with JSON results. Gemini integration is conditional
on its supported tool interfaces; do not assume arbitrary app tool attachment.
No generic shell, SQL, filesystem or downloader administration tools.
Future HTTP/API requires explicit opt-in, authentication, authorization, loopback/private
binding and replay resistance. No HTTP server is part of MVP.

## Persistence and identity
Local SQLite is authoritative offline: schema version, validated preferences, release
infohash, normalized title/episode, destination, state, timestamps and evidence.
Use transactions, uniqueness constraints and a process lock to prevent races.
Domain identity must eventually separate series/season/episode from filenames and source
IDs; unresolved identity is not grounds for destructive action. Acquire receipts and
future observation events carry provenance and confidence, not only mutable snapshots.
Do not copy legacy credentials or private torrent archives. Import later by explicit,
validated, reversible migration, never by reading arbitrary old configuration.

## Optional synchronization
Supabase is optional future metadata/event synchronization, never a media transport or
availability dependency. Prefer local transactions plus durable outbox, stable event IDs,
idempotent remote ingestion and explicit conflict rules (facts vs preferences).
Offline changes remain usable; reconcile on reconnect, with bounded retries and visible
conflicts. Device-scoped auth/RLS; no service-role keys on clients. Deletion tombstones,
export/backup, encryption and revocation need a separate design before implementation.
Compare local-only and hybrid deployments with actual infrastructure before enabling.

## Integrations (not implemented)
- **Android**: native Termux foreground first, private storage by default. Shared storage
  is explicit and subject to Android permission/mount constraints. Process death is normal.
- **Unraid**: read-only inventory of approved shares first; discover the actual organizer
  and layout. Never compete with an existing mover or expose Docker/admin sockets.
- **Plex**: capability-checked library/playback/observation adapter; subscription-dependent
  webhooks need verification. Prefer existing streaming over redundant copies.
- **Tailscale**: private connectivity and device identity, not a substitute for application
  authorization. No public management plane.
- **AniList**: OAuth adapter implemented (see Identity adapters), with a user-invoked single-entry
  update; confirmed identity, automatic scrobbling and a rate-limited reconciliation/outbox remain future, separately approved work. Playback evidence is distinct from inferred watched progress.
- **Players/other observations**: normalized durable watch events with source/confidence,
  deduplication, manual correction and season-aware identity resolution.

## Safe lifecycle, archive and reacquisition
Handover pipeline (implemented as read-only proposal data): a title is *reacquirable*
only when it is on the server (all file bytes verified against torrent piece hashes, no
unlisted extra files) and its torrent is public, unprotected and re-downloadable. This is
the "photos cleared once safely in the cloud" model: Kura proposes, the owner purges.
Statuses are recomputed from stored evidence (size, mtime, device, inode per file), so a
changed file turns a title `stale`. Private or unparsable material is protected, never
eligible. Quarantine planning and swarm-health checks build on this in later stages.
Candidate discovery (`manifest resolve`) is separate and untrusted by design: it only fetches
public torrent *descriptors* for unmatched titles into a Kura-owned directory, through a
verification-purpose search that is distinct from the single-episode acquisition path (same
hardened transport, paced, budgeted, resumable). It decides nothing; the piece-hash
verification in `manifest build` is the only thing that can make a title eligible.
Target lifecycle: discovered -> proposed -> acquiring -> staged -> ready -> watched ->
archive_candidate -> quarantined -> reclaimed -> reacquirable.
MVP stops at verified acquisition. Future organization uses approved roots, collision
checks, preview diffs, content fingerprints and rollback journals. Archive is metadata
first: lawful source pointers, hashes and provenance, not a guarantee of future availability.
Cleanup requires pin/protect flags, space budgets, explicit policy, quarantine/grace period,
verified undo and a separately approved purge. Never unrestricted recursive scans/deletes.
Reacquisition is a new policy/rights/space decision, not automatic resurrection.

## Security boundaries and audit
Provider text and torrent metadata are hostile. Restrict HTTPS provider hosts and paths,
reject redirects, bound network reads and XML/bencode complexity; validate infohash,
single-episode contents and exact byte budget before aria2 sees metadata.
Reject traversal, symlinks, special files and destinations outside approved roots.
MVP grants only private downloads and optionally the user's Android shared Download/Kura
root; no arbitrary filesystem writes. Local same-UID malicious processes are outside
the sandbox threat model; no claim of OS isolation from other Termux programs.
No shell interpolation, inherited aria2 config/hooks, exposed RPC or secrets in output.
Torrent peer traffic inherently exposes an IP address and contacts untrusted peers;
document this and keep a patched aria2. No persistent seeding policy is introduced.
Approval tiers: reads allowed; acquisition needs explicit per-command confirmation;
future remote account writes, file moves and deletion need scoped, revocable grants.
Audit records include proposal identity, applied preferences, authorization mode, state,
errors and completion evidence; no credentials. Limits: bounded metadata, total bytes,
free-space reserve, duration and concurrency. Failure/interrupt is never completion.
Protect main with PRs, no force/deletion, required lightweight tests; solo maintenance
does not require another GitHub account approval. Independent Terra review gates release.

## Future GUI decision
A GUI is an independent client of typed core contracts, not the domain owner.
Rust (for example a native shell) is a candidate, **not an approved implementation
decision**. Compare platform support, Android constraints, packaging and IPC before an ADR.
No Rust rewrite or GUI scaffolding in MVP. CLI remains usable without GUI dependencies.

## Evolution and acceptance
1. Bootstrap architecture/security; protected main.
2. One permissioned Termux acquisition, conservative Nyaa search, local preferences,
   duplicate protection, tests and independent review.
3. Design tracks: event ledger/identity, infrastructure inventory, lawful subscriptions,
   storage lifecycle, private delivery, bounded tools and optional cloud sync.
Each is a small PR with fixtures and failure evidence. No feature inferred from this
roadmap is authorized merely by being described.
