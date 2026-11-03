"""Persistent settings (settings.json) and debug logging setup.

Everything is stored next to the program (portable, like PCSX2): the folder
containing ps2vr.py, or the executable when frozen. If that folder is not
writable, %APPDATA%\\PS2VR is used instead.
"""

import glob
import json
import logging
import os
import sys
import time

log = logging.getLogger("ps2vr")

LOG_LEVELS = ["Off", "Error", "Warning", "Info", "Debug"]
LOG_MODES = {"overwrite": "Overwrite ps2vr.log each run",
             "new": "New timestamped log each run",
             "append": "Append to ps2vr.log"}

DEFAULTS = {
    # remembered state
    "single_image": "",
    "single_output": "",
    "batch_folder": "",
    "batch_recursive": False,
    "batch_output_dir": "",
    "batch_overwrite": False,
    "batch_sort": "",       # "<column>:asc|desc" of the batch table
    "batch_padding": False,
    "batch_padding_only": False,
    "last_tab": 0,
    "window_geometry": "960x680",
    # processing defaults (shared by both tabs)
    "mode": "rebuild",
    "duration": 1.0,
    "verify": True,
    # scan cache
    "cache_enabled": True,
    # logging
    "log_level": "Info",
    "log_mode": "overwrite",
    "log_keep": 10,
    "log_dir": "",          # empty = <app folder>/logs
}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """Folder for settings.json / scan_cache.csv / logs."""
    d = app_dir()
    try:
        probe = os.path.join(d, ".ps2vr_write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return d
    except OSError:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PS2VR")
        os.makedirs(d, exist_ok=True)
        return d


class Settings(dict):
    def __init__(self, path=None):
        super().__init__(DEFAULTS)
        self.path = path or os.path.join(data_dir(), "settings.json")
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); using defaults", self.path, e)
            return
        for k, v in data.items():
            if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])) or \
                    (k in DEFAULTS and isinstance(DEFAULTS[k], float) and isinstance(v, int)):
                self[k] = v
        if self["log_level"] not in LOG_LEVELS:
            self["log_level"] = DEFAULTS["log_level"]
        if self["log_mode"] not in LOG_MODES:
            self["log_mode"] = DEFAULTS["log_mode"]

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            log.debug("settings saved to %s", self.path)
        except OSError as e:
            log.error("could not save settings to %s: %s", self.path, e)

    @property
    def log_dir(self):
        return self["log_dir"] or os.path.join(os.path.dirname(self.path), "logs")

    @property
    def cache_path(self):
        return os.path.join(os.path.dirname(self.path), "scan_cache.csv")


_handler = None


def setup_logging(settings, level=None, restart=False):
    """(Re)configure the file log from settings. Returns the log file path or None.

    restart=True is used when settings change mid-session: the current session's
    log is continued rather than truncated or replaced; the overwrite/new choice
    takes effect from the next start.
    """
    global _handler
    level = level or settings["log_level"]
    previous = _handler.baseFilename if _handler is not None else None
    if _handler is not None:
        log.removeHandler(_handler)
        _handler.close()
        _handler = None
    if level == "Off":
        log.setLevel(logging.CRITICAL + 1)
        log.propagate = False
        return None

    folder = settings.log_dir
    os.makedirs(folder, exist_ok=True)
    mode = settings["log_mode"]
    if restart and previous and os.path.normcase(os.path.dirname(previous)) == \
            os.path.normcase(os.path.abspath(folder)):
        path, filemode = previous, "a"
    elif restart and mode != "new":
        path, filemode = os.path.join(folder, "ps2vr.log"), "a"
    elif mode == "new":
        path = os.path.join(folder, time.strftime("ps2vr_%Y%m%d_%H%M%S.log"))
        _prune(folder, max(1, int(settings["log_keep"])))
        filemode = "w"
    else:
        path = os.path.join(folder, "ps2vr.log")
        filemode = "a" if mode == "append" else "w"

    _handler = logging.FileHandler(path, mode=filemode, encoding="utf-8")
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_handler)
    log.setLevel(getattr(logging, level.upper()))
    log.propagate = False
    from . import __version__
    log.info("---- PS2VR %s %s (level %s, %s) ----", __version__,
             "logging reconfigured" if restart else "started", level, LOG_MODES[mode])
    log.debug("python %s on %s; data folder %s", sys.version.split()[0], sys.platform,
              os.path.dirname(settings.path))
    return path


def _prune(folder, keep):
    """Delete the oldest timestamped logs so at most `keep` remain after this run."""
    logs = sorted(glob.glob(os.path.join(folder, "ps2vr_*.log")))
    for old in logs[:max(0, len(logs) - (keep - 1))]:
        try:
            os.remove(old)
        except OSError:
            pass


def current_log_file():
    return _handler.baseFilename if _handler is not None else None
