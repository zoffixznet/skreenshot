"""The Wayland selection overlay: one frameless fullscreen window per screen,
all painting one shared frozen frame and one shared selection.

Wayland clients cannot position windows, so the X11 single-window-spanning
recipe (overlay.py) is impossible. The portable shape is Spectacle's: a plain
fullscreen xdg-toplevel per output. Layer-shell would pin above panels on
KWin/wlroots but GNOME refuses it for third parties, and LayerShellQt has no
Python bindings; a fullscreen toplevel needs neither (the compositor stacks
the active fullscreen window on top).

Load-bearing details, each verified against compositor/Qt sources:

- setScreen() and setGeometry(screen geometry) must happen BEFORE show():
  Qt then sends xdg_toplevel.set_fullscreen(output), which KWin, Mutter and
  wlroots honor. Changing the screen after mapping destroys and recreates
  the native window. Both are re-applied on QWindow::screenChanged because
  fractional per-screen scales can make Qt re-guess the screen after mapping
  (KDE bug 502047; Spectacle re-pins the same way).
- Cross-screen drags: while a button is held every compositor keeps pointer
  focus on the pressed surface and streams motion with surface-local
  coordinates beyond its bounds (negative or > size); Qt forwards them
  unclamped. So the pressed window alone hears the whole gesture, and the
  shared selection is kept in union-local logical coordinates; the other
  windows just repaint from it.
- Keyboard focus lives on exactly one surface, so Esc handling is installed
  on every window and a mouse press requests activation (xdg-activation,
  Qt 6.3+) so Esc keeps working after clicking a different output.
- Deactivation is NOT a cancel here, unlike the X11 overlay: activation
  legitimately hops between our own windows, and GNOME may deny initial
  focus outright (focus stealing prevention). A soft-lock is still
  impossible: right-click and click-without-drag cancel via pointer events,
  which need no keyboard focus.
"""

import logging

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

from .geometry import Rect, is_click, normalize_drag
from .overlay import DEFAULT_DIM_ALPHA

log = logging.getLogger("skreenshot")


class WaylandSelectionManager:
    """Shared selection state over per-screen overlay windows.

    Calls on_done exactly once with ("selected", Rect, save) in union-local
    logical coordinates (identical semantics to SelectionOverlay), or
    ("cancelled", reason), or ("error", message).
    """

    def __init__(self, pixmap, union, on_done, dim_alpha=DEFAULT_DIM_ALPHA,
                 screens=None):
        self._pixmap = pixmap
        self._union = union
        self._on_done = on_done
        self._dim_alpha = dim_alpha
        self._dragging = False
        self._origin = None  # QPoint, union-local logical
        self._current = None
        self._esc_pressed = False
        self._finished = False

        if screens is None:
            screens = QApplication.screens()
        if not screens:
            raise RuntimeError("no screens to show the overlay on")
        self._windows = [_ScreenOverlay(self, s, union) for s in screens]
        self._unpainted = set(self._windows)
        self._focus_logged = False
        log.info("overlay: %d windows, one per screen", len(self._windows))

    # -- lifecycle -------------------------------------------------------

    def show_and_activate(self):
        for w in self._windows:
            w.show_fullscreen_on_screen()
        # The last-shown window ends up focused (Qt requests activation per
        # window); any of them handles Esc, so which one wins does not matter.

    def hide(self):
        """Hide every overlay window (the CLI's crash net calls this)."""
        for w in self._windows:
            w.hide()

    def _finish(self, result, hide=True):
        # For a "selected" result the windows deliberately stay mapped until
        # the CLI has set the clipboard: the compositor only accepts a
        # selection from the client holding keyboard focus (KWin 5.27 and
        # Mutter both enforce it), and hiding first would drop that focus.
        # The CLI hides the overlay right after the copy, milliseconds later.
        if self._finished:
            return
        self._finished = True
        if hide:
            for w in self._windows:
                w.hide()
            QApplication.processEvents()
        self._on_done(result)

    def _cancel(self, reason):
        log.info("cancel: %s", reason)
        self._finish(("cancelled", reason))

    def _error(self, name, exc):
        log.exception("overlay: unhandled error in %s", name)
        self._finish(("error", f"{name}: {exc}"))

    @property
    def finished(self):
        return self._finished

    # -- shared selection state (union-local logical coordinates) --------

    def selection(self):
        """Current drag as a union-local logical Rect, or None."""
        if self._origin is None or self._current is None:
            return None
        return normalize_drag(
            self._origin.x(), self._origin.y(), self._current.x(), self._current.y()
        )

    def _clamp(self, point):
        x = min(max(point.x(), 0), self._union.w)
        y = min(max(point.y(), 0), self._union.h)
        return QPoint(x, y)

    def _repaint_all(self):
        for w in self._windows:
            w.update()

    # -- input, forwarded by the windows ----------------------------------

    def press(self, vpoint, button):
        if button == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._origin = self._clamp(vpoint)
            self._current = self._origin
            self._repaint_all()
        elif button == Qt.MouseButton.RightButton and not self._dragging:
            self._cancel("right-click")

    def move(self, vpoint):
        if not self._dragging:
            return
        self._current = self._clamp(vpoint)
        self._repaint_all()

    def release(self, vpoint, button, modifiers):
        if button != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._current = self._clamp(vpoint)
        sel = self.selection()
        if sel is None or is_click(sel):
            self._cancel("click-or-tiny-drag")
            return
        save = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        log.info(
            "selection: %dx%d at (%d, %d) logical, save=%s",
            sel.w, sel.h, sel.x, sel.y, save,
        )
        self._finish(("selected", sel, save), hide=False)

    def note_painted(self, window):
        # A Wayland client only paints after the compositor's configure, so
        # the last first-paint proves every window is actually mapped. The
        # e2e harness keys real input on this line.
        if window in self._unpainted:
            self._unpainted.discard(window)
            if not self._unpainted:
                log.info("overlay: all %d windows shown", len(self._windows))

    def note_focused(self):
        # First keyboard focus (wl_keyboard.enter): Esc is now live.
        if not self._focus_logged:
            self._focus_logged = True
            log.info("overlay: focused")

    def key_press(self, key):
        if key == Qt.Key.Key_Escape:
            # Same press+release pairing as the X11 overlay: acting on a
            # lone release protects against a stray Esc release from a
            # closing dialog (Spectacle, KDE bug 428478).
            self._esc_pressed = True

    def key_release(self, key):
        if key == Qt.Key.Key_Escape and self._esc_pressed:
            self._cancel("esc")

    def window_closed(self):
        if not self._finished:
            self._cancel("window-closed")


