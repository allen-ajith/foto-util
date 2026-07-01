"""EXIF reading (read-only) and the opt-in clock-shift seam.

Reads are done with piexif and never touch the file's bytes. Capture time
(``DateTimeOriginal`` + ``SubSecTimeOriginal``) drives time-gap grouping; a
small set of fields drives the one-line top bar in the viewer.

The clock-offset repair (guard G9) is opt-in, off by default, and always goes
through ``exiftool`` (never a hand-rolled writer). See :func:`shift_all_dates`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import piexif

# EXIF tag ids (piexif groups them by IFD).
_DTO = piexif.ExifIFD.DateTimeOriginal          # 36867
_SUBSEC = piexif.ExifIFD.SubSecTimeOriginal     # 37521
_FNUMBER = piexif.ExifIFD.FNumber               # 33437 (rational)
_EXPTIME = piexif.ExifIFD.ExposureTime          # 33434 (rational)
_ISO = piexif.ExifIFD.ISOSpeedRatings           # 34855
_FOCAL = piexif.ExifIFD.FocalLength             # 37386 (rational)


@dataclass(slots=True)
class CaptureTime:
    """A sortable capture instant. ``when`` is naive local time as recorded by
    the camera; ``subsec`` disambiguates within a second for burst frames."""

    when: datetime
    subsec: int = 0

    @property
    def key(self) -> tuple[float, int]:
        return (self.when.replace(tzinfo=timezone.utc).timestamp(), self.subsec)


def _as_text(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("ascii", "ignore").strip()
    return str(v).strip()


def _rational(v: object) -> float | None:
    try:
        num, den = v  # type: ignore[misc]
        return num / den if den else None
    except Exception:
        return None


def _load_exif(src: "str | Path | bytes") -> dict:
    """Load EXIF from a path *or* in-memory JPEG bytes. Passing bytes lets the
    scanner read a file once and both hash and parse it (no second card read)."""
    if isinstance(src, (bytes, bytearray)):
        return piexif.load(bytes(src))
    return piexif.load(str(src))


def read_capture_time(jpg: "str | Path | bytes") -> CaptureTime | None:
    """Parse ``DateTimeOriginal`` (+ sub-second). Accepts a path or JPEG bytes.
    Returns ``None`` if absent."""
    try:
        exif = _load_exif(jpg)
    except Exception:
        return None
    raw = _as_text(exif.get("Exif", {}).get(_DTO))
    if not raw:
        return None
    try:
        when = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    subsec_txt = _as_text(exif.get("Exif", {}).get(_SUBSEC)) or "0"
    m = re.match(r"\d+", subsec_txt)
    subsec = int(m.group()) if m else 0
    return CaptureTime(when=when, subsec=subsec)


@dataclass(slots=True)
class DisplayExif:
    """The thin top-bar line: capture time, aperture, shutter, ISO, focal length."""

    when: datetime | None = None  # capture time (DateTimeOriginal)
    fnumber: float | None = None
    exposure: str | None = None  # pretty "1/500" or "0.5s"
    iso: int | None = None
    focal_mm: float | None = None

    def one_line(self) -> str:
        """Camera settings only — the capture date is shown separately."""
        bits: list[str] = []
        if self.fnumber:
            bits.append(f"f/{self.fnumber:.1f}".rstrip("0").rstrip("."))
        if self.exposure:
            bits.append(self.exposure)
        if self.iso:
            bits.append(f"ISO{self.iso}")
        if self.focal_mm:
            bits.append(f"{self.focal_mm:g}mm")
        return " · ".join(bits)

    def when_str(self) -> str:
        """Capture date/time, month-day-year, e.g. ``May 17, 2026 · 17:16:00``."""
        if not self.when:
            return ""
        w = self.when
        return f"{w.strftime('%b')} {w.day}, {w.year} · {w.strftime('%H:%M:%S')}"


def _pretty_exposure(seconds: float | None) -> str | None:
    if not seconds:
        return None
    if seconds >= 1:
        return f"{seconds:g}s"
    return f"1/{round(1 / seconds)}"


def read_display_exif(jpg: "str | Path | bytes") -> DisplayExif:
    """Best-effort read of the handful of fields shown in the top bar."""
    try:
        exif = _load_exif(jpg)
    except Exception:
        return DisplayExif()
    ex = exif.get("Exif", {})
    # capture time, parsed from the same load (no second read)
    when = None
    raw = _as_text(ex.get(_DTO))
    if raw:
        try:
            when = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            when = None
    iso = ex.get(_ISO)
    if isinstance(iso, (list, tuple)):
        iso = iso[0] if iso else None
    return DisplayExif(
        when=when,
        fnumber=_rational(ex.get(_FNUMBER)),
        exposure=_pretty_exposure(_rational(ex.get(_EXPTIME))),
        iso=int(iso) if iso else None,
        focal_mm=_rational(ex.get(_FOCAL)),
    )


# --- Clock-shift / clock-fix (G9) ------------------------------------------
# Opt-in, off by default. Always via exiftool (never a hand-rolled writer — Sony
# ARW/makernote structure corrupts easily), keeping the per-file `_original`
# backup, and only behind a preview/confirm dialog in the UI.

import json
import shutil
import subprocess


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def image_data_hashes(paths: list[str | Path]) -> dict[str, str]:
    """``{path: hash}`` of *only the image data* (pixels / RAW sensor data), not
    the metadata — computed for many files in a single exiftool call.

    This is the key to safely reclaiming the clock-fix ``_original`` backups: a
    date shift rewrites *metadata*, so the whole-file hash differs from the
    backup, but exiftool's ``ImageDataHash`` covers only the image data and is
    therefore invariant to the date edit — it changes only if the actual image
    bytes are damaged. Paths missing from the result had no computable hash."""
    items = [str(p) for p in paths]
    if not items:
        return {}
    if not exiftool_available():
        raise RuntimeError("exiftool not found (install with `brew install exiftool`)")
    proc = subprocess.run(
        ["exiftool", "-api", "ImageDataHash=sha256", "-j", "-ImageDataHash", *items],
        capture_output=True, text=True,
    )
    out: dict[str, str] = {}
    try:
        for rec in json.loads(proc.stdout or "[]"):
            h = rec.get("ImageDataHash")
            if h and "SourceFile" in rec:
                out[rec["SourceFile"]] = h
    except json.JSONDecodeError:
        pass
    return out


def image_data_hash(path: str | Path) -> str | None:
    """Image-data hash for one file (see :func:`image_data_hashes`)."""
    return image_data_hashes([path]).get(str(path))


def compute_offset(reference_jpg: str | Path, true_when: datetime) -> int:
    """Offset, in seconds, to add to every date so the reference shot reads
    ``true_when`` (``true - recorded``). Raises if the reference has no EXIF
    capture time to anchor against."""
    ct = read_capture_time(reference_jpg)
    if ct is None:
        raise ValueError(f"{reference_jpg} has no DateTimeOriginal to anchor on")
    return int(round((true_when - ct.when).total_seconds()))


def _shift_token(offset_seconds: int) -> tuple[str, str]:
    """exiftool shift sign and ``Y:M:D h:m:s`` magnitude. Months/years are kept
    at 0 (calendar-ambiguous); the whole offset is expressed in days+time, which
    is unambiguous for any range."""
    sign = "+" if offset_seconds >= 0 else "-"
    a = abs(offset_seconds)
    days, rem = divmod(a, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    return sign, f"0:0:{days} {hours}:{mins}:{secs}"


def shift_all_dates(
    paths: list[str | Path],
    offset_seconds: int,
    *,
    batch: int = 32,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Shift ``AllDates`` (DateTimeOriginal/CreateDate/ModifyDate) by
    ``offset_seconds`` on every given file, via exiftool, keeping a ``_original``
    backup per file (the explicit safety net of this opt-in feature). Returns the
    number of files exiftool reports updated.

    Work is done in batches so a caller can show progress and interrupt *between*
    batches (each batch is one exiftool call). ``progress(done, total)`` is invoked
    after each batch; ``should_stop`` is polled before each.

    Note: editing a JPEG/ARW changes its bytes and therefore its content hash;
    callers should re-key affected rows (``Store.rekey``) so decisions survive.
    Best run before culling.
    """
    if not paths:
        return 0
    if not exiftool_available():
        raise RuntimeError("exiftool not found (install with `brew install exiftool`)")
    sign, token = _shift_token(offset_seconds)
    total = len(paths)
    done = updated = 0
    for start in range(0, total, batch):
        if should_stop is not None and should_stop():
            break
        chunk = [str(p) for p in paths[start : start + batch]]
        proc = subprocess.run(
            ["exiftool", f"-AllDates{sign}={token}", *chunk],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"exiftool failed: {proc.stderr.strip() or proc.stdout.strip()}")
        m = re.search(r"(\d+) image files updated", proc.stdout)
        updated += int(m.group(1)) if m else len(chunk)
        done += len(chunk)
        if progress is not None:
            progress(done, total)
    return updated


def clean_appledouble(root: str | Path) -> int:
    """Delete macOS ``._*`` AppleDouble sidecars under ``root`` and return the
    count removed. macOS writes these when a file is modified on an exFAT card
    (e.g. during a clock-fix); they are OS metadata, never image files, so the
    camera ignores them and removing them just restores the card's tidiness."""
    removed = 0
    for p in Path(root).rglob("._*"):
        try:
            if p.is_file():
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed
