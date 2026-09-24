"""Rasterize Feather SVG icons to PNGs matching the HUD's palette.

Run once at mod-build time; the mod itself ships only the resulting PNGs,
never touches SVG/Cairo at runtime.
"""
import re
from pathlib import Path

from PIL import Image
from reportlab.graphics import renderPM
from svglib.svglib import svg2rlg

HERE = Path(__file__).parent
SVG_DIR = HERE / "svg"
OUT_DIR = HERE / "png"
OUT_DIR.mkdir(exist_ok=True)

# Matches render_mixin.py's launch_text / launch_value HUD colors.
ICON_COLOR = (185, 215, 250)
SIZE = 28

for svg_path in sorted(SVG_DIR.glob("*.svg")):
    raw = svg_path.read_text(encoding="utf-8")
    raw = raw.replace("currentColor", "#{:02x}{:02x}{:02x}".format(*ICON_COLOR))
    tmp_path = OUT_DIR / f"_tmp_{svg_path.name}"
    tmp_path.write_text(raw, encoding="utf-8")

    drawing = svg2rlg(str(tmp_path))
    scale = SIZE / max(drawing.width, drawing.height)
    drawing.width *= scale
    drawing.height *= scale
    drawing.scale(scale, scale)

    out_name = re.sub(r"[^a-z0-9]+", "_", svg_path.stem.lower()) + ".png"
    raw_png = OUT_DIR / f"_raw_{out_name}"
    renderPM.drawToFile(drawing, str(raw_png), fmt="PNG", bg=0x000000)
    tmp_path.unlink()

    # Rendered on a black background; anti-aliased stroke brightness ==
    # pixel coverage, so use it directly as alpha and flatten RGB to the
    # fixed icon color, turning the black background fully transparent.
    img = Image.open(raw_png).convert("RGB")
    alpha = img.convert("L")
    out = Image.new("RGBA", img.size, ICON_COLOR + (0,))
    out.putalpha(alpha)
    out.save(OUT_DIR / out_name)
    raw_png.unlink()
    print("wrote", out_name)
