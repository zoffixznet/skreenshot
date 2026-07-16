"""End-to-end tests on a real Wayland compositor: the real skreenshot binary
on a nested kwin_wayland, capture served by a mock xdg-desktop-portal on a
private bus, input driven with xdotool through the nested compositor, and
the clipboard read back by a focused client inside the nested session.

See tests/waylandlab.py for the stack (and the KWin/libwayland workaround).

Run with: make e2e-wayland  (pytest -m e2e_wayland)
"""

import base64
import glob
import os
import re
import subprocess
import sys
import time

import pytest
from pnglib import png_pixels, png_size
from waylandlab import WaylandLab, missing_core_tools

pytestmark = pytest.mark.e2e_wayland

_missing = missing_core_tools()
if _missing:
    pytestmark = [
        pytest.mark.e2e_wayland,
        pytest.mark.skip(reason=f"missing: {_missing}"),
    ]

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "skreenshot")

SCREEN_W, SCREEN_H = 1280, 800
# Quadrant colors (single-output pattern), same layout as the X11 e2e suite.
BLUE = (32, 64, 192)
YELLOW = (255, 204, 0)
GREEN = (0, 170, 68)
RED = (204, 34, 0)
# Half colors (dual-output pattern).
ORANGE = (255, 128, 0)
TEAL = (0, 128, 128)


def _offscreen_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication(
        ["pattern-painter"]
    )


