"""`disk-tree du` — top-N heaviest children per level of a cached scan.

The `du -d N | sort -rh` of an *indexed* tree: no filesystem walk, just a
depth-pushed-down parquet read, so "what is eating my disk" is answerable in
milliseconds against a scan taken hours ago.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from click import argument, option
from humanize import naturalsize

from disk_tree.cli.base import cli
from disk_tree.config import SQLITE_PATH


def _fmt(size: int, human: bool) -> str:
    return naturalsize(size, binary=True, format='%.3g') if human else str(size)


@cli.command('du')
@option('-a', '--all-kinds', is_flag=True, help='Include files (default: directories only)')
@option('-d', '--depth', default=1, help='Levels below URI to descend (default 1)')
@option('-H', '--no-human', is_flag=True, help='Print raw bytes instead of human-readable sizes')
@option('-j', '--json', 'as_json', is_flag=True, help='Emit JSON')
@option('-n', '--top', default=15, help='Max children shown per level (0 = all)')
@option('-p', '--path-index', 'path_index', envvar='DISK_TREE_PATH_INDEX', default=None, help="Read the site's path index instead of a scan blob: a `path-index.parquet`, or a root whose newest `<date>/index/<gen>/` is used (e.g. `r2://disk-tree/listing/laptop`; $DISK_TREE_PATH_INDEX)")
@option('-s', '--scan-id', default=None, help='Use a specific scan id (default: freshest covering URI)')
@argument('uri')
def du_cmd(all_kinds: bool, depth: int, no_human: bool, as_json: bool, top: int, path_index: str | None, scan_id: str | None, uri: str):
    """Biggest children of URI, per level, from the freshest covering scan (or the site's path index, `-p`)."""
    uri = uri.rstrip('/') or '/'
    if path_index and not scan_id:
        from disk_tree.path_index import latest_path_index, read_subtree, source_label
        src = latest_path_index(path_index)
        df, root_size = read_subtree(src, uri, depth)
        if not all_kinds:
            df = df[df['kind'] == 'dir']
        meta = {'source': source_label(src)}
        header = f"path index {source_label(src)}"
        _print_tree(df, root_size, uri, top, no_human, as_json, meta, header, recl_map=None, recl=lambda _: None)
        return

    from disk_tree.extents import read_reclaim_sidecar
    from disk_tree.registry import freshest_scan_covering
    from disk_tree.resolve import rebase_frame, resolve_blob, resolve_chunk_for_path
    from disk_tree.storage import get_backend

    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    scan = freshest_scan_covering(con, uri, scan_id)
    con.close()
    if not scan:
        raise SystemExit(f"no scan covering {uri!r}")

    rel = '.' if scan['path'] == uri else uri[len(scan['path'].rstrip('/') + '/'):]
    blob, rebased = resolve_chunk_for_path(scan['blob'], rel)
    recl_map = read_reclaim_sidecar(resolve_blob(scan['blob']))
    def _recl(sub_rel: str):
        # Sidecar keys are relative to the scan root; `rel` is the URI's offset
        # from it (`rebased` is only the offset within a resolved chunk).
        if recl_map is None:
            return None
        scanrel = sub_rel if rel == '.' else (rel if sub_rel == '.' else f'{rel}/{sub_rel}')
        return recl_map.get(scanrel)
    df = get_backend().load(blob, follow_refs=True, path_prefix=rebased if rebased != '.' else None)
    root_size = int(df.loc[df['path'] == rebased, 'size'].iloc[0]) if (df['path'] == rebased).any() else 0
    df = rebase_frame(df, rebased)
    # Depth 0 is the subtree root itself (present only when `rel` is the scan root).
    df = df[(df['depth'] >= 1) & (df['depth'] <= depth)]
    if not all_kinds:
        df = df[df['kind'] == 'dir']
    meta = {'scan_id': scan['id'], 'time': scan['time']}
    header = f"scan {scan['id']} of {scan['path']}, {str(scan['time'])[:19]}"
    _print_tree(df, root_size, uri, top, no_human, as_json, meta, header, recl_map=recl_map, recl=_recl)


def _print_tree(df, root_size: int, uri: str, top: int, no_human: bool, as_json: bool, meta: dict, header: str, *, recl_map, recl) -> None:
    """The per-level top-N tree of `df` (paths relative to `uri`), as JSON or text."""
    _recl = recl
    kids: dict[str, list[dict]] = {}
    for row in df.sort_values('size', ascending=False).itertuples():
        parent = row.path.rsplit('/', 1)[0] if '/' in row.path else ''
        kids.setdefault(parent, []).append({
            'path': row.path,
            'size': int(row.size),
            'mtime': float(row.mtime),
            'kind': row.kind,
            'n_desc': int(row.n_desc),
            'reclaimable': _recl(row.path),
        })

    def walk(parent: str) -> list[dict]:
        rows = kids.get(parent, [])
        shown = rows if top == 0 else rows[:top]
        out = []
        for r in shown:
            out.append({**r, 'children': walk(r['path'])})
        rest = rows[len(shown):]
        if rest:
            rest_recl = [r['reclaimable'] for r in rest]
            out.append({
                'path': f'{parent}/…' if parent else '…',
                'size': sum(r['size'] for r in rest),
                'mtime': 0.0,
                'kind': 'rest',
                'n_desc': sum(r['n_desc'] for r in rest),
                'reclaimable': sum(v for v in rest_recl if v is not None) if any(v is not None for v in rest_recl) else None,
                'children': [],
                'n_rest': len(rest),
            })
        return out

    tree = walk('')
    root_recl = _recl('.')
    if as_json:
        print(json.dumps({'uri': uri, **meta, 'size': root_size, 'reclaimable': root_recl, 'rows': tree}, indent=2, default=str))
        return

    human = not no_human
    print(f"{uri} — {_fmt(root_size, human)} ({header})")

    show_recl = recl_map is not None

    def rcol(r) -> str:
        if not show_recl:
            return ''
        v = r.get('reclaimable')
        return f"  {(_fmt(v, human) if v is not None else '—'):>9}"

    if show_recl:
        print(f"{'apparent':>9}{'  frees':>11}  path")

    def emit(rows: list[dict], indent: int):
        for r in rows:
            name = r['path'].rsplit('/', 1)[-1]
            if r['kind'] == 'rest':
                print(f"{_fmt(r['size'], human):>9}{rcol(r)}  {'  ' * indent}… {r['n_rest']} more"
                      if show_recl else
                      f"{_fmt(r['size'], human):>9}  {'':>10}  {'  ' * indent}… {r['n_rest']} more")
                continue
            when = datetime.fromtimestamp(r['mtime']).strftime('%Y-%m-%d') if r['mtime'] else ''
            suffix = '/' if r['kind'] == 'dir' else ''
            if show_recl:
                print(f"{_fmt(r['size'], human):>9}{rcol(r)}  {'  ' * indent}{name}{suffix}")
            else:
                print(f"{_fmt(r['size'], human):>9}  {when:>10}  {'  ' * indent}{name}{suffix}")
            emit(r['children'], indent + 1)

    emit(tree, 0)
