"""The cull loop end-to-end: B / J / X / U on a real fixture card, plus resume."""

from __future__ import annotations

from foto_util import appdir, indexer, volume
from foto_util.controller import Session
from foto_util.hashing import hash_file
from foto_util.model import Decision


def make_session(card, store) -> Session:
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    root = volume.card_root_for(card / "DCIM")
    return Session(store, card_root=root, volume_id=vol, strict=volume.is_card(root))


def seek(session: Session, needle: str) -> None:
    """Point the cursor at the shot whose jpg/raw path contains ``needle``."""
    for i, s in enumerate(session.shots):
        if (s.jpg_path and needle in s.jpg_path) or (s.raw_path and needle in s.raw_path):
            session.index = i
            return
    raise AssertionError(f"no shot matching {needle}")


def test_current_group_shots_scopes_to_cursor_group(card, store):
    """The filmstrip seam returns just the cursor's group (and the cursor's index
    within it), and re-scopes when the cursor moves to another group."""
    s = make_session(card, store)
    # opening burst: three frames share one time-gap group
    seek(s, "100MSDCF/DSC00001.")
    group, idx = s.current_group_shots()
    assert len(group) == 3
    assert all(g.group_id == s.current.group_id for g in group)
    assert group[idx].hash == s.current.hash
    # a later, separate scene scopes to its own (smaller) set
    seek(s, "DSC00020.")
    group2, _ = s.current_group_shots()
    assert len(group2) == 1
    assert group2[0].group_id != group[0].group_id


def test_change_decision_reconciles_files(card, store):
    """Re-deciding a shot restores trashed files first, then applies the new
    decision — so you can go back and press a different key to change your mind."""
    s = make_session(card, store)
    jpg = card / "DCIM" / "100MSDCF" / "DSC00020.JPG"
    raw = card / "DCIM" / "100MSDCF" / "DSC00020.ARW"
    jhash, rhash = hash_file(jpg), hash_file(raw)

    # 1 (keep JPEG): RAW dropped to trash
    seek(s, "DSC00020.")
    s.decide(Decision.KEEP_JPG, advance=False)
    assert jpg.exists() and not raw.exists()
    row = store.get(s.current.hash)
    assert row.decision is Decision.KEEP_JPG and row.trash_raw

    # change to 2 (keep both): RAW must come back, pointers cleared
    s.decide(Decision.KEEP_BOTH, advance=False)
    assert jpg.exists() and raw.exists()
    assert hash_file(raw) == rhash                      # restored exactly
    row = store.get(s.current.hash)
    assert row.decision is Decision.KEEP_BOTH
    assert row.trash_jpg is None and row.trash_raw is None

    # change to 3 (delete): both dropped — no error from already-moved files
    s.decide(Decision.REJECT, advance=False)
    assert not jpg.exists() and not raw.exists()

    # change back to 1 (keep JPEG): JPEG restored, RAW stays dropped
    s.decide(Decision.KEEP_JPG, advance=False)
    assert jpg.exists() and not raw.exists()
    assert hash_file(jpg) == jhash
    row = store.get(s.current.hash)
    assert row.decision is Decision.KEEP_JPG


def test_reject_moves_both_and_advances(card, store):
    s = make_session(card, store)
    seek(s, "DSC00001.")
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    raw = card / "DCIM" / "100MSDCF" / "DSC00001.ARW"
    assert jpg.exists() and raw.exists()

    s.decide(Decision.REJECT)

    assert not jpg.exists() and not raw.exists()       # both moved off card
    # decision recorded with trash pointers
    decided = next(x for x in store.shots() if x.jpg_path and "DSC00001.JPG" in x.jpg_path
                   and "100MSDCF" in x.jpg_path)
    assert decided.decision is Decision.REJECT
    assert decided.trash_jpg and decided.trash_raw
    # advanced to the next undecided shot
    assert s.current.jpg_path.endswith("DSC00002.JPG")


def test_keep_jpg_drops_raw_keeps_jpeg(card, store):
    s = make_session(card, store)
    seek(s, "DSC00020.")
    jpg = card / "DCIM" / "100MSDCF" / "DSC00020.JPG"
    raw = card / "DCIM" / "100MSDCF" / "DSC00020.ARW"

    s.decide(Decision.KEEP_JPG)

    assert jpg.exists()            # JPEG kept
    assert not raw.exists()        # RAW dropped


def test_keep_jpg_errors_on_orphan_jpeg(card, store):
    import pytest

    from foto_util.controller import OrphanDecisionError

    s = make_session(card, store)
    seek(s, "DSC00010.")           # orphan JPEG (no RAW)
    jpg = card / "DCIM" / "100MSDCF" / "DSC00010.JPG"

    with pytest.raises(OrphanDecisionError, match="JPEG-only"):
        s.decide(Decision.KEEP_JPG)

    assert jpg.exists()            # untouched, and no decision recorded
    assert store.get(s.current.hash).decision is None


def test_keep_jpg_errors_on_orphan_raw(card, store):
    import pytest

    from foto_util.controller import OrphanDecisionError

    s = make_session(card, store)
    seek(s, "DSC00011.")           # orphan RAW (no JPEG)
    raw = card / "DCIM" / "100MSDCF" / "DSC00011.ARW"

    with pytest.raises(OrphanDecisionError, match="RAW-only"):
        s.decide(Decision.KEEP_JPG)

    assert raw.exists()
    assert store.get(s.current.hash).decision is None


