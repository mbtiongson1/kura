"""Synthetic Matroska bytes for tests. Nothing here is a real encode: payloads are filler.

`build(...)` assembles EBML header, SeekHead, Info, Tracks, optional Attachments/Chapters,
one Cluster and optional Tags, with knobs for the cases the probe must survive. Banners use
reserved names only. Fixtures never touch Git as files; tests build them in memory or in a
temporary directory.
"""
import struct

X264_UUID = bytes.fromhex("DC45E9BDE6D948B7962CD820D923EEEF")
X265_UUID = bytes.fromhex("2CA2DE09B51747DBBB55A4FE7FC2FC4E")
X264_TEXT = ("x264 - core 164 r3095 baee400 - H.264/MPEG-4 AVC codec - Copyleft 2003-2022 - "
             "http://codec.example/x264.html - options: cabac=1 ref=4 deblock=1:-1:-1 analyse=0x3:0x113 "
             "me=umh subme=9 psy=1 psy_rd=1.00:0.00 bframes=5 weightp=2 keyint=240 crf=18.0 qcomp=0.60 "
             "aq=1:1.00")
X265_TEXT = ("x265 (build 199) - 3.5+1-f0c1022b6:[Linux][GCC 11.2.0][64 bit] 10bit - H.265/HEVC codec - "
             "Copyright 2013-2018 (c) Example Codec Authors - http://codec.example/x265 - options: "
             "cpuid=1111039 frame-threads=3 wpp no-pmode no-psnr log-level=2 input-res=1920x1080 "
             "preset=slow crf=19.0 ref=5 sao no-open-gop")
UNKNOWN8 = b"\x01\xff\xff\xff\xff\xff\xff\xff"        # 8-byte size, all value bits set


# ---- EBML primitives -----------------------------------------------------------------------

def vint(n, width=None):
    """Size-style vint (marker bit set, value `n`) in the smallest or the given width."""
    width = width or next(w for w in range(1, 9) if n < (1 << (7 * w)) - 1)
    return ((1 << (7 * width)) | n).to_bytes(width, "big")


