# SeaDex mirror

`kura seadex` keeps a bounded, read-only copy of SeaDex's curated "best release" picks in
Kura's private state, so a later audit can compare a library against them without any
network access. SeaDex data is untrusted metadata: it never selects, authorizes or starts a
download.

## Commands

| Command | Network | Writes |
|---|---|---|
| `kura seadex sync [--full] [--files-for-library NAME] [--max-requests N]` | SeaDex only | private state |
| `kura seadex lookup (--anilist-id N \| --infohash H)` | none | none |
| `kura seadex status` | none | none |

`sync` is a full pull on the first run, with `--full`, or when the last full pull is more
than 7 days old. Otherwise it is incremental: records whose `updated` is at or after the
stored per-collection cursor are upserted. A full pull replaces every `seadex_*` row in one
transaction, so records deleted upstream disappear; an incremental pull cannot see deletions,
which is why a full pull is forced weekly. `--max-requests` defaults to 60 (range 1..500).

`--files-for-library NAME` then fetches the file lists of the public picks for the
AniList IDs of that library (resolved or pinned rows of `title_identities`) and the viewer's
list snapshot (`anilist_list_entries`), 40 records per request, until done or the request
budget ends (`files_pending` says how many remain). Both source tables may be empty; the
step then fetches nothing. Stored per torrent: up to 2000 `[name, bytes]` pairs, `total_bytes`,
`video_files` and `median_video_bytes` (video extensions only, excluding extras: names with an
`NCOP`/`NCED`/`OP`/`ED`/`PV`/`CM`/`Preview`/`Menu`/`Trailer` token and anything under an
`Extras`, `NC` or `Bonus` directory). File details are kept across a full pull while the
record's `updated` is unchanged and dropped when it changes.

## Output

`sync`: `{"mode","entries":{"upserted","total"},"torrents":{"upserted","total","public"},
"files_fetched","skipped_malformed","requests","cursor","synced_at"}` plus `files_pending`
when `--files-for-library` is given. `cursor` is `{"entries": ts, "torrents": ts}`.

`lookup --anilist-id`: `{"anilist_id","entry":{"id","incomplete","notes","comparison","updated"}|null,
"torrents":[{"release_group","public","is_best","dual_audio","tags","infohash","nyaa_id",
"total_bytes","video_files","median_video_bytes"}],"synced_at","stale"}`. `infohash` is present
only for public picks. `stale` is true when the last successful sync is older than 7 days or
there never was one.

`lookup --infohash` returns `{"infohash","matches":[<the object above, one per entry listing it>],
"synced_at","stale"}`. Only public infohashes are stored, so only they can be found.

## Public versus private picks

A torrent is `public` only when its tracker is Nyaa (any case), its `infoHash` is 40
hexadecimal characters and its `url` is exactly an `https://nyaa.si/view/<digits>` link. Every
other pick (private trackers publish `<redacted>`, relative URLs) is stored with `public=0`,
a NULL infohash and no `nyaa_id`: Kura treats "SeaDex lists it only on a private tracker" as
"no public pick".

## Transport and untrusted input

* https only, host exactly `seadex.moe`, paths `/api/collections/{entries,torrents}/records`
  only. The query is built by code; filters accept only integer AniList IDs, 40-hex
  infohashes, record ids `[a-z0-9]{1,32}` and `YYYY-MM-DD HH:MM:SS[.fff]Z` timestamps.
* No redirects, no proxy inheritance, DNS must resolve to public addresses, responses are
  capped at 8 MiB each (64 MiB per run) and at most 100 pages of 500 per collection; every page
  is parsed and dropped before the next is read. Cursors are stored with a fixed nine-digit
  fraction so their string order is time order, and never pass the run's start minus 5 minutes
  (records changing mid-run, or claiming a future date, cannot hide later updates).
* Every request goes through the shared `seadex` limiter (1 request per second across all
  Kura processes). HTTP 429 records a penalty honouring `Retry-After`. User agent carries the
  repository URL. No credentials are sent.
* Text is stripped of control and format characters (newlines and tabs become spaces) and
  bounded (notes, theoretical best: 4000 characters; tags: 20 x 40; comparison links: https
  only, at most 20). A malformed record is skipped and counted in `skipped_malformed`.
* A listing whose `totalItems` changes between pages, or whose item count does not match it,
  is refused (`seadex_unstable`): offset paging could otherwise skip records. A file list
  longer than 20000 entries is treated as malformed rather than summarized partially.
* A failed run keeps the previous rows, records `status=failed` in `sync_state` and returns
  a `success=false` envelope (`seadex_unavailable`, `seadex_invalid`, `rate_limited`,
  `request_budget`, `seadex_unstable`, `sync_conflict`). A failure in the optional files step leaves the committed listing as
  `ok` and reports `listing_committed`.
