"""Write a new ISO with selected files replaced.

Two strategies:

rebuild  (default) - every file located after a replaced file is moved down
         so the freed space is removed and the ISO shrinks. All ISO9660
         directory records and UDF File Entries are patched with the new
         locations; volume / partition sizes and UDF anchors are updated.
         Requires that all file-system metadata precedes the replaced files
         (true for every PS2 mastering tool we know of).

inplace  - replaced files keep their original LBA; the unused tail of the old
         extent is zero filled. The ISO keeps its size, but every other file
         keeps its exact LBA (for games that read raw sectors) and the image
         compresses extremely well (7z/CHD/ZSO).
"""

import logging
import os
import shutil
import struct
import zlib

from .disc import DiscImage, SECTOR
from .fs import FileSystem, udf_fix_tag

CHUNK = 8192  # sectors per copy step (16 MiB)

logger = logging.getLogger("ps2vr.rebuild")


class RebuildError(Exception):
    pass


class _Region:
    def __init__(self, ent, data):
        self.ent = ent
        self.data = data
        self.start = ent.lba
        self.old = ent.sectors
        self.new = (len(data) + SECTOR - 1) // SECTOR
        self.name = ent.path

    @property
    def end(self):
        return self.start + self.old


class _Gap(_Region):
    """Sectors no file owns, removed outright."""

    def __init__(self, start, sectors):
        self.ent = None
        self.data = b""
        self.start = start
        self.old = sectors
        self.new = 0
        self.name = "unused sectors at %d" % start


def _mapper(regions):
    regs = sorted(regions, key=lambda r: r.start)

    def new_lba(x):
        delta = 0
        for r in regs:
            if x >= r.end:
                delta += r.new - r.old
            elif x >= r.start:
                return r.start + delta
            else:
                break
        return x + delta
    return new_lba


def plan(fs, replacements, mode, gaps=()):
    """Validate replacements; return (regions, new_total_sectors). `gaps` are
    (start, sectors) extents that no file owns, to drop in rebuild mode."""
    regions = [_Gap(*g) for g in gaps] if mode == "rebuild" else []
    for key, data in replacements.items():
        ent = fs.files.get(key.upper())
        if ent is None:
            raise RebuildError("File not found on disc: %s" % key)
        r = _Region(ent, data)
        if mode == "inplace" and r.new > r.old:
            raise RebuildError("%s: replacement is larger than the original" % ent.path)
        regions.append(r)
    regions.sort(key=lambda r: r.start)
    for a, b in zip(regions, regions[1:]):
        if b.start < a.end:
            raise RebuildError("Overlapping extents: %s / %s" % (a.name, b.name))
    # extents shared with other files would be corrupted
    for ent in fs.files.values():
        for r in regions:
            if ent is not r.ent and ent.size and ent.lba < r.end and ent.lba + ent.sectors > r.start:
                raise RebuildError("%s shares sectors with %s" % (ent.path, r.name))
    total = fs.img.num_sectors
    if mode == "rebuild":
        meta_end = fs.metadata_end()
        logger.debug("metadata ends at sector %d", meta_end)
        if regions and regions[0].start < meta_end:
            raise RebuildError(
                "File-system metadata is located after %s; use in-place mode" % regions[0].name)
        # dual layer: layer 1 has its own file system with layer-relative
        # addresses, so it can only move as a whole block
        lb = fs.layer_break
        if lb is not None and regions and regions[-1].end > lb:
            raise RebuildError("%s is on the second layer of a dual-layer disc; use in-place mode"
                               % regions[-1].name)
        total += sum(r.new - r.old for r in regions)
    for r in regions:
        logger.debug("region %s: lba %d, %d -> %d sectors", r.name, r.start, r.old, r.new)
    return regions, total


def check_output(src_path, out_path):
    """Refuse an output that is any file of the source image, such as a CUE's BIN,
    under whatever name it is reached: other case, short name, link, mapped drive."""
    from .batch import image_files  # late import: batch imports this module

    def name(p):
        return os.path.normcase(os.path.realpath(p))
    for f in image_files(src_path):
        if name(f) == name(out_path) or (os.path.exists(f) and os.path.exists(out_path)
                                         and os.path.samefile(f, out_path)):
            raise RebuildError("Refusing to write over the source image: %s is %s"
                               % (out_path, f))


