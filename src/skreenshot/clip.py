"""Clipboard: mime composition and persistence.

X11 CLIPBOARD is ownership-based: the data lives in the owning process and
dies with it. On a bare X server with no clipboard manager, Qt setMimeData
followed by exit makes `xclip -o -t image/png` fail with "target not
available". The fix is what
xclip itself does: after taking ownership, keep a background process serving
selection requests until another client takes ownership (SelectionClear),
then exit.

skreenshot does that by re-exec'ing itself in --hold-clipboard mode as a
detached process. The holder offers:

- image/png bytes explicitly (the target every consumer asks for), set
  first so it is picked first,
- the full Qt image target set via setImageData,
- an EMPTY x-kde-force-image-copy format: Klipper's default config has
  IgnoreImages=true and PreventEmptyClipboard=true, so a plain image-only
  copy is not stored and gets replaced by stale history when the copier
  exits; the flag is the KDE-sanctioned opt-out (Spectacle ExportManager).

No text and no uri-list targets are offered, which keeps Xfce 4.20's
xfsettingsd image-only clipboard rescue eligible (it requires image targets
present, text and uri-list absent). The holder staying alive until replaced
also covers Spectacle's ~2000 ms Klipper linger with room to spare.
"""

import logging
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time

log = logging.getLogger("skreenshot")

FORCE_IMAGE_COPY_MIME = "x-kde-force-image-copy"
HANDSHAKE = "SKREENSHOT-HOLDING"
# How long the parent waits for the holder to confirm it owns the clipboard.
HOLDER_START_TIMEOUT = 10.0


class ClipboardError(Exception):
    """Could not hand the image to the clipboard."""


def encode_png(image):
    """QImage -> PNG bytes."""
    from PyQt6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buf, "PNG"):
        raise ClipboardError("PNG encoding failed")
    return bytes(buf.data())


def compose_mime_data(png_bytes, image=None):
    """Build the QMimeData offer. See module docstring for why each part."""
    from PyQt6.QtCore import QByteArray, QMimeData

    data = QMimeData()
    # Set image/png first so consumers that walk the target list in offer
    # order (and Qt itself) pick the lossless bytes we encoded.
    data.setData("image/png", QByteArray(png_bytes))
    if image is not None:
        data.setImageData(image)
    data.setData(FORCE_IMAGE_COPY_MIME, QByteArray())
    return data


def holder_argv(png_path):
    """Command line that re-execs this package in --hold-clipboard mode."""
    return [sys.executable, "-m", "skreenshot", "--hold-clipboard", png_path]


def holder_env():
    """Environment for the holder: same X display, package importable."""
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + prev if prev else "")
    return env


