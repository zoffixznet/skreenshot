"""CLI behavior: exit codes, session errors, lock, arg parsing.

These run the real entry point in a subprocess where environment isolation
matters, and in-process where it does not.
"""

import os
import subprocess
import sys
import tempfile

from skreenshot import cli

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "skreenshot")
# Keep config-file creation out of the real ~/.config during tests.
_CONFIG_HOME = tempfile.mkdtemp(prefix="skreenshot-test-config-")


def run_cli(args=(), env_overrides=None, drop=()):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", *drop)
    }
    env["XDG_CONFIG_HOME"] = _CONFIG_HOME
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, LAUNCHER, *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class TestWrongEnvironment:
    def test_no_display_one_error_line_nonzero(self):
        result = run_cli()
        assert result.returncode == 1
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "DISPLAY" in lines[0]

    def test_dead_wayland_display_fails_cleanly(self, tmp_path):
        # WAYLAND_DISPLAY set but no compositor socket behind it: the wayland
        # backend is chosen and the run must end in one error line before Qt
        # loads (Qt would abort the process), not a traceback or a hang.
        # XDG_RUNTIME_DIR is pinned so the socket pre-check actually resolves
        # a path on machines where the ambient variable is unset.
        result = run_cli(
            env_overrides={
                "WAYLAND_DISPLAY": "skreenshot-test-no-such-socket",
                "XDG_RUNTIME_DIR": str(tmp_path),
                "QT_QPA_PLATFORM": "wayland",
            }
        )
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "socket" in result.stderr.lower()

    def test_error_is_not_a_traceback(self):
        result = run_cli(env_overrides={"XDG_SESSION_TYPE": "wayland"})
        assert "Traceback" not in result.stderr


class TestArgs:
    def test_version(self):
        result = run_cli(["--version"])
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_help_mentions_env_vars(self):
        result = run_cli(["--help"])
        assert result.returncode == 0
        assert "SKREENSHOT_LOG" in result.stdout
        assert "SKREENSHOT_DIM" in result.stdout

    def test_hold_clipboard_is_hidden_but_accepted(self):
        # Internal flag: not advertised, but parses.
        args = cli.parse_args(["--hold-clipboard", "/tmp/x.png"])
        assert args.hold_clipboard == "/tmp/x.png"
        result = run_cli(["--help"])
        assert "--hold-clipboard" not in result.stdout


class TestInstanceLock:
    def test_lock_path_keyed_by_display(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0.0")
        p0 = cli._lock_path()
        monkeypatch.setenv("DISPLAY", ":99")
        p99 = cli._lock_path()
        assert p0 != p99

    def test_wayland_lock_keyed_by_wayland_display(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        p0 = cli._lock_path("wayland")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
        p1 = cli._lock_path("wayland")
        assert p0 != p1

    def test_wayland_and_x11_locks_are_independent(self, monkeypatch):
        # A wayland session with XWayland has both displays; a forced-xcb
        # run must not be blocked by (or block) the native-wayland lock.
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert cli._lock_path("x11") != cli._lock_path("wayland")

    def test_wayland_absolute_socket_path_is_flattened(self, monkeypatch):
        # Since Wayland 1.15 WAYLAND_DISPLAY may be an absolute path; the
        # lock file name must flatten its separators, not nest directories.
        monkeypatch.setenv("WAYLAND_DISPLAY", "/run/user/1000/wayland-0")
        path = cli._lock_path("wayland")
        assert os.path.basename(path) == "skreenshot-wl-_run_user_1000_wayland_0.lock"

    def test_second_acquire_fails_while_held(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setenv("DISPLAY", ":7.7")
        fd = cli.acquire_instance_lock()
        assert fd is not None
        try:
            assert cli.acquire_instance_lock() is None
        finally:
            os.close(fd)
        # Released: can be taken again.
        fd2 = cli.acquire_instance_lock()
        assert fd2 is not None
        os.close(fd2)

    def test_lock_file_records_pid(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setenv("DISPLAY", ":7.8")
        fd = cli.acquire_instance_lock()
        try:
            with open(cli._lock_path()) as fh:
                assert int(fh.read()) == os.getpid()
        finally:
            os.close(fd)


class TestSaveName:
    def test_default_screenshot_name_24h_format(self):
        import time

        tm = time.struct_time((2026, 7, 8, 18, 30, 0, 0, 0, -1))
        assert cli.default_screenshot_name(tm) == "screenshot-2026-07-08-18h30m.png"

    def test_default_screenshot_name_zero_pads(self):
        import time

        tm = time.struct_time((2026, 1, 2, 3, 4, 0, 0, 0, -1))
        assert cli.default_screenshot_name(tm) == "screenshot-2026-01-02-03h04m.png"

    def test_ensure_png_appends_when_missing(self):
        assert cli._ensure_png("/a/b/foo") == "/a/b/foo.png"

    def test_ensure_png_keeps_existing_extension(self):
        assert cli._ensure_png("/a/b/foo.png") == "/a/b/foo.png"

    def test_ensure_png_is_case_insensitive(self):
        assert cli._ensure_png("/a/b/foo.PNG") == "/a/b/foo.PNG"

    def test_write_png_writes_exact_bytes_and_adds_extension(self, tmp_path):
        out = cli._write_png(b"\x89PNG-data", str(tmp_path / "shot"))
        assert out == str(tmp_path / "shot.png")
        assert open(out, "rb").read() == b"\x89PNG-data"

    def test_write_png_reports_error_and_returns_none(self, tmp_path, capsys):
        out = cli._write_png(b"x", str(tmp_path / "no-such-dir" / "shot.png"))
        assert out is None
        assert "could not save" in capsys.readouterr().err


class TestHoldClipboardErrors:
    def test_missing_file_exits_nonzero(self):
        result = run_cli(
            ["--hold-clipboard", "/nonexistent/skreenshot-test.png"],
            env_overrides={"QT_QPA_PLATFORM": "offscreen"},
        )
        assert result.returncode == 1
