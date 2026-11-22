"""The `disky` emoji pack's images (`dt-cloud emoji build`; specs/emoji-vocabulary.md §4): each vocabulary key with a
custom name (`emoji_vocab`) drawn as a 128 px transparent PNG named ``<custom>.png``, the basename Slack's upload
dialog fills in as the emoji's name. Pillow is the `emoji` extra (`uv sync --extra emoji`), imported here only.

- ``trend.<d>``: the digest's trend arrow (was `job/gen-delta-arrows.py`): green (shrinking) ↔ grey (flat) ↔ red
  (growing), saturating with the slope, a chunky constant head at every angle.
- ``runway.<n>`` / ``runway.<shape>.<n>``: a disc (or the bucket's shape) in runway colour n, the shapes as the
  site's `slack.scss` `.hue` draws them.
- ``pool.<n>.<d>``: a white arrow at d° on a rounded square in runway colour n (was cw's `job/gen-pool-chips.py`; the
  site's `SlackPage.tsx` `PoolMarkSvg`)."""
from __future__ import annotations

import colorsys
import io
import math
from pathlib import Path
from typing import TYPE_CHECKING

from .emoji_vocab import RUNWAY_COLORS, Key

if TYPE_CHECKING:
    from PIL.Image import Image

#: Slack's custom emoji: at most 128 KB, square, up to 128 px shown (it accepts up to 256 px and scales down)
SIZE = 128
MAX_BYTES = 128 * 1024
#: Supersampling before the reduction to `SIZE`
SS = 4
#: The trend arrows' full-scale angle
MAX_A = 85


def _pil():
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise SystemExit("emoji build: needs Pillow (the dt-cloud `emoji` extra: `uv sync --extra emoji`)") from None
    return Image, ImageDraw


def _arrow_color(a: float) -> tuple[int, int, int]:
    """Green (shrink) ↔ grey (flat) ↔ red (grow), saturating with |a|."""
    t = min(1.0, abs(a) / MAX_A)
    hue = (140 if a < 0 else 4) / 360
    r, g, b = colorsys.hls_to_rgb(hue, 0.60 - 0.14 * t, 0.72 * t)
    return round(r * 255), round(g * 255), round(b * 255)


def _arrow_geometry(a: float, size: float) -> tuple[tuple[float, float], tuple[float, float], list[tuple[float, float]]]:
    """(tail, tip, head triangle) for the signed math angle ``a``, y-up → y-down: a square-ish glyph, thick stem and a
    big constant head (cf. Slack's ⬆️ family)."""
    c = size / 2
    rad = math.radians(a)
    dx, dy = math.cos(rad), -math.sin(rad)
    half = size * 0.36
    tail = (c - dx * half, c - dy * half)
    tip = (c + dx * half, c + dy * half)
    head_len, head_w = size * 0.40, size * 0.52
    bx, by = tip[0] - dx * head_len, tip[1] - dy * head_len
    px, py = -dy, dx
    return tail, tip, [tip, (bx + px * head_w / 2, by + py * head_w / 2), (bx - px * head_w / 2, by - py * head_w / 2)]


def arrow(a: int) -> Image:
    Image, ImageDraw = _pil()
    s = SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tail, tip, tri = _arrow_geometry(a, s)
    col = (*_arrow_color(a), 255)
    w = round(s * 0.20)
    # the shaft ends inside the head; the head covers the joint
    end = ((tail[0] + tip[0]) / 2 + (tip[0] - tail[0]) * 0.18, (tail[1] + tip[1]) / 2 + (tip[1] - tail[1]) * 0.18)
    d.line([tail, end], fill=col, width=w)
    d.ellipse([tail[0] - w / 2, tail[1] - w / 2, tail[0] + w / 2, tail[1] + w / 2], fill=col)
    d.polygon(tri, fill=col)
    return img.resize((SIZE, SIZE), Image.LANCZOS)


