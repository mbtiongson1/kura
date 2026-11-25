# AniList (read-only)

Kura reads your AniList anime list, favourites and media metadata into its private state. It
never writes to AniList. Public lists need no login; your own list (including private entries)
needs an access token.

## Log in (PIN flow)

1. Register an AniList API client once at the AniList developer settings and set its
   **Redirect URL** to `https://anilist.co/api/v2/oauth/pin`. The client ID is public. Pass it
   with `--client-id N` or `KURA_ANILIST_CLIENT_ID` (a built-in default is added once the
   project registers one).
2. `kura anilist login --print-url` prints the authorize URL
   (`https://anilist.co/api/v2/oauth/authorize?client_id=<ID>&response_type=token`). It touches
   no state. Open it, approve, and copy the token AniList shows.
3. `kura anilist login --token-stdin < file` or paste at the hidden prompt of a plain
   `kura anilist login` on a terminal. One Viewer request verifies the token before anything is
   stored.

There is no `--token` option. The token is never in argv, environment, output or logs, and is
never stored in SQLite. It lives in `anilist-auth.json` (file 0600 in a 0700 directory; symlinks
and loose permissions are refused). Commands show only `token_fingerprint` (`sha256:` plus 8 hex).

Tokens last about a year and cannot be refreshed. Kura reads `exp` from the token if present,
otherwise assumes issue date plus 365 days, warns (`anilist_token_expires_soon`) under 30 days
and refuses an expired token (`anilist_token_expired`). `kura anilist logout` deletes the file;
also revoke the app in your AniList settings.

## Commands

| Command | Requests | Notes |
| --- | --- | --- |
| `anilist login [--print-url \| --token-stdin]` | 0 or 1 | stores the token after a Viewer check |
| `anilist whoami [--check]` | 0, or 1 with `--check` | shows viewer, expiry, fingerprint |
| `anilist logout` | 0 | deletes the token file |
| `anilist list [--user NAME] [--max-age-h 12] [--refresh] [--entries] [--max-requests 10]` | 0 if cached | `--user` is anonymous (no Authorization header) |
| `anilist media (--id N ... \| --library NAME [--missing]) [--max-requests 10]` | 1 per 50 IDs | public metadata, halves a batch AniList rejects (minimum 5) |
| `anilist limits` | exactly 1 | reports `X-RateLimit-*` and the shared limiter state |

`list` fetches `MediaListCollection` in chunks of 500 (at most 20), then the favourites
(25 per page, at most 20 pages). Entries that appear in several lists are stored once; an entry
visible only through a custom list is kept. Scores are requested as `POINT_10_DECIMAL`; `0`
(unscored) is stored as NULL. The snapshot is replaced in one transaction only after every chunk
arrived; on a transient failure the previous snapshot is returned with `stale: true` and the
error code. Authentication errors, unknown or private users and bad arguments raise instead.

## Rate limits

Every request goes through the shared `anilist` limiter, so concurrent Kura processes share one
budget. AniList documents 90 requests per minute (30 while degraded), a one-minute timeout when
exceeded (`429` with `Retry-After` and `X-RateLimit-Reset`), and a separate burst limiter. Kura
starts at one request per 2 s, adopts the advertised `X-RateLimit-Limit`, waits for the reset
when `X-RateLimit-Remaining` reaches 0, persists a 429 penalty for all processes, never retries a
429 itself, and never waits more than 30 s for one request. `--max-requests` bounds each command.

Known limit (by design of the shared limiter, which spaces request *starts*): a process that starts
while another one's slow request is still in flight does not wait for its response. If that
response is a 429, the penalty is recorded when it arrives and every later start honours it; at
most the few requests started inside one request's duration (about 2 s apart) can overlap it.

## Testing the limits live (owner procedure, no bursts)

1. `kura anilist limits` and note `limit`, `remaining`, `reset`.
2. `kura anilist list --refresh` (a few requests), then `kura anilist list` again: the second
   call must report `from_cache: true` and `requests.made: 0`.
3. Record the observed `X-RateLimit-Limit` and `X-RateLimit-Remaining` in the issue. Do not loop.
