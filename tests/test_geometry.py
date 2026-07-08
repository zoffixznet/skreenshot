"""Unit tests for the pure geometry math."""

from skreenshot.geometry import (
    Rect,
    clamp_point,
    intersect,
    is_click,
    logical_to_device,
    normalize_drag,
    screen_offset,
    translate,
    union_rect,
)


class TestNormalizeDrag:
    def test_top_left_to_bottom_right(self):
        assert normalize_drag(10, 20, 110, 220) == Rect(10, 20, 100, 200)

    def test_bottom_right_to_top_left(self):
        assert normalize_drag(110, 220, 10, 20) == Rect(10, 20, 100, 200)

    def test_bottom_left_to_top_right(self):
        assert normalize_drag(10, 220, 110, 20) == Rect(10, 20, 100, 200)

    def test_top_right_to_bottom_left(self):
        assert normalize_drag(110, 20, 10, 220) == Rect(10, 20, 100, 200)

    def test_zero_drag(self):
        assert normalize_drag(5, 5, 5, 5) == Rect(5, 5, 0, 0)

    def test_negative_coordinates(self):
        # A monitor left of the primary puts logical coords below zero.
        assert normalize_drag(-100, -50, -10, 30) == Rect(-100, -50, 90, 80)


class TestIsClick:
    def test_zero_size_is_click(self):
        assert is_click(Rect(0, 0, 0, 0))

    def test_two_px_is_click(self):
        assert is_click(Rect(0, 0, 2, 2))

    def test_2x100_is_click(self):
        # Both dimensions must clear the threshold.
        assert is_click(Rect(0, 0, 2, 100))
        assert is_click(Rect(0, 0, 100, 2))

    def test_three_px_is_selection(self):
        assert not is_click(Rect(0, 0, 3, 3))

    def test_large_is_selection(self):
        assert not is_click(Rect(0, 0, 640, 480))


class TestUnionRect:
    def test_single_screen(self):
        assert union_rect([Rect(0, 0, 1920, 955)]) == Rect(0, 0, 1920, 955)

    def test_side_by_side(self):
        u = union_rect([Rect(0, 0, 1920, 1080), Rect(1920, 0, 1280, 1024)])
        assert u == Rect(0, 0, 3200, 1080)

    def test_monitor_left_of_primary(self):
        # flameshot PR #4127: primary not leftmost produced offset captures
        # when grabbing from primary alone; the union keeps it honest.
        u = union_rect([Rect(-1280, 0, 1280, 1024), Rect(0, 0, 1920, 1080)])
        assert u == Rect(-1280, 0, 3200, 1080)

    def test_vertical_stack_with_gap(self):
        u = union_rect([Rect(0, 0, 1000, 500), Rect(200, 800, 1000, 500)])
        assert u == Rect(0, 0, 1200, 1300)

    def test_empty_raises(self):
        import pytest

        with pytest.raises(ValueError):
            union_rect([])


class TestScreenOffset:
    def test_leftmost_screen_offset_zero(self):
        screens = [Rect(-1280, 100, 1280, 1024), Rect(0, 0, 1920, 1080)]
        u = union_rect(screens)
        assert screen_offset(screens[0], u) == (0, 100)
        assert screen_offset(screens[1], u) == (1280, 0)

    def test_single_screen_offset_zero(self):
        u = union_rect([Rect(0, 0, 1920, 955)])
        assert screen_offset(Rect(0, 0, 1920, 955), u) == (0, 0)


class TestTranslate:
    def test_translate(self):
        assert translate(Rect(10, 20, 30, 40), -10, -20) == Rect(0, 0, 30, 40)


class TestClampPoint:
    def test_inside_unchanged(self):
        assert clamp_point(50, 60, Rect(0, 0, 100, 100)) == (50, 60)

    def test_clamps_to_edges(self):
        b = Rect(0, 0, 100, 100)
        assert clamp_point(-5, 300, b) == (0, 100)
        assert clamp_point(101, -1, b) == (100, 0)

    def test_negative_origin_bounds(self):
        b = Rect(-100, -100, 200, 200)
        assert clamp_point(-500, 0, b) == (-100, 0)


class TestIntersect:
    def test_fully_inside(self):
        assert intersect(Rect(10, 10, 20, 20), Rect(0, 0, 100, 100)) == Rect(
            10, 10, 20, 20
        )

    def test_partial_overlap(self):
        assert intersect(Rect(-10, -10, 30, 30), Rect(0, 0, 100, 100)) == Rect(
            0, 0, 20, 20
        )

    def test_disjoint_is_empty(self):
        r = intersect(Rect(200, 200, 10, 10), Rect(0, 0, 100, 100))
        assert r.w == 0 and r.h == 0


class TestLogicalToDevice:
    def test_dpr_1_identity(self):
        assert logical_to_device(Rect(10, 20, 30, 40), 1.0) == Rect(10, 20, 30, 40)

    def test_dpr_2_doubles(self):
        # Verified live: QT_SCALE_FACTOR=2 makes a 640x400 logical screen
        # grab as 1280x800 device pixels.
        assert logical_to_device(Rect(10, 20, 30, 40), 2.0) == Rect(20, 40, 60, 80)

    def test_fractional_dpr_rounds_edges(self):
        # 1.25 DPR: left=12.5 rounds to 12, right=(10+30)*1.25=50.
        r = logical_to_device(Rect(10, 20, 30, 40), 1.25)
        assert r == Rect(12, 25, 38, 50)

    def test_fractional_dpr_no_positional_drift(self):
        # Two horizontally adjacent logical rects must stay adjacent in
        # device space (truncating x and w separately breaks this).
        a = logical_to_device(Rect(0, 0, 13, 10), 1.5)
        b = logical_to_device(Rect(13, 0, 13, 10), 1.5)
        assert a.x + a.w == b.x

    def test_zero_rect(self):
        assert logical_to_device(Rect(0, 0, 0, 0), 2.0) == Rect(0, 0, 0, 0)
