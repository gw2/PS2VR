"""Folder scanning, size estimation and batch processing of disc images."""

from dataclasses import dataclass, field
import logging
import os
import re

from . import compat, video, rebuild, padding
from .disc import DiscImage, SECTOR, cue_files
from .fs import FileSystem

IMAGE_EXTS = {".iso", ".cue", ".bin", ".mdf", ".mds", ".img"}
OUTPUT_RE = re.compile(r"\[PS2VR(-[VPS]+)?\]")     # [PS2VR] from older versions too

log_ = logging.getLogger("ps2vr.batch")


def _human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            if unit == "B":
                return "%d B" % n
            return ("%.2f %s" if unit == "GiB" else "%.1f %s") % (n, unit)
        n /= 1024.0


def output_tag(videos=False, blank=False, suspect=False):
    """[PS2VR-VP]: V videos replaced, P blank padding removed, S suspect padding removed."""
    letters = "V" * bool(videos) + "P" * bool(blank) + "S" * bool(suspect)
    return "[PS2VR-%s]" % letters if letters else "[PS2VR]"


class Cancelled(Exception):
    pass


@dataclass
class ImageReport:
    path: str
    files: list = field(default_factory=list)   # every file belonging to the image
    size: int = 0                               # current size on disk (all files)
    fmt: str = ""
    volume_id: str = ""
    serial: str = ""
    mode: str = "rebuild"
    videos: list = field(default_factory=list)
    targets: list = field(default_factory=list)  # compatible videos a blank would shrink
    est_size: int = None                        # output ISO size with the videos replaced
    iso_size: int = 0                           # output ISO size with the videos kept
    error: str = ""
    warning: str = ""
    video_count: int = 0                        # kept separately so cached rows
    target_count: int = 0                       # (which have no video objects) work
    cached: bool = False
    pads: list = field(default_factory=list)    # padding.Padding items (not cached)
    pad_safe: int = 0                           # bytes of blank padding
    pad_suspect: int = 0                        # bytes that only look like padding
    pad_warning: str = ""                       # the game may not survive removing pad_safe
    use_padding: bool = False                   # remove pad_safe
    padding_only: bool = False                  # keep the videos

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def compatible(self):
        return [v for v in self.videos if v.compatible]

    @property
    def after_size(self):
        if self.est_size is None:
            return None
        base = self.iso_size if self.padding_only else self.est_size
        return base - self.pad_safe if self.use_padding else base

    @property
    def savings(self):
        return 0 if self.after_size is None else self.size - self.after_size

    @property
    def can_process(self):
        if self.error:
            return False
        return (self.target_count > 0 and not self.padding_only) or \
            (self.use_padding and self.pad_safe > 0)

    @property
    def tag(self):
        return output_tag(self.target_count > 0 and not self.padding_only,
                          self.use_padding and self.pad_safe > 0)

    @property
    def contents(self):
        """What the Videos column shows."""
        videos = "%d videos kept" % self.video_count if self.padding_only else \
            "%d of %d replaceable" % (self.target_count, self.video_count)
        text = "%s, %s padding" % (videos, _human(self.pad_safe))
        if self.warning:
            text += "  (%s)" % self.warning
        known = compat.problems(compat.lookup(self.serial), self.mode,
                                self.target_count > 0 and not self.padding_only,
                                self.use_padding and self.pad_safe > 0)
        for problem in known:
            text += "  (WARNING: %s)" % problem
        if self.use_padding and self.pad_warning and not known:
            text += "  (WARNING: stores sector numbers, padding removal may break it)"
        return text


