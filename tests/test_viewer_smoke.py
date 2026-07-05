"""Headless (offscreen) smoke test for the viewer.

Runs the real Qt window with the ``offscreen`` platform plugin — no display
needed — and drives the same ``handle_action`` entry point the keyboard uses, so
the key→action→file-op wiring is verified end to end.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from foto_util import indexer, volume
from foto_util.controller import Session
from foto_util.hashing import hash_file


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(card, store):
    from foto_util.viewer import MainWindow

    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    session = Session(store, card_root=card, volume_id=vol, strict=True)
    win = MainWindow(session, card / "DCIM", start_scan=False)
    win.show()  # realize the window (offscreen) so overlay visibility is effective
    return win, session


def _seek(session, needle):
    for i, s in enumerate(session.shots):
        if (s.jpg_path and needle in s.jpg_path) or (s.raw_path and needle in s.raw_path):
            session.index = i
            return
    raise AssertionError(needle)


def test_keep_both_status_names_what_actually_exists():
    """The 'keep both' status flash must state exactly what was kept — naming the
    single file on an orphan, not implying a RAW+JPEG pair that doesn't exist."""
    from foto_util.model import Decision, Shot
    from foto_util.viewer import _decision_status

    pair = Shot(hash="a", jpg_path="/c/DSC1.JPG", raw_path="/c/DSC1.ARW",
                decision=Decision.KEEP_BOTH)
    jpg_only = Shot(hash="b", jpg_path="/c/DSC2.JPG", raw_path=None,
                    decision=Decision.KEEP_BOTH)
    raw_only = Shot(hash="c", jpg_path=None, raw_path="/c/DSC3.ARW",
                    decision=Decision.KEEP_BOTH)
    assert _decision_status(pair)[0] == "Kept RAW + JPEG"
    assert _decision_status(jpg_only)[0] == "Kept JPEG"
    assert _decision_status(raw_only)[0] == "Kept RAW"


def test_window_renders_first_shot(card, store, qapp):
    win, session = _make_window(card, store)
    assert session.current is not None
    assert win.lbl_name.text().strip()        # top bar filename populated
    assert win.lbl_pos.text().strip()         # status position populated


def test_real_key_dispatch_navigates(card, store, qapp):
    """Dispatch ACTUAL key events through Qt's focus system (not handle_action),
    so a regression where a child widget eats arrow keys / Space is caught."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    # the image view must never hold focus, or it swallows arrows + Space
    assert win.image.focusPolicy() == Qt.FocusPolicy.NoFocus

    start = session.index
    QTest.keyClick(win, Qt.Key.Key_Right)     # next
    assert session.index == start + 1
    QTest.keyClick(win, Qt.Key.Key_Space)     # next (Space must not scroll)
    assert session.index == start + 2
    QTest.keyClick(win, Qt.Key.Key_Left)      # previous
    assert session.index == start + 1


def test_real_key_dispatch_decides(card, store, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from foto_util.model import Decision

    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    _seek(session, "DSC00020.")
    win._render()
    target = session.current.hash

    QTest.keyClick(win, Qt.Key.Key_2)         # 2 = keep both, via a real keystroke
    assert store.get(target).decision is Decision.KEEP_BOTH


def test_closing_during_scan_does_not_leave_a_running_thread(card, store, qapp, monkeypatch):
    """Regression: closing the window mid-scan must stop the ScanWorker, not
    destroy a running QThread (which aborts the process, exit 134)."""
    import time

    from foto_util import indexer, volume
    from foto_util.controller import Session
    from foto_util.viewer import MainWindow

    def slow_scan(root, st, vol, *, gap_s=0.0, progress=None, should_stop=None):
        while should_stop is None or not should_stop():  # block until asked to stop
            time.sleep(0.01)
        return 0

    monkeypatch.setattr(indexer, "scan", slow_scan)

    vol = volume.volume_id_for(card)
    session = Session(store, card_root=card, volume_id=vol, strict=True)
    win = MainWindow(session, card / "DCIM", start_scan=False)
    win.show()
    win._start_scan()
    assert win._worker is not None and win._worker.isRunning()

    win.close()  # fires closeEvent -> _stop_workers (stop + join)
    assert win._worker is None  # cleanly stopped and cleared


def test_number_decision_keys(card, store, qapp):
    """1 = keep JPEG, 2 = keep both, 3 = delete. Old letter keys are unmapped."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from foto_util.model import Decision

    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()

    cases = [
        ("DSC00001.", Qt.Key.Key_1, Decision.KEEP_JPG),
        ("DSC00020.", Qt.Key.Key_2, Decision.KEEP_BOTH),
        ("DSC00002.", Qt.Key.Key_3, Decision.REJECT),
    ]
    for needle, key, expected in cases:
        win._cancel_pending_advance()   # we manually seek, so drop the soft-advance
        _seek(session, needle)
        win._render()
        target = session.current.hash
        QTest.keyClick(win, key)
        assert store.get(target).decision is expected

    # the old letter keys must do nothing now
    win._cancel_pending_advance()
    _seek(session, "DSC00003.")
    win._render()
    target = session.current.hash
    for old in (Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D, Qt.Key.Key_B,
                Qt.Key.Key_J, Qt.Key.Key_X, Qt.Key.Key_Z):
        QTest.keyClick(win, old)
    assert store.get(target).decision is None


