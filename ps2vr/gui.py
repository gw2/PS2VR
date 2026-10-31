"""Tkinter GUI for PS2VR."""

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import __version__, cli, batch, config
from .cache import ScanCache

logger = logging.getLogger("ps2vr.gui")

IMAGE_TYPES = [("PS2 disc images", "*.iso *.bin *.cue *.mdf *.mds *.img"),
               ("All files", "*.*")]
CHECK, UNCHECK, NA = "☑", "☐", "  "


class Context:
    """State shared by all tabs: settings, scan cache and the processing options."""

    def __init__(self, settings):
        self.settings = settings
        self.cache = ScanCache(settings.cache_path)
        self.mode_var = tk.StringVar(value=settings["mode"])
        self.dur_var = tk.DoubleVar(value=settings["duration"])
        self.verify_var = tk.BooleanVar(value=settings["verify"])

    def duration(self):
        try:
            return max(0.01, float(self.dur_var.get()))
        except (tk.TclError, ValueError):
            return 1.0

    def store_options(self):
        self.settings["mode"] = self.mode_var.get()
        self.settings["duration"] = self.duration()
        self.settings["verify"] = bool(self.verify_var.get())


class _Tab(ttk.Frame):
    """Background-thread plumbing shared by the tabs.

    Subclasses create self.log (Text), self.pb (Progressbar) and self.buttons
    (disabled while a job runs).
    """

    def __init__(self, parent, ctx):
        super().__init__(parent, padding=10)
        self.ctx = ctx
        self.settings = ctx.settings
        self.mode_var, self.dur_var, self.verify_var = ctx.mode_var, ctx.dur_var, ctx.verify_var
        self.q = queue.Queue()
        self.busy = False
        self.buttons = []
        self.after(100, self.poll)

    def write_log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.write_log(val)
                elif kind == "progress":
                    self.pb["value"] = val * 1000
                elif kind == "call":
                    val()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def run_bg(self, fn):
        if self.busy:
            return
        self.busy = True
        for b in self.buttons:
            b.state(["disabled"])

        def wrapper():
            try:
                fn()
            except Exception as e:  # report every failure in the GUI
                msg = str(e) or e.__class__.__name__
                logger.error("background task failed: %s", msg, exc_info=True)
                self.q.put(("log", "ERROR: " + msg))
                self.q.put(("call", lambda: messagebox.showerror("PS2VR", msg)))
            finally:
                self.q.put(("call", self.done))
        threading.Thread(target=wrapper, daemon=True).start()

    def done(self):
        self.busy = False
        for b in self.buttons:
            b.state(["!disabled"])

    def options_frame(self, row):
        """Mode / blank length / verify options (shared between tabs via Context)."""
        opts = ttk.LabelFrame(self, text="Options", padding=6)
        opts.grid(row=row, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Radiobutton(opts, text="Rebuild (shrink ISO)", value="rebuild",
                        variable=self.mode_var).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(opts, text="In-place (keep file locations, zero-fill)", value="inplace",
                        variable=self.mode_var).grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(opts, text="Blank length (s):").grid(row=0, column=2, padx=(20, 2))
        ttk.Spinbox(opts, from_=0.03, to=10, increment=0.5, width=6,
                    textvariable=self.dur_var).grid(row=0, column=3)
        ttk.Checkbutton(opts, text="Verify output", variable=self.verify_var).grid(
            row=0, column=4, padx=(20, 0))
        return opts

    def duration(self):
        return self.ctx.duration()

    def store(self):
        """Copy this tab's remembered fields into settings (called on exit)."""

    def make_tree(self, row, cols):
        """cols: (id, heading, width, anchor, stretch). Click a heading to sort by it."""
        tv = ttk.Treeview(self, columns=[c[0] for c in cols], show="headings", selectmode="none")
        self._headings = {c[0]: (c[1] or "☑") for c in cols}
        self.sort_col, self.sort_desc = None, False
        for c, t, w, a, s in cols:
            tv.heading(c, text=self._headings[c] if c == "sel" else t,
                       command=lambda c=c: self.sort_by(c))
            tv.column(c, width=w, anchor=a, stretch=s)
        tv.tag_configure("bad", foreground="#999")
        tv.grid(row=row, column=0, columnspan=4, sticky="nsew", pady=4)
        sb = ttk.Scrollbar(self, orient="vertical", command=tv.yview)
        sb.grid(row=row, column=4, sticky="ns")
        tv.configure(yscrollcommand=sb.set)
        return tv

    def sort_key(self, iid, col):
        """Value to sort row `iid` by in column `col`; None sorts last. Tabs override this."""
        return self.tree.set(iid, col).lower()

    def sort_by(self, col, desc=None):
        """Heading click: sort by `col`, reversing the order on a second click."""
        if desc is None:
            desc = not self.sort_desc if col == self.sort_col else False
        self.sort_col, self.sort_desc = col, desc
        self.apply_sort()

    def apply_sort(self):
        col = self.sort_col
        if col is None:
            return
        rows = [(self.sort_key(i, col), i) for i in self.tree.get_children()]
        have = sorted((r for r in rows if r[0] is not None), key=lambda r: r[0],
                      reverse=self.sort_desc)
        missing = [r for r in rows if r[0] is None]       # blanks/errors always at the bottom
        for pos, (_, iid) in enumerate(have + missing):
            self.tree.move(iid, "", pos)
        for c, text in self._headings.items():
            arrow = (" ▼" if self.sort_desc else " ▲") if c == col else ""
            self.tree.heading(c, text=text + arrow)

    def make_log(self, row):
        self.pb = ttk.Progressbar(self, maximum=1000)
        self.pb.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        self.log = tk.Text(self, height=8, wrap="word", state="disabled")
        self.log.grid(row=row + 1, column=0, columnspan=4, sticky="nsew")


class App(_Tab):
    """Single image tab."""

    def __init__(self, parent, ctx, image=None):
        super().__init__(parent, ctx)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)
        self.rowconfigure(7, weight=1)

        self.vids = []
        self.checked = set()
        remembered = self.settings["single_image"]
        self.image_var = tk.StringVar(value=image or remembered)
        self.out_var = tk.StringVar(value="" if image else self.settings["single_output"])
        self._out_for = None if image else remembered   # image the output path belongs to
        self.info_var = tk.StringVar(value="Open a PS2 disc image (ISO, BIN/CUE, MDF/MDS).")

        ttk.Label(self, text="Disc image:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.image_var).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(self, text="Browse...", command=self.browse_image).grid(row=0, column=2)
        self.analyze_btn = ttk.Button(self, text="Analyze", command=self.analyze)
        self.analyze_btn.grid(row=0, column=3, padx=(5, 0))

        ttk.Label(self, textvariable=self.info_var, foreground="#444").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 2))

        sel = ttk.Frame(self)
        sel.grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Button(sel, text="Select all compatible", command=self.select_all).pack(side="left")
        ttk.Button(sel, text="Select none", command=self.select_none).pack(side="left", padx=5)

        self.tree = self.make_tree(3, (
            ("sel", "", 32, "center", False), ("file", "File", 260, "w", True),
            ("size", "Size", 80, "e", False), ("status", "Status", 100, "w", False),
            ("details", "Details", 380, "w", True)))
        self.tree.bind("<Button-1>", self.toggle)
        self.tree.bind("<space>", self.toggle_focus)

        self.options_frame(4)

        ttk.Label(self, text="Output ISO:").grid(row=5, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.out_var).grid(row=5, column=1, sticky="ew", padx=5)
        ttk.Button(self, text="Browse...", command=self.browse_out).grid(row=5, column=2)
        self.go_btn = ttk.Button(self, text="Replace videos", command=self.replace)
        self.go_btn.grid(row=5, column=3, padx=(5, 0))

        self.make_log(6)
        self.buttons = [self.analyze_btn, self.go_btn]
        if image:
            self.after(200, self.analyze)

    def browse_image(self):
        p = filedialog.askopenfilename(title="Open PS2 disc image", filetypes=IMAGE_TYPES)
        if p:
            self.image_var.set(p)
            self.analyze()

    def store(self):
        self.settings["single_image"] = self.image_var.get().strip()
        self.settings["single_output"] = self.out_var.get().strip()

    def browse_out(self):
        p = filedialog.asksaveasfilename(title="Save output ISO", defaultextension=".iso",
                                         filetypes=[("ISO image", "*.iso")],
                                         initialfile=os.path.basename(self.out_var.get()))
        if p:
            self.out_var.set(p)

    def analyze(self):
        path = self.image_var.get().strip().strip('"')
        if not path or not os.path.exists(path):
            messagebox.showwarning("PS2VR", "Choose an existing disc image first.")
            return
        if not self.out_var.get() or self._out_for != path:
            self.out_var.set(cli.default_output(path))
            self._out_for = path
        self.tree.delete(*self.tree.get_children())
        self.write_log("Analyzing %s ..." % path)

        def work():
            desc, fs, vids = cli.analyze(path)

            def show():
                self.vids = vids
                self.checked = {i for i, v in enumerate(vids) if v.compatible}
                for i, v in enumerate(vids):
                    self.tree.insert("", "end", iid=str(i), values=(
                        CHECK if i in self.checked else (UNCHECK if v.compatible else NA),
                        v.path, cli._human(v.size), v.status,
                        v.describe() + ((" - " + v.detail) if v.detail else "")),
                        tags=() if v.compatible else ("bad",))
                self.apply_sort()
                ok = [v for v in vids if v.compatible]
                self.info_var.set("%s  |  %s, %s  |  %d files  |  %d videos, %d replaceable (%s)" % (
                    fs.volume_id or "(no label)", desc,
                    "ISO9660+UDF" if fs.udf else "ISO9660", len(fs.files),
                    len(vids), len(ok), cli._human(sum(v.size for v in ok))))
                self.write_log("Found %d video(s); %d compatible." % (len(vids), len(ok)))
            self.q.put(("call", show))
        self.run_bg(work)

    def sort_key(self, iid, col):
        v = self.vids[int(iid)]
        if col == "sel":       # selected first, then unselected, unsupported last
            return None if not v.compatible else (0 if int(iid) in self.checked else 1)
        if col == "size":
            return v.size
        if col == "file":
            return v.path.lower()
        if col == "status":
            return (0 if v.compatible else 1, v.status)
        return super().sort_key(iid, col)

    def _refresh_checks(self):
        for i, v in enumerate(self.vids):
            if v.compatible:
                self.tree.set(str(i), "sel", CHECK if i in self.checked else UNCHECK)

    def toggle(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.focus(row)
            self._toggle_row(int(row))

    def toggle_focus(self, _event):
        row = self.tree.focus()
        if row:
            self._toggle_row(int(row))

    def _toggle_row(self, i):
        if not self.vids[i].compatible:
            return
        self.checked ^= {i}
        self._refresh_checks()

    def select_all(self):
        self.checked = {i for i, v in enumerate(self.vids) if v.compatible}
        self._refresh_checks()

    def select_none(self):
        self.checked = set()
        self._refresh_checks()

    def replace(self):
        chosen = [self.vids[i] for i in sorted(self.checked)]
        if not chosen:
            messagebox.showinfo("PS2VR", "Select at least one compatible video.")
            return
        src = self.image_var.get().strip().strip('"')
        out = self.out_var.get().strip().strip('"')
        if not out:
            messagebox.showwarning("PS2VR", "Choose an output file.")
            return
        if os.path.exists(out) and not messagebox.askyesno("PS2VR", "%s exists. Overwrite?" % out):
            return
        dur = self.duration()
        mode, verify = self.mode_var.get(), self.verify_var.get()

        def log(m):
            self.q.put(("log", m))

        def prog(done, total, *_):
            self.q.put(("progress", done / max(1, total)))

        def work():
            with cli.DiscImage(src) as img:
                before = os.path.getsize(img.data_path)
            cli.run_replace(src, out, chosen, dur, mode, verify, log=log, progress=prog)
            after = os.path.getsize(out)
            log("Done. %s -> %s (saved %s)" % (cli._human(before), cli._human(after),
                                               cli._human(before - after)))
            self.q.put(("progress", 1.0))
            self.q.put(("call", lambda: messagebox.showinfo(
                "PS2VR", "Finished.\n\n%s\nSaved %s" % (out, cli._human(before - after)))))
        self.run_bg(work)


class BatchTab(_Tab):
    """Scan a folder of images, choose which to process, run them as a batch."""

    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)
        self.rowconfigure(7, weight=1)

        st = self.settings
        self.reports = []
        self.checked = set()
        self.cancel_flag = threading.Event()
        self.folder_var = tk.StringVar(value=st["batch_folder"])
        self.recursive_var = tk.BooleanVar(value=st["batch_recursive"])
        self.outdir_var = tk.StringVar(value=st["batch_output_dir"])
        self.overwrite_var = tk.BooleanVar(value=st["batch_overwrite"])
        self.info_var = tk.StringVar(value="Choose a folder containing PS2 disc images.")

        ttk.Label(self, text="Folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(self, text="Browse...", command=self.browse_folder).grid(row=0, column=2)
        scan = ttk.Frame(self)
        scan.grid(row=0, column=3, padx=(5, 0))
        self.scan_btn = ttk.Button(scan, text="Scan", command=self.scan)
        self.scan_btn.pack(side="left")
        self.rescan_btn = ttk.Button(scan, text="Rescan all", command=lambda: self.scan(force=True))
        self.rescan_btn.pack(side="left", padx=(5, 0))

        top = ttk.Frame(self)
        top.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 2))
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive_var).pack(side="left")
        ttk.Label(top, textvariable=self.info_var, foreground="#444").pack(side="left", padx=15)

        sel = ttk.Frame(self)
        sel.grid(row=2, column=0, columnspan=4, sticky="w")
        self.all_btn = ttk.Button(sel, text="Select all", command=self.select_all)
        self.all_btn.pack(side="left")
        self.none_btn = ttk.Button(sel, text="Select none", command=self.select_none)
        self.none_btn.pack(side="left", padx=5)

        self.tree = self.make_tree(3, (
            ("sel", "", 32, "center", False), ("image", "Image", 300, "w", True),
            ("size", "Size", 85, "e", False), ("after", "After", 85, "e", False),
            ("saves", "Saves", 85, "e", False), ("videos", "Videos", 230, "w", True)))
        self.tree.bind("<Button-1>", self.toggle)
        self.tree.bind("<space>", self.toggle_focus)

        self.options_frame(4)
        for var in (self.mode_var, self.dur_var):
            var.trace_add("write", self._options_changed)

        out = ttk.Frame(self)
        out.grid(row=5, column=0, columnspan=4, sticky="ew")
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Output folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(out, textvariable=self.outdir_var).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(out, text="Browse...", command=self.browse_outdir).grid(row=0, column=2)
        ttk.Checkbutton(out, text="Overwrite existing", variable=self.overwrite_var).grid(
            row=0, column=3, padx=8)
        self.go_btn = ttk.Button(out, text="Run batch", command=self.run_batch)
        self.go_btn.grid(row=0, column=4)
        self.cancel_btn = ttk.Button(out, text="Cancel", command=self.cancel_flag.set)
        self.cancel_btn.grid(row=0, column=5, padx=(5, 0))
        self.cancel_btn.state(["disabled"])
        ttk.Label(out, text="(leave empty to save next to each image as '<name> [PS2VR].iso')",
                  foreground="#666").grid(row=1, column=1, columnspan=4, sticky="w", padx=5)

        self.make_log(6)
        col, _, order = st["batch_sort"].partition(":")
        if col in self._headings:
            self.sort_by(col, order == "desc")
        self.buttons = [self.scan_btn, self.rescan_btn, self.go_btn, self.all_btn, self.none_btn]
        if st["batch_folder"] and os.path.isdir(st["batch_folder"]):
            self.after(300, self.scan)   # restore the last folder (instant when cached)

    def run_bg(self, fn):
        self.cancel_flag.clear()
        super().run_bg(fn)
        self.cancel_btn.state(["!disabled"])

    def done(self):
        super().done()
        self.cancel_btn.state(["disabled"])

    def store(self):
        st = self.settings
        st["batch_folder"] = self.folder_var.get().strip()
        st["batch_recursive"] = bool(self.recursive_var.get())
        st["batch_output_dir"] = self.outdir_var.get().strip()
        st["batch_overwrite"] = bool(self.overwrite_var.get())
        st["batch_sort"] = "%s:%s" % (self.sort_col, "desc" if self.sort_desc else "asc") \
            if self.sort_col else ""

    def _options_changed(self, *_):
        if self.reports and not self.busy:
            self.info_var.set("Options changed - click Scan to update the size estimates.")

    def browse_folder(self):
        p = filedialog.askdirectory(title="Folder with PS2 disc images")
        if p:
            self.folder_var.set(p)
            self.scan()

    def browse_outdir(self):
        p = filedialog.askdirectory(title="Output folder")
        if p:
            self.outdir_var.set(p)

    # ---------------------------------------------------------------- scan
    def _row(self, i, r):
        if r.error:
            sel, after, saves, vids = NA, "-", "-", "error: " + r.error
        else:
            sel = (CHECK if i in self.checked else UNCHECK) if r.can_process else NA
            after = cli._human(r.est_size) if r.est_size is not None else "-"
            saves = cli._human(r.savings) if r.can_process else "-"
            vids = "%d of %d replaceable" % (r.target_count, r.video_count)
            if r.cached:
                vids += "  (cached)"
            if r.warning:
                vids += " (%s)" % r.warning
        return (sel, r.name, cli._human(r.size), after, saves, vids)

    def _summary(self):
        chosen = [self.reports[i] for i in sorted(self.checked)]
        self.info_var.set("%d image(s), %d processable  |  selected: %d, saves %s" % (
            len(self.reports), sum(1 for r in self.reports if r.can_process),
            len(chosen), cli._human(sum(r.savings for r in chosen))))

    def scan(self, force=False):
        folder = self.folder_var.get().strip().strip('"')
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("PS2VR", "Choose an existing folder first.")
            return
        self.tree.delete(*self.tree.get_children())
        self.reports, self.checked = [], set()
        recursive, dur, mode = self.recursive_var.get(), self.duration(), self.mode_var.get()
        cache = self.ctx.cache if self.settings["cache_enabled"] else None
        self.store()
        if cache is not None:
            cache.hits = cache.misses = 0
        self.write_log("%s %s ..." % ("Rescanning" if force else "Scanning", folder))

        def work():
            def on_image(n, total, rep):
                def add():
                    i = len(self.reports)
                    self.reports.append(rep)
                    if rep.can_process:
                        self.checked.add(i)
                    self.tree.insert("", "end", iid=str(i), values=self._row(i, rep),
                                     tags=() if rep.can_process else ("bad",))
                    self.apply_sort()
                    self._summary()
                self.q.put(("call", add))
                self.q.put(("progress", n / total))
            use = None if (force or cache is None) else cache
            if force and cache is not None:
                # still record fresh results in the cache
                use = _WriteOnlyCache(cache)
            reps = batch.scan_folder(folder, recursive, dur, mode, on_image=on_image,
                                     cancel=self.cancel_flag.is_set, cache=use)
            msg = "Found %d image(s)." % len(reps) if reps else "No disc images found."
            if use is not None and reps:
                msg += " %d from cache, %d analysed." % (cache.hits, cache.misses)
            self.q.put(("log", msg))
        self.run_bg(work)

    def sort_key(self, iid, col):
        r = self.reports[int(iid)]
        if col == "image":
            return r.name.lower()
        if col == "size":
            return r.size
        if r.error:            # error rows: only name and size are meaningful
            return None
        if col == "sel":
            return None if not r.can_process else (0 if int(iid) in self.checked else 1)
        if col == "after":
            return r.est_size
        if col == "saves":
            return r.savings if r.can_process else None
        if col == "videos":
            return (r.target_count, r.video_count)
        return super().sort_key(iid, col)

    def _refresh(self):
        for i, r in enumerate(self.reports):
            self.tree.set(str(i), "sel", self._row(i, r)[0])
        self._summary()

    def toggle(self, event):
        row = self.tree.identify_row(event.y)
        if row and not self.busy:
            self.tree.focus(row)
            self._toggle_row(int(row))

    def toggle_focus(self, _event):
        row = self.tree.focus()
        if row and not self.busy:
            self._toggle_row(int(row))

    def _toggle_row(self, i):
        if self.reports[i].can_process:
            self.checked ^= {i}
            self._refresh()

    def select_all(self):
        self.checked = {i for i, r in enumerate(self.reports) if r.can_process}
        self._refresh()

    def select_none(self):
        self.checked = set()
        self._refresh()

    # ----------------------------------------------------------------- run
    def run_batch(self):
        chosen = [self.reports[i] for i in sorted(self.checked)]
        if not chosen:
            messagebox.showinfo("PS2VR", "Scan a folder and select at least one image.")
            return
        out_dir = self.outdir_var.get().strip().strip('"') or None
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        needed = sum(r.est_size or r.size for r in chosen)
        if not messagebox.askyesno("PS2VR", "Process %d image(s)?\n\nEstimated output: %s\n"
                                   "Estimated savings: %s" % (
                                       len(chosen), cli._human(needed),
                                       cli._human(sum(r.savings for r in chosen)))):
            return
        dur, mode = self.duration(), self.mode_var.get()
        verify, overwrite = self.verify_var.get(), self.overwrite_var.get()

        def log(m):
            self.q.put(("log", m))

        def prog(i, n, frac):
            self.q.put(("progress", (i + frac) / max(1, n)))

        def work():
            results = batch.run(chosen, out_dir, dur, mode, verify, overwrite, log=log,
                                progress=prog, cancel=self.cancel_flag.is_set)
            good = [r for r in results if r.ok]
            saved = sum(r.report.size - r.out_size for r in good)
            lines = ["%d of %d image(s) processed, saved %s." % (
                len(good), len(chosen), cli._human(saved))]
            lines += ["%s: %s" % (r.report.name, r.message) for r in results if not r.ok]
            log("Batch finished. " + lines[0])
            if len(good) == len(chosen):
                self.q.put(("progress", 1.0))
            show = messagebox.showinfo if len(good) == len(chosen) else messagebox.showwarning
            self.q.put(("call", lambda: show("PS2VR", "\n".join(lines))))
        self.run_bg(work)


