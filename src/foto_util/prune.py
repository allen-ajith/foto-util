"""Prune database — metadata-only housekeeping (guard G11).

This deletes only off-card DB rows; it never touches an image file, on any card
or folder. Three scopes, each surfaced behind a preview + confirm in the viewer:

* **stale** — rows for the *currently mounted* volume whose files are confirmed
  gone: not on the card **and** not in the local trash. Pruned rows for files
  that still exist are simply regenerated on the next scan.
* **forget this card** — every row for the mounted volume (e.g. after a
  reformat).
* **clear all** — reset the whole DB (hard confirm).

The critical safety point: staleness is judged only for the volume we are
*currently looking at*. Rows for a card that isn't connected are never inspected
or pruned — those files still exist and their rows are live resume state.
"""

from __future__ import annotations

import os

from .model import Shot
from .store import Store


def _is_gone(shot: Shot) -> bool:
    """True iff none of the files this row references exist anywhere — neither
    on the card (jpg/raw paths) nor in the off-card trash (trash pointers)."""
    refs = [p for p in (shot.jpg_path, shot.raw_path, shot.trash_jpg, shot.trash_raw) if p]
    if not refs:
        return True  # references nothing real; safe to drop
    return all(not os.path.exists(p) for p in refs)


def find_stale(store: Store, volume_id: str) -> list[Shot]:
    """Rows for ``volume_id`` whose files are confirmed gone. Caller must pass
    the *currently mounted* volume's id so live rows for other cards are never
    considered."""
    return [s for s in store.shots(volume_id) if _is_gone(s)]


def prune_stale(store: Store, volume_id: str) -> int:
    """Delete the stale rows for the mounted volume. Returns the count removed."""
    stale = find_stale(store, volume_id)
    return store.delete_rows([s.hash for s in stale])
