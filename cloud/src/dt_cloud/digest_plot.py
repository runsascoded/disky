"""The digest OP's image (`dt-cloud digest`): the panels both templates draw,
on one dark style, sized for a Slack image block / Discord attachment.

- :func:`render_tiers` (the `gcs` template): a 2-panel mosaic. Top = the
  total-TiB line, y-autofit, with a dashed reference at the month-start total;
  bottom = stacked storage classes, reversed cold→hot (stable Archive/Coldline
  at the bottom, active Nearline/Standard on top) so the total's wiggle lives
  at the readable top edge.
- :func:`render_quota` (the `cw` template): a sparkline of every scan (12-hourly
  points), y fit to the month's data; with a quota, the quota line + label kept
  in frame and a hatched headroom band between it and the usage line. With a
  ``diff`` (:func:`diff_tree`), a **diff treemap** panel below over the OP
  headline's interval: green cells grew, red shrank, area = |Δ|, nested as
  deep as the change is big — any dir holding ≥1% of the gross change opens
  into its children (`tmp` › `ttl=14d` › `skyrl` › `users` › `<name>`, up to 6
  boxes deep), so a cell names *whose* run moved; single-child chains collapse
  to one `a/b/…` node, small cells fold into their box's `…`, and small
  top-level dirs into `other`. Layout is a hand-rolled squarify.

In-package (not a standalone `uv run` script under `job/`) so the Batch image
runs it: the templates import and call the renderers in-process — the
2026-09-01 daily run failed resolving a repo-relative script path from
site-packages, and the slim image has neither `uv` nor matplotlib (the `[plot]`
extra). Ad-hoc renders: `python -m dt_cloud.digest_plot -T gcs|cw -d … -o …`.
The digest renders per run and cache-busts the OP's image URL so `chat.update`
refetches."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, timedelta
from json import load
from pathlib import Path

from click import Choice, Path as CP, command, option

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
# bottom→top: Archive (coldest/stablest) … Standard (hottest/active); Standard
# reddened for contrast against the gold Nearline.
TIERS = [
    ("arch", "Archive", "#6e7681"),
    ("cold", "Coldline", "#3b82f6"),
    ("near", "Nearline", "#e3b341"),
    ("std", "Standard", "#ff7b72"),
]


def _mpl():
    """matplotlib, imported lazily (the `[plot]` extra) on the Agg backend."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    return plt, mdates


def _style(ax) -> None:
    """The shared axes chrome: dark face, open top/right, dim ticks."""
    ax.set_facecolor(BG)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(GRID)
    ax.tick_params(colors=DIM, labelsize=9)


def _weekly_ticks(ax, mdates) -> None:
    """One x tick per Monday, labelled `M/D`."""
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))


def _save(fig, out: Path, h_pad: float) -> None:
    import matplotlib.pyplot as plt

    fig.tight_layout(h_pad=h_pad)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=BG)
    plt.close(fig)


# ---- the `gcs` template's mosaic --------------------------------------------


