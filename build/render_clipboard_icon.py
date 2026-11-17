"""Draw the Feather "clipboard" icon (Run Summary button) with Pillow alone.

render_icons.py needs svglib/reportlab; this draws the same 24-unit Feather
geometry directly, supersampled, in the same colour/size/alpha scheme, for
machines where that toolchain is unavailable. Output: png/clipboard.png.
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
OUT_DIR = HERE / "png"
OUT_DIR.mkdir(exist_ok=True)

ICON_COLOR = (185, 215, 250)
SIZE = 28
SS = 16                      # supersampling factor
K = SIZE / 24.0 * SS         # Feather viewBox unit -> supersampled px
STROKE = round(2 * K)


def box(x0, y0, x1, y1):
    return [x0 * K, y0 * K, x1 * K, y1 * K]


cov = Image.new("L", (SIZE * SS, SIZE * SS), 0)
d = ImageDraw.Draw(cov)
half = 1.0  # half the 2-unit stroke, so strokes centre on the path
# Board: rounded rect (4,4)-(20,22), r=2.
d.rounded_rectangle(box(4 - half, 4 - half, 20 + half, 22 + half),
                    radius=(2 + half) * K, outline=255, width=STROKE)
# The clip covers the board's top edge between x=8 and x=16.
d.rectangle(box(8, 2, 16, 6), fill=0)
# Clip: rect (8,2)-(16,6), r=1.
d.rounded_rectangle(box(8 - half, 2 - half, 16 + half, 6 + half),
                    radius=(1 + half) * K, outline=255, width=STROKE)

alpha = cov.resize((SIZE, SIZE), Image.LANCZOS)
out = Image.new("RGBA", (SIZE, SIZE), ICON_COLOR + (0,))
out.putalpha(alpha)
out.save(OUT_DIR / "clipboard.png")
print("wrote clipboard.png")
