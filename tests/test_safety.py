"""The path-safety guard (G2/G3): only image files under DCIM/<folder>/ may be
removed; management folders and everything else are hard-refused."""

from __future__ import annotations

import pytest

from foto_util.safety import UnsafePathError, assert_deletable


def test_allows_image_under_dcim(card):
    ok = assert_deletable(card / "DCIM" / "100MSDCF" / "DSC00001.ARW",
                          card, strict=True)
    assert ok.name == "DSC00001.ARW"


def test_refuses_management_folders(card):
    targets = [
        "PRIVATE/M4ROOT/MEDIAPRO.XML",
        "AVF_INFO/AVIN0001.BNP",
        "MP_ROOT/100ANV01/MAH00001.MP4",
        "MISC/canary.dat",
    ]
    for rel in targets:
        with pytest.raises(UnsafePathError):
            assert_deletable(card / rel, card, strict=True)


def test_refuses_non_image_even_under_dcim(card, tmp_path):
    stray = card / "DCIM" / "100MSDCF" / "DCIM.DAT"
    stray.write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        assert_deletable(stray, card, strict=True)


def test_refuses_file_directly_in_dcim_root(card):
    # Must live in a DCIM *sub*folder, not DCIM/ itself.
    loose = card / "DCIM" / "DSC09999.JPG"
    loose.write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        assert_deletable(loose, card, strict=True)


def test_refuses_path_outside_root(card, tmp_path):
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        assert_deletable(outside, card, strict=True)


def test_nonstrict_allows_plain_folder_image(tmp_path):
    folder = tmp_path / "loose"
    folder.mkdir()
    img = folder / "snap.jpg"
    img.write_bytes(b"x")
    assert assert_deletable(img, folder, strict=False).name == "snap.jpg"
    # ...but management names are refused even in non-strict mode.
    mgmt = folder / "PRIVATE" / "x.jpg"
    mgmt.parent.mkdir()
    mgmt.write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        assert_deletable(mgmt, folder, strict=False)