def write(src_path, out_path, replacements, mode="rebuild", progress=None, log=print, gaps=()):
    check_output(src_path, out_path)
    img = DiscImage(src_path)
    try:
        fs = FileSystem(img)
        cut = fs.truncated()
        if cut:
            raise RebuildError("Image is truncated: %d file(s) extend past the end of the image "
                               "(e.g. %s)" % (len(cut), cut[0].path))
        regions, total = plan(fs, replacements, mode, gaps)
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(out_path)) or ".").free
        if free < total * SECTOR + (64 << 20):
            raise RebuildError("Not enough disk space for output (%d MiB needed)" % (total * SECTOR >> 20))
        new_lba = _mapper(regions) if mode == "rebuild" else (lambda x: x)

        log("Writing %s (%s mode, %d sectors -> %d)" % (out_path, mode, img.num_sectors, total))
        done = 0
        with open(out_path, "wb") as out:
            def copy(a, b):
                nonlocal done
                s = a
                while s < b:
                    n = min(CHUNK, b - s)
                    out.write(img.read(s, n))
                    s += n
                    done += n
                    if progress:
                        progress(done, img.num_sectors)

            cur = 0
            for r in regions:
                copy(cur, r.start)
                pad = (r.new if mode == "rebuild" else r.old) * SECTOR - len(r.data)
                out.write(r.data + bytes(pad))
                done += r.old
                cur = r.end
            copy(cur, img.num_sectors)

        with open(out_path, "r+b") as out:
            _patch(out, fs, regions, new_lba, total, mode, log)
    finally:
        img.close()
    return total


def _rw(out, sector):
    out.seek(sector * SECTOR)
    return bytearray(out.read(SECTOR))


def _put(out, sector, buf):
    out.seek(sector * SECTOR)
    out.write(bytes(buf))


