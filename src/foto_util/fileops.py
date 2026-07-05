"""The single mutation surface (guards G2, G4, G5, G10).

This is the *only* module permitted to change the source volume, and the only
thing it ever does there is remove an image file the user explicitly rejected —
and even that is staged and reversible.

Removal order (G5), so an image can never be lost to an interrupted operation:

    1. copy the file to the off-card trash (to a temp name);
    2. fsync and verify the copy (size **and** content hash);
    3. atomically place it at its final trash path;
    4. only then ``unlink`` the original from the card.

If the process dies at any point before step 4, the original is still intact on
the card. Undo (:func:`restore`) reverses the move, writing the bytes back to
their exact original path and verifying the hash before removing the trash copy.

Every destructive call first runs :func:`foto_util.safety.assert_deletable`, which
hard-refuses anything that is not an eligible image file under ``DCIM/<folder>/``
(and never a management folder).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .hashing import hash_file
from .safety import (
    UnsafePathError,
    assert_deletable,
    is_image_file,
    is_within_dcim,
    touches_management_dir,
)

_TMP_SUFFIX = ".foto-util-tmp"


class VerificationError(Exception):
    """The trash copy did not match the original (size or content hash)."""


class RestoreConflictError(Exception):
    """The restore destination already holds a *different* file. Restoring would
    destroy it (there is no staging on the way back to the card), so the restore
    is refused and both files are left untouched."""


# -- low-level durability helpers ------------------------------------------
def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    # Durably record the rename/unlink in the directory entry.
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems disallow fsync on a directory; best-effort
    finally:
        os.close(fd)


def _unique(dest: Path) -> Path:
    """Pick a non-colliding final path under the trash dir."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        candidate = dest.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _copy_verify(src: Path, dst_tmp: Path, src_hash: str, src_size: int) -> None:
    """Copy ``src`` to ``dst_tmp`` and verify it byte-for-byte.

    Preserves the modification time and permission bits for fidelity, but
    deliberately does **not** copy extended attributes (unlike
    ``shutil.copystat``). On the exFAT filesystem SD cards use, macOS can't store
    xattrs inline, so it materializes them as ``._AppleDouble`` sidecar files —
    copying xattrs onto the card while restoring would litter it with ``._``
    files. We only ever care about the timestamp, so we skip xattrs entirely.
    """
    shutil.copyfile(src, dst_tmp)        # content only; src opened read-only
    shutil.copymode(src, dst_tmp)        # permission bits (no xattrs → no ._ sidecars)
    st = os.stat(src)
    os.utime(dst_tmp, ns=(st.st_atime_ns, st.st_mtime_ns))  # preserve mtime
    _fsync_file(dst_tmp)
    if dst_tmp.stat().st_size != src_size:
        dst_tmp.unlink(missing_ok=True)
        raise VerificationError(f"size mismatch copying {src}")
    if hash_file(dst_tmp) != src_hash:
        dst_tmp.unlink(missing_ok=True)
        raise VerificationError(f"hash mismatch copying {src}")


# -- the staged move (Reject / Keep-JPEG) ----------------------------------
def stage_move(
    src: str | Path,
    card_root: str | Path,
    trash_dir: str | Path,
    *,
    strict: bool,
) -> Path:
    """Move one image file from the card to the off-card trash, crash-safely.

    Returns the final trash path (to record for undo). Raises
    :class:`~foto_util.safety.UnsafePathError` if ``src`` is not eligible, or
    :class:`VerificationError` if the copy cannot be verified (in which case the
    original is left untouched on the card).
    """
    p = assert_deletable(src, card_root, strict=strict)
    root = Path(card_root).resolve()
    trash_dir = Path(trash_dir)

    rel = p.relative_to(root)
    dest_final = _unique(trash_dir / rel)
    dest_final.parent.mkdir(parents=True, exist_ok=True)
    dest_tmp = dest_final.with_name(dest_final.name + _TMP_SUFFIX)

    src_size = p.stat().st_size
    src_hash = hash_file(p)

    # (1)+(2) copy + verify into a temp name in the trash.
    _copy_verify(p, dest_tmp, src_hash, src_size)
    # (3) atomically place at the final trash path, durably.
    os.replace(dest_tmp, dest_final)
    _fsync_dir(dest_final.parent)
    # (4) only now remove the original from the card.
    os.unlink(p)
    _fsync_dir(p.parent)
    return dest_final


# -- undo -------------------------------------------------------------------
def restore(trash_path: str | Path, original_path: str | Path) -> Path:
    """Restore a trashed file to its exact original path, crash-safely.

    Verifies the restored bytes match the trash copy before removing the trash
    copy, so a crash mid-restore never loses the image. Idempotent: if the
    original already exists with a matching hash, the trash copy is simply
    cleaned up. If the original path holds a *different* file (e.g. the camera
    reused the filename after the shot was trashed), the restore is refused with
    :class:`RestoreConflictError` — it never overwrites content it can't verify.
    """
    trash_path = Path(trash_path)
    original = Path(original_path)

    if not trash_path.exists():
        # Already restored on a previous attempt (or never trashed).
        if original.exists():
            return original
        raise FileNotFoundError(f"trash copy missing: {trash_path}")

    trash_hash = hash_file(trash_path)
    if original.exists():
        if hash_file(original) == trash_hash:
            trash_path.unlink(missing_ok=True)  # idempotent cleanup
            return original
        raise RestoreConflictError(
            f"a different file now exists at {original} — refusing to overwrite "
            "it (the trashed copy is kept in the trash)"
        )

    original.parent.mkdir(parents=True, exist_ok=True)
    tmp = original.with_name(original.name + _TMP_SUFFIX)
    src_size = trash_path.stat().st_size
    _copy_verify(trash_path, tmp, trash_hash, src_size)
    os.replace(tmp, original)             # atomic; original now present
    _fsync_dir(original.parent)
    # Original is safely back; drop the trash copy last.
    trash_path.unlink(missing_ok=True)
    return original


