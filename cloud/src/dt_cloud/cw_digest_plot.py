"""Digest OP image for the Slack monthly thread (`dt-cloud digest`), two
stacked panels:

- **Sparkline** of every scan (12-hourly points), y-range fit to the month's
  data (a round floor below the min) but with the 1 PB quota line + label
  kept in frame — the used fill under the line and the hatched headroom band
  above it make the used/free split legible without squashing the month into
  the top of the axis (the site's "Size over time" chart with `fit`).
- **Diff treemap** over the OP headline's interval (lead-in scan → latest):
  green cells grew, red shrank, area = |Δ|, nested one level so `tmp/ttl=14d`,
  `marin/skyrl`, `users/<name>` … read as their own cells inside their
  top-level group; small cells fold into `<group>/…` and small groups into
  `other` (see `digest.tree_diff`). Layout is a hand-rolled squarify.

In-package (not a standalone `uv run` script under `job/`) so the Batch image
runs it: `digest.render_plot` imports and calls :func:`render` in-process (the
slim image has neither `uv` nor, without the `[plot]` extra, matplotlib).
Ad-hoc renders: `python -m dt_cloud.cw_digest_plot -d … -o …`.

Input: per-scan `{scan, tb}` rows + optional `DiffCell`s. Output: a PNG sized
for a Slack image block. The digest renders this per run and cache-busts the
OP's image URL so `chat.update` refetches."""
from json import load
from pathlib import Path

from click import Path as CP, command, option

BG = "#0d1117"
INK = "#c9d1d9"
DIM = "#8b949e"
GRID = "#21262d"
LINE = "#58a6ff"
FILL = "#1f6feb"
FREE = "#2ea043"
QUOTA = "#f85149"
GREW = "#2ea043"
SHRANK = "#f85149"
TIB = 1024**4

Rect = tuple[float, float, float, float]


def squarify(values: list[float], x: float, y: float, w: float, h: float) -> list[Rect]:
    """Squarified treemap layout (Bruls et al.): ``values`` (positive, laid in
    the given order — sort desc for the classic look) tile the rect
    ``(x, y, w, h)``; returns one ``(x, y, w, h)`` per value, areas
    proportional to the values. Rows go along the rect's shorter side; a row
    takes the next value while its worst aspect ratio doesn't get worse."""
    if not values:
        return []
    scale = w * h / sum(values)
    areas = [v * scale for v in values]
    out: list[Rect] = []
    i = 0
    while i < len(areas):
        side = min(w, h)
        row = [areas[i]]
        worst = _worst(row, side)
        while i + len(row) < len(areas):
            cand = row + [areas[i + len(row)]]
            cw = _worst(cand, side)
            if cw > worst:
                break
            row, worst = cand, cw
        span = sum(row) / side  # the row's thickness across the shorter side
        pos = 0.0
        for a in row:
            length = a / span
            out.append((x + pos, y, length, span) if w < h else (x, y + pos, span, length))
            pos += length
        if w < h:
            y, h = y + span, h - span
        else:
            x, w = x + span, w - span
        i += len(row)
    return out


def _worst(row: list[float], side: float) -> float:
    span = sum(row) / side
    return max(max(span * span / a, a / (span * span)) for a in row)


def _fmt_delta(d: int) -> str:
    return ("+" if d >= 0 else "−") + f"{abs(d) / TIB:.1f} Ti"


def _draw_treemap(ax, diff, label: str) -> None:
    """The nested diff treemap on ``ax`` (axes coords 0..1): groups squarified
    by Σ|Δ|, each group's cells squarified inside its rect."""
    from matplotlib.patches import Rectangle

    ax.set_xlim(0, 1)
    ax.set_ylim(1, 0)  # y down, like a screen
    ax.axis("off")
    groups: dict[str, list] = {}
    for c in diff:
        groups.setdefault(c.group, []).append(c)
    names = sorted(groups, key=lambda g: -sum(abs(c.delta) for c in groups[g]))
    gaps = 0.004
    for gname, (gx, gy, gw, gh) in zip(names, squarify([sum(abs(c.delta) for c in groups[g]) for g in names], 0, 0, 1, 1)):
        cells = groups[gname]
        ax.add_patch(Rectangle((gx + gaps / 2, gy + gaps / 2), gw - gaps, gh - gaps, facecolor="none", edgecolor=INK, linewidth=0.9, alpha=0.7))
        # a group header when the box has room; single-cell groups label the cell itself
        header = gh > 0.09 and gw > 0.12 and not (len(cells) == 1 and cells[0].path == gname)
        top = gy + (0.045 if header else 0) + gaps
        if header:
            ax.text(gx + 0.008, gy + 0.012, gname, ha="left", va="top", color=INK, fontsize=8.5, fontweight="bold")
        inner = squarify([abs(c.delta) for c in cells], gx + gaps, top, gw - 2 * gaps, gy + gh - top - gaps)
        for c, (cx, cy, cw, ch) in zip(cells, inner):
            col = GREW if c.delta > 0 else SHRANK
            ax.add_patch(Rectangle((cx, cy), cw, ch, facecolor=col, edgecolor=BG, linewidth=0.8, alpha=0.85))
            name = c.path if c.path in (gname, "other") else c.path.split("/", 1)[1]
            delta = _fmt_delta(c.delta)
            # ~0.0085 axes-width per bold character at fontsize 8 on a 9in figure
            fit = int(cw / 0.0085)
            if fit >= len(name) + 2 + len(delta) and ch > 0.035:
                ax.text(cx + cw / 2, cy + ch / 2, f"{name}  {delta}", ha="center", va="center", color="#ffffff", fontsize=8, fontweight="bold")
            elif fit >= len(delta) and ch > 0.06:
                # two lines, the name ellipsized to the cell's width
                shown = name if len(name) <= fit else (name[: fit - 1] + "…" if fit > 3 else "")
                ax.text(cx + cw / 2, cy + ch / 2, f"{shown}\n{delta}".strip(), ha="center", va="center", color="#ffffff", fontsize=7.5, fontweight="bold")
    grew = sum(c.delta for c in diff if c.delta > 0)
    shrank = -sum(c.delta for c in diff if c.delta < 0)
    ax.set_title(f"What changed — {label}", color=INK, fontsize=12, fontweight="bold", loc="left", pad=8)
    ax.text(1.0, 1.012, f"grew +{grew / TIB:,.1f} Ti · shrank −{shrank / TIB:,.1f} Ti · area = |Δ|", transform=ax.transAxes, ha="right", va="bottom", color=DIM, fontsize=8.5)


