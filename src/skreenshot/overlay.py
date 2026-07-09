"""The selection overlay: one frameless window showing the frozen screen,
dimmed, with the dragged region punched out.

Window recipe (flameshot capturewidget.cpp, proven on xfwm4 and KWin X11):

- WindowStaysOnTopHint | FramelessWindowHint | Tool, with a FIXED size equal
  to the virtual-desktop union rect. A real _NET_WM_STATE_FULLSCREEN window
  gets clamped to a single monitor by the WM -- and so does a plain borderless
  window sized by hand with no size hints (KWin clamps it to one monitor).
  Declaring a fixed size publishes WM_NORMAL_HINTS
  min == max == union, which is what makes the WM honor the full span across
  every monitor (the same min==max primitive KDE Spectacle uses per window).
- Qt.X11BypassWindowManagerHint is deliberately NOT used: unmanaged windows
  receive no keyboard input unless manually focused, and flameshot removed
  the flag after it caused crashes on X11 GNOME. The scars are the point.
- show(), raise_(), activateWindow(), and activateWindow() again on mouse
  press (flameshot does this because fullscreen-above-panels only holds
  while the window is focused).

Fullscreen-above-panels is focus-conditional in xfwm4 and KWin: if the
overlay loses focus, panels rise above it and Esc goes dead (flameshot issue
1072). Deactivation is therefore treated as cancel so the overlay can never
soft-lock the screen (hard requirement 4).
"""

import logging

from PyQt6.QtCore import QEvent, QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

from .geometry import is_click, normalize_drag

log = logging.getLogger("skreenshot")

DEFAULT_DIM_ALPHA = 140  # spec suggests 110-160 of 255


