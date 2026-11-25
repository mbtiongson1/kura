# Retain lists and title identity

A retain list names AniList IDs whose library copies Kura must never propose for
quarantine, even when a verified torrent exists. Retaining needs to know which AniList
entry each library folder is, so Kura keeps a per-title identity table
(`title_identities`) next to the manifest. Nothing here opens, moves or deletes a library
file: both features read and write only Kura's private state database.

## Retain file

```
# Kura retain list: AniList IDs whose library copies are never proposed for quarantine.
999000123   # free-text comment
```

One positive integer below 10^9 per non-comment line; `#` starts a comment; blank lines are
ignored; UTF-8; at most 10,000 lines and 1 MiB. Anything else is refused with
`invalid_policy` and the line number, so a typo cannot silently shorten the list. Duplicates
merge. The file is opened without following symlinks and must be a regular file.

The file is the only copy of the policy, like `protected.txt`. Plans and reports record
just `{"sha256", "ids"}`: the digest of the sorted ID list and how many IDs it holds.
The overrides file used by the library audit has the same format with one tag after the ID
(`<id> archival|efficient  # comment`), parsed by the same code (`kura.idlist.parse`).

## Identity

`kura identity sync --library NAME [--max-lookups 60] [--offline] [--refresh] [--title KEY]...`

Titles are matched in two passes, cheapest first.

1. Offline, against the owner's synced AniList list (`anilist_media` titles and synonyms for
   entries on the viewer's list; this snapshot is empty until the AniList list has been
   synced). The folder name, reduced by the same cleaner `manifest resolve` uses (group,
   resolution, codec, source and batch tags removed), must equal a title or be a whole-word
   subset of one. The closest match wins; a tie is `ambiguous` and the tied IDs are
   recorded. Every resolved row also records the weaker candidates it passed over, even
   behind an exact match (an unmarked folder may hold several seasons); `identity pin`
   settles such a title.
2. Alias fallback over the network, through the shared AniList rate limiter and the
   existing alias cache, at most `--max-lookups` actual requests per run. Titles left
   over are reported as `pending` and picked up by the next run. A rate-limit or network
   failure stops the run's lookups.
   `--offline` skips this pass.

Season rule. A folder marked season 2 or higher binds only to an entry whose matched title
carries that season (`Season N`, `Nth Season`, `SN`, or a trailing `N`); the season-1 entry
is never accepted for it. A folder marked season 1 never binds to an entry titled for a
later season. A folder spanning several seasons (`S01-S03`) or season 0 is `ambiguous`.
For the alias pass the season variant (`<title> Season N`) is asked first, and an answer
that fails the season check is `unresolved` with the rejected ID kept as a candidate.

Statuses: `resolved` (source `list` or `alias`), `pinned` and `not_anime` (owner),
`ambiguous`, `unresolved`, `unavailable`. Unchanged resolved rows are kept, not looked up again,
unless `--refresh`, which revalidates them: a row it cannot reconfirm (no match, failed
lookup, or an offline refresh of a row that came from the list) is replaced by the weaker
status. An offline refresh cannot judge an alias row and keeps it. Owner rows are never overwritten by a sync.

`kura identity pin --library NAME --title KEY (--anilist-id N | --not-anime | --clear)`
records the owner's decision; `--clear` removes only an owner row.
`kura identity show --library NAME [--status S]` lists what is recorded.

## Retain check and plans

`kura retain check --library NAME --retain FILE` reports which titles the list protects:
`retained_titles`, `unmatched_ids` (listed IDs no title is bound to), `possible` (titles
whose recorded candidate IDs include a listed ID) and `unidentified_titles` (titles
without a resolved or pinned identity, which includes `not_anime`).

`kura manifest plan ... --retain FILE` applies the list to the quarantine proposal. Only a
title that is otherwise eligible is tested, and it is refused with:

| reason | meaning |
| --- | --- |
| `identity_unknown` | no `resolved`/`pinned` identity row: unknown, ambiguous, unresolved, unavailable or not anime |
| `retained` | its AniList ID is on the list |
| `retain_possible` | a candidate ID recorded with its identity is on the list |

The move list is re-checked against the list before the plan is finished, so a retained
title can never appear in `moves`. The plan always carries a top-level `retain` key: the
`{"sha256", "ids"}` summary, or `null` when no list was given (`plan_version` stays 1). The
list changes the plan hash and batch folder. Without `--retain` nothing about
identities is consulted.

Because a list requires every candidate to be identified, run `identity sync` and resolve
ambiguous titles with `identity pin` first; a title that stays unidentified is simply not
proposed.
