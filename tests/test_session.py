"""Backend detection: which display server to use, and the failure path."""

from skreenshot.session import (
    detect_backend,
    wayland_display_name,
    wayland_socket_error,
)


def test_x11_session():
    assert detect_backend({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0.0"}) == (
        "x11",
        None,
    )


def test_x11_without_session_type():
    # tty/ssh launches often have no XDG_SESSION_TYPE but a valid DISPLAY.
    assert detect_backend({"DISPLAY": ":0"}) == ("x11", None)


def test_wayland_session():
    kind, error = detect_backend(
        {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
    )
    assert kind == "wayland"
    assert error is None


def test_wayland_wins_over_xwayland_display():
    # A Wayland session offering XWayland has both set; X11 grabs there see
    # only XWayland clients, so the wayland backend must win.
    kind, _ = detect_backend({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
    assert kind == "wayland"


def test_session_type_alone_is_not_trusted():
    # XDG_SESSION_TYPE says wayland but no compositor socket is reachable;
    # DISPLAY works (e.g. ssh -X from a wayland desktop). Use X11.
    kind, _ = detect_backend({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":9"})
    assert kind == "x11"


def test_no_display_rejected():
    kind, error = detect_backend({})
    assert kind is None
    assert "WAYLAND_DISPLAY" in error and "DISPLAY" in error


def test_empty_display_rejected():
    kind, error = detect_backend({"DISPLAY": "", "WAYLAND_DISPLAY": ""})
    assert kind is None
    assert error is not None


def test_wayland_display_name_default():
    assert wayland_display_name({}) == "wayland-0"


def test_wayland_display_name_explicit():
    assert wayland_display_name({"WAYLAND_DISPLAY": "wayland-5"}) == "wayland-5"


def test_socket_error_when_socket_missing(tmp_path):
    err = wayland_socket_error(
        {"WAYLAND_DISPLAY": "wayland-9", "XDG_RUNTIME_DIR": str(tmp_path)}
    )
    assert err is not None
    assert "wayland-9" in err


def test_socket_ok_when_present(tmp_path):
    (tmp_path / "wayland-0").touch()
    err = wayland_socket_error(
        {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": str(tmp_path)}
    )
    assert err is None


def test_socket_absolute_path(tmp_path):
    sock = tmp_path / "compositor.sock"
    sock.touch()
    assert wayland_socket_error({"WAYLAND_DISPLAY": str(sock)}) is None
    assert wayland_socket_error({"WAYLAND_DISPLAY": str(sock) + "-gone"}) is not None


def test_socket_check_skipped_without_runtime_dir():
    # A relative display name with no XDG_RUNTIME_DIR cannot be resolved
    # here; libwayland will produce its own error.
    assert wayland_socket_error({"WAYLAND_DISPLAY": "wayland-0"}) is None


def test_socket_check_skipped_with_wayland_socket_fd():
    assert wayland_socket_error({"WAYLAND_SOCKET": "5"}) is None
