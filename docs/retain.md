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
2. Alias fallback over the network, through the shared AniList rate limiter, at most
   `--max-lookups` lookups per run. The returned page of entries is matched with the same
   rules as the offline pass (season rule, weaker candidates recorded), not just the
   resolver's best pick. A recent negative answer (7 days; 10 minutes for an outage) is not
   asked again unless `--refresh`. Titles left
   over are reported as `pending` and picked up by the next run. A rate-limit or network
   failure stops the run's lookups.
   `--offline` skips this pass.

Season rule. A folder marked season 2 or higher binds only to an entry whose matched title
carries that season (`Season N`, `Nth Season`, `SN`, or a trailing `N`); the season-1 entry
is never accepted for it. A folder marked season 1 never binds to an entry titled for a
later season (an explicit marker, or a trailing number 2 to 99 that the folder name does not
share). A folder spanning several seasons (`S01-S03`) or season 0 is `ambiguous`.
For the alias pass the season variant (`<title> Season N`) is asked first; a page whose
entries all fail the season check is `unresolved`, with the rejected IDs kept as candidates.

Statuses: `resolved` (source `list` or `alias`), `pinned` and `not_anime` (owner),
`ambiguous`, `unresolved`, `unavailable`. Unchanged resolved rows are kept, not looked up again, unless `--refresh`, which revalidates
them: it first drops every resolved row the offline pass cannot reconfirm, then tries the
network within the budget, so anything it cannot reconfirm (no match, failed lookup, spent
budget, `--offline`) ends up `unresolved` or `unavailable`, never stale. Owner rows are never overwritten by a sync.

`kura identity pin --library NAME --title KEY (--anilist-id N | --not-anime | --clear)`
records the owner's decision; `--clear` removes only an owner row.
`kura identity pins --library NAME --from FILE` applies a pins file of such decisions in one
transaction. Each line is `<AniList ID or not-anime>`, a tab, then the title key verbatim up
to the newline (spaces, tabs and `#` allowed, so only whole-line `#` comments); a bad or duplicated line refuses the
whole file. Titles missing from the manifest are reported under `missing` and skipped, a pin
that already matches is left untouched (`unchanged`), and nothing is ever cleared: dropping a
line keeps the earlier decision until `identity pin --clear`. Keeping the file next to the
retain list lets a rebuilt state get the same decisions back.
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

## Limits

Matching can only weigh the titles Kura knows: the synced list, and whatever AniList
returns for an alias lookup. A retained series that is on neither can lose a folder to a
similarly named entry that is. Identities follow the folder name, as pins do: if a different
title is later placed under the same name, run `identity sync --refresh` (or `identity pin`).
Two guards help: every resolved row records the weaker
candidates it passed over (`retain_possible`), and `retain check` lists the retain IDs no
folder is bound to (`unmatched_ids`). Review that list before approving a plan, and
`identity pin` any folder the matcher got wrong.
