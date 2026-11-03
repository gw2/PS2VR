# Changelog

## 0.2.2 – 2026-11-03

### New
- **Game reports.** A GitHub form collects test results for the compatibility list. `analyze`, `replace`, `batch` and the GUI link to it, with the game filled in when known, and the single-image tab has a **Report a test result...** button. PS2VR sends nothing by itself.

## 0.2.1 – 2026-11-03

### Security
- `extract` without `-o` no longer lets a file name on a crafted disc write outside the current folder, and no longer replaces an existing file.

### Fixed
- A failed run could delete the source image if the source was named like the temporary output (`<output>.part`).
- Verification now fails when the output's UDF file system can no longer be read.
- Rebuild mode no longer moves discs with UDF structures after the files it removes, or files with several extents. It refuses them, which avoids a corrupt output.
- If all of the selected padding turns out to hold data, the job stops instead of writing an unchanged copy.
- Two batch images with the same output name no longer overwrite each other.
- Closing the GUI during a job now asks first, stops the job and removes its partial output. Browsing during a job no longer clears the list or undoes Cancel.
- `batch --rescan` no longer forgets the scan results of other folders, scan errors are no longer cached, and a damaged cache row is ignored.
- A missing log folder, a bad window size or a damaged settings file or list no longer stops the program from starting.
- `--duration` is limited to 0-600 seconds, and the GUI's blank length to 600 seconds.

## 0.2.0 – 2026-11-03

### New
- **Padding removal.** `--padding` removes blank padding as well as videos, and `--padding-only` (GUI: **Padding only (keep videos)**) keeps every video. `--padding-suspect` also removes files that only look like padding. `analyze`, the single-image list and the batch scan all report padding. It is off by default.
- **Sector number warning.** PS2VR warns when removing padding would move files whose sector numbers appear to be stored in a small root file or an `.ELF`. Such games usually break in rebuild mode, and verification cannot detect it. It only warns; the job still runs.
- **Compatibility list.** `ps2vr/compat.csv` lists 12,835 games by serial, with boot test results for those tested so far. PS2VR reads the serial from the disc, shows the game and its results in `analyze`, the scan and the GUI, and warns when a job matches a recorded failure.
- **Output names show what was removed:** `[PS2VR-V]` videos, `[PS2VR-P]` padding, `[PS2VR-VP]` both, with `S` added for suspect padding. Scans also skip the plain `[PS2VR]` of older versions.
- **Batch options** in the GUI and command line now cover padding and padding only. The batch After and Saves columns update when these are ticked.

### Changed
- **Your source image is never overwritten.** Every write refuses an output that is any file of the source image, including a CUE's BIN or an MDS's MDF, under any spelling of its path: other case, short name, link or mapped drive. `--overwrite` does not change this.
- The output is written to a `.part` file and only renamed once verified, so a failed or cancelled run leaves no partial output and keeps any earlier one.
- The single-image tab's button is now **Write ISO**. It asks you to click Analyze again if the selected image has changed.

### Fixed
- **Blank videos hanging games.** Many movie players hang on a blank whose picture flags differ from the original movie's. Blanks now copy the original's flags.
- **Archives taken for videos.** Files that start with a movie but hold other data, or several movies, are no longer treated as one video (Sly 3, Silent Hill 2, Katamari Damacy).
- **Dual-layer discs.** Layer 1 is now kept intact in rebuild mode and checked by verification. Edits that would have to move it are refused, with a pointer to in-place mode.
- **Truncated images** are refused with a clear message, instead of producing a bad output.
- Failure reasons are no longer lost, and a failed run no longer deletes an earlier output.
- In-place mode no longer reports padding as removed when it stays on the disc.
- Batch options can no longer be changed in the middle of a run, and the warning about small images being taken for CDs is no longer hidden.

### Tested
- Every replaceable video blanked in rebuild mode, 159 images (155 games), 60 s boot in PCSX2: 111 games ok, 14 broken, 30 unclear.
- All 159 outputs built and passed verification, with the size matching the estimate.
- Padding removed with videos kept, 75 s boot in PCSX2, 27 games: 16 ok, 7 broken, 4 unclear. The games are listed in the README.

### Known problems
- **Nine games stay on a black screen with blank videos in both modes**, while their originals boot: .hack Parts 1–4, ATV Offroad Fury, Paris-Dakar Rally, The Simpsons: Road Rage, Twisted Metal: Black and War of the Monsters. The blanks are the likely cause, but this has not been confirmed or fixed. Removing padding only is the option to try for these; it is known to break Paris-Dakar Rally.
- Some games break in rebuild mode because they read raw sector numbers. In-place mode helps Ratchet & Clank: Going Commando and ATV Offroad Fury 2.
- Tests reach an intro or menu only. Gameplay and real hardware are untested, so keep your original images.

## 0.1.1 – 2026-10-31

### New
- Windows builds: `PS2VR.exe` (GUI) and `PS2VR-cli.exe` (command line), with version info in their file properties.
- `build_exe.py` builds both exes and the release zip.

### Changed
- If `PS2VR.exe` is given command-line arguments, it shows a message pointing to `PS2VR-cli.exe` instead of failing silently.

Features and processing are unchanged from 0.1.0.

## 0.1.0 – 2026-10-31

First release.

- Opens ISO, BIN/CUE (2352 Mode 1 / Mode 2), MDF/MDS and raw dumps. Output is always a standard 2048-byte ISO.
- Lists every video on a disc with its format, resolution, frame rate, length, audio and whether it can be replaced.
- Generates an MPEG-2 blank for each Sony PSS / MPEG-2 video, matching the original's resolution, frame rate, timestamps, pack layout and audio header.
- Two output modes: rebuild moves later files down so the ISO shrinks, and in-place keeps every file where it was and zero-fills the freed space. ISO9660 and UDF structures are both patched.
- Verifies every output against the source.
- Batch mode scans a folder, shows each image's current size, estimated size and savings, and processes the ones you tick.
- Settings tab, remembered folders and options, a scan cache, and debug logging.
- IPU, Sofdec, Bink and Smacker videos are detected but cannot be replaced.
