"""Digest OP plot for the Slack monthly thread (`dt-cloud digest`): a 2-panel
mosaic. Top = total-TiB line, y-autofit (change over time) + a dashed reference
line at the month-start total (inc/dec at a glance). Bottom = stacked storage
tiers, reversed cold→hot (stable Archive/Coldline on the bottom, active
Nearline/Standard on top) so the total's wiggle lives at the readable top edge.

In-package (not a standalone `uv run` script under `job/`) so the Batch image
runs it: `digest.render_plot` imports and calls :func:`render` in-process —
the 2026-09-01 daily run failed resolving a repo-relative script path from
site-packages, and the slim image has neither `uv` nor matplotlib (now the
`[plot]` extra). Ad-hoc renders: `python -m dt_cloud.digest_plot -d … -o …`.

Input: per-scan `{date, std, near, cold, arch}` TiB rows. Output: a PNG sized
for a Slack image block. The digest renders this per run and cache-busts the
OP's image URL so `chat.update` refetches."""
from datetime import date as Date
from json import load
from pathlib import Path

from click import Path as CP, command, option

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


def render(
    rows: list[dict],
    out: Path,
    title: str | None = None,
    redact: bool = False,
) -> None:
    """Render the mosaic PNG for ``rows`` (each ``{date, std, near, cold,
    arch}``, TiB) to ``out``. matplotlib imported lazily — the `[plot]` extra.
    ``redact`` drops every size (y tick labels, the TiB call-out) — the shape
    of the month without the numbers, for the public README; sizes stay behind
    the site's sign-in."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    xs = [Date.fromisoformat(r["date"]) for r in rows]
    tot = [sum(r[k] for k, _, _ in TIERS) for r in rows]
    title = title or f"GCS usage — {xs[-1]:%B %Y}"
    # month-wide x frame: stable early in the month (a 1-scan month otherwise
    # degenerates — zero-width stackplot, tick-label explosion) and days fill
    # in left→right as the month progresses.
    from calendar import monthrange

    m0 = xs[-1].replace(day=1)
    m1 = m0.replace(day=monthrange(m0.year, m0.month)[1])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 4.6), dpi=200, height_ratios=[1, 1.7], sharex=True)
    fig.patch.set_facecolor(BG)
    for ax in (ax1, ax2):
        ax.set_facecolor(BG)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(GRID)
        ax.tick_params(colors=DIM, labelsize=9)
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
    ax1.text(1.0, 1.04, "gcs.oa.dev", transform=ax1.transAxes, ha="right", va="bottom", color=DIM, fontsize=9)
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
    from datetime import timedelta

    ax2.set_xlim(m0 - timedelta(hours=16), m1 + timedelta(hours=16))
    ax2.grid(True, color=GRID, lw=0.7, alpha=0.4, axis="y")
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    h, la = ax2.get_legend_handles_labels()  # legend hot→cold (visual top→bottom)
    ax2.legend(h[::-1], la[::-1], loc="upper right", fontsize=8, facecolor=BG, edgecolor=GRID, labelcolor=INK, ncol=4, framealpha=0.55)

    if redact:
        for ax in (ax1, ax2):
            ax.tick_params(axis="y", labelleft=False, left=False)
        ax2.text(0.0, -0.22, "sizes omitted — sign in at gcs.oa.dev for the numbers", transform=ax2.transAxes, ha="left", va="top", color=DIM, fontsize=8)
    fig.tight_layout(h_pad=0.6)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=BG)
    plt.close(fig)


@command()
@option("-d", "--days", "days_path", type=CP(exists=True, path_type=Path), default=Path("tmp/class-days.json"), help="Per-scan tier rows: {date, std, near, cold, arch} TiB")
@option("-o", "--out", type=CP(path_type=Path), default=Path("tmp/plot-mosaic.png"), help="Output PNG")
@option("-R", "--redact", is_flag=True, help="No sizes (y labels, TiB call-out) — the public README version")
@option("-t", "--title", default=None, help="Title (default: 'GCS usage — <Month Year>')")
def main(days_path: Path, out: Path, redact: bool, title: str | None) -> None:
    render(load(open(days_path)), out, title, redact=redact)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
