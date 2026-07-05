"""The off-card SQLite store — the single source of truth and the only seam
between the UI and the indexer (design doc §4, §6).

WAL mode lets the background indexer write while the UI reads. Each thread should
construct its own :class:`Store` (its own connection) over the same database
file; SQLite + WAL handles the concurrency.

This module records state only. It never touches an image file — that is
:mod:`foto_util.fileops`'s sole responsibility.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .model import Decision, Shot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shot(
  hash        TEXT PRIMARY KEY,
  volume_id   TEXT,
  jpg_path    TEXT,
  raw_path    TEXT,
  group_id    INTEGER,
  decision    TEXT,
  decided_at  TIMESTAMP,
  trash_jpg   TEXT,
  trash_raw   TEXT
);
CREATE INDEX IF NOT EXISTS shot_volume ON shot(volume_id);
CREATE INDEX IF NOT EXISTS shot_order ON shot(group_id, jpg_path, raw_path);

-- Per-file scan cache. Keyed by the *card-relative* path so it survives a
-- remount under a new mountpoint, and the stable volume id. A rescan reuses the
-- stored hash + capture time whenever size+mtime are unchanged, so an unmodified
-- card reads no file bytes at all and a changed card reads only the new/modified
-- files. Note the caveat: a cache hit *supplies* the identity hash, so a file
-- replaced in-place with identical size and mtime would keep its stale identity
-- (the standard stat-cache trade-off; vanishingly unlikely for camera files).
-- Safe to drop entirely; it just forces a one-time full re-hash to repopulate.
CREATE TABLE IF NOT EXISTS file_cache(
  volume_id     TEXT NOT NULL,
  rel_path      TEXT NOT NULL,
  size          INTEGER NOT NULL,
  mtime         REAL NOT NULL,
  hash          TEXT NOT NULL,
  capture_time  REAL,
  PRIMARY KEY (volume_id, rel_path)
);
"""


def _now_iso() -> str:
    # Millisecond precision so undo can order decisions reliably: the pause
    # between decisions is short, so a second-resolution stamp can tie two
    # decisions and make "undo the most recent" ambiguous.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _row_to_shot(row: sqlite3.Row) -> Shot:
    decision = row["decision"]
    return Shot(
        hash=row["hash"],
        volume_id=row["volume_id"],
        jpg_path=row["jpg_path"],
        raw_path=row["raw_path"],
        group_id=row["group_id"],
        decision=Decision(decision) if decision else None,
        decided_at=row["decided_at"],
        trash_jpg=row["trash_jpg"],
        trash_raw=row["trash_raw"],
    )


