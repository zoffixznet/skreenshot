"""CLI behavior: exit codes, session errors, lock, arg parsing.

These run the real entry point in a subprocess where environment isolation
matters, and in-process where it does not.
"""

import os
import subprocess
import sys

from skreenshot import cli

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "skreenshot")


def run_cli(args=(), env_overrides=None, drop=()):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", *drop)
    }
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
    def test_wayland_session_one_error_line_nonzero(self):
        result = run_cli(
            env_overrides={"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
        )
        assert result.returncode == 1
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "wayland" in lines[0].lower()
        assert result.stdout == ""

    def test_no_display_one_error_line_nonzero(self):
        result = run_cli()
        assert result.returncode == 1
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "DISPLAY" in lines[0]

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


class TestDimAlpha:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("SKREENSHOT_DIM", raising=False)
        assert cli._dim_alpha_from_env() == cli.DEFAULT_DIM_ALPHA

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("SKREENSHOT_DIM", "120")
        assert cli._dim_alpha_from_env() == 120

    def test_clamped(self, monkeypatch):
        monkeypatch.setenv("SKREENSHOT_DIM", "999")
        assert cli._dim_alpha_from_env() == 255

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("SKREENSHOT_DIM", "dark")
        assert cli._dim_alpha_from_env() == cli.DEFAULT_DIM_ALPHA


class TestHoldClipboardErrors:
    def test_missing_file_exits_nonzero(self):
        result = run_cli(
            ["--hold-clipboard", "/nonexistent/skreenshot-test.png"],
            env_overrides={"QT_QPA_PLATFORM": "offscreen"},
        )
        assert result.returncode == 1
