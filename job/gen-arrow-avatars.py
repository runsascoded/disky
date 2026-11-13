#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=10"]
# ///
"""Per-reply trend-arrow avatars for the Slack monthly digest (`dt-cloud
digest`). Each `av_deg{N}.png` is a solid red(grow)/green(shrink)/grey(flat)
square with a centered, chunky white arrow whose angle (and the bg colour
intensity) encode magnitude. Used as the per-message `icon_url` — legible at
Slack's ~40px avatar size, where a transparent glyph reads as noise.

STATIC assets: rendered once and hosted (job/icons/arrows/ → the
deployment's icons Pages project, `digest.yml` `icons_base`); the digest
references them by URL, it never re-renders them per run. Regenerate +
redeploy only to restyle the arrows."""
from PIL import Image, ImageDraw

GREY = (110, 118, 129)
RED = (218, 54, 51)
GREEN = (63, 185, 80)


def bg(deg: int) -> tuple[int, int, int]:
    if deg == 0:
        return GREY
    base = RED if deg > 0 else GREEN
    t = abs(deg) / 80
    return tuple(round(GREY[i] + (base[i] - GREY[i]) * t) for i in range(3))


def avatar(deg: int, S: int = 128) -> Image.Image:
    img = Image.new("RGB", (S, S), bg(deg))
    # 4x supersample for crisp edges, chunky filled arrow like the ➡️ emoji
    R = 4
    SS = S * R
    L = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(L)
    cx = cy = SS / 2
    sh = SS * 0.125                        # shaft half-thickness
    # v3: sleek + centered — long enough to read as an arrow at every angle,
    # short enough that diagonals don't crowd the frame corners (v1 was long
    # and corner-heavy at steep angles; v2 was stubby and read as a blob).
    # +0.03 shift right so the arrow's BOUNDING-RECT center coincides with
    # the frame center we rotate about — otherwise the pivot sits at the
    # arrowhead base and steep angles read as off-center.
    x0, xn, xt = SS * 0.19, SS * 0.57, SS * 0.81  # shaft start / neck / tip
    hh = SS * 0.225                        # arrowhead half-height
    d.polygon(
        [(x0, cy - sh), (xn, cy - sh), (xn, cy - hh), (xt, cy),
         (xn, cy + hh), (xn, cy + sh), (x0, cy + sh)],
        fill=(255, 255, 255, 255),
    )
    L = L.rotate(deg, resample=Image.BICUBIC, center=(cx, cy))  # CCW+ = up
    L = L.resize((S, S), Image.LANCZOS)
    img.paste(L, (0, 0), L)
    return img


def main() -> None:
    from pathlib import Path
    out = Path(__file__).parent / "icons" / "arrows"
    out.mkdir(parents=True, exist_ok=True)
    for deg in range(-80, 81, 10):
        avatar(deg).save(out / f"av_deg{deg}.png")
    print(f"wrote {len(range(-80, 81, 10))} avatars to {out}")


if __name__ == "__main__":
    main()
