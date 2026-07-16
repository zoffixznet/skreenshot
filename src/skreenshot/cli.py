"""Entry point: argument parsing, session checks, single-instance lock,
logging, the capture flow, and guaranteed teardown.

Exit codes: 0 image copied, 2 cancelled (Esc, right-click, click, focus
loss, portal permission denied), 1 anything that went wrong (no display,
lock held, grab or clipboard failure).
"""

import argparse
import errno
import fcntl
import logging
import os
import re
import sys
import time

from . import __version__, config
from .session import detect_backend, wayland_display_name, wayland_socket_error

log = logging.getLogger("skreenshot")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CANCELLED = 2


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="skreenshot",
        description=(
            "Freeze the screen, drag a rectangle, get it on the clipboard "
            "as a PNG. Esc cancels. Works on X11 and Wayland sessions."
        ),
        epilog=(
            "Config: ~/.config/skreenshot/config.yaml (save_dir, dim, log_file). "
            "SKREENSHOT_DIM and SKREENSHOT_LOG override dim/log_file for one run. "
            "Hold Shift while releasing a drag to also save the PNG to a file."
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


def setup_logging(verbose, log_file):
    handlers = []
    if log_file:
        handlers.append(logging.FileHandler(log_file))
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


def _lock_path(backend="x11"):
    """Single-instance lock file path, keyed by backend and display so
    independent servers (e.g. the test Xvfb or a nested compositor) do not
    block each other. WAYLAND_DISPLAY may be an absolute socket path since
    Wayland 1.15; the sanitizer flattens that to a usable file name."""
    if backend == "wayland":
        display = wayland_display_name()
        prefix = "wl"
    else:
        display = os.environ.get("DISPLAY", "nodisplay")
        prefix = "x11"
    key = re.sub(r"[^A-Za-z0-9.]", "_", display)
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not os.path.isdir(base):
        base = "/tmp"
    return os.path.join(base, f"skreenshot-{prefix}-{key}.lock")


def acquire_instance_lock(backend="x11"):
    """Take a non-blocking flock. Returns the open fd (kept for process
    lifetime) or None if another overlay is already up (hard req 4: a second
    invocation must not stack a second overlay)."""
    path = _lock_path(backend)
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


def run_capture(t0, verbose, cfg, backend="x11", release_lock=None):
    """The whole interactive flow. Assumes session and lock checks passed.

    release_lock (from main) releases the single-instance lock early when the
    Wayland path stays alive only to serve the clipboard: at that point no
    overlay exists, so a new invocation must be allowed to start one."""
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication

    from . import capture, clip

    app = QApplication(sys.argv[:1])
    # App identity for the portal's permission store and xdg-activation;
    # without it every unsandboxed app shares one anonymous grant bucket.
    QGuiApplication.setDesktopFileName("skreenshot")
    outcome = {"code": EXIT_ERROR, "message": "internal error"}

    try:
        if backend == "wayland":
            from .portal import PortalCancelled

            try:
                pixmap, union = capture.grab_wayland(app)
            except PortalCancelled as exc:
                log.info("portal: %s", exc)
                return EXIT_CANCELLED
        else:
            pixmap, union = capture.grab_virtual_desktop(app)

        def on_done(result):
            kind = result[0]
            try:
                if kind == "selected":
                    sel = result[1]
                    save = result[2] if len(result) > 2 else False
                    png, image = _crop_to_png(pixmap, sel)
                    if backend == "wayland":
                        # Must happen inside this (release) event handler and
                        # BEFORE the overlay hides: the compositor accepts a
                        # selection only from the focused client, with a
                        # fresh input serial. Hide right after, so the
                        # overlay is still gone the instant the mouse is
                        # released, before the (slow) save dialog and
                        # persistence steps.
                        mime = clip.copy_png_wayland(png, image, app)
                        overlay.hide()
                        QApplication.processEvents()
                        # A rejection cancel arrives within one round trip;
                        # checking now (not after the dialog) tells it apart
                        # from a later legitimate replacement.
                        accepted = clip.selection_accepted(app)
                    else:
                        clip.copy_png(png, verbose=verbose)
                    outcome.update(code=EXIT_OK, message=None)
                    if save:
                        # Saving is best effort: never let a save-side failure
                        # override the successful copy's exit code.
                        try:
                            _save_png(png, cfg.save_dir)
                        except Exception:  # noqa: BLE001
                            log.exception("save failed")
                    if backend == "wayland":
                        # After the dialog, so Klipper/Mutter had time to
                        # read anyway; may serve in-process until replaced.
                        clip.wayland_finalize(
                            app, mime, png,
                            accepted=accepted,
                            release_lock=release_lock,
                        )
                elif kind == "cancelled":
                    outcome.update(code=EXIT_CANCELLED, message=None)
                else:
                    outcome.update(code=EXIT_ERROR, message=str(result[1]))
            except Exception as exc:  # noqa: BLE001
                log.exception("capture handling failed")
                outcome.update(code=EXIT_ERROR, message=str(exc))
            finally:
                app.quit()

        if backend == "wayland":
            from .wayland_overlay import WaylandSelectionManager

            overlay = WaylandSelectionManager(
                pixmap, union, on_done, dim_alpha=cfg.dim
            )
        else:
            from .overlay import SelectionOverlay

            overlay = SelectionOverlay(pixmap, union, on_done, dim_alpha=cfg.dim)
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


def _crop_to_png(pixmap, sel):
    """Crop the frozen pixmap to the selection. Returns (png_bytes, QImage)."""
    from . import clip
    from .geometry import logical_to_device

    dpr = pixmap.devicePixelRatio()
    device = logical_to_device(sel, dpr)
    image = pixmap.toImage().copy(device.x, device.y, device.w, device.h)
    png = clip.encode_png(image)
    log.info(
        "crop: %dx%d device px (dpr=%s), %d PNG bytes",
        image.width(),
        image.height(),
        dpr,
        len(png),
    )
    return png, image


def default_screenshot_name(tm):
    """Pre-filled Save-As name: screenshot-YYYY-MM-DD-HHhMMm.png (24-hour)."""
    return time.strftime("screenshot-%Y-%m-%d-%Hh%Mm.png", tm)


def _ensure_png(path):
    """Append .png unless the path already ends in .png (case-insensitive)."""
    return path if path.lower().endswith(".png") else path + ".png"


def _write_png(png, path):
    """Write PNG bytes to path (ensuring a .png suffix). Returns the final path,
    or None if the write failed (already reported to stderr)."""
    path = _ensure_png(path)
    try:
        with open(path, "wb") as fh:
            fh.write(png)
        log.info("save: wrote %d bytes to %s", len(png), path)
        return path
    except OSError as exc:
        print(f"skreenshot: could not save {path}: {exc}", file=sys.stderr)
        log.error("save failed: %s", exc)
        return None


def _save_png(png, save_dir):
    """Show a Save-As dialog seeded from save_dir and write the PNG there.

    Best effort: a cancel or an error only affects the on-disk copy, never the
    exit code (the clipboard copy already happened). setDefaultSuffix keeps the
    dialog's overwrite confirmation on the final .png name.
    """
    from PyQt6.QtWidgets import QFileDialog

    start_dir = save_dir if os.path.isdir(save_dir) else os.path.expanduser("~")
    dialog = QFileDialog(None, "Save screenshot", start_dir, "PNG image (*.png)")
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setDefaultSuffix("png")
    dialog.selectFile(default_screenshot_name(time.localtime()))
    if not dialog.exec():
        log.info("save: dialog cancelled")
        return
    selected = dialog.selectedFiles()
    if selected:
        _write_png(png, selected[0])


def main(argv=None, t0=None):
    if t0 is None:
        t0 = time.monotonic()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = config.load()
    setup_logging(args.verbose, cfg.log_file)

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

    backend, error = detect_backend()
    if error:
        return fail(error)
    if backend == "wayland":
        # A dead compositor socket would make Qt abort the process (qFatal);
        # fail with a normal error line instead, before Qt loads.
        error = wayland_socket_error()
        if error:
            return fail(error)
        # Run as a native Wayland client (an XWayland grab would be black);
        # a user-set QT_QPA_PLATFORM always wins.
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
        log.info(
            "session: wayland, WAYLAND_DISPLAY=%s, QT_QPA_PLATFORM=%s",
            os.environ.get("WAYLAND_DISPLAY"),
            os.environ.get("QT_QPA_PLATFORM"),
        )
    else:
        log.info("session: x11, DISPLAY=%s", os.environ.get("DISPLAY"))

    lock_fd = acquire_instance_lock(backend)
    if lock_fd is None:
        return fail("another skreenshot overlay is already active")

    released = [False]

    def release_lock():
        # Idempotent: called early by the Wayland clipboard-serving path
        # (no overlay exists anymore, so a new invocation must not be
        # refused) and again by the finally below.
        if not released[0]:
            released[0] = True
            os.close(lock_fd)

    try:
        return run_capture(t0, args.verbose, cfg, backend, release_lock)
    except Exception as exc:  # noqa: BLE001 - top-level guard, no tracebacks
        log.exception("fatal")
        return fail(str(exc))
    finally:
        release_lock()
