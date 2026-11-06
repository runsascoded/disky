#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pillow", "click"]
# ///
"""The digest's `quota_marks: arrow-chip` emoji, `:pool-chip-N-D:`: a white arrow at D degrees on a rounded square in runway color N
(the site's `SlackPage.tsx` `PoolMarkSvg`; colors `site/src/digest/mrkdwn.ts`). Only the reachable ones: any color while growing or
flat, and a shrinking pool only red (at or over quota), step 3 (nearly full) or green."""
from pathlib import Path

from click import argument, command
from PIL import Image, ImageDraw

COLORS = ["#e01e1e", "#ec4e0c", "#f27c00", "#f2a900", "#ddc800", "#9cc41a", "#4fae34", "#1e8e4a"]
ARROW = [(10, 39), (58, 39), (58, 23), (90, 50), (58, 77), (58, 61), (10, 61)]
PX, SS = 128, 8
GROWING = [(n, d) for n in range(1, 9) for d in range(0, 81, 10)]
SHRINKING = [(n, -d) for n in (1, 3, 8) for d in range(10, 81, 10)]


def chip(n: int, deg: int) -> Image.Image:
    """Drawn large, then reduced."""
    size = PX * SS
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=COLORS[n - 1])
    arrow = Image.new("L", (size, size), 0)
    u = size / 100
    # scale 0.8 about the center, as the SVG's transform
    ImageDraw.Draw(arrow).polygon([((50 + (x - 50) * .8) * u, (50 + (y - 50) * .8) * u) for x, y in ARROW], fill=255)
    arrow = arrow.rotate(deg, resample=Image.BICUBIC, center=(size / 2, size / 2))
    im.paste((255, 255, 255, 255), (0, 0), arrow)
    return im.resize((PX, PX), Image.LANCZOS)


@command()
@argument("out", type=Path)
def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for n, d in GROWING + SHRINKING:
        chip(n, d).save(out / f"pool-chip-{n}-{d}.png")
    print(f"{len(GROWING) + len(SHRINKING)} emoji in {out}")


if __name__ == "__main__":
    main()
