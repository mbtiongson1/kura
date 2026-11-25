# Library probing (`kura probe`)

Reads the headers of Matroska/WebM and MP4/M4V files to learn what a library file actually is: encoder and its
settings, resolution, bit depth, tracks, fonts and chapters. It never reads a whole file,
touches no network and writes only Kura's private state.

```
kura probe file PATH [--max-read-kib N]
kura probe library --library NAME --root LABEL=PATH... [--sample groups|all] [--title KEY]...
                   [--max-files N] [--max-read-mib N] [--reprobe]
```

- `probe file` is stateless: an absolute path, one file opened read-only through `rofs`, no
  state database. `--max-read-kib` (default 2048, at most 16384) caps the bytes read.
- `probe library` needs a prior `manifest build` for the library: it reads the file rows that
  build recorded, never walks the tree itself, and holds the state lock for the whole run.

## Containers

The container is chosen from the first bytes of the file, never from the extension
(`probe.probe_container`): `1A 45 DF A3` is Matroska/WebM (`mkvprobe`); an ISO BMFF box type
(`ftyp`, `moov`, `mdat`, `free`, `skip` or `wide`) at offset 4 is MP4 (`mp4probe`). Anything
else is `unsupported_container` (AVI, TS, WMV, ... and files that are not video at all). Both
parsers return the same `kura.probe/1` document, so the audit reads either. The first 16 bytes are
fetched once and charged to `max_bytes` (the parser gets the rest); a parser read inside them is
answered from that copy and one running past them asks the file only for the remainder, so
`bytes_read` is exactly what the file was asked for.

## What is read (Matroska)

`mkvprobe.probe(read, size, max_bytes=2 MiB)` is pure: every byte comes through the `read(offset,
length)` callable. The CLI passes `os.pread` on a descriptor opened by `rofs`
(`O_RDONLY|O_NOFOLLOW|O_CLOEXEC`, regular files only).

1. The EBML header and the Segment's level-1 element headers (at most 12 bytes each, requested a few at a time so a read never reaches into a body). Bodies
   are skipped by size, so a multi-megabyte font attachment costs nothing.
2. Bodies are read only for: Info; Tracks (at most 1 MiB); Chapters (presence and atom count,
   at most 256 KiB); Attachments (child headers only: name, mime type and the size of the data;
   the data is never read; at most 500 files); SeekHead (at most 64 KiB); Tags (at most
   256 KiB; per-track `BPS`, `DURATION`, `NUMBER_OF_FRAMES`), and the first video block of the
   first Cluster (at most 256 KiB of its frame).
3. Elements that live behind the first Cluster (Tags, and Info/Tracks when a muxer wrote them
   late) are located through the SeekHead. A seek target inside an element the walk already
   passed (an attachment, say) is refused, so a forged SeekHead cannot redirect a read into one.
4. The first Cluster is walked block header by block header (so another track's block bodies
   are never read) for the first block of the video track number (never "the first block").
   Laced blocks are skipped with a warning. An unknown-size Cluster ends at the next level-1
   element.

Encoder settings come from the x264/x265 SEI message (type 5, user data unregistered) found in
the `hvcC` NAL arrays (`where: "codec_private"`) or the first video frame
(`where: "first_frame"`). Emulation-prevention bytes are removed before parsing, the NAL
length size comes from the codec configuration record, and every index is bounds-checked.
Video bit depth is `Colour/BitsPerChannel`, else the `hvcC` or `avcC` configuration record,
else null; the audio `BitDepth` element is never used for video.

## What is read (MP4)

`mp4probe.probe(read, size, max_bytes=2 MiB)` has the same contract and the same budgeted reader.
It walks box headers only (8 bytes, 16 with a 64-bit `largesize`; size 0 means "to the end of the
file"), so `mdat` is never read and a `moov` at the end of a non-faststart file is found by its
header alone. It descends along `moov / trak / mdia / minf / stbl` and reads a body only for:

- `mvhd` (timescale, duration), `mvex/mehd` (the duration of a fragmented file). A duration of all ones in its box version's width (32 bits in version 0, 64 in version 1) is unknown, as is zero. The movie duration is tried first, then `mehd`, then the longest track (`mdhd`) duration; one over seven days is treated as unknown with the `duration_implausible` warning. With no duration the video bitrate is null;
- per track: `tkhd` (track id, enabled flag, width and height as a fallback), `mdhd` (timescale,
  duration, language as packed ISO-639-2; `und` when it is not three letters), `elng`, `hdlr`
  (`vide`, `soun`, `sbtl`/`text`/`subt`), the first sample entry of `stsd` (at most 64 KiB), the
  first 64 `stts` entries (the entry covering the most samples gives the frame rate), the first
  `stsz` and `stco`/`co64` entries;
- the sample entry's children: `avcC`, `hvcC`, `av1C` (profile, bit depth, NAL length size, the
  `hvcC` SEI arrays) and `esds` (the AAC/MP3/AC-3 object type; `mp4a` without a readable `esds` is
  `aac`); width and height come from the visual sample entry, channels and sample rate from the
  audio one (QuickTime sound description versions 0 and 1);
