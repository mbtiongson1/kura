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

Name variants. Folder and AniList titles are compared after one normalisation: case,
compatibility forms, accents on Latin letters, every apostrophe variant dropped, `×` read
as `x` and `&` as "and". A title that differs only in spacing or punctuation
(`Tatte Iru` / `Tatteiru`, `To Aru` / `Toaru`) is an exact match too, ranked just below a
word-for-word one, so a word-for-word title wins a tie. Different spellings of a word
(`Russia-go` / `Rossiya-go`) are still not matched: use `identity pin`. A folder name that is
mojibake (UTF-8 read as cp1252 or latin-1, e.g. `ChÃ¤oS`) is repaired in memory for matching,
only when the repair round-trips cleanly and has fewer marker characters; nothing on disk is
renamed. A loose video file at a root has its episode marker removed first.

File names as a second opinion. When the folder name matches nothing, the series name and
season are read from the video files inside (the release-name parser; `Extras`/`NC`
folders and OP/ED/PV-style files are ignored). They are used only when every video file
yields a series name and all agree on name and season, and a season the folder marks does
not contradict them; any disagreement keeps the folder result. The name goes through the
same offline matching, then (after the folder name) the alias pass, which costs one more
lookup from the budget; the `query` column records the name that matched. Every title has a planned query sequence: the folder name's queries (the season variant
`<title> Season N` first, then the plain title), then the file-name fallback's queries. A
negative or undecided alias row records the plan and how much of it is finished in `query`,
for example `v2:1/3:Foo Season 2@2 | Foo@2 | Bar@-` (`@` is the season the returned page is
filtered by, `-` for none; a plan too long for the column is replaced by its hash). When the
budget ends part-way, the finished queries are kept and the next run resumes at the first one
not yet asked, so even `--max-lookups 1` gets there. A recent row is trusted only while the plan
is exactly the same (same queries, order and seasons): new files, a different file-name query or a
changed season marker look the title up again without `--refresh`, and a row in any other format
(written by an earlier version) is looked up again. A query AniList would refuse (over 200 characters) is skipped without being charged; the
other steps still run. A resolved row's `query` is the query that
matched.

Root entries that are not titles. A loose file that is not a video (poster or logo images,
`.nfo`, dotfiles, `*.ffs_db`) and a folder that holds files but no video (a `fonts` folder)
are skipped by `identity sync` (reported as `non_media`, not counted as unresolved; an old
non-owner row for one is dropped), by `identity show`'s `counts`, `unrecorded` and `titles` (a non-media entry that still has a row, such as an owner pin, is listed under `non_media` instead), by the retain check's
`unidentified_titles` and by the audit's `identity_unknown` flag. They get no identity, so a
retain plan still refuses to touch them (`identity_unknown`), and an owner row for one is kept.

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

### Pinning the parts of a title

A pins line may name one part: `<ID>@<selector><TAB><title key>`. Selectors: `auto` (these IDs are the
parts; Kura assigns files by the rules above), `season:N`, `dir:PATH` and `file:PATH` (relative to the
title), `ep:A-B` (absolute episode range, for when AniList counts disagree with the release),
`rest` (every file no other selector claims) and `all` (the whole title; only alone).

```
20464@auto	main/Example Show
20992@auto	main/Example Show
101338@dir:Season 2	main/Other Show
104578@file:Movies/Some Movie.mkv	main/Other Show
21827@rest	main/Other Show
```

A title may repeat only with selector lines (a plain line beside them, a repeated selector, `all` beside
others and `not-anime@...` are refused with the line number). Files without selector lines keep the same
digest as before. Applying the file writes the parts (`title_parts`, source `owner`, ordinal = line order)
and a **safety-net identity row**: pinned to the first part, the others as candidates, `matched` =
`parts:N`. Any reader that knows nothing about parts therefore still sees every part and fails closed.
As with plain pins, dropping a line keeps the part (`kept_parts`); a plain line for a title that has parts
replaces them all (`replaced_parts`).

`kura identity pin --title KEY --anilist-id N --part SELECTOR` adds or replaces one part (a title with a plain
owner decision must be cleared first); `--clear --part SELECTOR` removes one part and the last removal
drops the safety-net row (`--clear --part auto` is refused: several IDs share `auto`); plain `--clear`, a plain pin or
`--not-anime` remove every part, owner and derived.
`kura identity parts --library L --title KEY` shows the parts and, for every video file, the part it
belongs to with the rule or the reason it is unassigned (bounded to 2,000 files). `identity show` lists
`mode` and `parts` for parted titles. `manifest prune` drops derived parts like derived identities and
keeps owner parts until the library empties. Part IDs are included in `anilist media` and SeaDex ID
selection so their metadata is fetched.

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

