"""Session detection: Wayland and missing-display failure paths."""

from skreenshot.session import check_session


def test_x11_session_ok():
    assert check_session({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0.0"}) is None


def test_x11_without_session_type_ok():
    # tty/ssh launches often have no XDG_SESSION_TYPE but a valid DISPLAY.
    assert check_session({"DISPLAY": ":0"}) is None


def test_wayland_session_rejected():
    err = check_session(
        {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
    )
    assert err is not None
    assert "wayland" in err.lower()


def test_wayland_case_insensitive():
    err = check_session({"XDG_SESSION_TYPE": "Wayland", "DISPLAY": ":0"})
    assert err is not None


def test_wayland_display_without_x_rejected():
    err = check_session({"WAYLAND_DISPLAY": "wayland-0"})
    assert err is not None
    assert "wayland" in err.lower()


def test_no_display_rejected():
    err = check_session({})
    assert err is not None
    assert "DISPLAY" in err


def test_empty_display_rejected():
    assert check_session({"DISPLAY": ""}) is not None
