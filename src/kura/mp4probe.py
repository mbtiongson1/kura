"""Pure MP4/M4V (ISO BMFF) header probe: the ``kura.probe/1`` document of ``mkvprobe`` for MP4.

No I/O of its own: every byte arrives through the ``read(offset, length)`` callable the
caller supplies. The probe walks box headers only (8 bytes, 16 with a 64-bit ``largesize``,
requested incrementally), so ``mdat`` bodies are never read and a ``moov`` at the end of a
non-faststart file costs the same as one at the front. It descends only along the fixed path
``moov / trak / mdia / minf / stbl`` and reads a body only when it holds something wanted,
each under its own cap:

  mvhd, tkhd, mdhd, hdlr, elng (<= 128 bytes), stsd (<= 64 KiB: the first sample entry and
  its avcC/hvcC/av1C/esds children), stts (the first 64 entries), stsz and stco/co64 (the
  first sample only), the ``chpl``/``tref`` chapter hints, the ``(c)too`` encoder string, and
  the first video sample (<= 256 KiB; located through stco/co64 and stsz) for the x264/x265
  SEI message. The sample tables themselves are skipped by size, so a ``moov`` of any size
  costs a handful of small reads.

Safety is the same as in ``mkvprobe`` (whose helpers and budgeted reader it shares): nothing
is allocated from a declared size (every read is ``min(cap, bytes remaining)``), at most
``MAX_ELEMENTS`` box headers are visited, at most ``max_bytes`` are requested, and anything
that stops short is reported through ``status`` ("partial") and ``warnings``, never an
exception. Fragmented files (``moof``) are not walked: ``moov`` is reported with the
``fragmented_mp4`` warning.

Public API:
  probe(read, size, *, max_bytes) -> dict   the ``kura.probe/1`` document
"""
import struct

from .mkvprobe import (MAX_ELEMENTS, MAX_LIST, MAX_TRACKS, MAX_WARNINGS, SCHEMA, CLUSTER_CAP, _AVC_PROFILES,
                       _HEVC_PROFILES, _avcc_bit_depth, _Budget, _frame_nals, _hvcc_nals, _Limit, _Reader,
                       _sei_settings, _Stop, _text, parse_settings)

SNIFF_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide")

STSD_CAP = 64 << 10
LEAF_CAP = 128                       # mvhd, tkhd, mdhd, hdlr, elng, the stsz/stco headers
STTS_CAP = 8 + 8 * 64
TOOL_CAP = 512
MAX_DURATION_S = 7 * 86400          # a longer duration is a corrupt or hostile header

_VIDEO = {b"avc1": "h264", b"avc3": "h264", b"dvav": "h264", b"dva1": "h264",
          b"hvc1": "hevc", b"hev1": "hevc", b"dvh1": "hevc", b"dvhe": "hevc",
          b"av01": "av1", b"vp09": "vp9", b"vp08": "vp8", b"mp4v": "mpeg4"}
_AUDIO = {b"ac-3": "ac3", b"ec-3": "eac3", b"Opus": "opus", b"fLaC": "flac", b"alac": "alac",
          b".mp3": "mp3", b"mp3 ": "mp3", b"dtsc": "dts", b"dtsh": "dts", b"dtsl": "dts", b"dtse": "dts",
          b"sowt": "pcm", b"twos": "pcm", b"lpcm": "pcm", b"ipcm": "pcm", b"in24": "pcm", b"in32": "pcm",
          b"fl32": "pcm", b"fl64": "pcm"}
_SUBS = {b"tx3g": "tx3g", b"wvtt": "wvtt", b"stpp": "stpp"}
_ESDS_AUDIO = {0x40: "aac", 0x66: "aac", 0x67: "aac", 0x68: "aac", 0x69: "mp3", 0x6B: "mp3",
               0xA5: "ac3", 0xA6: "eac3", 0xA9: "dts", 0xAA: "dts", 0xAB: "dts", 0xAC: "dts"}
_AV1_PROFILES = {0: "main", 1: "high", 2: "professional"}
_SUB_HANDLERS = (b"sbtl", b"text", b"subt")


def _u(buf, pos, n):
    """Big-endian unsigned integer of `n` bytes at `pos`, or None when it does not fit."""
    if pos < 0 or pos + n > len(buf):
        return None
    return int.from_bytes(buf[pos:pos + n], "big")


def _ticks(value, width):
    """A duration in ticks; all ones in its width (32 or 64 bits by box version) means unknown."""
    return None if value is None or value == (1 << (8 * width)) - 1 else value


