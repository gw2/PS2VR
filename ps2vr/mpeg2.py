"""Minimal MPEG-2 video elementary stream synthesis.

Generates a valid MPEG-2 (Main Profile) elementary stream consisting of one
intra-coded black picture followed by P pictures in which every macroblock is
"motion compensated, not coded" with a zero vector (i.e. a repeat of the
previous frame). This is the smallest legal way to express N frames of a solid
colour, and it decodes on the PS2 IPU / sceMpeg library like any other stream.

Only the pieces of the bitstream syntax we actually emit are implemented.
"""

from dataclasses import dataclass
import struct


class BitWriter:
    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, value, nbits):
        for i in range(nbits - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> i) & 1)
            self.nbits += 1
            if self.nbits == 8:
                self.buf.append(self.acc)
                self.acc = 0
                self.nbits = 0

    def bits(self, s):
        """Write a string of '0'/'1' characters."""
        for ch in s:
            if ch in "01":
                self.write(1 if ch == "1" else 0, 1)

    def align(self):
        if self.nbits:
            self.write(0, 8 - self.nbits)

    def start_code(self, code):
        self.align()
        self.buf += b"\x00\x00\x01" + bytes([code])

    def getvalue(self):
        self.align()
        return bytes(self.buf)


# ISO/IEC 13818-2 Table B.1 - macroblock_address_increment
_MBA_VLC = {
    1: "1", 2: "011", 3: "010", 4: "0011", 5: "0010", 6: "00011", 7: "00010",
    8: "0000111", 9: "0000110", 10: "00001011", 11: "00001010", 12: "00001001",
    13: "00001000", 14: "00000111", 15: "00000110", 16: "0000010111",
    17: "0000010110", 18: "0000010101", 19: "0000010100", 20: "0000010011",
    21: "0000010010", 22: "00000100011", 23: "00000100010", 24: "00000100001",
    25: "00000100000", 26: "00000011111", 27: "00000011110", 28: "00000011101",
    29: "00000011100", 30: "00000011011", 31: "00000011010", 32: "00000011001",
    33: "00000011000",
}
_MBA_ESCAPE = "00000001000"

# Table B.12 - dct_dc_size_luminance
_DC_LUMA_SIZE = ["100", "00", "01", "101", "110", "1110", "11110", "111110",
                 "1111110", "11111110", "111111110", "111111111"]
# Table B.13 - dct_dc_size_chrominance
_DC_CHROMA_SIZE = ["00", "01", "10", "110", "1110", "11110", "111110",
                   "1111110", "11111110", "111111110", "1111111110",
                   "1111111111"]

# Frame rate codes (Table 6-4)
FRAME_RATES = {1: 24000 / 1001, 2: 24.0, 3: 25.0, 4: 30000 / 1001, 5: 30.0,
               6: 50.0, 7: 60000 / 1001, 8: 60.0}


def _write_mba(bw, inc):
    while inc > 33:
        bw.bits(_MBA_ESCAPE)
        inc -= 33
    bw.bits(_MBA_VLC[inc])


def _write_dc(bw, diff, luma):
    size = 0 if diff == 0 else abs(diff).bit_length()
    bw.bits((_DC_LUMA_SIZE if luma else _DC_CHROMA_SIZE)[size])
    if size:
        bw.write(diff if diff > 0 else diff + (1 << size) - 1, size)


