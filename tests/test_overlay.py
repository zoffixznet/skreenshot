"""Overlay window construction, focused on the size request that stops a
window manager from clamping the frozen-frame overlay to a single monitor.

Runs in-process on Qt's offscreen platform, so it checks what the widget
REQUESTS of the WM (its size constraints and geometry) -- exactly the lever
that makes KWin honor a full virtual-desktop span. The real spanning on a live
WM is exercised by hand; here we lock in the request so it cannot regress.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QSize
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from skreenshot.geometry import Rect
from skreenshot.overlay import SelectionOverlay


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _overlay(union):
    pixmap = QPixmap(union.w, union.h)
    return SelectionOverlay(pixmap, union, lambda result: None)


def test_overlay_pins_size_to_union_so_wm_cannot_clamp(qapp):
    # Without a fixed size, KWin clamps the frameless window to one monitor's
    # work area. A fixed size equal to the virtual-desktop union forces the
    # full span across every monitor.
    union = Rect(0, 0, 3600, 1080)
    ov = _overlay(union)
    assert ov.minimumSize() == QSize(union.w, union.h)
    assert ov.maximumSize() == QSize(union.w, union.h)


def test_overlay_size_pinned_for_negative_origin_union(qapp):
    # A monitor left of the primary makes union.x negative; the size the
    # overlay pins must still be the full union size.
    union = Rect(-1280, 0, 3200, 1080)
    ov = _overlay(union)
    assert ov.minimumSize() == QSize(union.w, union.h)
    assert ov.maximumSize() == QSize(union.w, union.h)


def _drag_release(qapp, modifier):
    """Build an overlay mid-drag and release the left button with `modifier`.
    Returns the result tuple passed to on_done.
    """
    from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    result = {}
    ov = SelectionOverlay(
        QPixmap(100, 100), Rect(0, 0, 100, 100), lambda r: result.setdefault("r", r)
    )
    ov._dragging = True
    ov._origin = QPoint(10, 10)
    ov._current = QPoint(60, 70)  # a real 50x60 selection, not a click
    event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(60, 70),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        modifier,
    )
    ov._mouse_release(event)
    return result["r"]


def test_release_with_shift_requests_save(qapp):
    from PyQt6.QtCore import Qt

    result = _drag_release(qapp, Qt.KeyboardModifier.ShiftModifier)
    assert result[0] == "selected"
    assert result[2] is True


def test_release_without_shift_does_not_request_save(qapp):
    from PyQt6.QtCore import Qt

    result = _drag_release(qapp, Qt.KeyboardModifier.NoModifier)
    assert result[0] == "selected"
    assert result[2] is False
