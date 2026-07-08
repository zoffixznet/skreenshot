"""Clipboard mime composition and holder plumbing.

Runs Qt with QT_QPA_PLATFORM=offscreen so no display is needed.
"""

import os
import sys

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