def test_keep_jpeg_on_orphan_flashes_error(card, store, qapp):
    """Pressing 1 (keep JPEG) on an orphan shows an error status and records
    no decision — the file is never touched."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    _seek(session, "DSC00011.")        # orphan RAW
    win._render()
    raw = card / "DCIM" / "100MSDCF" / "DSC00011.ARW"

    QTest.keyClick(win, Qt.Key.Key_1)

    assert raw.exists()                                  # untouched
    assert store.get(session.current.hash).decision is None
    assert win._toast.isVisible()                        # error message flashed
    assert "RAW-only" in win._toast.text()


def test_decision_pauses_then_advances_and_updates_badge(card, store, qapp):
    """After a decision the cursor stays (badge updates, status flashes); the
    soft auto-advance moves to the next undecided shot when the timer fires."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    _seek(session, "DSC00020.")        # a RAW+JPEG pair
    win._render()
    decided_idx = session.index

    QTest.keyClick(win, Qt.Key.Key_1)  # keep JPEG (drops RAW)

    # cursor has NOT advanced yet; badge now reflects the kept state
    assert session.index == decided_idx
    assert win.lbl_badge.text() == "JPEG only"
    assert win._toast.isVisible()      # status message flashed

    win._do_pending_advance()          # fire the soft-advance
    assert session.index != decided_idx
    assert not session.shots[session.index].is_decided


def test_loupe_zooms_toward_cursor_and_wheel_zooms(card, store, qapp, tmp_path):
    """Z enters a magnified 100% view (real photos are bigger than the window);
    the wheel keeps zooming. Uses a large image — the actual use case."""
    from PIL import Image

    big = tmp_path / "big.jpg"
    Image.new("RGB", (3000, 2000), (120, 90, 60)).save(big)

    win, session = _make_window(card, store)
    win.resize(1000, 700)
    view = win.image
    view.show_path(str(big))                            # load a >viewport image

    assert view._loupe is False
    fit_scale = view.transform().m11()
    assert fit_scale < 1.0                              # fit shrinks a 3000px image
    view.toggle_loupe()
    assert view._loupe is True
    assert view.transform().m11() > fit_scale           # zoomed in toward 100%

    before = view.transform().m11()
    view.zoom_by(1.18)
    assert view.transform().m11() > before              # wheel zooms further
    view.toggle_loupe()                                 # back to fit
    assert view._loupe is False
    assert abs(view.transform().m11() - fit_scale) < 1e-6