class _ScreenOverlay(QWidget):
    """One fullscreen window on one screen, painting its slice of the frozen
    frame and the shared selection. All state lives in the manager."""

    def __init__(self, manager, screen, union):
        super().__init__()
        self._manager = manager
        self._screen = screen
        g = screen.geometry()
        # This screen's origin in union-local logical coordinates.
        self._offset = QPoint(g.x() - union.x, g.y() - union.y)

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def show_fullscreen_on_screen(self):
        # Screen and geometry BEFORE show: Qt passes the output to
        # xdg_toplevel.set_fullscreen only if the window is already
        # associated with it, and a later setScreen recreates the window.
        self.setScreen(self._screen)
        self.setGeometry(self._screen.geometry())
        self.showFullScreen()
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._repin_screen)

    def _repin_screen(self, screen):
        # Fractional per-screen scales can make Qt re-assign the window to a
        # neighboring screen after mapping (KDE bug 502047); pin it back.
        if screen is not self._screen and not self._manager.finished:
            log.info(
                "overlay: window re-assigned to %s, re-pinning to %s",
                screen.name() if screen else "none",
                self._screen.name(),
            )
            self.setScreen(self._screen)
            self.setGeometry(self._screen.geometry())

    # -- coordinate mapping ------------------------------------------------

    def _to_union(self, position):
        """Window-local event position -> union-local logical QPoint.

        During an implicit grab the position may lie outside this window
        (cross-screen drag); the offset math stays valid, the manager clamps.
        """
        p = position.toPoint()
        return QPoint(p.x() + self._offset.x(), p.y() + self._offset.y())

    def _local_selection_qrect(self):
        sel = self._manager.selection()
        if sel is None:
            return None
        local = Rect(
            sel.x - self._offset.x(), sel.y - self._offset.y(), sel.w, sel.h
        )
        return QRect(local.x, local.y, local.w, local.h)

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("paintEvent", self._paint)

    def _paint(self):
        painter = QPainter(self)
        # The DPR-tagged pixmap covers the union; draw this screen's slice.
        painter.drawPixmap(-self._offset.x(), -self._offset.y(), self._pixmap())

        dim_region = QRegion(self.rect())
        sel = self._local_selection_qrect()
        if sel is not None and not sel.isEmpty():
            dim_region = dim_region.subtracted(QRegion(sel))
        painter.setClipRegion(dim_region)
        painter.fillRect(self.rect(), QColor(0, 0, 0, self._manager._dim_alpha))
        painter.setClipping(False)

        if sel is not None and not sel.isEmpty():
            pen = QPen(QColor(255, 255, 255, 230))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(sel.adjusted(-1, -1, 0, 0))
        painter.end()
        self._manager.note_painted(self)

    def _pixmap(self):
        return self._manager._pixmap

    # -- input ---------------------------------------------------------------

    def _guard(self, name, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - never leave a dead overlay
            self._manager._error(name, exc)

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("mousePressEvent", lambda: self._mouse_press(event))

    def _mouse_press(self, event):
        # Keyboard focus follows clicks between outputs (xdg-activation),
        # so Esc stays live on whichever screen the user works on.
        self.activateWindow()
        self._manager.press(self._to_union(event.position()), event.button())

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard(
            "mouseMoveEvent",
            lambda: self._manager.move(self._to_union(event.position())),
        )

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard(
            "mouseReleaseEvent",
            lambda: self._manager.release(
                self._to_union(event.position()), event.button(), event.modifiers()
            ),
        )

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("keyPressEvent", lambda: self._manager.key_press(event.key()))

    def keyReleaseEvent(self, event):  # noqa: N802 - Qt virtual
        self._guard("keyReleaseEvent", lambda: self._manager.key_release(event.key()))

    def changeEvent(self, event):  # noqa: N802 - Qt virtual
        # Activation is only observed for the readiness log; deactivation is
        # deliberately NOT a cancel here (see the module docstring).
        from PyQt6.QtCore import QEvent

        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._manager.note_focused()
        super().changeEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt virtual
        self._manager.window_closed()
        event.accept()