def test_keep_both_and_delete_work_on_orphans(card, store):
    """Orphans can still be kept (2) or deleted (3) — only keep-JPEG (1) errors."""
    s = make_session(card, store)
    ojpg = card / "DCIM" / "100MSDCF" / "DSC00010.JPG"
    oraw = card / "DCIM" / "100MSDCF" / "DSC00011.ARW"

    seek(s, "DSC00010.")           # keep the orphan JPEG
    s.decide(Decision.KEEP_BOTH, advance=False)
    assert ojpg.exists() and store.get(s.current.hash).decision is Decision.KEEP_BOTH

    seek(s, "DSC00011.")           # delete the orphan RAW
    s.decide(Decision.REJECT, advance=False)
    assert not oraw.exists() and store.get(s.current.hash).decision is Decision.REJECT


def test_undo_restores_rejected_files(card, store):
    s = make_session(card, store)
    seek(s, "DSC00001.")
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    raw = card / "DCIM" / "100MSDCF" / "DSC00001.ARW"
    jhash, rhash = hash_file(jpg), hash_file(raw)

    s.decide(Decision.REJECT)
    assert not jpg.exists() and not raw.exists()

    assert s.undo_last() is True

    assert jpg.exists() and raw.exists()
    assert hash_file(jpg) == jhash and hash_file(raw) == rhash   # exact restore
    restored = next(x for x in store.shots()
                    if x.jpg_path and x.jpg_path.endswith("100MSDCF/DSC00001.JPG"))
    assert restored.decision is None


def test_undo_survives_app_restart(card, store):
    """Root cause (a): undo is DB-derived, so a brand-new session (the app closed
    and reopened, no in-memory undo stack) can still recover a prior decision."""
    s = make_session(card, store)
    seek(s, "DSC00001.")
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    raw = card / "DCIM" / "100MSDCF" / "DSC00001.ARW"
    jhash, rhash = hash_file(jpg), hash_file(raw)
    s.decide(Decision.REJECT)
    assert not jpg.exists() and not raw.exists()

    # Fresh session over the same store = a restart.
    s2 = Session(store, card_root=volume.card_root_for(card / "DCIM"),
                 volume_id=volume.volume_id_for(card), strict=True)
    assert s2.undo_last() is True
    assert jpg.exists() and raw.exists()
    assert hash_file(jpg) == jhash and hash_file(raw) == rhash   # exact restore


def test_undo_after_rescan_restores_raw(card, store):
    """Root cause (b): after keep-JPEG a rescan pairs only the JPEG, but the
    upsert must keep raw_path (the restore destination) while trash_raw is live —
    so undo can still put the RAW back."""
    s = make_session(card, store)
    vol = volume.volume_id_for(card)
    raw = card / "DCIM" / "100MSDCF" / "DSC00020.ARW"
    rhash = hash_file(raw)
    seek(s, "DSC00020.")
    h = s.current.hash
    s.decide(Decision.KEEP_JPG, advance=False)
    assert not raw.exists()

    indexer.scan(card / "DCIM", store, vol)          # RAW gone → rescan finds JPEG only
    assert store.get(h).raw_path is not None          # destination preserved

    s.reload()
    assert s.undo_last() is True
    assert raw.exists() and hash_file(raw) == rhash


def test_recover_orphaned_trash_after_db_cleared(card, store):
    """Root cause (c): a metadata clear strands trashed files (their rows, the
    only DB link, are gone). The recover walk rebuilds them straight from the
    trash tree back onto the card."""
    s = make_session(card, store)
    j1 = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    r1 = card / "DCIM" / "100MSDCF" / "DSC00001.ARW"
    r20 = card / "DCIM" / "100MSDCF" / "DSC00020.ARW"
    j1h, r1h, r20h = hash_file(j1), hash_file(r1), hash_file(r20)

    seek(s, "DSC00001."); s.decide(Decision.REJECT, advance=False)    # both → trash
    seek(s, "DSC00020."); s.decide(Decision.KEEP_JPG, advance=False)  # RAW → trash
    assert not j1.exists() and not r1.exists() and not r20.exists()

    store.clear_all()                                # strand the trash (rows gone)

    restored, errors = s.recover_orphaned_trash()
    assert errors == [] and restored == 3
    assert j1.exists() and r1.exists() and r20.exists()
    assert hash_file(j1) == j1h and hash_file(r1) == r1h and hash_file(r20) == r20h


def test_resume_skips_decided_shots(card, store):
    s = make_session(card, store)
    seek(s, "DSC00001.")
    s.decide(Decision.KEEP_BOTH)

    # Re-open the same card (resume): a fresh session must skip the decided shot.
    resumed = Session(
        store,
        card_root=volume.card_root_for(card / "DCIM"),
        volume_id=volume.volume_id_for(card),
        strict=True,
    )
    assert resumed.current.decision is None
    assert not resumed.current.jpg_path.endswith("100MSDCF/DSC00001.JPG")