def _language(code):
    """ISO-639-2 text from an mdhd packed language, 'und' when it is not three letters."""
    if code is None:
        return "und"
    letters = [((code >> s) & 0x1F) + 0x60 for s in (10, 5, 0)]
    if not all(0x61 <= c <= 0x7A for c in letters):
        return "und"
    return bytes(letters).decode("ascii")


def _esds_codec(body):
    """The codec named by the esds object type indication, or None when it cannot be parsed."""
    def descriptor(pos):
        if pos >= len(body):
            return None
        tag, length = body[pos], 0
        pos += 1
        for _ in range(4):
            if pos >= len(body):
                return None
            b = body[pos]
            pos += 1
            length = (length << 7) | (b & 0x7F)
            if not b & 0x80:
                break
        return tag, pos

    d = descriptor(4)                                        # after the version and flags
    if d is None or d[0] != 3 or d[1] + 3 > len(body):
        return None
    p, flags = d[1] + 3, body[d[1] + 2]
    if flags & 0x80:
        p += 2
    if flags & 0x40:
        if p >= len(body):
            return None
        p += 1 + body[p]
    if flags & 0x20:
        p += 2
    d = descriptor(p)
    if d is None or d[0] != 4 or d[1] >= len(body):
        return None
    return _ESDS_AUDIO.get(body[d[1]], "other")


