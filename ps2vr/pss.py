"""Sony PSS / MPEG-2 program stream analysis and blank-stream generation.

A PSS file is an MPEG-2 program stream:
  * video  : stream 0xE0, MPEG-2 video (decoded by the PS2 IPU via libmpeg)
  * audio  : private_stream_1 (0xBD), sub stream 0xA0.., beginning with an
             'SShd' header (format, rate, channels, interleave) followed by an
             'SSbd' block containing PCM16 or SPU2 ADPCM data
  * padding: 0xBE

The generator re-uses the original file's pack size, PES size, system header
and video sequence header so the replacement looks to the game exactly like
the original, only much shorter.
"""

from dataclasses import dataclass, field
import struct

from . import mpeg2

PACK = 0xBA
SYSTEM = 0xBB
END = 0xB9
PADDING = 0xBE
PRIVATE1 = 0xBD


def _read_ts(b):
    return (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | \
           (b[3] << 7) | (b[4] >> 1)


def _write_ts(prefix, ts):
    ts &= (1 << 33) - 1
    return bytes([
        (prefix << 4) | (((ts >> 30) & 0x07) << 1) | 1,
        (ts >> 22) & 0xFF,
        (((ts >> 15) & 0x7F) << 1) | 1,
        (ts >> 7) & 0xFF,
        ((ts & 0x7F) << 1) | 1,
    ])


def _pack_header(scr, mux_rate):
    base = scr & ((1 << 33) - 1)
    ext = 0
    b = bytearray(b"\x00\x00\x01\xba")
    b.append(0x40 | (((base >> 30) & 0x07) << 3) | 0x04 | ((base >> 28) & 0x03))
    b.append((base >> 20) & 0xFF)
    b.append((((base >> 15) & 0x1F) << 3) | 0x04 | ((base >> 13) & 0x03))
    b.append((base >> 5) & 0xFF)
    b.append(((base & 0x1F) << 3) | 0x04 | ((ext >> 7) & 0x03))
    b.append(((ext & 0x7F) << 1) | 1)
    b.append((mux_rate >> 14) & 0xFF)
    b.append((mux_rate >> 6) & 0xFF)
    b.append(((mux_rate & 0x3F) << 2) | 0x03)
    b.append(0xF8)  # reserved + pack_stuffing_length 0
    return bytes(b)


@dataclass
class AudioInfo:
    substream: int = 0xA0
    prefix: bytes = b"\xa0\x00\x00"   # bytes preceding audio data in each PES
    fmt: int = 1
    rate: int = 48000
    channels: int = 2
    interleave: int = 0x200
    data_size: int = 0
    shd: bytes = b""                  # full SShd chunk

    @property
    def codec(self):
        return {0: "PCM16LE", 1: "PCM16", 0x10: "SPU2-ADPCM"}.get(self.fmt, "fmt 0x%x" % self.fmt)

    @property
    def byte_rate(self):
        if self.fmt == 0x10:   # 16-byte ADPCM frames hold 28 samples
            return self.rate * self.channels * 16 / 28
        return self.rate * self.channels * 2


@dataclass
class PssInfo:
    ok: bool = False
    reason: str = ""
    pack_size: int = 2048
    max_pes: int = 2048
    mux_rate: int = 0
    first_scr: int = 0
    system_header: bytes = b""
    video_hdr: bytes = b""            # first video PES header (incl. PTS/DTS)
    audio_hdr: bytes = b""            # first audio PES header
    seq: mpeg2.SequenceInfo = None
    audio: AudioInfo = None
    first_pts: int = None
    first_dts: int = None
    first_apts: int = None
    last_pts: int = None
    streams: dict = field(default_factory=dict)

    @property
    def duration(self):
        if self.first_pts is not None and self.last_pts is not None and self.seq:
            return (self.last_pts - self.first_pts) / 90000 + 1 / self.seq.fps
        if self.audio and self.audio.data_size:
            return self.audio.data_size / self.audio.byte_rate
        return None


def iter_packets(data, start=0):
    """Yield (offset, code, length) for each pack/packet in a program stream."""
    i = start
    n = len(data)
    while i + 4 <= n:
        if data[i:i + 3] != b"\x00\x00\x01":
            # resync
            j = data.find(b"\x00\x00\x01", i + 1)
            if j < 0:
                return
            i = j
            continue
        code = data[i + 3]
        if code == PACK:
            if i + 14 > n:
                return
            if data[i + 4] >> 6 == 1:
                ln = 14 + (data[i + 13] & 7)
            else:
                ln = 12   # MPEG-1 pack
            yield i, code, ln
            i += ln
        elif code == END:
            yield i, code, 4
            i += 4
        elif code >= 0xBB:
            if i + 6 > n:
                return
            ln = 6 + struct.unpack(">H", data[i + 4:i + 6])[0]
            yield i, code, ln
            i += ln
        else:
            i += 1


def _pes_payload(pkt):
    """Return (header_len, pts, dts) for an MPEG-2 PES packet."""
    if len(pkt) < 9 or pkt[6] >> 6 != 2:
        return 6, None, None
    flags = pkt[7]
    hl = pkt[8]
    pts = dts = None
    if flags & 0x80:
        pts = _read_ts(pkt[9:14])
    if flags & 0x40:
        dts = _read_ts(pkt[14:19])
    return 9 + hl, pts, dts


def analyze(head: bytes, tail: bytes = b""):
    """Analyse the start (and optionally the end) of a PSS/MPEG-PS file."""
    info = PssInfo()
    if head[:4] != b"\x00\x00\x01\xba":
        info.reason = "not an MPEG program stream"
        return info
    if head[4] >> 6 != 1:
        info.reason = "MPEG-1 program stream (Sofdec/MPEG-1) - not supported"
        return info

    pack_offsets = []
    es = bytearray()
    for off, code, ln in iter_packets(head):
        pkt = head[off:off + ln]
        if code == PACK:
            pack_offsets.append(off)
            if len(pack_offsets) == 1:
                b = pkt
                info.mux_rate = (b[10] << 14) | (b[11] << 6) | (b[12] >> 2)
                info.first_scr = (((b[4] >> 3) & 7) << 30) | ((b[4] & 3) << 28) | \
                    (b[5] << 20) | ((b[6] >> 3) << 15) | ((b[6] & 3) << 13) | \
                    (b[7] << 5) | (b[8] >> 3)
            continue
        if code == END:
            break
        info.streams[code] = info.streams.get(code, 0) + 1
        if code == SYSTEM:
            info.system_header = bytes(pkt)
            continue
        hl, pts, dts = _pes_payload(pkt)
        if 0xE0 <= code <= 0xEF:
            if not info.video_hdr:
                info.video_hdr = bytes(pkt[:hl])
                info.first_pts, info.first_dts = pts, dts
            if len(es) < 65536:
                es += pkt[hl:]
        elif code == PRIVATE1 and info.audio is None:
            payload = pkt[hl:]
            k = payload.find(b"SShd")
            if 0 <= k <= 8:
                a = AudioInfo()
                a.prefix = bytes(payload[:k])
                a.substream = payload[0]
                sz = struct.unpack("<I", payload[k + 4:k + 8])[0]
                body = payload[k + 8:k + 8 + sz]
                a.fmt, a.rate, a.channels, a.interleave = struct.unpack("<IIII", body[:16])
                a.shd = bytes(payload[k:k + 8 + sz])
                bd = payload.find(b"SSbd", k)
                if bd >= 0:
                    a.data_size = struct.unpack("<I", payload[bd + 4:bd + 8])[0]
                info.audio = a
                info.audio_hdr = bytes(pkt[:hl])
                info.first_apts = pts

    if len(pack_offsets) >= 2:
        info.pack_size = pack_offsets[1] - pack_offsets[0]
    info.max_pes = 0
    for off, code, ln in iter_packets(head):
        if code not in (PACK, SYSTEM, END):
            info.max_pes = max(info.max_pes, ln)
    info.seq = mpeg2.parse_sequence(bytes(es))

    # last PTS from tail of file
    for off, code, ln in iter_packets(tail):
        if 0xE0 <= code <= 0xEF and off + ln <= len(tail):
            _, pts, _ = _pes_payload(tail[off:off + ln])
            if pts is not None and (info.last_pts is None or pts > info.last_pts):
                info.last_pts = pts

    if info.seq is None:
        info.reason = "no MPEG-2 video sequence header found"
    elif not info.video_hdr:
        info.reason = "no video stream"
    else:
        info.ok = True
    return info


def build_blank(info: PssInfo, duration: float = 1.0, luma: int = 16):
    """Build a blank replacement PSS mirroring `info`'s structure."""
    seq = info.seq
    fps = seq.fps
    frames = max(1, int(round(duration * fps)))
    duration = frames / fps
    es, pic_offsets = mpeg2.blank_stream(seq, frames, luma)
    period = 90000 / fps

    pts0 = info.first_pts if info.first_pts is not None else 90000
    dts0 = info.first_dts if info.first_dts is not None else pts0 - int(period)

    # ---- chunk the video ES into PES packets -----------------------------
    pack_size = info.pack_size if info.pack_size >= 2048 else 2048
    max_pes = min(info.max_pes or 2048, pack_size - 14 - len(info.system_header), 65535 + 6)
    max_pes = max(max_pes, 512)

    units = []  # (dts_for_ordering, stream, header_bytes, payload)
    pos = 0
    first = True
    while pos < len(es):
        # does a picture start inside this chunk?
        hdr_full = 9 + 10
        cap = max_pes - hdr_full
        chunk_end = min(len(es), pos + cap)
        pics = [k for k, o in enumerate(pic_offsets) if pos <= o < chunk_end]
        if first and info.video_hdr:
            hdr = bytearray(info.video_hdr)
            cap = max_pes - len(hdr)
            chunk_end = min(len(es), pos + cap)
            order = dts0
        elif pics:
            k = pics[0]
            pts = int(pts0 + k * period)
            dts = int(dts0 + k * period)
            hdr = bytearray(b"\x00\x00\x01\xe0\x00\x00\x81\xc0\x0a")
            hdr += _write_ts(3, pts) + _write_ts(1, dts)
            order = dts
        else:
            hdr = bytearray(b"\x00\x00\x01\xe0\x00\x00\x81\x00\x00")
            chunk_end = min(len(es), pos + max_pes - len(hdr))
            order = units[-1][0] if units else dts0
        payload = es[pos:chunk_end]
        units.append((order, 0xE0, bytes(hdr), payload, b""))
        pos = chunk_end
        first = False

    # ---- silent audio ------------------------------------------------------
    a = info.audio
    if a is not None:
        block = max(1, a.interleave) * max(1, a.channels)
        nbytes = int(duration * a.byte_rate)
        nbytes = max(block, (nbytes + block - 1) // block * block)
        audio = bytearray(a.shd)
        audio += b"SSbd" + struct.pack("<I", nbytes)
        audio += bytes(nbytes)
        apts0 = info.first_apts if info.first_apts is not None else pts0
        apos = 0
        hdr_len = len(info.audio_hdr) if info.audio_hdr else 14
        cap = max_pes - hdr_len - len(a.prefix)
        while apos < len(audio):
            data = audio[apos:apos + cap]
            # timestamp of first audio byte in this packet
            consumed = max(0, apos - len(a.shd) - 8)
            pts = int(apts0 + consumed / a.byte_rate * 90000)
            if info.audio_hdr:
                hdr = bytearray(info.audio_hdr)
                hdr[9:14] = _write_ts(2, pts)
            else:
                hdr = bytearray(b"\x00\x00\x01\xbd\x00\x00\x81\x80\x05") + _write_ts(2, pts)
            units.append((pts - 1, PRIVATE1, bytes(hdr), bytes(data), bytes(a.prefix)))
            apos += len(data)

    units.sort(key=lambda u: u[0])

    # ---- multiplex into packs ---------------------------------------------
    out = bytearray()
    mux_rate = info.mux_rate or 25000
    scr = info.first_scr
    first_pack = True
    ui = 0
    while ui < len(units) or first_pack:
        pack = bytearray(_pack_header(scr, mux_rate))
        if first_pack and info.system_header:
            pack += info.system_header
        first_pack = False
        while ui < len(units):
            remaining = pack_size - len(pack)
            order, sid, hdr, payload, prefix = units[ui]
            need = len(hdr) + len(prefix) + len(payload)
            if need <= remaining:
                if 0 < remaining - need < 6:
                    # cannot fit a padding packet afterwards - split instead
                    cut = len(payload) - 6
                    units.insert(ui + 1, (order, sid, _cont_header(sid, hdr), payload[cut:], prefix))
                    payload = payload[:cut]
                pkt = bytearray(hdr) + prefix + payload
                pkt[4:6] = struct.pack(">H", len(pkt) - 6)
                pack += pkt
                ui += 1
                continue
            # split the payload across packs
            room = min(remaining, max_pes) - len(hdr) - len(prefix)
            if room < 64:
                break
            pkt = bytearray(hdr) + prefix + payload[:room]
            pkt[4:6] = struct.pack(">H", len(pkt) - 6)
            pack += pkt
            units[ui] = (order, sid, _cont_header(sid, hdr), payload[room:], prefix)
            if remaining - len(pkt) >= 64 + len(hdr):
                continue
            break
        remaining = pack_size - len(pack)
        if remaining:
            pad = bytearray(b"\x00\x00\x01\xbe") + struct.pack(">H", remaining - 6)
            pad += b"\xff" * (remaining - 6)
            pack += pad
        out += pack
        scr += int(pack_size * 90000 / (mux_rate * 50))
    out += b"\x00\x00\x01\xb9"
    return bytes(out)


def _cont_header(sid, hdr):
    """PES header for a continuation packet (no timestamps)."""
    if sid == PRIVATE1:
        # keep header length identical so the game's parser sees the same layout
        h = bytearray(hdr)
        hl = h[8]
        h[7] = 0x00
        h[9:9 + hl] = b"\xff" * hl
        return bytes(h)
    return bytes(b"\x00\x00\x01" + bytes([sid]) + b"\x00\x00\x81\x00\x00")