def test_scan_refresh_preserves_loupe_zoom(card, store, qapp):
    """A background-scan refresh repaints the view for minutes while you cull.
    It must NOT reload the current image when the displayed shot is unchanged —
    otherwise it resets the loupe out from under you (and re-decodes the JPEG)."""
    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    session.index = 0
    win._render()

    win.image.toggle_loupe()                       # inspect focus on the current shot
    assert win.image._loupe is True
    zoomed = win.image.transform().m11()

    win.refresh()                                  # simulate a scan-progress tick
    assert win.image._loupe is True                # still zoomed: image was not reloaded
    assert abs(win.image.transform().m11() - zoomed) < 1e-6


def test_rotate_key_rotates_the_view_only(card, store, qapp, tmp_path):
    """R rotates the displayed pixmap 90° (a landscape becomes portrait); it's a
    view transform — the file is never touched."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PIL import Image

    wide = tmp_path / "wide.jpg"
    Image.new("RGB", (400, 200), (90, 120, 60)).save(wide)   # landscape source
    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    win.image.show_path(str(wide))

    assert win.image._rotation == 0
    assert win.image._item.pixmap().width() == 400          # landscape
    QTest.keyClick(win, Qt.Key.Key_R)
    assert win.image._rotation == 90
    assert win.image._item.pixmap().width() == 200          # now portrait (w/h swapped)


def test_fullscreen_toggle(card, store, qapp):
    """F toggles borderless fullscreen ↔ a normal resizable window."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    win, _ = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    assert not win.isFullScreen()
    QTest.keyClick(win, Qt.Key.Key_F)
    assert win.isFullScreen()
    QTest.keyClick(win, Qt.Key.Key_F)
    assert not win.isFullScreen()


def test_real_key_dispatch_groups_loupe_help(card, store, qapp):
    """Bracket group-jump, 4 loupe, and ? / Esc hints — all via real keys."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()

    session.index = 0
    win._render()
    g0 = session.shots[0].group_id
    QTest.keyClick(win, Qt.Key.Key_BracketRight)
    assert session.shots[session.index].group_id != g0
    QTest.keyClick(win, Qt.Key.Key_BracketLeft)
    assert session.shots[session.index].group_id == g0

    assert win.image._loupe is False
    QTest.keyClick(win, Qt.Key.Key_4)          # 4 = zoom/loupe
    assert win.image._loupe is True
    QTest.keyClick(win, Qt.Key.Key_4)
    assert win.image._loupe is False

    QTest.keyClick(win, Qt.Key.Key_Question)   # toggle far-left hints
    assert win._help.isVisible()
    QTest.keyClick(win, Qt.Key.Key_H)          # H also toggles
    assert not win._help.isVisible()


def test_real_key_dispatch_reject_then_undo(card, store, qapp):
    """3 removes the pair from the card; U restores it."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from foto_util.model import Decision

    win, session = _make_window(card, store)
    win.activateWindow()
    win.setFocus()
    _seek(session, "DSC00001.")
    win._render()
    target = session.current.hash
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    h = hash_file(jpg)

    QTest.keyClick(win, Qt.Key.Key_3)          # 3 = delete both
    assert store.get(target).decision is Decision.REJECT
    assert not jpg.exists()

    QTest.keyClick(win, Qt.Key.Key_U)          # U = undo, cancels pending advance
    assert jpg.exists() and hash_file(jpg) == h
    assert store.get(target).decision is None


def test_reject_action_moves_then_undo_restores(card, store, qapp):
    win, session = _make_window(card, store)
    _seek(session, "DSC00001.")
    win._render()
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    raw = card / "DCIM" / "100MSDCF" / "DSC00001.ARW"
    h = hash_file(jpg)

    win.handle_action("reject")
    assert not jpg.exists() and not raw.exists()

    win.handle_action("undo")
    assert jpg.exists() and raw.exists()
    assert hash_file(jpg) == h


def test_keep_jpg_action_drops_raw(card, store, qapp):
    win, session = _make_window(card, store)
    _seek(session, "DSC00020.")
    win._render()
    jpg = card / "DCIM" / "100MSDCF" / "DSC00020.JPG"
    raw = card / "DCIM" / "100MSDCF" / "DSC00020.ARW"

    win.handle_action("keep_jpg")
    assert jpg.exists() and not raw.exists()


