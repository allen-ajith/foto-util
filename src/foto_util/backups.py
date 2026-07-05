"""Verify the clock-fix ``_original`` backups, then reclaim them — but only when
the image data provably didn't change.

A clock-fix (``meta.shift_all_dates``) rewrites a file's *metadata* (the dates),
so the whole-file hash no longer matches the ``_original`` backup exiftool kept.
That difference can't distinguish an intended date edit from corruption. exiftool's
``ImageDataHash`` (see :func:`foto_util.meta.image_data_hashes`) hashes only the image /
sensor data, so it is invariant to the date shift and changes *only* if the actual
image bytes are damaged.

So: a backup is removed only when ``ImageDataHash(live) == ImageDataHash(backup)``
— i.e. the live file is provably the same image, differing from the backup in
metadata alone. Anything that doesn't match (or can't be hashed) is left in place
and reported, so a genuinely corrupted file keeps its recoverable original.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import meta

BACKUP_SUFFIX = "_original"

ProgressFn = Callable[[int, int], None]  # (done, total)


@dataclass
class VerifyResult:
    removed: list[str] = field(default_factory=list)      # backups deleted (image data verified identical)
    mismatched: list[str] = field(default_factory=list)   # live files whose image data differs (backup kept)
    errored: list[str] = field(default_factory=list)      # couldn't hash / live missing (backup kept)

    @property
    def reclaimed(self) -> int:
        return len(self.removed)


def live_path(backup: Path) -> Path:
    """The live file a ``*_original`` backup belongs to (strip the suffix)."""
    return backup.with_name(backup.name[: -len(BACKUP_SUFFIX)])


def find_backups(root: str | Path) -> list[Path]:
    """Every exiftool ``*_original`` backup under ``root`` — scoped to the
    ``DCIM/`` tree when there is one (a card), because that is the only place
    the clock fix ever writes. Keeps the delete surface to the audited card
    area; a plain folder source (no ``DCIM``) is searched as given."""
    root = Path(root)
    dcim = root / "DCIM"
    scope = dcim if dcim.is_dir() else root
    return sorted(p for p in scope.rglob(f"*{BACKUP_SUFFIX}") if p.is_file())


def verify_and_remove(
    root: str | Path,
    *,
    batch: int = 64,
    progress: ProgressFn | None = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> VerifyResult:
    """Delete every ``_original`` backup under ``root`` whose live file has
    identical image data; keep and report the rest. Hashing is batched so
    exiftool starts once per ``batch`` files rather than once per file."""
    backups = find_backups(root)
    total = len(backups)
    res = VerifyResult()
    done = 0
    for start in range(0, total, batch):
        if should_stop is not None and should_stop():
            break
        chunk = backups[start : start + batch]
        lives = [live_path(b) for b in chunk]
        existing = [str(p) for p in (*chunk, *lives) if p.exists()]
        hashes = meta.image_data_hashes(existing)
        for backup in chunk:
            live = live_path(backup)
            hb = hashes.get(str(backup))
            hl = hashes.get(str(live))
            if not live.exists() or hb is None or hl is None:
                res.errored.append(str(live))
            elif hb == hl:
                os.remove(backup)              # image data identical → safe to reclaim
                res.removed.append(str(backup))
            else:
                res.mismatched.append(str(live))  # image data differs → keep the original
            done += 1
        if progress is not None:
            progress(min(done, total), total)
    return res
