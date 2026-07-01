"""Clock-fix (G9): offset computation always; the exiftool shift when present."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foto_util import meta


def test_compute_offset_round_trips(card):
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    recorded = meta.read_capture_time(jpg).when          # the wrong-clock value
    true_when = recorded + timedelta(days=2922, hours=3) # arbitrary true time

    offset = meta.compute_offset(jpg, true_when)

    assert recorded + timedelta(seconds=offset) == true_when


def test_shift_token_formatting():
    # +1h30m past two days
    sign, token = meta._shift_token(2 * 86400 + 5400)
    assert sign == "+" and token == "0:0:2 1:30:0"
    sign, token = meta._shift_token(-90)
    assert sign == "-" and token == "0:0:0 0:1:30"


@pytest.mark.skipif(not meta.exiftool_available(), reason="exiftool not installed")
def test_shift_all_dates_moves_exif_and_keeps_backup(card):
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    before = meta.read_capture_time(jpg).when

    n = meta.shift_all_dates([jpg], 3600)  # +1 hour

    assert n == 1
    after = meta.read_capture_time(jpg).when
    assert (after - before) == timedelta(hours=1)
    assert (jpg.parent / (jpg.name + "_original")).exists()  # backup kept (G9)


def test_shift_all_dates_raises_without_exiftool(card, monkeypatch):
    monkeypatch.setattr(meta, "exiftool_available", lambda: False)
    with pytest.raises(RuntimeError, match="exiftool not found"):
        meta.shift_all_dates([card / "DCIM" / "100MSDCF" / "DSC00001.JPG"], 3600)


@pytest.mark.skipif(not meta.exiftool_available(), reason="exiftool not installed")
def test_shift_all_dates_reports_progress_in_batches(card):
    jpgs = [card / "DCIM" / "100MSDCF" / f"DSC0000{i}.JPG" for i in (1, 2, 3)]
    seen: list[tuple[int, int]] = []
    n = meta.shift_all_dates(jpgs, 60, batch=2, progress=lambda d, t: seen.append((d, t)))
    assert n == 3
    assert seen == [(2, 3), (3, 3)]   # 3 files, batches of 2 → two callbacks, ending at total


def test_clean_appledouble_removes_only_sidecars(card):
    folder = card / "DCIM" / "100MSDCF"
    (folder / "._DSC00001.JPG").write_bytes(b"junk")
    (folder / "._misc").write_bytes(b"junk")
    real = folder / "DSC00001.JPG"

    n = meta.clean_appledouble(card / "DCIM")

    assert n == 2
    assert not (folder / "._DSC00001.JPG").exists()
    assert real.exists()              # real images are never touched
