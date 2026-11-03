"""Detection of padding: sectors a PS2 disc carries only to fill it up.

Mastered PS2 discs are padded to push game data towards the fast outer edge of
the DVD. Padding comes in four forms, and they differ in how sure we can be:

gap      - sectors no file owns, all one byte. Nothing can name them, and there
           is no data to lose, but files after them move when they are removed.
blank    - a file with a filler name (DUMMY.DAT, PAD_0.DAT...), all one byte.
named    - a filler name, but real-looking content (old builds, leftover files).
           Probably unused, but only the game knows. SUSPECT.
fill     - all one byte, but an ordinary name (a silent track, a solid image).
           The game may well read it. SUSPECT.

Sectors no file owns that hold data are not padding: Final Fantasy X and others
keep their whole game there, addressed by raw sector number.

Scanning samples each candidate. Before anything is removed, confirm() reads
every byte, so a sampling miss can never lose data.
"""

from dataclasses import dataclass
import logging
import re
import struct

from .disc import SECTOR

log = logging.getLogger("ps2vr.padding")

MIN_SECTORS = 512       # 1 MiB; anything smaller is not worth moving files for
WINDOW = 32             # sectors per sample (64 KiB)
SAMPLES = 16
CHUNK = 8192            # sectors per read when confirming (16 MiB)

SAFE = "padding"        # removed by --padding
SUSPECT = "suspect"     # removed only by --padding-suspect

# A table of sector numbers: a file this small or smaller that lists at least TABLE_HITS
# of the files that move, and at least one in TABLE_SHARE of them.
TABLE_MAX = 8 << 20
TABLE_HITS = 3
TABLE_SHARE = 3

_NAME = re.compile(
    r"""DUMMY | DUMY                # DUMMY.DAT, ZZZDUMMY.BIN, DVDUMMY.BIN, Z_DUMMY
      | PADDING                     # PADDING.NUL, PADDING.1
      | ^PAD(_?\d+)?(\.|$)          # PAD, PAD.TXT, PAD_0.DAT, PAD128.DAT (not PADMAN.IRX)
      | \.(PAD|DMY|NUL)$            # EUROPA.PAD, DUMMY.DMY
      | ^NULL\d+\.                  # NULL100.DAT (not NULL.AFS)
      | ^GARBAGE\d*(\.|$)
      | ^Z{4,}\d*(\.|$)             # ZZZZZ.BIN
    """, re.I | re.X)


def is_filler_name(name):
    return _NAME.search(name) is not None


@dataclass
class Padding:
    kind: str                   # "gap", "blank", "named" or "fill"
    start: int                  # first sector
    sectors: int
    path: str = ""              # empty for a gap
    fill: int = None            # the byte it is filled with, if sampled uniform
    detail: str = ""            # (same display fields as video.VideoFile)
    compatible: bool = True

    @property
    def size(self):
        return self.sectors * SECTOR

    @property
    def status(self):
        return SAFE if self.kind in ("gap", "blank") else SUSPECT

    @property
    def label(self):
        return self.path or "(unused sectors at %d)" % self.start

    def describe(self):
        content = "mixed data" if self.fill is None else "all 0x%02X" % self.fill
        return {"gap": "no file owns these sectors, %s",
                "blank": "filler name, %s",
                "named": "filler name, but %s - probably unused, cannot be proven",
                "fill": "ordinary name, %s - the game may read it"}[self.kind] % content


def _uniform(buf):
    """The byte `buf` consists of, or None."""
    if buf and buf.count(buf[0]) == len(buf):
        return buf[0]
    return None


