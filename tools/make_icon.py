"""Draw the app icon (assets/app.ico): graphite tile, off-white power glyph, one green status LED.

Run: .venv\\Scripts\\python.exe tools\\make_icon.py   (needs Pillow; build-time only)
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "app.ico"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

TILE = (28, 28, 30, 255)        # theme.SURFACE
EDGE = (52, 52, 56, 255)        # hairline highlight
GLYPH = (237, 237, 235, 255)    # theme.TEXT
LED = (60, 199, 126, 255)       # theme.ACCENT


def draw(size: int) -> Image.Image:
    s = 1024                                     # draw large, downsample (clean anti-aliasing)
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pad = 40
    d.rounded_rectangle((pad, pad, s - pad, s - pad), radius=210, fill=TILE, outline=EDGE, width=14)
    cx, cy, r = s // 2, s // 2 + 30, 250
    w = 76 if size >= 32 else 104                # thicker strokes survive 16-24 px
    d.arc((cx - r, cy - r, cx + r, cy + r), start=-60, end=240, fill=GLYPH, width=w)
    d.rounded_rectangle((cx - w // 2, cy - r - 70, cx + w // 2, cy - 20), radius=w // 2, fill=GLYPH)
    led_r = 70 if size >= 32 else 96
    lx, ly = s - pad - 150, pad + 150
    d.ellipse((lx - led_r, ly - led_r, lx + led_r, ly + led_r), fill=LED)
    return im.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    big = draw(256)
    big.save(OUT, format="ICO", sizes=[(n, n) for n in SIZES],
             append_images=[draw(n) for n in SIZES if n != 256])
    draw(256).save(OUT.with_suffix(".png"))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
