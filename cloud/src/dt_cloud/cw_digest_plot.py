"""Digest OP image for the Slack monthly thread (`dt-cloud digest`), two
stacked panels:

- **Sparkline** of every scan (12-hourly points), y-range fit to the month's
  data (a round floor below the min) but with the 1 PB quota line + label
  kept in frame — the used fill under the line and the hatched headroom band
  above it make the used/free split legible without squashing the month into
  the top of the axis (the site's "Size over time" chart with `fit`).
- **Diff treemap** over the OP headline's interval (lead-in scan → latest):
  green cells grew, red shrank, area = |Δ|, nested as deep as the change is
  big — any dir holding ≥1% of the gross change opens into its children
  (`tmp` › `ttl=14d` › `skyrl` › `users` › `<name>`, up to 6 boxes deep), so
  a cell names *whose* run moved; single-child chains collapse to one
  `a/b/…` node, small cells fold into their box's `…`, and small top-level
  dirs into `other` (see `digest.diff_tree`). Layout is a hand-rolled
  squarify.

In-package (not a standalone `uv run` script under `job/`) so the Batch image
runs it: `digest.render_plot` imports and calls :func:`render` in-process (the
slim image has neither `uv` nor, without the `[plot]` extra, matplotlib).
Ad-hoc renders: `python -m dt_cloud.cw_digest_plot -d … -o …`.

Input: per-scan `{scan, tb}` rows + an optional `DiffNode` tree. Output: a PNG sized
for a Slack image block. The digest renders this per run and cache-busts the
OP's image URL so `chat.update` refetches."""
from json import load
from pathlib import Path

from click import Path as CP, command, option

from .cw_digest import RESIDUAL

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


def _fit(s: str, n: int) -> str:
    """``s`` cut to ``n`` characters, ellipsized; empty when ``n`` can't hold
    more than the ellipsis."""
    return s if len(s) <= n else (s[: n - 1] + "…" if n > 3 else "")


# Text metrics in the treemap axes' units (0..1 each way; the panel is ~8.5 in
# × ~4.3 in on the 9 in figure): bold character width and line height per pt.
CHAR_W = 0.0085 / 8
LINE_H = 0.0042
HEADER_PT = 7.5
PAD_X, PAD_Y = 0.003, 0.005


def _draw_node(ax, node, x: float, y: float, w: float, h: float, depth: int, top: bool = False) -> None:
    """``node`` into the rect ``(x, y, w, h)``: a leaf fills it (green grew,
    red shrank) labeled `name  Δ` on one line or two; a box outlines it,
    heads it `name  Δ` when it has room, and squarifies its kids inside."""
    from matplotlib.patches import Rectangle

    name = "other" if top and node.name == RESIDUAL else node.name
    delta = _fmt_delta(node.delta)
    if not node.kids:
        ax.add_patch(Rectangle((x, y), w, h, facecolor=GREW if node.delta > 0 else SHRANK, edgecolor=BG, linewidth=0.8, alpha=0.85))
        pt = 8 if depth <= 1 else 7
        fit = int(w / (CHAR_W * pt))
        # the whole label on one line; else name over Δ; else one line, the name cut to fit
        if fit >= len(name) + 2 + len(delta) and h > LINE_H * pt * 1.4:
            label = f"{name}  {delta}"
        elif fit >= len(delta) and h > LINE_H * pt * 2.6:
            label, pt = f"{_fit(name, fit)}\n{delta}".strip(), pt - 0.5
        elif fit >= len(delta) and h > LINE_H * pt * 1.4:
            label = f"{_fit(name, fit - len(delta) - 2)}  {delta}".strip()
        else:
            return
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center", color="#ffffff", fontsize=pt, fontweight="bold")
        return
    ax.add_patch(Rectangle((x, y), w, h, facecolor="none", edgecolor=INK, linewidth=0.9 if depth <= 1 else 0.6, alpha=0.75 if depth <= 1 else 0.5))
    header_h = LINE_H * HEADER_PT * 1.3
    fit = int((w - 2 * PAD_X) / (CHAR_W * HEADER_PT))
    header = h > 2 * header_h and fit >= 4
    if header:
        label = f"{name}  {delta}" if fit >= len(name) + 2 + len(delta) else _fit(name, fit)
        ax.text(x + PAD_X + 0.002, y + PAD_Y * 0.6, label, ha="left", va="top", color=INK if depth <= 1 else DIM, fontsize=HEADER_PT, fontweight="bold")
    iy = y + (header_h if header else PAD_Y)
    rects = squarify([k.area for k in node.kids], x + PAD_X, iy, w - 2 * PAD_X, y + h - iy - PAD_Y)
    for k, r in zip(node.kids, rects):
        _draw_node(ax, k, *r, depth + 1)


def _draw_treemap(ax, diff, label: str) -> None:
    """The nested diff treemap (a `DiffNode` root, see `digest.diff_tree`) on
    ``ax`` (axes coords 0..1, y down): the top-level dirs squarified by area,
    each box's kids squarified inside it, recursively."""
    ax.set_xlim(0, 1)
    ax.set_ylim(1, 0)  # y down, like a screen
    ax.axis("off")
    for k, r in zip(diff.kids, squarify([k.area for k in diff.kids], 0, 0, 1, 1)):
        _draw_node(ax, k, *r, 1, top=True)
    leaves = list(_leaves(diff))
    grew = sum(n.delta for n in leaves if n.delta > 0)
    shrank = -sum(n.delta for n in leaves if n.delta < 0)
    ax.set_title(f"What changed — {label}", color=INK, fontsize=12, fontweight="bold", loc="left", pad=8)
    ax.text(1.0, 1.012, f"grew +{grew / TIB:,.1f} Ti · shrank −{shrank / TIB:,.1f} Ti · area = |Δ|", transform=ax.transAxes, ha="right", va="bottom", color=DIM, fontsize=8.5)


def _leaves(node):
    if not node.kids:
        yield node
    for k in node.kids:
        yield from _leaves(k)


def render(
    rows: list[dict],
    out: Path,
    title: str | None = None,
    redact: bool = False,
    diff=None,
    diff_label: str = "",
) -> None:
    """Render the PNG for ``rows`` (each ``{scan, tb}``) to ``out``; with
    ``diff`` (a `DiffNode` root, see `digest.diff_tree`) the treemap panel is added
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
