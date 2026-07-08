"""Pure geometry math for skreenshot. No Qt imports, so it unit-tests trivially.

All rects are Rect(x, y, w, h) in whatever unit the caller uses. The two
units that matter:

- logical coordinates: what Qt widgets and QScreen.geometry() report. The
  overlay window, mouse events and screen layout all live here.
- device pixels: what the grabbed pixmap actually contains. On X11 the grab
  is devicePixelRatio times larger than the logical size (verified live with
  QT_SCALE_FACTOR=2: a 640x400 logical screen grabs as 1280x800 pixels).

The logical-to-device mapping follows flameshot's extendedRect (rect * dpr)
but rounds the edges instead of truncating each component, matching what
Spectacle's dprRound does, so fractional DPRs cannot shift the crop.
"""

from collections import namedtuple

Rect = namedtuple("Rect", ["x", "y", "w", "h"])

# Drags narrower/shorter than this many logical pixels are treated as an
# accidental click and cancel the capture instead of producing a 1x1 shot.
CLICK_THRESHOLD = 3


def normalize_drag(x1, y1, x2, y2):
    """Rect from two drag corners, any drag direction."""
    left, right = (x1, x2) if x1 <= x2 else (x2, x1)
    top, bottom = (y1, y2) if y1 <= y2 else (y2, y1)
    return Rect(left, top, right - left, bottom - top)


def is_click(rect, threshold=CLICK_THRESHOLD):
    """True when a drag is too small to be an intentional selection."""
    return rect.w < threshold or rect.h < threshold


def union_rect(rects):
    """Bounding box of all given rects (the virtual desktop union)."""
    if not rects:
        raise ValueError("union_rect needs at least one rect")
    left = min(r.x for r in rects)
    top = min(r.y for r in rects)
    right = max(r.x + r.w for r in rects)
    bottom = max(r.y + r.h for r in rects)
    return Rect(left, top, right - left, bottom - top)


def screen_offset(screen_rect, union):
    """Logical offset of one screen's grab inside the composited image.

    flameshot x11LegacyScreenshot: painter.drawPixmap(offset, grab) with
    offset = screen.topLeft() - union.topLeft(). DPR is uniform across
    screens on X11, so logical offsets are safe.
    """
    return (screen_rect.x - union.x, screen_rect.y - union.y)


def translate(rect, dx, dy):
    return Rect(rect.x + dx, rect.y + dy, rect.w, rect.h)


def clamp_point(x, y, bounds):
    """Clamp a point into bounds (inclusive of the far edge, for drag ends)."""
    cx = min(max(x, bounds.x), bounds.x + bounds.w)
    cy = min(max(y, bounds.y), bounds.y + bounds.h)
    return (cx, cy)


def intersect(rect, bounds):
    """Intersection of two rects; Rect with zero w/h when disjoint."""
    left = max(rect.x, bounds.x)
    top = max(rect.y, bounds.y)
    right = min(rect.x + rect.w, bounds.x + bounds.w)
    bottom = min(rect.y + rect.h, bounds.y + bounds.h)
    return Rect(left, top, max(0, right - left), max(0, bottom - top))


def logical_to_device(rect, dpr):
    """Map a logical rect to device pixels of a DPR-tagged image.

    Edges are scaled and rounded independently (round(edge * dpr)) so that
    adjacent selections stay adjacent and fractional DPR cannot produce
    off-by-one drift that accumulates with position, which is what happens
    if x/y/w/h are each truncated separately (flameshot casts to int; we
    round, like Spectacle's dprRound = round(v * dpr) / dpr, then keep the
    device-space value).
    """
    left = round(rect.x * dpr)
    top = round(rect.y * dpr)
    right = round((rect.x + rect.w) * dpr)
    bottom = round((rect.y + rect.h) * dpr)
    return Rect(left, top, right - left, bottom - top)
