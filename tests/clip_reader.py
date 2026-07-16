"""Clipboard reader for the Wayland e2e suite.

Runs inside the nested session: shows a small window (which the compositor
focuses, giving this client access to the selection), polls the clipboard
for image/png, prints it as one "PNG:<base64>" line ("PNG:none" if nothing
appears), and with --replace then takes the clipboard over with text - which
releases a skreenshot that is serving its copy in the foreground.
"""

import base64
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget


def main():
    replace = "--replace" in sys.argv
    app = QApplication([a for a in sys.argv if a != "--replace"])
    w = QWidget()
    w.resize(120, 80)
    w.show()
    state = {"tries": 0}

    def attempt():
        clipboard = app.clipboard()
        mime = clipboard.mimeData()
        if mime is not None and mime.hasFormat("image/png"):
            png = bytes(mime.data("image/png"))
            print("PNG:" + base64.b64encode(png).decode(), flush=True)
            done()
            return
        state["tries"] += 1
        if state["tries"] > 60:
            print("PNG:none", flush=True)
            done()
            return
        QTimer.singleShot(250, attempt)

    def done():
        if replace:
            app.clipboard().setText("clipboard-replaced-by-reader")
            # Give the replaced owner a moment to see the takeover before the
            # reader (and with it the replacement selection) goes away.
            QTimer.singleShot(1000, app.quit)
        else:
            app.quit()

    QTimer.singleShot(600, attempt)
    QTimer.singleShot(40000, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