## Multi-part titles

One library folder is one Kura title, but it may hold several AniList entries (seasons, cours,
split-cour parts, movies). Such a title has **parts**: rows in `title_parts` (state schema 5), either
`owner` parts (written by a pins file or `identity pin`, whole part set, nothing else consulted)
or `relations` parts (derived from AniList relations, over-protective on purpose). A title with no
part rows behaves exactly as described above.

Which file belongs to which part is never stored. `titleparts.assign` recomputes it from the files,
the parts and the cached AniList media each time: owner selectors first (`file:` over `dir:` over
`season:N` over `ep:A-B` over `rest`), then season directories and file markers (`Season 2/`, `S02E01`),
then a directory that names exactly one part, then cumulative numbering (1..M over consecutive
finished parts, which must end exactly on a season boundary), then movie file names. Anything doubtful
stays unassigned; Kura never guesses "the third TV entry is season 3" (a season must be carried by the
part's own titles, or the owner pins it).

### Discovering parts automatically

Derived (`relations`) parts are written by two offline-friendly steps. `kura anilist relations
--library L` (public, budgeted, see `docs/anilist.md`) caches the SEQUEL/PREQUEL franchise of every
title Kura resolved itself. `kura identity discover --library L [--max-age-d 90] [--dry-run]` then
reads that cache; it uses no network and opens no library root. For each media title it derives the
franchise around the resolved ID and writes it as the title's parts (selector `auto`, `chain_pos` along
the TV main line, movies and other non-TV entries as side entries) only when all of this holds:

- the identity is `resolved`: an owner pin, a `not_anime` decision and any owner parts always win and
  are never touched;
- every entry of the franchise has a successful relation fetch no older than `--max-age-d` and cached
  media metadata, so a partly crawled franchise yields nothing (`relations_incomplete`,
  `relations_stale`, `media_unknown`);
- the TV entries form one unbranched chain (`chain_ambiguous` for a branch, a cycle or entries linked
  only through side entries), the resolved ID is on it, and the franchise has at most 50 entries;
- `titleparts.assign` places the title's own files into at least two of the parts
  (`files_do_not_span` otherwise: a folder that holds one season keeps behaving as a single title).

The report lists `written`, `unchanged`, `removed`, a count of `skipped` titles per reason and the
titles behind the reasons that deserve a look. A rerun with no change writes nothing. Derived parts
whose title is no longer a resolved media title, or whose identity moved, are removed; if a title only
stops qualifying (for example the cache is stale again) its parts are kept while the identity is still
the one they were derived from, because derived parts only ever add protection. Discovery decides
which entries belong to a title, never which file belongs to which part: that stays the job of
`assign` and fails closed (`parts_incomplete`).

Under `--retain` a parted title is refused with the existing tokens plus one new one:

| reason | meaning for a parted title |
| --- | --- |
| `identity_unknown` | the parts no longer match the recorded identity (`parts_stale`): a derived part set whose seed moved or whose identity row is unreadable, or an owner part set that the pinned identity row (first part pinned, the others as candidates) no longer agrees with, for example after a later `identity pin` or `--clear` |
| `retained` | owner mode: any declared part is listed; derived mode: a part that owns a file is listed |
| `retain_possible` | a recorded candidate is listed, or (derived mode) a related entry that owns no file is |
| `parts_incomplete` | a non-extra video file belongs to no part, so its owner is unknown |

Moves stay whole titles: if any part is protected nothing of the title is proposed. `retain check`
adds `parts` to `retained_titles`, `via` (`candidate` or `relative`) to `possible`, and
`incomplete_titles`. This is stricter than before for folders that span seasons: a title whose files
cannot all be placed is refused where it might once have been proposed.

## Limits

Matching can only weigh the titles Kura knows: the synced list, and whatever AniList
returns for an alias lookup. A retained series that is on neither can lose a folder to a
similarly named entry that is. Identities follow the folder name, as pins do: if a different
title is later placed under the same name, run `identity sync --refresh` (or `identity pin`).
Two guards help: every resolved row records the weaker
candidates it passed over (`retain_possible`), and `retain check` lists the retain IDs no
folder is bound to (`unmatched_ids`). Review that list before approving a plan, and
`identity pin` any folder the matcher got wrong.
