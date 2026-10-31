"""Command line interface for PS2VR."""

import argparse
import json
import logging
import os
import sys
import time

from . import __version__, video, rebuild, batch, config
from .cache import ScanCache
from .disc import DiscImage
from .fs import FileSystem


def _bar(prefix):
    last = [0.0]

    def cb(done, total, *_):
        now = time.time()
        if now - last[0] < 0.25 and done < total:
            return
        last[0] = now
        pct = 100.0 * done / max(1, total)
        sys.stderr.write("\r%s %5.1f%%" % (prefix, pct))
        if done >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()
    return cb


def _human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            if unit == "B":
                return "%d B" % n
            return ("%.2f %s" if unit == "GiB" else "%.1f %s") % (n, unit)
        n /= 1024.0


def analyze(path):
    with DiscImage(path) as img:
        fs = FileSystem(img)
        vids = video.scan(fs)
        return img.description, fs, vids


def cmd_analyze(a):
    desc, fs, vids = analyze(a.image)
    if a.json:
        print(json.dumps({
            "image": a.image, "format": desc, "volume_id": fs.volume_id,
            "system_id": fs.system_id, "sectors": fs.volume_sectors,
            "udf": fs.udf is not None, "files": len(fs.files),
            "videos": [{
                "path": v.path, "format": v.fmt, "status": v.status,
                "detail": v.detail, "size": v.size, "lba": v.lba,
                "width": v.width, "height": v.height, "fps": v.fps,
                "duration": v.duration, "audio": v.audio,
            } for v in vids]}, indent=2))
        return 0
    print("Image   : %s" % a.image)
    print("Format  : %s" % desc)
    print("Volume  : %s (%s), %d sectors, %s, %d files" % (
        fs.volume_id, fs.system_id, fs.volume_sectors,
        "ISO9660+UDF" if fs.udf else "ISO9660", len(fs.files)))
    if not vids:
        print("\nNo video files found.")
        return 1
    print("\n  #  %-34s %10s  %-12s %s" % ("File", "Size", "Status", "Details"))
    for n, v in enumerate(vids, 1):
        print("%3d  %-34s %10s  %-12s %s%s" % (
            n, v.path, _human(v.size), v.status, v.describe(),
            (" - " + v.detail) if v.detail else ""))
    ok = [v for v in vids if v.compatible]
    print("\n%d of %d videos can be replaced (%s reclaimable)." % (
        len(ok), len(vids), _human(sum(v.size for v in ok))))
    return 0


def select(vids, names, all_):
    ok = [v for v in vids if v.compatible]
    if all_:
        return ok
    chosen = []
    for n in names:
        match = None
        if n.isdigit() and 1 <= int(n) <= len(vids):
            match = vids[int(n) - 1]
        else:
            key = n.upper().replace("\\", "/")
            if not key.startswith("/"):
                key = "/" + key
            key = key.split(";")[0]
            match = next((v for v in vids if v.path.upper() == key or
                          v.path.upper().endswith(key)), None)
        if match is None:
            raise SystemExit("error: no video matches '%s'" % n)
        if not match.compatible:
            raise SystemExit("error: %s is %s (%s)" % (match.path, match.status, match.detail))
        chosen.append(match)
    return chosen


def default_output(path):
    base = os.path.splitext(path)[0]
    return base + " [PS2VR].iso"


logger = logging.getLogger("ps2vr")


def run_replace(image, out, vids, duration=1.0, mode="rebuild", verify=True,
                full_verify=False, log=print, progress=None):
    ui_log = log

    def log(m):
        ui_log(m)
        logger.info(m.strip())
    logger.info("replace: %s -> %s (%d video(s), %s, %.2fs)", image, out, len(vids), mode, duration)
    reps = {}
    for v in vids:
        data = video.make_blank(v, duration)
        reps[v.path.upper()] = data
        log("%-34s %10s -> %s" % (v.path, _human(v.size), _human(len(data))))
    total = rebuild.write(image, out, reps, mode, progress=progress, log=log)
    log("Output: %s (%s)" % (out, _human(total * 2048)))
    if verify:
        log("Verifying output...")
        errs = rebuild.verify(image, out, reps, full=full_verify, log=log)
        if errs:
            for e in errs:
                log("  FAIL " + e)
                logger.error("verify: %s", e)
            raise rebuild.RebuildError("verification failed (%d problems)" % len(errs))
        log("Verification passed.")
    return total


