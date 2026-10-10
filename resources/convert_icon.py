# convert_icon.py
"""
Icon converter for WetSpass-M Modern QGIS plugin.
Handles both directions: PNG -> PNG+ICO and ICO -> PNG.

Usage:
    python convert_icon.py path/to/input.(png|ico)
"""

import os
import sys
from PIL import Image


QGIS_SIZES = [(32, 32), (64, 64)]          # what QGIS actually uses
ICO_SIZES  = [(16, 16), (32, 32), (48, 48),
              (64, 64), (128, 128), (256, 256)]


def convert(input_path):
    if not os.path.exists(input_path):
        print(f"Error: file not found: {input_path}")
        return False

    # Load image (works for both PNG and ICO — ICO picks the largest frame)
    img = Image.open(input_path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    out_dir = os.path.dirname(os.path.abspath(input_path))

    # ---- 1. QGIS PNG (64x64 master) --------------------------------------
    png_64 = img.copy()
    png_64.thumbnail((64, 64), Image.Resampling.LANCZOS)
    # Pad to exact 64x64 canvas so aspect ratio is preserved
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    canvas.paste(png_64,
                 ((64 - png_64.width) // 2, (64 - png_64.height) // 2),
                 png_64)
    out_png = os.path.join(out_dir, "icon.png")
    canvas.save(out_png, "PNG")
    print(f"✅ PNG saved: {out_png}  ({canvas.size[0]}x{canvas.size[1]})")

    # ---- 2. Windows ICO (multi-size, single file) ------------------------
    out_ico = os.path.join(out_dir, "icon.ico")
    # Pillow builds multi-size ICO automatically from `sizes=`
    canvas.save(out_ico, format="ICO", sizes=ICO_SIZES)
    print(f"✅ ICO saved: {out_ico}  (sizes: {ICO_SIZES})")

    return True


if __name__ == "__main__":
    if len(sys.argv) > 1:
        src = sys.argv[1]
    else:
        src = input("Enter path to PNG or ICO file: ").strip().strip('"')

    convert(src)