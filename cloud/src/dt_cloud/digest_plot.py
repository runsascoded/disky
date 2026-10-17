"""The `gcs` template's digest OP image (`dt-cloud digest`): :func:`render_tiers`,
a 2-panel mosaic on a dark style, sized for a Slack image block / Discord
attachment. Top = the total-TiB line, y-autofit, with a dashed reference at the
month-start total; bottom = stacked storage classes, reversed cold→hot (stable
Archive/Coldline at the bottom, active Nearline/Standard on top) so the total's
wiggle lives at the readable top edge. (The `cw` template's image is the site's,
`POST /api/digest-image`.)

In-package (not a standalone `uv run` script under `job/`) so the Batch image
runs it: the template imports and calls the renderer in-process — the
2026-09-01 daily run failed resolving a repo-relative script path from
site-packages, and the slim image has neither `uv` nor matplotlib (the `[plot]`
extra).
The digest renders per run and cache-busts the OP's image URL so `chat.update`
refetches."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .scan_id import scan_time

BG = "#0d1117"
INK = "#c9d1d9"
DIM = "#8b949e"
GRID = "#21262d"
LINE = "#58a6ff"
FILL = "#1f6feb"
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

    # x = each scan's UTC instant: a day can hold several scans (`YYYY-MM-DDTHHMM`).
    xs = [scan_time(r["date"]).replace(tzinfo=None) for r in rows]
    tot = [sum(r[k] for k, _, _ in TIERS) for r in rows]
    # month-wide x frame: stable early in the month (a 1-scan month otherwise
    # degenerates — zero-width stackplot, tick-label explosion) and days fill
    # in left→right as the month progresses.
    from calendar import monthrange

    m0 = xs[-1].replace(day=1, hour=0, minute=0)
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
