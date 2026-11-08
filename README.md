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
