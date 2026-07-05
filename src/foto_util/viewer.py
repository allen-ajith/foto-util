"""PySide6 viewer — the single-window, keyboard-first cull loop (design doc §8).

The window is a thin shell over :class:`foto_util.controller.Session`: it renders the
current shot and maps keystrokes to ``Session`` actions. The scan runs on a
background :class:`ScanWorker` thread (its own store connection, per the WAL
design) so the first shots appear without blocking the UI.

The hot path never opens a modal dialog: a decision triggers a brief corner
flash (teal / amber / red) and auto-advances.

Action dispatch goes through :meth:`MainWindow.handle_action`, which the
offscreen smoke test drives directly without synthesising key events.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QKeyEvent, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QDialog,
    QFileDialog,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import backups, fileops, indexer, meta, prune, volume
from .appdir import db_path
from .controller import Session
from .grouping import SCENE_GAP_S
from .model import Decision
from .store import Store

# Visual tokens (design doc §8): dark, flat, fast. No gradients/shadows.
BG = "#0E0E10"
SURFACE = "#171719"
SURFACE_HI = "#1E1E21"        # slightly raised surface (bars)
HAIRLINE = "rgba(255,255,255,0.08)"  # ~8% white hairline borders
TEXT = "#E7E7E5"
TEXT_DIM = "#9A9A97"
TEXT_FAINT = "#6A6A68"
ACCENT_KEEP = "#1D9E75"
ACCENT_JPG = "#BA7517"
ACCENT_REJECT = "#E24B4A"
ACCENT_FOCUS = "#378ADD"
DOT_UNDECIDED = "#3A3A3E"     # filmstrip: not-yet-decided

DECISION_COLOR = {
    Decision.KEEP_BOTH: ACCENT_KEEP,
    Decision.KEEP_JPG: ACCENT_JPG,
    Decision.REJECT: ACCENT_REJECT,
}

# (key, action) rows for the hints panel — rendered as an aligned 2-column table.
HELP_ROWS = [
    ("1", "keep JPEG (drop RAW)"),
    ("2", "keep both"),
    ("3", "delete (drop both)"),
    ("4", "zoom (wheel zooms, drag pans)"),
    ("R", "rotate 90°"),
    ("F", "fullscreen on / off"),
    ("← / →", "previous / next"),
    ("Space", "next"),
    ("U", "undo"),
    ("[ / ]", "previous / next group"),
    ("⌘E", "eject card"),
    ("? / H", "hide hints"),
]


def _hints_html() -> str:
    """The hints as a 2-column HTML table so keys and actions align cleanly
    regardless of how wide each key label is (``1`` vs ``← / →`` vs ``⌘E``)."""
    rows = "".join(
        f"<tr>"
        f"<td style='color:{TEXT}; font-weight:600; text-align:right;"
        f" padding:4px 16px 4px 0; white-space:nowrap'>{k}</td>"
        f"<td style='color:{TEXT_DIM}; padding:4px 0'>{d}</td>"
        f"</tr>"
        for k, d in HELP_ROWS
    )
    return f"<table style='border-collapse:collapse'>{rows}</table>"

# How long to show the decided shot (updated badge + status flash) before the
# cursor auto-advances to the next undecided shot.
ADVANCE_MS = 400


def _decision_status(shot) -> tuple[str, str]:
    """Status-flash text + colour describing what a decision did to ``shot``."""
    d = shot.decision
    if d is Decision.REJECT:
        return ("Deleted → trash", ACCENT_REJECT)
    if d is Decision.KEEP_BOTH:
        # Name exactly what was kept. "Keep both" on an orphan keeps the single
        # file that exists — don't imply a RAW+JPEG pair that isn't there.
        if shot.jpg_path and shot.raw_path:
            return ("Kept RAW + JPEG", ACCENT_KEEP)
        if shot.jpg_path:
            return ("Kept JPEG", ACCENT_KEEP)
        return ("Kept RAW", ACCENT_KEEP)
    if d is Decision.KEEP_JPG:
        if shot.jpg_path and shot.raw_path:
            return ("Kept JPEG · RAW → trash", ACCENT_JPG)
        if shot.jpg_path:
            return ("Kept JPEG", ACCENT_JPG)
        return ("Kept RAW (only file)", TEXT_DIM)
    return ("", TEXT_DIM)


def _badge_for(shot) -> tuple[str, str]:
    """The RAW/JPEG badge text + colour, reflecting what is *kept* once decided."""
    d = shot.decision
    has_jpg = shot.jpg_path is not None
    has_raw = shot.raw_path is not None
    if d is Decision.REJECT:
        return ("Dropped", ACCENT_REJECT)
    if d is Decision.KEEP_JPG and has_jpg:
        return ("JPEG only", ACCENT_JPG)        # RAW was moved to trash
    if has_jpg and has_raw:
        return ("RAW + JPEG", TEXT_DIM)
    if has_jpg:
        return ("JPEG only", ACCENT_JPG)
    return ("RAW only", TEXT_DIM)


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _n(count: int, noun: str, plural: str | None = None) -> str:
    """Counted noun for UI text: '1 file' / '3 files', never 'file(s)'."""
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


class ScanWorker(QThread):
    """Runs the read-only scan off the UI thread, with its own store connection.

    Interruptible: ``stop()`` asks the scan to abort at the next shot, so closing
    the window (or switching cards) mid-scan never destroys a running thread."""

    progressed = Signal(int, int)
    done = Signal(int)
    failed = Signal(str)

    def __init__(self, scan_target: Path, volume_id: str | None, gap_s: float):
        super().__init__()
        self.scan_target = scan_target
        self.volume_id = volume_id
        self.gap_s = gap_s
        self._stop = False

    def run(self) -> None:  # pragma: no cover - exercised via the GUI
        store = Store(db_path())
        try:
            n = indexer.scan(
                self.scan_target, store, self.volume_id, gap_s=self.gap_s,
                progress=lambda d, t: self.progressed.emit(d, t),
                should_stop=lambda: self._stop,
            )
        except Exception as e:
            # Surface the failure instead of dying silently — an uncaught error
            # here would leave the UI stuck showing "scanning…" forever.
            if not self._stop:
                self.failed.emit(str(e))
            return
        finally:
            store.close()
        if not self._stop:          # don't kick off follow-on work during shutdown
            self.done.emit(n)

    def stop(self) -> None:
        self._stop = True


class BackupVerifyWorker(QThread):
    """Verifies clock-fix ``_original`` backups against their live files by
    image-data hash, off the UI thread, deleting the ones that provably match.
    Interruptible at each batch so closing the window never strands the thread."""

    progressed = Signal(int, int)
    result_ready = Signal(object)  # backups.VerifyResult

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self._stop = False

    def run(self) -> None:  # pragma: no cover - exercised via the GUI
        # The progress emission is gated on the stop flag too: after a cancel,
        # the in-flight batch still finishes and reports — and a setValue() on a
        # canceled QProgressDialog would *re-show* it as a frozen zombie.
        res = backups.verify_and_remove(
            self.root,
            batch=16,
            progress=lambda d, t: None if self._stop else self.progressed.emit(d, t),
            should_stop=lambda: self._stop,
        )
        if not self._stop:   # don't deliver a result into a closing window
            self.result_ready.emit(res)

    def stop(self) -> None:
        self._stop = True


class ClockFixWorker(QThread):
    """Runs the clock-fix off the UI thread: shift dates (batched, with progress),
    re-key the rows so decisions survive the byte change, then clean the ``._``
    sidecars the shift created. One continuous progress bar covers the shift and
    re-key phases.

    Deliberately *not* interruptible: a clock-fix is all-or-nothing (stopping
    between batches would leave the card half-corrected, and stopping mid-rekey
    would orphan decisions whose files were already shifted). The window refuses
    to close while this runs; a hung exiftool is bounded by meta's timeout."""

    progressed = Signal(int, int)
    result_ready = Signal(int, str)  # (files_shifted, error_message_or_empty)

    def __init__(self, paths: list[str], offset: int, shots: list,
                 card_root: Path, volume_id: str | None):
        super().__init__()
        self._paths = paths
        self._offset = offset
        self._shots = shots
        self._card_root = card_root
        self._volume_id = volume_id

    def run(self) -> None:  # pragma: no cover - exercised via the GUI
        store = Store(db_path())
        n_paths, n_shots = len(self._paths), len(self._shots)
        grand = n_paths + n_shots
        try:
            n = meta.shift_all_dates(
                self._paths, self._offset, batch=32,
                progress=lambda d, t: self.progressed.emit(d, grand),
            )
            # Re-key rows so decisions survive the byte change, refreshing the
            # scan cache in the same pass — the next scan then reads no bytes.
            indexer.rekey_shifted(
                store, self._shots, self._volume_id, self._card_root,
                self._offset,
                progress=lambda d, t: self.progressed.emit(n_paths + d, grand),
            )
            # only the sidecars this shift created — never a card-wide sweep
            meta.clean_appledouble(self._paths)
        except Exception as e:  # surface the failure; never crash the thread
            store.close()
            self.result_ready.emit(0, str(e))
            return
        store.close()
        self.result_ready.emit(n, "")


