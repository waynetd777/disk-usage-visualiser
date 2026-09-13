#!/usr/bin/env python3
"""Draw the app's artwork: design/icon.png (the Dock icon source) and src-tauri/icons/tray@2x.png.

The icon is the app itself in miniature: a dark tile holding a treemap of nested blocks in the
level colours the app uses (red, orange, amber, green, teal, blue). Drawn with PIL rather than
exported from an SVG because no SVG rasteriser is installed here, so this file is the source of
truth for the artwork.

Lessons carried from backup-manager:
  * the tile needs real transparent corners (qlmanage flattens on white, PIL does not);
  * the tray image is a TEMPLATE: black plus alpha only, macOS tints it, so shape is the message;
  * supersample and downsample for smooth edges.

    python3 tools/make_icons.py            # then: npx tauri icon design/icon.png
    make icons                             # does both
"""

import pathlib

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = 1024
SS = 2

TILE_TOP = (47, 47, 51)
TILE_BOTTOM = (23, 23, 26)
# The level colours from src/styles.css, light variants (they read on the dark tile).
RED, ORANGE, AMBER, GREEN, TEAL, BLUE = (215, 38, 61), (232, 89, 12), (217, 154, 0), (26, 158, 75), (14, 154, 167), (30, 127, 224)


def px(v):
    return round(v * SS)


def tile(img):
    grad = Image.new("RGB", (1, px(S)))
    for y in range(px(S)):
        t = y / (px(S) - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TILE_TOP, TILE_BOTTOM)))
    grad = grad.resize((px(S), px(S)))
    mask = Image.new("L", (px(S), px(S)), 0)
    ImageDraw.Draw(mask).rounded_rectangle([px(100), px(100), px(S - 100), px(S - 100)], radius=px(186), fill=255)
    img.paste(grad, (0, 0), mask)
    sheen = Image.new("L", (1, px(S)), 0)
    for y in range(px(S)):
        t = y / (px(S) / 2)
        sheen.putpixel((0, y), max(0, round(40 * (1 - t))))
    sheen = sheen.resize((px(S), px(S)))
    sheen = Image.composite(sheen, Image.new("L", sheen.size, 0), mask)
    img.paste(Image.new("RGB", (px(S), px(S)), (255, 255, 255)), (0, 0), sheen)


def block(d, x0, y0, x1, y1, colour, r=22):
    d.rounded_rectangle([px(x0), px(y0), px(x1), px(y1)], radius=px(r), fill=colour)


def treemap(d):
    """A squarified-looking arrangement inside a 600x600 area: one big block, two medium, three small."""
    L, T, R, B = 212, 212, 812, 812
    g = 22  # gap
    # Left column: big red block with an orange child inside it.
    block(d, L, T, 560, B, RED)
    block(d, L + 52, T + 118, 560 - 52, B - 52, ORANGE, 18)
    block(d, L + 52 + 44, T + 118 + 96, 560 - 52 - 44, B - 52 - 44, AMBER, 14)
    # Right column: green on top, teal + blue below.
    block(d, 560 + g, T, R, 540, GREEN)
    block(d, 560 + g, 540 + g, 700, B, TEAL)
    block(d, 700 + g, 540 + g, R, B, BLUE)


def app_icon():
    img = Image.new("RGBA", (px(S), px(S)), (0, 0, 0, 0))
    tile(img)
    d = ImageDraw.Draw(img)
    treemap(d)
    out = ROOT / "design/icon.png"
    img.resize((S, S), Image.LANCZOS).save(out)
    print(f"wrote {out}")


def tray_icon():
    """Nested squares, black on alpha, 44px (22pt @2x). Fills ~1.32 of the 32-unit design like
    backup-manager's ring so it sits at the same optical size as the system's own items."""
    T = 44
    ss = 8
    img = Image.new("RGBA", (T * ss, T * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    black = (0, 0, 0, 255)
    w = 3.0 * ss
    o = 4.5 * ss           # outer inset
    # Outer rounded square as a stroke.
    d.rounded_rectangle([o, o, T * ss - o, T * ss - o], radius=6 * ss, outline=black, width=int(w))
    # Inner blocks: one large on the left, two stacked on the right.
    i = o + w + 3.5 * ss
    mid_x = T * ss * 0.56
    mid_y = T * ss * 0.5
    gap = 3 * ss
    d.rounded_rectangle([i, i, mid_x - gap / 2, T * ss - i], radius=2 * ss, fill=black)
    d.rounded_rectangle([mid_x + gap / 2, i, T * ss - i, mid_y - gap / 2], radius=2 * ss, fill=black)
    d.rounded_rectangle([mid_x + gap / 2, mid_y + gap / 2, T * ss - i, T * ss - i], radius=2 * ss, fill=black)
    out = ROOT / "src-tauri/icons/tray@2x.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((T, T), Image.LANCZOS).save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    app_icon()
    tray_icon()