def _write_private_tmp(png_bytes):
    """Write PNG bytes to a mode-0600 temp file, return its path."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    directory = runtime_dir if runtime_dir and os.path.isdir(runtime_dir) else None
    fd, path = tempfile.mkstemp(prefix="skreenshot-", suffix=".png", dir=directory)
    try:
        os.write(fd, png_bytes)
    finally:
        os.close(fd)
    return path


def spawn_holder(png_bytes, verbose=False):
    """Start the detached clipboard holder and wait for its handshake.

    Returns once the holder confirms it owns CLIPBOARD, so the caller can
    exit immediately afterwards without losing the copy. Raises
    ClipboardError if the holder fails to start or to take ownership.
    """
    path = _write_private_tmp(png_bytes)
    try:
        proc = subprocess.Popen(
            holder_argv(path),
            env=holder_env(),
            stdout=subprocess.PIPE,
            stderr=None if verbose else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach: survives the parent and its tty
        )
    except OSError as exc:
        os.unlink(path)
        raise ClipboardError(f"could not start clipboard holder: {exc}") from exc

    # The holder unlinks the temp file itself after reading it; the parent
    # only cleans up when the holder dies before the handshake.
    ready, _, _ = select.select([proc.stdout], [], [], HOLDER_START_TIMEOUT)
    line = proc.stdout.readline().decode(errors="replace").strip() if ready else ""
    proc.stdout.close()
    if line != HANDSHAKE:
        if proc.poll() is None:
            proc.terminate()
        if os.path.exists(path):
            os.unlink(path)
        raise ClipboardError(
            "clipboard holder failed to take clipboard ownership"
            + (f" (said: {line!r})" if line else "")
        )
    log.info("copy: clipboard held by detached pid %d", proc.pid)
    return proc.pid


def copy_via_xclip(png_bytes):
    """Fallback: delegate persistence to xclip.

    xclip forks its own background holder and serves until replaced, but it
    offers ONLY image/png: the x-kde-force-image-copy flag is lost, so
    default-config Klipper will drop the image. Fallback only.
    """
    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"],
            input=png_bytes,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClipboardError(f"xclip fallback failed: {exc}") from exc
    log.info("copy: delegated to xclip (Klipper force flag not offered)")


def copy_png(png_bytes, verbose=False):
    """Primary strategy, then fallback. Raises ClipboardError if both fail."""
    try:
        spawn_holder(png_bytes, verbose=verbose)
    except ClipboardError as exc:
        log.warning("copy: holder failed (%s), trying xclip", exc)
        copy_via_xclip(png_bytes)


# -- Wayland ------------------------------------------------------------
#
# The detached-holder trick above is X11-only: on Wayland a windowless
# process cannot take the selection at all on GNOME (no data-control
# protocol, and core set_selection requires the caller to be focused).
# Instead the MAIN process sets the clipboard while handling the mouse
# release: the release event's serial is the freshest there is, and the
# still-mapped overlay keeps this client focused - compositors (KWin and
# Mutter both) only accept a selection from the focused client, which is
# why the overlay hides only after the copy. Surviving process exit is then
# delegated to whatever the session provides, decided by
# wayland_persistence_plan():
#
# - Klipper (Plasma): reads the offer through data-control and, with the
#   x-kde-force-image-copy flag we set, stores the image in history; its
#   default PreventEmptyClipboard restores it when our source dies.
# - GNOME: Mutter's built-in clipboard manager (3.34+) re-owns the best
#   offered mimetype after the owner exits. It prefers text over images, so
#   the offer must stay image-only - which it is.
# - wl-copy: takes the selection via the data-control protocol and serves
#   from a self-daemonized child until replaced (works on KWin and wlroots
#   compositors without a clipboard manager).
# - Otherwise the process itself keeps serving until another client takes
#   the selection over - the same contract as the X11 holder, just without
#   the detach.
#
# In every case the event loop must keep spinning briefly after the copy:
# Klipper and Mutter read the pixels asynchronously through a pipe, and Qt
# only writes them when the data source receives a send - exiting
# immediately races that read. The mime object counts reads so the wait can
# end as soon as a consumer finished pulling data.


def compose_mime_data_observed(png_bytes, image=None):
    """compose_mime_data, but the returned object counts consumer reads
    (read_count / last_read) so the linger loop knows when data was pulled."""
    from PyQt6.QtCore import QByteArray, QMimeData

    class _ObservedMimeData(QMimeData):
        read_count = 0
        last_read = 0.0

        def retrieveData(self, mimetype, preferred_type):  # noqa: N802
            self.read_count += 1
            self.last_read = time.monotonic()
            return super().retrieveData(mimetype, preferred_type)

    data = _ObservedMimeData()
    data.setData("image/png", QByteArray(png_bytes))
    if image is not None:
        data.setImageData(image)
    data.setData(FORCE_IMAGE_COPY_MIME, QByteArray())
    return data


def copy_png_wayland(png_bytes, image, app):
    """Take clipboard ownership in-process, from inside the release handler.

    Returns the mime object for wayland_finalize to observe. Ownership is
    not verified here: QtWayland flips ownsClipboard() only after the event
    loop processes a rejection, so the check belongs in wayland_finalize.
    """
    from PyQt6.QtGui import QClipboard

    data = compose_mime_data_observed(png_bytes, image)
    app.clipboard().setMimeData(data, QClipboard.Mode.Clipboard)
    log.info("copy: wayland clipboard set in-process")
    return data


def _gnome_session(environ):
    desktops = environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    return "gnome" in desktops


def _klipper_running():
    from PyQt6.QtDBus import QDBusConnection

    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    reply = bus.interface().isServiceRegistered("org.kde.klipper")
    return bool(reply.value()) if hasattr(reply, "value") else bool(reply)


PERSISTENCE_PLANS = ("klipper", "gnome", "wl-copy", "serve")


def wayland_persistence_plan(environ=None, klipper_running=None, wl_copy=None):
    """What keeps the clipboard alive after this process exits.

    Returns "klipper", "gnome", "wl-copy" or "serve". Parameters exist for
    tests; production callers pass nothing. SKREENSHOT_WL_PERSIST overrides
    the whole decision (internal, like --hold-clipboard: the e2e suite uses
    it to pin one plan regardless of what the host machine has installed).
    """
    env = os.environ if environ is None else environ
    forced = env.get("SKREENSHOT_WL_PERSIST")
    if forced in PERSISTENCE_PLANS:
        return forced
    if klipper_running is None:
        klipper_running = _klipper_running()
    if wl_copy is None:
        wl_copy = shutil.which("wl-copy")
    if klipper_running:
        return "klipper"
    if _gnome_session(env):
        return "gnome"
    if wl_copy:
        return "wl-copy"
    return "serve"


def _linger_for_readers(app, data, grace_ms=1200, idle_ms=300):
    """Pump the loop so async clipboard managers can pull the data.

    Returns True once at least one read happened and idle_ms passed since
    the last one; False when grace_ms elapsed with no read at all.
    """
    from PyQt6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    deadline = time.monotonic() + grace_ms / 1000.0

    def tick():
        now = time.monotonic()
        if data.read_count and now - data.last_read >= idle_ms / 1000.0:
            loop.quit()
        elif now >= deadline:
            loop.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(50)
    loop.exec()
    timer.stop()
    log.info("copy: linger done, %d read(s) served", data.read_count)
    return data.read_count > 0


def selection_accepted(app, settle_ms=150):
    """Pump the loop briefly, then report whether the compositor kept our
    selection.

    A rejection (the compositor only takes selections from the focused
    client) arrives as a cancel within one round trip, so a short settle
    distinguishes it from a legitimate replacement minutes later. Must run
    right after the copy, before the save dialog can stretch the timeline.
    """
    from PyQt6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(settle_ms, loop.quit)
    loop.exec()
    return app.clipboard().ownsClipboard()


def copy_via_wl_copy(png_bytes):
    """Persistence hand-off: wl-copy re-takes the selection via data-control
    and serves it from a self-daemonized child until something replaces it."""
    try:
        subprocess.run(
            ["wl-copy", "--type", "image/png"],
            input=png_bytes,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClipboardError(f"wl-copy failed: {exc}") from exc
    log.info("copy: persistence delegated to wl-copy")


def serve_until_replaced(app):
    """Keep this process serving the selection until another client takes it
    over, then return - X11-holder semantics without the detach.

    Only called once the copy is known accepted (selection_accepted), so a
    non-owning clipboard here means a legitimate replacement, not a loss."""
    from PyQt6.QtCore import QEventLoop, QTimer
    from PyQt6.QtGui import QClipboard

    clipboard = app.clipboard()
    if not clipboard.ownsClipboard():
        log.info("copy: clipboard already replaced; nothing to serve")
        return
    print(
        "skreenshot: holding the clipboard until another copy replaces it "
        "(install wl-clipboard for the command to return immediately)",
        file=sys.stderr,
    )
    log.info("copy: serving clipboard in-process until replaced")

    loop = QEventLoop()

    def quit_if_replaced():
        if not clipboard.ownsClipboard():
            loop.quit()

    clipboard.changed.connect(
        lambda mode: mode == QClipboard.Mode.Clipboard and quit_if_replaced()
    )
    poll = QTimer()
    poll.setInterval(2000)
    poll.timeout.connect(quit_if_replaced)
    poll.start()
    loop.exec()
    poll.stop()
    log.info("copy: clipboard taken over, exiting")


def wayland_finalize(app, data, png_bytes, accepted=True, environ=None,
                     release_lock=None):
    """After copy_png_wayland (and the optional save dialog): make the copy
    outlive the process.

    accepted is selection_accepted()'s verdict from right after the copy;
    when False the compositor rejected the selection and the only recovery
    is wl-copy - failing that, this raises ClipboardError so the run exits
    1 instead of pretending the capture is on the clipboard. release_lock
    (when given) is called before any open-ended serving, so a new
    skreenshot invocation is never blocked by a process that is merely
    keeping an old copy alive; the new copy then also releases the server.
    """
    plan = wayland_persistence_plan(environ)
    log.info("copy: wayland persistence plan: %s (accepted=%s)", plan, accepted)

    def serve():
        if release_lock is not None:
            release_lock()
        serve_until_replaced(app)

    if not accepted:
        if shutil.which("wl-copy"):
            copy_via_wl_copy(png_bytes)  # its failure is fatal: nothing else can
            return
        raise ClipboardError(
            "the compositor did not accept the clipboard copy (the overlay "
            "may have lost focus); installing wl-clipboard makes copies "
            "robust against this"
        )

    if plan == "serve":
        serve()
        return

    if plan == "wl-copy":
        if not app.clipboard().ownsClipboard():
            # Someone replaced the clipboard while the save dialog was open;
            # re-setting the screenshot now would clobber the newer copy.
            log.info("copy: clipboard replaced meanwhile; leaving it alone")
            return
        try:
            copy_via_wl_copy(png_bytes)
        except ClipboardError as exc:
            # The in-process selection is still valid; serving beats losing it.
            log.warning("copy: wl-copy hand-off failed (%s); serving instead", exc)
            serve()
        return

    # klipper / gnome: a clipboard manager is expected to pull the data.
    if _linger_for_readers(app, data):
        return
    if not app.clipboard().ownsClipboard():
        log.info("copy: clipboard replaced meanwhile; leaving it alone")
        return
    # Nothing read the offer (manager missing or hung). Fall back rather
    # than silently losing the copy on exit.
    if shutil.which("wl-copy"):
        try:
            copy_via_wl_copy(png_bytes)
            return
        except ClipboardError as exc:
            log.warning("copy: wl-copy hand-off failed (%s); serving instead", exc)
    serve()


def hold_clipboard_main(png_path):
    """Entry point for --hold-clipboard mode (runs in the detached process).

    Owns CLIPBOARD and serves requests until some other client takes the
    selection over, then exits: exactly xclip's copy-then-serve behavior,
    which is the only zero-assumption way to survive process exit on X11.
    On Xfce 4.20 xfsettingsd's rescue takes ownership within ~0.5 s and the
    holder exits almost immediately; on a bare session it stays until the
    next copy. Klipper never takes ownership just to read, so the holder
    also outlives Spectacle's 2000 ms linger requirement.
    """
    try:
        with open(png_path, "rb") as fh:
            png_bytes = fh.read()
    except OSError as exc:
        print(f"skreenshot: holder could not read image: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            os.unlink(png_path)
        except OSError:
            pass

    from PyQt6.QtGui import QGuiApplication, QImage
    from PyQt6.QtGui import QClipboard
    from PyQt6.QtCore import QTimer

    app = QGuiApplication([])
    clipboard = app.clipboard()

    image = QImage.fromData(png_bytes, "PNG")
    data = compose_mime_data(png_bytes, image if not image.isNull() else None)
    clipboard.setMimeData(data, QClipboard.Mode.Clipboard)

    if not clipboard.ownsClipboard():
        print("skreenshot: holder failed to take clipboard ownership", file=sys.stderr)
        return 1

    def quit_if_replaced():
        if not clipboard.ownsClipboard():
            app.quit()

    clipboard.changed.connect(
        lambda mode: mode == QClipboard.Mode.Clipboard and quit_if_replaced()
    )
    # Belt and braces: the changed() signal is delivered from X events; poll
    # ownership too in case an event is coalesced away.
    poll = QTimer()
    poll.setInterval(2000)
    poll.timeout.connect(quit_if_replaced)
    poll.start()

    print(HANDSHAKE, flush=True)
    # Detach from the parent's pipe so the parent never blocks on us.
    sys.stdout.close()
    os.close(1)

    app.exec()
    return 0
