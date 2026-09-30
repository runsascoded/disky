"""`disk-tree tiers` — cut the path store's sorts from any layer-2, and plan a
read over a tier's `.groups.json` offline (spec `path-store.md` §4.1, §5)."""
from __future__ import annotations

import json

from click import Context, Group, argument, echo, option

from disk_tree.cli.base import cli
from disk_tree.find.tiers import DEFAULT_MEM, DEFAULT_ROW_GROUP_ROWS, TIERS


class DefaultGroup(Group):
    """A group whose first positional, when it names no subcommand, routes to
    `cut` — so `disk-tree tiers L2 …` is `disk-tree tiers cut L2 …`."""
    default_cmd = 'cut'

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in ('--help', '-h'):
            args = [self.default_cmd, *args]
        return super().parse_args(ctx, args)


@cli.group('tiers', cls=DefaultGroup, invoke_without_command=False)
def tiers_group():
    """The path store's sorts: cut them from a layer-2 (`cut`, the default),
    or plan a read over a `.groups.json` (`plan`)."""


def _hr(n: int) -> str:
    x = float(n)
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if x < 1024 or unit == 'T':
            return f'{x:.0f}{unit}' if unit == 'B' else f'{x:.1f}{unit}'
        x /= 1024
    return f'{x:.1f}T'


@tiers_group.command('cut')
@option('-g', '--groups', is_flag=True, help='Also write each tier\'s `.groups.json` footer sidecar beside it (`find/groups.py`)')
@option('-j', '--json', 'as_json', is_flag=True, help='One JSON document on stdout')
@option('-m', '--mem', default=DEFAULT_MEM, help=f'DuckDB memory limit for the sort (default {DEFAULT_MEM}); it spills past this, so it bounds peak RSS, not the input')
@option('-p', '--threads', default=None, type=int, help='DuckDB threads (default: DuckDB\'s)')
@option('-r', '--row-group-rows', default=DEFAULT_ROW_GROUP_ROWS, help=f'Max rows per parquet row group, a multiple of 2048 (default {DEFAULT_ROW_GROUP_ROWS}: the HTTP range-read unit)')
@option('-s', '--stem', default=None, help='Output stem: tiers land at `<stem>.<tier>[-by-<cols>].parquet`; may be a URL (cut locally, then uploaded). Default: the layer-2 beside itself minus `.parquet`, or its basename in the cwd for a URL source')
@option('-t', '--tiers', default=','.join(TIERS), help=f'Tiers to cut, comma-separated, any subset of {",".join(TIERS)} (default: all)')
@option('-T', '--tmp', 'tmp_dir', default=None, help='DuckDB spill directory for the sort (default: `.duckdb-tmp` beside the output stem, removed after); put it on the volume with room')
@option('-v', '--sort-variant', 'sort_variants', multiple=True, help='Extra sorted copies of each tier led by these comma-separated columns (e.g. `usr` → `(usr, depth, path)`, file `…path-by-usr.parquet`); repeatable')
@argument('layer2')
def cut_cmd(
    groups: bool,
    as_json: bool,
    mem: str,
    threads: int | None,
    row_group_rows: int,
    stem: str | None,
    tiers: str,
    tmp_dir: str | None,
    sort_variants: tuple[str, ...],
    layer2: str,
):
    """Cut the path store's sorts from LAYER2 (a local path or an fsspec URL:
    `gs://…`, `r2://…`, `s3://…`).

    `path` is every row (objects and directories) sorted `(depth, path, …labels)`;
    `bysize` is the same rows sorted `(⌊log2 size⌋ desc, path, …labels)`, size
    0 last. Each file carries `tier` / `sort` (+ `bucket: log2`) in its
    parquet metadata and inherits the source's listing format. Prints each
    tier's rows, row groups, bytes and metadata.
    """
    from disk_tree.find.tiers import cut_tiers, parse_tiers
    reports = cut_tiers(
        layer2, stem=stem, tiers=parse_tiers(tiers), row_group_rows=row_group_rows,
        sort_variants=tuple(tuple(c for c in v.split(',') if c) for v in sort_variants), groups=groups,
        mem=mem, threads=threads, tmp_dir=tmp_dir,
    )
    if as_json:
        echo(json.dumps([r.asdict() for r in reports], indent=2))
        return
    for r in reports:
        kv = ' '.join(f'{k}={v}' for k, v in r.kv.items() if not k.startswith('disk_tree.'))
        echo(f"{r.path}: {r.rows:,} rows, {r.groups:,} group(s), {_hr(r.bytes)}{' (+ groups.json)' if groups else ''}  [{kv}]")


@tiers_group.command('plan')
@option('-a', '--atten', default=1.0, help='Per-level threshold attenuation: `thrAt(d) = THR · atten^(d − dP − 1)` (default 1: one threshold at every depth)')
@option('-C', '--no-count', is_flag=True, help='Sidecar only: skip opening the parquet to count the rows that actually pass (no `matched` / waste)')
@option('-d', '--max-depth', default=None, type=int, help='Read only depths `dP+1 … dP+N` (default: unbounded, as the treemap reads)')
@option('-j', '--json', 'as_json', is_flag=True, help='One JSON document on stdout (includes the selected group ids)')
@option('-p', '--parquet', default=None, help='The tier parquet to count matches from (default: beside the sidecar, `.groups.json` → `.parquet`)')
@option('-t', '--tier', default=None, help='Which predicate: `path` (mirrors `readRects`) or `bysize` (the bucket sort\'s). Default: from the sidecar\'s name')
@argument('sidecar')
@argument('path')
@argument('thr', type=float)
def plan_cmd(
    atten: float,
    no_count: bool,
    max_depth: int | None,
    as_json: bool,
    parquet: str | None,
    tier: str | None,
    sidecar: str,
    path: str,
    thr: float,
):
    """The reader's span selection for a subtree read of PATH at threshold THR
    bytes, run offline over SIDECAR (a tier's `.groups.json`, local or URL).

    PATH is `.` (or empty) for the root, else the row's path as the tier holds
    it (`d0/sub`). Reports the row groups selected, the rows they hold, the
    bytes their column chunks span, and — from the parquet — how many rows
    actually satisfy the read, i.e. the decode waste.
    """
    from disk_tree.find.tier_plan import Query, plan
    q = Query(path=path, thr=thr, atten=atten, max_depth=max_depth)
    p = plan(sidecar, q, tier=tier, parquet=parquet, count=not no_count)
    if as_json:
        echo(json.dumps(p.asdict(), indent=2))
        return
    d_hi = '∞' if q.d_hi is None else str(q.d_hi)
    echo(f"{p.tier}: P={q.path or '.'} (depth {q.depth}) thr={thr:g} atten={atten:g} depths {q.d_lo}..{d_hi} paths [{q.p_lo!r}, {q.p_hi!r})")
    line = (
        f"  groups {p.groups:,}/{p.total_groups:,}  rows {p.rows:,}/{p.total_rows:,}"
        f"  bytes {_hr(p.bytes)}/{_hr(p.total_bytes)}"
    )
    if p.matched is not None:
        line += f"  matched {p.matched:,}"
        if p.waste is not None:
            line += f"  waste {100 * p.waste:.1f}%"
    echo(line)
