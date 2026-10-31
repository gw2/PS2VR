"""Detection and compatibility analysis of video files found on a PS2 disc."""

from dataclasses import dataclass
import logging
import os

from . import pss, mpeg2

VIDEO_EXTS = {".PSS", ".IPU", ".BIK", ".BK2", ".SFD", ".MPG", ".MPEG", ".M2V",
              ".STR", ".PMF", ".SMK", ".VP6", ".MOV", ".AVI", ".XMV", ".THP",
              ".M1V", ".PSM", ".DAT_PSS"}

log = logging.getLogger("ps2vr.video")

COMPATIBLE = "compatible"
EXPERIMENTAL = "experimental"
UNSUPPORTED = "unsupported"


@dataclass
class VideoFile:
    path: str
    size: int
    lba: int
    fmt: str
    status: str
    detail: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = None
    audio: str = ""
    info: object = None        # pss.PssInfo / mpeg2.SequenceInfo

    @property
    def compatible(self):
        return self.status in (COMPATIBLE, EXPERIMENTAL)

    def describe(self):
        parts = [self.fmt]
        if self.width:
            parts.append("%dx%d @ %.3g fps" % (self.width, self.height, self.fps))
        if self.duration:
            parts.append("%.1fs" % self.duration)
        if self.audio:
            parts.append(self.audio)
        return ", ".join(parts)


def _classify(head):
    if head[:4] == b"\x00\x00\x01\xba":
        if len(head) > 4 and head[4] >> 6 == 1:
            return "MPEG-2 PS"
        if b"SofdecStream" in head[:4096]:
            return "Sofdec"
        return "MPEG-1 PS"
    if head[:4] == b"\x00\x00\x01\xb3":
        return "MPEG ES"
    if head[:4] == b"ipum":
        return "IPU"
    if head[:3] == b"BIK":
        return "Bink"
    if head[:3] == b"KB2":
        return "Bink 2"
    if head[:3] == b"SMK":
        return "Smacker"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "AVI"
    if head[:4] == b"PSMF":
        return "PSMF"
    return None


def probe(fs, ent):
    ext = os.path.splitext(ent.name)[1].upper()
    if ent.size < 16:
        return None
    head = fs.read_file(ent, 4096)
    kind = _classify(head)
    if kind is None:
        if ext in VIDEO_EXTS and ext not in (".DAT_PSS",):
            return VideoFile(ent.path, ent.size, ent.lba, "unknown (%s)" % ext,
                             UNSUPPORTED, "unrecognised video container")
        return None

    v = VideoFile(ent.path, ent.size, ent.lba, kind, UNSUPPORTED)
    if kind == "MPEG-2 PS":
        head = fs.read_file(ent, 512 * 1024)
        tail = fs.read_file(ent, 256 * 1024, max(0, ent.size - 256 * 1024))
        info = pss.analyze(head, tail)
        v.info = info
        if info.audio is not None:
            v.fmt = "PSS"
            a = info.audio
            v.audio = "%s %d Hz %dch" % (a.codec, a.rate, a.channels)
        elif any(0xC0 <= s <= 0xDF for s in info.streams):
            v.audio = "MPEG audio (will be dropped)"
        if info.seq:
            v.width, v.height, v.fps = info.seq.width, info.seq.height, info.seq.fps
        v.duration = info.duration
        if not info.ok:
            v.detail = info.reason
        elif info.audio is not None or not any(
                (0xC0 <= s <= 0xDF) or s == pss.PRIVATE1 for s in info.streams):
            v.status = COMPATIBLE
        else:
            v.status = EXPERIMENTAL
            v.detail = "non-PSS audio stream; replacement will be video only"
    elif kind == "MPEG ES":
        seq = mpeg2.parse_sequence(fs.read_file(ent, 65536))
        v.info = seq
        if seq:
            v.width, v.height, v.fps = seq.width, seq.height, seq.fps
            v.status = COMPATIBLE
        else:
            v.detail = "MPEG-1 elementary stream - not supported"
    elif kind == "IPU":
        v.detail = "raw IPU stream - not yet supported"
    elif kind in ("Bink", "Bink 2", "Smacker"):
        v.detail = "proprietary RAD codec - cannot be synthesised"
    elif kind in ("Sofdec", "MPEG-1 PS"):
        v.detail = "CRI Sofdec / MPEG-1 - not yet supported"
    else:
        v.detail = "format not supported"
    return v


def scan(fs, progress=None):
    vids = []
    ents = sorted(fs.files.values(), key=lambda e: e.lba)
    for n, ent in enumerate(ents):
        if progress:
            progress(n, len(ents), ent.path)
        try:
            v = probe(fs, ent)
        except Exception as e:  # keep scanning other files
            log.warning("probe failed for %s: %s", ent.path, e, exc_info=True)
            v = VideoFile(ent.path, ent.size, ent.lba, "error", UNSUPPORTED, str(e))
        if v:
            vids.append(v)
    return vids


def make_blank(v: VideoFile, duration=1.0, luma=16):
    if isinstance(v.info, pss.PssInfo):
        i = v.info
        log.debug("blank for %s: pack %d, pes %d, mux %d, pts %s/%s, audio %s", v.path,
                  i.pack_size, i.max_pes, i.mux_rate, i.first_pts, i.first_dts,
                  i.audio.codec if i.audio else None)
        return pss.build_blank(v.info, duration, luma)
    if isinstance(v.info, mpeg2.SequenceInfo):
        frames = max(1, round(duration * v.info.fps))
        return mpeg2.blank_stream(v.info, frames, luma)[0]
    raise ValueError("%s: cannot build a replacement for %s" % (v.path, v.fmt))
