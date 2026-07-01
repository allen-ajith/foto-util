"""Store upserts must refresh paths on rescan while preserving decisions and
trash pointers (the resume + undo state)."""

from __future__ import annotations

from foto_util.model import Decision


def test_rescan_refreshes_paths_for_undecided_shot(store):
    """With no trash pointer, a rescan refreshes the paths and group (the normal
    remount case)."""
    store.upsert_scanned(
        hash="abc", volume_id="vol1",
        jpg_path="/card/DCIM/100MSDCF/DSC1.JPG",
        raw_path="/card/DCIM/100MSDCF/DSC1.ARW", group_id=0,
    )
    store.upsert_scanned(
        hash="abc", volume_id="vol1",
        jpg_path="/Volumes/NEW/DCIM/100MSDCF/DSC1.JPG",
        raw_path="/Volumes/NEW/DCIM/100MSDCF/DSC1.ARW", group_id=3,
    )
    shot = store.get("abc")
    assert shot.jpg_path == "/Volumes/NEW/DCIM/100MSDCF/DSC1.JPG"  # refreshed
    assert shot.raw_path == "/Volumes/NEW/DCIM/100MSDCF/DSC1.ARW"  # refreshed
    assert shot.group_id == 3                                      # refreshed


def test_rescan_does_not_null_path_with_pending_trash(store):
    """Root cause (b): after keep-JPEG the RAW is off the card, so a rescan pairs
    only the JPEG (raw_path=None). The upsert must NOT overwrite the stored
    raw_path while trash_raw is live — that path is the restore destination."""
    store.upsert_scanned(
        hash="abc", volume_id="vol1",
        jpg_path="/card/DCIM/100MSDCF/DSC1.JPG",
        raw_path="/card/DCIM/100MSDCF/DSC1.ARW", group_id=0,
    )
    store.set_decision("abc", Decision.KEEP_JPG, trash_raw="/trash/DSC1.ARW")

    # Rescan now finds only the JPEG; the JPEG path may also have moved (remount).
    store.upsert_scanned(
        hash="abc", volume_id="vol1",
        jpg_path="/Volumes/NEW/DCIM/100MSDCF/DSC1.JPG",
        raw_path=None, group_id=3,
    )

    shot = store.get("abc")
    assert shot.jpg_path == "/Volumes/NEW/DCIM/100MSDCF/DSC1.JPG"  # no trash → refreshed
    assert shot.raw_path == "/card/DCIM/100MSDCF/DSC1.ARW"         # trash live → preserved
    assert shot.group_id == 3                                      # refreshed
    assert shot.decision is Decision.KEEP_JPG                      # preserved
    assert shot.trash_raw == "/trash/DSC1.ARW"                     # preserved


def test_clear_decision_wipes_trash_pointers(store):
    store.upsert_scanned(hash="u", volume_id="v", jpg_path="/a.jpg",
                         raw_path="/a.arw", group_id=0)
    store.set_decision("u", Decision.KEEP_JPG, trash_raw="/trash/a.arw")
    store.clear_decision("u")
    shot = store.get("u")
    assert shot.decision is None and shot.decided_at is None
    assert shot.trash_jpg is None and shot.trash_raw is None


def test_last_recoverable_and_pending_count(store):
    """Undo derives from the DB: only decided shots that still have a trash
    pointer are recoverable, and the most recently decided one comes first."""
    for h in ("keepboth", "kept", "rej"):
        store.upsert_scanned(hash=h, volume_id="v", jpg_path=f"/{h}.jpg",
                             raw_path=f"/{h}.arw", group_id=0)
    # keep-both moved no files → no trash pointer → not recoverable / not counted
    store.set_decision("keepboth", Decision.KEEP_BOTH,
                       decided_at="2026-01-01T00:00:03+00:00")
    store.set_decision("kept", Decision.KEEP_JPG, trash_raw="/t/kept.arw",
                       decided_at="2026-01-01T00:00:01+00:00")
    store.set_decision("rej", Decision.REJECT, trash_jpg="/t/rej.jpg",
                       trash_raw="/t/rej.arw", decided_at="2026-01-01T00:00:02+00:00")

    assert store.pending_trash_count("v") == 2
    assert store.last_recoverable("v").hash == "rej"   # latest decided_at wins


def test_clear_trash_pointers_keeps_decision(store):
    """Empty trash: pointers drop (decision is now final) so undo stops offering
    files that no longer exist."""
    store.upsert_scanned(hash="h", volume_id="v", jpg_path="/a.jpg",
                         raw_path="/a.arw", group_id=0)
    store.set_decision("h", Decision.REJECT, trash_jpg="/t/a.jpg", trash_raw="/t/a.arw")
    assert store.clear_trash_pointers("v") == 1
    shot = store.get("h")
    assert shot.decision is Decision.REJECT
    assert shot.trash_jpg is None and shot.trash_raw is None
    assert store.last_recoverable("v") is None


def test_reset_trashed_decisions_undecides(store):
    """After recovering files to the card, the shots that had trash pointers are
    undecided again (a clean slate)."""
    store.upsert_scanned(hash="h", volume_id="v", jpg_path="/a.jpg",
                         raw_path="/a.arw", group_id=0)
    store.set_decision("h", Decision.KEEP_JPG, trash_raw="/t/a.arw")
    assert store.reset_trashed_decisions("v") == 1
    shot = store.get("h")
    assert shot.decision is None and shot.trash_raw is None


def test_counts(store):
    for i in range(5):
        store.upsert_scanned(hash=f"h{i}", volume_id="v", jpg_path=f"/{i}.jpg",
                             raw_path=None, group_id=0)
    store.set_decision("h0", Decision.KEEP_BOTH)
    store.set_decision("h1", Decision.REJECT)
    c = store.counts("v")
    assert c == {"total": 5, "decided": 2, "undecided": 3}


def test_rekey_preserves_decision(store):
    store.upsert_scanned(hash="old", volume_id="v", jpg_path="/a.jpg",
                         raw_path=None, group_id=2)
    store.set_decision("old", Decision.KEEP_BOTH)

    assert store.rekey("old", "new") is True
    assert store.get("old") is None
    new = store.get("new")
    assert new.decision is Decision.KEEP_BOTH
    assert new.group_id == 2


def test_rekey_refuses_when_target_exists(store):
    store.upsert_scanned(hash="a", volume_id="v", jpg_path="/a.jpg",
                         raw_path=None, group_id=0)
    store.upsert_scanned(hash="b", volume_id="v", jpg_path="/b.jpg",
                         raw_path=None, group_id=0)
    assert store.rekey("a", "b") is False
    assert store.get("a") is not None and store.get("b") is not None


def test_delete_volume_and_clear_all(store):
    store.upsert_scanned(hash="a", volume_id="v1", jpg_path="/a", raw_path=None, group_id=0)
    store.upsert_scanned(hash="b", volume_id="v2", jpg_path="/b", raw_path=None, group_id=0)
    assert store.delete_volume("v1") == 1
    assert store.get("a") is None and store.get("b") is not None
    assert store.clear_all() == 1
    assert store.get("b") is None