def test_overlay_and_loupe_actions_do_not_crash(card, store, qapp):
    win, session = _make_window(card, store)
    win.handle_action("loupe")                # toggle 100%
    win.handle_action("loupe")                # back to fit
    win.handle_action("help")
    assert win._help.isVisible()


def test_dropped_files_recoverable_until_empty_trash(card, store, qapp):
    """Deletes stay in the trash (undoable) until Empty trash permanently frees
    them — and then undo is no longer offered for them."""
    from foto_util.model import Decision

    win, session = _make_window(card, store)
    _seek(session, "DSC00001.")
    win._render()
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    shot = session.current
    win.handle_action("reject")
    assert not jpg.exists()
    trash_jpg = Path(store.get(shot.hash).trash_jpg)
    assert trash_jpg.exists()                 # recoverable copy lives in trash

    # empty the trash: permanent removal + trash pointers dropped (commit boundary)
    from foto_util import fileops
    fileops.empty_trash(session.trash_dir)
    store.clear_trash_pointers(session.volume_id)
    assert not trash_jpg.exists()
    assert session.undo_last() is False       # nothing left to undo
    assert store.get(shot.hash).decision is Decision.REJECT  # decision stands


def test_clock_fix_live_through_the_viewer(card, store, qapp, monkeypatch):
    """Drive the real Tools→Fix clock offset handler headlessly: EXIF shifts,
    the _original backup is kept, and the decision survives the re-key."""
    from datetime import timedelta

    from foto_util import meta
    from foto_util.model import Decision
    from foto_util.viewer import QInputDialog, QMessageBox

    if not meta.exiftool_available():
        import pytest

        pytest.skip("exiftool not installed")

    win, session = _make_window(card, store)
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    _seek(session, "100MSDCF/DSC00001.")
    win._render()

    # Give the reference shot a decision so we can prove it survives the re-key.
    h_before = session.current.hash
    store.set_decision(h_before, Decision.KEEP_BOTH)
    session.reload()
    _seek(session, "100MSDCF/DSC00001.")  # make it current again (the reference)

    recorded = meta.read_capture_time(jpg).when
    true_when = (recorded + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: (true_when, True)))
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    win._do_clock_fix()
    assert win._clock_worker is not None           # runs on a background thread now
    win._clock_worker.wait(20000)                  # shift + re-key + ._ clean
    qapp.processEvents()                           # deliver result_ready → _on_clockfix_done

    # EXIF moved by +1h, and the safety backup is on the card (G9).
    assert (meta.read_capture_time(jpg).when - recorded) == timedelta(hours=1)
    assert (jpg.parent / (jpg.name + "_original")).exists()
    # The decision survived: the row was re-keyed to the new content hash.
    row = next(s for s in store.shots() if s.jpg_path and s.jpg_path.endswith("100MSDCF/DSC00001.JPG"))
    assert row.hash != h_before
    assert row.decision is Decision.KEEP_BOTH


def test_menus_are_wired(card, store, qapp):
    from foto_util import meta

    win, _ = _make_window(card, store)
    menus = win.menus
    assert "File" in menus and "Database" in menus and "Tools" in menus

    file_actions = [a.text() for a in menus["File"].actions() if a.text()]
    assert "Open card or folder" in file_actions
    assert "Empty trash (permanently delete)" in file_actions

    db_actions = [a.text() for a in menus["Database"].actions() if a.text()]
    assert "Prune stale rows" in db_actions
    assert "Forget this card" in db_actions
    assert "Clear all" in db_actions

    tools = {a.text(): a for a in menus["Tools"].actions() if a.text()}
    clock = tools["Fix clock offset"]
    # the clock-fix item is enabled exactly when exiftool is installed
    assert clock.isEnabled() == meta.exiftool_available()
    # the verify-backups item is present and gated on exiftool the same way
    assert "Verify and clear clock-fix backups" in tools
    assert tools["Verify and clear clock-fix backups"].isEnabled() == meta.exiftool_available()


