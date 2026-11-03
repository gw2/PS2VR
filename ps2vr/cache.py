"""CSV cache of folder-scan results, so unchanged images are not re-analysed.

A row is reused only when the image's files have the same total size and
modification time and the scan used the same mode and blank length (both
change the estimated output size), and was made by the same scanner version
(SCANNER is bumped whenever detection changes). The cache only holds what the batch list
displays; processing an image always re-analyses it first.
"""

import csv
import logging
import os
import time

log = logging.getLogger("ps2vr.cache")

SCANNER = "9"   # bump when what a scan records changes, so old rows are rescanned

FIELDS = ["scanner", "path", "size", "mtime_ns", "mode", "duration", "format", "volume_id", "serial",
          "videos", "targets", "target_names", "est_size", "iso_size", "pad_safe", "pad_suspect",
          "pad_warning", "error", "warning", "scanned"]


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def fingerprint(files):
    size = mtime = 0
    for f in files:
        st = os.stat(f)
        size += st.st_size
        mtime = max(mtime, st.st_mtime_ns)
    return size, mtime


class ScanCache:
    def __init__(self, path):
        self.path = path
        self.rows = {}
        self.dirty = False
        self.hits = self.misses = 0
        self.load()

    def load(self):
        self.rows = {}
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("path"):
                        self.rows[_key(row["path"])] = row
        except FileNotFoundError:
            pass
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            log.warning("ignoring unreadable cache %s: %s", self.path, e)
            self.rows = {}
        log.debug("loaded %d cached scan rows from %s", len(self.rows), self.path)

    def save(self):
        if not self.dirty:
            return
        # drop rows for images that no longer exist
        self.rows = {k: r for k, r in self.rows.items() if os.path.exists(r["path"])}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, FIELDS)
                w.writeheader()
                for r in sorted(self.rows.values(), key=lambda r: r["path"].lower()):
                    w.writerow({k: r.get(k, "") for k in FIELDS})
            os.replace(tmp, self.path)
            self.dirty = False
            log.debug("saved %d cached scan rows to %s", len(self.rows), self.path)
        except OSError as e:
            log.error("could not write scan cache %s: %s", self.path, e)

    def clear(self):
        self.rows = {}
        self.dirty = False
        try:
            os.remove(self.path)
        except OSError:
            pass
        log.info("scan cache cleared")

    def __len__(self):
        return len(self.rows)

    def get(self, path, files, mode, duration):
        """Return an ImageReport rebuilt from the cache, or None on a miss."""
        from .batch import ImageReport
        row = self.rows.get(_key(path))
        try:
            size, mtime = fingerprint(files)
        except OSError:
            return None
        if row is None or row.get("scanner") != SCANNER or int(row["size"]) != size or \
                int(row["mtime_ns"]) != mtime or row["mode"] != mode or abs(float(row["duration"]) - duration) > 1e-6:
            self.misses += 1
            log.debug("cache miss: %s", path)
            return None
        self.hits += 1
        log.debug("cache hit: %s (scanned %s)", path, row["scanned"])
        rep = ImageReport(path, files=list(files), size=size, fmt=row["format"],
                          volume_id=row["volume_id"], serial=row["serial"], mode=mode,
                          error=row["error"],
                          warning=row["warning"],
                          est_size=int(row["est_size"]) if row["est_size"] else None)
        rep.iso_size = int(row["iso_size"] or 0)
        rep.video_count = int(row["videos"] or 0)
        rep.target_count = int(row["targets"] or 0)
        rep.pad_safe = int(row["pad_safe"] or 0)
        rep.pad_suspect = int(row["pad_suspect"] or 0)
        rep.pad_warning = row["pad_warning"]
        rep.cached = True
        return rep

    def put(self, rep, mode, duration):
        try:
            size, mtime = fingerprint(rep.files or [rep.path])
        except OSError:
            return
        self.rows[_key(rep.path)] = {
            "scanner": SCANNER, "path": os.path.abspath(rep.path), "size": size, "mtime_ns": mtime,
            "mode": mode, "duration": repr(float(duration)), "format": rep.fmt,
            "volume_id": rep.volume_id, "serial": rep.serial, "videos": rep.video_count,
            "targets": rep.target_count,
            "target_names": ";".join(v.path for v in rep.targets),
            "est_size": "" if rep.est_size is None else rep.est_size,
            "iso_size": rep.iso_size,
            "pad_safe": rep.pad_safe, "pad_suspect": rep.pad_suspect,
            "pad_warning": rep.pad_warning,
            "error": rep.error, "warning": rep.warning,
            "scanned": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.dirty = True