- `udta/meta/ilst/(c)too` or the QuickTime `(c)too` atom (reported as `writing_app`; `muxing_app`
  is null), and chapters: a `chpl` box (its count) or a `tref/chap` reference (the referenced text
  track's sample count; that track is not listed as a subtitle);
- the first video sample (at most 256 KiB), found through the first `stco`/`co64` entry and the
  first `stsz` entry, for the x264 or x265 SEI message (`where: "first_frame"`); the `hvcC` arrays
  are tried first (`where: "codec_private"`).

Sample tables are skipped by size, so a `moov` of any size costs a handful of small reads (the
Matroska caps on Tracks and Info do not apply). Codecs reported: `h264` (`avc1`, `avc3`, Dolby
Vision `dvav`/`dva1`), `hevc` (`hvc1`, `hev1`, `dvh1`, `dvhe`), `av1`, `vp9`, `vp8`, `mpeg4`, else
`other`; audio `aac`, `mp3`, `ac3`, `eac3`, `dts`, `opus`, `flac`, `alac`, `pcm`, else `other`.
`codec_id` is the sample entry fourcc (`avc1`), not a Matroska codec id. Subtitle `format` is
`tx3g`, `wvtt`, `stpp` or `other`; `name` is null and `forced` false. An audio or subtitle track's
`default` is its `tkhd` enabled flag. There are no attachments (always 0). `bitrate_source` is always
`size_over_duration` (the whole file over the movie duration, audio included).

A fragmented file (`mvex` in `moov`, or a `moof` before any `moov`) is not walked past `moov`: the
result carries what `moov` gives plus the `fragmented_mp4` warning. The video track is the enabled
one that is not a one-sample still when any such track exists (so cover art never wins, whatever
its codec), then one with a known codec over an unknown one, with `multiple_video_tracks` when
several video tracks exist. The sample entries are parsed before the status is decided, so any
truncation warning makes the result `partial`.

## Hostile input

Nothing is allocated from a declared size: each read is `min(cap, bytes remaining)`. Nesting
stops at depth 8, a probe visits at most 20 000 elements and requests at most `max_bytes`.
Anything cut short reports `status: "partial"` with a warning token; the probe does not raise
for any content (this holds for both parsers; an unforeseen parse error becomes the `internal_error` warning, which the
tests assert never occurs). Only the caller's own `read` can raise.

## Result document (`kura.probe/1`)

```
{"schema": "kura.probe/1", "status": "ok|partial|unsupported_container|invalid",
 "container": "matroska|webm|mp4|unknown", "bytes_read", "duration_s", "muxing_app", "writing_app",
 "video": null | {"track", "codec_id", "codec", "profile", "bit_depth", "width", "height", "fps",
                  "bitrate_kbps", "bitrate_source": "tags|size_over_duration"},
 "encoder": null | {"library", "version", "where", "settings": {...}},
 "audio": [{"track", "codec", "channels", "sample_rate", "lang", "default"}],
 "subtitles": [{"track", "format": "ass|srt|pgs|vobsub|tx3g|wvtt|stpp|other", "lang", "name", "default", "forced"}],
 "attachments": {"count", "fonts", "bytes", "names": [at most 50]},
 "chapters": {"present", "count"}, "warnings": [...]}
```

Every string is printable and at most 200 characters. `video` is null when the file has no
enabled video track.

Video bitrate: a re-encoder's muxer can copy the `BPS` statistics tag unchanged from the source
stream, so the tag is trusted only when it is plausible. With the file size and the duration known,
`total_kbps = size x 8 / duration`. The tag is **stale** when any of these hold:

- it exceeds `total_kbps x 1.02` (the primary test: the video cannot outrun the whole file);
- the track's `_STATISTICS_WRITING_APP` names a different product (leading letters, case-insensitive,
  version ignored: `Lavf59.2` is `lavf`) than both the segment's `writing_app` and `muxing_app`;
- the track's `DURATION` tag differs from the segment duration by more than 2% (both must exist).

