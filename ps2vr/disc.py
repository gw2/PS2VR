"""Disc image access: ISO, BIN/CUE, MDF/MDS (and other raw sector dumps).

All images are exposed as a sequence of 2048-byte user-data sectors, which
is what the ISO9660/UDF file systems (and the PS2 itself) see.
"""

import logging
import os
import re

log = logging.getLogger("ps2vr.disc")

SECTOR = 2048
SYNC = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"

# (raw sector size, offset of user data inside the raw sector)
_LAYOUTS = [
    (2048, 0),    # plain ISO
    (2352, 24),   # Mode 2 Form 1 (PS2 CDs)
    (2352, 16),   # Mode 1
    (2336, 8),    # Mode 2 without sync/header
    (2448, 24),   # Mode 2 + 96 bytes subchannel (MDF)
    (2448, 16),   # Mode 1 + subchannel
]


class ImageError(Exception):
    pass


def _resolve_track_file(path):
    """Return (data_file, hint) for CUE/MDS descriptors, or the path itself."""
    ext = os.path.splitext(path)[1].lower()
    folder = os.path.dirname(os.path.abspath(path))
    if ext == ".cue":
        with open(path, "r", errors="replace") as f:
            text = f.read()
        files = re.findall(r'^\s*FILE\s+"?(.+?)"?\s+\w+\s*$', text, re.M | re.I)
        if not files:
            raise ImageError("CUE sheet does not reference any FILE")
        m = re.search(r"TRACK\s+\d+\s+(\S+)", text, re.I)
        mode = m.group(1).upper() if m else ""
        data = os.path.join(folder, files[0])
        if not os.path.exists(data):
            raise ImageError("Track file referenced by CUE not found: %s" % data)
        return data, mode
    if ext == ".mds":
        base = os.path.splitext(path)[0]
        for cand in (base + ".mdf", base + ".MDF"):
            if os.path.exists(cand):
                return cand, ""
        raise ImageError("MDF file for this MDS was not found next to it")
    return path, ""


class DiscImage:
    def __init__(self, path):
        self.path = path
        self.data_path, self.mode_hint = _resolve_track_file(path)
        self.f = open(self.data_path, "rb")
        self.file_size = os.path.getsize(self.data_path)
        self.raw_size, self.data_offset = self._detect()
        self.num_sectors = self.file_size // self.raw_size
        log.debug("opened %s: data file %s, %d bytes, %s, %d sectors", path, self.data_path,
                  self.file_size, self.description, self.num_sectors)

    def _detect(self):
        candidates = list(_LAYOUTS)
        if "2352" in self.mode_hint:
            candidates.sort(key=lambda l: l[0] != 2352)
        for raw, off in candidates:
            self.f.seek(16 * raw + off)
            d = self.f.read(6)
            if d[1:6] == b"CD001":
                if raw == 2352:
                    self.f.seek(16 * raw)
                    hdr = self.f.read(16)
                    if hdr[:12] == SYNC:
                        off = 16 if hdr[15] == 1 else 24
                return raw, off
        raise ImageError("No ISO9660 file system found (unsupported image format)")

    @property
    def is_iso(self):
        return self.raw_size == SECTOR

    @property
    def description(self):
        if self.is_iso:
            return "ISO (2048 bytes/sector)"
        mode = "Mode 1" if self.data_offset == 16 else "Mode 2 Form 1"
        return "raw %d bytes/sector, %s" % (self.raw_size, mode)

    def read(self, lba, count=1):
        if self.is_iso:
            self.f.seek(lba * SECTOR)
            return self.f.read(count * SECTOR)
        out = bytearray()
        # read in moderately large batches for speed
        while count > 0:
            n = min(count, 512)
            self.f.seek(lba * self.raw_size)
            raw = self.f.read(n * self.raw_size)
            for i in range(len(raw) // self.raw_size):
                s = i * self.raw_size + self.data_offset
                out += raw[s:s + SECTOR]
            lba += n
            count -= n
        return bytes(out)

    def read_bytes(self, lba, size, offset=0):
        first = lba + offset // SECTOR
        skip = offset % SECTOR
        nsec = (skip + size + SECTOR - 1) // SECTOR
        return self.read(first, nsec)[skip:skip + size]

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