def test_storage_dialog_lists_trash_and_gates_empty(card, store, qapp):
    """The Storage dialog lists every per-card trash dir with its size, marks the
    open card, and only enables Empty for it (and only when it has files)."""
    from foto_util.model import Decision
    from foto_util.viewer import StorageDialog

    win, session = _make_window(card, store)

    # trash empty → row-less listing, Empty disabled
    dlg = StorageDialog(win)
    assert not dlg.empty_btn.isEnabled()

    # stage something into this card's trash, plus a fake other-card trash dir
    _seek(session, "DSC00020.")
    session.decide(Decision.REJECT, advance=False)
    other = session.trash_dir.parent / "uuid_OTHER-CARD"
    (other / "DCIM" / "100MSDCF").mkdir(parents=True)
    (other / "DCIM" / "100MSDCF" / "DSC00001.JPG").write_bytes(b"x" * 100)

    dlg = StorageDialog(win)
    labels = [dlg.list.item(i).text() for i in range(dlg.list.count())]
    assert any("the card open now" in t for t in labels)       # current card marked
    assert any("uuid_OTHER-CARD" in t for t in labels)         # unmounted card visible
    assert dlg.empty_btn.isEnabled()                           # current card has files
    assert "Total:" in dlg.lbl_total.text()


def test_blocking_progress_dialog_ignores_close_until_allowed(qapp):
    """The clock-fix dialog must swallow Esc/close while work runs — dropping its
    modal barrier would let culling race the exiftool rewrite."""
    from foto_util.viewer import BlockingProgressDialog

    dlg = BlockingProgressDialog("working…", "", 0, 10)
    dlg.show()
    dlg.reject()                     # Esc lands here → ignored
    assert dlg.isVisible()
    dlg.close()                      # window close button → ignored
    assert dlg.isVisible()
    dlg.allow_close()
    dlg.close()                      # completion handler path
    assert not dlg.isVisible()


def test_rejected_shot_previews_from_its_trash_copy(card, store, qapp):
    """Navigating back to a 3-deleted (red) shot must still show the photo —
    rendered from its byte-verified trash copy — so deciding whether to
    un-delete it is never done against a blank frame. Restoring must switch
    the view back to the card file."""
    win, session = _make_window(card, store)
    _seek(session, "DSC00020.")
    win._render()
    assert win._shown_path == session.current.jpg_path   # normal: card file

    win.handle_action("reject")
    win._pending_timer.stop()
    shot = store.get(session.current.hash)
    assert shot.trash_jpg and not Path(shot.jpg_path).exists()

    # navigate away and back — the red shot shows its trash copy, decoded
    win.handle_action("skip")
    win.handle_action("prev")
    assert win._shown_path == shot.trash_jpg
    assert win.image._base_pix is not None               # a real image, not blank

    # un-delete: the view returns to the restored card file
    win.handle_action("keep_both")
    assert win._shown_path == session.current.jpg_path
    assert win.image._base_pix is not None


def test_restoring_redecide_announces_itself(card, store, qapp):
    """Pressing 2 on a trashed (red) shot restores the files — and the status
    flash must say so, so a revival is never mistaken for a phantom entry."""
    win, session = _make_window(card, store)
    _seek(session, "DSC00020.")
    win.handle_action("reject")
    win._pending_timer.stop()          # stay on the shot (no auto-advance)
    win.handle_action("keep_both")
    assert "Restored from trash" in win._toast.text()
    assert "Kept RAW + JPEG" in win._toast.text()
    # and the files really are back
    assert (card / "DCIM" / "100MSDCF" / "DSC00020.JPG").exists()
    assert (card / "DCIM" / "100MSDCF" / "DSC00020.ARW").exists()

    # a plain first-time decision must NOT claim a restore
    win._pending_timer.stop()
    _seek(session, "DSC00001.")
    win.handle_action("keep_both")
    assert "Restored" not in win._toast.text()