def render(
    rows: list[dict],
    out: Path,
    title: str | None = None,
    redact: bool = False,
    diff=None,
    diff_label: str = "",
) -> None:
    """Render the PNG for ``rows`` (each ``{scan, tb}``) to ``out``; with
    ``diff`` (`DiffCell`s, see `digest.tree_diff`) the treemap panel is added
    below. matplotlib imported lazily — the `[plot]` extra. ``redact`` drops
    the numbers (y tick labels, the call-out, cell deltas) — the shape of the
    month without the sizes, for the public README."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from .cw_digest import QUOTA_TIB, scan_ts

    xs = [scan_ts(r["scan"]) for r in rows]
    tot = [r["tb"] for r in rows]
    title = title or f"CoreWeave usage — {xs[-1]:%B %Y}"
    # month-wide x frame: stable early in the month (a 1-scan month otherwise
    # degenerates) and scans fill in left→right as the month progresses.
    from calendar import monthrange
    from datetime import timedelta

    m0 = xs[-1].replace(day=1, hour=0, minute=0)
    m1 = m0.replace(day=monthrange(m0.year, m0.month)[1], hour=23, minute=59)
    x0, x1 = m0 - timedelta(hours=16), m1 + timedelta(hours=16)

    if diff:
        fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9, 7.6), dpi=150, height_ratios=[1, 1.5])
    else:
        fig, ax = plt.subplots(figsize=(9, 3.2), dpi=150)
        ax2 = None
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(GRID)
    ax.tick_params(colors=DIM, labelsize=9)
    ax.grid(True, color=GRID, lw=0.7, alpha=0.5, axis="y")

    # y: fit to the data (a round 50-TiB floor a little under the month's
    # min) but keep the quota line + its label in frame
    lo = max(0.0, (min(tot) - max(10.0, (QUOTA_TIB - min(tot)) * 0.06)) // 50 * 50)
    hi = QUOTA_TIB + (QUOTA_TIB - lo) * 0.07
    # headroom band: everything between the usage line and the quota
    ax.fill_between(xs, tot, QUOTA_TIB, color=FREE, alpha=0.10, hatch="///", edgecolor=FREE, linewidth=0)
    ax.fill_between(xs, lo, tot, color=FILL, alpha=0.25)
    ax.axhline(tot[0], color=DIM, lw=1, ls="--", alpha=0.6)
    ax.axhline(QUOTA_TIB, color=QUOTA, lw=1.4)
    ax.text(x1, QUOTA_TIB, "1 PB quota ", ha="right", va="bottom", color=QUOTA, fontsize=9, fontweight="bold")
    ax.plot(xs, tot, color=LINE, lw=2)
    ax.plot(xs, tot, ".", color=LINE, ms=3.5)
    ax.plot(xs[-1], tot[-1], "o", color=LINE, ms=5)
    if not redact:
        left_half = (xs[-1] - m0) < (m1 - xs[-1])
        ax.annotate(
            f"{tot[-1]:,.0f} TiB · {tot[-1] / QUOTA_TIB * 100:.0f}%",
            (xs[-1], tot[-1]), textcoords="offset points",
            xytext=(6, -14) if left_half else (-6, -14), ha="left" if left_half else "right",
            color=INK, fontsize=11, fontweight="bold",
        )
    ax.set_ylim(lo, hi)
    ax.set_xlim(x0, x1)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    ax.set_title(title, color=INK, fontsize=14, fontweight="bold", loc="left", pad=10)
    ax.text(1.0, 1.04, "cw-s3.oa.dev · TiB", transform=ax.transAxes, ha="right", va="bottom", color=DIM, fontsize=9)
    if redact:
        ax.tick_params(axis="y", labelleft=False, left=False)
        ax.text(0.0, -0.22, "sizes omitted — sign in at cw-s3.oa.dev for the numbers", transform=ax.transAxes, ha="left", va="top", color=DIM, fontsize=8)

    if ax2 is not None:
        ax2.set_facecolor(BG)
        _draw_treemap(ax2, diff, diff_label)

    fig.tight_layout(h_pad=1.2)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=BG)
    plt.close(fig)


@command()
@option("-d", "--rows", "rows_path", type=CP(exists=True, path_type=Path), default=Path("tmp/cw-scans.json"), help="Per-scan rows: {scan, tb}")
@option("-o", "--out", type=CP(path_type=Path), default=Path("tmp/plot-cw.png"), help="Output PNG")
@option("-R", "--redact", is_flag=True, help="No numbers (y labels, call-out) — the public README version")
@option("-t", "--title", default=None, help="Title (default: 'CoreWeave usage — <Month Year>')")
def main(rows_path: Path, out: Path, redact: bool, title: str | None) -> None:
    render(load(open(rows_path)), out, title, redact=redact)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
