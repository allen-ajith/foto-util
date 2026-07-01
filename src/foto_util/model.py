"""Core value types shared across modules.

``Shot`` mirrors a row of the ``shot`` table (see the schema in the design doc,
§4). Pairing produces a lighter :class:`PairedShot` from the filesystem; the
indexer enriches it (hash, capture time, group) into a persisted ``Shot``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Decision(str, Enum):
    """A per-shot decision. The string values are what is stored in SQLite."""

    KEEP_BOTH = "keep_both"   # B — no file op
    KEEP_JPG = "keep_jpg"     # J — stage-move the RAW to trash
    REJECT = "reject"         # X — stage-move RAW and JPEG to trash

    def __str__(self) -> str:  # nicer logging
        return self.value


@dataclass(slots=True)
class PairedShot:
    """A logical shot as discovered on disk: a JPEG and/or a RAW sharing a stem.

    Exactly one or both of ``jpg_path`` / ``raw_path`` is set. ``stem`` and
    ``folder`` identify the pair within a single card folder (stems are only
    unique within a folder, e.g. ``DSC00001`` can recur across ``100MSDCF`` and
    ``101MSDCF``).
    """

    stem: str
    folder: Path
    jpg_path: Path | None = None
    raw_path: Path | None = None

    @property
    def has_jpg(self) -> bool:
        return self.jpg_path is not None

    @property
    def has_raw(self) -> bool:
        return self.raw_path is not None

    @property
    def is_orphan(self) -> bool:
        return not (self.has_jpg and self.has_raw)

    @property
    def hash_source(self) -> Path:
        """File whose bytes key this shot: the JPEG, or the RAW if JPEG-only."""
        return self.jpg_path if self.jpg_path is not None else self.raw_path  # type: ignore[return-value]


@dataclass(slots=True)
class Shot:
    """A persisted shot row (one source of truth, off-card)."""

    hash: str
    volume_id: str | None = None
    jpg_path: str | None = None
    raw_path: str | None = None
    group_id: int | None = None
    decision: Decision | None = None
    decided_at: str | None = None
    trash_jpg: str | None = None
    trash_raw: str | None = None

    @property
    def is_decided(self) -> bool:
        return self.decision is not None
