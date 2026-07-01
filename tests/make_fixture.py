"""Generate a synthetic Sony a6400 card that mirrors the real on-disk format.

This is the test bed (no real SD card needed). It reproduces the things that
actually exercise the code and the safety guards:

* the real layout — ``DCIM/<NNN>MSDCF/DSC0####.{JPG,ARW}`` — with the camera's
  management folders alongside (``PRIVATE``, ``AVF_INFO``, ``MP_ROOT``, ``MISC``)
  as untouchable canaries;
* folder-scoped stems: ``100MSDCF`` and ``101MSDCF`` both contain ``DSC00001``,
  which must pair independently;
* a misset-but-consistent clock — EXIF *and* file mtimes are both shifted into
  the wrong era by the same constant, exactly as a misconfigured camera writes
  them — so grouping-by-gaps can be shown to be robust;
* real JPEGs with genuine EXIF, plus realistic bursts and scene gaps, and both
  orphan kinds (JPEG-only and RAW-only).

One intentional deviation: ``.ARW`` files are real **TIFF** files (ARW is a
TIFF container) with EXIF dates but without Sony's proprietary makernotes — the
app decodes JPEG only (design doc §7), so the RAW bytes only need to exist, hash,
move, restore, and be parseable by exiftool for the clock-fix.

Run directly to (re)generate the default fixture::

    uv run python -m tests.make_fixture
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import piexif
from PIL import Image

# A clock set ~8 years early but otherwise fine — the "wrong but consistent"
# case. Real capture order/gaps are preserved; only the absolute era is wrong.
WRONG_CLOCK_BASE = datetime(2018, 3, 15, 14, 22, 1)

DEFAULT_DEST = Path(__file__).parent / "_fixture_card"

MANAGEMENT_FILES = {
    "PRIVATE/M4ROOT/MEDIAPRO.XML": b"<mediaprofile/>\n",
    "AVF_INFO/AVIN0001.BNP": b"\x00\x01BNP-CANARY\x00",
    "MP_ROOT/100ANV01/MAH00001.MP4": b"FAKE-MP4-CANARY",
    "MISC/canary.dat": b"do-not-touch",
}


@dataclass(frozen=True)
class ShotSpec:
    folder: str          # e.g. "100MSDCF"
    stem: str            # e.g. "DSC00001"
    has_jpg: bool
    has_raw: bool
    offset_s: float      # seconds after WRONG_CLOCK_BASE
    subsec: int
    scene: int           # expected scene-cluster index (for assertions)

    @property
    def kind(self) -> str:
        if self.has_jpg and self.has_raw:
            return "pair"
        return "orphan_jpg" if self.has_jpg else "orphan_raw"


# Capture timeline (all on the wrong-clock era):
#   scene 0: a 3-frame burst (~1 s apart)
#   scene 1: +10 min — an orphan JPEG then an orphan RAW ~2 s later
#   scene 2: +20 min — a normal pair
#   scene 3: +30 min — a pair in a *second* folder, reusing stem DSC00001
SHOTS: list[ShotSpec] = [
    ShotSpec("100MSDCF", "DSC00001", True, True, 0.0, 10, 0),
    ShotSpec("100MSDCF", "DSC00002", True, True, 1.0, 50, 0),
    ShotSpec("100MSDCF", "DSC00003", True, True, 2.0, 30, 0),
    ShotSpec("100MSDCF", "DSC00010", True, False, 600.0, 0, 1),
    ShotSpec("100MSDCF", "DSC00011", False, True, 602.0, 0, 1),
    ShotSpec("100MSDCF", "DSC00020", True, True, 1200.0, 5, 2),
    ShotSpec("101MSDCF", "DSC00001", True, True, 1800.0, 0, 3),
]


def _color(i: int) -> tuple[int, int, int]:
    # Distinct per shot so JPEG bytes (and thus hashes) differ; offset off black
    # so the synthetic frames are visible in a demo.
    return (50 + 37 * i % 190, 50 + 91 * i % 190, 50 + 173 * i % 190)


def _exif_bytes(when: datetime, subsec: int) -> bytes:
    zeroth = {
        piexif.ImageIFD.Make: b"SONY",
        piexif.ImageIFD.Model: b"ILCE-6400",
        piexif.ImageIFD.Software: b"ILCE-6400 v2.00",
    }
    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: when.strftime("%Y:%m:%d %H:%M:%S").encode(),
        piexif.ExifIFD.SubSecTimeOriginal: str(subsec).encode(),
        piexif.ExifIFD.FNumber: (28, 10),
        piexif.ExifIFD.ExposureTime: (1, 500),
        piexif.ExifIFD.ISOSpeedRatings: 400,
        piexif.ExifIFD.FocalLength: (35, 1),
        piexif.ExifIFD.LensModel: b"E 35mm F1.8 OSS",
    }
    return piexif.dump({"0th": zeroth, "Exif": exif_ifd, "1st": {}, "GPS": {}, "Interop": {}})


def _write_jpeg(path: Path, idx: int, when: datetime, subsec: int) -> None:
    img = Image.new("RGB", (160, 120), _color(idx))
    img.save(path, "jpeg", quality=90, exif=_exif_bytes(when, subsec))


def _write_arw(path: Path, idx: int, when: datetime, subsec: int) -> None:
    # ARW is a TIFF container; write a real (minimal) TIFF so exiftool can parse
    # and clock-shift it, while staying distinct per shot. No Sony makernotes —
    # the app never decodes RAW (design doc §7).
    img = Image.new("RGB", (160, 120), _color(idx + 100))
    try:
        img.save(path, format="TIFF", exif=_exif_bytes(when, subsec))
    except (TypeError, ValueError, OSError):
        img.save(path, format="TIFF")


def build(dest: str | os.PathLike = DEFAULT_DEST) -> Path:
    """(Re)create the fixture card at ``dest`` and return its path."""
    dest = Path(dest)
    if dest.exists():
        import shutil

        shutil.rmtree(dest)

    for i, s in enumerate(SHOTS):
        folder = dest / "DCIM" / s.folder
        folder.mkdir(parents=True, exist_ok=True)
        when = WRONG_CLOCK_BASE + timedelta(seconds=s.offset_s)
        epoch = when.timestamp()  # camera-clock era; used for file mtimes too
        if s.has_jpg:
            jpg = folder / f"{s.stem}.JPG"
            _write_jpeg(jpg, i, when, s.subsec)
            os.utime(jpg, (epoch, epoch))
        if s.has_raw:
            arw = folder / f"{s.stem}.ARW"
            _write_arw(arw, i, when, s.subsec)
            os.utime(arw, (epoch, epoch))

    # Management folders (canaries) — never read-modified or deleted by the app.
    for rel, data in MANAGEMENT_FILES.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    return dest


if __name__ == "__main__":
    out = build()
    print(f"Fixture card written to {out}")
    print(f"  {len(SHOTS)} shots across "
          f"{len({s.folder for s in SHOTS})} DCIM folders, "
          f"{len(MANAGEMENT_FILES)} management canaries")
