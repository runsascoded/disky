"""`disk-tree recompress` / `disk-tree listing-format` — rewrite v1 layer-2
listings as v2 in place, and audit which format files are in (spec
`listing-slim.md` phase 2)."""
from __future__ import annotations

import json

from click import argument, echo, option
from utz import err

from disk_tree.cli.base import cli
from disk_tree import listing_format as lf


def _hr(n: int | None) -> str:
    if n is None:
        return '-'
    x = float(n)
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if x < 1024 or unit == 'T':
            return f'{x:.0f}{unit}' if unit == 'B' else f'{x:.1f}{unit}'
        x /= 1024
    return f'{x:.1f}T'


@cli.command('recompress')
@option('-j', '--json', 'as_json', is_flag=True, help='Machine-readable report (one JSON document on stdout)')
@option('-k', '--keep', is_flag=True, help='Keep the old file beside the new one as `<stem>.v1.parquet`')
@option('-n', '--dry-run', is_flag=True, help='List what would be rewritten (scan root, implied columns, size); write nothing')
@argument('paths', nargs=-1, required=True)
def recompress_cmd(as_json: bool, keep: bool, dry_run: bool, paths: tuple[str, ...]):
    """Rewrite old-format (v1) layer-2 listings as v2 in place, lossless.

    PATHS are parquet files, directories (recursive `*.parquet`; sidecars
    skipped) or fsspec URLs (`gs://…`, `r2://…`, `s3://…`). A v2 file under
    `$DISK_TREE_PARQUET_CODEC` is skipped; one under another codec is
    re-encoded (same columns and keys). Each rewrite streams by row group
    (never a whole file in memory), drops `uri` and the pivot columns equal to
    `size`, re-encodes under `$DISK_TREE_PARQUET_CODEC` in ≤64K-row groups,
    verifies the row count and an order-insensitive digest of `(path, size,
    mtime, kind)` against the original, then swaps (atomic rename; copy +
    delete on a URL). A file that
    fails verification, or whose `uri` column is not `<root>/<path>` on every
    row, is left untouched and reported; the exit status is then 1.
    """
    from disk_tree.recompress import RecompressError, Result, expand, recompress
    files = expand(list(paths))
    results: list[Result] = []
    failures: list[tuple[str, str]] = []
    for f in files:
        try:
            r = recompress(f, keep=keep, dry_run=dry_run)
        except RecompressError as e:
            failures.append((f, str(e)))
            err(f"{f}: {e}")
            continue
        results.append(r)
        if not as_json:
            echo(_line(r))
    rewritten = [r for r in results if r.status == 'rewritten']
    planned = [r for r in results if r.status == 'planned']
    skipped = [r for r in results if r.status == 'skipped']
    old = sum(r.old_size for r in rewritten)
    new = sum(r.new_size for r in rewritten)
    if as_json:
        echo(json.dumps({
            'results': [r.asdict() for r in results],
            'failures': [{'path': p, 'error': e} for p, e in failures],
            'totals': {
                'rewritten': len(rewritten), 'planned': len(planned), 'skipped': len(skipped), 'failed': len(failures),
                'old_size': old, 'new_size': new, 'ratio': (new / old) if old else None,
            },
        }, indent=2))
    else:
        verb = 'would rewrite' if dry_run else 'rewrote'
        n = len(planned) if dry_run else len(rewritten)
        parts = [f"{verb} {n} file(s)"]
        if rewritten:
            parts.append(f"{_hr(old)} → {_hr(new)} ({100 * new / old:.1f}%)" if old else f"{_hr(old)} → {_hr(new)}")
        if planned:
            parts.append(f"{_hr(sum(r.old_size for r in planned))} before")
        parts.append(f"{len(skipped)} already v2")
        if failures:
            parts.append(f"{len(failures)} failed")
        echo(', '.join(parts))
    if failures:
        raise SystemExit(1)


def _line(r) -> str:
    imp = f" implied {','.join(r.implied)}" if r.implied else ''
    if r.status == 'skipped':
        return f"{r.path}: already v2 {r.codec} ({_hr(r.old_size)}, {r.rows:,} rows)"
    recoded = f", {r.recoded} → {lf.codec()}" if r.recoded else ''
    if r.status == 'planned':
        return f"{r.path}: would rewrite ({_hr(r.old_size)}, {r.rows:,} rows, root {r.scan_root}{imp}{recoded})"
    kept = f", old kept at {r.kept}" if r.kept else ''
    return f"{r.path}: {_hr(r.old_size)} → {_hr(r.new_size)} ({100 * r.ratio:.1f}%, {r.rows:,} rows, root {r.scan_root}{imp}{recoded}{kept})"


@cli.command('listing-format')
@option('-j', '--json', 'as_json', is_flag=True, help='One JSON document on stdout')
@argument('paths', nargs=-1, required=True)
def listing_format_cmd(as_json: bool, paths: tuple[str, ...]):
    """Print each parquet's layer-2 listing format (v1 | v2), codec, row groups,
    rows and size — a footer read per file. PATHS as for `recompress`; a v1
    line is a `recompress` candidate.
    """
    from disk_tree.recompress import expand, info
    infos = [info(f) for f in expand(list(paths))]
    if as_json:
        echo(json.dumps([i.asdict() for i in infos], indent=2))
        return
    for i in infos:
        imp = f" implied {','.join(i.implied)}" if i.implied else ''
        root = f" root {i.scan_root}" if i.scan_root else ''
        v = f'v{i.version}' if i.listing else 'not-a-listing'
        echo(f"{i.path}: {v} {i.codec} {i.row_groups} group(s) {i.rows:,} rows {_hr(i.size)}{root}{imp}")
