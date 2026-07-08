#!/bin/sh
# Render the icon SVG to the PNG sizes desktop entries want.
# Uses ImageMagick's convert (its MSVG renderer is enough for this SVG, so
# rsvg-convert and inkscape are not required).
# Run from anywhere: paths are relative to this script.
set -eu

here=$(dirname "$(readlink -f "$0")")
svg="$here/skreenshot.svg"

for size in 48 128 256; do
    # Rasterize oversized via -density, then downscale: convert renders the
    # SVG at its natural 256 px otherwise, which looks soft at 48 px.
    density=$((72 * size * 2 / 256))
    convert -background none -density "$density" "$svg" \
        -resize "${size}x${size}" -strip "$here/skreenshot-${size}.png"
    echo "rendered $here/skreenshot-${size}.png"
done
