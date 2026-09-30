"""The digest OP plot: a static PNG the OP references (Slack image block) or
carries (Discord attachment). Rendered with plotly + kaleido — disk-tree's
existing plotting stack, no new dependency.

:func:`render_bytes` is the reference profile's panel — total TiB over time, with
a dashed reference line at the period-start total so growth reads at a glance. A
richer profile (gcs's stacked storage-class tiers) supplies its own renderer.
"""
from __future__ import annotations

from datetime import date as Date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .profile import BytesRow

BG = "#0d1117"
INK = "#c9d1d9"
DIM = "#8b949e"
GRID = "#21262d"
LINE = "#58a6ff"
FILL = "rgba(31, 111, 235, 0.12)"


def render_bytes(rows: list[BytesRow], out: Path, title: str) -> None:
    """Render the total-TiB-over-time PNG for ``rows`` to ``out``.

    A dark line (filled to the floor) with a dashed reference at the first
    scan's total and a labelled endpoint marker. plotly + kaleido imported
    lazily so the module stays importable without the ``plot`` extra."""
    import plotly.graph_objects as go

    xs = [Date.fromisoformat(r.date) for r in rows]
    ys = [r.tb for r in rows]
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.25 or max(hi * 0.002, 1.0)

    fig = go.Figure()
    fig.add_hline(y=ys[0], line=dict(color=DIM, width=1, dash="dash"))
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines", line=dict(color=LINE, width=2),
        fill="tozeroy", fillcolor=FILL, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=[xs[-1]], y=[ys[-1]], mode="markers+text", marker=dict(color=LINE, size=8),
        text=[f"  {ys[-1]:,.0f} TiB"], textposition="top left",
        textfont=dict(color=INK, size=14), hoverinfo="skip",
    ))
    fig.update_layout(
        title=dict(text=title, font=dict(color=INK, size=18), x=0.02, xanchor="left"),
        paper_bgcolor=BG, plot_bgcolor=BG, showlegend=False,
        width=900, height=460, margin=dict(l=64, r=24, t=56, b=40),
        xaxis=dict(color=DIM, gridcolor=GRID, showline=True, linecolor=GRID),
        yaxis=dict(color=DIM, gridcolor=GRID, range=[lo - pad, hi + pad], tickformat=",.0f"),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_image(str(out))