class _Probe:
    def __init__(self, read, size, max_bytes):
        self.rd = _Reader(read, size, max_bytes)
        self.size = size
        self.elements = 0
        self.warnings = []
        self.partial = False
        self.cut = False                            # stopped by a budget or limit, not by the file
        self.container = "mp4"
        self.status = None
        self.moov = False
        self.movie = {}                             # timescale, duration (ticks), fragment duration
        self.writing_app = None
        self.tracks = []
        self.encoder = None
        self.chpl = None                            # chapter count from a chpl box
        self.chapter_ids = set()                    # track ids named by a 'chap' track reference

    # -- bookkeeping ---------------------------------------------------------------------

    def warn(self, token, partial=False):
        if token not in self.warnings and len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(token)
        self.partial = self.partial or partial

    def truncated(self, token="truncated"):
        self.warn(token, partial=True)

    def count(self):
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            self.warn("element_limit", partial=True)
            self.cut = True
            raise _Limit

    # -- boxes ---------------------------------------------------------------------------

    def boxes(self, get, start, end):
        """Yield ``(type, body, body_end, complete)`` for the boxes in ``[start, end)``.

        `get(offset, n)` is the file reader or a slice of a buffer; only the header is read.
        A box that runs past `end` is clipped and flagged; size 0 means "to `end`". A header
        that is impossible ends the iteration (partial). The position always advances.
        """
        pos = start
        while pos < end:
            self.count()
            head = get(pos, min(8, end - pos))
            if len(head) < 8:
                self.truncated()
                return
            size, kind, hl = int.from_bytes(head[:4], "big"), bytes(head[4:8]), 8
            if size == 1:
                ext = get(pos + 8, min(8, end - pos - 8))
                if len(ext) < 8:
                    self.truncated()
                    return
                size, hl = int.from_bytes(ext, "big"), 16
            elif size == 0:
                size = end - pos
            if size < hl:
                self.warn("invalid_box", partial=True)
                return
            body = pos + hl
            if size > end - pos:
                self.truncated()
                yield kind, body, end, False
                return
            yield kind, body, pos + size, True
            pos += size

    def kids(self, start, end):
        return self.boxes(self.rd.read, start, end)

    def leaf(self, start, end, cap, token=None):
        """The first `cap` bytes of a body, flagging a longer one only when `token` is given."""
        want = min(end - start, cap)
        if token and end - start > cap:
            self.warn(token, partial=True)
        data = self.rd.read(start, want)
        if len(data) < want:
            self.truncated()
        return data

    # -- driver --------------------------------------------------------------------------

    def run(self):
        size = self.size
        head = self.rd.read(0, 8)
        if len(head) < min(size, 8):
            self.status = "invalid"                 # the source delivered less than the file holds
            self.truncated()
            return
        if size < 8:
            self.status = "invalid"
            self.warn("too_short")
            return
        if head[4:8] not in SNIFF_BOXES:
            self.container = "unknown"
            self.status = "unsupported_container"
            return
        for kind, body, bend, full in self.kids(0, size):
            if kind == b"moov":
                self.moov_box(body, bend)
                break
            if kind == b"moof":                     # a fragment before any movie box: not walked
                self.warn("fragmented_mp4")
                break
        if not self.moov:
            self.warn("no_moov")
            return
        self.first_sample()

    def moov_box(self, start, end):
        self.moov = True
        for kind, b, e, full in self.kids(start, end):
            if kind == b"mvhd":
                self.mvhd(self.leaf(b, e, LEAF_CAP))
            elif kind == b"trak":
                if len(self.tracks) >= MAX_TRACKS:
                    self.warn("tracks_truncated", partial=True)
                    break
                self.trak(b, e)
            elif kind == b"udta" or kind == b"meta":
                self.udta(kind, b, e)
            elif kind == b"mvex":
                self.warn("fragmented_mp4")
                for k2, b2, e2, f2 in self.kids(b, e):
                    if k2 == b"mehd":
                        d = self.leaf(b2, e2, 16)
                        if d[:1] == b"\x01":
                            self.movie["fragment"] = _ticks(_u(d, 4, 8), 8)
                        elif d[:1] == b"\x00":
                            self.movie["fragment"] = _ticks(_u(d, 4, 4), 4)
                        break

    def mvhd(self, d):
        if d[:1] == b"\x01":
            scale, dur = _u(d, 20, 4), _ticks(_u(d, 24, 8), 8)
            ok = len(d) >= 32
        elif d[:1] == b"\x00":
            scale, dur = _u(d, 12, 4), _ticks(_u(d, 16, 4), 4)
            ok = len(d) >= 20
        else:
            scale = dur = None
            ok = False
        if scale is None or not ok:
            self.warn("invalid_box", partial=True)
            return
        self.movie.setdefault("timescale", scale)
        self.movie.setdefault("duration", dur)

    # -- udta / meta: encoder string and chapters ----------------------------------------

    def udta(self, kind, start, end):
        if kind == b"meta":                          # a full box, except in old QuickTime files
            head = self.rd.read(start, 8)
            if len(head) < 8:
                return
            start += 0 if head[4:8] == b"hdlr" else 4
            self.ilst_parent(start, end)
            return
        for k, b, e, full in self.kids(start, end):
            if k == b"\xa9too":                      # QuickTime text atom: length, language, text
                d = self.leaf(b, e, TOOL_CAP)
                self.writing_app = self.writing_app or _text(d[4:])
            elif k == b"meta":
                head = self.rd.read(b, 8)
                if len(head) == 8:
                    self.ilst_parent(b + (0 if head[4:8] == b"hdlr" else 4), e)
            elif k == b"chpl" and self.chpl is None:
                d = self.leaf(b, e, 16)
                at = 8 if d[:1] == b"\x01" else 4
                self.chpl = d[at] if len(d) > at else 0

    def ilst_parent(self, start, end):
        for k, b, e, full in self.kids(start, end):
            if k != b"ilst":
                continue
            for k2, b2, e2, f2 in self.kids(b, e):
                if k2 != b"\xa9too":
                    continue
                for k3, b3, e3, f3 in self.kids(b2, e2):
                    if k3 == b"data":
                        d = self.leaf(b3, e3, TOOL_CAP)
                        self.writing_app = self.writing_app or _text(d[8:])
                        break
                break
            break

    # -- trak ----------------------------------------------------------------------------

    def trak(self, start, end):
        t = {"seen": set(), "id": None, "enabled": True, "type": None, "timescale": None,
             "duration": None, "lang": None, "stsd": None, "delta": None, "samples": None,
             "first_size": None, "first_offset": None, "tk_width": None, "tk_height": None}
        for kind, b, e, full in self.kids(start, end):
            if kind in (b"tkhd", b"mdia", b"tref") and kind in t["seen"]:
                continue
            t["seen"].add(kind)
            if kind == b"tkhd":
                self.tkhd(self.leaf(b, e, LEAF_CAP), t)
            elif kind == b"tref":
                for k2, b2, e2, f2 in self.kids(b, e):
                    if k2 == b"chap":
                        d = self.leaf(b2, e2, 64)
                        self.chapter_ids.update(_u(d, i, 4) for i in range(0, len(d) - 3, 4))
            elif kind == b"mdia":
                self.mdia(b, e, t)
        self.tracks.append(t)

    def tkhd(self, d, t):
        v = d[:1]
        track_id = _u(d, 20, 4) if v == b"\x01" else _u(d, 12, 4) if v == b"\x00" else None
        if track_id is None:
            self.warn("invalid_box", partial=True)
            return
        t["id"] = track_id
        t["enabled"] = bool(d[3] & 1)
        at = 88 if v == b"\x01" else 76
        w, h = _u(d, at, 4), _u(d, at + 4, 4)
        t["tk_width"], t["tk_height"] = (w >> 16 if w else None), (h >> 16 if h else None)

    def mdia(self, start, end, t):
        for kind, b, e, full in self.kids(start, end):
            if kind in t["seen"] and kind in (b"mdhd", b"hdlr", b"elng", b"minf"):
                continue
            t["seen"].add(kind)
            if kind == b"mdhd":
                d = self.leaf(b, e, LEAF_CAP)
                if d[:1] == b"\x01":
                    t["timescale"], t["duration"], code = _u(d, 20, 4), _ticks(_u(d, 24, 8), 8), _u(d, 32, 2)
                elif d[:1] == b"\x00":
                    t["timescale"], t["duration"], code = _u(d, 12, 4), _ticks(_u(d, 16, 4), 4), _u(d, 20, 2)
                else:
                    code = None
                if t["timescale"] is None:
                    self.warn("invalid_box", partial=True)
                t["lang"] = t.get("elng") or _language(code)
            elif kind == b"hdlr":
                d = self.leaf(b, e, LEAF_CAP)
                if len(d) >= 12:
                    t["type"] = bytes(d[8:12])
            elif kind == b"elng":
                t["elng"] = _text(self.leaf(b, e, 40)[4:], 35)
                if t["elng"] and t.get("lang") is not None:
                    t["lang"] = t["elng"]
            elif kind == b"minf":
                for k2, b2, e2, f2 in self.kids(b, e):
                    if k2 == b"stbl":
                        self.stbl(b2, e2, t)
                        break

    def stbl(self, start, end, t):
        for kind, b, e, full in self.kids(start, end):
            if kind in t["seen"] and kind in (b"stsd", b"stts", b"stsz", b"stco", b"co64"):
                continue
            t["seen"].add(kind)
            if kind == b"stsd":
                t["stsd"] = self.leaf(b, e, STSD_CAP, "stsd_truncated")
            elif kind == b"stts":
                d = self.leaf(b, e, STTS_CAP)
                n, best = min(_u(d, 4, 4) or 0, (len(d) - 8) // 8), (0, None)
                for i in range(max(n, 0)):               # the entry that covers the most samples
                    cnt, delta = _u(d, 8 + 8 * i, 4), _u(d, 12 + 8 * i, 4)
                    if cnt > best[0]:
                        best = (cnt, delta)
                t["delta"] = best[1] or None
            elif kind == b"stsz":
                d = self.leaf(b, e, 16)
                t["samples"] = _u(d, 8, 4)
                if t["samples"]:
                    t["first_size"] = _u(d, 4, 4) or _u(d, 12, 4)
            elif kind in (b"stco", b"co64"):
                d = self.leaf(b, e, 16)
                if _u(d, 4, 4):
                    t["first_offset"] = _u(d, 8, 4 if kind == b"stco" else 8)

    # -- sample entries ------------------------------------------------------------------

    def entry(self, t):
        """Parse the first sample entry of a track into `t` (codec fourcc and its facts)."""
        buf = t.get("stsd")
        if not buf or (_u(buf, 4, 4) or 0) < 1:
            return
        for fourcc, b, e, full in self.boxes(lambda p, n: buf[p:p + n], 8, len(buf)):
            t["fourcc"] = fourcc
            kind = t["type"]
            if kind == b"vide" or (kind is None and fourcc in _VIDEO):
                self.visual(buf, b, e, t)
            elif kind == b"soun" or (kind is None and fourcc in _AUDIO):
                self.audio(buf, b, e, t)
            break

    def visual(self, buf, b, e, t):
        t["width"], t["height"] = _u(buf, b + 24, 2), _u(buf, b + 26, 2)
        if e - b < 78:
            return
        for kind, cb, ce, full in self.boxes(lambda p, n: buf[p:p + n], b + 78, e):
            if kind in (b"avcC", b"hvcC", b"av1C"):
                t["config"] = (kind, bytes(buf[cb:ce]))
                break

    def audio(self, buf, b, e, t):
        version = _u(buf, b + 8, 2)
        if version in (0, 1):
            t["channels"], t["rate"] = _u(buf, b + 16, 2), _u(buf, b + 24, 2)
        skip = {0: 28, 1: 44, 2: 64}.get(version)
        start = e if skip is None else b + skip
        if t["fourcc"] == b"mp4a":
            for kind, cb, ce, full in self.boxes(lambda p, n: buf[p:p + n], start, e):
                if kind == b"esds":
                    t["esds"] = _esds_codec(bytes(buf[cb:ce]))
                    break

    # -- first video sample --------------------------------------------------------------

    def video(self):
        """The track whose codec describes the file: enabled, never a one-sample still when a
        real video track exists, then a known codec over an unknown one."""
        videos = [t for t in self.tracks if t["type"] == b"vide"]
        enabled = [t for t in videos if t["enabled"]]
        if videos and not enabled:
            self.warn("video_track_disabled")
        pool = enabled or videos
        if len(pool) > 1:
            self.warn("multiple_video_tracks")
        if not pool:
            return None
        moving = [t for t in pool if t["samples"] != 1]
        return max(moving or pool, key=lambda t: t.get("fourcc") in _VIDEO)

    def parse_entries(self):
        for i, t in enumerate(self.tracks):
            if t["id"] is None:
                t["id"] = i + 1
            if not t.get("parsed"):
                t["parsed"] = True
                self.entry(t)

    def first_sample(self):
        self.parse_entries()
        video = self.video()
        if video is None:
            return
        fourcc = video.get("fourcc")
        codec = {"h264": "avc", "hevc": "hevc"}.get(_VIDEO.get(fourcc))
        if codec is None:
            return
        private = video.get("config", (None, b""))[1]
        length_size = 4
        if codec == "avc" and len(private) > 4 and private[0] == 1:
            length_size = (private[4] & 3) + 1
        elif codec == "hevc" and len(private) > 21 and private[0] == 1:
            length_size = (private[21] & 3) + 1
        if codec == "hevc":
            for nal in _hvcc_nals(private):
                found = _sei_settings(nal, codec)
                if found:
                    self.set_encoder(found, "codec_private")
                    break
        frame, clamped = None, False
        offset, length = video["first_offset"], video["first_size"]
        if self.encoder is None and offset is not None and length:
            if offset >= self.size:
                self.warn("bad_sample_offset")
            else:
                want = min(length, CLUSTER_CAP, max(self.rd.max - self.rd.asked, 0))
                clamped = want < min(length, CLUSTER_CAP)
                frame = self.rd.read(offset, want)
                if length > want and not clamped:
                    self.warn("frame_prefix_only")           # normal for a large keyframe, not damage
        if self.encoder is None and frame is not None:
            for nal in _frame_nals(frame, length_size):
                found = _sei_settings(nal, codec)
                if found:
                    self.set_encoder(found, "first_frame")
                    break
        if self.encoder is None and frame is None and not clamped:
            self.warn("no_video_sample")
        if self.encoder is None:
            self.warn("no_encoder_settings")
            if clamped:                                  # the budget, not the file, ended the search
                self.warn("budget_exhausted", partial=True)
                self.cut = True

    def set_encoder(self, found, where):
        library, raw = found
        parsed = parse_settings(raw)
        self.encoder = {"library": library, "version": parsed["version"], "where": where,
                        "settings": parsed["options"]}

    # -- output --------------------------------------------------------------------------

    def seconds(self, ticks, scale):
        """Seconds for a duration, or None when it is absent, zero or implausible (over a week)."""
        if not scale or not ticks:
            return None
        value = ticks / scale
        if value > MAX_DURATION_S:
            self.warn("duration_implausible")
            return None
        return round(value, 3)

    def duration(self):
        """The movie duration, else the fragment duration (mehd), else the longest track's."""
        scale = self.movie.get("timescale")
        found = self.seconds(self.movie.get("duration"), scale)
        if found is None:
            found = self.seconds(self.movie.get("fragment"), scale)
        if found is None:
            spans = [self.seconds(t["duration"], t["timescale"]) for t in self.tracks]
            found = max((x for x in spans if x), default=None)
        return found

    def video_doc(self, v, duration):
        fourcc = v.get("fourcc")
        codec = _VIDEO.get(fourcc, "other")
        kind, private = v.get("config", (None, b""))
        profile = bits = None
        if kind == b"avcC" and len(private) > 1 and private[0] == 1:
            profile = _AVC_PROFILES.get(private[1])
            bits = _avcc_bit_depth(private) or {66: 8, 77: 8, 88: 8, 100: 8, 110: 10}.get(private[1])
        elif kind == b"hvcC" and len(private) > 17 and private[0] == 1:
            profile = _HEVC_PROFILES.get(private[1] & 0x1F)
            bits = (private[17] & 7) + 8
        elif kind == b"av1C" and len(private) > 2 and private[0] == 0x81:
            profile = _AV1_PROFILES.get(private[1] >> 5)
            bits = 12 if private[2] & 0x60 == 0x60 else 10 if private[2] & 0x40 else 8
        fps = None
        if v["delta"] and v["timescale"]:
            fps = round(v["timescale"] / v["delta"], 3)
        elif v["samples"] and v["timescale"] and v["duration"]:
            fps = round(v["samples"] * v["timescale"] / v["duration"], 3)
        bitrate = source = None
        if duration and duration > 0 and self.size > 0:
            bitrate, source = round(self.size * 8 / duration / 1000), "size_over_duration"
        return {"track": v["id"], "codec_id": _text(fourcc) if fourcc else None, "codec": codec,
                "profile": profile, "bit_depth": bits,
                "width": v.get("width") or v["tk_width"], "height": v.get("height") or v["tk_height"],
                "fps": fps, "bitrate_kbps": bitrate, "bitrate_source": source}

    def chapters(self):
        count = self.chpl
        if count is None:
            for t in self.tracks:
                if t["id"] in self.chapter_ids:
                    count = t["samples"] or 0
                    break
        if count is None and not self.chapter_ids:
            return {"present": False, "count": 0}
        return {"present": True, "count": count or 0}

    def result(self):
        self.parse_entries()                    # may flag a damaged sample entry: before the status
        if self.rd.short:
            self.truncated()                    # whatever the cause, the file was not read in full
        if self.status is None:
            if self.tracks:
                self.status = "partial" if self.partial else "ok"
            elif self.cut or (self.partial and self.movie):
                self.status = "partial"
            else:
                self.status = "invalid"
                self.warn("no_tracks")
        duration = self.duration()
        video = self.video() if self.tracks else None
        audio, subs = [], []
        for t in self.tracks:
            kind = t["type"]
            if kind == b"soun" and len(audio) < MAX_LIST:
                fourcc = t.get("fourcc")
                codec = (t.get("esds") or "aac") if fourcc == b"mp4a" else _AUDIO.get(fourcc, "other")
                audio.append({"track": t["id"], "codec": codec, "channels": t.get("channels"),
                              "sample_rate": t.get("rate") or None, "lang": t["lang"] or "und",
                              "default": t["enabled"]})
            elif kind in _SUB_HANDLERS and t["id"] not in self.chapter_ids and len(subs) < MAX_LIST:
                subs.append({"track": t["id"], "format": _SUBS.get(t.get("fourcc"), "other"),
                             "lang": t["lang"] or "und", "name": None, "default": t["enabled"],
                             "forced": False})
        video_doc = self.video_doc(video, duration) if video else None
        return {"schema": SCHEMA, "status": self.status, "container": self.container,
                "bytes_read": self.rd.used, "duration_s": duration,
                "muxing_app": None, "writing_app": self.writing_app,
                "video": video_doc,
                "encoder": self.encoder, "audio": audio, "subtitles": subs,
                "attachments": {"count": 0, "fonts": 0, "bytes": 0, "names": []},
                "chapters": self.chapters(), "warnings": list(self.warnings)}


_ERRORS = (ArithmeticError, ValueError, IndexError, KeyError, TypeError, AttributeError,
           struct.error, UnicodeError, RecursionError)


def probe(read, size, *, max_bytes=2 << 20):
    """Probe one MP4 file. ``read(offset, length)`` returns up to `length` bytes (fewer at EOF).

    Never raises for any content: the result's ``status`` says how far the probe got.
    Total bytes requested from `read` never exceed `max_bytes`.
    """
    if not isinstance(size, int) or size < 0 or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("size and max_bytes must be non-negative integers")
    p = _Probe(read, size, max_bytes)
    try:
        p.run()
    except _Budget:
        p.warn("budget_exhausted", partial=True)
        p.cut = True
    except _Stop:
        pass
    except _ERRORS:
        p.warn("internal_error", partial=True)
    try:
        return p.result()
    except _ERRORS:
        return {"schema": SCHEMA, "status": "invalid", "container": p.container, "bytes_read": p.rd.used,
                "duration_s": None, "muxing_app": None, "writing_app": None, "video": None,
                "encoder": None, "audio": [], "subtitles": [],
                "attachments": {"count": 0, "fonts": 0, "bytes": 0, "names": []},
                "chapters": {"present": False, "count": 0}, "warnings": ["internal_error"]}
