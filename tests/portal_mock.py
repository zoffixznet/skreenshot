"""Mock org.freedesktop.portal.Screenshot service for tests.

Implements just enough of the portal protocol: the Screenshot method returns
a Request object path derived from the caller's unique name and handle_token
(the same rule the real portal uses), then emits the Response signal on that
path with a file:// URI pointing at a prepared PNG.

Usage: portal_mock.py <png-path> [response-code] [delay-ms]

response-code 0 responds with the uri; 1 (cancelled) and 2 (failed) respond
without one; -1 never responds at all (for timeout tests). Prints "MOCK: ready"
once the service name is claimed, and serves until killed.
"""

import os
import shutil
import sys
import tempfile

from PyQt6.QtCore import (
    QCoreApplication,
    QObject,
    QTimer,
    QUrl,
    pyqtClassInfo,
    pyqtSlot,
)
from PyQt6.QtDBus import (
    QDBusAbstractAdaptor,
    QDBusConnection,
    QDBusMessage,
    QDBusObjectPath,
    QDBusVariant,
)


def sender_token(unique_name):
    return unique_name.lstrip(":").replace(".", "_")


class PortalObject(QObject):
    def __init__(self, png_path, code, delay_ms, bus):
        super().__init__()
        self.png_path = png_path
        self.code = code
        self.delay_ms = delay_ms
        self.bus = bus


@pyqtClassInfo("D-Bus Interface", "org.freedesktop.portal.Screenshot")
class ScreenshotAdaptor(QDBusAbstractAdaptor):
    @pyqtSlot("QString", "QVariantMap", QDBusMessage, result=QDBusObjectPath)
    def Screenshot(self, parent_window, options, msg):  # noqa: N802 - D-Bus name
        parent = self.parent()
        token = str(options.get("handle_token", "t"))
        path = f"/org/freedesktop/portal/desktop/request/{sender_token(msg.service())}/{token}"
        print(
            f"MOCK: Screenshot(parent={parent_window!r}, token={token!r}, "
            f"interactive={options.get('interactive')!r}) -> {path}",
            flush=True,
        )
        if parent.code >= 0:
            QTimer.singleShot(parent.delay_ms, lambda: self._respond(path))
        return QDBusObjectPath(path)

    def _respond(self, path):
        parent = self.parent()
        sig = QDBusMessage.createSignal(
            path, "org.freedesktop.portal.Request", "Response"
        )
        results = {}
        if parent.code == 0:
            # Like the real portal, each request gets its own file whose
            # ownership passes to the caller (skreenshot deletes it after
            # reading); the master pattern must survive for the next shot.
            fd, copy_path = tempfile.mkstemp(
                prefix="portal-mock-", suffix=".png",
                dir=os.path.dirname(os.path.abspath(parent.png_path)),
            )
            with os.fdopen(fd, "wb") as out, open(parent.png_path, "rb") as src:
                shutil.copyfileobj(src, out)
            results["uri"] = QDBusVariant(QUrl.fromLocalFile(copy_path).toString())
        sig.setArguments([parent.code, results])
        ok = parent.bus.send(sig)
        print(f"MOCK: Response code={parent.code} sent ok={ok}", flush=True)


def main():
    png_path = sys.argv[1]
    code = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    delay_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    app = QCoreApplication(sys.argv)
    bus = QDBusConnection.sessionBus()
    obj = PortalObject(png_path, code, delay_ms, bus)
    ScreenshotAdaptor(obj)
    if not bus.registerObject(
        "/org/freedesktop/portal/desktop",
        obj,
        QDBusConnection.RegisterOption.ExportAdaptors,
    ):
        print("MOCK: registerObject failed", flush=True)
        return 1
    if not bus.registerService("org.freedesktop.portal.Desktop"):
        print("MOCK: registerService failed", flush=True)
        return 1
    print("MOCK: ready", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