def _sample(img, start, sectors):
    """The single byte an extent seems to consist of, or None."""
    fill = None
    n = min(WINDOW, sectors)
    for i in range(SAMPLES):
        byte = _uniform(img.read(start + (sectors - n) * i // (SAMPLES - 1), n))
        if byte is None or fill not in (None, byte):
            return None
        fill = byte
    return fill


def confirm(img, item, tick=None):
    """Read all of an item; True if the sampled fill byte holds for every sector.
    `named` items have no fill byte, so there is nothing to check."""
    if item.fill is None:
        return item.kind == "named"
    want = bytes([item.fill]) * (CHUNK * SECTOR)
    pos, end = item.start, item.start + item.sectors
    while pos < end:
        n = min(CHUNK, end - pos)
        if img.read(pos, n) != want[:n * SECTOR]:
            return False
        pos += n
        if tick:
            tick()
    return True


def _gaps(fs):
    """Unowned extents as (start, sectors), from the end of the metadata to the
    last file. The tail of the image is left alone: it holds the UDF end anchor
    and lets the layout stay what the mastering tool wrote (normally 20 MiB)."""
    ents = sorted((e for e in fs.files.values() if e.size), key=lambda e: e.lba)
    limit = fs.layer_break or fs.img.num_sectors
    cur = fs.metadata_end()
    for e in ents:
        if e.lba >= limit:
            break
        if e.lba - cur >= MIN_SECTORS:
            yield cur, e.lba - cur
        cur = max(cur, e.lba + e.sectors)


def scan(fs, videos=()):
    """Find padding by sampling. `videos` are files already handled as video:
    DUMMY.PSS is usually a real, playable placeholder (Fatal Frame, Wild Arms 3)."""
    known = {v.path.upper() for v in videos if not v.fmt.startswith("unknown")}
    items = []
    unowned = 0
    for start, sectors in _gaps(fs):
        fill = _sample(fs.img, start, sectors)
        if fill is None:
            unowned += sectors
            log.info("%d unowned sectors at %d hold data: keeping them", sectors, start)
        else:
            items.append(Padding("gap", start, sectors, fill=fill))
    limit = fs.layer_break or fs.img.num_sectors
    for key, e in fs.files.items():
        if e.sectors < MIN_SECTORS or key in known or e.lba + e.sectors > limit:
            continue
        fill = _sample(fs.img, e.lba, e.sectors)
        named = is_filler_name(e.name)
        if fill is None and not named:
            continue
        kind = ("blank" if fill is not None else "named") if named else "fill"
        items.append(Padding(kind, e.lba, e.sectors, e.path, fill))
    items.sort(key=lambda p: p.start)
    for p in items:
        log.debug("%s: %d sectors, %s, %s", p.label, p.sectors, p.status, p.describe())
    return items, unowned * SECTOR


def choose(items, suspects=False):
    """The items to remove by default, or with the suspects as well."""
    return [p for p in items if p.status == SAFE or suspects]


# PCSX2 calls an image of up to this many sectors a CD if two fields of its volume
# descriptor match (FindDiskType in CDVDcommon.cpp). Most DVD games do not match, and
# boot as DVDs even at 364 MiB, but 3 of 247 tested images would come up as CDs.
PCSX2_DVD_MIN = 452849


def becomes_cd(fs, sectors):
    """Would shrinking this DVD image to `sectors` make PCSX2 take it for a CD?"""
    pvd = fs.img.read(fs.pvd_sector)
    return (fs.img.num_sectors > PCSX2_DVD_MIN and sectors <= PCSX2_DVD_MIN and
            pvd[166:168] == pvd[171:173])


def lba_warning(fs, items):
    """A warning if removing `items` would move files whose sector numbers the game
    keeps in an executable or small root file, else "". Patching the file system cannot
    repoint such a game. A table may be unused, so this warns rather than refuses."""
    if not items:
        return ""
    first = min(p.start for p in items)
    gone = {p.path.upper() for p in items if p.path}
    moved = {e.lba for k, e in fs.files.items() if e.size and e.lba > first and k not in gone}
    need = max(TABLE_HITS, (len(moved) + TABLE_SHARE - 1) // TABLE_SHARE)
    if len(moved) < need:
        return ""
    for key, e in fs.files.items():
        if not 0 < e.size <= TABLE_MAX or not (key.count("/") == 1 or key.endswith(".ELF")):
            continue
        data = fs.read_file(e)
        n = len(data) // 4
        hits = len(moved.intersection(struct.unpack("<%dI" % n, data[:n * 4])))
        if hits >= need:
            return ("%s stores the sector numbers of %d of the %d files that would move. "
                    "Removing padding will probably break this game: use in-place mode, "
                    "or test the output" % (e.path, hits, len(moved)))
    return ""


def plan_removal(fs, items, saved, mode, warn):
    """Turn items into (replacements, gaps) for rebuild.write. `saved` is what other
    replacements already free. A file becomes one blank sector, so its name still opens."""
    gaps = [(p.start, p.sectors) for p in items if not p.path] if mode == "rebuild" else []
    files = [p for p in items if p.path]
    total = fs.img.num_sectors - saved - sum(g[1] for g in gaps) - sum(p.sectors - 1 for p in files)
    if mode == "rebuild" and becomes_cd(fs, total):
        warn("PCSX2 may take the %d-sector output for a CD: test it" % total)
    return {p.path.upper(): bytes(SECTOR) for p in files}, gaps