def image_files(path):
    """All files on disk that make up the image at `path`."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cue":
        return [path] + [p for p in cue_files(path) if os.path.exists(p)]
    if ext == ".mds":
        base = os.path.splitext(path)[0]
        return [path] + [p for p in (base + ".mdf", base + ".MDF") if os.path.exists(p)][:1]
    return [path]


def find_images(folder, recursive=False, include_outputs=False):
    """Return image entry points (CUE/MDS preferred over the data files they own)."""
    found = []
    for root, dirs, names in os.walk(folder):
        if not recursive:
            dirs[:] = []
        dirs.sort()
        paths = [os.path.join(root, n) for n in sorted(names, key=str.lower)
                 if os.path.splitext(n)[1].lower() in IMAGE_EXTS]
        owned = set()
        for p in paths:
            if os.path.splitext(p)[1].lower() in (".cue", ".mds"):
                try:
                    owned.update(os.path.normcase(os.path.abspath(f)) for f in image_files(p)[1:])
                except OSError:
                    pass
        for p in paths:
            if os.path.normcase(os.path.abspath(p)) in owned:
                continue
            if not include_outputs and OUTPUT_RE.search(os.path.basename(p)):
                continue
            found.append(p)
    return found


def estimate(path, duration=1.0, mode="rebuild"):
    """Analyse one image and estimate its size after replacing all compatible videos."""
    rep = ImageReport(path, mode=mode)
    try:
        rep.files = image_files(path)
        rep.size = sum(os.path.getsize(f) for f in rep.files)
        if path.lower().endswith(".cue") and len(rep.files) > 2:
            rep.warning = "multi-track CUE: only the data track is kept"
        with DiscImage(path) as img:
            rep.fmt = img.description
            fs = FileSystem(img)
            rep.volume_id = fs.volume_id
            rep.serial = compat.serial_of(fs) or ""
            cut = fs.truncated()
            if cut:
                raise ValueError("image is truncated: %d file(s) extend past the end of the "
                                 "image (bad dump?)" % len(cut))
            if fs.system_id.upper() != "PLAYSTATION":
                rep.warning = (rep.warning + "; " if rep.warning else "") + \
                    "system id '%s' - may not be a PS2 disc" % fs.system_id
            rep.videos = video.scan(fs)
            sectors = img.num_sectors
            rep.iso_size = sectors * SECTOR
            for v in rep.compatible:
                old = (v.size + SECTOR - 1) // SECTOR
                new = (len(video.make_blank(v, duration)) + SECTOR - 1) // SECTOR
                if new < old:          # otherwise it is already blank / tiny
                    rep.targets.append(v)
                    if mode == "rebuild":
                        sectors -= old - new
            rep.est_size = sectors * SECTOR
            rep.video_count, rep.target_count = len(rep.videos), len(rep.targets)
            rep.pads, _ = padding.scan(fs, rep.videos)
            rep.pad_safe = sum(p.size for p in rep.pads if p.status == padding.SAFE)
            rep.pad_suspect = sum(p.size for p in rep.pads if p.status == padding.SUSPECT)
            rep.pad_warning = padding.lba_warning(fs, padding.choose(rep.pads))
            log_.info("scanned %s: %s, %d video(s), %d replaceable, %d -> %d bytes",
                      path, rep.fmt, rep.video_count, rep.target_count, rep.size, rep.est_size)
            for v in rep.videos:
                log_.debug("  %s [%s] %s %s", v.path, v.status, v.describe(), v.detail)
    except Exception as e:  # report and continue with the next image
        rep.error = str(e) or e.__class__.__name__
        log_.warning("scan failed for %s: %s", path, rep.error, exc_info=log_.isEnabledFor(logging.DEBUG))
    return rep


def scan_folder(folder, recursive=False, duration=1.0, mode="rebuild",
                include_outputs=False, on_image=None, cancel=None, cache=None):
    """Scan a folder. With a ScanCache, unchanged images are taken from it."""
    reports = []
    paths = find_images(folder, recursive, include_outputs)
    log_.info("scanning %s (recursive=%s): %d image(s)", folder, recursive, len(paths))
    try:
        for n, p in enumerate(paths):
            if cancel and cancel():
                log_.info("scan cancelled")
                break
            rep = None
            if cache is not None:
                try:
                    rep = cache.get(p, image_files(p), mode, duration)
                except OSError:
                    rep = None
            if rep is None:
                rep = estimate(p, duration, mode)
                if cache is not None and not rep.error:    # an error may be a passing one
                    cache.put(rep, mode, duration)
            reports.append(rep)
            if on_image:
                on_image(n + 1, len(paths), rep)
    finally:
        if cache is not None:
            log_.info("scan cache: %d hit(s), %d miss(es)", cache.hits, cache.misses)
            cache.save()
    return reports


def output_path(rep, out_dir=None):
    stem = os.path.splitext(rep.name)[0]
    return os.path.join(out_dir or os.path.dirname(os.path.abspath(rep.path)),
                        "%s %s.iso" % (stem, rep.tag))


@dataclass
class BatchResult:
    report: ImageReport
    output: str
    ok: bool
    message: str
    out_size: int = 0


def run(reports, out_dir=None, duration=1.0, mode="rebuild", verify=True,
        overwrite=False, log=print, progress=None, cancel=None):
    """Process each report in turn. progress(image_index, image_count, fraction)."""
    from .cli import run_replace  # late import: cli imports this module
    results = []
    used = set()
    todo = [r for r in reports if r.can_process]
    for i, rep in enumerate(todo):
        if cancel and cancel():
            log("Batch cancelled.")
            break
        out = output_path(rep, out_dir)
        log("[%d/%d] %s" % (i + 1, len(todo), rep.name))
        key = os.path.normcase(os.path.abspath(out))
        if key in used:
            log("  skipped: another image in this batch also gives %s" % os.path.basename(out))
            results.append(BatchResult(rep, out, False, "same output name as another image"))
            continue
        used.add(key)
        if os.path.exists(out) and not overwrite:
            log("  skipped: %s already exists" % os.path.basename(out))
            results.append(BatchResult(rep, out, False, "output exists"))
            continue

        def prog(done, total, *_):
            if cancel and cancel():
                raise Cancelled()
            if progress:
                progress(i, len(todo), done / max(1, total))
        try:
            rebuild.check_output(rep.path, out)
            if rep.cached or not (rep.targets or rep.pads):
                # cached rows only hold display data - analyse the image now
                log("  analysing...")
                fresh = estimate(rep.path, duration, mode)
                if fresh.error:
                    raise rebuild.RebuildError(fresh.error)
                fresh.size = rep.size
                fresh.use_padding = rep.use_padding
                fresh.padding_only = rep.padding_only
                rep = fresh
            pads = padding.choose(rep.pads) if rep.use_padding else []
            targets = [] if rep.padding_only else rep.targets
            if not targets and not pads:
                raise rebuild.RebuildError("no replaceable videos")
            log_.info("processing %s -> %s (%s, %.2fs, verify=%s)", rep.path, out, mode,
                      duration, verify)
            run_replace(rep.path, out, targets, duration, mode, verify,
                        log=lambda m: log("  " + m), progress=prog, pads=pads)
            size = os.path.getsize(out)
            log("  saved %s" % _human(rep.size - size))
            results.append(BatchResult(rep, out, True, "ok", size))
        except Cancelled:
            log("  cancelled - partial output deleted")
            results.append(BatchResult(rep, out, False, "cancelled"))
            break
        except Exception as e:  # keep going with the remaining images
            log("  FAILED: %s" % e)
            log_.error("batch item %s failed: %s", rep.path, e, exc_info=True)
            results.append(BatchResult(rep, out, False, str(e)))
        if progress:
            progress(i + 1, len(todo), 0.0)
    return results