class Store:
    """A thin connection wrapper. Cheap to construct; one per thread."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- scan / upsert ------------------------------------------------------
    def upsert_scanned(
        self,
        *,
        hash: str,
        volume_id: str | None,
        jpg_path: str | None,
        raw_path: str | None,
        group_id: int | None,
    ) -> None:
        """Insert a freshly scanned shot, or refresh the volatile fields of an
        existing one.

        Crucially this preserves an existing ``decision`` / ``decided_at`` /
        ``trash_*`` — the resume + undo state. Only the paths, volume id, and
        group (which legitimately change across remounts and rescans) are
        updated.

        A path with a live ``trash_*`` pointer is *not* overwritten, even when a
        rescan supplies a new (or NULL) value for it: that stored path is the
        restore destination for the trashed file. After keep-JPEG the RAW is off
        the card, so a rescan pairs only the JPEG and would otherwise NULL
        ``raw_path`` — orphaning the trashed RAW (it could no longer be put
        back). Keeping the path until the trash pointer clears is what makes a
        decision recoverable across a rescan.
        """
        self.conn.execute(
            """
            INSERT INTO shot(hash, volume_id, jpg_path, raw_path, group_id)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                volume_id = excluded.volume_id,
                jpg_path  = CASE WHEN trash_jpg IS NOT NULL
                                 THEN jpg_path ELSE excluded.jpg_path END,
                raw_path  = CASE WHEN trash_raw IS NOT NULL
                                 THEN raw_path ELSE excluded.raw_path END,
                group_id  = excluded.group_id
            """,
            (hash, volume_id, jpg_path, raw_path, group_id),
        )
        self.conn.commit()

    # -- scan cache (performance only) --------------------------------------
    def cached_files(
        self, volume_id: str
    ) -> dict[str, tuple[int, float, str, float | None]]:
        """``rel_path -> (size, mtime, hash, capture_time)`` for a volume, so the
        scanner can skip reading a file whose size+mtime are unchanged."""
        rows = self.conn.execute(
            "SELECT rel_path, size, mtime, hash, capture_time "
            "FROM file_cache WHERE volume_id=?",
            (volume_id,),
        ).fetchall()
        return {
            r["rel_path"]: (r["size"], r["mtime"], r["hash"], r["capture_time"])
            for r in rows
        }

    def upsert_file_cache(
        self,
        *,
        volume_id: str,
        rel_path: str,
        size: int,
        mtime: float,
        hash: str,
        capture_time: float | None,
    ) -> None:
        """Record (or refresh) a file's stat signature → identity for the cache."""
        self.conn.execute(
            """
            INSERT INTO file_cache(volume_id, rel_path, size, mtime, hash, capture_time)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(volume_id, rel_path) DO UPDATE SET
                size=excluded.size, mtime=excluded.mtime,
                hash=excluded.hash, capture_time=excluded.capture_time
            """,
            (volume_id, rel_path, size, mtime, hash, capture_time),
        )
        self.conn.commit()

    def prune_file_cache(self, volume_id: str, keep_rel_paths: set[str]) -> int:
        """Drop cache rows for files that are no longer present. Call only after a
        *complete* scan (an interrupted one hasn't seen every file yet)."""
        rows = self.conn.execute(
            "SELECT rel_path FROM file_cache WHERE volume_id=?", (volume_id,)
        ).fetchall()
        gone = [r["rel_path"] for r in rows if r["rel_path"] not in keep_rel_paths]
        if gone:
            self.conn.executemany(
                "DELETE FROM file_cache WHERE volume_id=? AND rel_path=?",
                [(volume_id, rp) for rp in gone],
            )
            self.conn.commit()
        return len(gone)

    # -- decisions ----------------------------------------------------------
    def set_decision(
        self,
        hash: str,
        decision: Decision,
        *,
        trash_jpg: str | None = None,
        trash_raw: str | None = None,
        decided_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE shot SET decision=?, decided_at=?, trash_jpg=?, trash_raw=?
            WHERE hash=?
            """,
            (decision.value, decided_at or _now_iso(), trash_jpg, trash_raw, hash),
        )
        self.conn.commit()

    def clear_decision(self, hash: str) -> None:
        """Used by undo: wipe the decision and any trash pointers."""
        self.conn.execute(
            """
            UPDATE shot
            SET decision=NULL, decided_at=NULL, trash_jpg=NULL, trash_raw=NULL
            WHERE hash=?
            """,
            (hash,),
        )
        self.conn.commit()

    def clear_trash_pointers(self, volume_id: str | None = None) -> int:
        """Finalize the shots whose trash has just been emptied: drop the
        ``trash_*`` pointers (keeping the decision), and null the matching
        ``jpg_path`` / ``raw_path`` — the file a pointer referred to is now
        permanently gone, so its on-card path is fiction. Keeping it would let
        the row lie (e.g. a keep-JPEG shot re-decided to 'keep both' would claim
        a RAW that no longer exists anywhere). A fully rejected row ends up
        referencing nothing, which is exactly what lets pruning drop it from the
        roll. Clearing the pointers also stops undo from offering to restore
        files that no longer exist."""
        where = "(trash_jpg IS NOT NULL OR trash_raw IS NOT NULL)"
        args: tuple = ()
        if volume_id is not None:
            where += " AND volume_id=?"
            args = (volume_id,)
        cur = self.conn.execute(
            f"""
            UPDATE shot SET
                jpg_path = CASE WHEN trash_jpg IS NOT NULL THEN NULL ELSE jpg_path END,
                raw_path = CASE WHEN trash_raw IS NOT NULL THEN NULL ELSE raw_path END,
                trash_jpg = NULL, trash_raw = NULL
            WHERE {where}
            """,
            args,
        )
        self.conn.commit()
        return cur.rowcount

    def reset_trashed_decisions(self, volume_id: str | None = None) -> int:
        """Un-decide every shot that still has a trash pointer. Used after
        recovering trashed files back onto the card: the files are present again,
        so those shots are undecided (a clean slate), not 'kept JPEG' / 'rejected'
        with a now-dangling trash pointer."""
        where = "(trash_jpg IS NOT NULL OR trash_raw IS NOT NULL)"
        args: tuple = ()
        if volume_id is not None:
            where += " AND volume_id=?"
            args = (volume_id,)
        cur = self.conn.execute(
            "UPDATE shot SET decision=NULL, decided_at=NULL, "
            f"trash_jpg=NULL, trash_raw=NULL WHERE {where}",
            args,
        )
        self.conn.commit()
        return cur.rowcount

    def last_recoverable(self, volume_id: str | None = None) -> Shot | None:
        """The most-recently-decided shot that still has a file in the trash —
        the next candidate for undo.

        Ordered by ``decided_at`` (persisted) rather than an in-memory stack, so
        undo keeps working after the app is restarted: the recoverable state is
        the trash pointers in the database, not session memory.
        """
        where = (
            "decision IS NOT NULL "
            "AND (trash_jpg IS NOT NULL OR trash_raw IS NOT NULL)"
        )
        args: tuple = ()
        if volume_id is not None:
            where += " AND volume_id=?"
            args = (volume_id,)
        row = self.conn.execute(
            f"SELECT * FROM shot WHERE {where} ORDER BY decided_at DESC LIMIT 1",
            args,
        ).fetchone()
        return _row_to_shot(row) if row else None

    def pending_trash_count(self, volume_id: str | None = None) -> int:
        """How many shots still have a file staged in the (not-yet-emptied)
        trash. Used to refuse a metadata clear that would strand those files."""
        where = "(trash_jpg IS NOT NULL OR trash_raw IS NOT NULL)"
        args: tuple = ()
        if volume_id is not None:
            where += " AND volume_id=?"
            args = (volume_id,)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM shot WHERE {where}", args
        ).fetchone()[0]

    def set_group(self, hash: str, group_id: int | None) -> None:
        """Set just the group id (second pass of progressive indexing)."""
        self.conn.execute(
            "UPDATE shot SET group_id=? WHERE hash=?", (group_id, hash)
        )
        self.conn.commit()

    def rekey(self, old_hash: str, new_hash: str) -> bool:
        """Move a row to a new content hash, preserving decision / trash. Used
        after a clock-fix edits a file's bytes (and thus its identity). Returns
        False if there is no such row or the target already exists (caller should
        leave both alone)."""
        if old_hash == new_hash:
            return False
        cur = self.conn.execute(
            "UPDATE shot SET hash=? WHERE hash=? "
            "AND NOT EXISTS (SELECT 1 FROM shot WHERE hash=?)",
            (new_hash, old_hash, new_hash),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # -- reads --------------------------------------------------------------
    def get(self, hash: str) -> Shot | None:
        row = self.conn.execute(
            "SELECT * FROM shot WHERE hash=?", (hash,)
        ).fetchone()
        return _row_to_shot(row) if row else None

    def shots(self, volume_id: str | None = None) -> list[Shot]:
        """All shots (optionally for one volume), in capture-aligned order."""
        if volume_id is None:
            rows = self.conn.execute(
                "SELECT * FROM shot ORDER BY group_id, jpg_path, raw_path"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM shot WHERE volume_id=? "
                "ORDER BY group_id, jpg_path, raw_path",
                (volume_id,),
            ).fetchall()
        return [_row_to_shot(r) for r in rows]

    def counts(self, volume_id: str | None = None) -> dict[str, int]:
        # ``is None`` (not truthiness), matching every other volume-scoped query:
        # "" must scope to nothing rather than silently counting all volumes.
        where = "WHERE volume_id=?" if volume_id is not None else ""
        args = (volume_id,) if volume_id is not None else ()
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM shot {where}", args
        ).fetchone()[0]
        decided = self.conn.execute(
            f"SELECT COUNT(*) FROM shot {where} "
            f"{'AND' if where else 'WHERE'} decision IS NOT NULL",
            args,
        ).fetchone()[0]
        return {"total": total, "decided": decided, "undecided": total - decided}

    # -- pruning (metadata only, guard G11) ---------------------------------
    def delete_rows(self, hashes: list[str]) -> int:
        """Delete the given rows. Never touches files."""
        if not hashes:
            return 0
        cur = self.conn.executemany(
            "DELETE FROM shot WHERE hash=?", [(h,) for h in hashes]
        )
        self.conn.commit()
        return cur.rowcount

    def delete_volume(self, volume_id: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM shot WHERE volume_id=?", (volume_id,)
        )
        self.conn.commit()
        return cur.rowcount

    def clear_all(self) -> int:
        """Reset the whole DB (the hard-confirm 'clear all' scope): every shot
        row *and* the scan cache, so the next scan re-reads and re-hashes every
        file — a true cold reset. Never touches an image file. Returns the shot
        row count removed."""
        cur = self.conn.execute("DELETE FROM shot")
        self.conn.execute("DELETE FROM file_cache")
        self.conn.commit()
        return cur.rowcount
