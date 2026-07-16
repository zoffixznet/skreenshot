"""Clipboard mime composition and holder plumbing.

Runs Qt with QT_QPA_PLATFORM=offscreen so no display is needed.
"""

import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QGuiApplication, QImage  # noqa: E402

from skreenshot import clip  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


@pytest.fixture()
def image(qapp):
    img = QImage(8, 4, QImage.Format.Format_RGB32)
    img.fill(0xFFCC2200)
    return img


class TestEncodePng:
    def test_produces_png_bytes(self, image):
        png = clip.encode_png(image)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        back = QImage.fromData(png, "PNG")
        assert back.width() == 8 and back.height() == 4


class TestComposeMimeData:
    def test_image_png_present_with_our_bytes(self, image):
        png = clip.encode_png(image)
        data = clip.compose_mime_data(png, image)
        formats = data.formats()
        assert "image/png" in formats
        assert bytes(data.data("image/png")) == png

    def test_force_image_copy_flag_present_and_empty(self, image):
        # Klipper's default IgnoreImages=true drops image-only copies and
        # PreventEmptyClipboard restores stale history on exit; the empty
        # x-kde-force-image-copy format is the KDE-sanctioned opt-out.
        png = clip.encode_png(image)
        data = clip.compose_mime_data(png, image)
        assert clip.FORCE_IMAGE_COPY_MIME in data.formats()
        assert bytes(data.data(clip.FORCE_IMAGE_COPY_MIME)) == b""

    def test_no_text_and_no_uri_targets(self, image):
        # xfsettingsd 4.20's clipboard rescue only fires for image-only
        # offers: image targets present, text and uri-list absent.
        png = clip.encode_png(image)
        data = clip.compose_mime_data(png, image)
        assert not data.hasText()
        assert not data.hasUrls()
        for fmt in data.formats():
            assert not fmt.startswith("text/")
            assert fmt != "UTF8_STRING"

    def test_image_data_set(self, image):
        png = clip.encode_png(image)
        data = clip.compose_mime_data(png, image)
        assert data.hasImage()

    def test_works_without_qimage(self, image):
        png = clip.encode_png(image)
        data = clip.compose_mime_data(png)
        assert "image/png" in data.formats()
        assert clip.FORCE_IMAGE_COPY_MIME in data.formats()


