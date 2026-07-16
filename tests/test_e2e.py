"""End-to-end smoke tests: the real skreenshot binary on a bare Xvfb.

Why Xvfb and not the live display: Xvfb has no clipboard manager and no
VBoxClient, so clipboard persistence is tested honestly. On the live :0,
xfsettingsd's rescue and VirtualBox clipboard sync both mask ownership bugs.

Harness: a quadrant test pattern is painted on the Xvfb root (kept alive by
a persistent `display` process; its root pixmap dies with the client), drags
are driven with xdotool, results read back with xclip, dimensions parsed
from the PNG header and pixels checked in-process.

Run with: make e2e  (pytest -m e2e)
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest
from pnglib import png_pixels, png_size

pytestmark = pytest.mark.e2e

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "skreenshot")

SCREEN_W, SCREEN_H = 1280, 800
# Quadrant colors of the root pattern (see _make_pattern).
BLUE = (32, 64, 192)  # top-left
YELLOW = (255, 204, 0)  # top-right
GREEN = (0, 170, 68)  # bottom-left
RED = (204, 34, 0)  # bottom-right

REQUIRED_TOOLS = ["Xvfb", "xdotool", "xclip", "convert", "display", "import"]
missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
if missing:
    pytestmark = [pytest.mark.e2e, pytest.mark.skip(reason=f"missing: {missing}")]


# -- Xvfb session fixture ---------------------------------------------------


class XvfbSession:
    def __init__(self, display, tmpdir):
        self.display = display
        self.tmpdir = tmpdir
        self.procs = []
        self.holder_pids = []

    def env(self, **extra):
        env = dict(os.environ)
        env.pop("WAYLAND_DISPLAY", None)
        # test_clip.py sets QT_QPA_PLATFORM=offscreen at collection time;
        # inheriting it here would render the pattern window and the app
        # itself offscreen instead of on the Xvfb.
        env.pop("QT_QPA_PLATFORM", None)
        env.pop("QT_SCALE_FACTOR", None)
        env["XDG_SESSION_TYPE"] = "x11"
        env["DISPLAY"] = self.display
        env["XDG_RUNTIME_DIR"] = str(self.tmpdir)
        # Keep config-file creation out of the real ~/.config during e2e.
        env["XDG_CONFIG_HOME"] = str(self.tmpdir)
        env.update(extra)
        return env

    def run(self, cmd, **kw):
        kw.setdefault("timeout", 20)
        kw.setdefault("capture_output", True)
        return subprocess.run(cmd, env=self.env(), **kw)

    def clear_clipboard(self):
        """Deterministic empty clipboard: kill tracked owners, then own the
        selection with a foreground xclip and kill it (owner gone = empty).
        Only processes this harness started are ever killed."""
        for pid in self.holder_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self.holder_pids.clear()
        proc = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-quiet", "-i"],
            env=self.env(),
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        proc.stdin.write(b"scratch")
        proc.stdin.close()
        time.sleep(0.3)
        proc.terminate()
        proc.wait(timeout=5)
        assert self.read_clipboard_png() is None

    def read_clipboard_png(self):
        result = self.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
        )
        return result.stdout if result.returncode == 0 else None

    def screenshot_root(self):
        out = os.path.join(self.tmpdir, "root-grab.png")
        # png24: forces 8-bit RGB output; import otherwise writes palette
        # PNGs for low-color screens, which png_pixels does not decode.
        self.run(["import", "-window", "root", f"png24:{out}"], check=True)
        with open(out, "rb") as fh:
            return fh.read()

    def drag(self, x1, y1, x2, y2):
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        self.run(
            [
                "xdotool",
                "mousemove", str(x1), str(y1),
                "mousedown", "1",
                "mousemove", str(mx), str(my),
                "mousemove", str(x2), str(y2),
                "sleep", "0.2",
                "mouseup", "1",
            ],
            check=True,
        )

    def start_skreenshot(self, name, env_extra=None):
        log_path = os.path.join(self.tmpdir, f"{name}.log")
        if os.path.exists(log_path):
            os.unlink(log_path)
        env = self.env(SKREENSHOT_LOG=log_path, **(env_extra or {}))
        proc = subprocess.Popen(
            [sys.executable, LAUNCHER],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.procs.append(proc)
        return proc, log_path

    def wait_overlay(self, log_path, timeout=15):
        """The overlay is up once the timing line is logged."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(log_path):
                with open(log_path) as fh:
                    text = fh.read()
                if "timing: overlay visible" in text:
                    time.sleep(0.3)  # give X a beat to map and focus it
                    return text
            time.sleep(0.05)
        raise AssertionError(f"overlay never became visible; log: {log_path}")

    def finish(self, proc, log_path):
        code = proc.wait(timeout=20)
        with open(log_path) as fh:
            log = fh.read()
        m = re.search(r"clipboard held by detached pid (\d+)", log)
        if m:
            self.holder_pids.append(int(m.group(1)))
        return code, log


