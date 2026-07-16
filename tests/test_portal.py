"""Portal client: pure helpers in-process, and the full D-Bus request/response
flow against the mock portal service on a private bus.

The flow tests run client and mock in subprocesses under dbus-run-session so
the real session bus (and any real portal on it) is never involved; that also
keeps QDBusConnection.sessionBus() caching from leaking between tests.
"""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from skreenshot.portal import PortalError, request_path, sender_token, uri_to_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK = os.path.join(REPO, "tests", "portal_mock.py")


class TestHelpers:
    def test_sender_token_strips_colon_and_dots(self):
        assert sender_token(":1.42") == "1_42"

    def test_sender_token_multi_dot(self):
        assert sender_token(":1.42.7") == "1_42_7"

    def test_request_path(self):
        assert request_path(":1.5", "tok") == (
            "/org/freedesktop/portal/desktop/request/1_5/tok"
        )

    def test_uri_to_path_plain(self):
        assert uri_to_path("file:///tmp/shot.png") == "/tmp/shot.png"

    def test_uri_to_path_percent_encoded(self):
        assert uri_to_path("file:///tmp/a%20b.png") == "/tmp/a b.png"

    def test_uri_to_path_rejects_http(self):
        with pytest.raises(PortalError):
            uri_to_path("http://example.com/shot.png")

    def test_uri_to_path_rejects_relative(self):
        with pytest.raises(PortalError):
            uri_to_path("shot.png")


needs_dbus = pytest.mark.skipif(
    shutil.which("dbus-run-session") is None, reason="dbus-run-session missing"
)

CLIENT_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {src!r})
    from PyQt6.QtCore import QCoreApplication
    from skreenshot import portal
    app = QCoreApplication(sys.argv)
    try:
        path = portal.capture_fullscreen_png(timeout_ms={timeout_ms})
        with open(path, "rb") as fh:
            head = fh.read(8)
        print(f"RESULT path={{path}} magic_ok={{head == bytes.fromhex('89504e470d0a1a0a')}}")
    except portal.PortalCancelled as exc:
        print(f"RESULT cancelled: {{exc}}")
    except portal.PortalError as exc:
        print(f"RESULT error: {{exc}}")
    """
)


def run_flow(tmp_path, code, delay_ms=50, timeout_ms=15000):
    """Run mock + client under one private session bus; return client output."""
    png = tmp_path / "mockshot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nnot-really-but-fine")
    client = tmp_path / "client.py"
    client.write_text(
        CLIENT_SCRIPT.format(src=os.path.join(REPO, "src"), timeout_ms=timeout_ms)
    )
    driver = tmp_path / "driver.sh"
    # The client runs under `timeout 45` so the driver always reaches its
    # kill line before pytest's own 60s subprocess timeout SIGKILLs the
    # session leader - otherwise the backgrounded mock would be orphaned.
    driver.write_text(
        f"""#!/bin/sh
{sys.executable} {MOCK} {png} {code} {delay_ms} &
MOCK_PID=$!
i=0
while [ $i -lt 50 ]; do
    if {sys.executable} -c "
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtDBus import QDBusConnection
import sys
app = QCoreApplication(sys.argv)
bus = QDBusConnection.sessionBus()
sys.exit(0 if bus.interface().isServiceRegistered('org.freedesktop.portal.Desktop').value() else 1)
"; then break; fi
    i=$((i+1)); sleep 0.1
done
timeout 45 {sys.executable} {client}
STATUS=$?
kill $MOCK_PID 2>/dev/null
exit $STATUS
"""
    )
    result = subprocess.run(
        ["dbus-run-session", "--", "sh", str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, str(png)


@needs_dbus
class TestPortalFlow:
    def test_success_returns_readable_png(self, tmp_path):
        # The mock hands out a fresh copy per request (like the real portal),
        # so assert the returned file's content, not its exact path.
        out, _ = run_flow(tmp_path, code=0)
        assert "RESULT path=" in out
        assert "magic_ok=True" in out

    def test_user_cancel_raises_cancelled(self, tmp_path):
        out, _ = run_flow(tmp_path, code=1)
        assert "RESULT cancelled" in out

    def test_failure_code_raises_error(self, tmp_path):
        out, _ = run_flow(tmp_path, code=2)
        assert "RESULT error" in out
        assert "code 2" in out

    def test_no_response_times_out(self, tmp_path):
        out, _ = run_flow(tmp_path, code=-1, timeout_ms=1500)
        assert "RESULT error" in out
        assert "respond" in out
