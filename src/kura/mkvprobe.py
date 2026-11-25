"""Pure Matroska/WebM header probe: container facts and the x264/x265 encoder settings.

No I/O of its own: every byte arrives through the ``read(offset, length)`` callable the
caller supplies (``probe.py`` and the CLI give it ``os.pread`` on a descriptor that came
from ``rofs``). The probe never needs the whole file. It walks the Segment's level-1
element headers (at most 16 bytes per header), skips bodies, and reads a body only when it
holds something wanted, each under its own cap:

  Info, Tracks (<= 1 MiB), Chapters (<= 256 KiB, presence and atom count), Attachments
  (child headers only; the file data is never read), Tags (<= 256 KiB, per-track BPS,
  DURATION, NUMBER_OF_FRAMES), SeekHead (<= 64 KiB, to find what lies behind the first
  Cluster), and the first Cluster (<= 256 KiB, the first block of the video track).

Hostile input cannot make this module allocate from a declared size (every read is
``min(cap, bytes remaining)``), recurse past ``MAX_DEPTH``, visit more than
``MAX_ELEMENTS`` elements or read more than ``max_bytes``. Anything that stops short is
reported through ``status`` ("partial") and ``warnings``, never an exception; only the
caller's own ``read`` can raise. As a last resort an unforeseen parse error is converted
to the ``internal_error`` warning, which the tests assert never happens.

Public API:
  probe(read, size, *, max_bytes) -> dict   the ``kura.probe/1`` document
  parse_settings(raw)                       {"library", "version", "options"}
"""
import math
import re
import struct

PROBE_VERSION = 1
SCHEMA = "kura.probe/1"

MAX_ELEMENTS = 20_000
MAX_DEPTH = 8
MAX_TRACKS = 128
MAX_LIST = 32                       # audio/subtitle entries reported
MAX_ATTACHMENTS = 500
MAX_NAMES = 50
MAX_WARNINGS = 20
MAX_OPTIONS = 256
SETTINGS_MAX = 8 << 10
TEXT_MAX = 200

EBML_HEADER_CAP = 4096
SEEKHEAD_CAP = 64 << 10
INFO_CAP = 64 << 10
TRACKS_CAP = 1 << 20
CHAPTERS_CAP = 256 << 10
TAGS_CAP = 256 << 10
CLUSTER_CAP = 256 << 10
PRIVATE_CAP = 64 << 10
STRING_READ_CAP = 512               # attachment names and mime types

# ---- element ids ----------------------------------------------------------------------
EBML, DOCTYPE = 0x1A45DFA3, 0x4282
SEGMENT, SEEKHEAD, SEEK, SEEKID, SEEKPOS = 0x18538067, 0x114D9B74, 0x4DBB, 0x53AB, 0x53AC
INFO, TRACKS, CLUSTER = 0x1549A966, 0x1654AE6B, 0x1F43B675
CHAPTERS, ATTACHMENTS, TAGS = 0x1043A770, 0x1941A469, 0x1254C367
TIMESCALE, DURATION, MUXINGAPP, WRITINGAPP = 0x2AD7B1, 0x4489, 0x4D80, 0x5741
TRACKENTRY, TRACKNUMBER, TRACKUID, TRACKTYPE = 0xAE, 0xD7, 0x73C5, 0x83
FLAGENABLED, FLAGDEFAULT, FLAGFORCED = 0xB9, 0x88, 0x55AA
DEFAULTDURATION, TRACKNAME, LANGUAGE, LANGUAGEBCP47 = 0x23E383, 0x536E, 0x22B59C, 0x22B59D
CODECID, CODECPRIVATE, CODECNAME, CONTENTENCODINGS = 0x86, 0x63A2, 0x258688, 0x6D80
VIDEO, AUDIO = 0xE0, 0xE1
PIXELW, PIXELH, COLOUR, BITSPERCHANNEL = 0xB0, 0xBA, 0x55B0, 0x55B2
CHANNELS, SAMPLERATE, OUTPUTSAMPLERATE = 0x9F, 0xB5, 0x78B5
ATTACHEDFILE, FILENAME, FILEMIME, FILEDATA = 0x61A7, 0x466E, 0x4660, 0x465C
EDITIONENTRY, CHAPTERATOM = 0x45B9, 0xB6
TAG, TARGETS, TAGTRACKUID, SIMPLETAG, TAGNAME, TAGSTRING = 0x7373, 0x63C0, 0x63C5, 0x67C8, 0x45A3, 0x4487
SIMPLEBLOCK, BLOCKGROUP, BLOCK = 0xA3, 0xA0, 0xA1
LEVEL1 = frozenset({SEEKHEAD, INFO, TRACKS, CLUSTER, 0x1C53BB6B, CHAPTERS, ATTACHMENTS, TAGS})

