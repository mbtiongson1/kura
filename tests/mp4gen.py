"""Synthetic MP4 bytes for tests. Nothing here is a real encode: payloads are filler.

`build(...)` assembles ftyp, moov (one video, optional audio and subtitle tracks) and an mdat that
holds one video sample, with knobs for the layouts and damage the probe must survive. Banners and
names use reserved placeholders only. Fixtures are built in memory, never committed as files.
"""
import struct

import mkvgen


def u16(v):
    return struct.pack(">H", v)


def u32(v):
    return struct.pack(">I", v)


def u64(v):
    return struct.pack(">Q", v)


def box(kind, payload=b"", *, large=False, size0=False):
    if size0:
        return u32(0) + kind + payload
    if large:
        return u32(1) + kind + u64(len(payload) + 16) + payload
    return u32(len(payload) + 8) + kind + payload


def full(kind, payload=b"", version=0, flags=0):
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def mvhd(timescale=1000, duration=1_420_000, v1=False):
    if v1:
        return full(b"mvhd", u64(0) + u64(0) + u32(timescale) + u64(duration) + bytes(80), version=1)
    return full(b"mvhd", u32(0) + u32(0) + u32(timescale) + u32(duration) + bytes(80))


def tkhd(track_id, enabled=True, width=0, height=0, v1=False):
    flags = 3 if enabled else 2
    sizes = u32(width << 16) + u32(height << 16)
    if v1:     # ... id, reserved, duration(8), reserved(8), layer/group/volume/reserved(8), matrix(36)
        body = u64(0) + u64(0) + u32(track_id) + u32(0) + u64(0) + bytes(8) + bytes(8) + bytes(36) + sizes
        return full(b"tkhd", body, version=1, flags=flags)
    body = u32(0) + u32(0) + u32(track_id) + u32(0) + u32(0) + bytes(8) + bytes(8) + bytes(36) + sizes
    return full(b"tkhd", body, flags=flags)


def pack_lang(code):
    a, b, c = (ord(ch) - 0x60 for ch in code)
    return u16((a << 10) | (b << 5) | c)


def mdhd(timescale, duration, lang="und", v1=False):
    if v1:
        return full(b"mdhd", u64(0) + u64(0) + u32(timescale) + u64(duration) + pack_lang(lang) + u16(0),
                    version=1)
    return full(b"mdhd", u32(0) + u32(0) + u32(timescale) + u32(duration) + pack_lang(lang) + u16(0))


def hdlr(kind):
    return full(b"hdlr", u32(0) + kind + bytes(12) + b"Handler\0")


# ---- sample entries ------------------------------------------------------------------------

def visual_entry(fourcc, width, height, children=b""):
    body = bytes(6) + u16(1) + bytes(16) + u16(width) + u16(height) + u32(0x480000) + u32(0x480000) \
        + u32(0) + u16(1) + bytes(32) + u16(24) + u16(0xFFFF)
    return box(fourcc, body + children)


def audio_entry(fourcc, channels=2, rate=48000, children=b"", version=0):
    body = bytes(6) + u16(1) + u16(version) + u16(0) + u32(0) + u16(channels) + u16(16) + u16(0) + u16(0) \
        + u32(rate << 16)
    if version == 1:
        body += bytes(16)
    return box(fourcc, body + children)


def esds(object_type=0x40):
    dec = bytes([0x04, 13, object_type, 0x15]) + bytes(11)
    return full(b"esds", bytes([0x03, 3 + len(dec), 0, 1, 0]) + dec)


def stsd(entry):
    return full(b"stsd", u32(1) + entry)


def stts(entries):
    return full(b"stts", u32(len(entries)) + b"".join(u32(c) + u32(d) for c, d in entries))


def stsz(count, first=0, constant=0):
    if constant:
        return full(b"stsz", u32(constant) + u32(count))
    return full(b"stsz", u32(0) + u32(count) + (u32(first) if count else b"") + bytes(4 * max(count - 1, 0)))


def stco(offset, *, wide=False):
    if wide:
        return full(b"co64", u32(1) + u64(offset))
    return full(b"stco", u32(1) + u32(offset))


def avcc(profile=100, length_size=4, bit_depth=None):
    return box(b"avcC", mkvgen.avcc(profile, length_size, bit_depth))


def hvcc(bit_depth=10, length_size=4, nals=()):
    return box(b"hvcC", mkvgen.hvcc(bit_depth, length_size, nals))


def av1c(bit_depth=10, profile=0):
    high, twelve = bit_depth > 8, bit_depth == 12
    return box(b"av1C", bytes([0x81, profile << 5 | 8, (0x40 if high else 0) | (0x20 if twelve else 0), 0]))