def cmd_replace(a):
    desc, fs, vids = analyze(a.image)
    chosen = select(vids, a.videos, a.all)
    if not chosen:
        raise SystemExit("error: nothing selected (give video numbers/paths or --all)")
    out = a.output or default_output(a.image)
    with DiscImage(a.image) as img:
        before = os.path.getsize(img.data_path)
    run_replace(a.image, out, chosen, a.duration, a.mode, not a.no_verify,
                a.full_verify, progress=_bar("Writing"))
    after = os.path.getsize(out)
    print("Size: %s -> %s (saved %s)" % (_human(before), _human(after), _human(before - after)))
    return 0


def print_batch_table(reports):
    print("  #  %-46s %10s %10s %10s  %s" % ("Image", "Size", "After", "Saves", "Videos"))
    for n, r in enumerate(reports, 1):
        if r.error:
            print("%3d  %-46s %10s %10s %10s  error: %s" % (n, r.name[:46], _human(r.size), "-", "-", r.error))
            continue
        after = _human(r.est_size) if r.est_size is not None else "-"
        print("%3d  %-46s %10s %10s %10s  %d of %d replaceable%s" % (
            n, r.name[:46], _human(r.size), after,
            _human(r.savings) if r.can_process else "-",
            r.target_count, r.video_count, ("  (" + r.warning + ")") if r.warning else ""))
    ok = [r for r in reports if r.can_process]
    print("\n%d image(s) found, %d with replaceable videos; total savings %s." % (
        len(reports), len(ok), _human(sum(r.savings for r in ok))))


def cmd_batch(a):
    def scanned(n, total, rep):
        sys.stderr.write("\rScanning %d/%d" % (n, total))
        sys.stderr.flush()
    settings = config.Settings()
    cache = None
    if settings["cache_enabled"] and not a.no_cache:
        cache = ScanCache(settings.cache_path)
        if a.rescan:
            cache.rows = {}
    reports = batch.scan_folder(a.folder, a.recursive, a.duration, a.mode,
                                a.include_outputs, on_image=scanned, cache=cache)
    sys.stderr.write("\r" + " " * 30 + "\r")
    if not reports:
        print("No disc images found in %s" % a.folder)
        return 1
    print_batch_table(reports)
    if not (a.all or a.images):
        print("\nRe-run with --all or image numbers to process them.")
        return 0
    if a.all:
        chosen = [r for r in reports if r.can_process]
    else:
        chosen = []
        for n in a.images:
            if not n.isdigit() or not 1 <= int(n) <= len(reports):
                raise SystemExit("error: '%s' is not an image number from the list" % n)
            r = reports[int(n) - 1]
            if not r.can_process:
                raise SystemExit("error: %s has nothing to replace" % r.name)
            chosen.append(r)
    if a.output_dir:
        os.makedirs(a.output_dir, exist_ok=True)
    bar = _bar("Batch")
    results = batch.run(chosen, a.output_dir, a.duration, a.mode, not a.no_verify,
                        a.overwrite, progress=lambda i, n, f: bar(i + f, n))
    good = [r for r in results if r.ok]
    print("\n%d of %d image(s) processed, saved %s." % (
        len(good), len(chosen), _human(sum(r.report.size - r.out_size for r in good))))
    for r in results:
        if not r.ok:
            print("  %s: %s" % (r.report.name, r.message))
    return 0 if len(good) == len(chosen) else 3


def cmd_blank(a):
    with DiscImage(a.image) as img:
        fs = FileSystem(img)
        vids = video.scan(fs)
        v = select(vids, [a.video], False)[0]
        data = video.make_blank(v, a.duration)
    with open(a.output, "wb") as f:
        f.write(data)
    print("Wrote %s (%s) modelled on %s" % (a.output, _human(len(data)), v.path))
    return 0


