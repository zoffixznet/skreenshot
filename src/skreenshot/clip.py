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
import subprocess
import sys
import tempfile

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
