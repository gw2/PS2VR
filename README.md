# PS2VR – PlayStation 2 Video Replacer

Replaces FMV files in PS2 disc images with the shortest possible blank videos
to reduce image size, and can also remove the padding that fills most PS2 discs. Works as a GUI (run with no arguments) or from the
command line. Pure Python 3 standard library – no ffmpeg or other tools needed.

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## Download (Windows)

Get `PS2VR-<version>-windows-x64.zip` from the [Releases](https://github.com/gw2/PS2VR/releases)
page and unzip it anywhere. No Python or installer needed.

- **`PS2VR.exe`** – the GUI. Double-click it, or drag a disc image onto it.
- **`PS2VR-cli.exe`** – the command line. Run it from a terminal with the same commands as below,
  e.g. `PS2VR-cli.exe batch "D:\PS2" -r`.

Settings, the scan cache and logs are stored next to the exes, so the folder is portable.
The exes are not code-signed, so Windows SmartScreen may warn the first time you run one. Choose
"More info" → "Run anyway", or run from source instead.

To build the exes yourself: `python -m pip install pyinstaller`, then `python build_exe.py`.

## Usage (from source)

Requires Python 3.10+.

```
python ps2vr.py                         # GUI
python ps2vr.py "Game.iso"              # GUI, image pre-loaded (or drag an image onto ps2vr.py)
python ps2vr.py analyze "Game.iso"      # list videos and compatibility (--json available)
python ps2vr.py replace "Game.iso" --all              # replace every compatible video
python ps2vr.py replace "Game.iso" 2 /MOVIES/X.PSS    # replace by number or path
python ps2vr.py replace "Game.cue" --all --mode inplace -o out.iso
python ps2vr.py replace "Game.iso" --all --padding    # also remove blank padding
python ps2vr.py replace "Game.iso" --padding          # remove padding only, keep the videos
python ps2vr.py batch "D:\PS2" -r --all --padding     # the same for a whole folder
python ps2vr.py batch "D:\PS2" -r --all --padding-only # a whole folder, keep the videos
python ps2vr.py blank "Game.iso" 1 -o blank.pss       # just write a blank video
python ps2vr.py extract "Game.iso" /MOVIES/X.PSS      # pull a file out of the image

python ps2vr.py batch "D:\PS2"                        # scan a folder: size now / after / savings per image
python ps2vr.py batch "D:\PS2" -r --all -o "D:\PS2\slim"   # process every image (incl. subfolders)
python ps2vr.py batch "D:\PS2" 1 3                    # process images 1 and 3 from the scan list
```

**Batch mode** (GUI tab "Batch (folder)" or the `batch` command) scans a folder
for ISO / CUE / MDS / MDF / BIN / IMG images. BIN and MDF files that belong to a CUE or MDS are
grouped under it, and images already named `[PS2VR...]` are skipped. For each image it shows:
- the current size on disk
- the estimated output size, which is exact (the blank videos are generated during the scan)
- the savings, and how many videos can be replaced

Tick the images you want and run them in sequence. Outputs go next to each image (or into
an output folder) as `<name> [PS2VR-VP].iso`, see below. Existing outputs are skipped unless
"Overwrite" is set. An image that fails is reported and the batch continues. Cancel
stops after deleting the partial output. A failed run also leaves no output, and keeps any earlier one. Note that converting a BIN/CUE to ISO saves space
by itself (2352 → 2048 bytes per sector); that is included in the estimate.

Options for `replace`:

| option | meaning |
|---|---|
| `--mode rebuild` (default) | Removes the freed space: files after a replaced video move down, all ISO9660 and UDF structures are patched. The image shrinks. |
| `--mode inplace` | Every file keeps its LBA; freed space is zero-filled. Same image size but compresses to almost nothing (7z/CHD/ZSO). Use this for games that read raw sector numbers. |
| `--duration S` | Length of the blank (default 1.0 s, minimum 1 frame). |
| `--full-verify` | CRC every file of the output against the source (default verification samples each file and checks replaced files fully). |
| `--padding` | Also remove blank padding (see below). Rebuild mode shrinks the image. With `replace`, in-place mode only zero-fills blank files; `batch` needs rebuild mode. Off by default. |
| `--padding-only` | Batch: remove blank padding and keep every video. Rebuild mode only. In the GUI: **Padding only (keep videos)**. |
| `--padding-suspect` | Also remove files that only look like padding. Test the result: the game may read them. |

Input: ISO, BIN/CUE (2352 Mode 1 / Mode 2 Form 1), MDF/MDS (2048/2352/2448),
or any raw dump. Output is always a 2048-byte/sector ISO, which PCSX2 and OPL load directly.

## Settings, scan cache and logs

All three live next to `ps2vr.py`, so the install is portable. If that folder isn't writable, they go to `%APPDATA%\PS2VR` instead.

| file | contents |
|---|---|
| `settings.json` | Saved when the window closes or you click **Save settings**. It remembers the last single image and output, the batch folder, subfolder and output folder choices, overwrite, mode, blank length, verify, the last tab and the window size, plus all Settings-tab options. |
| `scan_cache.csv` | One row per scanned image: path, size, modified time, mode, blank length, format, volume label, video counts, replaceable video names, estimated size, errors and scan time. A row is reused only when the image's files are unchanged and the mode and blank length match. Rows for deleted images are dropped. Processing an image always re-analyses it first, so a stale cache can never affect an output. **Rescan all** ignores the cache and refreshes it. |
| `logs/` | Debug log, configured on the **Settings** tab (see below). |

Click a column heading in either table to sort by it, and click again to reverse. Sizes sort by their real byte value, and rows with errors or nothing to save always stay at the bottom. The batch table's sort is remembered.

The batch tab re-opens the last folder on start. Unchanged images appear instantly from the cache and are marked "(cached)".

Logging options:
- **Levels:** Off, Error, Warning, Info (what was done) and Debug. Debug adds the detected sector layout, file-system and UDF details, each video's parameters, the blank-stream muxing parameters, relocated regions, patched sectors, cache hits and misses, and full tracebacks.
- **Overwrite** `ps2vr.log` on each start.
- **New** timestamped log on each start, keeping the last N.
- **Append** to `ps2vr.log`.

Changing logging mid-session continues the current file; the overwrite/new choice applies from the next start. On the command line, `--log-level Debug` overrides the level for one run. `batch` accepts `--rescan` and `--no-cache`.

## Research notes

**PS2 video formats on game discs**

| Format | Container / codec | Status in PS2VR |
|---|---|---|
| **PSS** (Sony, by far the most common) | MPEG-2 program stream. Video: MPEG-2 MP@ML in stream `0xE0`, decoded by the IPU via `libmpeg`. Audio: `private_stream_1` (`0xBD`) carrying an `SShd` header (format, rate, channels, interleave) + `SSbd` block of PCM16 or SPU2-ADPCM. | **Supported** |
| MPEG-2 PS without SShd / `.MPG` | Same, with MPEG audio | Experimental (video-only replacement) |
| MPEG-2 elementary stream `.M2V` | raw video | Supported |
| IPU (`ipum` header) | Raw IPU (intra MPEG-2-like) frames | Detected, not yet supported |
| Sofdec `.SFD` (CRI) | MPEG-1 PS + ADX audio | Detected, not yet supported |
| Bink `.BIK` / Smacker | Proprietary RAD codecs | Detected, cannot be synthesised |

**How the blank video is made.** The generator writes a real MPEG-2 bitstream
directly (see `ps2vr/mpeg2.py`): one intra picture where every macroblock is
DC-only black (≈30 bits per macroblock), then P pictures where each slice
codes only its first and last macroblock as "motion-compensated, not coded"
with a zero vector and skips the rest (≈9 bytes per row). Sequence header,
quant matrices and extensions are copied from the original so width, height,
frame rate and aspect are identical. The program stream is re-muxed
mirroring the original's pack size (e.g. 16 KiB), PES size, system header,
start timestamps and audio `SShd` header, with silent audio of matching length.
A GTA III movie of 46 MiB becomes 208 KiB (1 s).

**Disc layout.** PS2 DVDs are ISO9660 + UDF 1.02 bridge discs; CDs are
ISO9660 only. The PS2 boots through ISO9660, but the UDF side is kept
consistent too (File Entries, partition length, Logical Volume Integrity size
table, end anchor; tag CRC/checksums recomputed).

**Your source image is never overwritten.** Every write refuses an output that is any file of
the source image, including a CUE's BIN or an MDS's MDF, however the path is spelled: other case,
short name, link or mapped drive. This holds even with `--overwrite` or **Overwrite existing**,
which only replace earlier `[PS2VR...]` outputs.

**Output names** say what was removed: `[PS2VR-V]` videos, `[PS2VR-P]` blank padding,
`[PS2VR-VP]` both, with `S` added for suspect padding (`[PS2VR-VPS]`). Different options
therefore give separate files. In-place mode leaves blank padding in place, so it adds no `P`.
Scans also skip the plain `[PS2VR]` of older versions.

**Padding.** Many PS2 discs are filled up on purpose, usually to push game data
towards the faster outer edge of the DVD. `analyze`, the single-image list and the batch scan all report it.
A survey of 247 images (773 GiB) found about 80 GiB of blank padding in 77 of them.

| Form | Example | Status | Removed by |
|---|---|---|---|
| Sectors no file owns, all one byte | 1.9 GiB before the ELF in GTA III | padding | `--padding` |
| Filler name, all one byte | `DUMMY.DAT`, `PAD_0.DAT`, `NULL100.DAT`, `DVDUMMY.BIN` | padding | `--padding` |
| Filler name, real-looking data | `GARBAGE` (BloodRayne 2), `PADDING.NUL` (GTA III) | suspect | `--padding-suspect` |
| Ordinary name, all one byte | `DATA.DAT` (Predator), `1GB.000` (Simpsons Hit & Run) | suspect | `--padding-suspect` |
| Sectors no file owns, holding data | Final Fantasy X, X-2 and XII keep the game here | kept | never |

A scan only samples each candidate. Before writing, every byte of the chosen padding is read, and
anything that is not blank is kept. A removed file stays on the disc as one blank sector, so its
name still opens. `DUMMY.PSS` files that are real movies are treated as videos, not padding.

**Removing padding can break a game.** It moves every file after it, so a game that locates files
by raw sector number stops working, and verification cannot detect this. In PCSX2 such a game
logs `Error reading past end of disc`. Use in-place mode for those games; the padding is
already zeros there, so the image compresses to almost nothing. Burned discs lose the outer-edge
speed that padding exists for, which could matter for streaming on real hardware (not tested).

PS2VR warns about this, in `analyze`, the scan and the log, when removing padding would move files
whose sector numbers are stored in a small root file or any `.ELF`: at least 3 files and a third of
those that move. It only warns; rebuild mode still runs. Across the 102 padded images it flagged 10:
ATV Offroad Fury 2, Katamari Damacy, Journey to Jaburo, Mister Mosquito (with suspects), Devil May Cry,
We Love Katamari (2 copies), Paris-Dakar Rally, Ratchet & Clank: Going Commando and Mercenaries. All
but Mercenaries broke in the boot test below (Mister Mosquito has no clean comparison); Mercenaries
stores such a table but kept running, and may or may not need it later. No warning does not prove a
game is safe, so still test the output.

Boot test: padding only removed, 75 s in PCSX2, intro or menu reached, not played.

| Result | Games |
|---|---|
| Boots and keeps running | GTA III, Devil May Cry 2, Ghost in the Shell SAC, Mercenaries, Lethal Skies II, Front Mission 4, Silent Hill 2, Armored Core 2, PS Underground Holiday 2001, Ico, Black, Burnout 2, Juiced, MS Gundam Federation vs Zeon, DBZ Budokai 2, War of the Monsters; with suspects too: GTA III, BloodRayne 2 |
| Broken (raw sector numbers) | ATV Offroad Fury 2, Katamari Damacy, MS Gundam Journey to Jaburo, Devil May Cry, We Love Katamari, Paris-Dakar Rally, Ratchet & Clank: Going Commando; with suspects too: Mister Mosquito |
| Inconclusive (the original also sat on a static screen in the test) | Soulcalibur II, .hack//G.U. Vol. 1, Silent Scope 2, Time Crisis Crisis Zone |

PCSX2 can take a small image for a CD ([`FindDiskType`](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/CDVD/CDVDcommon.cpp));
DVD games shrunk to 364 MiB still came up as DVDs, and PS2VR warns about the few images where this could apply.

## Compatibility list

[`ps2vr/compat.csv`](ps2vr/compat.csv) lists PS2 games by serial, with
any results recorded for it. PS2VR reads the serial from the disc's `SYSTEM.CNF`, then shows the game and its
results in `analyze` and the GUI, and warns when a job matches a recorded failure. It never
blocks a job.

Each result is `ok`, `broken` or `unclear`, with the test date:

| Column | Tested with |
|---|---|
| `videos_rebuild` | All replaceable videos blanked, rebuild mode |
| `videos_inplace` | All replaceable videos blanked, in-place mode |
| `padding_rebuild` | Blank padding removed, videos kept, rebuild mode |

**What a result does not say.** Almost all rows are blank, which means untested, not safe. Results come
from one library in PCSX2, for 75 seconds or less, reaching an intro or menu: no gameplay and no real
hardware. A serial can cover several revisions of a game. The two in-place results still dated 2026-10-31
predate later fixes to blank videos.

Latest video test, rebuild mode, 155 games: 111 ok, 14 broken, 30 unclear. Nine of the broken games also stay on a
black screen in in-place mode while their originals boot, so blank videos are the likely cause there:
.hack Parts 1–4, ATV Offroad Fury, Paris-Dakar Rally, The Simpsons: Road Rage, Twisted Metal: Black and War of the Monsters.

## Verification done so far (GTA III USA, SLUS-20062)

* `analyze` finds `/MOVIES/DMANTSC.PSS` (13.7 s) and `/MOVIES/INTRNTSC.PSS` (95 s), both 640x480 30 fps, PCM16 48 kHz stereo.
* Rebuild: 4.32 GiB → 4.27 GiB; built-in verification passes; 7-Zip (ISO) and the Windows CDFS driver both read the new tree and file contents match.
* UDF: all 407 descriptor tags in the original verify with PS2VR's CRC code, and all 407 in the output are valid and consistent with the ISO9660 tree.
* BIN/CUE (2352 Mode 2) input produces a byte-identical ISO to the ISO input.
* The generated video decodes in Windows' Media Foundation MPEG-2 decoder: 30 frames, all pixels Y=16 / CbCr=128.
* PCSX2 v2.5.350 (BIOS SCPH-39001 v1.60): the rebuilt ISO boots, both blank movies pass in under 6 s (originals: ~109 s), the title screen appears at ~9 s, and the game loads and plays its in-engine opening cutscene with no CDVD errors in the log. Not yet tested on real hardware.
