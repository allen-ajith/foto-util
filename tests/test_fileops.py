"""The single mutation surface: staged move, crash-safe ordering, undo, empty."""

from __future__ import annotations

import pytest

import foto_util.fileops as fileops
from foto_util.hashing import hash_file
from foto_util.safety import UnsafePathError


def _arw(card):
    return card / "DCIM" / "100MSDCF" / "DSC00001.ARW"


def test_stage_move_then_card_loses_file_and_trash_keeps_it(card, tmp_path):
    trash = tmp_path / "trash"
    src = _arw(card)
    h = hash_file(src)

    dest = fileops.stage_move(src, card, trash, strict=True)

    assert not src.exists()                 # removed from card
    assert dest.exists()                    # present in off-card trash
    assert hash_file(dest) == h             # identical content
    # trash mirrors the card-relative path
    assert dest.relative_to(trash) == src.relative_to(card)


def test_crash_between_copy_and_unlink_leaves_original_intact(card, tmp_path, monkeypatch):
    """G5: if the process dies after the verified trash copy but before the
    card unlink, the original is still present and intact on the card."""
    trash = tmp_path / "trash"
    src = _arw(card)
    h = hash_file(src)

    # Simulate the crash exactly at the unlink step.
    def boom(*a, **k):
        raise RuntimeError("simulated crash before unlink")

    monkeypatch.setattr(fileops.os, "unlink", boom)

    with pytest.raises(RuntimeError):
        fileops.stage_move(src, card, trash, strict=True)

    assert src.exists()                     # original never lost
    assert hash_file(src) == h              # ...and intact
    # a verified copy is already safely in the trash
    copies = [p for p in trash.rglob("*") if p.is_file() and not p.name.endswith(fileops._TMP_SUFFIX)]
    assert copies and hash_file(copies[0]) == h


def test_undo_restores_exact_path_and_hash(card, tmp_path):
    trash = tmp_path / "trash"
    src = _arw(card)
    h = hash_file(src)

    dest = fileops.stage_move(src, card, trash, strict=True)
    assert not src.exists()

    restored = fileops.restore(dest, src)

    assert restored == src
    assert src.exists()
    assert hash_file(src) == h              # identical content hash
    assert not dest.exists()                # trash copy cleaned up


def test_trash_roundtrip_preserves_mtime_but_drops_xattrs(card, tmp_path):
    """A stage-move + restore keeps the file's timestamp, but never carries
    extended attributes onto the card — on exFAT those become ._AppleDouble
    sidecar litter, so we deliberately don't propagate them."""
    import os

    trash = tmp_path / "trash"
    src = _arw(card)
    orig_mtime_ns = os.stat(src).st_mtime_ns
    try:
        os.setxattr(src, "user.footest", b"marker")
        xattr_supported = "user.footest" in os.listxattr(src)
    except (OSError, AttributeError):
        xattr_supported = False

    dest = fileops.stage_move(src, card, trash, strict=True)
    fileops.restore(dest, src)

    assert src.exists()
    assert os.stat(src).st_mtime_ns == orig_mtime_ns       # timestamp preserved
    if xattr_supported:
        assert "user.footest" not in os.listxattr(src)     # xattrs NOT propagated


def test_restore_refuses_to_overwrite_a_different_file(card, tmp_path):
    """If the original path now holds *different* content (e.g. the camera
    reused the filename after the shot was trashed), restore must refuse —
    overwriting would destroy the newer file with no staging on the way back."""
    trash = tmp_path / "trash"
    src = _arw(card)
    orig_hash = hash_file(src)
    dest = fileops.stage_move(src, card, trash, strict=True)
    src.write_bytes(b"A NEW, DIFFERENT PHOTO AT THE SAME PATH")
    new_hash = hash_file(src)

    with pytest.raises(fileops.RestoreConflictError):
        fileops.restore(dest, src)

    assert hash_file(src) == new_hash                      # new file untouched
    assert dest.exists() and hash_file(dest) == orig_hash  # trash copy retained


def test_restore_is_idempotent_when_original_present(card, tmp_path):
    trash = tmp_path / "trash"
    src = _arw(card)
    dest = fileops.stage_move(src, card, trash, strict=True)
    fileops.restore(dest, src)
    # Second restore (e.g. double undo / retry) must not explode.
    again = fileops.restore(dest, src)
    assert again == src and src.exists()


def test_stage_move_refuses_unsafe_path(card, tmp_path):
    trash = tmp_path / "trash"
    mgmt = card / "MISC" / "canary.dat"
    with pytest.raises(UnsafePathError):
        fileops.stage_move(mgmt, card, trash, strict=True)
    assert mgmt.exists()                    # untouched


