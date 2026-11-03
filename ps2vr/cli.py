"""Command line interface for PS2VR."""

import argparse
import json
import logging
import math
import os
import re
import sys
import time

from . import __version__, compat, video, rebuild, batch, config, padding
from .batch import _human
from .cache import ScanCache, WriteOnlyCache
from .disc import DiscImage, SECTOR
from .fs import FileSystem

logger = logging.getLogger("ps2vr")


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


def analyze(path):
    with DiscImage(path) as img:
        fs = FileSystem(img)
        vids = video.scan(fs)
        pads, unowned = padding.scan(fs, vids)
        return (img.description, fs, vids, pads, unowned, padding.lba_warning(fs, pads),
                compat.lookup(compat.serial_of(fs)))


def cmd_analyze(a):
    desc, fs, vids, pads, unowned, lba_warning, game = analyze(a.image)
    if a.json:
        print(json.dumps({
            "image": a.image, "format": desc, "volume_id": fs.volume_id,
            "system_id": fs.system_id, "sectors": fs.volume_sectors,
            "udf": fs.udf is not None, "files": len(fs.files),
            "game": game and {"serial": game.serial, "title": game.title, "region": game.region,
                              "tested": game.describe()},
            "report_url": compat.report_url(game),
            "videos": [{
                "path": v.path, "format": v.fmt, "status": v.status,
                "detail": v.detail, "size": v.size, "lba": v.lba,
                "width": v.width, "height": v.height, "fps": v.fps,
                "duration": v.duration, "audio": v.audio,
            } for v in vids],
            "padding": [{
                "path": p.path, "kind": p.kind, "status": p.status, "lba": p.start,
                "size": p.size, "fill": p.fill,
            } for p in pads], "unowned_data": unowned, "padding_warning": lba_warning}, indent=2))
        return 0
    print("Image   : %s" % a.image)
    print("Format  : %s" % desc)
    print("Volume  : %s (%s), %d sectors, %s, %d files" % (
        fs.volume_id, fs.system_id, fs.volume_sectors,
        "ISO9660+UDF" if fs.udf else "ISO9660", len(fs.files)))
    if game:
        print("Game    : %s (%s, %s)" % (game.title, game.serial, game.region))
        if game.describe():
            print("Tested  : %s" % game.describe())
            print("          (boot to the intro or menu in PCSX2, not gameplay)")
        else:
            print("Tested  : not yet")
    print("Report  : tested it? Add your result to the list: %s" % compat.report_url(game))
    cut = fs.truncated()
    if cut:
        print("WARNING : image is truncated - %d file(s) extend past the end (bad dump?); "
              "it cannot be processed." % len(cut))
    print_padding(pads, unowned, lba_warning)
    if not vids:
        print("\nNo video files found.")
        return 0 if pads else 1
    print("\n  #  %-34s %10s  %-12s %s" % ("File", "Size", "Status", "Details"))
    for n, v in enumerate(vids, 1):
        print("%3d  %-34s %10s  %-12s %s%s" % (
            n, v.path, _human(v.size), v.status, v.describe(),
            (" - " + v.detail) if v.detail else ""))
    ok = [v for v in vids if v.compatible]
    print("\n%d of %d videos can be replaced (%s reclaimable)." % (
        len(ok), len(vids), _human(sum(v.size for v in ok))))
    return 0


def print_padding(pads, unowned, lba_warning):
    if pads:
        print("\n     %-34s %10s  %-12s %s" % ("Padding", "Size", "Status", "Details"))
        for p in pads:
            print("     %-34s %10s  %-12s %s" % (p.label, _human(p.size), p.status, p.describe()))
        print("\nPadding: %s removable with --padding, %s more with --padding-suspect." % (
            _human(sum(p.size for p in pads if p.status == padding.SAFE)),
            _human(sum(p.size for p in pads if p.status == padding.SUSPECT))))
    if lba_warning:
        print("WARNING : %s." % lba_warning)
    if unowned:
        print("\nNote: %s of sectors outside any file hold data. The game reads them directly, "
              "so they are kept." % _human(unowned))


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


