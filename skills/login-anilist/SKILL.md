---
name: login-anilist
description: Log a Kura install into AniList so Kura can read the user's list and update it (progress, status, score). Use when the user says "/login-anilist", "log in to AniList", "connect AniList", or when a kura anilist command fails with anilist_not_logged_in, anilist_token_expired or anilist_auth_invalid. The user pastes the token into their own terminal; it never passes through the agent.
---

# Log Kura into AniList

Kura keeps one AniList login per state directory (`~/.local/share/kura` for the user who runs
it). Each machine or container that runs Kura needs its own login. The login is an AniList
access token: it lasts about a year and grants full access to the account, so it is handled
like a password.

## Rules

- **Never handle the token yourself.** Do not ask the user to paste it into the chat, do not
  put it in a command line, a heredoc, a file you write or an environment variable, and do not
  run `kura anilist login --token-stdin` with it. The user types it into Kura's hidden prompt in
  their own terminal.
- If the user pastes a token into the chat anyway: do not repeat it, store it or use it. Tell
  them to revoke Kura under AniList Settings > Apps, then log in again with the steps below.
- Never print `~/.local/share/kura/anilist-auth.json`. `kura anilist whoami` shows only a
  fingerprint, which is safe to show.

## Steps

1. **Find Kura.** Use `kura` if it is on `PATH`. Otherwise, from a checkout of this repository,
   install it with `python -m pip install --no-deps .` (or `pipx install .`), or run it as
   `python -m kura` with `src` on `PYTHONPATH`. Use the same command in every step below.
2. **Check the current login.** Run `kura anilist whoami --check` (one request). If it reports
   `logged_in: true` and `checked: true` with no `anilist_token_expired` warning, the user is
   already logged in: say which account (`viewer.name`) and stop.
3. **Get the approval link.** Run `kura anilist login --print-url` and give the user the
   `authorize_url` from its output. It touches no state.
4. **Hand over to the user.** Tell them, in this order:
   1. open the link, sign in to AniList if asked, and approve Kura;
   2. copy the long token AniList shows;
   3. in their **own terminal window** (not this chat, and not through the agent's shell),
      run `kura anilist login`, paste the token at the hidden `AniList token (hidden):`
      prompt and press Enter. Nothing appears while pasting, which is expected;
   4. tell you when it is done.

   On a machine without a terminal the user can reach (a server or container), they run the
   same command in an SSH session or `docker exec -it` shell there.

   If the prompt seems stuck after Enter (Kura before the fix for long tokens, on macOS whose
   terminal caps one typed line at 1024 bytes), the user presses Ctrl-C, copies the token again
   and runs `pbpaste | kura anilist login --token-stdin` (macOS; on Linux with a clipboard tool,
   `xclip -o -selection clipboard |` or `wl-paste |` in place of `pbpaste |`), then clears the
   clipboard (`pbcopy < /dev/null` on macOS). The token goes from the clipboard straight into
   Kura: not on screen, not in shell history, not through the agent.
5. **Verify.** Run `kura anilist whoami --check` and report the account name and
   `expires_at`. If it shows `anilist_token_expires_soon`, remind them to repeat this skill
   before then.

## Errors

| Code | Meaning | What to do |
| --- | --- | --- |
| `anilist_not_logged_in` | no login in this state directory | run this skill |
| `anilist_token_expired` | the token is past its expiry | run this skill again |
| `anilist_auth_invalid` | AniList rejected the token (revoked or wrong account) | run this skill again |
| `anilist_token_invalid` | the stored token file holds something that is not a token | run this skill again |
| `anilist_token_insecure` | the token file or its directory has loose permissions or is a symlink | the user fixes the permissions (file 0600, directory 0700) or logs in again; never print the file |
| `anilist_login_needs_tty` | `kura anilist login` was run without a terminal | the user runs it in their own terminal window |
| `anilist_rate_limited` | AniList's rate limit | wait the `retry_after` seconds; do not loop |

## After the login

The user can now say things like "mark episode 8 of <show> watched". Run
`kura anilist update --title "<show>" --progress 8 --dry-run` first and show the planned
`change` and the matched title (`resolved.matched`). If it is the right show, run it again
without `--dry-run`. On `anilist_title_ambiguous` or `anilist_title_unknown`, show the
`candidates`, let the user pick, and use `--id N`; add `--search` only for a show that is not
stored yet. See [docs/anilist.md](../../docs/anilist.md) for every option and guard.

To log out: `kura anilist logout`, then revoke Kura under AniList Settings > Apps.
