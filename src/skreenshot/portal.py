"""xdg-desktop-portal Screenshot client (the Wayland capture path).

Speaks org.freedesktop.portal.Screenshot over the session bus using PyQt6's
bundled QtDBus, so it adds no dependencies. The flow follows the portal spec:

- The response does not come in the method reply. Screenshot() returns the
  object path of a Request; the actual result arrives as the Response signal
  on that Request. The request path is derivable up front from the caller's
  unique bus name and the handle_token option, and the client must subscribe
  BEFORE calling to close the race (per the spec; portals older than 0.9
  could return a different server-generated path, so the returned handle is
  checked and re-subscribed if it differs).
- Response is (code, results): 0 success with results["uri"], 1 the user
  cancelled (a permission dialog can appear: GNOME always gates the first
  non-interactive request, KDE does from Plasma 6.4; both remember the
  answer), anything else is an error.
- The uri names a PNG file the portal wrote for us. KDE and GNOME write it
  into the user's Pictures folder and nothing ever deletes it, so the caller
  must unlink it after reading (see grab in capture.py).

The timeout is generous because the first-ever call may sit behind that
permission dialog waiting for a human.
"""

import logging
import os
from urllib.parse import unquote, urlparse

log = logging.getLogger("skreenshot")

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# Response codes from the portal spec.
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1

DEFAULT_TIMEOUT_MS = 120_000  # first call may wait on a permission dialog


class PortalError(Exception):
    """The portal call failed (no portal, D-Bus error, timeout, bad reply)."""


class PortalCancelled(Exception):
    """The user said no at the portal's permission/interaction dialog."""


def sender_token(unique_name):
    """Sender part of a request path: unique name, ':' stripped, '.' -> '_'."""
    return unique_name.lstrip(":").replace(".", "_")


def request_path(unique_name, token):
    """The Request object path the portal will use for our handle_token."""
    return f"/org/freedesktop/portal/desktop/request/{sender_token(unique_name)}/{token}"


def uri_to_path(uri):
    """file:// URI -> local path. Raises PortalError for anything else."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise PortalError(f"portal returned a non-file uri: {uri!r}")
    return unquote(parsed.path)


def _new_token():
    """A unique, object-path-safe handle token for one request."""
    return f"skreenshot_{os.getpid()}_{os.urandom(4).hex()}"


def capture_fullscreen_png(timeout_ms=None):
    """Ask the portal for a full-(virtual-)screen screenshot.

    Returns the path of the PNG file the portal wrote (caller deletes it).
    Raises PortalCancelled if the user denied it, PortalError otherwise.
    Needs a running QCoreApplication (uses a nested event loop).
    """
    from PyQt6.QtCore import QEventLoop, QObject, QTimer, pyqtSlot
    from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage

    if timeout_ms is None:
        timeout_ms = int(
            os.environ.get("SKREENSHOT_PORTAL_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)
        )

    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        raise PortalError("cannot connect to the D-Bus session bus")

    token = _new_token()
    expected = request_path(bus.baseService(), token)

    loop = QEventLoop()

    class _Catcher(QObject):
        result = None

        @pyqtSlot(QDBusMessage)
        def handle(self, msg):
            args = msg.arguments()
            code = int(args[0]) if args else -1
            results = args[1] if len(args) > 1 else {}
            self.result = (code, dict(results) if results else {})
            loop.quit()

    catcher = _Catcher()

    # Subscribe on the predicted path before calling (spec race rule).
    if not bus.connect(
        PORTAL_SERVICE, expected, REQUEST_IFACE, "Response", catcher.handle
    ):
        raise PortalError("could not subscribe to the portal response signal")

    try:
        iface = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, SCREENSHOT_IFACE, bus)
        reply = iface.call(
            "Screenshot", "", {"handle_token": token, "interactive": False}
        )
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            raise PortalError(
                "screenshot portal call failed "
                f"({reply.errorName()}: {reply.errorMessage()}); is "
                "xdg-desktop-portal with a desktop backend running?"
            )
        args = reply.arguments()
        handle = args[0] if args else None
        handle_path = handle.path() if hasattr(handle, "path") else str(handle)
        if handle_path and handle_path != expected:
            # Pre-0.9 portals return their own handle path; follow it.
            log.info("portal: request handle differs, re-subscribing")
            bus.disconnect(
                PORTAL_SERVICE, expected, REQUEST_IFACE, "Response", catcher.handle
            )
            bus.connect(
                PORTAL_SERVICE, handle_path, REQUEST_IFACE, "Response", catcher.handle
            )

        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        if catcher.result is None:
            loop.exec()
        timer.stop()
    finally:
        # Drop whichever subscription is active; failures here are harmless.
        bus.disconnect(
            PORTAL_SERVICE, expected, REQUEST_IFACE, "Response", catcher.handle
        )

    if catcher.result is None:
        raise PortalError(
            f"screenshot portal did not respond within {timeout_ms / 1000:.0f}s"
        )
    code, results = catcher.result
    log.info("portal: response code=%d keys=%s", code, sorted(results))
    if code == RESPONSE_CANCELLED:
        raise PortalCancelled("screenshot request cancelled at the portal dialog")
    if code != RESPONSE_SUCCESS:
        raise PortalError(f"screenshot portal reported failure (code {code})")
    uri = results.get("uri")
    if not uri:
        raise PortalError("screenshot portal response carried no uri")
    return uri_to_path(str(uri))
