"""Source discovery (volume.list_sources) and the startup picker dialog."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from foto_util import volume


def test_list_sources_flags_cards_first(monkeypatch, tmp_path, card):
    # one card (has DCIM) and one plain volume
    plain = tmp_path / "USB_STICK"
    plain.mkdir()
    monkeypatch.setattr(volume, "_mounted_volume_dirs", lambda: [plain, card])

    sources = volume.list_sources()
    assert [s.name for s in sources] == [card.name, "USB_STICK"]  # card first
    cardsrc = sources[0]
    assert cardsrc.is_card is True
    assert cardsrc.dcim == card / "DCIM"
    assert sources[1].is_card is False and sources[1].dcim is None


def test_list_sources_empty(monkeypatch):
    monkeypatch.setattr(volume, "_mounted_volume_dirs", lambda: [])
    assert volume.list_sources() == []


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_source_dialog_lists_and_selects_card(monkeypatch, tmp_path, card, qapp):
    from foto_util.viewer import SourceDialog

    plain = tmp_path / "OTHER"
    plain.mkdir()
    monkeypatch.setattr(volume, "_mounted_volume_dirs", lambda: [plain, card])

    dlg = SourceDialog()
    assert dlg.list.count() == 2
    # first row is the card; selecting it yields the DCIM path
    dlg.list.setCurrentRow(0)
    dlg._accept_selected()
    assert dlg.selected_path == card / "DCIM"


def test_source_dialog_browse(monkeypatch, tmp_path, qapp):
    from foto_util.viewer import QFileDialog, SourceDialog

    monkeypatch.setattr(volume, "_mounted_volume_dirs", lambda: [])
    folder = tmp_path / "loose_photos"
    folder.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(folder)))

    dlg = SourceDialog()
    dlg._browse()
    assert dlg.selected_path == folder


def test_open_source_switches_session(monkeypatch, tmp_path, card, store, qapp):
    """File → Open swaps the window to a new folder without restarting."""
    from foto_util import indexer
    from foto_util.controller import Session
    from foto_util.viewer import MainWindow

    # start on a plain folder with one image
    first = tmp_path / "first"
    (first).mkdir()
    from PIL import Image
    Image.new("RGB", (32, 24), (10, 20, 30)).save(first / "a.jpg")

    vol = volume.volume_id_for(first)
    indexer.scan(first, store, vol)
    session = Session(store, card_root=first, volume_id=vol, strict=False)
    win = MainWindow(session, first, start_scan=False)
    win.show()
    assert win.session.volume_id == vol

    # switch to the real fixture card (open_source kicks off a background scan)
    win.open_source(card / "DCIM")
    assert win.scan_target == card / "DCIM"
    assert win.strip.session is win.session
    # the new session targets the card volume
    new_vol = volume.volume_id_for(card)
    assert win.session.volume_id == new_vol
    # let the background scan finish so the thread doesn't dangle
    if getattr(win, "_worker", None) is not None:
        win._worker.wait(5000)
