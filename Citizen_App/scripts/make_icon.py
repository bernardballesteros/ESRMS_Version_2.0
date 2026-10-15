"""Draw the app icon (src/assets/icon.png, 1024x1024) from the same geometry as radar_logo.svg.

`flet build` picks up assets/icon.png for the Android launcher icon. Needs Pillow (in flet[all]).

    .venv\\Scripts\\python scripts\\make_icon.py
"""
import os

from PIL import Image, ImageDraw

SIZE, SS = 1024, 4                      # final size, supersampling factor
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "assets", "icon.png")
CYAN, BLUE, DARK, BG = (0, 217, 255), (22, 133, 255), (8, 16, 28), (12, 20, 34)

# Emblem coordinates are the SVG's 100x100 space, scaled to fill ~62% of the icon so it survives
# Android's adaptive-icon mask (which crops to the middle two thirds).
K = SIZE * SS * 0.62 / 100
OFF = (SIZE * SS - 100 * K) / 2


def p(x, y):
    return (OFF + x * K, OFF + y * K)


def ring(draw, r, color, width):
    (x0, y0), (x1, y1) = p(50 - r, 50 - r), p(50 + r, 50 + r)
    draw.ellipse((x0, y0, x1, y1), outline=color, width=round(width * K))


def line(draw, pts, color, width):
    draw.line([p(x, y) for x, y in pts], fill=color, width=round(width * K), joint="curve")
    r = width * K / 2
    for x, y in (pts[0], pts[-1]):      # round caps
        cx, cy = p(x, y)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)


def main():
    img = Image.new("RGBA", (SIZE * SS, SIZE * SS), BG + (255,))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    ring(d, 42, CYAN + (150,), 1.8)
    ring(d, 33, BLUE + (100,), 2)
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)
    for a, b in (((50, 10), (50, 18)), ((50, 82), (50, 90)), ((10, 50), (18, 50)), ((82, 50), (90, 50))):
        line(d, [a, b], CYAN, 2.5)
    (x0, y0), (x1, y1) = p(28, 28), p(72, 72)
    d.ellipse((x0, y0, x1, y1), fill=DARK, outline=CYAN, width=round(2.5 * K))
    line(d, [(34, 50), (40, 50), (44, 38), (50, 62), (56, 46), (60, 50), (66, 50)], CYAN, 3)
    cx, cy = p(50, 27)
    d.ellipse((cx - 3 * K, cy - 3 * K, cx + 3 * K, cy + 3 * K), fill=CYAN)
    img.resize((SIZE, SIZE), Image.LANCZOS).convert("RGB").save(OUT)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
