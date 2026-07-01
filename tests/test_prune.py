"""Prune database (G11): metadata-only, scoped to the mounted volume, never
prunes a row whose files still exist on the card or in the trash."""

from __future__ import annotations

from foto_util import fileops, indexer, prune, volume
from foto_util.controller import Session
from foto_util.model import Decision


def _seek(session, needle):
    for i, s in enumerate(session.shots):
        if (s.jpg_path and needle in s.jpg_path) or (s.raw_path and needle in s.raw_path):
            session.index = i
            return
    raise AssertionError(needle)


def test_nothing_stale_when_all_files_present(card, store):
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    assert prune.find_stale(store, vol) == []


def test_file_removed_outside_app_becomes_stale(card, store):
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    (card / "DCIM" / "100MSDCF" / "DSC00010.JPG").unlink()  # orphan jpg, no trash
    stale = prune.find_stale(store, vol)
    assert len(stale) == 1
    assert stale[0].jpg_path.endswith("DSC00010.JPG")


def test_rejected_shot_is_stale_only_after_trash_emptied(card, store):
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    s = Session(store, card_root=card, volume_id=vol, strict=True)
    _seek(s, "DSC00001.")
    s.decide(Decision.REJECT)

    # Files are off the card but live in the trash → undoable → NOT stale.
    assert prune.find_stale(store, vol) == []

    fileops.empty_trash(s.trash_dir)
    stale = prune.find_stale(store, vol)
    assert any("DSC00001.JPG" in (x.jpg_path or "") for x in stale)


def test_prune_is_scoped_to_the_mounted_volume(store):
    """The key G11 guard: rows for a card that isn't connected (files 'missing'
    only because it's unmounted) must never be considered stale."""
    store.upsert_scanned(hash="other", volume_id="OTHER_CARD",
                         jpg_path="/Volumes/gone/DCIM/x.JPG", raw_path=None, group_id=0)
    store.upsert_scanned(hash="mine", volume_id="MINE",
                         jpg_path="/Volumes/gone/DCIM/y.JPG", raw_path=None, group_id=0)

    stale = prune.find_stale(store, "MINE")
    assert [s.hash for s in stale] == ["mine"]   # only the mounted volume
    assert store.get("other") is not None        # other card's row untouched

    assert prune.prune_stale(store, "MINE") == 1
    assert store.get("mine") is None
    assert store.get("other") is not None
