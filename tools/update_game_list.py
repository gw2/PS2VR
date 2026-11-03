"""Refresh the game list in ps2vr/compat.csv from PCSX2's GameIndex.yaml, keeping results.

    python tools/update_game_list.py
"""

import csv
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ps2vr.compat import CSV_PATH, FIELDS, load

URL = "https://raw.githubusercontent.com/PCSX2/pcsx2/master/bin/resources/GameIndex.yaml"
_ENTRY = re.compile(r"^([A-Z]{2,5}-\d+[A-Z]?):[ \t]*\n((?:[ \t]+.*\n|\n)*)", re.M)


def _field(body, name):
    m = re.search(r'^  %s:\s*"?(.*?)"?\s*$' % name, body, re.M)
    return m.group(1).replace('\\"', '"') if m else ""


def parse(text):
    """{serial: (title, region)}. Reads the few fields it needs, without a YAML library."""
    return {m.group(1): (_field(m.group(2), "name-en") or _field(m.group(2), "name"),
                         _field(m.group(2), "region"))
            for m in _ENTRY.finditer(text)}


def main():
    with urllib.request.urlopen(URL, timeout=120) as r:
        listed = parse(r.read().decode("utf-8", "replace"))
    old = load()
    gone = [s for s, g in old.items() if s not in listed and g.describe()]
    rows = []
    for serial in sorted(set(listed) | set(gone)):
        game = old.get(serial)
        row = {f: getattr(game, f) for f in FIELDS} if game else dict.fromkeys(FIELDS, "")
        row["serial"] = serial
        if serial in listed:
            row["title"], row["region"] = listed[serial]
        rows.append(row)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print("%d games, %d kept only for their results" % (len(rows), len(gone)))


if __name__ == "__main__":
    main()
