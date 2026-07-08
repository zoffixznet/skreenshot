"""Entry point: argument parsing, session checks, single-instance lock,
logging, the capture flow, and guaranteed teardown.

Exit codes: 0 image copied, 2 cancelled (Esc, right-click, click, focus
loss), 1 anything that went wrong (Wayland session, no display, lock held,
grab or clipboard failure).
"""

import argparse
import errno
import fcntl
import logging
import os
import re
import sys
import time

from . import __version__
from .session import check_session

log = logging.getLogger("skreenshot")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CANCELLED = 2

DEFAULT_DIM_ALPHA = 140


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="skreenshot",
        description=(
            "Freeze the screen, drag a rectangle, get it on the clipboard "
            "as a PNG. Esc cancels. X11 only."
        ),
        epilog=(
            "Environment: SKREENSHOT_LOG=FILE appends a debug log; "
            "SKREENSHOT_DIM=0..255 sets overlay dim opacity (default %d)."
            % DEFAULT_DIM_ALPHA
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log details to stderr"
    )
    parser.add_argument(
        "--hold-clipboard",
        metavar="PNG",
        help=argparse.SUPPRESS,  # internal: detached clipboard holder mode
    )
    parser.add_argument(
        "--install-hotkey",
        action="store_true",
        help="bind Shift+Super+S to skreenshot in the desktop environment",
    )
    parser.add_argument(
        "--uninstall-hotkey",
        action="store_true",
        help="remove the hotkey binding created by --install-hotkey",
    )
    parser.add_argument(
        "--de",
        choices=["xfce", "kde"],
        help="desktop environment for hotkey install (default: autodetect)",
    )
    return parser.parse_args(argv)


def setup_logging(verbose):
    handlers = []
    logfile = os.environ.get("SKREENSHOT_LOG")
    if logfile:
        handlers.append(logging.FileHandler(logfile))
    if verbose:
        handlers.append(logging.StreamHandler(sys.stderr))
    if not handlers:
        log.addHandler(logging.NullHandler())
        log.setLevel(logging.CRITICAL)
        return
    fmt = logging.Formatter("%(asctime)s skreenshot %(levelname)s %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.DEBUG)


def _lock_path():
    """Single-instance lock file path, keyed by display so independent X
    servers (e.g. the test Xvfb) do not block each other."""
    display = os.environ.get("DISPLAY", "nodisplay")
    key = re.sub(r"[^A-Za-z0-9.]", "_", display)
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not os.path.isdir(base):
        base = "/tmp"
    return os.path.join(base, f"skreenshot-{key}.lock")


def acquire_instance_lock():
    """Take a non-blocking flock. Returns the open fd (kept for process
    lifetime) or None if another overlay is already up (hard req 4: a second
    invocation must not stack a second overlay)."""
    path = _lock_path()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None
        raise
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def fail(message):
    print(f"skreenshot: error: {message}", file=sys.stderr)
    log.error("%s", message)
    return EXIT_ERROR


def run_capture(t0, verbose):
    """The whole interactive flow. Assumes session and lock checks passed."""
    from PyQt6.QtWidgets import QApplication

    from . import capture
    from .overlay import SelectionOverlay

    app = QApplication(sys.argv[:1])
    outcome = {"code": EXIT_ERROR, "message": "internal error"}

    try:
        dim_alpha = _dim_alpha_from_env()
        pixmap, union = capture.grab_virtual_desktop(app)

        def on_done(result):
            kind, payload = result
            try:
                if kind == "selected":
                    _copy_selection(pixmap, payload, verbose)
                    outcome.update(code=EXIT_OK, message=None)
                elif kind == "cancelled":
                    outcome.update(code=EXIT_CANCELLED, message=None)
                else:
                    outcome.update(code=EXIT_ERROR, message=str(payload))
            except Exception as exc:  # noqa: BLE001
                log.exception("copy failed")
                outcome.update(code=EXIT_ERROR, message=str(exc))
            finally:
                app.quit()

        overlay = SelectionOverlay(pixmap, union, on_done, dim_alpha=dim_alpha)
        overlay.show_and_activate()
        app.processEvents()
        if t0 is not None:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.info("timing: overlay visible %.0f ms after start", elapsed_ms)

        # Any exception escaping a Qt event handler must tear the overlay
        # down before the process dies (hard req 4). The overlay guards its
        # own handlers; this hook is the net for everything else.
        def excepthook(exc_type, exc, tb):
            log.error("unhandled exception", exc_info=(exc_type, exc, tb))
            outcome.update(code=EXIT_ERROR, message=str(exc))
            try:
                overlay.hide()
            finally:
                app.quit()

        sys.excepthook = excepthook

        app.exec()
    finally:
        # Belt and braces: no window may outlive this function.
        for w in QApplication.topLevelWidgets():
            w.hide()
        app.processEvents()

    if outcome["message"]:
        return fail(outcome["message"])
    return outcome["code"]


def _dim_alpha_from_env():
    raw = os.environ.get("SKREENSHOT_DIM", "")
    if not raw:
        return DEFAULT_DIM_ALPHA
    try:
        return min(255, max(0, int(raw)))
    except ValueError:
        log.warning("ignoring invalid SKREENSHOT_DIM=%r", raw)
        return DEFAULT_DIM_ALPHA


def _copy_selection(pixmap, sel, verbose):
    """Crop the frozen pixmap to the selection and hand it to the clipboard."""
    from . import clip
    from .geometry import logical_to_device

    dpr = pixmap.devicePixelRatio()
    device = logical_to_device(sel, dpr)
    image = pixmap.toImage().copy(device.x, device.y, device.w, device.h)
    png = clip.encode_png(image)
    log.info(
        "copy: %dx%d device px (dpr=%s), %d PNG bytes",
        image.width(),
        image.height(),
        dpr,
        len(png),
    )
    clip.copy_png(png, verbose=verbose)


def main(argv=None, t0=None):
    if t0 is None:
        t0 = time.monotonic()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging(args.verbose)

    if args.hold_clipboard:
        from . import clip

        return clip.hold_clipboard_main(args.hold_clipboard)

    if args.install_hotkey or args.uninstall_hotkey:
        from . import hotkey

        try:
            if args.install_hotkey:
                return hotkey.install(de=args.de)
            return hotkey.uninstall(de=args.de)
        except hotkey.HotkeyError as exc:
            return fail(str(exc))

    error = check_session()
    if error:
        return fail(error)
    log.info("session: x11, DISPLAY=%s", os.environ.get("DISPLAY"))

    lock_fd = acquire_instance_lock()
    if lock_fd is None:
        return fail("another skreenshot overlay is already active")

    try:
        return run_capture(t0, args.verbose)
    except Exception as exc:  # noqa: BLE001 - top-level guard, no tracebacks
        log.exception("fatal")
        return fail(str(exc))
    finally:
        os.close(lock_fd)
