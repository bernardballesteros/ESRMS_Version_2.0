"""Render app/static/img/emblem.svg (the ESRMS emblem) as packaging/esrms.ico with Pillow."""
import os

from PIL import Image, ImageDraw

S = 8  # supersampling: draw at 960px for the 120-unit SVG, then downscale
HERE = os.path.dirname(os.path.abspath(__file__))


def p(*xy):
    return [v * S for v in xy]


def render():
    img = Image.new("RGBA", (120 * S, 120 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(p(1, 1, 119, 119), radius=28 * S, fill="#111622", outline="#1f293d", width=2 * S)
    # soft cyan glow, top to bottom
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for y in range(12 * S, 108 * S):
        a = int(0.3 * 255 * (1 - (y - 12 * S) / (96 * S)))
        gd.line([(12 * S, y), (108 * S, y)], fill=(0, 229, 255, a))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(p(12, 12, 108, 108), radius=20 * S, fill=255)
    img.paste(glow, (0, 0), Image.composite(glow, Image.new("RGBA", img.size), mask).getchannel("A"))
    rays = Image.new("RGBA", img.size, (0, 0, 0, 0))
    rd = ImageDraw.Draw(rays)
    for line in ((60, 26, 60, 94), (26, 60, 94, 60), (36, 36, 84, 84), (36, 84, 84, 36)):
        rd.line(p(*line), fill=(0, 229, 255, 64), width=int(4.5 * S))
    img = Image.alpha_composite(img, rays)
    d = ImageDraw.Draw(img)
    d.ellipse(p(36, 36, 84, 84), fill="#0d111a", outline="#0b9be6", width=3 * S)
    d.line(p(48, 60, 53, 60, 57, 50, 61, 68, 65, 57, 68, 60, 72, 60), fill="#00e5ff", width=int(3.5 * S), joint="curve")
    d.ellipse(p(56.5, 34.5, 63.5, 41.5), fill="#00e5ff")
    return img.resize((256, 256), Image.LANCZOS)


if __name__ == "__main__":
    out = os.path.join(HERE, "esrms.ico")
    render().save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", out)
