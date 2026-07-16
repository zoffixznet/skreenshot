"""Session environment checks. Runs before Qt is imported so the failure
path is instant and can never flash a black overlay (hard requirement 5)."""

import os


def detect_backend(environ=None):
    """Decide which display backend to use. Returns (kind, error): kind is
    "x11" or "wayland" (None on failure), error is a one-line message (None
    on success).

    WAYLAND_DISPLAY is the one signal that predicts a reachable Wayland
    compositor, so it wins. XDG_SESSION_TYPE is deliberately not consulted:
    it reads "tty" over ssh even when DISPLAY points at a forwarded X server,
    and it is often missing from systemd user services and hotkey daemons.
    A DISPLAY without WAYLAND_DISPLAY is a plain X11 session (or forwarding);
    grabWindow(0) is valid there. With both set (a Wayland session offering
    XWayland) the capture must still go through the portal: X11 grabs under
    rootless XWayland see only XWayland clients and come out black.
    """
    env = os.environ if environ is None else environ
    if env.get("WAYLAND_DISPLAY"):
        return "wayland", None
    if env.get("DISPLAY"):
        return "x11", None
    return None, (
        "no display: neither WAYLAND_DISPLAY nor DISPLAY is set; "
        "skreenshot needs a Wayland or X11 session"
    )


def wayland_display_name(environ=None):
    """The Wayland display this session would connect to (for lock keying).

    Mirrors libwayland's wl_display_connect: WAYLAND_DISPLAY, else the
    literal default "wayland-0". (WAYLAND_SOCKET fd numbers are not stable
    identifiers, so they are ignored for keying.)
    """
    env = os.environ if environ is None else environ
    return env.get("WAYLAND_DISPLAY") or "wayland-0"


def wayland_socket_error(environ=None):
    """Pre-flight the compositor socket, or None when it looks connectable.

    Qt aborts the whole process (qFatal, SIGABRT) when the wayland platform
    plugin cannot connect, so a dead WAYLAND_DISPLAY must be caught before
    Qt loads. Mirrors wl_display_connect's resolution: an absolute name is
    used as-is, a relative one lives in XDG_RUNTIME_DIR. WAYLAND_SOCKET (an
    inherited fd) cannot be checked from here; its rare users skip the check.
    """
    env = os.environ if environ is None else environ
    if env.get("WAYLAND_SOCKET"):
        return None
    name = wayland_display_name(env)
    if os.path.isabs(name):
        path = name
    else:
        runtime = env.get("XDG_RUNTIME_DIR")
        if not runtime:
            return None  # let libwayland report it; nothing to check here
        path = os.path.join(runtime, name)
    if not os.path.exists(path):
        return (
            f"Wayland socket {path} does not exist; is the compositor "
            "running (WAYLAND_DISPLAY correct)?"
        )
    return None
