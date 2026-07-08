"""Session environment checks. Runs before Qt is imported so the failure
path is instant and can never flash a black overlay (hard requirement 5)."""

import os


def check_session(environ=None):
    """Return None when an X11 capture can proceed, else a one-line error.

    Wayland is out of scope for v1. Even when DISPLAY points at XWayland,
    grabWindow(0) sees only XWayland clients, not the real screen, so a
    "capture" there would be black or incomplete. Fail loudly instead.
    """
    env = os.environ if environ is None else environ
    session_type = env.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "wayland":
        return (
            "Wayland session detected; skreenshot v1 supports X11 only "
            "(set up an X11 session or use your desktop's native tool)"
        )
    if not env.get("DISPLAY"):
        if env.get("WAYLAND_DISPLAY"):
            return (
                "Wayland session detected (WAYLAND_DISPLAY set, no DISPLAY); "
                "skreenshot v1 supports X11 only"
            )
        return "no X display (DISPLAY is not set); skreenshot needs an X11 session"
    return None