def _free_display():
    for n in range(99, 89, -1):
        if not os.path.exists(f"/tmp/.X{n}-lock"):
            return f":{n}"
    raise RuntimeError("no free X display number in :90-:99")


@pytest.fixture(scope="module")
def xvfb(tmp_path_factory):
    tmpdir = tmp_path_factory.mktemp("e2e")
    display = _free_display()
    xvfb_proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{SCREEN_W}x{SCREEN_H}x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    socket_path = f"/tmp/.X11-unix/X{display[1:]}"
    deadline = time.monotonic() + 10
    while not os.path.exists(socket_path):
        if time.monotonic() > deadline:
            xvfb_proc.terminate()
            raise RuntimeError("Xvfb did not come up")
        time.sleep(0.1)

    session = XvfbSession(display, str(tmpdir))

    def teardown():
        for pid in session.holder_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for proc in session.procs:
            if proc.poll() is None:
                proc.terminate()
        xvfb_proc.terminate()
        xvfb_proc.wait(timeout=10)

    # Everything below must tear down on failure too, or a broken setup
    # leaks Xvfb and pattern-window processes between runs.
    try:
        # Paint the quadrant pattern. A persistent, focus-transparent window
        # does it; a root background pixmap would die with the client that
        # set it (X frees a disconnected client's resources; only
        # xsetroot-style RetainPermanent survives, and nothing installed
        # here does that for images).
        pattern = os.path.join(str(tmpdir), "pattern.png")
        subprocess.run(
            [
                "convert", "-size", f"{SCREEN_W}x{SCREEN_H}",
                "xc:rgb(32,64,192)",
                "-fill", "rgb(255,204,0)",
                "-draw", f"rectangle {SCREEN_W // 2},0 {SCREEN_W},{SCREEN_H // 2}",
                "-fill", "rgb(0,170,68)",
                "-draw", f"rectangle 0,{SCREEN_H // 2} {SCREEN_W // 2},{SCREEN_H}",
                "-fill", "rgb(204,34,0)",
                "-draw", f"rectangle {SCREEN_W // 2},{SCREEN_H // 2} {SCREEN_W},{SCREEN_H}",
                pattern,
            ],
            check=True,
            timeout=30,
        )
        root_painter = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "tests", "pattern_window.py"), pattern],
            env=session.env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        session.procs.append(root_painter)
        # Wait until the pattern is actually on screen.
        deadline = time.monotonic() + 15
        last_error = None
        while time.monotonic() < deadline:
            try:
                shot = session.screenshot_root()
                if png_pixels(shot)(10, 10) == BLUE:
                    break
            except Exception as exc:  # noqa: BLE001 - keep polling, report last
                last_error = exc
            time.sleep(0.2)
        else:
            raise RuntimeError(
                f"root pattern never appeared (last error: {last_error})"
            )
    except BaseException:
        teardown()
        raise

    yield session
    teardown()


# -- the tests --------------------------------------------------------------