@dataclass
class SequenceInfo:
    width: int
    height: int
    frame_rate_code: int
    aspect_code: int = 2
    progressive: bool = False
    raw_headers: bytes = b""   # sequence header + extensions copied verbatim
    pce: dict = None           # picture coding extension flags of the original

    @property
    def fps(self):
        return FRAME_RATES.get(self.frame_rate_code, 30.0)

    @property
    def mb_width(self):
        return (self.width + 15) // 16

    @property
    def mb_height(self):
        # Frame pictures of interlaced sequences are coded in 32-line units
        if self.progressive:
            return (self.height + 15) // 16
        return 2 * ((self.height + 31) // 32)


def parse_sequence(es: bytes):
    """Find the first sequence header (+ its extensions) in an ES chunk."""
    i = es.find(b"\x00\x00\x01\xb3")
    if i < 0 or len(es) < i + 12:
        return None
    b = es[i + 4:i + 8]
    width = (b[0] << 4) | (b[1] >> 4)
    height = ((b[1] & 0x0F) << 8) | b[2]
    aspect = b[3] >> 4
    frc = b[3] & 0x0F
    # Copy everything up to the GOP / picture start code
    end = i + 4
    progressive = True
    mpeg2 = False
    while True:
        j = es.find(b"\x00\x00\x01", end)
        if j < 0:
            return None
        code = es[j + 3]
        if code == 0xB5:
            ext_id = es[j + 4] >> 4
            if ext_id == 1:
                mpeg2 = True
                progressive = bool(es[j + 5] & 0x08)
            end = j + 4
            continue
        if code in (0xB2,):  # user data - skip but keep looking
            end = j + 4
            continue
        break
    raw = es[i:j]
    if not mpeg2:
        return None
    return SequenceInfo(width, height, frc, aspect, progressive, raw, _first_pce(es, j))


_START = bytes([0, 0, 1])
_PIC_START = _START + bytes([0])


def _first_pce(es, start):
    """Flags of the first picture coding extension. Some players set up the IPU
    from the original movie's flags, so the blank must use the same ones."""
    k = es.find(_PIC_START, start)
    while k >= 0:
        k = es.find(_START, k + 4)
        if k < 0 or len(es) < k + 9:
            return None
        if es[k + 3] == 0xB5 and es[k + 4] >> 4 == 8:
            f = int.from_bytes(es[k + 4:k + 9], "big")   # 40 bits, flags start at bit 20
            bit = lambda n: (f >> (39 - n)) & 1
            return {"dcp": (f >> 18) & 3, "tff": bit(24), "fpfd": bit(25), "qst": bit(27),
                    "ivf": bit(28), "alt": bit(29), "pf": bit(32)}
        if es[k + 3] <= 0xAF:
            return None              # reached the slices without an extension
    return None


_PCE_DEFAULT = {"dcp": 0, "tff": 0, "fpfd": 1, "qst": 0, "ivf": 0, "alt": 0, "pf": 1}


def _default_headers(info: SequenceInfo):
    bw = BitWriter()
    bw.start_code(0xB3)
    bw.write(info.width & 0xFFF, 12)
    bw.write(info.height & 0xFFF, 12)
    bw.write(info.aspect_code, 4)
    bw.write(info.frame_rate_code, 4)
    bw.write(15000000 // 400 & 0x3FFFF, 18)  # bit_rate_value
    bw.write(1, 1)
    bw.write(112, 10)                         # vbv_buffer_size_value
    bw.write(0, 1)                            # constrained_parameters_flag
    bw.write(0, 1)                            # load_intra_quantiser_matrix
    bw.write(0, 1)                            # load_non_intra_quantiser_matrix
    bw.start_code(0xB5)
    bw.write(1, 4)                            # sequence extension id
    bw.write(0x48, 8)                         # Main Profile @ Main Level
    bw.write(1 if info.progressive else 0, 1)
    bw.write(1, 2)                            # 4:2:0
    bw.write(0, 2)
    bw.write(0, 2)
    bw.write(0, 12)
    bw.write(1, 1)
    bw.write(0, 8)
    bw.write(0, 1)                            # low_delay
    bw.write(0, 2)
    bw.write(0, 5)
    return bw.getvalue()


def _picture(bw, info, temporal_ref, ptype):
    f = dict(_PCE_DEFAULT, **(info.pce or {}))
    if info.progressive:
        f["fpfd"] = f["pf"] = 1      # required for progressive sequences
    eob = "0110" if f["ivf"] else "10"   # end of block in table B.15 / B.14
    bw.start_code(0x00)
    bw.write(temporal_ref & 0x3FF, 10)
    bw.write(ptype, 3)            # 1 = I, 2 = P
    bw.write(0xFFFF, 16)          # vbv_delay (VBR)
    if ptype == 2:
        bw.write(0, 1)            # full_pel_forward_vector
        bw.write(7, 3)            # forward_f_code (must be 111 in MPEG-2)
    bw.write(0, 1)                # extra_bit_picture
    # picture_coding_extension
    bw.start_code(0xB5)
    bw.write(8, 4)
    if ptype == 2:
        bw.write(1, 4); bw.write(1, 4)        # forward f_codes
    else:
        bw.write(15, 4); bw.write(15, 4)
    bw.write(15, 4); bw.write(15, 4)          # backward f_codes (unused)
    bw.write(f["dcp"], 2)         # intra_dc_precision
    bw.write(3, 2)                # picture_structure = frame
    bw.write(f["tff"], 1)         # top_field_first
    bw.write(f["fpfd"], 1)        # frame_pred_frame_dct
    bw.write(0, 1)                # concealment_motion_vectors
    bw.write(f["qst"], 1)         # q_scale_type
    bw.write(f["ivf"], 1)         # intra_vlc_format
    bw.write(f["alt"], 1)         # alternate_scan
    bw.write(0, 1)                # repeat_first_field
    bw.write(f["pf"], 1)          # chroma_420_type (== progressive_frame)
    bw.write(f["pf"], 1)          # progressive_frame
    bw.write(0, 1)                # composite_display_flag

    dc_reset = 1 << (7 + f["dcp"])
    luma = info_luma(info) << f["dcp"]
    mbw = info.mb_width
    for row in range(info.mb_height):
        bw.start_code(row + 1)    # slice_start_code
        bw.write(8, 5)            # quantiser_scale_code
        bw.write(0, 1)            # extra_bit_slice
        if ptype == 1:
            pred_y = dc_reset     # DC predictor reset value at slice start
            for _ in range(mbw):
                _write_mba(bw, 1)
                bw.bits("1")      # macroblock_type: intra
                if not f["fpfd"]:
                    bw.bits("0")  # dct_type: frame DCT
                for _blk in range(4):
                    _write_dc(bw, luma - pred_y, True)
                    pred_y = luma
                    bw.bits(eob)
                for _blk in range(2):
                    _write_dc(bw, 0, False)  # Cb/Cr = 128 (neutral)
                    bw.bits(eob)
        else:
            # first and last macroblock must be coded; all others skipped.
            mc = "001" + ("" if f["fpfd"] else "10")   # MC not coded (+ frame_motion_type)
            _write_mba(bw, 1)
            bw.bits(mc)
            bw.bits("1"); bw.bits("1")   # motion_code 0,0
            if mbw > 1:
                _write_mba(bw, mbw - 1)
                bw.bits(mc)
                bw.bits("1"); bw.bits("1")


def info_luma(info):
    return getattr(info, "luma", 16)


def blank_stream(info: SequenceInfo, frames: int, luma: int = 16):
    """Return (es_bytes, picture_offsets) for `frames` black frames."""
    frames = max(1, int(frames))
    info.luma = max(16, min(235, luma))
    out = bytearray(info.raw_headers or _default_headers(info))
    # GOP header: time code 00:00:00:00, closed GOP
    out += b"\x00\x00\x01\xb8\x00\x08\x00\x40"
    offsets = []
    for n in range(frames):
        bw = BitWriter()
        _picture(bw, info, n, 1 if n == 0 else 2)
        offsets.append(len(out))
        out += bw.getvalue()
    out += b"\x00\x00\x01\xb7"  # sequence_end_code
    # picture 0 offset should include the sequence/GOP headers so the first
    # PES carrying a PTS starts at the sequence header.
    offsets[0] = 0
    return bytes(out), offsets
