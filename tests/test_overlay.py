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
