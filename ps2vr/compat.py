"""Known test results for PS2 games, looked up by the serial in SYSTEM.CNF.

compat.csv lists PS2 games by serial. Most rows have no results. A result is "ok", "broken" or "unclear" plus the test
date, and only means what the test saw: a boot to the intro or menu in PCSX2.
"""

from dataclasses import dataclass
import csv
import logging
import os
import re

log = logging.getLogger("ps2vr.compat")

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compat.csv")
FIELDS = ["serial", "title", "region", "videos_rebuild", "videos_inplace", "padding_rebuild", "note"]
LABELS = {"videos_rebuild": "replacing videos, rebuild", "videos_inplace": "replacing videos, in-place",
          "padding_rebuild": "removing padding, rebuild"}

_BOOT = re.compile(r"BOOT2?\s*=\s*cdrom0?:\\*([^;\r\n ]+)", re.I)
_SERIAL = re.compile(r"([A-Z]{4})[_-](\d{3})\.(\d{2})")
_games = None


@dataclass
class Game:
    serial: str
    title: str
    region: str
    videos_rebuild: str = ""
    videos_inplace: str = ""
    padding_rebuild: str = ""
    note: str = ""

    def result(self, field):
        return getattr(self, field).partition(" ")[0]

    def describe(self):
        """The recorded results as one line, or "" if the game is untested."""
        parts = ["%s: %s" % (LABELS[f], getattr(self, f)) for f in LABELS if getattr(self, f)]
        if self.note:
            parts.append(self.note)
        return "; ".join(parts)


def serial_of(fs):
    """SLUS_210.08 in SYSTEM.CNF becomes SLUS-21008. None if the disc does not say."""
    ent = fs.files.get("/SYSTEM.CNF")
    if ent is None:
        return None
    boot = _BOOT.search(fs.read_file(ent, 2048).decode("latin1"))
    name = _SERIAL.fullmatch(boot.group(1).rsplit("\\", 1)[-1].upper()) if boot else None
    return "%s-%s%s" % name.groups() if name else None


def load(path=CSV_PATH):
    with open(path, newline="", encoding="utf-8") as f:
        return {row["serial"]: Game(**row) for row in csv.DictReader(f)}


def lookup(serial):
    global _games
    if _games is None:
        try:
            _games = load()
        except (OSError, TypeError, ValueError) as e:
            log.warning("no compatibility list: %s", e)
            _games = {}
    return _games.get(serial)


def problems(game, mode, videos, pads):
    """What testing found wrong with this exact job, as short phrases."""
    if game is None:
        return []
    found = []
    if videos and game.result("videos_%s" % mode) == "broken":
        found.append("replacing videos in %s mode broke this game in testing" % mode)
    if pads and mode == "rebuild" and game.result("padding_rebuild") == "broken":
        found.append("removing padding broke this game in testing")
    return found
