"""Screen capture: grab first, before any window of ours exists.

X11: composite per-screen grabWindow(0) grabs into one pixmap over the
virtual-desktop union (flameshot's x11LegacyScreenshot). Freeze-frame means
there is no unmap race (xfce4-screenshooter has to sleep 200 ms after hiding
its live overlay to avoid capturing its own fade-out) and no compositor
requirement.

Wayland: clients cannot read the screen; the xdg-desktop-portal Screenshot
call (portal.py) returns one PNG that every known backend (KWin, Mutter,
grim) composites over the same union-of-outputs box this module uses, at a
uniform scale equal to the largest output scale. Only the device-pixel-ratio
tag has to be derived here; the geometry model is shared with X11.
"""

import logging
import os

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

    # Grab every screen and composite. Each grab keeps its OWN device pixel
    # ratio; composite_desktop sizes the canvas at the largest DPR present, so
    # a mixed-scaling layout (a HiDPI screen beside a 1x screen) composites and
    # crops correctly, while a uniform-DPR layout stays a 1:1 copy.
    grabs = []
    for screen, rect in zip(screens, rects):
        grab = screen.grabWindow(0)
        if grab.isNull() or grab.width() == 0:
            log.warning("capture: screen %s grab failed, leaving it black", rect)
            continue
        dx, dy = screen_offset(rect, union)
        grabs.append((grab, dx, dy, screen.devicePixelRatio()))

    if not grabs:
        raise CaptureError("X11 screen grab returned an empty image")

    canvas = composite_desktop(grabs, union)
    log.info(
        "capture: %d screens composited into %dx%d device px, dpr=%s",
        len(grabs),
        canvas.width(),
        canvas.height(),
        canvas.devicePixelRatio(),
    )
    return canvas, union


def derive_portal_dpr(image_w, image_h, union):
    """Device pixel ratio of a portal screenshot relative to the logical union.

    Every studied portal backend renders the composite at max(output scale),
    so width / logical-width IS the scale. The height is checked only to warn:
    a mismatch means the backend cropped or letterboxed in a way we cannot
    correct blind, and a width-derived ratio is still the best available map.
    """
    if union.w <= 0 or union.h <= 0:
        raise CaptureError("virtual desktop union has no size")
    dpr_w = image_w / union.w
    dpr_h = image_h / union.h
    if abs(dpr_w - dpr_h) > 0.02:
        log.warning(
            "portal image %dx%d does not scale uniformly to the %dx%d union "
            "(w-ratio %.3f, h-ratio %.3f); using the width ratio",
            image_w,
            image_h,
            union.w,
            union.h,
            dpr_w,
            dpr_h,
        )
    # A near-integer ratio IS that integer: pixel-exact crops beat the
    # sub-pixel noise of integer image sizes divided by integer unions.
    nearest = round(dpr_w)
    if nearest >= 1 and abs(dpr_w - nearest) < 0.01:
        return float(nearest)
    return dpr_w


def grab_wayland(app):
    """Portal grab. Returns (pixmap, union_rect) like grab_virtual_desktop.

    The pixmap covers the whole virtual desktop with its devicePixelRatio
    set, so the overlay and crop code shared with X11 applies unchanged.
    """
    from PyQt6.QtGui import QPixmap

    from . import portal

    screens = app.screens()
    if not screens:
        raise CaptureError("no screens reported by the compositor")
    rects = screen_rects(screens)
    union = union_rect(rects)

    path = portal.capture_fullscreen_png()
    try:
        pixmap = QPixmap(path)
    finally:
        # The portal wrote the file for us (into ~/Pictures on KDE/GNOME);
        # nothing else ever deletes it.
        try:
            os.unlink(path)
        except OSError as exc:
            log.warning("capture: could not remove portal file %s: %s", path, exc)

    if pixmap.isNull() or pixmap.width() == 0:
        raise CaptureError(
            f"the desktop portal's screenshot (from {path}) was not a "
            "readable image"
        )

    dpr = derive_portal_dpr(pixmap.width(), pixmap.height(), union)
    pixmap.setDevicePixelRatio(dpr)
    log.info(
        "capture: portal image %dx%d device px over %dx%d logical union, dpr=%s",
        pixmap.width(),
        pixmap.height(),
        union.w,
        union.h,
        dpr,
    )
    return pixmap, union


def composite_desktop(grabs, union):
    """Composite per-screen grabs into one pixmap covering the union rect.

    grabs is a list of (pixmap, dx, dy, dpr): each screen's device-pixel grab,
    its logical offset within the union (screen.topLeft() - union.topLeft()),
    and that screen's own device pixel ratio. The canvas is sized at the
    largest DPR present so a high-DPI screen keeps full resolution; each grab
    is tagged with its own DPR and drawn at its logical offset, so Qt scales it
    into the shared device space. A single uniform DPR reduces to a 1:1 copy.
    """
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtGui import QPainter, QPixmap

    dpr = max((g[3] for g in grabs), default=1.0)
    canvas = QPixmap(round(union.w * dpr), round(union.h * dpr))
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.black)

    painter = QPainter(canvas)
    for grab, dx, dy, grab_dpr in grabs:
        grab.setDevicePixelRatio(grab_dpr)
        painter.drawPixmap(QPoint(dx, dy), grab)
    painter.end()
    return canvas
