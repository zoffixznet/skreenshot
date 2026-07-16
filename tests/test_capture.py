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

from skreenshot.capture import CaptureError, composite_desktop, derive_portal_dpr
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


class TestPortalDpr:
    """dpr of a portal screenshot: every portal backend composites all
    outputs at max(output scale) over the logical union, so the width ratio
    is the scale."""

    def test_exact_match_is_one(self):
        assert derive_portal_dpr(2560, 800, Rect(0, 0, 2560, 800)) == 1.0

    def test_hidpi_double(self):
        assert derive_portal_dpr(5120, 1600, Rect(0, 0, 2560, 800)) == 2.0

    def test_fractional_scale_kept(self):
        dpr = derive_portal_dpr(3200, 1000, Rect(0, 0, 2560, 800))
        assert abs(dpr - 1.25) < 0.001

    def test_near_integer_snaps(self):
        # Rounding in the compositor can make a 2x image a pixel short;
        # 2559/1280 must still be treated as the integer scale 2.
        assert derive_portal_dpr(2559, 1599, Rect(0, 0, 1280, 800)) == 2.0

    def test_height_mismatch_uses_width_ratio(self):
        # A letterboxed/cropped backend: width ratio wins, no exception.
        assert derive_portal_dpr(2560, 700, Rect(0, 0, 2560, 800)) == 1.0

    def test_zero_union_raises(self):
        with pytest.raises(CaptureError):
            derive_portal_dpr(100, 100, Rect(0, 0, 0, 0))
