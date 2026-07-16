"""Wayland overlay: the shared selection state machine and the per-window
coordinate mapping, on Qt's offscreen platform.

The offscreen platform has one screen, so multi-window behavior on real
outputs (fullscreen placement, cross-screen drags through the implicit grab)
is exercised by the wayland e2e suite; here the manager's logic is pinned:
clamping, click-threshold cancel, Shift-save, Esc press/release pairing,
exactly-once completion, and the union-offset arithmetic.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from skreenshot.geometry import Rect
from skreenshot.wayland_overlay import WaylandSelectionManager


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _manager(qapp, union=Rect(0, 0, 1000, 800), results=None):
    pixmap = QPixmap(union.w, union.h)
    sink = results if results is not None else []
    mgr = WaylandSelectionManager(pixmap, union, sink.append)
    return mgr, sink


def test_drag_reports_selection_in_union_coords(qapp):
    mgr, results = _manager(qapp)
    mgr.press(QPoint(10, 20), Qt.MouseButton.LeftButton)
    mgr.move(QPoint(200, 150))
    mgr.release(QPoint(200, 150), Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier)
    assert results == [("selected", Rect(10, 20, 190, 130), False)]


def test_shift_release_requests_save(qapp):
    mgr, results = _manager(qapp)
    mgr.press(QPoint(0, 0), Qt.MouseButton.LeftButton)
    mgr.release(QPoint(100, 100), Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ShiftModifier)
    assert results[0][0] == "selected"
    assert results[0][2] is True


def test_drag_beyond_union_is_clamped(qapp):
    # Cross-screen drags deliver coordinates outside every window; anything
    # beyond the union must clamp to its edge, like the X11 overlay's edge
    # clamp.
    mgr, results = _manager(qapp, union=Rect(0, 0, 500, 400))
    mgr.press(QPoint(100, 100), Qt.MouseButton.LeftButton)
    mgr.move(QPoint(9000, -50))
    mgr.release(QPoint(9000, -50), Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier)
    assert results == [("selected", Rect(100, 0, 400, 100), False)]


def test_tiny_drag_cancels(qapp):
    mgr, results = _manager(qapp)
    mgr.press(QPoint(50, 50), Qt.MouseButton.LeftButton)
    mgr.release(QPoint(51, 51), Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier)
    assert results == [("cancelled", "click-or-tiny-drag")]


def test_right_click_cancels_when_not_dragging(qapp):
    mgr, results = _manager(qapp)
    mgr.press(QPoint(50, 50), Qt.MouseButton.RightButton)
    assert results == [("cancelled", "right-click")]


def test_right_click_during_drag_does_not_cancel(qapp):
    mgr, results = _manager(qapp)
    mgr.press(QPoint(10, 10), Qt.MouseButton.LeftButton)
    mgr.press(QPoint(20, 20), Qt.MouseButton.RightButton)
    assert results == []


def test_esc_requires_press_then_release(qapp):
    # A lone Esc release (e.g. from a dialog that closed over the overlay)
    # must not cancel; a press+release pair must.
    mgr, results = _manager(qapp)
    mgr.key_release(Qt.Key.Key_Escape)
    assert results == []
    mgr.key_press(Qt.Key.Key_Escape)
    mgr.key_release(Qt.Key.Key_Escape)
    assert results == [("cancelled", "esc")]


def test_finishes_exactly_once(qapp):
    mgr, results = _manager(qapp)
    mgr.key_press(Qt.Key.Key_Escape)
    mgr.key_release(Qt.Key.Key_Escape)
    mgr.key_release(Qt.Key.Key_Escape)
    mgr.press(QPoint(0, 0), Qt.MouseButton.LeftButton)
    mgr.release(QPoint(100, 100), Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier)
    assert len(results) == 1


def test_window_offset_maps_local_to_union(qapp):
    # A screen at (1280, 0) in a union starting at (0, 0): local event
    # positions must translate by the screen offset. The offscreen platform
    # has one real QScreen; the offset math only needs its geometry replaced.
    union = Rect(0, 0, 2560, 800)
    mgr, results = _manager(qapp, union=union)
    window = mgr._windows[0]
    window._offset = QPoint(1280, 0)  # as if this window sat on screen 2
    assert window._to_union(QPointF(20, 30)) == QPoint(1300, 30)
    # And the reverse mapping used for painting:
    mgr.press(QPoint(1300, 30), Qt.MouseButton.LeftButton)
    mgr.move(QPoint(1400, 90))
    local = window._local_selection_qrect()
    assert (local.x(), local.y(), local.width(), local.height()) == (20, 30, 100, 60)


def test_negative_union_origin_offsets(qapp):
    # A monitor left of the primary puts union.x at -1280; a window on the
    # primary (offscreen platform: screen at x=0) must get offset +1280 into
    # union-local space FROM THE CONSTRUCTOR, so a local (0,0) event maps to
    # union (1280, 0).
    union = Rect(-1280, 0, 2560, 800)
    mgr, _ = _manager(qapp, union=union)
    window = mgr._windows[0]
    screen_x = window._screen.geometry().x()
    assert window._offset == QPoint(screen_x + 1280, 0)
    assert window._to_union(QPointF(0, 0)) == QPoint(screen_x + 1280, 0)


def test_hide_hides_every_window(qapp):
    mgr, _ = _manager(qapp)
    for w in mgr._windows:
        w.show()
    mgr.hide()
    assert all(not w.isVisible() for w in mgr._windows)