def render_tiers(rows: list[dict], out: Path, title: str, host: str, redact: bool = False) -> None:
    """Render the mosaic PNG for ``rows`` (each ``{date, std, near, cold,
    arch}``, TiB) to ``out``. ``host`` is the corner credit; ``redact`` drops
    every size (y tick labels, the TiB call-out) — the shape of the month
    without the numbers, for a public README; sizes stay behind the site's
    sign-in."""
    plt, mdates = _mpl()

    xs = [Date.fromisoformat(r["date"]) for r in rows]
    tot = [sum(r[k] for k, _, _ in TIERS) for r in rows]
    # month-wide x frame: stable early in the month (a 1-scan month otherwise
    # degenerates — zero-width stackplot, tick-label explosion) and days fill
    # in left→right as the month progresses.
    from calendar import monthrange

    m0 = xs[-1].replace(day=1)
    m1 = m0.replace(day=monthrange(m0.year, m0.month)[1])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 4.6), dpi=200, height_ratios=[1, 1.7], sharex=True)
    fig.patch.set_facecolor(BG)
    for ax in (ax1, ax2):
        _style(ax)
        ax.margins(x=0.02)
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")

    # top: total line, autofit + dashed month-start reference
    ax1.axhline(tot[0], color=DIM, lw=1, ls="--", alpha=0.6)
    ax1.fill_between(xs, tot, min(tot) - (max(tot) - min(tot)) * 0.15, color=FILL, alpha=0.12)
    ax1.plot(xs, tot, color=LINE, lw=2)
    ax1.plot(xs[-1], tot[-1], "o", color=LINE, ms=5)
    # label goes on whichever side of the point has room in the month frame
    left_half = (xs[-1] - m0) < (m1 - xs[-1])
    if not redact:
        ax1.annotate(f"{tot[-1]:,.0f} TiB", (xs[-1], tot[-1]), textcoords="offset points", xytext=(6, 7) if left_half else (-6, 7), ha="left" if left_half else "right", color=INK, fontsize=11, fontweight="bold")
    lo, hi = min(tot), max(tot)
    pad = (hi - lo) * 0.25 or max(hi * 0.002, 1.0)
    ax1.set_ylim(lo - pad, hi + pad)
    ax1.set_title(title, color=INK, fontsize=14, fontweight="bold", loc="left", pad=10)
    ax1.text(1.0, 1.04, host, transform=ax1.transAxes, ha="right", va="bottom", color=DIM, fontsize=9)
    ax1.grid(True, color=GRID, lw=0.7, alpha=0.5, axis="y")

    # bottom: stacked tiers, cold→hot (a 1-scan month gets a stacked bar —
    # stackplot over a single x is a zero-width polygon, i.e. invisible)
    ys = [[r[k] for r in rows] for k, _, _ in TIERS]
    if len(xs) > 1:
        ax2.stackplot(xs, *ys, labels=[n for _, n, _ in TIERS], colors=[c for *_, c in TIERS], alpha=0.92)
    else:
        bottom = 0.0
        for (k, name, c), y in zip(TIERS, ys):
            ax2.bar(xs, y, bottom=bottom, width=0.8, label=name, color=c, alpha=0.92)
            bottom += y[0]
    ax2.set_ylim(0, None)
    ax2.set_xlim(m0 - timedelta(hours=16), m1 + timedelta(hours=16))
    ax2.grid(True, color=GRID, lw=0.7, alpha=0.4, axis="y")
    _weekly_ticks(ax2, mdates)
    h, la = ax2.get_legend_handles_labels()  # legend hot→cold (visual top→bottom)
    ax2.legend(h[::-1], la[::-1], loc="upper right", fontsize=8, facecolor=BG, edgecolor=GRID, labelcolor=INK, ncol=4, framealpha=0.55)

    if redact:
        for ax in (ax1, ax2):
            ax.tick_params(axis="y", labelleft=False, left=False)
        ax2.text(0.0, -0.22, f"sizes omitted — sign in at {host} for the numbers", transform=ax2.transAxes, ha="left", va="top", color=DIM, fontsize=8)
    _save(fig, out, 0.6)


# ---- the `cw` template's quota sparkline + diff treemap ------------------------