def test_recover_all_restores_to_mirrored_card_path(card, tmp_path):
    """The rescue walk puts every eligible trash file back at card_root/<rel>,
    reconstructed from the trash layout — correct even if the card is now mounted
    elsewhere — and leaves stray non-image files alone."""
    trash = tmp_path / "trash"
    a = _arw(card)
    b = card / "DCIM" / "100MSDCF" / "DSC00002.ARW"
    ha, hb = hash_file(a), hash_file(b)
    fileops.stage_move(a, card, trash, strict=True)
    fileops.stage_move(b, card, trash, strict=True)
    # a stray non-image file in the trash must be ignored, not restored
    (trash / "DCIM" / "100MSDCF" / "notes.txt").write_text("junk")

    # Recover to a *different* root (a remount under a new mountpoint).
    new_root = tmp_path / "NEW"
    restored, errors = fileops.recover_all(trash, new_root)

    assert errors == []
    assert restored == 2
    ra = new_root / "DCIM" / "100MSDCF" / "DSC00001.ARW"
    rb = new_root / "DCIM" / "100MSDCF" / "DSC00002.ARW"
    assert ra.exists() and hash_file(ra) == ha
    assert rb.exists() and hash_file(rb) == hb
    assert (trash / "DCIM" / "100MSDCF" / "notes.txt").exists()  # stray left alone


def test_recover_all_never_overwrites_a_different_card_file(card, tmp_path):
    """If the card slot now holds *different* content (e.g. a clock-fixed copy),
    recover must keep the card version and leave the trash copy in place."""
    trash = tmp_path / "trash"
    a = _arw(card)
    orig_hash = hash_file(a)
    dest = fileops.stage_move(a, card, trash, strict=True)   # card slot now empty
    a.write_bytes(b"DIFFERENT CONTENT AT THE SAME PATH")      # a different file reappears
    changed_hash = hash_file(a)

    restored, errors = fileops.recover_all(trash, card)

    assert restored == 0
    assert len(errors) == 1 and "different file" in errors[0][1]
    assert hash_file(a) == changed_hash                        # card version untouched
    assert dest.exists() and hash_file(dest) == orig_hash      # trash copy retained


def test_recover_all_cleans_up_when_card_already_has_same_bytes(card, tmp_path):
    """If the card already holds identical bytes, recover just drops the redundant
    trash copy (idempotent) without touching the card file."""
    trash = tmp_path / "trash"
    a = _arw(card)
    h = hash_file(a)
    dest = fileops.stage_move(a, card, trash, strict=True)
    a.write_bytes(dest.read_bytes())                           # same bytes back on card
    assert hash_file(a) == h

    restored, errors = fileops.recover_all(trash, card)

    assert errors == []
    assert restored == 1
    assert hash_file(a) == h                                   # card version intact
    assert not dest.exists()                                   # redundant copy cleaned up


def test_recover_all_non_strict_restores_plain_folder_layout(tmp_path):
    """A plain-folder source mirrors its own layout into the trash (no DCIM
    shape); non-strict recover must put those files back rather than silently
    skipping everything."""
    trash = tmp_path / "trash"
    root = tmp_path / "folder"
    (root / "sub").mkdir(parents=True)
    img = root / "sub" / "IMG_0001.JPG"
    img.write_bytes(b"jpeg bytes")
    h = hash_file(img)
    dest = fileops.stage_move(img, root, trash, strict=False)
    assert not img.exists()

    restored, errors = fileops.recover_all(trash, root, strict=False)

    assert errors == [] and restored == 1
    assert img.exists() and hash_file(img) == h
    assert not dest.exists()


def test_empty_trash_only_touches_trash(card, tmp_path):
    trash = tmp_path / "trash"
    fileops.stage_move(_arw(card), card, trash, strict=True)
    fileops.stage_move(card / "DCIM" / "100MSDCF" / "DSC00002.ARW", card, trash, strict=True)

    n = fileops.empty_trash(trash)
    assert n == 2
    assert not trash.exists()
    # the card still has everything we didn't move
    assert (card / "DCIM" / "100MSDCF" / "DSC00003.ARW").exists()


def test_sidecar_sweep_is_pattern_locked_to_dcim(card):
    """Eject's opt-in sidecar sweep may only remove ``._*`` files under DCIM/.
    Photos, and ._ files elsewhere on the card, are untouchable."""
    folder = card / "DCIM" / "100MSDCF"
    in_dcim = folder / "._DSC00001.JPG"
    in_dcim.write_bytes(b"\x00\x05\x16\x07")
    outside = card / "PRIVATE" / "._SONYCARD"
    outside.parent.mkdir(exist_ok=True)
    outside.write_bytes(b"\x00\x05\x16\x07")
    photo = folder / "DSC00001.JPG"

    found = fileops.find_sidecars(card)
    assert in_dcim in found and outside not in found

    assert fileops.clean_sidecars(card) == len(found)
    assert not in_dcim.exists()
    assert outside.exists() and photo.exists()      # untouched

    # a source with no DCIM/ (plain folder) has nothing to sweep
    assert fileops.find_sidecars(card / "PRIVATE") == []