class _WriteOnlyCache:
    """Cache wrapper for "Rescan all": never returns hits but stores fresh results."""

    def __init__(self, cache):
        self.cache = cache

    def get(self, *a):
        self.cache.misses += 1
        return None

    def put(self, *a):
        self.cache.put(*a)

    def save(self):
        self.cache.save()

    @property
    def hits(self):
        return self.cache.hits

    @property
    def misses(self):
        return self.cache.misses


class SettingsTab(ttk.Frame):
    def __init__(self, parent, ctx):
        super().__init__(parent, padding=12)
        self.ctx = ctx
        st = self.settings = ctx.settings
        self.columnconfigure(0, weight=1)

        # -------- logging
        lg = ttk.LabelFrame(self, text="Debug logging", padding=8)
        lg.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        lg.columnconfigure(1, weight=1)
        self.level_var = tk.StringVar(value=st["log_level"])
        self.logmode_var = tk.StringVar(value=st["log_mode"])
        self.keep_var = tk.IntVar(value=st["log_keep"])
        self.logdir_var = tk.StringVar(value=st["log_dir"])

        ttk.Label(lg, text="Log level:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(lg, textvariable=self.level_var, values=config.LOG_LEVELS,
                     state="readonly", width=10).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(lg, text="Off = no log file  |  Error / Warning = problems only  |  "
                           "Info = what was done  |  Debug = everything (disc layout, "
                           "per-video details, patched sectors, tracebacks)",
                  foreground="#666", wraplength=640).grid(row=1, column=0, columnspan=4,
                                                          sticky="w", pady=(2, 6))
        ttk.Label(lg, text="Log file:").grid(row=2, column=0, sticky="nw")
        modes = ttk.Frame(lg)
        modes.grid(row=2, column=1, columnspan=3, sticky="w", padx=5)
        for i, (key, text) in enumerate(config.LOG_MODES.items()):
            ttk.Radiobutton(modes, text=text, value=key, variable=self.logmode_var,
                            command=self._mode_changed).grid(row=i, column=0, sticky="w")
        keep = ttk.Frame(modes)
        keep.grid(row=1, column=1, sticky="w", padx=(12, 0))
        ttk.Label(keep, text="keep last").pack(side="left")
        self.keep_spin = ttk.Spinbox(keep, from_=1, to=999, width=5, textvariable=self.keep_var)
        self.keep_spin.pack(side="left", padx=4)
        ttk.Label(keep, text="log files").pack(side="left")

        ttk.Label(lg, text="Log folder:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(lg, textvariable=self.logdir_var).grid(row=3, column=1, sticky="ew", padx=5,
                                                          pady=(6, 0))
        ttk.Button(lg, text="Browse...", command=self._browse_logdir).grid(row=3, column=2,
                                                                           pady=(6, 0))
        ttk.Label(lg, text="(empty = 'logs' next to the program)", foreground="#666").grid(
            row=4, column=1, sticky="w", padx=5)
        self.curlog_var = tk.StringVar()
        ttk.Label(lg, textvariable=self.curlog_var, foreground="#444").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        lb = ttk.Frame(lg)
        lb.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(lb, text="Open current log", command=self._open_log).pack(side="left")
        ttk.Button(lb, text="Open log folder",
                   command=lambda: self._open(self._logdir())).pack(side="left", padx=5)

        # -------- cache
        cf = ttk.LabelFrame(self, text="Scan cache", padding=8)
        cf.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.cache_var = tk.BooleanVar(value=st["cache_enabled"])
        ttk.Checkbutton(cf, text="Remember scan results so unchanged images are not re-analysed",
                        variable=self.cache_var).grid(row=0, column=0, columnspan=3, sticky="w")
        self.cacheinfo_var = tk.StringVar()
        ttk.Label(cf, textvariable=self.cacheinfo_var, foreground="#444").grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(cf, text="Clear cache", command=self._clear_cache).grid(
            row=1, column=1, padx=10, pady=(4, 0))
        ttk.Label(cf, text="An image is re-analysed automatically when its size or modified "
                           "time changes, or the mode / blank length differ from the cached scan.",
                  foreground="#666", wraplength=640).grid(row=2, column=0, columnspan=3,
                                                          sticky="w", pady=(4, 0))

        # -------- defaults
        df = ttk.LabelFrame(self, text="Processing defaults (shared with the other tabs)",
                            padding=8)
        df.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Radiobutton(df, text="Rebuild (shrink ISO)", value="rebuild",
                        variable=ctx.mode_var).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(df, text="In-place (keep file locations)", value="inplace",
                        variable=ctx.mode_var).grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(df, text="Blank length (s):").grid(row=0, column=2, padx=(20, 2))
        ttk.Spinbox(df, from_=0.03, to=10, increment=0.5, width=6,
                    textvariable=ctx.dur_var).grid(row=0, column=3)
        ttk.Checkbutton(df, text="Verify output", variable=ctx.verify_var).grid(
            row=0, column=4, padx=(20, 0))

        # -------- files + buttons
        ff = ttk.LabelFrame(self, text="Files", padding=8)
        ff.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(ff, text="Settings: " + st.path).grid(row=0, column=0, sticky="w")
        ttk.Label(ff, text="Scan cache: " + st.cache_path).grid(row=1, column=0, sticky="w")
        ttk.Button(ff, text="Open folder",
                   command=lambda: self._open(os.path.dirname(st.path))).grid(
            row=0, column=1, rowspan=2, padx=10)

        bf = ttk.Frame(self)
        bf.grid(row=4, column=0, sticky="e")
        ttk.Button(bf, text="Reset to defaults", command=self._reset).pack(side="left")
        ttk.Button(bf, text="Save settings", command=self.save).pack(side="left", padx=(5, 0))
        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7").grid(
            row=5, column=0, sticky="e", pady=(4, 0))

        self._mode_changed()
        self.refresh()

    # ------------------------------------------------------------ helpers
    def refresh(self):
        cur = config.current_log_file()
        self.curlog_var.set("Current log: " + (cur or "(logging is off)"))
        self.cacheinfo_var.set("%d image(s) cached" % len(self.ctx.cache))

    def _mode_changed(self):
        self.keep_spin.state(["!disabled"] if self.logmode_var.get() == "new" else ["disabled"])

    def _logdir(self):
        d = self.logdir_var.get().strip() or os.path.join(os.path.dirname(self.settings.path),
                                                          "logs")
        os.makedirs(d, exist_ok=True)
        return d

    def _browse_logdir(self):
        p = filedialog.askdirectory(title="Log folder")
        if p:
            self.logdir_var.set(p)

    def _open(self, path):
        try:
            os.startfile(path)
        except (OSError, AttributeError) as e:
            messagebox.showerror("PS2VR", "Cannot open %s:\n%s" % (path, e))

    def _open_log(self):
        cur = config.current_log_file()
        if not cur:
            messagebox.showinfo("PS2VR", "Logging is off. Choose a log level and save.")
            return
        for h in logging.getLogger("ps2vr").handlers:
            h.flush()
        self._open(cur)

    def _clear_cache(self):
        if messagebox.askyesno("PS2VR", "Delete all %d cached scan result(s)?" % len(self.ctx.cache)):
            self.ctx.cache.clear()
            self.refresh()

    def _reset(self):
        if not messagebox.askyesno("PS2VR", "Reset logging, cache and processing defaults?\n"
                                            "(Remembered folders are kept.)"):
            return
        d = config.DEFAULTS
        self.level_var.set(d["log_level"])
        self.logmode_var.set(d["log_mode"])
        self.keep_var.set(d["log_keep"])
        self.logdir_var.set(d["log_dir"])
        self.cache_var.set(d["cache_enabled"])
        self.ctx.mode_var.set(d["mode"])
        self.ctx.dur_var.set(d["duration"])
        self.ctx.verify_var.set(d["verify"])
        self._mode_changed()
        self.save()

    def store(self):
        st = self.settings
        old = (st["log_level"], st["log_mode"], st["log_dir"])
        st["log_level"] = self.level_var.get()
        st["log_mode"] = self.logmode_var.get()
        try:
            st["log_keep"] = max(1, int(self.keep_var.get()))
        except (tk.TclError, ValueError):
            pass
        st["log_dir"] = self.logdir_var.get().strip()
        st["cache_enabled"] = bool(self.cache_var.get())
        self.ctx.store_options()
        return old != (st["log_level"], st["log_mode"], st["log_dir"])

    def save(self):
        logging_changed = self.store()
        self.master.event_generate("<<StoreAll>>")
        self.settings.save()
        if logging_changed:
            path = config.setup_logging(self.settings, restart=True)
            logger.info("logging reconfigured from the settings tab")
            self.status_var.set("Saved. Logging now goes to %s" % path if path else
                                "Saved. Logging is off.")
        else:
            self.status_var.set("Saved to %s" % self.settings.path)
        self.refresh()


