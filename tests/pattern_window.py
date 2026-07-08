"""E2E harness helper: show a PNG fullscreen and stay alive.

The Xvfb root background cannot be painted persistently with the tools on
this box (ImageMagick's `display -window root` pixmap dies with its client;
xsetroot only does solid colors), so the e2e fixture keeps this process
running instead. The window refuses focus and lets input pass through, so
it can never steal the overlay's keyboard or clicks; skreenshot maps later
and therefore stacks above it.

Usage: python3 pattern_window.py PATTERN.PNG
Prints PATTERN-UP once the window has been exposed.
"""

import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QLabel


def main():
    app = QApplication(sys.argv[:1])
    pixmap = QPixmap(sys.argv[1])
    if pixmap.isNull():
        print("could not load pattern", file=sys.stderr)
        return 1
    label = QLabel()
    label.setPixmap(pixmap)
    label.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowDoesNotAcceptFocus
        | Qt.WindowType.WindowTransparentForInput
    )
    label.setGeometry(0, 0, pixmap.width(), pixmap.height())
    label.show()

    def announce():
        print("PATTERN-UP", flush=True)

    QTimer.singleShot(200, announce)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