def _paint_quadrants(img):
    from PyQt6.QtGui import QColor, QPainter

    w, h = img.width(), img.height()
    p = QPainter(img)
    p.fillRect(0, 0, w // 2, h // 2, QColor(*BLUE))
    p.fillRect(w // 2, 0, w - w // 2, h // 2, QColor(*YELLOW))
    p.fillRect(0, h // 2, w // 2, h - h // 2, QColor(*GREEN))
    p.fillRect(w // 2, h // 2, w - w // 2, h - h // 2, QColor(*RED))
    p.end()


def _paint_halves(img):
    from PyQt6.QtGui import QColor, QPainter

    w, h = img.width(), img.height()
    p = QPainter(img)
    p.fillRect(0, 0, w // 2, h, QColor(*ORANGE))
    p.fillRect(w // 2, 0, w - w // 2, h, QColor(*TEAL))
    p.end()


@pytest.fixture(scope="module")
def lab():
    _offscreen_app()
    lab = WaylandLab(outputs=1, width=SCREEN_W, height=SCREEN_H)
    lab.setup(_paint_quadrants)
    if lab.skip_reason:
        pytest.skip(f"wayland lab unavailable: {lab.skip_reason}")
    yield lab
    lab.teardown()


@pytest.fixture(scope="module")
def dual_lab():
    import shutil as _shutil

    if not _shutil.which("xwininfo"):
        pytest.skip("xwininfo missing (needed to find the nested output windows)")
    _offscreen_app()
    lab = WaylandLab(outputs=2, width=SCREEN_W, height=SCREEN_H)
    lab.setup(_paint_halves)
    if lab.skip_reason:
        pytest.skip(f"wayland dual lab unavailable: {lab.skip_reason}")
    # Anything failing between here and yield must not leak the Xvfb, the
    # nested compositor, the bus and the mock portal.
    try:
        windows = lab.compositor_windows()
        if len(windows) != 2:
            pytest.skip(f"expected 2 nested output windows, found {len(windows)}")
        # Without a WM on the lab Xvfb both output windows map at (0, 0);
        # spread them to match the logical layout so host coordinates equal
        # union coordinates once the output mapping is verified (below).
        lab.xdo("windowmove", str(windows[0]), "0", "0")
        lab.xdo("windowmove", str(windows[1]), str(SCREEN_W), "0")
        lab.xdo("windowraise", str(windows[0]))
        lab.xdo("windowraise", str(windows[1]))
        _align_windows_to_outputs(lab, windows)
    except BaseException:
        lab.teardown()
        raise
    yield lab
    lab.teardown()


# -- helpers -----------------------------------------------------------------


def start_app(lab, name, env_extra=None):
    log_path = os.path.join(lab.rtd, f"{name}.log")
    if os.path.exists(log_path):
        os.unlink(log_path)
    # stderr goes to a file, not a pipe: a serving app can outlive the drag
    # by a while, and an undrained pipe would block it once full.
    stderr = open(os.path.join(lab.rtd, f"{name}.stderr"), "wb")
    proc = subprocess.Popen(
        [sys.executable, LAUNCHER],
        env=lab.env(SKREENSHOT_LOG=log_path, **(env_extra or {})),
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )
    stderr.close()
    return proc, log_path


def read_log(log_path):
    if not os.path.exists(log_path):
        return ""
    with open(log_path) as fh:
        return fh.read()


def wait_log(log_path, needle, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = read_log(log_path)
        if needle in text:
            return text
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared; log:\n{read_log(log_path)}")


def wait_overlay(log_path, timeout=25):
    text = wait_log(log_path, "timing: overlay visible", timeout)
    # Compositor-acked readiness: every window painted (mapped after the
    # xdg configure) and one holds keyboard focus, so xdotool input and
    # Escape have somewhere real to go. A short residual sleep covers the
    # host-X11-to-nested-input plumbing.
    wait_log(log_path, "overlay: all", timeout)
    text = wait_log(log_path, "overlay: focused", timeout)
    time.sleep(0.3)
    return text


READER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clip_reader.py")


def read_clipboard_png(lab, replace=False):
    """Run a focused client inside the nested session that reads the
    clipboard (and optionally replaces it, releasing a serving skreenshot)."""
    args = [sys.executable, READER]
    if replace:
        args.append("--replace")
    result = subprocess.run(
        args,
        env=lab.env(QT_QPA_PLATFORM="wayland"),
        capture_output=True,
        text=True,
        timeout=45,
    )
    m = re.search(r"^PNG:(\S*)$", result.stdout, re.M)
    assert m, f"reader produced no PNG line: {result.stdout!r} {result.stderr!r}"
    return base64.b64decode(m.group(1)) if m.group(1) != "none" else None


def finish(proc, timeout=30):
    return proc.wait(timeout=timeout)


def complete_serving_capture(lab, proc, log_path):
    """After a selecting drag: verify the app serves the clipboard, read the
    PNG from inside the session, replace the clipboard, and reap the app.
    Also asserts the portal's screenshot file was deleted after reading:
    a leftover means user-visible litter (~/Pictures on KDE/GNOME)."""
    wait_log(log_path, "persistence plan: serve")
    wait_log(log_path, "serving clipboard in-process")
    assert proc.poll() is None, "app must stay alive while serving"
    png = read_clipboard_png(lab, replace=True)
    code = finish(proc)
    leftovers = glob.glob(os.path.join(lab.rtd, "portal-mock-*.png"))
    assert leftovers == [], f"portal screenshot files not cleaned up: {leftovers}"
    return png, code


def _align_windows_to_outputs(lab, windows):
    """Make host coordinates equal union coordinates on the dual lab.

    Which host window belongs to which output is discovered with a probe
    drag: a 30x30 selection at host (100, 100) reports its union position in
    the log. If it lands on the right output the two windows are swapped.
    """
    proc, log_path = start_app(lab, "mapping-probe")
    wait_overlay(log_path)
    lab.drag(100, 100, 130, 130)
    text = wait_log(log_path, "selection:")
    m = re.search(r"selection: 30x30 at \((\d+), (\d+)\)", text)
    assert m, f"probe selection not logged: {text}"
    if int(m.group(1)) >= SCREEN_W:
        lab.xdo("windowmove", str(windows[0]), str(SCREEN_W), "0")
        lab.xdo("windowmove", str(windows[1]), "0", "0")
    # The probe app is now serving the clipboard; release and reap it.
    complete_serving_capture(lab, proc, log_path)


# -- single-output tests ------------------------------------------------------


def test_session_detected_as_wayland_with_portal_capture(lab):
    proc, log_path = start_app(lab, "session")
    text = wait_overlay(log_path)
    assert "session: wayland" in text
    lab.xdo("mousemove", "300", "300", "key", "Escape")
    code = finish(proc)
    assert code == 2
    text = read_log(log_path)
    assert "capture: portal image" in text


def test_drag_copies_correct_crop_and_serves_clipboard(lab):
    proc, log_path = start_app(lab, "basic")
    wait_overlay(log_path)
    lab.drag(200, 150, 500, 350)  # 300x200 inside the blue quadrant
    png, code = complete_serving_capture(lab, proc, log_path)
    assert code == 0
    assert "selection: 300x200 at (200, 150)" in read_log(log_path)
    assert png is not None
    assert png_size(png) == (300, 200)
    pixel = png_pixels(png)
    assert pixel(0, 0) == BLUE
    assert pixel(299, 199) == BLUE
    assert pixel(150, 100) == BLUE


def test_crop_spanning_quadrants_lands_exactly(lab):
    # 200x200 centered on the quadrant intersection at (640, 400): each
    # corner must be a different color, catching any offset error in the
    # portal-image-to-union mapping.
    proc, log_path = start_app(lab, "quadrants")
    wait_overlay(log_path)
    lab.drag(540, 300, 740, 500)
    png, code = complete_serving_capture(lab, proc, log_path)
    assert code == 0
    assert png is not None
    assert png_size(png) == (200, 200)
    pixel = png_pixels(png)
    assert pixel(0, 0) == BLUE
    assert pixel(199, 0) == YELLOW
    assert pixel(0, 199) == GREEN
    assert pixel(199, 199) == RED
    assert pixel(99, 0) == BLUE
    assert pixel(100, 0) == YELLOW


def test_esc_cancels_without_touching_clipboard(lab):
    proc, log_path = start_app(lab, "esc")
    wait_overlay(log_path)
    lab.xdo("mousemove", "300", "300", "key", "Escape")
    code = finish(proc)
    assert code == 2
    assert "cancel: esc" in read_log(log_path)


def test_right_click_cancels(lab):
    proc, log_path = start_app(lab, "rclick")
    wait_overlay(log_path)
    lab.xdo("mousemove", "300", "300", "click", "3")
    code = finish(proc)
    assert code == 2
    assert "cancel: right-click" in read_log(log_path)


def test_tiny_drag_is_cancel_not_1x1_shot(lab):
    proc, log_path = start_app(lab, "tiny")
    wait_overlay(log_path)
    lab.drag(300, 300, 301, 301)
    code = finish(proc)
    assert code == 2
    assert "cancel: click-or-tiny-drag" in read_log(log_path)


def test_second_instance_refused_while_overlay_up(lab):
    proc, log_path = start_app(lab, "first")
    wait_overlay(log_path)
    try:
        second = subprocess.run(
            [sys.executable, LAUNCHER],
            env=lab.env(),
            capture_output=True,
            timeout=30,
        )
        assert second.returncode == 1
        assert b"already active" in second.stderr
    finally:
        lab.xdo("mousemove", "300", "300", "key", "Escape")
        code = finish(proc)
    assert code == 2


def test_new_capture_allowed_while_previous_serves_clipboard(lab):
    # After a capture, the process may stay alive only to serve the
    # clipboard. It must release the instance lock then: a hotkey press
    # minutes later has to open a NEW overlay, not die with "already
    # active" while no overlay exists.
    first, first_log = start_app(lab, "serving")
    wait_overlay(first_log)
    lab.drag(100, 100, 300, 250)
    wait_log(first_log, "serving clipboard in-process")
    assert first.poll() is None

    second, second_log = start_app(lab, "during-serve")
    wait_overlay(second_log)  # a second overlay came up: lock was released
    lab.xdo("mousemove", "300", "300", "key", "Escape")
    assert finish(second) == 2

    # Release the still-serving first instance and reap it.
    png = read_clipboard_png(lab, replace=True)
    assert png is not None
    assert finish(first) == 0


# -- dual-output tests ---------------------------------------------------------


def test_two_outputs_produce_one_union_capture(dual_lab):
    proc, log_path = start_app(dual_lab, "dual-session")
    text = wait_overlay(log_path)
    assert f"portal image {SCREEN_W * 2}x{SCREEN_H}" in text
    assert "overlay: 2 windows" in text
    dual_lab.xdo("mousemove", "300", "300", "key", "Escape")
    assert finish(proc) == 2


def test_cross_screen_drag_selects_across_outputs(dual_lab):
    # Start the drag on the left output and release on the right one: the
    # implicit grab keeps streaming motion to the pressed window with
    # coordinates beyond its bounds, and the selection must span the seam.
    proc, log_path = start_app(dual_lab, "cross")
    wait_overlay(log_path)
    left_x, right_x = SCREEN_W - 80, SCREEN_W + 120
    dual_lab.drag(left_x, 300, right_x, 500)
    png, code = complete_serving_capture(dual_lab, proc, log_path)
    assert code == 0
    assert f"selection: 200x200 at ({left_x}, 300)" in read_log(log_path)
    assert png is not None
    assert png_size(png) == (200, 200)
    pixel = png_pixels(png)
    assert pixel(0, 0) == ORANGE  # left of the seam: output 1
    assert pixel(199, 0) == TEAL  # right of the seam: output 2
    assert pixel(79, 100) == ORANGE
    assert pixel(80, 100) == TEAL
