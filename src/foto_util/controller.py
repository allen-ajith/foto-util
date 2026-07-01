"""The cull-loop controller (framework-agnostic).

``Session`` turns the keyboard decisions into the safe, staged file operations
and the store updates, and owns the cursor over the ordered shot list. The Qt
viewer is a thin shell over this object, which keeps the decision→file-op mapping
in one place that can be tested without a GUI.

Decision → file operation (design doc §5):

* **keep both** — no file op.
* **keep JPEG** — stage-move the RAW to trash; keep the JPEG. Only valid for a
  RAW+JPEG pair: on an orphan it raises :class:`OrphanDecisionError` (use keep or
  delete instead) rather than touching the sole file.
* **drop both** — stage-move whatever exists (RAW and/or JPEG) to trash.
* **undo** — reverse the most recent decision, restoring any trashed file to its
  exact original path.

Decisions are reversible until the trash is emptied: re-deciding a shot restores
any previously trashed files first, so you can change your mind freely.
"""

from __future__ import annotations

from pathlib import Path

from . import fileops
from .appdir import trash_root
from .model import Decision, Shot
from .store import Store
from .volume import trash_dirname


class OrphanDecisionError(ValueError):
    """A decision was applied that doesn't make sense for an orphan (single-file)
    shot — e.g. 'keep JPEG, drop RAW' when there is no RAW+JPEG pair."""


