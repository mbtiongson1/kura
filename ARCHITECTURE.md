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
No AniList authentication, seasonal automation, daemon or autonomous cleanup.
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
- **AniList**: separately approved OAuth adapter; confirmed identity, rate-limited
  reconciliation/outbox. Playback evidence is distinct from inferred watched progress.
- **Players/other observations**: normalized durable watch events with source/confidence,
  deduplication, manual correction and season-aware identity resolution.

## Safe lifecycle, archive and reacquisition
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
