"""Pair-aware enumeration of a folder into logical shots.

Files group into a shot by basename stem within a single folder, matching
extensions case-insensitively (``DSC00123.ARW`` + ``DSC00123.JPG`` →
``{DSC00123.ARW, DSC00123.JPG}``). Orphans (JPEG-only or RAW-only) are kept.

Stems are only unique *within* a folder, so the pairing key is
``(folder, stem.lower())`` — ``DSC00001`` may legitimately recur across
``100MSDCF`` and ``101MSDCF``.

Enumeration is strictly read-only (guard G7): it stats and lists, never writes.
"""

from __future__ import annotations

from pathlib import Path

from .model import PairedShot

# Extensions we understand, lower-cased and without the dot.
JPEG_EXTS = {"jpg", "jpeg"}
RAW_EXTS = {"arw"}  # Sony RAW; the interface stays open to more later.
IMAGE_EXTS = JPEG_EXTS | RAW_EXTS

# Camera-managed folders (video clips + their thumbnails, indexes). Their
# contents are never photos to cull — e.g. Sony stores video-clip thumbnails as
# ``PRIVATE/M4ROOT/THMBNL/C0001T01.JPG`` — and the app must never touch them
# (guards G2/G3). Enumeration skips anything under them; :mod:`foto_util.safety`
# reuses this set for its delete guard.
MANAGEMENT_DIRS = {"PRIVATE", "AVF_INFO", "MP_ROOT", "MISC", "M4ROOT", "SONY"}


def _ext(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def is_image(path: Path) -> bool:
    return _ext(path) in IMAGE_EXTS


def pair_folder(root: str | Path) -> list[PairedShot]:
    """Recursively enumerate ``root`` and return paired shots.

    The result is sorted by (folder, stem) for a stable, capture-aligned-ish
    order; the indexer refines ordering with EXIF time and grouping.
    """
    root = Path(root)
    # key: (folder, stem_lower) -> PairedShot
    pairs: dict[tuple[Path, str], PairedShot] = {}

    for path in sorted(root.rglob("*")):
        if not path.is_file() or not is_image(path):
            continue
        rel_parts = path.relative_to(root).parts
        # Skip anything hidden — dot *files* (macOS ``._*`` AppleDouble sidecars
        # carry an image extension but are metadata, not photos) and anything
        # inside a dot *directory* (``.Trashes`` on a card holds Finder-deleted
        # photos, which must not reappear as phantom shots to cull).
        if any(p.startswith(".") for p in rel_parts):
            continue
        # Skip the camera's management folders (video clips + thumbnails carry
        # image extensions like ``C0001T01.JPG`` but are not photos to cull).
        if any(p.upper() in MANAGEMENT_DIRS for p in rel_parts[:-1]):
            continue
        ext = _ext(path)
        key = (path.parent, path.stem.lower())
        shot = pairs.get(key)
        if shot is None:
            shot = PairedShot(stem=path.stem, folder=path.parent)
            pairs[key] = shot
        if ext in JPEG_EXTS:
            # Prefer the first JPEG seen for the stem; deterministic via sort.
            if shot.jpg_path is None:
                shot.jpg_path = path
        elif ext in RAW_EXTS:
            if shot.raw_path is None:
                shot.raw_path = path

    return [pairs[k] for k in sorted(pairs.keys(), key=lambda k: (str(k[0]), k[1]))]