class Session:
    def __init__(
        self,
        store: Store,
        *,
        card_root: str | Path,
        volume_id: str | None,
        strict: bool,
    ):
        self.store = store
        self.card_root = Path(card_root)
        self.volume_id = volume_id
        self.strict = strict
        vol = volume_id or "unknown"
        self.trash_dir = trash_root() / trash_dirname(vol)
        self.shots: list[Shot] = []
        self.index: int = 0
        self.reload()
        self.goto_first_undecided()  # resume: land on the first undecided shot

    # -- shot list / cursor -------------------------------------------------
    def reload(self) -> None:
        """Re-read the ordered shot list, keeping the cursor on the same shot
        where possible."""
        keep = self.current.hash if self.shots else None
        self.shots = self.store.shots(self.volume_id)
        if keep is not None:
            self.index = next(
                (i for i, s in enumerate(self.shots) if s.hash == keep), 0
            )
        self.index = min(self.index, max(0, len(self.shots) - 1))

    @property
    def current(self) -> Shot | None:
        return self.shots[self.index] if self.shots else None

    def _refresh_current(self) -> None:
        cur = self.current
        if cur is not None:
            fresh = self.store.get(cur.hash)
            if fresh is not None:
                self.shots[self.index] = fresh

    # -- navigation ---------------------------------------------------------
    def goto_first_undecided(self) -> bool:
        """Position the cursor on the first undecided shot (used on resume)."""
        for i, s in enumerate(self.shots):
            if not s.is_decided:
                self.index = i
                return True
        return False

    def goto_next_undecided(self) -> bool:
        """Move the cursor to the next undecided shot after the current one.
        Returns False if there is none (cursor unchanged)."""
        for i in range(self.index + 1, len(self.shots)):
            if not self.shots[i].is_decided:
                self.index = i
                return True
        # wrap once to catch undecided shots earlier in the list
        for i in range(0, self.index):
            if not self.shots[i].is_decided:
                self.index = i
                return True
        return False

    def skip(self) -> None:
        if self.index < len(self.shots) - 1:
            self.index += 1

    def back(self) -> None:
        if self.index > 0:
            self.index -= 1

    def next_group(self) -> None:
        if not self.shots:
            return
        g = self.shots[self.index].group_id
        for i in range(self.index + 1, len(self.shots)):
            if self.shots[i].group_id != g:
                self.index = i
                return

    def prev_group(self) -> None:
        if not self.shots:
            return
        g = self.shots[self.index].group_id
        for i in range(self.index - 1, -1, -1):
            if self.shots[i].group_id != g:
                target = self.shots[i].group_id
                # jump to the first shot of that previous group
                while i > 0 and self.shots[i - 1].group_id == target:
                    i -= 1
                self.index = i
                return

    def current_group_shots(self) -> tuple[list[Shot], int]:
        """The shots in the cursor's time-gap group, plus the cursor's index
        within them. The filmstrip scopes to this so a big card's strip stays
        legible instead of collapsing to hundreds of 1px ticks; it re-scopes
        automatically when the cursor crosses into a new group."""
        cur = self.current
        if cur is None:
            return [], 0
        group = [s for s in self.shots if s.group_id == cur.group_id]
        idx = next((i for i, s in enumerate(group) if s.hash == cur.hash), 0)
        return group, idx

    # -- decisions ----------------------------------------------------------
    def decide(self, decision: Decision, *, advance: bool = True) -> None:
        """Apply (or change) the decision for the current shot.

        Decisions are *not* final until the trash is emptied: re-deciding a shot
        first restores anything previously moved to trash (back to a clean
        'both on card' state), then applies the new decision. So you can navigate
        back and press a different key any time to change your mind.
        """
        shot = self.current
        if shot is None:
            return
        # "Keep JPEG, drop RAW" only makes sense for a RAW+JPEG pair. On an
        # orphan there is nothing to choose between, so refuse with guidance
        # rather than silently keeping the one file (which mislabels the shot).
        if decision is Decision.KEEP_JPG and not (shot.jpg_path and shot.raw_path):
            kind = "JPEG-only" if shot.jpg_path else "RAW-only"
            raise OrphanDecisionError(
                f"This shot is {kind} — “keep JPEG, drop RAW” needs a RAW+JPEG "
                "pair. Press 2 to keep it or 3 to delete it."
            )
        # reconcile: undo any prior file moves for this shot before re-deciding
        self._restore_files(shot)

        if decision is Decision.KEEP_BOTH:
            self.store.set_decision(shot.hash, Decision.KEEP_BOTH)
        elif decision is Decision.KEEP_JPG:
            trash_raw = None
            if shot.jpg_path and shot.raw_path:  # only drop RAW when a JPEG remains
                trash_raw = str(self._move(shot.raw_path))
            self.store.set_decision(shot.hash, Decision.KEEP_JPG, trash_raw=trash_raw)
        elif decision is Decision.REJECT:
            trash_jpg = str(self._move(shot.jpg_path)) if shot.jpg_path else None
            trash_raw = str(self._move(shot.raw_path)) if shot.raw_path else None
            self.store.set_decision(
                shot.hash, Decision.REJECT, trash_jpg=trash_jpg, trash_raw=trash_raw
            )
        self._refresh_current()
        if advance:
            self.goto_next_undecided()

    def _restore_files(self, shot: Shot) -> None:
        """Put any trashed files for ``shot`` back on the card (exact path), so a
        changed decision starts from a clean slate. No-op for an undecided shot.

        The destination is the stored ``jpg_path`` / ``raw_path`` when present;
        if that was lost (an older row whose path got NULLed before the upsert
        fix), it is reconstructed from the trash layout so the file is never
        stranded by a restore that silently skips it."""
        if shot.trash_jpg:
            fileops.restore(shot.trash_jpg, shot.jpg_path or self._dest_for(shot.trash_jpg))
        if shot.trash_raw:
            fileops.restore(shot.trash_raw, shot.raw_path or self._dest_for(shot.trash_raw))

    def _dest_for(self, trash_path: str) -> str:
        """Reconstruct an on-card destination from a file's mirrored location in
        the trash tree (``card_root / <path relative to trash_dir>``)."""
        rel = Path(trash_path).relative_to(self.trash_dir)
        return str(self.card_root / rel)

    def _move(self, path: str) -> Path:
        return fileops.stage_move(
            path, self.card_root, self.trash_dir, strict=self.strict
        )

    # -- undo ---------------------------------------------------------------
    def undo_last(self) -> bool:
        """Reverse the most recent still-reversible decision: restore the trashed
        file(s) of the most-recently-decided shot that still has any, and clear
        its decision. Returns False if nothing is reversible.

        The candidate comes from the database (``last_recoverable``), not an
        in-memory stack, so undo works even in a fresh session after the app was
        closed and reopened — the recoverable state *is* the trash pointers."""
        shot = self.store.last_recoverable(self.volume_id)
        if shot is None:
            return False
        # Restore first; only clear the decision once the file(s) are safely
        # back, so a failed restore leaves the pointer (and the file) intact.
        self._restore_files(shot)
        self.store.clear_decision(shot.hash)
        # park the cursor on the restored shot, if it is in the current list
        self.index = next(
            (i for i, s in enumerate(self.shots) if s.hash == shot.hash), self.index
        )
        self._refresh_current()
        return True

    def recover_orphaned_trash(self) -> tuple[int, list[tuple[str, str]]]:
        """Restore every file in this card's trash back onto the card — the
        rescue path for files stranded with no usable database link (e.g. after a
        metadata clear dropped their rows). On a clean run (no per-file errors)
        the affected shots are reset to undecided, since their files are present
        again. Returns ``(restored_count, errors)``."""
        restored, errors = fileops.recover_all(self.trash_dir, self.card_root)
        if restored and not errors:
            self.store.reset_trashed_decisions(self.volume_id)
        return restored, errors
