"""The per-deployment half of a digest: a :class:`DigestProfile` turns scan
metadata into the digest's row model and every metric-specific string (the OP
body, a reply's sender/body/avatar, the plot). The engine
(:mod:`disk_tree.notify.digest`) owns the mechanism and calls into a profile;
swapping the profile is how gcs (``$`` + storage-class TiB) and cw (``%`` of a
quota) share one posting engine.

:class:`BytesProfile` is the reference profile shipped with disk-tree: a
bytes-over-time monthly digest with no cloud pricing. It gives disk-tree a
runnable default and is the fixture the engine's specs converge against.
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

from .digest import Period, deg, pct, pct_val, signed, yymmdd

if TYPE_CHECKING:
    from pathlib import Path

TIB = 1024**4


@runtime_checkable
class Row(Protocol):
    """A digest row — opaque to the engine, which only reads ``date`` (for the
    per-scan reply keying). Everything else is the profile's to interpret."""

    date: str


RowT = TypeVar("RowT", bound=Row)


class DigestProfile(Protocol[RowT]):
    """The interface the engine drives, generic over its own row type. A profile
    is a *configured object* (it holds its own site URL, icons host, metric
    prices/units), so its methods close over that config rather than taking it
    per call."""

    #: Slack OP ``icon_emoji`` (the thread starter's avatar).
    op_icon: str
    #: Discord OP avatar URL (webhooks take a URL, not a Slack emoji).
    op_avatar: str

    def title(self, period: Period) -> str:
        """The thread title / OP sender name (e.g. ``disk-tree usage — August 2026``)."""
        ...

    def op_body(self, rows: list[RowT], period: Period, plot_url: str | None) -> str:
        """The OP markdown. ``plot_url=None`` omits the hosted-image line (Discord
        attaches the PNG as a file instead)."""
        ...

    def reply(self, row: RowT, platform: str = "slack") -> tuple[str, str, str]:
        """One scan's reply -> ``(sender_username, body, avatar_url)``."""
        ...

    def rows_from_meta(self, dated_meta: list[tuple[str, dict]]) -> list[RowT]:
        """Build rows from ``(date, meta)`` pairs in date order. The first pair
        seeds the second's delta; callers pass one lead-in scan then slice it.
        Where the ``(date, meta)`` pairs come from is a source's job
        (:mod:`disk_tree.notify.sources`), not the profile's."""
        ...

    def render_plot(self, rows: list[RowT], period: Period, out: Path) -> None:
        """Render the OP plot PNG for ``rows`` to ``out`` (needs the ``plot`` extra)."""
        ...


# ---- reference profile: bytes over time ------------------------------------


@dataclass(frozen=True)
class BytesRow:
    """One scan for the bytes-over-time digest: total TiB + delta vs. the
    previous scan (``dtb`` is ``None`` only when there is no prior scan)."""

    date: str
    tb: float
    dtb: float | None