def render_quota(
    rows: list[dict],
    out: Path,
    title: str,
    host: str,
    quota_tib: float | None = None,
    quota_name: str = "",
    redact: bool = False,
    diff=None,
    diff_label: str = "",
) -> None:
    """Render the PNG for ``rows`` (each ``{ts, tb}``: a UTC datetime and TiB)
    to ``out``. With ``quota_tib``, the quota line (labelled ``quota_name``)
    and headroom band; with ``diff`` (a :class:`DiffNode` root) the treemap
    panel below. ``redact`` drops the numbers (y tick labels, the call-out) —
    the shape of the month without the sizes, for a public README."""
    plt, mdates = _mpl()

    xs = [r["ts"] for r in rows]
    tot = [r["tb"] for r in rows]
    # month-wide x frame: stable early in the month (a 1-scan month otherwise
    # degenerates) and scans fill in left→right as the month progresses.
    from calendar import monthrange

    m0 = xs[-1].replace(day=1, hour=0, minute=0)
    m1 = m0.replace(day=monthrange(m0.year, m0.month)[1], hour=23, minute=59)
    x0, x1 = m0 - timedelta(hours=16), m1 + timedelta(hours=16)

    if diff:
        fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9, 7.6), dpi=150, height_ratios=[1, 1.5])
    else:
        fig, ax = plt.subplots(figsize=(9, 3.2), dpi=150)
        ax2 = None
    fig.patch.set_facecolor(BG)
    _style(ax)
    ax.grid(True, color=GRID, lw=0.7, alpha=0.5, axis="y")

    if quota_tib is not None:
        # y: fit to the data (a round 50-TiB floor a little under the month's
        # min) but keep the quota line + its label in frame
        lo = max(0.0, (min(tot) - max(10.0, (quota_tib - min(tot)) * 0.06)) // 50 * 50)
        hi = quota_tib + (quota_tib - lo) * 0.07
        # headroom band: everything between the usage line and the quota
        ax.fill_between(xs, tot, quota_tib, color=FREE, alpha=0.10, hatch="///", edgecolor=FREE, linewidth=0)
    else:
        lo = max(0.0, (min(tot) - max(10.0, (max(tot) - min(tot)) * 0.06)) // 50 * 50)
        hi = max(tot) + (max(tot) - lo) * 0.07
    ax.fill_between(xs, lo, tot, color=FILL, alpha=0.25)
    ax.axhline(tot[0], color=DIM, lw=1, ls="--", alpha=0.6)
    if quota_tib is not None:
        ax.axhline(quota_tib, color=QUOTA, lw=1.4)
        ax.text(x1, quota_tib, f"{quota_name} quota ", ha="right", va="bottom", color=QUOTA, fontsize=9, fontweight="bold")
    ax.plot(xs, tot, color=LINE, lw=2)
    ax.plot(xs, tot, ".", color=LINE, ms=3.5)
    ax.plot(xs[-1], tot[-1], "o", color=LINE, ms=5)
    if not redact:
        left_half = (xs[-1] - m0) < (m1 - xs[-1])
        pct = f" · {tot[-1] / quota_tib * 100:.0f}%" if quota_tib is not None else ""
        ax.annotate(
            f"{tot[-1]:,.0f} TiB{pct}",
            (xs[-1], tot[-1]), textcoords="offset points",
            xytext=(6, -14) if left_half else (-6, -14), ha="left" if left_half else "right",
            color=INK, fontsize=11, fontweight="bold",
        )
    ax.set_ylim(lo, hi)
    ax.set_xlim(x0, x1)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    _weekly_ticks(ax, mdates)
    ax.set_title(title, color=INK, fontsize=14, fontweight="bold", loc="left", pad=10)
    ax.text(1.0, 1.04, f"{host} · TiB", transform=ax.transAxes, ha="right", va="bottom", color=DIM, fontsize=9)
    if redact:
        ax.tick_params(axis="y", labelleft=False, left=False)
        ax.text(0.0, -0.22, f"sizes omitted — sign in at {host} for the numbers", transform=ax.transAxes, ha="left", va="top", color=DIM, fontsize=8)

    if ax2 is not None:
        ax2.set_facecolor(BG)
        _draw_treemap(ax2, diff, diff_label)

    _save(fig, out, 1.2)


# ---- diff treemap: data -------------------------------------------------------

RESIDUAL = "…"
TREE_OTHER = "(other)"


@dataclass(frozen=True)
class DiffNode:
    """One box of the diff treemap: ``name`` is a path segment, a collapsed
    single-child chain (`curriculum-sft/snowball`), or :data:`RESIDUAL` — what
    a dir's drawn children don't account for (children pruned from the tree,
    or folded as too small). ``delta`` is the net byte Δ base→latest. A node
    with ``kids`` draws as a labeled box around them; a leaf as a cell
    colored by the sign of its Δ."""

    name: str
    delta: int
    kids: tuple[DiffNode, ...] = ()

    @property
    def area(self) -> int:
        """Treemap area: |Δ| for a leaf, the sum of the kids' areas for a box —
        gross change, so a dir whose children grew and shrank in about equal
        measure still gets the room to show both."""
        return sum(k.area for k in self.kids) if self.kids else abs(self.delta)


@dataclass(frozen=True)
class _Raw:
    name: str
    delta: int
    gross: int
    kids: tuple[_Raw, ...]


def _kids(node: dict) -> dict[str, dict]:
    """A tree node's named children; the builder's `(other)` (direct files +
    children below its size floor, `tree_build`) is left out — it's residual,
    and its membership differs scan to scan."""
    return {c["n"]: c for c in node.get("c", []) if c["n"] != TREE_OTHER}


def _raw_diff(name: str, base: dict, latest: dict) -> _Raw:
    """The full diff of two tree nodes (`{n,b,c}`): per child (a dir present on
    one side only counts as fully grown/shrunk), plus a residual for what the
    named children don't account for (direct files, and dirs below the trees'
    size floor). ``gross`` = Σ|Δ| over the leaves."""
    bk, lk = _kids(base), _kids(latest)
    delta = latest.get("b", 0) - base.get("b", 0)
    kids = [k for k in (_raw_diff(n, bk.get(n, {}), lk.get(n, {})) for n in sorted(set(bk) | set(lk))) if k.gross]
    if kids and (rest := delta - sum(k.delta for k in kids)):
        kids.append(_Raw(RESIDUAL, rest, abs(rest), ()))
    return _Raw(name, delta, sum(k.gross for k in kids) if kids else abs(delta), tuple(kids))


def _fold(n: _Raw, thresh: float, depth: int, max_depth: int) -> DiffNode:
    """``n`` as a drawable node at nesting ``depth``: children whose gross
    change is under ``thresh`` fold into the residual; a dir with one
    surviving child (and a negligible residual) collapses into it — one node
    named `a/b`, not a box holding a single box; a chain down to a leaf names
    the whole chain (`users/romain/<run>/…`), so the cell says *whose* change
    it is. Below ``max_depth`` boxes, everything's a leaf."""
    leaf = DiffNode(n.name, n.delta)
    if depth >= max_depth or not n.kids:
        return leaf
    keep = [k for k in n.kids if k.gross >= thresh and k.name != RESIDUAL]
    rest = n.delta - sum(k.delta for k in keep)
    if not keep:
        return leaf
    if len(keep) == 1 and (not rest or abs(rest) < thresh):
        sub = _fold(keep[0], thresh, depth, max_depth)
        return DiffNode(f"{n.name}/{sub.name}", n.delta, sub.kids)
    return _box(n.name, n.delta, [_fold(k, thresh, depth + 1, max_depth) for k in keep])


def _box(name: str, delta: int, kids: list[DiffNode]) -> DiffNode:
    """A box over ``kids``, plus a residual cell for the rest of ``delta``.
    Kids with no area (a dir whose ups and downs each fell under the fold,
    netting ~0) can't be drawn — their Δ joins the residual. Area desc, then
    name."""
    kids = [k for k in kids if k.area]
    if rest := delta - sum(k.delta for k in kids):
        kids.append(DiffNode(RESIDUAL, rest))
    return DiffNode(name, delta, tuple(sorted(kids, key=lambda k: (-k.area, k.name))))


def diff_tree(base: dict, latest: dict, min_frac: float = 0.0, max_depth: int = 6) -> DiffNode:
    """Diff two size trees (one bucket's node of each scan's `tree.json`, nodes
    `{n,b,o,d,c}`) into a nested treemap: the root's kids are the top-level
    dirs, and any dir whose gross change is at least ``min_frac`` of the
    whole's opens into its own children, down to ``max_depth`` levels of
    boxes. Smaller changes fold into their parent's `…` (at the top level,
    that's everything below the fold). Kids are ordered by area desc, then
    name."""
    raw = _raw_diff("", base, latest)
    thresh = min_frac * raw.gross
    keep = [k for k in raw.kids if k.gross >= thresh and k.name != RESIDUAL]
    return _box("", raw.delta, [_fold(k, thresh, 1, max_depth) for k in keep])


# ---- diff treemap: layout + drawing -------------------------------------------

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


def _draw_treemap(ax, diff: DiffNode, label: str) -> None:
    """The nested diff treemap on ``ax`` (axes coords 0..1, y down): the
    top-level dirs squarified by area, each box's kids squarified inside it,
    recursively."""
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


@command()
@option("-d", "--rows", "rows_path", type=CP(exists=True, path_type=Path), required=True, help="Per-scan rows: gcs {date, std, near, cold, arch} / cw {scan, tb} (TiB)")
@option("-o", "--out", type=CP(path_type=Path), default=Path("tmp/digest-plot.png"), help="Output PNG")
@option("-R", "--redact", is_flag=True, help="No sizes (y labels, call-out) — a public README version")
@option("-t", "--title", default=None, help="Title (default: '<template title> — <Month Year>')")
@option("-T", "--template", type=Choice(["gcs", "cw"]), default="gcs", help="Which template's panels (and preset title/host/quota)")
def main(rows_path: Path, out: Path, redact: bool, title: str | None, template: str) -> None:
    from urllib.parse import urlparse

    from .digest import PRESETS, scan_ts

    cfg = PRESETS[template]
    rows = load(open(rows_path))
    host = urlparse(cfg.site_url).netloc
    if template == "gcs":
        title = title or f"{cfg.title} — {Date.fromisoformat(rows[-1]['date']):%B %Y}"
        render_tiers(rows, out, title, host, redact=redact)
    else:
        q = cfg.primary_quota
        pts = [{"ts": scan_ts(r["scan"]), "tb": r["tb"]} for r in rows]
        title = title or f"{cfg.title} — {pts[-1]['ts']:%B %Y}"
        render_quota(pts, out, title, host, q.bytes / TIB if q else None, q.name if q else "", redact=redact)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