def _patch(out, fs, regions, new_lba, total, mode, log):
    sizes = {r.ent.path.upper(): len(r.data) for r in regions if r.ent}
    delta = total - fs.img.num_sectors

    # ---- ISO9660 ------------------------------------------------------------
    for s in range(16, 32):
        vd = _rw(out, s)
        if vd[1:6] != b"CD001":
            break
        if vd[0] == 255:
            break
        if vd[0] in (1, 2) and mode == "rebuild":
            vol = struct.unpack("<I", vd[80:84])[0] + delta
            vd[80:84] = struct.pack("<I", vol)
            vd[84:88] = struct.pack(">I", vol)
            _put(out, s, vd)
        if vd[0] == 2:
            log("warning: supplementary (Joliet) volume descriptor present - only the primary tree is patched")

    cache = {}
    for key, ent in fs.files.items():
        new_size = sizes.get(key)
        nl = new_lba(ent.lba)
        if nl == ent.lba and new_size is None:
            continue
        for ref in ent.iso_refs:
            buf = cache.get(ref.sector)
            if buf is None:
                buf = cache[ref.sector] = _rw(out, ref.sector)
            o = ref.offset
            buf[o + 2:o + 6] = struct.pack("<I", nl)
            buf[o + 6:o + 10] = struct.pack(">I", nl)
            if new_size is not None:
                buf[o + 10:o + 14] = struct.pack("<I", new_size)
                buf[o + 14:o + 18] = struct.pack(">I", new_size)
    for sector, buf in cache.items():
        _put(out, sector, buf)
        logger.debug("patched ISO9660 directory sector %d", sector)
    log("ISO9660: patched %d directory sectors" % len(cache))

    # ---- UDF ----------------------------------------------------------------
    u = fs.udf
    if u is None:
        return
    ps = u.part_start
    fes = [(e.udf_fe, sizes.get(k)) for k, e in fs.files.items() if e.udf_fe is not None]
    fes += [(abs_s, sizes.get(k)) for k, abs_s in u.orphans]
    patched = 0
    for fe_abs, new_size in fes:
        fe = _rw(out, fe_abs)
        tag = struct.unpack("<H", fe[0:2])[0]
        info_len, exts, adtype, ad_off, l_ad = fs._udf_extents(fe)
        if exts is None:
            continue
        changed = False
        step = 8 if adtype == 0 else 16
        remaining = new_size
        for k in range(l_ad // step):
            o = ad_off + k * step
            ln_raw, pos = struct.unpack("<II", fe[o:o + 8])
            ln = ln_raw & 0x3FFFFFFF
            npos = new_lba(ps + pos) - ps
            if npos != pos:
                fe[o + 4:o + 8] = struct.pack("<I", npos)
                changed = True
            if new_size is not None:
                use = min(ln, remaining)
                remaining -= use
                fe[o:o + 4] = struct.pack("<I", (ln_raw & 0xC0000000) | use)
                changed = True
        if new_size is not None:
            fe[56:64] = struct.pack("<Q", new_size)
            fe[64:72] = struct.pack("<Q", (new_size + SECTOR - 1) // SECTOR)
        if changed or new_size is not None:
            udf_fix_tag(fe)
            _put(out, fe_abs, fe)
            patched += 1
    log("UDF: patched %d file entries" % patched)

    if mode != "rebuild" or delta == 0:
        return
    new_plen = u.part_len + delta
    for s in u.pd_sectors:
        pd = _rw(out, s)
        pd[192:196] = struct.pack("<I", new_plen)
        _put(out, s, udf_fix_tag(pd))
    if u.lvid_sector:
        lv = _rw(out, u.lvid_sector)
        if struct.unpack("<H", lv[0:2])[0] == 9:
            nparts = struct.unpack("<I", lv[72:76])[0]
            if nparts >= 1:
                o = 80 + 4 * nparts
                lv[o:o + 4] = struct.pack("<I", new_plen)
                _put(out, u.lvid_sector, udf_fix_tag(lv))
    for old in u.anchors:
        if old == 256:
            continue
        ns = new_lba(old)
        av = _rw(out, ns)
        if struct.unpack("<H", av[0:2])[0] == 2:
            _put(out, ns, udf_fix_tag(av, ns))
    log("UDF: partition length %d -> %d" % (u.part_len, new_plen))


def verify(src_path, out_path, replacements, full=False, progress=None, log=print):
    """Re-open the output and check every file against the source."""
    errors = []
    with DiscImage(src_path) as a, DiscImage(out_path) as b:
        fa, fb = FileSystem(a), FileSystem(b)
        keys = sorted(fa.files)
        for n, key in enumerate(keys):
            if progress:
                progress(n, len(keys))
            ea, eb = fa.files[key], fb.files.get(key)
            if eb is None:
                errors.append("missing: " + key)
                continue
            if key in {k.upper() for k in replacements}:
                want = replacements[next(k for k in replacements if k.upper() == key)]
                if fb.read_file(eb) != want:
                    errors.append("replacement mismatch: " + key)
                continue
            if ea.size != eb.size:
                errors.append("size changed: " + key)
                continue
            if full:
                same = _crc_file(fa, ea) == _crc_file(fb, eb)
            else:
                same = fa.read_file(ea, 4096) == fb.read_file(eb, 4096) and \
                    fa.read_file(ea, 4096, max(0, ea.size - 4096)) == \
                    fb.read_file(eb, 4096, max(0, eb.size - 4096))
            if not same:
                errors.append("content differs: " + key)
            if fb.udf and eb.udf_fe is not None:
                info_len, exts, *_ = fb.udf_file_extents(eb.udf_fe)
                if exts and (info_len != eb.size or fb.udf.part_start + exts[0][0] != eb.lba):
                    errors.append("UDF entry inconsistent: " + key)
        la, lb = fa.layer_break, fb.layer_break
        if la is not None:
            # dual layer: layer 1 must follow the shrunken layer 0 unchanged
            if lb is None:
                errors.append("layer 1 volume descriptor not found at sector %d" % fb.volume_sectors)
            elif a.num_sectors - la != b.num_sectors - lb:
                errors.append("layer 1 size changed")
            else:
                n = a.num_sectors - la
                # (the last sector is the UDF end anchor, rewritten with its new location)
                for off in sorted({0, 16, 17, n // 2, n - 2}):
                    if off < n and a.read(la + off) != b.read(lb + off):
                        errors.append("layer 1 sector %d differs" % off)
        elif fb.volume_sectors != b.num_sectors:
            errors.append("volume size %d != image sectors %d" % (fb.volume_sectors, b.num_sectors))
    return errors


def _crc_file(fs, ent):
    crc = 0
    pos = 0
    while pos < ent.size:
        n = min(16 << 20, ent.size - pos)
        crc = zlib.crc32(fs.read_file(ent, n, pos), crc)
        pos += n
    return crc