class BytesProfile:
    """Reference :class:`DigestProfile`: total bytes over time, no cloud pricing.

    Configured with the deployment's display ``name``, ``site_url`` (the
    dashboard the links point at), ``icons_base`` (where ``av_deg{N}.png`` trend
    arrows live), and ``avatar_rev`` (bump when the glyphs change — Slack caches
    avatars per URL). A deployment with no icons host can pass ``icons_base=None``
    to degrade to plain senders (no avatars)."""

    op_icon = ":calendar:"

    def __init__(
        self,
        name: str = "disk-tree usage",
        site_url: str = "https://disk-tree.example",
        icons_base: str | None = "https://disk-tree.example/icons",
        avatar_rev: int = 1,
    ) -> None:
        self.name = name
        self.site_url = site_url.rstrip("/")
        self.icons_base = icons_base.rstrip("/") if icons_base else None
        self.avatar_rev = avatar_rev
        self.op_avatar = f"{self.icons_base}/calendar.png?v=2" if self.icons_base else ""

    def title(self, period: Period) -> str:
        return f"{self.name} — {period.start:%B %Y}"

    def rows_from_meta(self, dated_meta: list[tuple[str, dict]]) -> list[BytesRow]:
        out: list[BytesRow] = []
        ptb: float | None = None
        for date, m in dated_meta:
            tb = round(m["total_bytes"] / TIB, 1)
            out.append(BytesRow(date=date, tb=tb, dtb=round(tb - ptb, 1) if ptb is not None else None))
            ptb = tb
        return out

    def op_body(self, rows: list[BytesRow], period: Period, plot_url: str | None) -> str:
        base_tb = rows[0].tb - (rows[0].dtb or 0)
        mdtb = rows[-1].tb - base_tb
        mweekly = (mdtb / base_tb * 100 * 7 / len(rows)) if base_tb else 0
        lines = [
            f":arrow_deg{deg(mweekly)}: **{signed(mdtb)} TiB** month-to-date · [dashboard]({self.site_url}/)",
            "",
            "*Weekly summaries*",
        ]
        weeks: OrderedDict[dt.date, list[BytesRow]] = OrderedDict()
        for r in rows:
            d = dt.date.fromisoformat(r.date)
            weeks.setdefault(d - dt.timedelta(days=d.weekday()), []).append(r)
        last_mon = list(weeks)[-1]
        base_date = dt.date.fromisoformat(rows[0].date) - dt.timedelta(days=1)
        prev_end: BytesRow | None = None
        for mon, ws in weeks.items():
            end = ws[-1]
            b_tb = prev_end.tb if prev_end is not None else base_tb
            b_date = dt.date.fromisoformat(prev_end.date) if prev_end is not None else base_date
            wdtb = end.tb - b_tb
            wpct = wdtb / b_tb * 100 if b_tb else 0
            partial = " _(partial)_" if len(ws) < 7 and mon == last_mon else ""
            span = (dt.date.fromisoformat(end.date) - b_date).days
            lines.append(
                f":arrow_deg{deg(wpct)}: [wk of {mon.month}/{mon.day}]({self.site_url}/?d={yymmdd(end.date)}-{span}d#over-time){partial} — "
                f"**{end.tb:,.0f} TiB** ({signed(wdtb)}, {pct(wdtb, end.tb)}%)"
            )
            prev_end = end
        if plot_url is not None:
            lines += ["", f"![{self.title(period)}]({plot_url})"]
        return "\n".join(lines)

    def reply(self, row: BytesRow, platform: str = "slack") -> tuple[str, str, str]:
        d = dt.date.fromisoformat(row.date)
        dtb = row.dtb or 0
        sender = f"{d.month}/{d.day} — {row.tb:,.0f} TiB ({signed(dtb)}, {pct(dtb, row.tb)}%)"
        url = f"{self.site_url}/?d={yymmdd(row.date)}#over-time"
        # ↗︎ = NE arrow + text-presentation selector (a link-coloured
        # glyph, not the cartoonish emoji Slack makes of a bare ↗); Discord's
        # is too small to notice, so there the link reads "view →".
        link = f"· [view →]({url})" if platform == "discord" else f"[↗︎]({url})"
        avatar = f"{self.icons_base}/arrows/av_deg{deg(pct_val(dtb, row.tb), 7)}.png?v={self.avatar_rev}" if self.icons_base else ""
        return sender, link, avatar

    def render_plot(self, rows: list[BytesRow], period: Period, out: Path) -> None:
        from .plot import render_bytes

        render_bytes(rows, out, self.title(period))


def build_profile(spec: dict) -> DigestProfile:
    """Construct a :class:`DigestProfile` from a config block's ``profile:``
    selector and its keys. Built-in ``bytes`` -> :class:`BytesProfile`; an
    import path (``pkg.mod:Class`` or ``pkg.mod:factory``) lets a deployment
    ship its own (called with the remaining keys as kwargs)."""
    spec = dict(spec)
    kind = spec.pop("profile", "bytes")
    keys = {k: spec[k] for k in ("name", "site_url", "icons_base", "avatar_rev") if k in spec}
    if kind == "bytes":
        return BytesProfile(**keys)
    if ":" in kind:
        import importlib

        mod, attr = kind.split(":", 1)
        return getattr(importlib.import_module(mod), attr)(**keys)
    raise ValueError(f"unknown digest profile {kind!r} — use 'bytes' or an import path 'pkg.mod:Class'")
