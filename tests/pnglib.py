"""Tiny PNG helpers shared by the e2e suites (no extra deps)."""

import struct
import zlib


def png_size(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def png_pixels(png):
    """Decode a PNG (8-bit RGB/RGBA, non-interlaced) into a pixel getter."""
    w, h = png_size(png)
    bit_depth, color_type = png[24], png[25]
    assert bit_depth == 8 and color_type in (2, 6), "unexpected PNG format"
    channels = 3 if color_type == 2 else 4
    data = b""
    pos = 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        ctype = png[pos + 4 : pos + 8]
        if ctype == b"IDAT":
            data += png[pos + 8 : pos + 8 + length]
        pos += length + 12
    raw = zlib.decompress(data)
    stride = w * channels + 1
    rows = []
    prev = bytearray(w * channels)
    for y in range(h):
        filt = raw[y * stride]
        line = bytearray(raw[y * stride + 1 : (y + 1) * stride])
        if filt == 1:  # Sub
            for i in range(channels, len(line)):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:  # Up
            for i in range(len(line)):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:  # Average
            for i in range(len(line)):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:  # Paeth
            for i in range(len(line)):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        rows.append(bytes(line))
        prev = line

    def pixel(x, y):
        o = x * channels
        return tuple(rows[y][o : o + 3])

    return pixel
