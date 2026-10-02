#!/usr/bin/env python3
"""
Generate every app icon the release needs, from one drawing routine.

    python3 packaging/make_icons.py

Writes into packaging/icons/:

    icon-1024.png           master artwork (also the Linux 1024px icon)
    icon-{512,256,128,64,48,32,16}.png
    icon.ico                multi-size Windows icon (16..256)
    icon.icns               macOS bundle icon

The drawing is deliberately simple and deterministic: a dark rounded
tile, a wireframe globe and two orbiting arrows -- "traffic leaving
through a rotating pool of upstreams".  No fonts, no external assets.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"

SIZE = 1024                      # master canvas
TILE = (15, 17, 23)              # #0f1117 -- panel background
TILE_EDGE = (43, 50, 69)         # #2b3245 -- border colour
GLOBE = (79, 142, 247)           # #4f8ef7 -- accent blue
GLOBE_DIM = (41, 74, 133)        # dimmed meridians
ARROW = (46, 204, 113)           # #2ecc71 -- "alive" green


def _rounded_tile(size: int) -> Image.Image:
    """Dark rounded square on a transparent background."""
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    margin = size // 16
    radius = size // 5
    d.rounded_rectangle([margin, margin, size - margin, size - margin],
                        radius=radius, fill=TILE + (255,),
                        outline=TILE_EDGE + (255,), width=max(2, size // 128))
    return tile


def _globe(draw: ImageDraw.ImageDraw, c: int, r: int, w: int) -> None:
    """Wireframe globe: outline, equator, two meridians."""
    draw.ellipse([c - r, c - r, c + r, c + r], outline=GLOBE + (255,),
                 width=w)
    # equator
    draw.line([(c - r, c), (c + r, c)], fill=GLOBE + (255,), width=w)
    # meridians: ellipses squashed horizontally
    for squash in (0.42, 0.78):
        rw = int(r * squash)
        draw.ellipse([c - rw, c - r, c + rw, c + r], outline=GLOBE_DIM + (255,),
                     width=w)
    # poles
    draw.ellipse([c - w, c - r - w // 2, c + w, c - r + w // 2],
                 fill=GLOBE + (255,))
    draw.ellipse([c - w, c + r - w // 2, c + w, c + r + w // 2],
                 fill=GLOBE + (255,))


def _arrowhead(draw: ImageDraw.ImageDraw, x: float, y: float,
               angle_deg: float, size: float) -> None:
    """Triangle pointing along `angle_deg` (0 = east)."""
    import math
    a = math.radians(angle_deg)
    tip = (x + math.cos(a) * size, y + math.sin(a) * size)
    left = (x + math.cos(a + 2.5) * size, y + math.sin(a + 2.5) * size)
    right = (x + math.cos(a - 2.5) * size, y + math.sin(a - 2.5) * size)
    draw.polygon([tip, left, right], fill=ARROW + (255,))


def _orbit_arrows(draw: ImageDraw.ImageDraw, c: int, r: int, w: int) -> None:
    """Two thick arcs around the globe, each capped with an arrowhead."""
    import math
    # top arc sweeping right (east), bottom arc sweeping left (west)
    draw.arc([c - r, c - r, c + r, c + r], start=-70, end=40,
             fill=ARROW + (255,), width=w)
    draw.arc([c - r, c - r, c + r, c + r], start=110, end=220,
             fill=ARROW + (255,), width=w)
    # arrowheads at the end of each arc (PIL angles: 0 = east, CW)
    end_a = math.radians(40)
    _arrowhead(draw, c + math.cos(end_a) * r, c + math.sin(end_a) * r,
               40 + 90, w * 3.4)
    end_b = math.radians(220)
    _arrowhead(draw, c + math.cos(end_b) * r, c + math.sin(end_b) * r,
               220 + 90, w * 3.4)


def draw_icon(size: int = SIZE) -> Image.Image:
    img = _rounded_tile(size)
    d = ImageDraw.Draw(img)
    c = size // 2
    r = int(size * 0.26)              # globe radius
    w = max(3, size // 64)            # stroke width
    _globe(d, c, r, w)
    _orbit_arrows(d, c, int(size * 0.36), w)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    master = draw_icon(SIZE)
    master.save(OUT / "icon-1024.png")

    for px in (512, 256, 128, 64, 48, 32, 16):
        master.resize((px, px), Image.LANCZOS).save(OUT / f"icon-{px}.png")

    # Windows: one .ico containing every practical size
    master.save(OUT / "icon.ico",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                       (128, 128), (256, 256)])

    # macOS: .icns (Pillow writes ICNS directly)
    master.save(OUT / "icon.icns")

    for f in sorted(OUT.iterdir()):
        print(f"  {f.name:<16} {f.stat().st_size:>9,} bytes")


if __name__ == "__main__":
    main()
