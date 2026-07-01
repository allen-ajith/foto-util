"""Path-safety classification for the card (guards G2/G3).

The destructive surface is deliberately tiny: the *only* files the app may ever
remove are image files the user rejected, living in ``DCIM/<folder>/``. The
camera's management folders are strictly off-limits — never read-modified, never
deleted. This module decides whether a given path is eligible for removal; the
actual mutation lives in :mod:`foto_util.fileops`, which calls
:func:`assert_deletable` before doing anything.

Keeping the predicate here (separate from the mutator) makes it independently
testable and impossible to bypass accidentally.
"""

from __future__ import annotations

from pathlib import Path

from .pairing import IMAGE_EXTS, MANAGEMENT_DIRS

# ``MANAGEMENT_DIRS`` (the camera's off-limits folders) lives in :mod:`foto_util.pairing`
# so the scan and this delete guard share one definition. The positive DCIM
# requirement below already excludes them in strict mode; the set is the
# belt-and-suspenders refusal (and what enumeration uses to skip them).


class UnsafePathError(Exception):
    """Raised when something tries to remove a path that is not an eligible
    image file under ``DCIM/<folder>/`` (or that touches a management folder)."""


def _rel_parts(path: Path, root: Path) -> tuple[str, ...]:
    """Parts of ``path`` relative to ``root``; raises if ``path`` escapes root."""
    rp = path.resolve().relative_to(root.resolve())
    return rp.parts


def touches_management_dir(path: Path, root: Path) -> bool:
    parts = {p.upper() for p in _rel_parts(path, root)}
    return bool(parts & MANAGEMENT_DIRS)


def is_within_dcim(path: Path, root: Path) -> bool:
    """True iff ``path`` is ``root/DCIM/<folder>/.../file`` (the camera layout)."""
    parts = _rel_parts(path, root)
    return len(parts) >= 3 and parts[0].upper() == "DCIM"


def is_image_file(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in IMAGE_EXTS


def assert_deletable(path: str | Path, root: str | Path, *, strict: bool) -> Path:
    """Hard-refuse anything that is not an eligible image file.

    ``strict`` is the real-card mode: the file must sit under ``DCIM/<folder>/``.
    When pointed at an arbitrary (non-card) folder, ``strict`` is relaxed but the
    file must still be an image within ``root`` and never a management folder.

    Returns the resolved path on success; raises :class:`UnsafePathError`
    otherwise. The path must already exist as a regular file.
    """
    p = Path(path).resolve()
    r = Path(root).resolve()

    try:
        rel = p.relative_to(r)
    except ValueError as e:
        raise UnsafePathError(f"{p} is outside the indexed root {r}") from e

    if not p.is_file():
        raise UnsafePathError(f"{p} is not a regular file")
    if not is_image_file(p):
        raise UnsafePathError(f"{p} is not an image file ({rel})")
    if touches_management_dir(p, r):
        raise UnsafePathError(f"{p} is inside a camera management folder")
    if strict and not is_within_dcim(p, r):
        raise UnsafePathError(f"{p} is not under DCIM/<folder>/ ({rel})")
    return p