def test_drag_copies_correct_crop(xvfb):
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("basic")
    xvfb.wait_overlay(log_path)
    xvfb.drag(200, 150, 500, 350)  # 300x200 inside the blue quadrant
    code, log = xvfb.finish(proc, log_path)
    assert code == 0
    assert "selection: 300x200 at (200, 150)" in log

    png = xvfb.read_clipboard_png()
    assert png is not None, "clipboard should hold a PNG after the drag"
    assert png_size(png) == (300, 200)
    pixel = png_pixels(png)
    assert pixel(0, 0) == BLUE
    assert pixel(299, 199) == BLUE
    assert pixel(150, 100) == BLUE


def test_crop_spanning_quadrants_lands_exactly(xvfb):
    # 200x200 centered on the quadrant intersection at (640, 400): each
    # corner of the crop must be a different color, which catches any
    # off-by-offset error in both axes.
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("quadrants")
    xvfb.wait_overlay(log_path)
    xvfb.drag(540, 300, 740, 500)
    code, log = xvfb.finish(proc, log_path)
    assert code == 0

    png = xvfb.read_clipboard_png()
    assert png is not None
    assert png_size(png) == (200, 200)
    pixel = png_pixels(png)
    assert pixel(0, 0) == BLUE
    assert pixel(199, 0) == YELLOW
    assert pixel(0, 199) == GREEN
    assert pixel(199, 199) == RED
    # The exact boundary: device x=100 maps to screen x=640, first yellow px.
    assert pixel(99, 0) == BLUE
    assert pixel(100, 0) == YELLOW


