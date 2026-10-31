"""Entry point: GUI when started without arguments, CLI otherwise."""

import os
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] == "gui" or (len(argv) == 1 and os.path.isfile(argv[0])):
        # no arguments, "gui [image]", or an image dropped onto the program
        from . import gui
        return gui.main(argv[1:2] if argv and argv[0] == "gui" else argv[:1])
    from . import cli
    return cli.main(argv)