class SelectionOverlay(QWidget):
    """Fullscreen frozen-frame overlay. Calls on_done exactly once with
    ("selected", Rect, save) in window-local logical coordinates (save is True
    when Shift was held at release), or ("cancelled", reason), or
    ("error", message)."""

    def __init__(self, pixmap, union, on_done, dim_alpha=DEFAULT_DIM_ALPHA):
        super().__init__()
        self._union_pos = (union.x, union.y)
        self._pixmap = pixmap
        self._on_done = on_done
        self._dim_alpha = dim_alpha
        self._dragging = False
        self._origin = None
        self._current = None
        self._esc_pressed = False
        self._was_active = False
        self._finished = False

        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        # Manual geometry over the whole virtual desktop, not fullscreen state.
        # A fixed size equal to the union sets WM size hints (WM_NORMAL_HINTS
        # min == max), which KWin honors; without them KWin clamps the frameless
        # window to a single monitor's work area, so the overlay would otherwise
        # cover only one screen on multi-monitor setups. xfwm4 and other
        # ICCCM-compliant WMs respect the same hint.
        self.setFixedSize(union.w, union.h)
        self.setGeometry(QRect(union.x, union.y, union.w, union.h))
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    # -- lifecycle -------------------------------------------------------

    def show_and_activate(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def moveEvent(self, event):  # noqa: N802 - Qt virtual
        # Some window managers (e.g. KWin) ignore the requested top-left of a
        # frameless window and re-place it on the primary monitor after mapping.
        # When the primary monitor is not the top-left one, that shifts the
        # frozen composite out of alignment with the physical screens. Snap back
        # to the union origin whenever the WM moves us off it; the correction
        # converges in a single step.
        ux, uy = self._union_pos
        if self.x() != ux or self.y() != uy:
            self.move(ux, uy)

    def _finish(self, result):
        if self._finished:
            return
        self._finished = True
        # Hide before reporting so the overlay is gone the instant the mouse
        # is released; flush the unmap to the X server right away.
        self.hide()
        QApplication.processEvents()
        self._on_done(result)

    def _cancel(self, reason):
        log.info("cancel: %s", reason)
        self._finish(("cancelled", reason))

    def _guard(self, name, fn):
        """Run an event handler body; any exception tears the overlay down
        (hard requirement 4: never leave a dead overlay on the screen)."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            log.exception("overlay: unhandled error in %s", name)
            self._finish(("error", f"{name}: {exc}"))

    # -- selection state -------------------------------------------------

    def _selection(self):
        """Current drag as a window-local logical Rect, or None."""
        if self._origin is None or self._current is None:
            return None
        return normalize_drag(
            self._origin.x(), self._origin.y(), self._current.x(), self._current.y()
        )

    def _selection_qrect(self):
        sel = self._selection()
        if sel is None:
            return None
        return QRect(sel.x, sel.y, sel.w, sel.h)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("paintEvent", lambda: self._paint())

    def _paint(self):
        painter = QPainter(self)
        # The pixmap has its devicePixelRatio set, so this fills 1:1.
        painter.drawPixmap(0, 0, self._pixmap)

        # Dim everything except the selection: alpha-black clipped to the
        # full rect minus the selection hole (flameshot drawInactiveRegion).
        dim_region = QRegion(self.rect())
        sel = self._selection_qrect()
        if sel is not None and not sel.isEmpty():
            dim_region = dim_region.subtracted(QRegion(sel))
        painter.setClipRegion(dim_region)
        painter.fillRect(self.rect(), QColor(0, 0, 0, self._dim_alpha))
        painter.setClipping(False)

        if sel is not None and not sel.isEmpty():
            pen = QPen(QColor(255, 255, 255, 230))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(sel.adjusted(-1, -1, 0, 0))
        painter.end()

    # -- input -----------------------------------------------------------

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("mousePressEvent", lambda: self._mouse_press(event))

    def _mouse_press(self, event):
        # Re-request activation on press: staying above panels and keeping
        # the keyboard is focus-conditional (flameshot does the same).
        self.activateWindow()
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()
        elif event.button() == Qt.MouseButton.RightButton and not self._dragging:
            # Right-click with no drag in progress cancels (hard req 3).
            self._cancel("right-click")

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("mouseMoveEvent", lambda: self._mouse_move(event))

    def _mouse_move(self, event):
        if not self._dragging:
            return
        pos = event.position().toPoint()
        # Clamp to the overlay so a drag past the edge selects to the edge.
        x = min(max(pos.x(), 0), self.width())
        y = min(max(pos.y(), 0), self.height())
        from PyQt6.QtCore import QPoint

        self._current = QPoint(x, y)
        self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("mouseReleaseEvent", lambda: self._mouse_release(event))

    def _mouse_release(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        sel = self._selection()
        if sel is None or is_click(sel):
            # A click or sub-3px drag is a cancel, not a 1x1 screenshot.
            self._cancel("click-or-tiny-drag")
            return
        # Shift held at release also saves the PNG to a file (see cli._save_png).
        save = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        log.info(
            "selection: %dx%d at (%d, %d) logical, save=%s",
            sel.w,
            sel.h,
            sel.x,
            sel.y,
            save,
        )
        self._finish(("selected", sel, save))

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("keyPressEvent", lambda: self._key_press(event))

    def _key_press(self, event):
        if event.key() == Qt.Key.Key_Escape:
            # Cancel on the release of an Esc that was PRESSED here too.
            # Acting on a lone release avoids a stray Esc release from a
            # closing dialog killing the session (Spectacle, KDE bug 428478).
            self._esc_pressed = True

    def keyReleaseEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("keyReleaseEvent", lambda: self._key_release(event))

    def _key_release(self, event):
        if event.key() == Qt.Key.Key_Escape and self._esc_pressed:
            self._cancel("esc")

    # -- focus loss ------------------------------------------------------

    def changeEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("changeEvent", lambda: self._change_event(event))
        super().changeEvent(event)

    def _change_event(self, event):
        if event.type() != QEvent.Type.ActivationChange or self._finished:
            return
        if self.isActiveWindow():
            self._was_active = True
        elif self._was_active:
            # Focus lost: panels can now rise above the overlay and Esc no
            # longer reaches it (flameshot issue 1072). Bail out rather than
            # risk a soft-locked screen.
            self._cancel("focus-lost")

    def closeEvent(self, event):  # noqa: N802 - Qt virtual
        if not self._finished:
            self._cancel("window-closed")
        event.accept()
