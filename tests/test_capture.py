"""Compositing math for multi-monitor capture, exercised in-process on Qt's
offscreen platform with synthetic per-screen grabs. No real display, no Xvfb:
these lock in that each screen's grab is placed at the right offset AND scaled
by its OWN device pixel ratio, so a mixed-DPR layout (a HiDPI laptop beside a
1080p external) composites correctly rather than at one screen's DPR.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication

from skreenshot.capture import composite_desktop
from skreenshot.geometry import Rect


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _solid(w, h, color):
    pm = QPixmap(w, h)
    pm.fill(QColor(color))
    return pm


def test_composite_uniform_dpr_places_screens_one_to_one(qapp):
    # Two dpr=1 screens: red left (x 0..200), blue right (x 200..300).
    union = Rect(0, 0, 300, 100)
    grabs = [
        (_solid(200, 100, "red"), 0, 0, 1.0),
        (_solid(100, 100, "blue"), 200, 0, 1.0),
    ]
    canvas = composite_desktop(grabs, union)
    assert (canvas.width(), canvas.height()) == (300, 100)
    assert canvas.devicePixelRatio() == 1.0
    img = canvas.toImage()
    assert QColor(img.pixel(100, 50)) == QColor("red")
    assert QColor(img.pixel(250, 50)) == QColor("blue")


def test_composite_mixed_dpr_scales_and_places_each_screen(qapp):
    # union is 300x100 logical. Screen A: dpr=1, device grab 200x100, red at
    # (0,0). Screen B: dpr=2, device grab 200x200 (== logical 100x100), blue at
    # logical (200,0). The canvas must be sized at the MAX dpr (2) -> 600x200,
    # with each screen scaled by its own dpr into that shared device space.
    union = Rect(0, 0, 300, 100)
    grabs = [
        (_solid(200, 100, "red"), 0, 0, 1.0),
        (_solid(200, 200, "blue"), 200, 0, 2.0),
    ]
    canvas = composite_desktop(grabs, union)
    assert (canvas.width(), canvas.height()) == (600, 200)
    assert canvas.devicePixelRatio() == 2.0
    img = canvas.toImage()
    # A occupies device x 0..400 (dpr=1 grab upscaled 2x); B device x 400..600.
    assert QColor(img.pixel(200, 100)) == QColor("red")
    assert QColor(img.pixel(500, 100)) == QColor("blue")
