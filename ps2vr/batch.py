"""Folder scanning, size estimation and batch processing of disc images."""

from dataclasses import dataclass, field
import logging
import os
import re

from . import video, rebuild
from .disc import DiscImage, SECTOR
from .fs import FileSystem

IMAGE_EXTS = {".iso", ".cue", ".bin", ".mdf", ".mds", ".img"}
OUTPUT_TAG = "[PS2VR]"

log_ = logging.getLogger("ps2vr.batch")


class Cancelled(Exception):
    pass


@dataclass
class ImageReport:
    path: str
    files: list = field(default_factory=list)   # every file belonging to the image
    size: int = 0                               # current size on disk (all files)
    fmt: str = ""
    volume_id: str = ""
    videos: list = field(default_factory=list)
    targets: list = field(default_factory=list)  # compatible videos a blank would shrink
    est_size: int = None                        # output ISO size after replacement
    error: str = ""
    warning: str = ""
    video_count: int = 0                        # kept separately so cached rows
    target_count: int = 0                       # (which have no video objects) work
    cached: bool = False

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def compatible(self):
        return [v for v in self.videos if v.compatible]

    @property
    def savings(self):
        return 0 if self.est_size is None else self.size - self.est_size

    @property
    def can_process(self):
        return not self.error and self.target_count > 0


def _cue_files(path):
    folder = os.path.dirname(os.path.abspath(path))
    with open(path, "r", errors="replace") as f:
        names = re.findall(r'^\s*FILE\s+"?(.+?)"?\s+\w+\s*$', f.read(), re.M | re.I)
    return [os.path.join(folder, n) for n in names]


def image_files(path):
    """All files on disk that make up the image at `path`."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cue":
        return [path] + [p for p in _cue_files(path) if os.path.exists(p)]
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
            if not include_outputs and OUTPUT_TAG in os.path.basename(p):
                continue
            found.append(p)
    return found


def estimate(path, duration=1.0, mode="rebuild"):
    """Analyse one image and estimate its size after replacing all compatible videos."""
    rep = ImageReport(path)
    try:
        rep.files = image_files(path)
        rep.size = sum(os.path.getsize(f) for f in rep.files)
        if path.lower().endswith(".cue") and len(rep.files) > 2:
            rep.warning = "multi-track CUE: only the data track is kept"
        with DiscImage(path) as img:
            rep.fmt = img.description
            fs = FileSystem(img)
            rep.volume_id = fs.volume_id
            if fs.system_id.upper() != "PLAYSTATION":
                rep.warning = (rep.warning + "; " if rep.warning else "") + \
                    "system id '%s' - may not be a PS2 disc" % fs.system_id
            rep.videos = video.scan(fs)
            sectors = img.num_sectors
            for v in rep.compatible:
                old = (v.size + SECTOR - 1) // SECTOR
                new = (len(video.make_blank(v, duration)) + SECTOR - 1) // SECTOR
                if new < old:          # otherwise it is already blank / tiny
                    rep.targets.append(v)
                    if mode == "rebuild":
                        sectors -= old - new
            rep.est_size = sectors * SECTOR
            rep.video_count, rep.target_count = len(rep.videos), len(rep.targets)
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
                if cache is not None:
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
                        "%s %s.iso" % (stem, OUTPUT_TAG))


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
    from .cli import run_replace, _human  # late import: cli imports this module
    results = []
    todo = [r for r in reports if r.can_process]
    for i, rep in enumerate(todo):
        if cancel and cancel():
            log("Batch cancelled.")
            break
        out = output_path(rep, out_dir)
        log("[%d/%d] %s" % (i + 1, len(todo), rep.name))
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
            if rep.cached or not rep.targets:
                # cached rows only hold display data - analyse the image now
                log("  analysing...")
                fresh = estimate(rep.path, duration, mode)
                if fresh.error:
                    raise rebuild.RebuildError(fresh.error)
                fresh.size = rep.size
                rep = fresh
                if not rep.targets:
                    raise rebuild.RebuildError("no replaceable videos")
            log_.info("processing %s -> %s (%s, %.2fs, verify=%s)", rep.path, out, mode,
                      duration, verify)
            run_replace(rep.path, out, rep.targets, duration, mode, verify,
                        log=lambda m: log("  " + m), progress=prog)
            size = os.path.getsize(out)
            log("  saved %s" % _human(rep.size - size))
            results.append(BatchResult(rep, out, True, "ok", size))
        except Cancelled:
            _remove(out)
            log("  cancelled - partial output deleted")
            results.append(BatchResult(rep, out, False, "cancelled"))
            break
        except Exception as e:  # keep going with the remaining images
            _remove(out)
            log("  FAILED: %s" % e)
            log_.error("batch item %s failed: %s", rep.path, e, exc_info=True)
            results.append(BatchResult(rep, out, False, str(e)))
        if progress:
            progress(i + 1, len(todo), 0.0)
    return results


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