class BlockingProgressDialog(QProgressDialog):
    """A progress dialog that cannot be dismissed while its work runs.

    Esc (which lands in ``reject``) and the window-close button are ignored
    until :meth:`allow_close` is called by the completion handler. Used for the
    clock-fix: it has no cancel path by design, and if the dialog could be
    dismissed early its window-modal barrier would drop — letting the user cull
    files while exiftool is still rewriting them underneath."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._closable = False

    def allow_close(self) -> None:
        self._closable = True

    def reject(self) -> None:
        if self._closable:
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self._closable:
            super().closeEvent(event)
        else:
            event.ignore()


class ImageView(QGraphicsView):
    """Fit-to-window image with a loupe that zooms toward the cursor.

    ``4`` toggles between fit-to-window and a 100% loupe centred on wherever the
    pointer is (not the middle of the frame), so you zoom into the spot you're
    inspecting. The scroll wheel zooms in/out continuously, anchored under the
    cursor; drag to pan when zoomed."""

    LOUPE_SCALE = 1.0    # 100% (1 image pixel : 1 screen pixel) for the Z loupe
    MIN_SCALE = 0.05
    MAX_SCALE = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item: QGraphicsPixmapItem | None = None
        self._base_pix: QPixmap | None = None   # unrotated source pixmap
        self._rotation = 0                       # display-only rotation, 0/90/180/270
        self._zoomed = False   # True when not fit-to-window (loupe or wheel zoom)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setStyleSheet(f"background: {BG};")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Zoom anchored under the mouse — the spot under the cursor stays put.
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # Never take keyboard focus: otherwise the view consumes arrow keys and
        # Space (scrolling) before they reach the window's key handler, so the
        # keyboard-first cull loop appears dead. The window owns all keys.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    @property
    def _loupe(self) -> bool:  # back-compat name used elsewhere/tests
        return self._zoomed

    def show_path(self, path: str | None) -> None:
        pix = QPixmap(path) if path else None
        self._base_pix = pix if (pix is not None and not pix.isNull()) else None
        self._rotation = 0          # each new photo starts upright
        self._render_pix()

    def _render_pix(self) -> None:
        """(Re)build the scene from the base pixmap at the current rotation."""
        self._scene.clear()
        self._item = None
        self._zoomed = False
        self._set_panning(False)
        if self._base_pix is not None:
            pix = self._base_pix
            if self._rotation:
                pix = pix.transformed(QTransform().rotate(self._rotation))
            self._item = self._scene.addPixmap(pix)
            self._scene.setSceneRect(pix.rect())
        self._fit()

    def rotate_cw(self) -> None:
        """Rotate the displayed image 90° clockwise — view only, never the file."""
        if self._base_pix is None:
            return
        self._rotation = (self._rotation + 90) % 360
        self._render_pix()

    def _cursor_scene_pos(self):
        """Scene point under the pointer, or the view centre if it's outside."""
        local = self.mapFromGlobal(QCursor.pos())
        if not self.viewport().rect().contains(local):
            local = self.viewport().rect().center()
        return self.mapToScene(local)

    def _set_panning(self, on: bool) -> None:
        self.setDragMode(
            QGraphicsView.DragMode.ScrollHandDrag if on
            else QGraphicsView.DragMode.NoDrag)
        policy = (Qt.ScrollBarPolicy.ScrollBarAsNeeded if on
                  else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(policy)
        self.setVerticalScrollBarPolicy(policy)

    def toggle_loupe(self) -> bool:
        if self._item is None:
            return self._zoomed
        if self._zoomed:
            self._zoomed = False
            self._set_panning(False)
            self._fit()
        else:
            # zoom to 100% centred on the spot under the cursor
            target = self._cursor_scene_pos()
            self._zoomed = True
            self._set_panning(True)
            self.resetTransform()
            self.scale(self.LOUPE_SCALE, self.LOUPE_SCALE)
            self.centerOn(target)
        return self._zoomed

    def zoom_by(self, factor: float) -> None:
        """Multiply the zoom (anchored under the cursor). Enters zoomed mode."""
        if self._item is None:
            return
        current = self.transform().m11()
        new = max(self.MIN_SCALE, min(self.MAX_SCALE, current * factor))
        if new == current:
            return
        if not self._zoomed:
            self._zoomed = True
            self._set_panning(True)
        self.scale(new / current, new / current)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self._item is None:
            return
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(1.18 if delta > 0 else 1 / 1.18)
            event.accept()

    def _fit(self) -> None:
        if self._item is not None:
            self.resetTransform()
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def _apply_fit(self) -> None:  # retained for callers; fits only when not zoomed
        if self._item is None or self._zoomed:
            return
        self._fit()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        if not self._loupe:
            self._apply_fit()


class FilmStrip(QWidget):
    """A full-width segmented progress strip: one slot per shot, decision-
    coloured, with faint group dividers and a highlighted current shot.

    Slots span the whole width so the strip reads as a deliberate progress bar
    rather than a cluster of dots. When there are more shots than pixels allow,
    slots collapse to thin ticks (still proportional and positional)."""

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self._scan: tuple[int, int] | None = None  # (done, total) while scanning
        self.setFixedHeight(34)

    def set_scanning(self, done: int, total: int) -> None:
        """Show a scan-progress fill instead of slots — during a scan the shots
        aren't grouped yet, so the per-group strip has nothing to scope to."""
        self._scan = (done, total)
        self.update()

    def clear_scanning(self) -> None:
        self._scan = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(SURFACE))
        pad = 10.0
        usable = max(1.0, self.width() - 2 * pad)
        top, bot = 8.0, self.height() - 8.0
        h = bot - top

        # While scanning, the shots have no groups yet — show a clean progress
        # fill rather than cramming everything into invisible ticks.
        if self._scan is not None:
            done, total = self._scan
            frac = (done / total) if total else 0.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(DOT_UNDECIDED))
            p.drawRoundedRect(QRectF(pad, top, usable, h), 4, 4)
            p.setBrush(QColor(ACCENT_FOCUS))
            p.drawRoundedRect(QRectF(pad, top, max(2.0, usable * frac), h), 4, 4)
            p.end()
            return

        # Scope to the current time-gap group only — a full-card strip with
        # hundreds of shots collapses to invisible 1px ticks. This re-scopes
        # itself whenever the cursor moves into a new group.
        shots, cur_idx = self.session.current_group_shots()
        if not shots:
            p.end()
            return

        n = len(shots)
        slot = usable / n
        gap = 3.0 if slot > 8 else (1.5 if slot > 4 else 0.0)
        radius = min(4.0, max(1.0, (slot - gap) / 2))

        for i, shot in enumerate(shots):
            x0 = pad + i * slot
            w = max(1.0, slot - gap)
            is_current = i == cur_idx
            if shot.decision is not None:
                color = QColor(DECISION_COLOR[shot.decision])
            else:
                color = QColor(ACCENT_FOCUS) if is_current else QColor(DOT_UNDECIDED)
            p.setPen(Qt.PenStyle.NoPen)
            if is_current:
                # taller, brighter slot with a focus underline
                p.setBrush(color)
                p.drawRoundedRect(QRectF(x0, top - 3, w, h + 6), radius, radius)
                p.setBrush(QColor(ACCENT_FOCUS))
                p.drawRoundedRect(QRectF(x0, bot + 1, w, 2.5), 1, 1)
            else:
                p.setBrush(color)
                p.drawRoundedRect(QRectF(x0, top, w, h), radius, radius)
        p.end()