#: The runway shapes' outlines in a unit box (the site's `slack.scss` `.hue` clip paths)
_SHAPES = {
    "diamond": [(0.5, 0), (1, 0.5), (0.5, 1), (0, 0.5)],
    "triangle": [(0.5, 0.04), (1, 0.92), (0, 0.92)],
    "star": [(0.5, 0), (0.647, 0.298), (0.976, 0.345), (0.738, 0.577), (0.794, 0.905), (0.5, 0.75), (0.206, 0.905),
             (0.262, 0.577), (0.024, 0.345), (0.353, 0.298)],
}


def runway(n: int, shape: str = "circle") -> Image:
    Image, ImageDraw = _pil()
    s = SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = RUNWAY_COLORS[n - 1]
    if shape == "circle":
        m = s * 0.04
        d.ellipse([m, m, s - m, s - m], fill=color)
    elif shape == "square":
        m = s * 0.08
        d.rounded_rectangle([m, m, s - m, s - m], radius=round(s * 0.16), fill=color)
    else:
        d.polygon([(x * s, y * s) for x, y in _SHAPES[shape]], fill=color)
    return img.resize((SIZE, SIZE), Image.LANCZOS)


#: The trend avatars' arrow in a 100-unit box (`SlackPage.tsx` `ARROW_POINTS`)
_CHIP_ARROW = [(10, 39), (58, 39), (58, 23), (90, 50), (58, 77), (58, 61), (10, 61)]


def pool_chip(n: int, deg: int) -> Image:
    """Drawn large (8×), then reduced."""
    Image, ImageDraw = _pil()
    size = SIZE * 8
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=RUNWAY_COLORS[n - 1])
    mask = Image.new("L", (size, size), 0)
    u = size / 100
    # scaled 0.8 about the center, as the SVG's transform
    ImageDraw.Draw(mask).polygon([((50 + (x - 50) * .8) * u, (50 + (y - 50) * .8) * u) for x, y in _CHIP_ARROW], fill=255)
    mask = mask.rotate(deg, resample=Image.BICUBIC, center=(size / 2, size / 2))
    im.paste((255, 255, 255, 255), (0, 0), mask)
    return im.resize((SIZE, SIZE), Image.LANCZOS)


def draw(k: Key) -> Image:
    """Key ``k``'s custom image."""
    parts = k.key.split(".")
    if parts[0] == "trend":
        return arrow(int(parts[1]))
    if parts[0] == "runway":
        return runway(int(parts[-1]), parts[1] if len(parts) == 3 else "circle")
    if parts[0] == "pool":
        return pool_chip(int(parts[1]), int(parts[2]))
    raise ValueError(f"emoji build: no drawing for {k.key!r}")


def png(k: Key) -> bytes:
    """Key ``k``'s image as PNG bytes, refused past Slack's `MAX_BYTES`."""
    buf = io.BytesIO()
    draw(k).save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError(f"emoji build: {k.custom}.png is {len(data):,} bytes, past Slack's {MAX_BYTES:,}")
    return data


def build(keys: list[Key], out: Path, sheet: Path | None = None) -> list[Path]:
    """Write each key's ``<custom>.png`` under ``out`` (nothing else: the folder is what a person drags into Slack's
    upload); with ``sheet``, a contact sheet there too (every image on a light and a dark row, Slack's two themes) for
    review. Returns the emoji files written."""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for k in keys:
        p = out / f"{k.custom}.png"
        p.write_bytes(png(k))
        paths.append(p)
    if sheet is not None and keys:
        Image, _ = _pil()
        pad, cols = 6, min(len(keys), 17)
        rows = math.ceil(len(keys) / cols)
        w, h = pad + cols * (SIZE + pad), pad + rows * (SIZE + pad)
        im = Image.new("RGB", (w, 2 * h), "#f8f8f8")
        im.paste(Image.new("RGB", (w, h), "#1a1d21"), (0, h))
        for i, p in enumerate(paths):
            glyph = Image.open(p)
            x, y = pad + (i % cols) * (SIZE + pad), pad + (i // cols) * (SIZE + pad)
            im.paste(glyph, (x, y), glyph)
            im.paste(glyph, (x, h + y), glyph)
        sheet.parent.mkdir(parents=True, exist_ok=True)
        im.save(sheet)
    return paths