def cmd_extract(a):
    with DiscImage(a.image) as img:
        fs = FileSystem(img)
        key = "/" + a.path.upper().lstrip("/\\").replace("\\", "/").split(";")[0]
        ent = fs.files.get(key)
        if ent is None:
            raise SystemExit("error: %s not found" % a.path)
        out = a.output or ent.name
        with open(out, "wb") as f:
            pos = 0
            while pos < ent.size:
                n = min(16 << 20, ent.size - pos)
                f.write(fs.read_file(ent, n, pos))
                pos += n
    print("Extracted %s -> %s" % (ent.path, out))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="ps2vr",
        description="PS2VR - PlayStation 2 Video Replacer. Replaces FMV files in PS2 "
                    "disc images with minimal blank videos to shrink the image. "
                    "Run without arguments to open the GUI.")
    p.add_argument("--version", action="version", version="PS2VR " + __version__)
    p.add_argument("--log-level", choices=config.LOG_LEVELS,
                   help="override the log level from settings.json for this run")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("analyze", aliases=["list"], help="list videos on a disc image")
    s.add_argument("image", help="ISO / BIN / CUE / MDF / MDS")
    s.add_argument("--json", action="store_true", help="machine readable output")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("replace", help="replace videos and write a new ISO")
    s.add_argument("image")
    s.add_argument("videos", nargs="*", help="video numbers (from analyze) or paths")
    s.add_argument("--all", action="store_true", help="replace every compatible video")
    s.add_argument("-o", "--output", help="output ISO (default: '<name> [PS2VR].iso')")
    s.add_argument("--mode", choices=["rebuild", "inplace"], default="rebuild",
                   help="rebuild: shrink the ISO by moving later files (default); "
                        "inplace: keep all LBAs, zero-fill freed space")
    s.add_argument("--duration", type=float, default=1.0,
                   help="length of the blank video in seconds (default 1.0, min 1 frame)")
    s.add_argument("--no-verify", action="store_true", help="skip output verification")
    s.add_argument("--full-verify", action="store_true",
                   help="CRC-compare every file instead of sampling")
    s.set_defaults(func=cmd_replace)

    s = sub.add_parser("batch", help="scan a folder of images; optionally process them")
    s.add_argument("folder")
    s.add_argument("images", nargs="*", help="image numbers (from the scan list) to process")
    s.add_argument("--all", action="store_true", help="process every image with replaceable videos")
    s.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    s.add_argument("-o", "--output-dir", help="where to write ISOs (default: next to each image)")
    s.add_argument("--mode", choices=["rebuild", "inplace"], default="rebuild")
    s.add_argument("--duration", type=float, default=1.0)
    s.add_argument("--overwrite", action="store_true", help="replace existing [PS2VR] outputs")
    s.add_argument("--include-outputs", action="store_true",
                   help="also list images already named [PS2VR]")
    s.add_argument("--no-verify", action="store_true")
    s.add_argument("--rescan", action="store_true",
                   help="ignore cached scan results (the cache is refreshed)")
    s.add_argument("--no-cache", action="store_true", help="do not read or write the scan cache")
    s.set_defaults(func=cmd_batch)

    s = sub.add_parser("blank", help="write a single blank video modelled on a disc video")
    s.add_argument("image")
    s.add_argument("video")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--duration", type=float, default=1.0)
    s.set_defaults(func=cmd_blank)

    s = sub.add_parser("extract", help="extract a file from the image")
    s.add_argument("image")
    s.add_argument("path")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_extract)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 1
    config.setup_logging(config.Settings(), a.log_level)
    logger.info("command line: %s", " ".join(argv if argv is not None else sys.argv[1:]))
    try:
        return a.func(a)
    except (rebuild.RebuildError, OSError, ValueError) as e:
        logger.error("command failed: %s", e, exc_info=True)
        print("error: %s" % e, file=sys.stderr)
        return 2
    except Exception:
        logger.exception("unexpected error")
        raise
