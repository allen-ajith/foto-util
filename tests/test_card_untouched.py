"""Whole-card safety gates (design doc §10): scanning writes nothing, the
management folders are byte-identical after a full session, and no file is ever
renamed (the surviving names are a subset of the originals — never new ones)."""

from __future__ import annotations

from foto_util import appdir, indexer, volume
from foto_util.controller import Session
from foto_util.model import Decision
from tests.conftest import snapshot_tree


def test_scan_writes_nothing_to_the_card(card, store):
    before = snapshot_tree(card)
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    after = snapshot_tree(card)
    # size, mtime, and content of every file unchanged; no files added/removed.
    assert before == after


def test_management_folders_untouched_after_full_session(card, store):
    mgmt_dirs = ["PRIVATE", "AVF_INFO", "MP_ROOT", "MISC"]
    before = {d: snapshot_tree(card / d) for d in mgmt_dirs}

    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    s = Session(store, card_root=card, volume_id=vol, strict=True)
    # Reject everything — the most destructive run possible.
    while s.current is not None and not s.current.is_decided:
        s.decide(Decision.REJECT)

    after = {d: snapshot_tree(card / d) for d in mgmt_dirs}
    assert before == after


def test_no_file_is_ever_renamed(card, store):
    original_names = {p.name for p in (card / "DCIM").rglob("*") if p.is_file()}

    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    s = Session(store, card_root=card, volume_id=vol, strict=True)
    # A representative mix of decisions across the card.
    decisions = [Decision.KEEP_BOTH, Decision.KEEP_JPG, Decision.REJECT]
    i = 0
    while s.current is not None and not s.current.is_decided:
        s.decide(decisions[i % len(decisions)])
        i += 1

    remaining = {p.name for p in (card / "DCIM").rglob("*") if p.is_file()}
    # Nothing new appeared (no renames, no app artifacts); files only disappear.
    assert remaining <= original_names


def test_app_artifacts_never_land_on_the_card(card, store, app_dir):
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    s = Session(store, card_root=card, volume_id=vol, strict=True)
    seek_done = False
    while s.current is not None and not s.current.is_decided:
        s.decide(Decision.REJECT)
        seek_done = True
    assert seek_done

    # DB, trash, and any app file live under the app dir, never on the card.
    assert appdir.db_path().exists()
    assert str(app_dir) in str(appdir.db_path())
    card_files = {p.name for p in card.rglob("*") if p.is_file()}
    assert "cull.db" not in card_files
    assert not any(name.startswith(".") for name in card_files)  # no .DS_Store etc.
