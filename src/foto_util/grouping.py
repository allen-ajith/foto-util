"""Time-gap grouping (burst / scene clustering).

Shots are ordered by capture time and split where the inter-shot gap exceeds a
threshold. This is robust to the wrong-clock problem: grouping uses gaps
*between* shots, and a misset-once clock shifts every timestamp by a constant,
leaving intervals intact (design doc §4).

The stored ``group_id`` is the *scene* cluster (gap > 5 min) — the coarse unit
the ``[`` / ``]`` keys jump across, and what draws the filmstrip dividers.
"""

from __future__ import annotations

from dataclasses import dataclass

# Default scene threshold (tunable via settings, design doc §11).
SCENE_GAP_S = 5 * 60.0


@dataclass(slots=True)
class GroupItem:
    """Minimal sortable handle the grouper works with. ``key`` is the value
    used both for ordering and for gap computation (seconds, monotonic)."""

    ident: str          # the shot hash
    time_value: float   # seconds since epoch (EXIF time, or mtime fallback)
    seq: int            # file-number / discovery sequence, the tiebreaker


def _ordered(items: list[GroupItem]) -> list[GroupItem]:
    return sorted(items, key=lambda it: (it.time_value, it.seq, it.ident))


def assign_groups(items: list[GroupItem], gap_s: float = SCENE_GAP_S) -> dict[str, int]:
    """Return ``{ident: group_id}`` where a new group starts whenever the gap to
    the previous shot exceeds ``gap_s``. Group ids are contiguous from 0 in
    capture order, so ``ORDER BY group_id`` reflects time order."""
    result: dict[str, int] = {}
    group = 0
    prev: float | None = None
    for it in _ordered(items):
        if prev is not None and (it.time_value - prev) > gap_s:
            group += 1
        result[it.ident] = group
        prev = it.time_value
    return result