def trak(track_id, handler, entry, *, lang="und", timescale=1000, duration=1_420_000, enabled=True,
         width=0, height=0, samples=2, delta=None, first=64, offset=0, wide=False, refs=b"", v1=False,
         tables=True):
    stbl = stsd(entry)
    if tables:
        stbl += stts([(samples, delta or 1)]) + stsz(samples, first) + stco(offset, wide=wide)
    mdia = box(b"mdia", mdhd(timescale, duration, lang, v1) + hdlr(handler) + box(b"minf", box(b"stbl", stbl)))
    return box(b"trak", tkhd(track_id, enabled, width, height, v1) + refs + mdia)


def ilst_tool(tool):
    data = box(b"data", u32(1) + u32(0) + tool.encode())
    return box(b"meta", u32(0) + hdlr(b"mdir") + box(b"ilst", box(b"\xa9too", data)))


def chpl(count):
    return full(b"chpl", bytes(4) + bytes([count]) + b"".join(bytes(9) + b"\x01c" for _ in range(count)),
                version=1)


FTYP = box(b"ftyp", b"isom" + u32(512) + b"isomiso2avc1mp41")


def build(*, codec="avc", settings=mkvgen.X264_TEXT, sei="frame", faststart=True, audio=((b"mp4a", "jpn"),),
          subs=((b"tx3g", "eng"),), mdat="normal", wide=False, duration_s=1420, hevc_bits=10, avc_profile=100,
          avc_bit_depth=None, tool="Lavf59.27.100", tool_style="ilst", chapters=0, chapter_track=False,
          fragmented=False, v1=False, extra_moov=b"", extra_top=b"", mdat_pad=0, esds_type=0x40,
          audio_channels=2, video_enabled=True):
    """The bytes of a small, valid file. `sei` is "frame", "private" or None; `mdat` is
    "normal", "large" (64-bit largesize) or "size0" (to the end of the file)."""
    uuid = mkvgen.X264_UUID if codec == "avc" else mkvgen.X265_UUID
    nal_codec = "avc" if codec == "avc" else "hevc"
    messages = [mkvgen.encoder_message(settings, uuid)] if settings is not None else []
    sei_unit = mkvgen.sei_nal(nal_codec, messages)
    nals = [mkvgen.slice_nal(nal_codec)]
    private_nals = []
    if sei == "frame":
        nals.insert(0, sei_unit)
    elif sei == "private":
        private_nals = [sei_unit]
    sample = mkvgen.frame(nals)
    if codec == "avc":
        entry = visual_entry(b"avc1", 1920, 1080, avcc(avc_profile, bit_depth=avc_bit_depth))
    elif codec == "hevc":
        entry = visual_entry(b"hvc1", 1920, 1080, hvcc(hevc_bits, nals=private_nals))
    else:
        entry = visual_entry(b"av01", 1920, 1080, av1c(hevc_bits))
    mdat_body = sample + bytes(mdat_pad)

    def moov(offset):
        ref = box(b"tref", box(b"chap", u32(90))) if chapter_track else b""
        traks = trak(1, b"vide", entry, timescale=24000, duration=duration_s * 24000, width=1920, height=1080,
                     delta=1001, first=len(sample), offset=offset, wide=wide, enabled=video_enabled,
                     refs=ref, v1=v1)
        for k, (fourcc, lang) in enumerate(audio):
            kids = esds(esds_type) if fourcc == b"mp4a" else b""
            traks += trak(10 + k, b"soun", audio_entry(fourcc, audio_channels, 48000, kids), lang=lang,
                          timescale=48000, duration=duration_s * 48000, enabled=(k == 0), offset=offset)
        for k, (fourcc, lang) in enumerate(subs):
            traks += trak(30 + k, b"sbtl", box(fourcc, bytes(6) + u16(1)), lang=lang, enabled=(k == 0),
                          offset=offset)
        if chapter_track:
            traks += trak(90, b"text", box(b"text", bytes(6) + u16(1)), samples=5, offset=offset)
        kids = b""
        if chapters:
            kids += chpl(chapters)
        if tool:
            kids += box(b"\xa9too", u16(len(tool)) + u16(0x55C4) + tool.encode()) if tool_style == "qt" \
                else ilst_tool(tool)
        udta = box(b"udta", kids) if kids else b""
        mvex = box(b"mvex", full(b"mehd", u32(duration_s * 1000))) if fragmented else b""
        return box(b"moov", mvhd(1000, 0 if fragmented else duration_s * 1000, v1) + traks + udta + mvex
                   + extra_moov)

    def mdat_box():
        return box(b"mdat", mdat_body, large=(mdat == "large"), size0=(mdat == "size0"))

    head = len(FTYP) + len(extra_top) + (16 if mdat == "large" else 8)
    if faststart:
        return FTYP + extra_top + moov(head + len(moov(0))) + mdat_box()
    return FTYP + extra_top + mdat_box() + moov(head)
