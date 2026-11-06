"""`dt-cloud fleet-status`: several deployments' state in one table, read from each site's own API with its own
read-only token: its newest scan and how old that is, its recent runs' outcomes, and the build it serves. No cloud keys
and no store reads: what a viewer of each site can see, side by side. A site it can't read is a row saying why, so one
deployment's outage or missing token never hides the rest."""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from click import BadParameter, command, option

from .site import SiteError, get_json

#: The run kinds that aren't scans (`site/src/scanRunsModel.ts` `MAINT_KINDS`): what a row's recent runs leave out.
MAINTENANCE = ('compact', 'race', 'merge')
#: One character per run, newest first.
MARKS = {"ok": "✓", "short": "~", "failed": "✗", "running": "…"}


@dataclass(frozen=True)
class Site:
    """`name=url=TOKEN_VAR`: a deployment, and the environment variable holding its read-only token."""
    name: str
    url: str
    token_var: str


@dataclass
class Row:
    """One deployment's line. `error`: why it couldn't be read (and nothing else is known)."""
    name: str
    url: str
    error: str | None = None
    scan: str | None = None
    age_s: int | None = None
    runs: list[str] = field(default_factory=list)
    build: str | None = None


def parse_site(value: str) -> Site:
    parts = value.split("=")
    if len(parts) != 3 or not all(parts):
        raise BadParameter(f"{value!r}: want NAME=URL=TOKEN_VAR")
    return Site(*parts)


def outcome(summary: dict) -> str | None:
    """A run's mark (`MARKS`): `short` when it succeeded with a step's issue still open (no later run recovered it or passed since). None: it did nothing (`nop`)."""
    status = summary["run"]["status"]
    if status == "nop":
        return None
    if status == "failed":
        return "failed"
    if status == "running":
        return "running"
    return "short" if any(not (i.get("recovery") or i.get("passing")) for i in summary.get("phase_issues", [])) else "ok"


def read_site(site: Site, n: int) -> Row:
    """`site`'s row, from `/api/health` and `/api/scan-runs`."""
    row = Row(site.name, site.url)
    token = os.environ.get(site.token_var)
    if not token:
        row.error = f"no token (${site.token_var})"
        return row
    try:
        health = get_json(site.url, token, "/api/health")
        runs = get_json(site.url, token, "/api/scan-runs")
    except SiteError as e:
        row.error = str(e)
        return row
    fresh = health.get("freshness") or {}
    row.scan = fresh.get("newest_scan")
    row.age_s = fresh.get("newest_scan_age")
    row.build = ((health.get("build") or {}).get("sha") or "")[:8] or None
    scans = [r for r in runs.get("runs", []) if r["run"]["kind"] not in MAINTENANCE]
    row.runs = [o for o in map(outcome, scans) if o][:n]
    return row


def age(seconds: int | None) -> str:
    """`seconds` to the nearest hour, or day past two."""
    if seconds is None:
        return "?"
    hours = round(seconds / 3600)
    return f"{hours}h ago" if hours < 48 else f"{round(hours / 24)}d ago"


def render(rows: Sequence[Row]) -> str:
    """One line per deployment, aligned on its name."""
    width = max((len(r.name) for r in rows), default=0)
    lines = []
    for r in rows:
        if r.error:
            lines.append(f"{r.name:<{width}}  {r.error}")
            continue
        marks = "".join(MARKS[m] for m in r.runs) or "no runs"
        lines.append(f"{r.name:<{width}}  scan {r.scan or '?'} · {age(r.age_s)} · {marks} · build {r.build or '?'}")
    return "\n".join(lines) + "\n"


@command("fleet-status")
@option("-j", "--json", "as_json", is_flag=True, help="The rows as JSON")
@option("-n", "--runs", default=5, type=int, help="Recent runs per deployment, newest first")
@option("-s", "--site", "sites", multiple=True, required=True, help="NAME=URL=TOKEN_VAR: a deployment, and the env var holding its read-only token (repeatable)")
def fleet_status_cmd(as_json: bool, runs: int, sites: Sequence[str]) -> None:
    """Several deployments' newest scan, recent runs and serving build, one line each. Exits 0 whatever they say: this
    reports, and an unreadable site is a row."""
    rows = [read_site(parse_site(s), runs) for s in sites]
    print(json.dumps([asdict(r) for r in rows], indent=2) if as_json else render(rows), end="\n" if as_json else "")
