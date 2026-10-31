"""ISO9660 and UDF (1.02, as used on PS2 DVDs) metadata parsing.

We only need enough to:
  * list files with their extents
  * know where every metadata structure referencing an extent lives, so the
    rebuilder can patch sizes and locations in place.
"""

from dataclasses import dataclass, field
import logging
import struct

log = logging.getLogger("ps2vr.fs")

SECTOR = 2048


@dataclass
class DirRecordRef:
    sector: int      # absolute sector containing the record
    offset: int      # byte offset of the record inside that sector


@dataclass
class FileEntry:
    path: str
    lba: int
    size: int
    is_dir: bool = False
    iso_refs: list = field(default_factory=list)   # DirRecordRef for ISO9660
    udf_fe: int = None                             # absolute sector of UDF File Entry

    @property
    def sectors(self):
        return (self.size + SECTOR - 1) // SECTOR

    @property
    def name(self):
        return self.path.rsplit("/", 1)[-1]


class FileSystem:
    def __init__(self, img):
        self.img = img
        self.files = {}          # path (upper case, no ;1) -> FileEntry
        self.dirs = {}           # path -> FileEntry
        self.pvd_sector = 16
        self.volume_sectors = 0
        self.volume_id = ""
        self.system_id = ""
        self.udf = None          # UdfInfo
        self._parse_iso()
        try:
            self._parse_udf()
        except Exception as e:   # UDF is optional for the PS2; keep going
            self.udf = None
            self.udf_error = str(e)
            log.warning("UDF parse failed (%s); continuing with ISO9660 only", e)
        log.debug("filesystem: volume '%s' system '%s', %d sectors, %d files, %d dirs, UDF=%s",
                  self.volume_id, self.system_id, self.volume_sectors, len(self.files),
                  len(self.dirs), "yes (partition %d+%d)" % (self.udf.part_start, self.udf.part_len)
                  if self.udf else "no")

    # ---------------------------------------------------------------- ISO9660
    def _parse_iso(self):
        pvd = self.img.read(16)
        if pvd[1:6] != b"CD001" or pvd[0] != 1:
            raise ValueError("Primary Volume Descriptor not found")
        self.system_id = pvd[8:40].decode("latin1").strip()
        self.volume_id = pvd[40:72].decode("latin1").strip()
        self.volume_sectors = struct.unpack("<I", pvd[80:84])[0]
        self.path_tables = (struct.unpack("<I", pvd[132:136])[0],   # size
                            struct.unpack("<I", pvd[140:144])[0],   # L table
                            struct.unpack(">I", pvd[148:152])[0])   # M table
        root = pvd[156:190]
        rlba = struct.unpack("<I", root[2:6])[0]
        rsize = struct.unpack("<I", root[10:14])[0]
        rootent = FileEntry("/", rlba, rsize, True, [DirRecordRef(16, 156)])
        self.dirs["/"] = rootent
        self._walk(rlba, rsize, "/", set())

    def _walk(self, lba, size, path, seen):
        if lba in seen:
            return
        seen.add(lba)
        nsec = (size + SECTOR - 1) // SECTOR
        data = self.img.read(lba, nsec)
        for s in range(nsec):
            sec = data[s * SECTOR:(s + 1) * SECTOR]
            i = 0
            while i < SECTOR:
                ln = sec[i]
                if ln == 0:
                    break
                rec = sec[i:i + ln]
                elba = struct.unpack("<I", rec[2:6])[0]
                esize = struct.unpack("<I", rec[10:14])[0]
                flags = rec[25]
                nlen = rec[32]
                raw = rec[33:33 + nlen]
                ref = DirRecordRef(lba + s, i)
                i += ln
                if raw in (b"\x00", b"\x01"):
                    if raw == b"\x00":
                        self.dirs[path if path != "" else "/"].iso_refs.append(ref)
                    continue
                name = raw.decode("latin1")
                if ";" in name:
                    name = name.split(";")[0]
                name = name.rstrip(".")
                full = (path.rstrip("/") + "/" + name)
                key = full.upper()
                if flags & 0x02:
                    ent = self.dirs.get(key)
                    if ent is None:
                        ent = FileEntry(full, elba, esize, True)
                        self.dirs[key] = ent
                    ent.iso_refs.append(ref)
                    self._walk(elba, esize, key, seen)
                else:
                    ent = self.files.get(key)
                    if ent is None:
                        ent = FileEntry(full, elba, esize)
                        self.files[key] = ent
                    ent.iso_refs.append(ref)

    # -------------------------------------------------------------------- UDF
    def _parse_udf(self):
        # look for NSR descriptor in the volume recognition sequence
        has_nsr = False
        for s in range(16, 32):
            d = self.img.read(s)
            if d[1:6] in (b"NSR02", b"NSR03"):
                has_nsr = True
            if d[1:6] == b"TEA01":
                break
        if not has_nsr:
            return
        avdp = self.img.read(256)
        if struct.unpack("<H", avdp[0:2])[0] != 2:
            raise ValueError("UDF anchor not found at sector 256")
        u = UdfInfo()
        u.anchors = [256]
        n = self.img.num_sectors
        for s in (n - 1, n - 257):
            if s > 256 and struct.unpack("<H", self.img.read(s)[0:2])[0] == 2:
                u.anchors.append(s)
        main_len, main_loc, res_len, res_loc = struct.unpack("<IIII", avdp[16:32])
        for loc, ln in ((main_loc, main_len), (res_loc, res_len)):
            for s in range(loc, loc + ln // SECTOR):
                d = self.img.read(s)
                tag = struct.unpack("<H", d[0:2])[0]
                if tag == 5:
                    u.pd_sectors.append(s)
                    u.part_start = struct.unpack("<I", d[188:192])[0]
                    u.part_len = struct.unpack("<I", d[192:196])[0]
                elif tag == 6:
                    u.lvd_sectors.append(s)
                    fsd_len, fsd_lb = struct.unpack("<II", d[248:256])
                    u.fsd_lb = fsd_lb
                    ilen, iloc = struct.unpack("<II", d[432:440])
                    if ilen:
                        u.lvid_sector = iloc
                elif tag == 8:
                    break
        if u.part_start is None:
            raise ValueError("UDF partition descriptor missing")
        fsd = self.img.read(u.part_start + u.fsd_lb)
        if struct.unpack("<H", fsd[0:2])[0] != 256:
            raise ValueError("UDF file set descriptor missing")
        root_lb = struct.unpack("<I", fsd[404:408])[0]
        self.udf = u
        self._udf_walk(root_lb, "/", set())

    def _udf_fe(self, lb):
        abs_s = self.udf.part_start + lb
        d = self.img.read(abs_s)
        tag = struct.unpack("<H", d[0:2])[0]
        if tag not in (261, 266):
            raise ValueError("expected UDF File Entry at %d, got tag %d" % (abs_s, tag))
        return abs_s, d

    def _udf_extents(self, fe):
        tag = struct.unpack("<H", fe[0:2])[0]
        if tag == 261:
            info_len = struct.unpack("<Q", fe[56:64])[0]
            l_ea, l_ad = struct.unpack("<II", fe[168:176])
            ad_off = 176 + l_ea
        else:  # Extended File Entry
            info_len = struct.unpack("<Q", fe[56:64])[0]
            l_ea, l_ad = struct.unpack("<II", fe[208:216])
            ad_off = 216 + l_ea
        adtype = struct.unpack("<H", fe[34:36])[0] & 7
        exts = []
        if adtype == 0:
            for k in range(l_ad // 8):
                ln, pos = struct.unpack("<II", fe[ad_off + 8 * k:ad_off + 8 * k + 8])
                exts.append((pos, ln & 0x3FFFFFFF))
        elif adtype == 1:
            for k in range(l_ad // 16):
                ln, pos = struct.unpack("<II", fe[ad_off + 16 * k:ad_off + 16 * k + 8])
                exts.append((pos, ln & 0x3FFFFFFF))
        elif adtype == 3:
            exts = None   # embedded data
        return info_len, exts, adtype, ad_off, l_ad

    def _udf_walk(self, dir_lb, path, seen):
        if dir_lb in seen:
            return
        seen.add(dir_lb)
        fe_abs, fe = self._udf_fe(dir_lb)
        info_len, exts, adtype, ad_off, l_ad = self._udf_extents(fe)
        if exts is None:
            data = fe[ad_off:ad_off + l_ad]
        else:
            data = bytearray()
            for pos, ln in exts:
                data += self.img.read(self.udf.part_start + pos, (ln + SECTOR - 1) // SECTOR)[:ln]
            data = bytes(data[:info_len])
        self.udf.dir_fes.append(fe_abs)
        if path == "/":
            self.udf.root_fe = fe_abs
        i = 0
        while i + 38 <= len(data):
            if struct.unpack("<H", data[i:i + 2])[0] != 257:
                break
            chars = data[i + 18]
            l_fi = data[i + 19]
            icb_lb = struct.unpack("<I", data[i + 24:i + 28])[0]
            l_iu = struct.unpack("<H", data[i + 36:i + 38])[0]
            raw = data[i + 38 + l_iu:i + 38 + l_iu + l_fi]
            rec = 38 + l_iu + l_fi
            i += (rec + 3) & ~3
            if chars & 0x08:   # parent
                continue
            name = _udf_name(raw)
            full = path.rstrip("/") + "/" + name
            key = full.upper()
            if chars & 0x02:   # directory
                self._udf_walk(icb_lb, key, seen)
            else:
                abs_s, _ = self._udf_fe(icb_lb)
                ent = self.files.get(key)
                if ent is not None:
                    ent.udf_fe = abs_s
                else:
                    self.udf.orphans.append((key, abs_s))

    def udf_file_extents(self, fe_abs):
        fe = self.img.read(fe_abs)
        return self._udf_extents(fe)

    # ---------------------------------------------------------------- helpers
    def read_file(self, ent, size=None, offset=0):
        size = ent.size - offset if size is None else min(size, ent.size - offset)
        return self.img.read_bytes(ent.lba, max(0, size), offset)

    def metadata_end(self):
        """Highest sector (exclusive) used by directories / UDF descriptors."""
        end = 0
        for d in self.dirs.values():
            end = max(end, d.lba + max(1, d.sectors))
        if self.udf:
            for s in self.udf.dir_fes:
                end = max(end, s + 1)
            for ent in self.files.values():
                if ent.udf_fe is not None:
                    end = max(end, ent.udf_fe + 1)
            if self.udf.lvid_sector:
                end = max(end, self.udf.lvid_sector + 1)
        end = max(end, self.path_tables[1] + 1, self.path_tables[2] + 1)
        return end


@dataclass
class UdfInfo:
    anchors: list = field(default_factory=list)
    pd_sectors: list = field(default_factory=list)
    lvd_sectors: list = field(default_factory=list)
    lvid_sector: int = None
    part_start: int = None
    part_len: int = None
    fsd_lb: int = 0
    root_fe: int = None
    dir_fes: list = field(default_factory=list)
    orphans: list = field(default_factory=list)


def _udf_name(raw):
    if not raw:
        return ""
    comp = raw[0]
    if comp == 16:
        return raw[1:].decode("utf-16-be", "replace")
    return raw[1:].decode("latin1")


# ---------------------------------------------------------------- tag helpers
def _crc_itu(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def udf_fix_tag(buf, location=None):
    """Recompute CRC and checksum of a UDF descriptor tag (in place)."""
    if location is not None:
        buf[12:16] = struct.pack("<I", location)
    crc_len = struct.unpack("<H", buf[10:12])[0]
    buf[8:10] = struct.pack("<H", _crc_itu(bytes(buf[16:16 + crc_len])))
    buf[4] = sum(buf[0:4] + buf[5:16]) & 0xFF
    return buf