def main(argv=()):
    settings = config.Settings()
    config.setup_logging(settings)
    root = tk.Tk()
    root.title("PS2VR - PlayStation 2 Video Replacer %s" % __version__)
    root.geometry(settings["window_geometry"])
    root.minsize(760, 520)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    ctx = Context(settings)
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    image = argv[0] if argv else None
    tabs = [App(nb, ctx, image), BatchTab(nb, ctx), SettingsTab(nb, ctx)]
    for tab, text in zip(tabs, ("Single image", "Batch (folder)", "Settings")):
        nb.add(tab, text=text)
    if not image and 0 <= settings["last_tab"] < len(tabs):
        nb.select(settings["last_tab"])
    nb.bind("<<NotebookTabChanged>>", lambda e: tabs[2].refresh())

    def store_all(_e=None):
        for t in tabs:
            t.store()
        settings["last_tab"] = nb.index(nb.select())
        if root.state() == "normal":
            settings["window_geometry"] = root.geometry()
    nb.bind("<<StoreAll>>", store_all)

    def on_close():
        store_all()
        settings.save()
        ctx.cache.save()
        logger.info("PS2VR closed")
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    def excepthook(exc, val, tb):
        logger.error("unhandled GUI error", exc_info=(exc, val, tb))
        messagebox.showerror("PS2VR", "%s: %s" % (exc.__name__, val))
    root.report_callback_exception = excepthook
    logger.info("GUI started (data folder %s)", os.path.dirname(settings.path))
    root.mainloop()
    return 0