class TestWaylandPersistencePlan:
    """The decision table for what keeps the clipboard alive after exit.
    All inputs injected; nothing here talks to a real bus or desktop."""

    def test_klipper_wins(self):
        plan = clip.wayland_persistence_plan(
            {"XDG_CURRENT_DESKTOP": "KDE"}, klipper_running=True, wl_copy="/bin/wl-copy"
        )
        assert plan == "klipper"

    def test_gnome_without_klipper(self):
        plan = clip.wayland_persistence_plan(
            {"XDG_CURRENT_DESKTOP": "GNOME"}, klipper_running=False, wl_copy=None
        )
        assert plan == "gnome"

    def test_gnome_in_colon_list_case_insensitive(self):
        plan = clip.wayland_persistence_plan(
            {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, klipper_running=False, wl_copy=None
        )
        assert plan == "gnome"

    def test_wl_copy_on_bare_compositor(self):
        plan = clip.wayland_persistence_plan(
            {"XDG_CURRENT_DESKTOP": "sway"},
            klipper_running=False,
            wl_copy="/usr/bin/wl-copy",
        )
        assert plan == "wl-copy"

    def test_serve_when_nothing_available(self):
        plan = clip.wayland_persistence_plan(
            {}, klipper_running=False, wl_copy=None
        )
        assert plan == "serve"

    def test_kde_without_klipper_does_not_claim_klipper(self):
        # Plasma with Klipper disabled: the desktop name alone must not be
        # trusted; fall through to wl-copy/serve.
        plan = clip.wayland_persistence_plan(
            {"XDG_CURRENT_DESKTOP": "KDE"}, klipper_running=False, wl_copy=None
        )
        assert plan == "serve"

    def test_env_override_pins_the_plan(self):
        # SKREENSHOT_WL_PERSIST overrides everything (the e2e suite relies
        # on it to be independent of what the host has installed).
        plan = clip.wayland_persistence_plan(
            {"SKREENSHOT_WL_PERSIST": "serve", "XDG_CURRENT_DESKTOP": "GNOME"},
            klipper_running=True,
            wl_copy="/usr/bin/wl-copy",
        )
        assert plan == "serve"

    def test_env_override_rejects_unknown_values(self):
        plan = clip.wayland_persistence_plan(
            {"SKREENSHOT_WL_PERSIST": "bogus", "XDG_CURRENT_DESKTOP": "GNOME"},
            klipper_running=False,
            wl_copy=None,
        )
        assert plan == "gnome"


class TestWaylandLinger:
    def test_no_reader_times_out_false(self, qapp, image):
        png = clip.encode_png(image)
        data = clip.compose_mime_data_observed(png, image)
        start = time.monotonic()
        assert clip._linger_for_readers(qapp, data, grace_ms=200, idle_ms=50) is False
        assert time.monotonic() - start >= 0.15

    def test_read_ends_linger_early(self, qapp, image):
        from PyQt6.QtCore import QTimer

        png = clip.encode_png(image)
        data = clip.compose_mime_data_observed(png, image)
        # Simulate a clipboard manager pulling the data mid-linger.
        QTimer.singleShot(100, lambda: data.data("image/png"))
        start = time.monotonic()
        assert clip._linger_for_readers(qapp, data, grace_ms=5000, idle_ms=50) is True
        # Ended at ~150ms (read + idle), nowhere near the 5s grace cap.
        assert time.monotonic() - start < 2.0

    def test_serve_returns_immediately_when_not_owner(self, qapp):
        # Nothing was copied by this process: serve_until_replaced must not
        # block (ownsClipboard is False), so this returning at all IS the test.
        clip.serve_until_replaced(qapp)


class TestWaylandFinalize:
    """The persistence ladder, with every side effect recorded. The e2e
    suite only ever exercises plan=serve; these pin the other branches."""

    @pytest.fixture()
    def recorder(self, qapp, monkeypatch):
        calls = []
        monkeypatch.setattr(
            clip, "copy_via_wl_copy", lambda png: calls.append("wl-copy")
        )
        monkeypatch.setattr(
            clip, "serve_until_replaced", lambda app: calls.append("serve")
        )
        return calls

    class _FakeApp:
        """wayland_finalize only asks the app for clipboard ownership."""

        def __init__(self, owns):
            self._owns = owns

        def clipboard(self):
            app = self

            class _FakeClipboard:
                def ownsClipboard(self):
                    return app._owns

            return _FakeClipboard()

    def _finalize(self, qapp, image, plan, monkeypatch, accepted=True,
                  linger_read=None, owns=True, wl_copy_found=True,
                  release_lock=None):
        monkeypatch.setattr(clip, "wayland_persistence_plan", lambda environ=None: plan)
        if linger_read is not None:
            monkeypatch.setattr(
                clip, "_linger_for_readers", lambda app, data: linger_read
            )
        monkeypatch.setattr(
            clip.shutil, "which",
            lambda name: "/usr/bin/wl-copy" if wl_copy_found else None,
        )
        png = clip.encode_png(image)
        data = clip.compose_mime_data_observed(png, image)
        clip.wayland_finalize(
            self._FakeApp(owns), data, png,
            accepted=accepted,
            release_lock=release_lock,
        )

    def test_rejected_copy_recovers_via_wl_copy(self, qapp, image, monkeypatch, recorder):
        self._finalize(qapp, image, "klipper", monkeypatch, accepted=False)
        assert recorder == ["wl-copy"]

    def test_rejected_copy_without_wl_copy_raises(self, qapp, image, monkeypatch, recorder):
        with pytest.raises(clip.ClipboardError):
            self._finalize(
                qapp, image, "serve", monkeypatch, accepted=False, wl_copy_found=False
            )
        assert recorder == []

    def test_serve_plan_releases_lock_first(self, qapp, image, monkeypatch, recorder):
        released = []
        self._finalize(
            qapp, image, "serve", monkeypatch,
            release_lock=lambda: released.append(True),
        )
        assert recorder == ["serve"]
        assert released == [True]

    def test_wl_copy_plan_skips_when_clipboard_replaced(self, qapp, image, monkeypatch, recorder):
        self._finalize(qapp, image, "wl-copy", monkeypatch, owns=False)
        assert recorder == []

    def test_wl_copy_failure_falls_back_to_serving(self, qapp, image, monkeypatch, recorder):
        def boom(png):
            recorder.append("wl-copy")
            raise clip.ClipboardError("wl-copy exploded")

        monkeypatch.setattr(clip, "copy_via_wl_copy", boom)
        self._finalize(qapp, image, "wl-copy", monkeypatch, owns=True)
        assert recorder == ["wl-copy", "serve"]

    def test_manager_read_ends_the_flow(self, qapp, image, monkeypatch, recorder):
        self._finalize(qapp, image, "klipper", monkeypatch, linger_read=True)
        assert recorder == []

    def test_manager_silent_falls_back_to_wl_copy(self, qapp, image, monkeypatch, recorder):
        self._finalize(
            qapp, image, "gnome", monkeypatch, linger_read=False, owns=True
        )
        assert recorder == ["wl-copy"]

    def test_manager_silent_and_replaced_leaves_clipboard_alone(
        self, qapp, image, monkeypatch, recorder
    ):
        self._finalize(
            qapp, image, "klipper", monkeypatch, linger_read=False, owns=False
        )
        assert recorder == []


class TestObservedMimeData:
    def test_same_targets_as_x11_composition(self, image):
        png = clip.encode_png(image)
        data = clip.compose_mime_data_observed(png, image)
        assert "image/png" in data.formats()
        assert clip.FORCE_IMAGE_COPY_MIME in data.formats()
        assert data.hasImage()
        assert not data.hasText()
        assert not data.hasUrls()

    def test_reads_are_counted(self, image):
        png = clip.encode_png(image)
        data = clip.compose_mime_data_observed(png, image)
        assert data.read_count == 0
        got = bytes(data.data("image/png"))
        assert got == png
        assert data.read_count > 0
        assert data.last_read > 0


class TestHolderPlumbing:
    def test_holder_argv_reexecs_this_package(self):
        argv = clip.holder_argv("/run/user/1000/skreenshot-x.png")
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "skreenshot"]
        assert argv[3] == "--hold-clipboard"
        assert argv[4] == "/run/user/1000/skreenshot-x.png"

    def test_holder_env_makes_package_importable(self):
        env = clip.holder_env()
        first = env["PYTHONPATH"].split(os.pathsep)[0]
        assert os.path.isdir(os.path.join(first, "skreenshot"))

    def test_private_tmp_file_mode_and_content(self):
        path = clip._write_private_tmp(b"pngdata")
        try:
            assert (os.stat(path).st_mode & 0o777) == 0o600
            with open(path, "rb") as fh:
                assert fh.read() == b"pngdata"
        finally:
            os.unlink(path)

    def test_spawn_holder_failure_raises(self, monkeypatch):
        # A holder that dies before the handshake must raise, and the
        # temp file must be cleaned up.
        seen = {}
        real_write = clip._write_private_tmp

        def spy_write(data):
            seen["path"] = real_write(data)
            return seen["path"]

        monkeypatch.setattr(clip, "_write_private_tmp", spy_write)
        monkeypatch.setattr(
            clip, "holder_argv", lambda path: [sys.executable, "-c", "pass"]
        )
        monkeypatch.setattr(clip, "HOLDER_START_TIMEOUT", 5.0)
        with pytest.raises(clip.ClipboardError):
            clip.spawn_holder(b"not-a-png")
        assert not os.path.exists(seen["path"])