def default_output(path, vids=(), pads=(), mode="rebuild"):
    """Named after what is removed. In-place mode leaves blank padding as it is, so
    only suspects, whose content it does zero, count there."""
    tag = batch.output_tag(vids, mode == "rebuild" and any(p.status == padding.SAFE for p in pads),
                           any(p.status == padding.SUSPECT for p in pads))
    return "%s %s.iso" % (os.path.splitext(path)[0], tag)


def is_default_output(path, out):
    """Is `out` a name default_output could give, rather than the user's own choice?"""
    base = os.path.splitext(path)[0] + " "
    stem, ext = os.path.splitext(out)
    return (ext.lower() == ".iso" and stem.startswith(base) and
            batch.OUTPUT_RE.fullmatch(stem[len(base):]) is not None)


def _confirmed_plan(image, pads, reps, mode, log, progress):
    """Read every sector of the items, drop any that turn out not to be blank and
    plan the removal of the rest. Returns (items, replacements, gaps)."""
    tick = (lambda: progress(0, 1)) if progress else None   # lets a batch cancel
    good = []
    with DiscImage(image) as img:
        for p in pads:
            if padding.confirm(img, p, tick):
                good.append(p)
            else:
                log("%s holds data after all - keeping it" % p.label)
        fs = FileSystem(img)
        warning = padding.lba_warning(fs, good) if mode == "rebuild" else ""
        if warning:
            log("WARNING: %s." % warning)
        saved = sum(fs.files[k].sectors - (len(d) + SECTOR - 1) // SECTOR for k, d in reps.items())
        return (good,) + padding.plan_removal(fs, good, saved, mode,
                                              lambda m: log("WARNING: %s." % m))


def run_replace(image, out, vids, duration=1.0, mode="rebuild", verify=True,
                full_verify=False, log=print, progress=None, pads=()):
    ui_log = log

    def log(m):
        ui_log(m)
        logger.info(m.strip())
    rebuild.check_output(image, out)
    logger.info("replace: %s -> %s (%d video(s), %s, %.2fs)", image, out, len(vids), mode, duration)
    if mode == "inplace":
        pads = [p for p in pads if p.path]   # in-place keeps every sector, so a gap stays
    if not vids and not pads:
        raise rebuild.RebuildError("Nothing to do: in-place mode keeps unused sectors")
    with DiscImage(image) as img:
        game = compat.lookup(compat.serial_of(FileSystem(img)))
    for problem in compat.problems(game, mode, vids, pads):
        log("WARNING: %s." % problem)
    reps = {}
    for v in vids:
        data = video.make_blank(v, duration)
        reps[v.path.upper()] = data
        log("%-34s %10s -> %s" % (v.path, _human(v.size), _human(len(data))))
    gaps = ()
    if pads:
        log("Checking %d padding item(s) byte by byte..." % len(pads))
        pads, pad_reps, gaps = _confirmed_plan(image, pads, reps, mode, log, progress)
        reps.update(pad_reps)
        for p in pads:
            log("%-34s %10s -> %s" % (p.label, _human(p.size),
                                      "removed" if mode == "rebuild" else "zero-filled"))
    if not reps and not gaps:
        raise rebuild.RebuildError("Nothing to do: all of the padding holds data after all")
    part = out + ".part"     # a failed run leaves no output and keeps an earlier one
    rebuild.check_output(image, part)
    try:
        total = rebuild.write(image, part, reps, mode, progress=progress, log=log, gaps=gaps)
        if verify:
            log("Verifying output...")
            errs = rebuild.verify(image, part, reps, full=full_verify, log=log)
            if errs:
                for e in errs:
                    log("  FAIL " + e)
                    logger.error("verify: %s", e)
                raise rebuild.RebuildError("verification failed (%d problems)" % len(errs))
            log("Verification passed.")
        os.replace(part, out)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    log("Output: %s (%s)" % (out, _human(total * SECTOR)))
    return total


def cmd_replace(a):
    if a.output:
        rebuild.check_output(a.image, a.output)
    desc, fs, vids, pads, *_, game = analyze(a.image)
    chosen = select(vids, a.videos, a.all)
    pads = padding.choose(pads, a.padding_suspect) if a.padding or a.padding_suspect else []
    if not chosen and not pads:
        raise SystemExit("error: nothing selected (give video numbers/paths, --all or --padding)")
    out = a.output or default_output(a.image, chosen, pads, a.mode)
    with DiscImage(a.image) as img:
        before = os.path.getsize(img.data_path)
    run_replace(a.image, out, chosen, a.duration, a.mode, not a.no_verify,
                a.full_verify, progress=_bar("Writing"), pads=pads)
    after = os.path.getsize(out)
    print("Size: %s -> %s (saved %s)" % (_human(before), _human(after), _human(before - after)))
    print("Please test it, then add your result to the list: %s" % compat.report_url(game))
    return 0


def print_batch_table(reports):
    print("  #  %-46s %10s %10s %10s  %s" % ("Image", "Size", "After", "Saves", "Videos"))
    for n, r in enumerate(reports, 1):
        if r.error:
            print("%3d  %-46s %10s %10s %10s  error: %s" % (n, r.name[:46], _human(r.size), "-", "-", r.error))
            continue
        after = _human(r.after_size) if r.after_size is not None else "-"
        print("%3d  %-46s %10s %10s %10s  %s" % (
            n, r.name[:46], _human(r.size), after,
            _human(r.savings) if r.can_process else "-",
            r.contents))
    ok = [r for r in reports if r.can_process]
    print("\n%d image(s) found, %d can be processed; total savings %s." % (
        len(reports), len(ok), _human(sum(r.savings for r in ok))))
    if not any(r.use_padding for r in reports):
        print("Blank padding across these images: %s (add --padding to remove it)." %
              _human(sum(r.pad_safe for r in reports if not r.error)))


def cmd_batch(a):
    a.padding = a.padding or a.padding_only
    if a.padding and a.mode != "rebuild":
        raise SystemExit("error: --padding and --padding-only need rebuild mode "
                         "(in-place cannot shrink the image)")

    def scanned(n, total, rep):
        sys.stderr.write("\rScanning %d/%d" % (n, total))
        sys.stderr.flush()
    settings = config.Settings()
    cache = None
    if settings["cache_enabled"] and not a.no_cache:
        cache = ScanCache(settings.cache_path)
        if a.rescan:
            cache = WriteOnlyCache(cache)
    reports = batch.scan_folder(a.folder, a.recursive, a.duration, a.mode,
                                a.include_outputs, on_image=scanned, cache=cache)
    for r in reports:
        r.use_padding, r.padding_only = a.padding, a.padding_only
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
                raise SystemExit("error: %s has nothing to process" % r.name)
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
    if good:
        print("Please test them, then add your results to the list: %s" % compat.report_url())
    return 0 if len(good) == len(chosen) else 3


def cmd_blank(a):
    with DiscImage(a.image) as img:
        fs = FileSystem(img)
        vids = video.scan(fs)
        v = select(vids, [a.video], False)[0]
        data = video.make_blank(v, a.duration)
    rebuild.check_output(a.image, a.output)
    with open(a.output, "wb") as f:
        f.write(data)
    print("Wrote %s (%s) modelled on %s" % (a.output, _human(len(data)), v.path))
    return 0


def _safe_name(name):
    """A file name read from a disc, without anything that could leave the folder."""
    name = re.split(r"[\\/:]", name)[-1]
    if name in ("", ".", ".."):
        raise SystemExit("error: the disc gives this file an unusable name; choose one with -o")
    return name


def cmd_extract(a):
    with DiscImage(a.image) as img:
        fs = FileSystem(img)
        key = "/" + a.path.upper().lstrip("/\\").replace("\\", "/").split(";")[0]
        ent = fs.files.get(key)
        if ent is None:
            raise SystemExit("error: %s not found" % a.path)
        out = a.output or _safe_name(ent.name)
        if not a.output and os.path.exists(out):
            raise SystemExit("error: %s already exists; choose a name with -o" % out)
        rebuild.check_output(a.image, out)
        with open(out, "wb") as f:
            pos = 0
            while pos < ent.size:
                n = min(16 << 20, ent.size - pos)
                f.write(fs.read_file(ent, n, pos))
                pos += n
    print("Extracted %s -> %s" % (ent.path, out))
    return 0


def _duration(text):
    d = float(text)
    if not math.isfinite(d) or not 0 <= d <= video.MAX_DURATION:
        raise argparse.ArgumentTypeError("must be between 0 and %d seconds" % video.MAX_DURATION)
    return d


def build_parser():
    p = argparse.ArgumentParser(
        prog="ps2vr",
        description="PS2VR - PlayStation 2 Video Replacer. Replaces FMV files in PS2 "
                    "disc images with minimal blank videos and removes padding to "
                    "shrink the image. "
                    "Run without arguments to open the GUI.")
    p.add_argument("--version", action="version", version="PS2VR " + __version__)
    p.add_argument("--log-level", choices=config.LOG_LEVELS,
                   help="override the log level from settings.json for this run")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("analyze", aliases=["list"], help="list videos and padding on a disc image")
    s.add_argument("image", help="ISO / BIN / CUE / MDF / MDS")
    s.add_argument("--json", action="store_true", help="machine readable output")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("replace", help="replace videos and/or remove padding, writing a new ISO")
    s.add_argument("image")
    s.add_argument("videos", nargs="*", help="video numbers (from analyze) or paths")
    s.add_argument("--all", action="store_true", help="replace every compatible video")
    s.add_argument("-o", "--output", help="output ISO (default: '<name> [PS2VR-VP].iso': V videos, P padding, S suspects)")
    s.add_argument("--mode", choices=["rebuild", "inplace"], default="rebuild",
                   help="rebuild: shrink the ISO by moving later files (default); "
                        "inplace: keep all LBAs, zero-fill freed space")
    s.add_argument("--duration", type=_duration, default=1.0,
                   help="length of the blank video in seconds (default 1.0, min 1 frame)")
    s.add_argument("--no-verify", action="store_true", help="skip output verification")
    s.add_argument("--full-verify", action="store_true",
                   help="CRC-compare every file instead of sampling")
    s.add_argument("--padding", action="store_true",
                   help="also remove blank padding: unused sectors and blank files with filler names")
    s.add_argument("--padding-suspect", action="store_true",
                   help="also remove files that only look like padding (implies --padding; "
                        "test the result: the game may read them)")
    s.set_defaults(func=cmd_replace)

    s = sub.add_parser("batch", help="scan a folder of images; optionally process them")
    s.add_argument("folder")
    s.add_argument("images", nargs="*", help="image numbers (from the scan list) to process")
    s.add_argument("--all", action="store_true", help="process every image that has something to remove")
    s.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    s.add_argument("-o", "--output-dir", help="where to write ISOs (default: next to each image)")
    s.add_argument("--mode", choices=["rebuild", "inplace"], default="rebuild")
    s.add_argument("--duration", type=_duration, default=1.0)
    s.add_argument("--overwrite", action="store_true", help="replace an earlier output of the same name")
    s.add_argument("--padding", action="store_true",
                   help="also remove blank padding: unused sectors and blank files with filler names")
    s.add_argument("--padding-only", action="store_true",
                   help="remove blank padding and keep every video (implies --padding; rebuild mode)")
    s.add_argument("--include-outputs", action="store_true",
                   help="also list images already named [PS2VR...]")
    s.add_argument("--no-verify", action="store_true")
    s.add_argument("--rescan", action="store_true",
                   help="ignore cached scan results (the cache is refreshed)")
    s.add_argument("--no-cache", action="store_true", help="do not read or write the scan cache")
    s.set_defaults(func=cmd_batch)

    s = sub.add_parser("blank", help="write a single blank video modelled on a disc video")
    s.add_argument("image")
    s.add_argument("video")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--duration", type=_duration, default=1.0)
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