UUIDS = {bytes.fromhex("DC45E9BDE6D948B7962CD820D923EEEF"): "x264",
         bytes.fromhex("2CA2DE09B51747DBBB55A4FE7FC2FC4E"): "x265"}

_AVC_PROFILES = {66: "baseline", 77: "main", 88: "extended", 100: "high", 110: "high10",
                 122: "high422", 244: "high444", 44: "cavlc444"}
_HEVC_PROFILES = {1: "main", 2: "main10", 3: "main_still", 4: "rext", 5: "high_throughput",
                  9: "scc"}
_FONT_EXT = (".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".pfb", ".pfm")
_FONT_MIME = ("font", "truetype", "opentype")


# ---- internal control flow ---------------------------------------------------------------

class _Stop(Exception):
    """Probing ends here; the reason is already a warning and `partial` is set."""


class _Short(Exception):
    """A header ran past the end of the available bytes."""


class _Bad(Exception):
    """A header is not valid EBML."""


# ---- byte helpers (all bounds-checked) -----------------------------------------------------

def _header(buf, pos, end):
    """``(id, size, header_len)`` of the element at ``buf[pos]``; size None means unknown."""
    end = min(end, len(buf))
    if pos >= end:
        raise _Short
    first = buf[pos]
    if first == 0:
        raise _Bad
    n = 9 - first.bit_length()                       # id length in bytes
    if n > 4:
        raise _Bad
    if pos + n >= end:
        raise _Short
    eid = int.from_bytes(buf[pos:pos + n], "big")
    pos += n
    first = buf[pos]
    if first == 0:
        raise _Bad
    m = 9 - first.bit_length()                       # size length in bytes
    if pos + m > end:
        raise _Short
    value = first & (0xFF >> m)
    for i in range(1, m):
        value = (value << 8) | buf[pos + i]
    return eid, (None if value == (1 << (7 * m)) - 1 else value), n + m


def _vint(buf, pos):
    """Block track number: ``(value, length)`` or None. Markers are stripped."""
    if pos >= len(buf) or buf[pos] == 0:
        return None
    n = 9 - buf[pos].bit_length()
    if pos + n > len(buf):
        return None
    value = buf[pos] & (0xFF >> n)
    for i in range(1, n):
        value = (value << 8) | buf[pos + i]
    return value, n


def _uint(data):
    return int.from_bytes(data, "big") if 0 < len(data) <= 8 else None


def _float(data):
    if len(data) == 4:
        value = struct.unpack(">f", data)[0]
    elif len(data) == 8:
        value = struct.unpack(">d", data)[0]
    else:
        return None
    return value if math.isfinite(value) else None


def _text(data, limit=TEXT_MAX):
    """Printable, bounded text from untrusted bytes, or None when nothing is left."""
    s = bytes(data).split(b"\0", 1)[0].decode("utf-8", "replace")
    s = "".join(c if c.isprintable() else "?" for c in s).strip()
    return s[:limit] or None


def _digits(value):
    """A positive integer from ASCII digits only (never int() on arbitrary text), else None."""
    return int(value) if value and re.fullmatch(r"[0-9]{1,15}", value) and int(value) > 0 else None


def _unescape(data):
    """Remove H.264/H.265 emulation-prevention bytes (00 00 03 -> 00 00)."""
    out, i = bytearray(), 0
    while True:
        j = data.find(b"\x00\x00\x03", i)
        if j < 0:
            out += data[i:]
            return bytes(out)
        out += data[i:j + 2]
        i = j + 3


def _is_font(name, mime):
    mime = (mime or "").lower()
    name = (name or "").lower()
    return any(k in mime for k in _FONT_MIME) or name.endswith(_FONT_EXT)


# ---- encoder settings ---------------------------------------------------------------------

def _sei_settings(nal, codec):
    """``(library, text)`` of an x264/x265 user_data_unregistered SEI message in `nal`."""
    if not nal:
        return None
    if codec == "avc":
        if nal[0] & 0x1F != 6:
            return None
        head = 1
    else:
        if (nal[0] >> 1) & 0x3F not in (39, 40):
            return None
        head = 2
    if len(nal) <= head:
        return None
    body = _unescape(nal[head:])
    n, q = len(body), 0
    while n - q >= 2:                       # fewer bytes left is only the rbsp trailing bits
        ptype = 0
        while q < n and body[q] == 0xFF:
            ptype += 255
            q += 1
        if q >= n:
            return None
        ptype += body[q]
        q += 1
        psize = 0
        while q < n and body[q] == 0xFF:
            psize += 255
            q += 1
        if q >= n:
            return None
        psize += body[q]
        q += 1
        payload = body[q:q + psize]
        q += psize
        if ptype == 5 and len(payload) > 16 and payload[:16] in UUIDS:
            raw = payload[16:16 + SETTINGS_MAX].split(b"\0", 1)[0].decode("latin-1")
            return UUIDS[payload[:16]], "".join(c for c in raw if c.isprintable())
    return None


def _frame_nals(frame, length_size):
    """Length-prefixed NAL units of one access unit; a truncated tail is clipped, not trusted."""
    p, count = 0, 0
    while p + length_size <= len(frame) and count < 64:
        n = int.from_bytes(frame[p:p + length_size], "big")
        p += length_size
        if n:
            yield frame[p:p + n]
        p += n
        count += 1


def _hvcc_nals(private):
    """NAL units stored in the HEVCDecoderConfigurationRecord arrays."""
    if len(private) < 23:
        return
    p = 23
    for _ in range(min(private[22], 32)):
        if p + 3 > len(private):
            return
        count = int.from_bytes(private[p + 1:p + 3], "big")
        p += 3
        for i in range(count):
            if p + 2 > len(private):
                return
            n = int.from_bytes(private[p:p + 2], "big")
            nal = private[p + 2:p + 2 + n]
            p += 2 + n
            if i < 64:
                yield nal


def _avcc_bit_depth(private):
    """Luma bit depth from the avcC extension that high profiles carry after the PPS list."""
    if len(private) < 7 or private[1] not in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        return None
    p = 6
    for _ in range(private[5] & 0x1F):                       # SPS entries
        if p + 2 > len(private):
            return None
        p += 2 + int.from_bytes(private[p:p + 2], "big")
    if p >= len(private):
        return None
    count, p = private[p], p + 1
    for _ in range(count):                                   # PPS entries
        if p + 2 > len(private):
            return None
        p += 2 + int.from_bytes(private[p:p + 2], "big")
    if p + 3 > len(private):
        return None
    return (private[p + 1] & 7) + 8


def parse_settings(raw):
    """Turn an x264/x265 banner (``... - options: key=value ...``) into data.

    Returns ``{"library", "version", "options"}``. ``options`` maps each key to its string
    value, or True for a bare flag; it is bounded in size and printable.
    """
    raw = str(raw)[:SETTINGS_MAX]
    head, _, tail = raw.partition("options:")
    words = head.split()
    first = words[0] if words else ""
    library = "x264" if first.startswith("x264") else "x265" if first.startswith("x265") \
        else _text(first.encode("utf-8", "replace"), 32)
    version = None
    if library == "x264":
        m = re.search(r"core (\d+)(?: (r\d+))?(?: ([0-9a-f]{5,40}))?", head)
        if m:
            version = " ".join(g for g in m.groups() if g)
    elif library == "x265":
        fields = [f.strip() for f in head.split(" - ")]
        if len(fields) > 1:
            version = _text(fields[1].split(":")[0].encode("utf-8", "replace"), 64)
    options = {}
    for token in tail.split():
        if token == "/" or token.startswith(("[", "(")) or len(options) >= MAX_OPTIONS:
            continue
        key, eq, value = token.partition("=")
        key = _text(key.encode("utf-8", "replace"), 64)
        if key:
            options[key] = (_text(value.encode("utf-8", "replace")) or "") if eq else True
    return {"library": library, "version": version, "options": options}


# ---- the probe ----------------------------------------------------------------------------

class _Reader:
    """Budgeted, bounds-clamped positional reader around the caller's ``read``."""

    def __init__(self, read, size, max_bytes):
        self._read, self.size, self.max, self.used = read, size, max_bytes, 0

    def read(self, offset, n):
        if n <= 0 or offset < 0 or offset >= self.size:
            return b""
        n = min(n, self.size - offset)
        if self.used + n > self.max:
            raise _Budget
        data = bytes(self._read(offset, n))[:n]
        self.used += len(data)
        return data


class _Budget(_Stop):
    pass


class _Limit(_Stop):
    pass


def _codec_name(codec_id):
    c = codec_id or ""
    for prefix, name in (("V_MPEG4/ISO/AVC", "h264"), ("V_MPEGH/ISO/HEVC", "hevc"), ("V_AV1", "av1"),
                         ("V_VP9", "vp9"), ("V_VP8", "vp8"), ("V_MPEG2", "mpeg2"),
                         ("V_MS/VFW", "vfw"), ("A_AAC", "aac"), ("A_EAC3", "eac3"), ("A_AC3", "ac3"),
                         ("A_DTS", "dts"), ("A_FLAC", "flac"), ("A_OPUS", "opus"),
                         ("A_VORBIS", "vorbis"), ("A_TRUEHD", "truehd"), ("A_MPEG/L3", "mp3"),
                         ("A_MPEG/L2", "mp2"), ("A_PCM", "pcm")):
        if c.startswith(prefix):
            return name
    return "other"


def _sub_format(codec_id):
    c = (codec_id or "").upper()
    if c in ("S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA"):
        return "ass"
    if c in ("S_TEXT/UTF8", "S_TEXT/ASCII"):
        return "srt"
    if c == "S_HDMV/PGS":
        return "pgs"
    if c.startswith("S_VOBSUB"):
        return "vobsub"
    return "other"


class _Probe:
    def __init__(self, read, size, max_bytes):
        self.rd = _Reader(read, size, max_bytes)
        self.size = size
        self.elements = 0
        self.warnings = []
        self.partial = False
        self.cut = False                            # stopped by a budget or limit, not by the file
        self.container = "unknown"
        self.status = None
        self.seg0 = self.seg_end = 0
        self.seek = {}
        self.seekheads = 0
        self.seen = set()
        self.info = {}
        self.tracks = []
        self.encoder = None
        self.attach = {"count": 0, "fonts": 0, "bytes": 0, "names": []}
        self.chapters = {"present": False, "count": 0}
        self.tags = {}

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

    # -- iteration -----------------------------------------------------------------------

    def file_children(self, start, end):
        """Yield ``(id, body, body_end, complete)`` for the elements in ``[start, end)`` of the
        file, reading only their headers. A size beyond `end` is clipped (and flagged); an
        unknown size ends the iteration after yielding with ``body_end`` None."""
        pos = start
        while pos < end:
            self.count()
            head = self.rd.read(pos, min(16, end - pos))
            try:
                eid, size, hl = _header(head, 0, len(head))
            except _Short:
                self.truncated()
                return
            except _Bad:
                self.warn("invalid_element", partial=True)
                return
            body = pos + hl
            if size is None:
                yield eid, body, None, False
                return
            if size > end - body:
                self.truncated()
                yield eid, body, end, False
                return
            yield eid, body, body + size, True
            pos = body + size

    def children(self, buf, start, end, strict=True):
        """Same shape over an in-memory buffer. Offsets are into `buf`.

        A header or element that does not fit inside its parent is damage and marks the result
        partial; `strict=False` is for a buffer that was cut at a read cap (the first Cluster),
        where running out of bytes is expected."""
        pos, end = start, min(end, len(buf))
        while pos < end:
            self.count()
            try:
                eid, size, hl = _header(buf, pos, end)
            except _Short:
                if strict:
                    self.truncated()
                return
            except _Bad:
                self.warn("invalid_element", partial=True)
                return
            body = pos + hl
            if size is None or size > end - body:
                if strict:
                    self.truncated()
                yield eid, body, end, False
                return
            yield eid, body, body + size, True
            pos = body + size

    def body(self, start, end, cap, token):
        """The first `cap` bytes of a body, flagging when it is longer or cut short."""
        want = min(end - start, cap)
        if end - start > cap:
            self.warn(token, partial=True)
        data = self.rd.read(start, want)
        if len(data) < want:
            self.truncated()
        return data

    # -- driver --------------------------------------------------------------------------

    def run(self):
        size = self.size
        head = self.rd.read(0, 16)
        if len(head) < min(size, 16):
            self.status = "invalid"                 # the source delivered less than the file holds
            self.truncated()
            return
        if size < 8:
            self.status = "invalid"
            self.warn("too_short")
            return
        if head[:4] == b"\x1a\x45\xdf\xa3":
            self.container = "matroska"
        elif head[4:8] == b"ftyp":
            self.container = "mp4"
            self.status = "unsupported_container"
            return
        else:
            self.status = "unsupported_container"
            return
        try:
            eid, hsize, hl = _header(head, 0, len(head))
        except (_Short, _Bad):
            self.warn("invalid_ebml_header", partial=True)
            return
        if hsize is None or hsize > EBML_HEADER_CAP:
            self.warn("invalid_ebml_header", partial=True)
            return
        buf = self.rd.read(hl, hsize)
        if len(buf) < hsize:
            self.truncated()
            return
        for cid, b, e, full in self.children(buf, 0, len(buf)):
            if cid == DOCTYPE and full:
                doctype = (_text(buf[b:e], 16) or "").lower()
                if doctype == "webm":
                    self.container = "webm"
        # Find the Segment after the EBML header.
        segment = None
        for sid, body, bend, full in self.file_children(hl + hsize, size):
            if sid == SEGMENT:
                segment = (body, bend)
                break
        if segment is None:
            self.warn("no_segment", partial=True)
            return
        self.seg0 = segment[0]
        self.seg_end = size if segment[1] is None else min(segment[1], size)
        self.segment()

    def segment(self):
        cluster = None
        for eid, body, bend, full in self.file_children(self.seg0, self.seg_end):
            if eid == CLUSTER:
                cluster = (body, bend)
                break
            if bend is None:
                self.warn("unknown_size_element", partial=True)
                break
            self.element(eid, body, bend)
        # Whatever the SeekHead says lives past the first Cluster: the track list first.
        for eid in (INFO, TRACKS):
            self.jump(eid)
        if cluster is not None:
            self.first_cluster(*cluster)
        for eid in (ATTACHMENTS, CHAPTERS, TAGS):
            self.jump(eid)

    def element(self, eid, body, bend):
        """Handle one level-1 element once; later duplicates are ignored."""
        if eid == SEEKHEAD:
            self.seekheads += 1
            if self.seekheads <= 4:
                self.seekhead(body, bend)
            return
        if eid in self.seen or eid not in (INFO, TRACKS, CHAPTERS, ATTACHMENTS, TAGS):
            return
        self.seen.add(eid)
        if eid == INFO:
            self.parse_info(self.body(body, bend, INFO_CAP, "info_truncated"))
        elif eid == TRACKS:
            self.parse_tracks(self.body(body, bend, TRACKS_CAP, "tracks_truncated"))
        elif eid == CHAPTERS:
            self.parse_chapters(self.body(body, bend, CHAPTERS_CAP, "chapters_truncated"))
        elif eid == TAGS:
            self.parse_tags(self.body(body, bend, TAGS_CAP, "tags_truncated"))
        else:
            self.parse_attachments(body, bend)

    def jump(self, wanted):
        """Fetch a level-1 element that sat behind the first Cluster, via the SeekHead."""
        if wanted in self.seen or wanted not in self.seek:
            return
        pos = self.seek[wanted]
        if not self.seg0 <= pos < self.seg_end:
            self.warn("bad_seek")
            return
        for eid, body, bend, full in self.file_children(pos, self.seg_end):
            if eid != wanted or bend is None:
                self.warn("bad_seek")
            else:
                self.element(eid, body, bend)
            return

    # -- level-1 bodies ------------------------------------------------------------------

    def seekhead(self, body, bend):
        buf = self.body(body, bend, SEEKHEAD_CAP, "seekhead_truncated")
        for eid, b, e, full in self.children(buf, 0, len(buf)):
            if eid != SEEK:
                continue
            sid = pos = None
            for cid, cb, ce, cfull in self.children(buf, b, e):
                if cid == SEEKID and cfull:
                    sid = _uint(buf[cb:ce])
                elif cid == SEEKPOS and cfull:
                    pos = _uint(buf[cb:ce])
            if sid is not None and pos is not None and pos < self.size:
                self.seek.setdefault(sid, self.seg0 + pos)

    def parse_info(self, buf):
        scale, ticks = 1_000_000, None
        for eid, b, e, full in self.children(buf, 0, len(buf)):
            if not full:
                continue
            if eid == TIMESCALE:
                scale = _uint(buf[b:e]) or scale
            elif eid == DURATION:
                ticks = _float(buf[b:e])
            elif eid == MUXINGAPP:
                self.info["muxing_app"] = _text(buf[b:e])
            elif eid == WRITINGAPP:
                self.info["writing_app"] = _text(buf[b:e])
        if ticks is not None and ticks >= 0:
            seconds = ticks * scale / 1e9
            if math.isfinite(seconds):
                self.info["duration_s"] = round(seconds, 3)

    def parse_tracks(self, buf):
        for eid, b, e, full in self.children(buf, 0, len(buf)):
            if eid != TRACKENTRY:
                continue
            if len(self.tracks) >= MAX_TRACKS:
                self.warn("tracks_truncated", partial=True)
                break
            self.tracks.append(self.parse_track(buf, b, e))

    def parse_track(self, buf, start, end):
        t = {"encoded": False}
        for eid, b, e, full in self.children(buf, start, end):
            d = buf[b:e]
            if eid == VIDEO or eid == AUDIO:
                self.parse_media(buf, b, e, t)
            elif eid == CONTENTENCODINGS:
                t["encoded"] = True
            elif eid == CODECPRIVATE:
                t["private"] = bytes(d[:PRIVATE_CAP])
            elif not full:
                continue
            elif eid == TRACKNUMBER:
                t["number"] = _uint(d)
            elif eid == TRACKUID:
                t["uid"] = _uint(d)
            elif eid == TRACKTYPE:
                t["type"] = _uint(d)
            elif eid == FLAGENABLED:
                t["enabled"] = bool(_uint(d))
            elif eid == FLAGDEFAULT:
                t["default"] = bool(_uint(d))
            elif eid == FLAGFORCED:
                t["forced"] = bool(_uint(d))
            elif eid == DEFAULTDURATION:
                t["default_duration"] = _uint(d)
            elif eid == TRACKNAME:
                t["name"] = _text(d)
            elif eid == LANGUAGE:
                t["lang"] = _text(d, 35)
            elif eid == LANGUAGEBCP47:
                t["bcp47"] = _text(d, 35)
            elif eid == CODECID:
                t["codec_id"] = _text(d)
        return t

    def parse_media(self, buf, start, end, t):
        for eid, b, e, full in self.children(buf, start, end):
            if not full and eid != COLOUR:
                continue
            d = buf[b:e]
            if eid == PIXELW:
                t["width"] = _uint(d)
            elif eid == PIXELH:
                t["height"] = _uint(d)
            elif eid == COLOUR:
                for cid, cb, ce, cfull in self.children(buf, b, e):
                    if cid == BITSPERCHANNEL and cfull:
                        t["bits"] = _uint(buf[cb:ce])
            elif eid == CHANNELS:
                t["channels"] = _uint(d)
            elif eid == SAMPLERATE:
                t["rate"] = _float(d)
            elif eid == OUTPUTSAMPLERATE:
                t["out_rate"] = _float(d)
            # The Audio BitDepth element is deliberately not read: it says nothing about video.

    def parse_chapters(self, buf):
        self.chapters["present"] = True

        def walk(start, end, depth):
            for eid, b, e, full in self.children(buf, start, end):
                if eid == CHAPTERATOM:
                    self.chapters["count"] += 1
                if eid in (EDITIONENTRY, CHAPTERATOM):
                    if depth >= MAX_DEPTH:
                        self.warn("depth_limit", partial=True)
                        return
                    walk(b, e, depth + 1)

        walk(0, len(buf), 0)

    def parse_tags(self, buf):
        for eid, b, e, full in self.children(buf, 0, len(buf)):
            if eid != TAG:
                continue
            uids, values = [], {}
            for cid, cb, ce, cfull in self.children(buf, b, e):
                if cid == TARGETS:
                    for tid, tb, te, tfull in self.children(buf, cb, ce):
                        if tid == TAGTRACKUID and tfull:
                            uids.append(_uint(buf[tb:te]))
                elif cid == SIMPLETAG and len(values) < 64:
                    name = value = None
                    for sid, sb, se, sfull in self.children(buf, cb, ce):
                        if sid == TAGNAME and sfull:
                            name = _text(buf[sb:se], 64)
                        elif sid == TAGSTRING and sfull:
                            value = _text(buf[sb:se], 64)
                    if name and value is not None:
                        values[name.upper().split("-", 1)[0]] = value
            for uid in uids:
                if uid is not None and len(self.tags) < 256:
                    self.tags.setdefault(uid, {}).update(values)

    def parse_attachments(self, body, bend):
        a = self.attach
        for eid, b, e, full in self.file_children(body, bend):
            if eid != ATTACHEDFILE or e is None:
                continue
            if a["count"] >= MAX_ATTACHMENTS:
                self.warn("attachments_truncated", partial=True)
                return
            a["count"] += 1
            name = mime = None
            for cid, cb, ce, cfull in self.file_children(b, e):
                if ce is None:
                    break
                if cid == FILENAME:
                    name = _text(self.rd.read(cb, min(ce - cb, STRING_READ_CAP)))
                elif cid == FILEMIME:
                    mime = _text(self.rd.read(cb, min(ce - cb, STRING_READ_CAP)))
                elif cid == FILEDATA:
                    a["bytes"] += ce - cb          # the data itself is never read
            if _is_font(name, mime):
                a["fonts"] += 1
            if name and len(a["names"]) < MAX_NAMES:
                a["names"].append(name)

    # -- first cluster: the video frame that carries the x264/x265 settings ----------------

    def video_track(self):
        video = [t for t in self.tracks if t.get("type") == 1 and t.get("number") is not None
                 and t.get("enabled", True)]
        if len(video) > 1:
            self.warn("multiple_video_tracks")
        return video[0] if video else None

    def blocks(self, buf):
        """Yield the raw payload of each (Simple)Block of a cluster buffer, in file order."""
        for eid, b, e, full in self.children(buf, 0, len(buf), strict=False):
            if eid == SIMPLEBLOCK:
                yield buf[b:e]
            elif eid == BLOCKGROUP:
                for cid, cb, ce, cfull in self.children(buf, b, e, strict=False):
                    if cid == BLOCK:
                        yield buf[cb:ce]
            elif eid in LEVEL1:
                return                           # an unknown-size Cluster ends at the next one

    def first_cluster(self, body, bend):
        video = self.video_track()
        if video is None:
            return
        end = self.seg_end if bend is None else bend
        capped = end - body > CLUSTER_CAP
        buf = self.rd.read(body, min(end - body, CLUSTER_CAP))
        if video["encoded"]:
            self.warn("content_encoded_video")
            return
        codec = {"V_MPEG4/ISO/AVC": "avc", "V_MPEGH/ISO/HEVC": "hevc"}.get(video.get("codec_id") or "")
        if codec is None:
            return
        private = video.get("private", b"")
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
        frame = None
        for block in self.blocks(buf):
            track = _vint(block, 0)
            if track is None or track[0] != video["number"] or len(block) < track[1] + 3:
                continue
            if block[track[1] + 2] & 0x06:
                self.warn("laced_block_skipped")
                continue
            frame = block[track[1] + 3:]
            break
        if self.encoder is None and frame is not None:
            for nal in _frame_nals(frame, length_size):
                found = _sei_settings(nal, codec)
                if found:
                    self.set_encoder(found, "first_frame")
                    break
        if frame is None:
            if capped:
                self.warn("cluster_truncated", partial=True)
            else:
                self.warn("no_video_block")
        if self.encoder is None:
            self.warn("no_encoder_settings")

    def set_encoder(self, found, where):
        library, raw = found
        parsed = parse_settings(raw)
        self.encoder = {"library": library, "version": parsed["version"], "where": where,
                        "settings": parsed["options"]}

    # -- output --------------------------------------------------------------------------

    def video_doc(self, video, duration):
        codec_id = video.get("codec_id")
        codec = _codec_name(codec_id)
        private = video.get("private", b"")
        profile = bits = None
        if codec == "h264" and len(private) > 1 and private[0] == 1:
            profile = _AVC_PROFILES.get(private[1])
            bits = _avcc_bit_depth(private) or {66: 8, 77: 8, 88: 8, 100: 8, 110: 10}.get(private[1])
        elif codec == "hevc" and len(private) > 17 and private[0] == 1:
            profile = _HEVC_PROFILES.get(private[1] & 0x1F)
            bits = (private[17] & 7) + 8
        if video.get("bits"):
            bits = video["bits"]
        tags = self.tags.get(video.get("uid"), {}) if video.get("uid") is not None else {}
        fps = None
        dd = video.get("default_duration")
        if dd:
            fps = round(1e9 / dd, 3)
        elif duration and _digits(tags.get("NUMBER_OF_FRAMES")):
            fps = round(_digits(tags["NUMBER_OF_FRAMES"]) / duration, 3)
        bitrate = source = None
        bps = _digits(tags.get("BPS"))
        if bps:
            bitrate, source = round(bps / 1000), "tags"
        elif duration and duration > 0 and self.size > 0:
            bitrate, source = round(self.size * 8 / duration / 1000), "size_over_duration"
        return {"track": video["number"], "codec_id": codec_id, "codec": codec, "profile": profile,
                "bit_depth": bits, "width": video.get("width"), "height": video.get("height"),
                "fps": fps, "bitrate_kbps": bitrate, "bitrate_source": source}

    def tag_duration(self):
        for t in self.tracks:
            value = self.tags.get(t.get("uid"), {}).get("DURATION")
            m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", value or "")
            if m:
                return round(int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), 3)
        return None

    def result(self):
        if self.status is None:
            if self.tracks:
                self.status = "partial" if self.partial else "ok"
            elif self.cut or (self.partial and self.info):
                self.status = "partial"
            else:
                self.status = "invalid"
                self.warn("no_tracks")
        duration = self.info.get("duration_s")
        if duration is None:
            duration = self.tag_duration()
        video = self.video_track() if self.tracks else None
        audio, subs = [], []
        for t in self.tracks:
            if t.get("number") is None:
                continue
            lang = t.get("bcp47") or t.get("lang") or "eng"
            if t.get("type") == 2 and len(audio) < MAX_LIST:
                rate = t.get("out_rate") or t.get("rate")
                audio.append({"track": t["number"], "codec": _codec_name(t.get("codec_id")),
                              "channels": t.get("channels"),
                              "sample_rate": round(rate) if rate else None, "lang": lang,
                              "default": t.get("default", True)})
            elif t.get("type") == 17 and len(subs) < MAX_LIST:
                subs.append({"track": t["number"], "format": _sub_format(t.get("codec_id")),
                             "lang": lang, "name": t.get("name"),
                             "default": t.get("default", True), "forced": t.get("forced", False)})
        return {"schema": SCHEMA, "status": self.status, "container": self.container,
                "bytes_read": self.rd.used, "duration_s": duration,
                "muxing_app": self.info.get("muxing_app"), "writing_app": self.info.get("writing_app"),
                "video": self.video_doc(video, duration) if video else None,
                "encoder": self.encoder, "audio": audio, "subtitles": subs,
                "attachments": self.attach, "chapters": self.chapters,
                "warnings": list(self.warnings)}


def probe(read, size, *, max_bytes=2 << 20):
    """Probe one file. ``read(offset, length)`` returns up to `length` bytes (fewer at EOF).

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
    except (ArithmeticError, ValueError, IndexError, KeyError, TypeError, AttributeError,
            struct.error, UnicodeError, RecursionError):
        p.warn("internal_error", partial=True)
    return p.result()