def test_clipboard_survives_exit_and_holder_leaves_when_replaced(xvfb):
    # Xvfb has no clipboard manager, so this only passes if a detached
    # holder keeps serving the selection after the foreground process exits.
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("persist")
    xvfb.wait_overlay(log_path)
    xvfb.drag(100, 100, 400, 300)
    code, log = xvfb.finish(proc, log_path)
    assert code == 0
    assert proc.poll() is not None, "foreground process must have exited"

    png = xvfb.read_clipboard_png()
    assert png is not None, "paste must work after the foreground exits"
    assert png_size(png) == (300, 200)

    m = re.search(r"clipboard held by detached pid (\d+)", log)
    assert m, "holder pid should be logged"
    holder_pid = int(m.group(1))
    assert _pid_alive(holder_pid), "holder should serve until replaced"

    # Another client takes the selection over: the holder must exit
    # (SelectionClear, the xclip pattern).
    replacement = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-quiet", "-i"],
        env=xvfb.env(),
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    replacement.stdin.write(b"replacement")
    replacement.stdin.close()
    try:
        deadline = time.monotonic() + 5
        while _pid_alive(holder_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _pid_alive(holder_pid), "holder must exit once replaced"
    finally:
        replacement.terminate()
        replacement.wait(timeout=5)


def test_esc_cancels_and_leaves_clipboard_untouched(xvfb):
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("esc")
    xvfb.wait_overlay(log_path)
    xvfb.run(["xdotool", "key", "Escape"], check=True)
    code, log = xvfb.finish(proc, log_path)
    assert code == 2
    assert "cancel: esc" in log
    assert xvfb.read_clipboard_png() is None, "cancel must not touch clipboard"
    # The overlay must be gone: the root pattern shows undimmed again.
    shot = png_pixels(xvfb.screenshot_root())
    assert shot(10, 10) == BLUE


def test_right_click_cancels(xvfb):
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("rclick")
    xvfb.wait_overlay(log_path)
    xvfb.run(["xdotool", "mousemove", "300", "300", "click", "3"], check=True)
    code, log = xvfb.finish(proc, log_path)
    assert code == 2
    assert "cancel: right-click" in log
    assert xvfb.read_clipboard_png() is None


def test_tiny_drag_is_cancel_not_1x1_shot(xvfb):
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("tiny")
    xvfb.wait_overlay(log_path)
    xvfb.drag(300, 300, 301, 301)  # sub-3px: a click, not a selection
    code, log = xvfb.finish(proc, log_path)
    assert code == 2
    assert "cancel: click-or-tiny-drag" in log
    assert xvfb.read_clipboard_png() is None


def test_overlay_dims_screen_and_selection_shows_through(xvfb):
    # Mid-drag: inside the selection the frozen frame shows through
    # undimmed; outside it is darkened.
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("dim")
    xvfb.wait_overlay(log_path)
    xvfb.run(
        ["xdotool", "mousemove", "100", "100", "mousedown", "1",
         "mousemove", "500", "350"],
        check=True,
    )
    time.sleep(0.4)  # let the overlay repaint
    try:
        pixel = png_pixels(xvfb.screenshot_root())
        inside = pixel(300, 200)
        outside = pixel(900, 600)  # red quadrant, outside the selection
        assert inside == BLUE, "selection hole must show the frame undimmed"
        assert outside != RED and outside[0] < RED[0], (
            f"outside the selection must be dimmed, got {outside}"
        )
    finally:
        xvfb.run(["xdotool", "mouseup", "1"], check=True)
        code, _ = xvfb.finish(proc, log_path)
    assert code == 0


def test_scale_factor_2_crop_dimensions_and_content(xvfb):
    # Hard requirement 7: with QT_SCALE_FACTOR=2 the logical screen is
    # 640x400 but the grab is 1280x800 device pixels. xdotool moves in
    # device pixels; a 400x200 device-pixel drag must produce a 400x200
    # crop with the pixels that were actually under the rectangle.
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("dpr2", {"QT_SCALE_FACTOR": "2"})
    xvfb.wait_overlay(log_path)
    xvfb.drag(400, 200, 800, 400)  # spans the blue/yellow boundary at x=640
    code, log = xvfb.finish(proc, log_path)
    assert code == 0
    assert "dpr=2.0" in log

    png = xvfb.read_clipboard_png()
    assert png is not None
    assert png_size(png) == (400, 200), "device-pixel dims must be preserved"
    pixel = png_pixels(png)
    assert pixel(0, 0) == BLUE
    assert pixel(399, 0) == YELLOW
    # Boundary check: screen x=640 is crop x=240.
    assert pixel(239, 100) == BLUE
    assert pixel(240, 100) == YELLOW


def test_second_instance_refused_while_overlay_up(xvfb):
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("first")
    xvfb.wait_overlay(log_path)
    try:
        second = xvfb.run([sys.executable, LAUNCHER], timeout=30)
        assert second.returncode == 1
        assert b"already active" in second.stderr
    finally:
        xvfb.run(["xdotool", "key", "Escape"], check=True)
        code, _ = xvfb.finish(proc, log_path)
    assert code == 2


FOCUS_STEALER = """
import sys
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget
app = QApplication(sys.argv)
w = QWidget()
w.setGeometry(10, 10, 100, 100)
w.show()
w.raise_()
w.activateWindow()
QTimer.singleShot(1500, app.quit)
app.exec()
"""


def test_focus_loss_cancels_overlay(xvfb):
    # Hard requirement 4: if the overlay loses focus (another window is
    # activated), panels can rise above it and Esc goes dead (flameshot
    # issue 1072), so deactivation must cancel instead of soft-locking.
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("focus")
    xvfb.wait_overlay(log_path)
    xvfb.run([sys.executable, "-c", FOCUS_STEALER], check=True, timeout=30)
    code, log = xvfb.finish(proc, log_path)
    assert code == 2
    assert "cancel: focus-lost" in log
    assert xvfb.read_clipboard_png() is None


def test_overlay_timing_stays_sane(xvfb):
    xvfb.clear_clipboard()
    proc, log_path = xvfb.start_skreenshot("timing")
    log_text = xvfb.wait_overlay(log_path)
    m = re.search(r"timing: overlay visible (\d+) ms", log_text)
    assert m, "timing line must be logged"
    # ~270 ms measured on this machine; 2000 is the do-not-regress fence
    # (generous so a loaded CI box does not flake).
    assert int(m.group(1)) < 2000
    xvfb.run(["xdotool", "key", "Escape"], check=True)
    xvfb.finish(proc, log_path)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
