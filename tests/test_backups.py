"""Verify-and-reclaim of clock-fix ``_original`` backups via image-data hashing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foto_util import backups, meta


def test_live_path_strips_the_suffix():
    assert backups.live_path(Path("/c/DSC1.JPG_original")) == Path("/c/DSC1.JPG")
    assert backups.live_path(Path("/c/DSC1.ARW_original")) == Path("/c/DSC1.ARW")


def test_find_backups_scopes_to_dcim_on_a_card(card):
    """On a card, only the DCIM tree is searched — a user's own *_original file
    elsewhere on the card is not foto-util's to touch. A plain folder source
    (no DCIM) is searched as given."""
    inside = card / "DCIM" / "100MSDCF" / "DSC00001.JPG_original"
    inside.write_bytes(b"x")
    outside = card / "PRIVATE" / "notes.txt_original"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"x")

    found = backups.find_backups(card)
    assert inside in found
    assert outside not in found

    # folder mode: no DCIM/ under the root → the root itself is the scope
    assert outside in backups.find_backups(card / "PRIVATE")


def test_verify_removes_only_image_identical_backups(card):
    if not meta.exiftool_available():
        pytest.skip("exiftool not installed")
    folder = card / "DCIM" / "100MSDCF"

    # MATCH — backup has the same image data as the live file; only the date
    # differs (exactly the clock-fix situation), so the backup is reclaimed.
    live_ok = folder / "DSC00001.JPG"
    backup_ok = folder / "DSC00001.JPG_original"
    shutil.copy2(live_ok, backup_ok)
    subprocess.run(  # shift the live file's date in place (no competing _original)
        ["exiftool", "-overwrite_original", "-AllDates+=1", str(live_ok)],
        capture_output=True, text=True, check=True,
    )

    # MISMATCH — backup is a *different* image (stand-in for corruption); kept.
    live_bad = folder / "DSC00002.JPG"
    backup_bad = folder / "DSC00002.JPG_original"
    shutil.copy2(folder / "DSC00003.JPG", backup_bad)

    res = backups.verify_and_remove(card / "DCIM")

    assert str(backup_ok) in res.removed and not backup_ok.exists()   # verified → gone
    assert str(live_bad) in res.mismatched and backup_bad.exists()    # differs → kept
    assert res.reclaimed == 1
