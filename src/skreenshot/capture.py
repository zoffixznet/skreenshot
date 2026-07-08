"""X11 screen capture: grab first, composite per-screen grabs into one
pixmap over the virtual-desktop union (flameshot's x11LegacyScreenshot).

Freeze-frame means the grab happens before any window of ours exists, so
there is no unmap race (xfce4-screenshooter has to sleep 200 ms after hiding
its live overlay to avoid capturing its own fade-out) and no compositor
requirement.
"""

import logging

from .geometry import Rect, screen_offset, union_rect

log = logging.getLogger("skreenshot")


class CaptureError(Exception):
    """Screen grab failed (no screens, null pixmap, X error)."""


def screen_rects(screens):
    """Logical geometry of each QScreen as a plain Rect."""
    rects = []
    for s in screens:
        g = s.geometry()
        rects.append(Rect(g.x(), g.y(), g.width(), g.height()))
    return rects


def grab_virtual_desktop(app):
    """Grab all screens and composite them. Returns (pixmap, union_rect).

    The pixmap covers the whole virtual desktop in device pixels with its
    devicePixelRatio set, so drawing it at (0, 0) in a widget spanning the
    union rect fills it 1:1. union_rect is in logical coordinates.

    Do NOT grab only the primary screen with virtual-desktop coordinates:
    that produced offset captures when the primary was not the leftmost
    monitor (flameshot PR #4127). Per-screen grabs composited at
    screen.topLeft() - union.topLeft() are correct by construction.
    """
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QPainter, QPixmap
    from PyQt6.QtCore import Qt

    screens = app.screens()
    if not screens:
        raise CaptureError("no screens reported by the X server")

    rects = screen_rects(screens)
    union = union_rect(rects)

    if len(screens) == 1:
        screen = screens[0]
        pixmap = screen.grabWindow(0)
        if pixmap.isNull() or pixmap.width() == 0:
            raise CaptureError("X11 screen grab returned an empty image")
        # Qt normally tags the grab already, but be explicit like flameshot:
        # painting a DPR-tagged pixmap at logical (0,0) fills the overlay 1:1.
        pixmap.setDevicePixelRatio(screen.devicePixelRatio())
        log.info(
            "capture: single screen %dx%d device px, dpr=%s",
            pixmap.width(),
            pixmap.height(),
            pixmap.devicePixelRatio(),
        )
        return pixmap, union

    # DPR is uniform across screens on X11 (it is a global scale factor),
    # so compositing in logical coordinates is safe (flameshot's comment in
    # x11LegacyScreenshot; Spectacle's Geometry.cpp makes the same call).
    dpr = screens[0].devicePixelRatio()
    canvas = QPixmap(round(union.w * dpr), round(union.h * dpr))
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.black)

    painter = QPainter(canvas)
    grabbed_any = False
    for screen, rect in zip(screens, rects):
        grab = screen.grabWindow(0)
        if grab.isNull() or grab.width() == 0:
            log.warning("capture: screen %s grab failed, leaving it black", rect)
            continue
        grab.setDevicePixelRatio(dpr)
        dx, dy = screen_offset(rect, union)
        painter.drawPixmap(QPoint(dx, dy), grab)
        grabbed_any = True
    painter.end()

    if not grabbed_any:
        raise CaptureError("X11 screen grab returned an empty image")

    log.info(
        "capture: %d screens composited into %dx%d device px, dpr=%s",
        len(screens),
        canvas.width(),
        canvas.height(),
        dpr,
    )
    return canvas, union