def eid(i):
    return i.to_bytes((i.bit_length() + 7) // 8, "big")


def el(i, payload=b"", *, unknown=False, size_width=None):
    return eid(i) + (UNKNOWN8 if unknown else vint(len(payload), size_width)) + payload


def uint(i, v, width=None):
    width = width or max(1, (v.bit_length() + 7) // 8)
    return el(i, v.to_bytes(width, "big"))


def text(i, s):
    return el(i, s.encode("utf-8"))


def flt(i, v, width=8):
    return el(i, struct.pack(">d" if width == 8 else ">f", v))


# ---- codec configuration -------------------------------------------------------------------

def avcc(profile=100, length_size=4, bit_depth=None):
    """avcC; high profiles get the bit-depth extension only when `bit_depth` is given."""
    sps = bytes([0x67, profile, 0x00, 0x28, 0xAC])
    out = (bytes([1, profile, 0, 40, 0xFC | (length_size - 1), 0xE1]) + struct.pack(">H", len(sps)) + sps
           + bytes([1]) + struct.pack(">H", 2) + b"\x68\xee")
    if bit_depth:
        out += bytes([0xFD, 0xF8 | (bit_depth - 8), 0xF8 | (bit_depth - 8), 0])
    return out


def hvcc(bit_depth=10, length_size=4, nals=()):
    head = bytes([1, 0x02, 0x20, 0, 0, 0, 0x90, 0, 0, 0, 0, 0, 120, 0xF0, 0, 0xFC, 0xFD,
                  0xF8 | (bit_depth - 8), 0xF8 | (bit_depth - 8), 0, 0, (1 << 3) | (length_size - 1)])
    by_type = {}
    for nal in nals:
        by_type.setdefault((nal[0] >> 1) & 0x3F, []).append(nal)
    arrays = b"".join(bytes([0x80 | t]) + struct.pack(">H", len(group))
                      + b"".join(struct.pack(">H", len(n)) + n for n in group)
                      for t, group in by_type.items())
    return head + bytes([len(by_type)]) + arrays


def escape(rbsp):
    """Insert emulation-prevention bytes the way an encoder does."""
    out, zeros = bytearray(), 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def sei_message(ptype, payload):
    def ext(n):
        return b"\xff" * (n // 255) + bytes([n % 255])
    return ext(ptype) + ext(len(payload)) + payload


def encoder_message(text_, uuid=X264_UUID):
    return sei_message(5, uuid + text_.encode("latin-1") + b"\0")


def sei_nal(codec, messages):
    """An SEI NAL unit for `codec` ("avc"|"hevc") carrying already-encoded SEI messages."""
    header = bytes([0x06]) if codec == "avc" else bytes([39 << 1, 1])
    return header + escape(b"".join(messages) + b"\x80")


def slice_nal(codec, size=64):
    header = bytes([0x65]) if codec == "avc" else bytes([19 << 1, 1])
    return header + bytes(size)


def frame(nals, length_size=4):
    return b"".join(len(n).to_bytes(length_size, "big") + n for n in nals)


# ---- blocks, tracks, other elements --------------------------------------------------------

def simple_block(track, payload, flags=0x80, tc=0):
    return el(0xA3, vint(track) + struct.pack(">h", tc) + bytes([flags]) + payload)


def block_group(track, payload, flags=0, tc=0):
    return el(0xA0, el(0xA1, vint(track) + struct.pack(">h", tc) + bytes([flags]) + payload))


def track_entry(number, ttype, codec_id, *, uid=None, default=None, forced=None, lang=None, name=None,
                private=None, default_duration=None, video=None, audio=None, encoded=False):
    b = uint(0xD7, number) + uint(0x73C5, uid if uid is not None else 1000 + number) + uint(0x83, ttype)
    if default is not None:
        b += uint(0x88, int(default))
    if forced is not None:
        b += uint(0x55AA, int(forced))
    if default_duration:
        b += uint(0x23E383, default_duration, 4)
    if name is not None:
        b += text(0x536E, name)
    if lang is not None:
        b += text(0x22B59C, lang)
    b += text(0x86, codec_id)
    if private is not None:
        b += el(0x63A2, private)
    if encoded:
        b += el(0x6D80, el(0x6240, uint(0x5031, 0)))
    if video:
        v = uint(0xB0, video.get("width", 1920)) + uint(0xBA, video.get("height", 1080))
        if video.get("bits"):
            v += el(0x55B0, uint(0x55B2, video["bits"]))
        b += el(0xE0, v)
    if audio:
        a = flt(0xB5, audio.get("rate", 48000.0)) + uint(0x9F, audio.get("channels", 2))
        if audio.get("bit_depth"):
            a += uint(0x6264, audio["bit_depth"])
        b += el(0xE1, a)
    return el(0xAE, b)


def info(duration=1420.0, scale=1_000_000, muxing="Kura Mux 1.0", writing="Kura Writer 1.0"):
    b = uint(0x2AD7B1, scale) + text(0x4D80, muxing) + text(0x5741, writing)
    if duration is not None:
        b += flt(0x4489, duration * 1e9 / scale)
    return el(0x1549A966, b)


def attachment(name, mime, size):
    return el(0x61A7, text(0x466E, name) + text(0x4660, mime) + uint(0x46AE, 7, 2) + el(0x465C, bytes(size)))


def attachments(items):
    return el(0x1941A469, b"".join(attachment(*i) for i in items))


def chapters(n):
    atoms = b"".join(el(0xB6, uint(0x73C4, i + 1) + uint(0x91, i * 1_000_000_000)) for i in range(n))
    return el(0x1043A770, el(0x45B9, atoms))


def nested_chapters(depth):
    atom = el(0xB6, uint(0x73C4, 1))
    for _ in range(depth):
        atom = el(0xB6, uint(0x73C4, 1) + atom)
    return el(0x1043A770, el(0x45B9, atom))


def tags(per_track):
    """`per_track` maps TrackUID -> {tag name: string}."""
    out = b""
    for uid, values in per_track.items():
        simple = b"".join(el(0x67C8, text(0x45A3, k) + text(0x4487, v)) for k, v in values.items())
        out += el(0x7373, el(0x63C0, uint(0x63C5, uid)) + simple)
    return el(0x1254C367, out)


def seekhead(entries):
    """`entries` is [(element id, position relative to the Segment body)] with fixed-width positions."""
    return el(0x114D9B74, b"".join(
        el(0x4DBB, el(0x53AB, eid(i)) + el(0x53AC, pos.to_bytes(4, "big"))) for i, pos in entries))


def ebml(doc_type="matroska"):
    return el(0x1A45DFA3, uint(0x4286, 1) + text(0x4282, doc_type) + uint(0x4287, 4) + uint(0x4285, 2))


def wrap(segment_body, unknown=False):
    """EBML header plus a Segment around hand-made level-1 bytes."""
    return ebml() + el(0x18538067, segment_body, unknown=unknown)


# ---- whole files ---------------------------------------------------------------------------

def build(*, codec="avc", settings=X264_TEXT, sei="frame", extra_messages=(), colour_bits=None,
          audio_bit_depth=None, avc_profile=100, avc_bit_depth=None, hevc_bits=10, subs=(("S_TEXT/ASS", "eng", None),),
          audio_codecs=("A_AAC",), attach=(), chapter_atoms=0, tag_values=None, duration=1420.0,
          doc_type="matroska", video_number=1, audio_first=True, decoy_audio=False,
          laced_decoy_video=False, unknown_segment=False, unknown_cluster=False, with_seekhead=True,
          content_encoded=False, default_duration=41_708_333, extra_level1=b""):
    """Return the bytes of a small, valid file. `sei` is "frame", "private" or None."""
    uuid = X264_UUID if codec == "avc" else X265_UUID
    nal_codec = "avc" if codec == "avc" else "hevc"
    messages = [*extra_messages]
    if settings is not None:
        messages.append(encoder_message(settings, uuid))
    sei_unit = sei_nal(nal_codec, messages)
    nals = [slice_nal(nal_codec)]
    private_nals = []
    if sei == "frame":
        nals.insert(0, sei_unit)
    elif sei == "private":
        private_nals = [sei_unit]
    codec_id, private = (("V_MPEG4/ISO/AVC", avcc(avc_profile, bit_depth=avc_bit_depth)) if codec == "avc"
                         else ("V_MPEGH/ISO/HEVC", hvcc(hevc_bits, nals=private_nals)))
    audio_number = 2 if video_number == 1 else 1
    tracks = track_entry(video_number, 1, codec_id, uid=1001, private=private, default_duration=default_duration,
                         video={"bits": colour_bits}, encoded=content_encoded)
    for k, a in enumerate(audio_codecs):
        tracks += track_entry(audio_number + 10 * k, 2, a, uid=2000 + k, lang="jpn", default=(k == 0),
                              audio={"bit_depth": audio_bit_depth})
    for k, (sub_codec, lang, name) in enumerate(subs):
        tracks += track_entry(30 + k, 17, sub_codec, uid=3000 + k, lang=lang, name=name, default=False,
                              forced=(k == 1))
    tracks = el(0x1654AE6B, tracks)

    blocks = b""
    if audio_first:
        decoy = frame([sei_nal("avc", [encoder_message("x264 - core 0 - DECOY - options: crf=99")])]) \
            if decoy_audio else b"\x21\x00\x01"
        blocks += simple_block(audio_number, decoy)
    if laced_decoy_video:
        blocks += simple_block(video_number, frame([sei_nal(nal_codec, [encoder_message("x264 - core 0 - LACED - "
                                                                                       "options: crf=98", uuid)])]),
                               flags=0x82)
    blocks += simple_block(video_number, frame(nals))
    blocks += block_group(audio_number, b"\x21\x00\x02")
    cluster_body = uint(0xE7, 0) + blocks
    cluster = el(0x1F43B675, cluster_body, unknown=unknown_cluster)

    parts = {"info": info(duration), "tracks": tracks}
    if attach:
        parts["attachments"] = attachments(attach)
    if chapter_atoms:
        parts["chapters"] = chapters(chapter_atoms)
    ids = {"info": 0x1549A966, "tracks": 0x1654AE6B, "attachments": 0x1941A469, "chapters": 0x1043A770}
    tag_el = tags(tag_values) if tag_values else None

    if extra_level1:
        parts["extra"] = extra_level1
    order = [*parts.items(), ("cluster", cluster)] + ([("tags", tag_el)] if tag_el else [])
    ids.update({"cluster": 0x1F43B675, "tags": 0x1254C367})
    head_len = len(seekhead([(ids[k], 0) for k, _ in order if k in ids])) if with_seekhead else 0
    pos, entries, body = head_len, [], b""
    for key, data in order:
        if key in ids:
            entries.append((ids[key], pos))
        pos += len(data)
        body += data
    seg_body = (seekhead(entries) if with_seekhead else b"") + body
    return ebml(doc_type) + el(0x18538067, seg_body, unknown=unknown_segment)
