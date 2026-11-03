"""Build the Windows executables with PyInstaller.

    python -m pip install pyinstaller
    python build_exe.py

Produces in dist/:
    PS2VR.exe       - GUI (no console window)
    PS2VR-cli.exe   - command line (same commands as `python ps2vr.py ...`)
    PS2VR-<version>-windows-x64.zip  - both exes + README + CHANGELOG + LICENSE
"""

import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")


def version():
    with open(os.path.join(ROOT, "ps2vr", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'__version__ = "([^"]+)"', f.read()).group(1)


def version_file(ver, name, description):
    nums = [int(x) for x in re.findall(r"\d+", ver)][:4]
    nums += [0] * (4 - len(nums))
    path = os.path.join(BUILD, "version_%s.txt" % name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("""VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(t)s, prodvers=%(t)s, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'PS2VR - PlayStation 2 Video Replacer'),
      StringStruct('FileDescription', '%(desc)s'),
      StringStruct('FileVersion', '%(v)s'),
      StringStruct('ProductVersion', '%(v)s'),
      StringStruct('InternalName', '%(name)s'),
      StringStruct('OriginalFilename', '%(name)s.exe'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 gw2 - MIT License')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""" % {"t": tuple(nums), "v": ver, "name": name, "desc": description})
    return path


def pyinstaller(name, windowed, description, ver):
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--windowed" if windowed else "--console",
            "--name", name,
            "--distpath", DIST, "--workpath", os.path.join(BUILD, name),
            "--specpath", BUILD,
            "--version-file", version_file(ver, name, description),
            "--add-data", os.path.join(ROOT, "ps2vr", "compat.csv") + os.pathsep + "ps2vr",
            os.path.join(ROOT, "ps2vr.py")]
    print(">", " ".join(args[3:]))
    subprocess.run(args, check=True)


def main():
    ver = version()
    os.makedirs(BUILD, exist_ok=True)
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    pyinstaller("PS2VR", True, "PS2VR (GUI)", ver)
    pyinstaller("PS2VR-cli", False, "PS2VR (command line)", ver)

    zpath = os.path.join(DIST, "PS2VR-%s-windows-x64.zip" % ver)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("PS2VR.exe", "PS2VR-cli.exe"):
            z.write(os.path.join(DIST, f), "PS2VR-%s/%s" % (ver, f))
        for f in ("README.md", "CHANGELOG.md", "LICENSE"):
            z.write(os.path.join(ROOT, f), "PS2VR-%s/%s" % (ver, f))
    print("\nBuilt:")
    for f in sorted(os.listdir(DIST)):
        print("  %-36s %8.1f MiB" % (f, os.path.getsize(os.path.join(DIST, f)) / 2**20))


if __name__ == "__main__":
    main()