# -- bulk recovery (rescue files stranded in the trash) --------------------
def recover_all(
    trash_dir: str | Path, card_root: str | Path, *, strict: bool = True
) -> tuple[int, list[tuple[str, str]]]:
    """Restore every eligible image under ``trash_dir`` to its mirrored path on
    the card — the inverse of :func:`stage_move`'s layout (it trashes to
    ``trash_dir / <path relative to card_root>``, so the card path is
    ``card_root / <path relative to trash_dir>``).

    This is the safety net for files stranded in the trash with no usable
    database link (e.g. after a metadata clear): it works straight off the trash
    tree, reconstructing each destination from the mirrored path so it is correct
    even if the card is now mounted somewhere else.

    ``strict`` mirrors :func:`~foto_util.safety.assert_deletable`'s card mode: on a
    real card only ``DCIM/<folder>/`` image files are restored; for a plain
    (non-card) folder source the DCIM-shape requirement is relaxed, since its
    trash mirror has whatever layout the folder had. Image-only and
    management-folder exclusions always apply; anything else is skipped.

    Conflict-safe: a trash file is restored only when its card slot is **empty**
    (the true orphan case) or already holds the **same bytes** (in which case the
    redundant trash copy is just cleaned up). If the card already has a
    *different* file at that path — e.g. a clock-fixed copy whose EXIF date was
    rewritten after the original was trashed — the card version is left untouched
    and the file is reported, never overwritten.

    Returns ``(restored_count, [(trash_path, reason), ...])`` where the second
    list is files that were deliberately not restored (a conflict) or failed.
    """
    trash_dir = Path(trash_dir)
    card_root = Path(card_root)
    if not trash_dir.exists():
        return 0, []
    restored = 0
    errors: list[tuple[str, str]] = []
    for f in sorted(trash_dir.rglob("*")):
        if not f.is_file() or f.name.endswith(_TMP_SUFFIX):
            continue
        if not (
            is_image_file(f)
            and (not strict or is_within_dcim(f, trash_dir))
            and not touches_management_dir(f, trash_dir)
        ):
            continue  # stray non-image / wrong-shape file: leave it alone
        dest = card_root / f.relative_to(trash_dir)
        try:
            if dest.exists() and hash_file(dest) != hash_file(f):
                errors.append(
                    (str(f), "card already has a different file here — kept the card version")
                )
                continue
            restore(f, dest)   # empty slot → moved back; identical → trash cleaned up
            restored += 1
        except Exception as e:  # keep going; report per-file failures
            errors.append((str(f), str(e)))
    return restored, errors


# -- macOS sidecar cleanup (opt-in, offered at eject) -----------------------
def find_sidecars(card_root: str | Path) -> list[Path]:
    """The macOS ``._`` AppleDouble sidecar files under the card's ``DCIM/``.

    macOS materializes extended attributes as ``._<name>`` files on exFAT
    cards; cameras never create or read them. Pattern-locked: only regular
    files whose own name starts with ``._``, only under the DCIM tree — the
    pattern cannot match a photo. A source with no ``DCIM/`` yields nothing.
    """
    dcim = Path(card_root) / "DCIM"
    if not dcim.is_dir():
        return []
    return sorted(
        p for p in dcim.rglob("._*") if p.is_file() and p.name.startswith("._")
    )


def clean_sidecars(card_root: str | Path) -> int:
    """Remove the sidecars :func:`find_sidecars` reports. Returns the count."""
    removed = 0
    for p in find_sidecars(card_root):
        p.unlink(missing_ok=True)
        removed += 1
    return removed


# -- permanent removal (explicit, confirmed; guard G10) --------------------
def empty_trash(trash_dir: str | Path) -> int:
    """Permanently delete everything under ``trash_dir``. Returns the count of
    staged files removed (leftover ``*.foto-util-tmp`` debris is deleted too but
    not counted — it was never a recoverable file).

    This touches only the off-card trash; it never goes near a card.
    """
    trash_dir = Path(trash_dir)
    if not trash_dir.exists():
        return 0
    count = sum(
        1 for f in trash_dir.rglob("*")
        if f.is_file() and not f.name.endswith(_TMP_SUFFIX)
    )
    shutil.rmtree(trash_dir)
    return count


__all__ = [
    "stage_move",
    "restore",
    "recover_all",
    "find_sidecars",
    "clean_sidecars",
    "empty_trash",
    "VerificationError",
    "RestoreConflictError",
    "UnsafePathError",
]