class MainWindow(QMainWindow):
    # action name -> key(s); kept declarative so the smoke test can call actions.
    def __init__(
        self,
        session: Session,
        scan_target: Path,
        *,
        gap_s: float = SCENE_GAP_S,
        start_scan: bool = True,
    ) -> None:
        super().__init__()
        self.session = session
        self.scan_target = scan_target
        self.gap_s = gap_s
        self._worker: ScanWorker | None = None
        self._verify_worker: BackupVerifyWorker | None = None
        self._clock_worker: ClockFixWorker | None = None
        self._shown_path: str | None = None   # currently displayed image; skip redundant reloads
        self._resumed = False                 # resume-to-first-undecided happens once, on load
        self._freed_cache: int | None = None  # cached trash size; invalidated when the trash changes
        # (path, parsed EXIF) for the shown shot — re-read only when the path
        # changes, not on every render tick during a long background scan.
        self._exif_cache: tuple[str, meta.DisplayExif] | None = None
        self.setWindowTitle("foto-util")
        self.resize(1100, 760)
        self.setStyleSheet(
            f"QMainWindow {{ background: {BG}; }}"
            f"QMenuBar {{ background: {SURFACE_HI}; color: {TEXT_DIM};"
            f"  border-bottom: 1px solid {HAIRLINE}; padding: 2px 6px; }}"
            f"QMenuBar::item {{ background: transparent; padding: 4px 10px;"
            f"  border-radius: 5px; }}"
            f"QMenuBar::item:selected {{ background: {SURFACE}; color: {TEXT}; }}"
            f"QMenu {{ background: {SURFACE}; color: {TEXT};"
            f"  border: 1px solid {HAIRLINE}; padding: 4px; }}"
            f"QMenu::item {{ padding: 5px 18px; border-radius: 4px; }}"
            f"QMenu::item:selected {{ background: {ACCENT_FOCUS}; color: #fff; }}"
        )

        # -- top bar: bold filename · pill badge ········· EXIF (right) -------
        self.topbar = self._make_bar(48)
        tl = self.topbar.layout()
        self.lbl_name = QLabel("")
        self.lbl_name.setStyleSheet(
            f"color: {TEXT}; font-size: 14px; font-weight: 600;")
        self.lbl_badge = self._pill("", TEXT_FAINT)
        self.lbl_exif = QLabel("")
        self.lbl_exif.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        # capture date/time — its own label, set off to the far right so it reads
        # cleanly instead of being crammed in with the camera settings.
        self.lbl_when = QLabel("")
        self.lbl_when.setStyleSheet(f"color: {TEXT}; font-size: 12px;")
        tl.addWidget(self.lbl_name)
        tl.addWidget(self.lbl_badge)
        tl.addStretch(1)
        tl.addWidget(self.lbl_exif)
        tl.addSpacing(22)
        tl.addWidget(self.lbl_when)

        # -- viewport (subtle inset so the photo doesn't butt the window) -----
        self.image = ImageView()
        viewport = QWidget()
        viewport.setStyleSheet(f"background: {BG};")
        vl = QVBoxLayout(viewport)
        vl.setContentsMargins(14, 14, 14, 14)
        vl.addWidget(self.image)

        self.strip = FilmStrip(session)

        # -- status bar: position (left) ········· space-to-free (right) -----
        self.statusbar = self._make_bar(30, top_border=True)
        sl = self.statusbar.layout()
        self.lbl_pos = QLabel("")
        self.lbl_pos.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        self.lbl_freed = QLabel("")
        self.lbl_freed.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        sl.addWidget(self.lbl_pos)
        sl.addStretch(1)
        sl.addWidget(self.lbl_freed)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.topbar)
        lay.addWidget(viewport, 1)
        lay.addWidget(self.strip)
        lay.addWidget(self.statusbar)
        self.setCentralWidget(central)
        # The window owns the keyboard (its children are all NoFocus), so every
        # keypress reaches keyPressEvent — the cull loop is keyboard-first.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # transient overlays (flash, help, toast)
        self._flash = QLabel(self)
        self._flash.setVisible(False)
        self._help = QLabel(_hints_html(), self)
        self._help.setTextFormat(Qt.TextFormat.RichText)
        # top-right, translucent hints panel (toggled with ? / H)
        self._help.setStyleSheet(
            "background: rgba(20,20,23,180);"
            f" border: 1px solid {HAIRLINE}; padding: 16px 22px;"
            " font-size: 13px; border-radius: 12px;"
        )
        self._help.setVisible(False)
        self._toast = QLabel(self)
        self._toast.setStyleSheet(
            f"color: {TEXT}; background: rgba(22,22,25,242);"
            f" border: 1px solid {HAIRLINE}; padding: 9px 16px;"
            " border-radius: 8px; font-size: 12px;"
        )
        self._toast.setVisible(False)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self._toast.setVisible(False))
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self._flash.setVisible(False))
        # soft auto-advance after a decision: pause so the result is visible
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.timeout.connect(self._do_pending_advance)

        self._build_menus()
        self.refresh()
        self.setFocus()  # grab the keyboard immediately on open
        if start_scan:
            self._start_scan()

    # -- small UI builders --------------------------------------------------
    def _make_bar(self, height: int, *, top_border: bool = False) -> QWidget:
        """A thin horizontal bar (top or status) with a hairline edge."""
        bar = QWidget()
        bar.setFixedHeight(height)
        edge = "border-top" if top_border else "border-bottom"
        bar.setStyleSheet(
            f"background: {SURFACE_HI}; {edge}: 1px solid {HAIRLINE};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(10)
        return bar

    def _pill(self, text: str, color: str) -> QLabel:
        """A small rounded badge/pill label."""
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {color}; background: rgba(255,255,255,0.06);"
            f" border: 1px solid {HAIRLINE}; border-radius: 8px;"
            " padding: 1px 8px; font-size: 11px; font-weight: 600;")
        return lbl

    # -- menus (off the hot path; each previews + confirms) -----------------
    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("File")
        file_menu.addAction("Open card or folder", self._do_open_source)
        file_menu.addSeparator()
        file_menu.addAction("Recover trashed files to card", self._do_recover_trash)
        file_menu.addAction("Empty trash (permanently delete)", self._do_empty_trash)
        file_menu.addSeparator()
        file_menu.addAction("Storage…", self._do_storage)
        file_menu.addSeparator()
        eject = file_menu.addAction("Eject card", self._act_eject)
        eject.setShortcut("Ctrl+E")   # shown as ⌘E on macOS; same handler as the key

        db = bar.addMenu("Database")
        db.addAction("Prune stale rows", self._do_prune_stale)
        db.addAction("Forget this card", self._do_forget_card)
        db.addSeparator()
        db.addAction("Clear all", self._do_clear_all)

        tools = bar.addMenu("Tools")
        act = tools.addAction("Fix clock offset", self._do_clock_fix)
        verify_act = tools.addAction(
            "Verify and clear clock-fix backups", self._do_verify_backups)
        if not meta.exiftool_available():
            for a in (act, verify_act):
                a.setEnabled(False)
                a.setToolTip("Install exiftool (brew install exiftool) to enable")

        # Hold references so the menus aren't garbage-collected / detached.
        self.menus = {"File": file_menu, "Database": db, "Tools": tools}

    def open_source(self, target: Path) -> None:
        """Switch the window to a new card/folder: rebuild the session over the
        same store and rescan, without restarting the app."""
        target = Path(target)
        root = volume.card_root_for(target)
        vol = volume.source_id_for(root)
        strict = volume.is_card(root)
        self._stop_workers()  # halt any in-flight scan for the old source
        self.scan_target = target
        self.session = Session(
            self.session.store, card_root=root, volume_id=vol, strict=strict)
        self.strip.session = self.session
        self._shown_path = None      # force the first image of the new source to load
        self._exif_cache = None
        self._resumed = False        # resume onto the new source's first undecided shot
        self._invalidate_freed()     # different volume → different trash dir
        self.setWindowTitle(f"foto-util · {root.name or target}")
        self.refresh()
        self._start_scan()

    def _do_open_source(self) -> None:  # pragma: no cover - dialog-bound
        picked = pick_source(self)
        if picked is not None:
            self.open_source(picked)

    def _do_storage(self) -> None:  # pragma: no cover - dialog-bound
        StorageDialog(self).exec()

    # -- scanning -----------------------------------------------------------
    def _stop_workers(self) -> None:
        """Cleanly stop and join the background threads (so a QThread is never
        destroyed while running — that aborts the process). The clock-fix worker
        is deliberately never interrupted (all-or-nothing); it is only joined —
        closeEvent refuses to close while it runs, and the modal progress dialog
        blocks every other path here, so in practice it has already finished."""
        for attr in ("_worker", "_verify_worker", "_clock_worker"):
            w = getattr(self, attr)
            if w is not None:
                if hasattr(w, "stop"):
                    w.stop()    # interruptible workers re-check the flag per batch
                w.wait(600000)  # bounded by the per-batch exiftool timeout
                setattr(self, attr, None)

    def _start_scan(self) -> None:
        self.strip.set_scanning(0, 0)   # show the progress fill straight away
        self._worker = ScanWorker(self.scan_target, self.session.volume_id, self.gap_s)
        self._worker.progressed.connect(self._on_progress)
        self._worker.done.connect(self._on_scan_done)
        self._worker.failed.connect(self._on_scan_failed)
        self._worker.start()

    def _rescan_current_source(self) -> None:
        """Re-scan the mounted card from scratch and repopulate the view. Used
        after an action empties the rows out from under the viewer (a DB clear,
        or recovering trashed files) so it doesn't just go blank."""
        self._stop_workers()       # never leave a scan running for the old state
        self._resumed = False      # resume onto the first shot once rows reappear
        self._invalidate_freed()
        self.refresh()
        self._start_scan()

    def _on_progress(self, done: int, total: int) -> None:
        self.strip.set_scanning(done, total)   # progress fill until grouping is done
        # Pull in whatever rows exist so early shots appear during the scan,
        # but throttle the full reload so a big card isn't O(n²).
        if done == 1 or done % 16 == 0 or done == total:
            self.refresh()
        self.lbl_freed.setText(f"scanning {done}/{total}…")

    def _on_scan_done(self, n: int) -> None:
        self.strip.clear_scanning()            # groups assigned now → per-group strip
        self.refresh()

    def _on_scan_failed(self, err: str) -> None:
        # Leave whatever rows were written (they're valid); just stop pretending
        # a scan is still running and tell the user what happened.
        self.strip.clear_scanning()
        self.refresh()
        self.show_status(f"scan failed: {err}", ACCENT_REJECT, ms=6000)

    # -- rendering ----------------------------------------------------------
    def refresh(self) -> None:
        self.session.reload()
        # Resume onto the first undecided shot once, when rows first appear — not
        # on every scan tick (that races the ~400 ms post-decision pause and can
        # yank the cursor out from under it).
        if not self._resumed and self.session.shots:
            self.session.goto_first_undecided()
            self._resumed = True
        self._render()

    def _render(self) -> None:
        shot = self.session.current
        if shot is None:
            self.lbl_name.setText("no shots yet")
            self.lbl_badge.setText("")
            self.lbl_exif.setText("")
            self.lbl_when.setText("")
            if self._shown_path is not None:
                self.image.show_path(None)
                self._shown_path = None
            self.lbl_pos.setText("")
            self.lbl_freed.setText("")
            self.strip.update()
            return

        path = shot.jpg_path or shot.raw_path
        self.lbl_name.setText(Path(path).name if path else "?")
        badge, bcolor = _badge_for(shot)   # reflects what's kept once decided
        self.lbl_badge.setText(badge)
        self.lbl_badge.setStyleSheet(
            f"color: {bcolor}; background: rgba(255,255,255,0.06);"
            f" border: 1px solid {HAIRLINE}; border-radius: 8px;"
            " padding: 1px 8px; font-size: 11px; font-weight: 600;")
        # A rejected shot's JPEG is off the card, but its byte-verified trash
        # copy exists — display (and read EXIF) from that, so a red shot can be
        # reviewed instead of un-deleted blind. trash_jpg is only ever set after
        # the copy was verified, so this never guesses.
        display_jpg = shot.trash_jpg or shot.jpg_path
        # EXIF for the top bar, cached per path — the scan refreshes the view
        # every few files for minutes, and re-parsing the same JPEG each tick
        # is a wasted disk read.
        if display_jpg:
            if self._exif_cache is None or self._exif_cache[0] != display_jpg:
                self._exif_cache = (display_jpg, meta.read_display_exif(display_jpg))
            de = self._exif_cache[1]
        else:
            de = meta.DisplayExif()
        self.lbl_exif.setText(de.one_line())
        self.lbl_when.setText(de.when_str())

        # Only (re)decode when the image actually changes. The background scan
        # refreshes the view periodically for minutes; reloading the same JPEG
        # each time would reset the loupe zoom and burn a full-res decode.
        if display_jpg != self._shown_path:
            self.image.show_path(display_jpg)  # JPEG-only viewing (no RAW decode)
            self._shown_path = display_jpg

        total = len(self.session.shots)
        counts = self.session.store.counts(self.session.volume_id)
        self.lbl_pos.setText(
            f"{self.session.index + 1} / {total}"
            f"   ·   {counts['undecided']} left")
        freed = self._freed_bytes()
        self.lbl_freed.setText(f"~{_human_size(freed)} to free")
        self.strip.update()

    def _freed_bytes(self) -> int:
        """Total size of the off-card trash, cached. Recomputed only after the
        trash changes (decision / undo / empty trash), never on plain navigation
        — otherwise every keypress re-walks the whole trash tree (a stat per
        file), which grows unbounded as you cull."""
        if self._freed_cache is None:
            t = self.session.trash_dir
            self._freed_cache = (
                sum(p.stat().st_size for p in t.rglob("*") if p.is_file())
                if t.exists() else 0
            )
        return self._freed_cache

    def _invalidate_freed(self) -> None:
        self._freed_cache = None

    # -- actions ------------------------------------------------------------
    def handle_action(self, action: str) -> None:
        fn = getattr(self, f"_act_{action}", None)
        if fn is not None:
            fn()

    # -- soft auto-advance (pause after a decision so the result is visible) --
    def _flush_pending_advance(self) -> None:
        """If a post-decision advance is queued, do it now (e.g. the next
        decision should land on the next shot, not re-decide this one)."""
        if self._pending_timer.isActive():
            self._pending_timer.stop()
            self.session.goto_next_undecided()
            self._render()

    def _cancel_pending_advance(self) -> None:
        self._pending_timer.stop()

    def _do_pending_advance(self) -> None:
        self.session.goto_next_undecided()
        self._render()

    def _decide(self, decision: Decision) -> None:
        self._flush_pending_advance()
        shot = self.session.current
        if shot is None:
            return
        # A trashed JPEG displays from its trash copy, so a re-decide that moves
        # it (either direction) changes the display path and the render below
        # reloads via the ordinary path-equality guard — no forcing needed.
        restored_any = bool(shot.trash_jpg or shot.trash_raw)
        try:
            self.session.decide(decision, advance=False)  # pause before advancing
        except Exception as e:  # orphan / safety refusal — surface, never crash
            self.show_status(str(e), ACCENT_REJECT, ms=2200)
            return
        self._invalidate_freed()                          # the trash changed
        self.flash(DECISION_COLOR[decision])
        self._render()                                    # badge now shows kept state
        text, color = _decision_status(self.session.current)
        # A re-decide that pulled files back out of the trash says so explicitly
        # — a revived shot must never be mistaken for a phantom entry.
        if restored_any and decision is not Decision.REJECT and text:
            text = f"Restored from trash · {text}"
        self.show_status(text, color, ms=1800 if restored_any else 1100)
        self._pending_timer.start(ADVANCE_MS)             # then auto-advance

    def _act_keep_both(self) -> None:
        self._decide(Decision.KEEP_BOTH)

    def _act_keep_jpg(self) -> None:
        self._decide(Decision.KEEP_JPG)

    def _act_reject(self) -> None:
        self._decide(Decision.REJECT)

    def _act_skip(self) -> None:
        self._cancel_pending_advance()
        self.session.skip()
        self._render()

    def _act_prev(self) -> None:
        self._cancel_pending_advance()
        self.session.back()
        self._render()

    def _act_undo(self) -> None:
        self._cancel_pending_advance()
        try:
            ok = self.session.undo_last()
        except Exception as e:  # e.g. trash emptied — can't restore
            self.show_status(f"can't undo: {e}", ACCENT_REJECT)
            self._render()
            return
        if not ok:
            self.show_status("nothing to undo", TEXT_DIM)
        self._invalidate_freed()   # undo restored files out of the trash
        self._render()

    def _act_next_group(self) -> None:
        self._cancel_pending_advance()
        self.session.next_group()
        self._render()

    def _act_prev_group(self) -> None:
        self._cancel_pending_advance()
        self.session.prev_group()
        self._render()

    def _act_loupe(self) -> None:
        self.image.toggle_loupe()

    def _act_rotate(self) -> None:
        self.image.rotate_cw()

    def _act_fullscreen(self) -> None:
        """Toggle borderless fullscreen ↔ a normal, resizable window."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _act_help(self) -> None:
        self._help.setVisible(not self._help.isVisible())
        self._position_overlays()

    def _act_eject(self) -> None:  # pragma: no cover - interactive/confirm
        # Only a card is ejectable. A plain folder source lives on some mounted
        # volume too (often the boot disk) — ejecting that would be a nasty
        # surprise, so refuse outright.
        if not volume.is_card(self.session.card_root):
            self.show_status("this source is a folder, not a card, so there is "
                             "nothing to eject", TEXT_DIM, ms=2200)
            return
        if QMessageBox.question(
            self, "Eject card",
            "Eject the card now?\n\n"
            "Tip: run Recover Image DB on the camera afterwards (Menu → Setup).",
        ) != QMessageBox.StandardButton.Yes:
            return
        # Offer to hand the card back pristine: macOS materializes xattrs as
        # ``._`` sidecar files on exFAT, which the camera never reads. Opt-in
        # every confirmed eject; pattern-locked to ._ files under DCIM
        # (fileops.find_sidecars) so it can never match a photo. Declining
        # still ejects.
        n_side = len(fileops.find_sidecars(self.session.card_root))
        if n_side and QMessageBox.question(
            self, "Eject card",
            f"While we're at it, remove {_n(n_side, 'macOS “._” sidecar file')} "
            "from the card?\n\nmacOS leaves these behind on exFAT cards. Your "
            "camera never creates or reads them, so they are safe to remove.",
        ) == QMessageBox.StandardButton.Yes:
            fileops.clean_sidecars(self.session.card_root)
        import subprocess

        mp = volume.mount_point(self.scan_target)
        try:
            proc = subprocess.run(
                ["diskutil", "eject", str(mp)],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.show_status(f"couldn't eject the card: {e}", ACCENT_REJECT, ms=4000)
            return
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "diskutil error"
            self.show_status(f"couldn't eject the card: {detail}", ACCENT_REJECT, ms=4000)
            return
        # The card is gone, so the roll on screen points at nothing. Offer a
        # new source; if the user is done, close the window rather than leave
        # a dead view behind.
        picked = pick_source(self)
        if picked is not None:
            self.open_source(picked)
        else:
            self.close()

    def _do_recover_trash(self) -> None:  # pragma: no cover - dialog-bound
        """Put every file in this card's trash back on the card. The rescue for
        files stranded with no usable DB link; until Empty trash runs, it is also
        a bulk 'undo all of my deletions for this card'."""
        t = self.session.trash_dir
        n = (
            sum(1 for p in t.rglob("*")
                if p.is_file() and not p.name.endswith(".foto-util-tmp"))
            if t.exists() else 0
        )
        if n == 0:
            self.show_status("nothing in the trash to recover", TEXT_DIM)
            return
        if QMessageBox.question(
            self, "Recover trashed files",
            f"Put {_n(n, 'file')} from this card's trash back on the card?\n"
            "The shots involved become undecided again.",
        ) != QMessageBox.StandardButton.Yes:
            return
        restored, errors = self.session.recover_orphaned_trash()
        self._rescan_current_source()   # pick up recovered files (incl. orphans)
        if errors:
            QMessageBox.warning(
                self, "Recover trashed files",
                f"Put back {_n(restored, 'file')}, but {_n(len(errors), 'file')} "
                "couldn't be restored:\n"
                + "\n".join(f"• {Path(p).name}: {e}" for p, e in errors[:10]),
            )
        else:
            self.show_status(f"put {_n(restored, 'file')} back on the card", ACCENT_KEEP)

    def _do_empty_trash(self) -> None:  # pragma: no cover - dialog-bound
        """The explicit 'clear the deleted stuff' commit boundary (G10): the
        only thing that permanently frees dropped files. Until this runs they
        stay recoverable in the off-card trash."""
        t = self.session.trash_dir
        n = (
            sum(1 for p in t.rglob("*")
                if p.is_file() and not p.name.endswith(".foto-util-tmp"))
            if t.exists() else 0
        )
        if n == 0:
            self.show_status("trash is already empty", TEXT_DIM)
            return
        size = _human_size(self._freed_bytes())
        if QMessageBox.warning(
            self, "Empty trash",
            f"Permanently delete {_n(n, 'dropped file')} ({size}) for this card?\n"
            "There is no way back after this. Those shots will be gone for good.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        removed = fileops.empty_trash(t)
        # The staged files are gone, so those decisions are now final: drop the
        # trash pointers (and the now-fictional paths) so undo stops offering to
        # restore deleted files — then prune the rows left referencing nothing,
        # so fully deleted shots disappear from the roll instead of lingering as
        # blank tombstones.
        self._store().clear_trash_pointers(self._vol())
        prune.prune_stale(self._store(), self._vol())
        self._invalidate_freed()
        self.refresh()
        self.show_status(f"deleted {_n(removed, 'file')} for good", ACCENT_REJECT)

    # -- database / clock-fix menu handlers ---------------------------------
    def _store(self) -> Store:
        return self.session.store

    def _vol(self) -> str:
        return self.session.volume_id or ""

    def _do_prune_stale(self) -> None:  # pragma: no cover - dialog-bound
        stale = prune.find_stale(self._store(), self._vol())
        if not stale:
            QMessageBox.information(self, "Prune stale rows",
                                    "No stale rows for this card.")
            return
        if QMessageBox.question(
            self, "Prune stale rows",
            f"Forget {_n(len(stale), 'stale entry', 'stale entries')} for this "
            "card?\nTheir files are gone from both the card and the trash. "
            "No photo is touched.",
        ) != QMessageBox.StandardButton.Yes:
            return
        n = self._store().delete_rows([s.hash for s in stale])
        self.refresh()
        self.show_toast(f"forgot {_n(n, 'stale entry', 'stale entries')}")

    def _guard_pending_trash(self, title: str, volume_id: str | None) -> bool:
        """Refuse a metadata clear that would strand files still in the trash
        (those rows are the only link back to them). Returns True if the caller
        should abort. ``volume_id=None`` checks every card (Clear all)."""
        n = self._store().pending_trash_count(volume_id)
        if n:
            QMessageBox.warning(
                self, title,
                f"There are still trashed files for {_n(n, 'shot')}. Clearing "
                "the database now would strand them with no way to restore "
                "from inside the app.\n\n"
                "Put them back first with File → Recover trashed files, or "
                "delete them for good with File → Empty trash.",
            )
            return True
        return False

    def _do_forget_card(self) -> None:  # pragma: no cover - dialog-bound
        if self._guard_pending_trash("Forget this card", self._vol()):
            return
        c = self._store().counts(self._vol())
        if QMessageBox.question(
            self, "Forget this card",
            f"Forget everything about this card ({_n(c['total'], 'entry', 'entries')}) "
            "and rescan it from scratch?\nNo photo is touched. Every shot "
            "comes back undecided.",
        ) != QMessageBox.StandardButton.Yes:
            return
        n = self._store().delete_volume(self._vol())
        self._rescan_current_source()
        self.show_toast(f"forgot {_n(n, 'entry', 'entries')} · rescanning this card")

    def _do_clear_all(self) -> None:  # pragma: no cover - dialog-bound
        if self._guard_pending_trash("Clear all", None):
            return
        if QMessageBox.warning(
            self, "Clear all",
            "Reset the entire database, for every card?\n"
            "No photo is touched, but every decision is gone for good.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        n = self._store().clear_all()
        self._rescan_current_source()
        self.show_toast(f"cleared {_n(n, 'entry', 'entries')} · rescanning this card")

    def _do_clock_fix(self) -> None:  # pragma: no cover - dialog-bound
        shot = self.session.current
        if shot is None or not shot.jpg_path:
            self.show_toast("clock fix needs a JPEG reference shot")
            return
        ct = meta.read_capture_time(shot.jpg_path)
        if ct is None:
            self.show_toast("reference shot has no EXIF date")
            return
        default = ct.when.strftime("%Y-%m-%d %H:%M:%S")
        text, ok = QInputDialog.getText(
            self, "Fix clock offset",
            f"This shot is recorded as:\n  {default}\n\n"
            "Enter its TRUE date/time (YYYY-MM-DD HH:MM:SS):",
            text=default,
        )
        if not ok:
            return
        try:
            true_when = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            self.show_toast("couldn't make sense of that date, use YYYY-MM-DD HH:MM:SS")
            return
        offset = meta.compute_offset(shot.jpg_path, true_when)
        if offset == 0:
            self.show_toast("that matches the recorded time already, nothing to shift")
            return

        shots = self._store().shots(self._vol())
        paths = [p for s in shots for p in (s.jpg_path, s.raw_path)
                 if p and Path(p).exists()]
        sign = "+" if offset >= 0 else "−"
        if QMessageBox.question(
            self, "Fix clock offset",
            f"Shift every capture date on this card by {sign}{abs(offset)} seconds "
            f"({_n(len(paths), 'file')})?\nEach file keeps a _original backup, "
            "so nothing is lost until you verify and clear them.",
        ) != QMessageBox.StandardButton.Yes:
            return
        # Run the shift + re-key + ._ cleanup off the UI thread behind a modal
        # progress bar — a real card is thousands of files, so this must never
        # freeze the window. Non-cancelable, and the window refuses to close
        # while it runs: a clock-fix is all-or-nothing (a partial shift would
        # leave the card half-corrected, and an interrupted re-key would orphan
        # decisions whose files were already shifted).
        grand = len(paths) + len(shots)
        dlg = BlockingProgressDialog("Fixing clock on the card…", "", 0, grand, self)
        dlg.setWindowTitle("Fix clock offset")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setCancelButton(None)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        worker = ClockFixWorker(paths, offset, shots, self.session.card_root,
                                self.session.volume_id)
        self._clock_worker = worker
        worker.progressed.connect(lambda d, t: (dlg.setMaximum(t), dlg.setValue(d)))
        worker.result_ready.connect(lambda n, err: self._on_clockfix_done(n, err, dlg))
        worker.start()

    def _on_clockfix_done(self, n: int, err: str, dlg) -> None:  # pragma: no cover - dialog-bound
        dlg.allow_close()
        dlg.close()               # close, not reset() — reset leaves an emptied bar lingering
        self._clock_worker = None
        self._exif_cache = None   # dates changed in place; re-read the top bar
        self.refresh()
        if err:
            self.show_toast(f"clock fix failed: {err}")
        else:
            self.show_toast(f"shifted {_n(n, 'file')} · every decision kept · "
                            "._ leftovers cleaned")

    def _do_verify_backups(self) -> None:  # pragma: no cover - dialog-bound
        """Check each clock-fix ``_original`` backup against its live photo by
        image-data hash, deleting the ones that provably match (only the date
        changed); keep and flag anything whose image data differs."""
        root = self.session.card_root
        found = backups.find_backups(root)
        if not found:
            QMessageBox.information(
                self, "Verify and clear backups", "No _original backups on this card.")
            return
        if QMessageBox.question(
            self, "Verify and clear backups",
            f"Check {_n(len(found), '_original backup')} against the live photos "
            "and delete the ones that match exactly?\n\n"
            "A backup is removed only when the photo is provably the same image "
            "(only its date changed). Anything that differs is kept and flagged.",
        ) != QMessageBox.StandardButton.Yes:
            return

        # A previous (canceled) verify may still be finishing its in-flight
        # batch — join it before replacing the reference, or the dropped QThread
        # could be destroyed while running (which aborts the process).
        if self._verify_worker is not None:
            self._verify_worker.stop()
            self._verify_worker.wait(600000)
            self._verify_worker = None

        dlg = QProgressDialog("Verifying backups…", "Stop", 0, len(found), self)
        dlg.setWindowTitle("Verify and clear backups")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        worker = BackupVerifyWorker(Path(root))
        self._verify_worker = worker
        worker.progressed.connect(lambda d, t: (dlg.setMaximum(t), dlg.setValue(d)))
        worker.result_ready.connect(lambda res: self._on_verify_done(res, dlg))
        dlg.canceled.connect(lambda: self._on_verify_canceled(worker))
        worker.start()

    def _on_verify_canceled(self, worker) -> None:  # pragma: no cover - dialog-bound
        """Stop (between batches) and tell the user where they stand — the
        operation is resumable: already-cleared backups stay cleared, the rest
        are picked up by simply running it again."""
        worker.stop()
        self.show_status(
            "verify stopped. anything already cleared stays cleared; "
            "run it again to finish the rest", TEXT_DIM, ms=4000)

    def _on_verify_done(self, res, dlg) -> None:  # pragma: no cover - dialog-bound
        dlg.close()
        self._verify_worker = None
        msg = f"Cleared {_n(res.reclaimed, 'verified backup')}."
        if res.mismatched:
            names = "\n".join("  • " + Path(p).name for p in res.mismatched[:12])
            more = "" if len(res.mismatched) <= 12 else f"\n  …and {len(res.mismatched) - 12} more"
            msg += (f"\n\n⚠ {_n(len(res.mismatched), 'file')} had DIFFERENT image "
                    f"data, so those backups were kept. Take a look at:\n{names}{more}")
        if res.errored:
            msg += (f"\n\n{_n(len(res.errored), 'file')} couldn't be checked; "
                    "those backups were kept too.")
        box = QMessageBox.warning if (res.mismatched or res.errored) else QMessageBox.information
        box(self, "Verify and clear backups", msg)

    # -- overlays -----------------------------------------------------------
    def flash(self, color: str) -> None:
        self._flash.setStyleSheet(f"background: {color};")
        self._flash.setGeometry(self.width() - 60, self.topbar.height() + 26, 44, 44)
        self._flash.setVisible(True)
        self._flash.raise_()
        self._flash_timer.start(120)

    def show_status(self, text: str, color: str = TEXT, ms: int = 1100) -> None:
        """A brief, translucent status message (e.g. the decision result)."""
        if not text:
            return
        self._toast.setText(text)
        self._toast.setStyleSheet(
            f"color: {color}; background: rgba(20,20,23,190);"
            f" border: 1px solid {HAIRLINE}; padding: 9px 18px;"
            " border-radius: 9px; font-size: 13px; font-weight: 600;")
        self._toast.adjustSize()
        self._toast.setVisible(True)
        self._toast.raise_()
        self._position_overlays()
        self._toast_timer.start(ms)

    def show_toast(self, text: str) -> None:
        self.show_status(text, TEXT, 2200)

    def _position_overlays(self) -> None:
        # hints panel docked to the top-right, just under the top bar
        self._help.adjustSize()
        self._help.move(
            max(16, self.width() - self._help.width() - 16),
            self.topbar.height() + 12,
        )
        # status message centred near the bottom
        self._toast.move(
            (self.width() - self._toast.width()) // 2,
            self.height() - self._toast.height() - 40,
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_overlays()

    # -- keys ---------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        cmd_or_ctrl = mods & (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier)
        mapping = {
            # decisions: 1 keep JPEG, 2 keep both, 3 delete; 4 zoom
            Qt.Key.Key_1: "keep_jpg",
            Qt.Key.Key_2: "keep_both",
            Qt.Key.Key_3: "reject",
            Qt.Key.Key_4: "loupe",
            Qt.Key.Key_R: "rotate",   # rotate the view 90° (display only)
            Qt.Key.Key_F: "fullscreen",
            # navigation + housekeeping
            Qt.Key.Key_Right: "skip",
            Qt.Key.Key_Space: "skip",
            Qt.Key.Key_Left: "prev",
            Qt.Key.Key_U: "undo",
            Qt.Key.Key_BracketLeft: "prev_group",
            Qt.Key.Key_BracketRight: "next_group",
            Qt.Key.Key_Question: "help",   # toggle the far-left hints panel
            Qt.Key.Key_H: "help",
        }
        if key == Qt.Key.Key_E and cmd_or_ctrl:
            self.handle_action("eject")
        elif key == Qt.Key.Key_Escape:
            self._help.setVisible(False)
            self._toast.setVisible(False)
        elif key in mapping:
            self.handle_action(mapping[key])
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Never interrupt a running clock-fix: stopping mid-way would leave the
        # card half-shifted with decisions orphaned. Finish first, then close.
        if self._clock_worker is not None and self._clock_worker.isRunning():
            self.show_status("the clock fix is still running, let it finish first",
                             ACCENT_JPG, ms=3000)
            event.ignore()
            return
        self._stop_workers()  # join the scan thread before teardown (avoids SIGABRT)
        super().closeEvent(event)


class SourceDialog(QDialog):
    """Startup picker: choose a mounted card/volume or browse to any folder.

    Lists mounted volumes (cards — those with a ``DCIM`` — first and tagged), and
    a Browse button for anywhere else. Read-only discovery; selecting a card
    points the app at its ``DCIM``. ``selected_path`` holds the result on accept.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.selected_path: Path | None = None
        self.setWindowTitle("Open a card or folder")
        self.resize(560, 420)
        self.setStyleSheet(
            f"QDialog {{ background: {BG}; }}"
            f"QLabel {{ color: {TEXT}; }}"
            f"QListWidget {{ background: {SURFACE}; color: {TEXT};"
            f"  border: 1px solid {HAIRLINE}; border-radius: 8px; padding: 4px;"
            f"  font-size: 13px; outline: none; }}"
            f"QListWidget::item {{ padding: 9px 10px; border-radius: 6px; }}"
            f"QListWidget::item:selected {{ background: {ACCENT_FOCUS}; color: #fff; }}"
            f"QPushButton {{ background: {SURFACE_HI}; color: {TEXT};"
            f"  border: 1px solid {HAIRLINE}; border-radius: 7px;"
            f"  padding: 7px 16px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: {SURFACE}; }}"
            f"QPushButton:default {{ background: {ACCENT_FOCUS}; border-color: {ACCENT_FOCUS}; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)
        heading = QLabel("Where are the photos?")
        heading.setStyleSheet(f"color: {TEXT}; font-size: 16px; font-weight: 600;")
        lay.addWidget(heading)
        sub = QLabel("Pick a mounted card or volume, or browse to a folder.")
        sub.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        lay.addWidget(sub)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._accept_selected())
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setWordWrap(True)
        lay.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._populate)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.open_btn = QPushButton("Open")
        self.open_btn.setDefault(True)
        self.open_btn.clicked.connect(self._accept_selected)
        buttons.addWidget(refresh)
        buttons.addWidget(browse)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.open_btn)
        lay.addLayout(buttons)

        self._populate()

    def _populate(self) -> None:
        self.list.clear()
        sources = volume.list_sources()
        if not sources:
            item = QListWidgetItem("No mounted volumes found. Use Browse…")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor(TEXT_DIM))
            self.list.addItem(item)
            self.open_btn.setEnabled(False)
            return
        self.open_btn.setEnabled(True)
        for src in sources:
            tag = "  📷 CARD" if src.is_card else ""
            item = QListWidgetItem(f"{src.name}{tag}\n{src.path}")
            target = src.dcim if (src.is_card and src.dcim) else src.path
            item.setData(Qt.ItemDataRole.UserRole, str(target))
            if not src.is_card:
                item.setForeground(QColor(TEXT_DIM))
            self.list.addItem(item)
        self.list.setCurrentRow(0)  # cards sort first

    def _browse(self) -> None:
        start = "/Volumes" if sys.platform == "darwin" and Path("/Volumes").is_dir() else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        if chosen:
            self.selected_path = Path(chosen)
            self.accept()

    def _accept_selected(self) -> None:
        item = self.list.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not data:
            return
        self.selected_path = Path(data)
        self.accept()


