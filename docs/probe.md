# Library probing (`kura probe`)

Reads the headers of Matroska files to learn what a library file actually is: encoder and its
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

## What is read

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

## Hostile input

Nothing is allocated from a declared size: each read is `min(cap, bytes remaining)`. Nesting
stops at depth 8, a probe visits at most 20 000 elements and requests at most `max_bytes`.
Anything cut short reports `status: "partial"` with a warning token; the probe does not raise
for any content (an unforeseen parse error becomes the `internal_error` warning, which the
tests assert never occurs). Only the caller's own `read` can raise.

## Result document (`kura.probe/1`)

```
{"schema": "kura.probe/1", "status": "ok|partial|unsupported_container|invalid",
 "container": "matroska|webm|mp4|unknown", "bytes_read", "duration_s", "muxing_app", "writing_app",
 "video": null | {"track", "codec_id", "codec", "profile", "bit_depth", "width", "height", "fps",
                  "bitrate_kbps", "bitrate_source": "tags|size_over_duration"},
 "encoder": null | {"library", "version", "where", "settings": {...}},
 "audio": [{"track", "codec", "channels", "sample_rate", "lang", "default"}],
 "subtitles": [{"track", "format": "ass|srt|pgs|vobsub|other", "lang", "name", "default", "forced"}],
 "attachments": {"count", "fonts", "bytes", "names": [at most 50]},
 "chapters": {"present", "count"}, "warnings": [...]}
```

Every string is printable and at most 200 characters. `video` is null when the file has no
enabled video track. `size_over_duration` is the whole file (audio included) over the duration.
Status meaning: `ok` complete; `partial` truncated file, budget or limit reached;
`invalid` nothing usable parsed; `unsupported_container` an MP4 or anything that is not EBML.

Warnings: `truncated`, `budget_exhausted`, `element_limit`, `depth_limit`, `invalid_element`,
`unknown_size_element`, `bad_seek`, `no_tracks`, `no_video_block`, `no_encoder_settings`,
`laced_block_skipped`, `content_encoded_video`, `multiple_video_tracks`, `frame_prefix_only`,
`attachments_truncated`, `tracks_truncated`, `chapters_truncated`, `tags_truncated`.

## Library runs

Files come from `library_files` (video extensions only). Sampling:

- `groups` (default): per title, files are clustered by a name signature (release group,
  resolution tokens, codec tokens); the first file (sorted by path) of each cluster is probed,
  at most four clusters per title.
- `all`: every video file; resumable under the byte budget.

Each file is opened through `rofs` below its root, and its size and mtime must equal the
inventory row, else the file is reported `changed` and not probed. Results are upserted into
`probe_results` as each file finishes, so an interrupted run loses nothing.

Cache: a stored result is reused, at no I/O cost, when the inventory's size and mtime and
`PROBE_VERSION` match and the stored status describes content (`ok`, `partial`,
`unsupported_container`, `invalid`) and the probe was not cut by the per-file byte limit (a cut
result is stored with the `allowance` it ran under and is retried only when a larger one is asked for). `unsafe`, `changed` and
`io_error` rows record why a file was not probed and are retried every run. The cache trusts the inventory (it never opens the file), but a hit still needs the title's
root label to be given with `--root`, the root to open safely (no symlinks) and to be on the
device the inventory recorded (`root_mismatch` otherwise). Re-run `manifest build` after files change.

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
