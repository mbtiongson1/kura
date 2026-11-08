# Security policy

## Supported versions
Kura is pre-release. Only the current main branch is supported; no older versions or
legacy MPV AniList Sync releases are maintained here. Update Python and aria2 through
Termux packages. There is no guarantee of unattended/background reliability.

## Report a vulnerability
Use GitHub **Security -> Advisories -> Report a vulnerability** for a private report
(https://github.com/mbtiongson1/kura/security/advisories/new).
If private reporting is unavailable, open a minimal issue asking for a private channel,
without exploit details, tokens, media names or personal data. Do not publish credentials.
Include affected commit, platform, reproduction, impact and sanitized logs.

The maintainer aims to acknowledge within 7 days and provide a triage update within
14 days; these are best-effort targets, not an SLA. Agree a coordinated disclosure
date after impact assessment; ordinarily allow up to 90 days for remediation.
Credit is opt-in. Good-faith research on your own local installation is welcome;
do not test against third-party providers or another user's infrastructure.

## Boundaries
Treat provider responses, release titles, torrent paths and agent arguments as untrusted.
No arbitrary commands, public downloader RPC or unrestricted filesystem operations.
Only acquire content you are authorized to download. Private metadata, credentials,
torrents and media must never enter Git or public issue attachments.
See ARCHITECTURE.md for policy, approval, persistence and future integration boundaries.
GitHub alerts/scanning supplement, but do not replace, tests and independent review.