def pick_source(parent=None) -> Path | None:
    """Show the picker and return the chosen folder, or None if cancelled."""
    dlg = SourceDialog(parent)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.selected_path
    return None


class StorageDialog(QDialog):
    """File → Storage: how much disk the off-card trash takes, per card.

    Lists every per-card trash folder under the app dir — including cards that
    aren't mounted, whose space is otherwise invisible. Emptying is only offered
    for the *currently open* card, through the exact same guarded path as
    File → Empty trash, so a card's staged files can never be discarded behind
    its back; for any other card, open it first.
    """

    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win)
        self._win = win
        self.setWindowTitle("Storage")
        self.resize(560, 400)
        self.setStyleSheet(
            f"QDialog {{ background: {BG}; }}"
            f"QLabel {{ color: {TEXT}; }}"
            f"QListWidget {{ background: {SURFACE}; color: {TEXT};"
            f"  border: 1px solid {HAIRLINE}; border-radius: 8px; padding: 4px;"
            f"  font-size: 13px; outline: none; }}"
            f"QListWidget::item {{ padding: 9px 10px; border-radius: 6px; }}"
            f"QListWidget::item:selected {{ background: {SURFACE_HI}; }}"
            f"QPushButton {{ background: {SURFACE_HI}; color: {TEXT};"
            f"  border: 1px solid {HAIRLINE}; border-radius: 7px;"
            f"  padding: 7px 16px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: {SURFACE}; }}"
            f"QPushButton:disabled {{ color: {TEXT_FAINT}; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)
        heading = QLabel("Off-card trash usage")
        heading.setStyleSheet(f"color: {TEXT}; font-size: 16px; font-weight: 600;")
        lay.addWidget(heading)
        sub = QLabel(
            "Staged deletions live here until the trash is emptied. To reclaim "
            "another card's space, open that card first (File → Open).")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        lay.addWidget(sub)

        self.list = QListWidget()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(self.list, 1)

        self.lbl_total = QLabel("")
        self.lbl_total.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        lay.addWidget(self.lbl_total)

        buttons = QHBoxLayout()
        reveal = QPushButton("Reveal in Finder")
        reveal.clicked.connect(self._reveal)
        self.empty_btn = QPushButton("Empty this card's trash…")
        self.empty_btn.clicked.connect(self._empty_current)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        buttons.addWidget(reveal)
        buttons.addStretch(1)
        buttons.addWidget(self.empty_btn)
        buttons.addWidget(close)
        lay.addLayout(buttons)

        self._populate()

    def _populate(self) -> None:
        from .appdir import trash_root

        self.list.clear()
        root = trash_root()
        current = self._win.session.trash_dir
        total = 0
        current_size = 0
        dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.exists() else []
        for d in dirs:
            files = [p for p in d.rglob("*") if p.is_file()]
            size = sum(p.stat().st_size for p in files)
            total += size
            is_current = d == current
            if is_current:
                current_size = size
            mark = "   ← the card open now" if is_current else ""
            item = QListWidgetItem(
                f"{d.name}{mark}\n{_n(len(files), 'file')} · {_human_size(size)}")
            item.setData(Qt.ItemDataRole.UserRole, str(d))
            if not is_current:
                item.setForeground(QColor(TEXT_DIM))
            self.list.addItem(item)
        if not dirs:
            item = QListWidgetItem("The trash is empty. Nothing to reclaim.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor(TEXT_DIM))
            self.list.addItem(item)
        self.lbl_total.setText(f"Total: {_human_size(total)}")
        self.empty_btn.setEnabled(current_size > 0)

    def _reveal(self) -> None:  # pragma: no cover - opens Finder
        from .appdir import trash_root
        import subprocess

        if sys.platform == "darwin":
            subprocess.run(["open", str(trash_root())], check=False)

    def _empty_current(self) -> None:  # pragma: no cover - dialog-bound
        self._win._do_empty_trash()   # same confirm + pointer cleanup as the menu
        self._populate()


def _session_for(target: Path) -> tuple[Session, Path]:
    """Build a Session for ``target`` (resolving the card root / source id)."""
    root = volume.card_root_for(target)
    vol = volume.source_id_for(root)
    strict = volume.is_card(root)
    store = Store(db_path())
    return Session(store, card_root=root, volume_id=vol, strict=strict), target


def launch(scan_target: str | Path | None = None, *, gap_s: float = SCENE_GAP_S) -> int:
    """GUI entry: if no target is given, show the source picker first."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("foto-util")

    if scan_target is None:
        picked = pick_source()
        if picked is None:
            return 0  # user cancelled
        scan_target = picked

    session, target = _session_for(Path(scan_target))
    win = MainWindow(session, target, gap_s=gap_s)
    win.showFullScreen()   # borderless, fills the screen; F toggles to a window
    return app.exec()


def run(session: Session, scan_target: str | Path, *, gap_s: float = SCENE_GAP_S) -> int:
    """Launch the viewer for an already-built session (used by tests/callers)."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("foto-util")
    win = MainWindow(session, Path(scan_target), gap_s=gap_s)
    win.show()
    return app.exec()
