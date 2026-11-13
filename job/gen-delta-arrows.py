#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "pillow"]
# ///
"""Directional Δ-arrow emojis for the usage digest.

A sweep of arrows encoding each day's size delta: signed angle a ∈ [-85°, +85°]
in math convention (0° = due E = flat/no change), negative = pointing S-ward =
shrinking (green), positive = N-ward = growing (red). Chunky constant head at every angle (grey flat at 0).
Emits SVG sources + supersampled PNGs (Slack custom emoji: ≤128px, transparent),
plus a contact-sheet PNG for quick review.

Names double as the Slack emoji names (upload dialog prefills from basename):
arrow_deg-80 … arrow_deg-10, arrow_deg0, arrow_deg10 … arrow_deg80.
"""
import colorsys
import math
from pathlib import Path

from click import command, option

from PIL import Image, ImageDraw

SIZE = 128            # emoji canvas (Slack max)
SS = 4                # supersample factor for PNG antialiasing
MAX_A = 85            # angle at full-scale delta


def color(a: float) -> tuple[int, int, int]:
    """Green (shrink) ↔ grey (flat) ↔ red (grow), saturating with |a|."""
    t = min(1.0, abs(a) / MAX_A)
    hue = (140 if a < 0 else 4) / 360
    sat = 0.72 * t
    lit = 0.60 - 0.14 * t
    r, g, b = colorsys.hls_to_rgb(hue, lit, sat)
    return round(r * 255), round(g * 255), round(b * 255)


def geometry(a: float, size: float) -> tuple[tuple, tuple, list[tuple]]:
    """(tail, tip, head-triangle) for signed math-angle `a`, y-up → y-down.

    Chunky, emoji-like proportions (cf. Slack's ⬆️ family): square-ish glyph,
    thick stem, big constant head at every angle (flat grey included)."""
    c = size / 2
    rad = math.radians(a)
    dx, dy = math.cos(rad), -math.sin(rad)  # svg/PIL y-down
    half = size * 0.36
    tail = (c - dx * half, c - dy * half)
    tip = (c + dx * half, c + dy * half)
    head_len = size * 0.40
    head_w = size * 0.52
    bx, by = tip[0] - dx * head_len, tip[1] - dy * head_len  # head base center
    px, py = -dy, dx  # perpendicular
    tri = [tip, (bx + px * head_w / 2, by + py * head_w / 2), (bx - px * head_w / 2, by - py * head_w / 2)]
    return tail, tip, tri


def svg(a: float) -> str:
    tail, tip, tri = geometry(a, SIZE)
    r, g, b = color(a)
    col = f"#{r:02x}{g:02x}{b:02x}"
    w = SIZE * 0.20
    # shaft ends inside the head; head triangle covers the joint
    end = ((tail[0] + tip[0]) / 2 + (tip[0] - tail[0]) * 0.18,
           (tail[1] + tip[1]) / 2 + (tip[1] - tail[1]) * 0.18)
    pts = ' '.join(f"{x:.1f},{y:.1f}" for x, y in tri)
    parts = [f'<line x1="{tail[0]:.1f}" y1="{tail[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
             f'stroke="{col}" stroke-width="{w:.1f}" stroke-linecap="round"/>',
             f'<polygon points="{pts}" fill="{col}"/>']
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">' + ''.join(parts) + '</svg>\n')


def png(a: float) -> Image.Image:
    s = SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tail, tip, tri = geometry(a, s)
    col = (*color(a), 255)
    w = round(s * 0.20)
    end = ((tail[0] + tip[0]) / 2 + (tip[0] - tail[0]) * 0.18,
           (tail[1] + tip[1]) / 2 + (tip[1] - tail[1]) * 0.18)
    d.line([tail, end], fill=col, width=w)
    d.ellipse([tail[0] - w / 2, tail[1] - w / 2, tail[0] + w / 2, tail[1] + w / 2], fill=col)
    d.polygon(tri, fill=col)
    return img.resize((SIZE, SIZE), Image.LANCZOS)


def name(a: int) -> str:
    # File basename == Slack emoji name (the upload dialog prefills from it).
    # Slack allows `-` in emoji names, so negatives read naturally: arrow_deg-30.
    return f"arrow_deg{a}"


@command()
@option("-m", "--max-angle", default=80, help="Sweep endpoint (±deg)")
@option("-o", "--out", type=Path, default=Path("tmp/delta-arrows"), help="Output dir (svg/, png/, sheet.png)")
@option("-s", "--step", default=10, help="Degrees between arrows (5 for the full 35-emoji set)")
def main(
    max_angle: int,
    out: Path,
    step: int,
) -> None:
    angles = list(range(-max_angle, max_angle + 1, step))
    (out / "svg").mkdir(parents=True, exist_ok=True)
    (out / "png").mkdir(parents=True, exist_ok=True)
    imgs = []
    for a in angles:
        (out / "svg" / f"{name(a)}.svg").write_text(svg(a))
        im = png(a)
        im.save(out / "png" / f"{name(a)}.png")
        imgs.append((a, im))
    # contact sheet: sweep left→right on a light + dark row (Slack themes)
    pad, cell = 6, SIZE
    wsheet = pad + len(imgs) * (cell + pad)
    sheet = Image.new("RGB", (wsheet, 2 * (cell + pad) + pad + 18), "#f8f8f8")
    dark = Image.new("RGB", (wsheet, cell + pad), "#1a1d21")
    sheet.paste(dark, (0, cell + pad + 18))
    d = ImageDraw.Draw(sheet)
    for i, (a, im) in enumerate(imgs):
        x = pad + i * (cell + pad)
        d.text((x + 2, 2), f"{a:+d}°" if a else "0°", fill="#333")
        sheet.paste(im, (x, 18), im)
        sheet.paste(im, (x, cell + pad + 18 + pad // 2), im)
    sheet.save(out / "sheet.png")
    print(f"{len(angles)} arrows → {out}/ (svg/, png/, sheet.png)")


if __name__ == "__main__":
    main()