A stale tag is replaced: `bitrate_kbps` is `total_kbps` minus the `BPS` of every audio track whose own
tags pass the same tests, else `total_kbps` itself, with `bitrate_source: "size_over_duration"` and the
warning `stale_statistics_tags`. Attachments and subtitles stay inside the estimate, so it is a slight
over-estimate. A plain `size_over_duration` (no video tag) is the whole file over the duration, audio included, with no subtraction. When the
file size or segment duration is unknown (a duration taken from a track tag does not count), or the
file was cut short, the tag cannot be checked: it is kept
(`bitrate_source: "tags"`) and the result carries `bitrate_unverified`. With no tag and no duration the
bitrate is null.

`PROBE_VERSION` is 3 (MP4 support; 2 was stale-tag handling). Cached results of an older version are
probed again, which is how the MP4 files once stored as `unsupported_container` get their real result.
Status meaning: `ok` complete; `partial` truncated file, budget or limit reached;
`invalid` nothing usable parsed; `unsupported_container` neither Matroska/WebM nor MP4.

Warnings: `truncated`, `budget_exhausted`, `element_limit`, `depth_limit`, `invalid_element`,
`unknown_size_element`, `bad_seek`, `no_tracks`, `no_video_block`, `no_encoder_settings`,
`laced_block_skipped`, `content_encoded_video`, `multiple_video_tracks`, `frame_prefix_only`,
`attachments_truncated`, `tracks_truncated`, `chapters_truncated`, `tags_truncated`,
`seekhead_truncated`, `info_truncated`, `too_short`, `invalid_ebml_header`, `no_segment`,
`stale_statistics_tags`, `bitrate_unverified`, `internal_error` (a bug; never expected).

MP4-only warnings: `invalid_box` (a box size below its header), `no_moov`, `fragmented_mp4`,
`stsd_truncated`, `duration_implausible`, `no_video_sample`, `bad_sample_offset` (the first chunk offset lies beyond the
file), `video_track_disabled`. `tracks_truncated`, `truncated`, `budget_exhausted`, `element_limit`,
`too_short`, `multiple_video_tracks`, `frame_prefix_only` and `no_encoder_settings` mean the same as above.

## Library runs

Files come from `library_files` (video extensions only). Sampling:

- `groups` (default): per title, files are clustered by a name signature (release group,
  resolution tokens, codec tokens); the first file (sorted by path) of each cluster is probed,
  at most four clusters per title.
- `all`: every video file; resumable under the byte budget.

Each file is opened through `rofs` below its root, and its size and mtime must equal the
inventory row, else the file is reported `changed` and not probed. Results are upserted into
`probe_results` as each file finishes, so an interrupted run loses nothing.

Cache: a stored result is reused (no bytes are read, but the file is opened and its size, mtime,
device and inode compared with the inventory) when the inventory's size and mtime and
`PROBE_VERSION` match and the stored status describes content (`ok`, `partial`,
`unsupported_container`, `invalid`) and the probe was not cut by the per-file byte limit (a cut
result is stored with the `allowance` it ran under and is retried only when a larger one is asked for). `unsafe`, `changed` and
`io_error` rows record why a file was not probed and are retried every run. A hit also needs the title's root label to be given with `--root`, the
root to open safely (no symlinks) and to be on the device the inventory recorded (`root_mismatch` otherwise). Re-run `manifest build` after files change. Titles under root labels that a complete `manifest build` no longer saw are absent and are skipped (no `no_root` failures); `kura manifest prune` removes them from state.

Budget: `--max-files` (default 2000) bounds the files that need I/O in one run (cache hits are free, so
repeated runs progress through a larger selection); `--max-read-mib` is a hard cap on bytes read per run; a file gets the smaller of the
per-file allowance (2 MiB) and what is left, and a probe cut short by the run budget is
discarded and stays `pending`. A second run continues where the first stopped. `--reprobe` always starts from the beginning of
the selection, so for a library larger than the caps reprobe per `--title` or raise the caps.

Output: `{"library", "sample": {"strategy", "titles", "files_selected"},
"probed", "cached", "changed", "failed": {code: n}, "bytes_read", "budget_exhausted",
"pending"}`. `failed` codes: `unsafe` (symlink, FIFO or other non-regular file), `io_error`,
`no_root` (a title's root label was not given with `--root`), `root_mismatch`.

## Read-only guarantees

A state directory equal to, inside or above a library root is refused (`unsafe_state_dir`), so the
database and lock file can never be created under a library.

Every read is either an element header, a capped body of a wanted element, or the frame prefix
of the first video block, so attachment data is never read.

Library files are reached only through `rofs.open_root` and `rofs.open_file_ro` and read with
`os.pread`; no write, create, rename or delete happens under a root, and `mkvprobe` imports no
I/O module. The tests patch `os.open` and assert no input is opened with a write flag and that
the library is byte- and mtime-identical afterwards.
