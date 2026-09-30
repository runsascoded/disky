#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pandas", "pyarrow"]
# ///
"""Deterministic layer-2 fixture for the Pages Functions tests: the schema
`StorageBackend.save` writes (`storage/base.py` columns), sorted `(depth,
path)`, in **4-row groups** so row-group pruning is exercised on a 12-row file
(groups: `['.', a, b, t1]`, `[t2, a/c, a/f1, a/f2]`, `[b/h1, b/h2, a/c/g1,
a/c/g2]`). Beside it, the `.scan.json` manifest `reduce --to` would leave.
Regenerate with `./gen.py` (content-stable; pyarrow's writer version tag may
change the bytes)."""
import json
from os.path import dirname, join

import pandas as pd

ROOT = '/fixture'
T0 = 1_700_000_000

#: path, kind, size, n_desc, n_children — `uri`, `parent`, `depth`, `mtime` derived.
NODES = [
    ('.',      'dir',  3600, 11, 4),
    ('a',      'dir',  1000,  5, 3),
    ('b',      'dir',  1100,  2, 2),
    ('t1',     'file',  700,  0, 0),
    ('t2',     'file',  800,  0, 0),
    ('a/c',    'dir',   700,  2, 2),
    ('a/f1',   'file',  100,  0, 0),
    ('a/f2',   'file',  200,  0, 0),
    ('b/h1',   'file',  500,  0, 0),
    ('b/h2',   'file',  600,  0, 0),
    ('a/c/g1', 'file',  300,  0, 0),
    ('a/c/g2', 'file',  400,  0, 0),
]


def write_v2(df: pd.DataFrame, out: str) -> None:
    """The same rows as a v2 listing (spec `listing-slim.md`): no `uri`, zstd,
    the scan root + v1 column order in the key-value metadata — what the
    duckdb/stream engines now write. `parquet.test.ts` reads both alike."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    tbl = pa.Table.from_pandas(df.drop(columns=['uri']), preserve_index=False)
    kv = {
        'disk_tree.listing_format': '2',
        'disk_tree.scan_root': ROOT,
        'disk_tree.columns': json.dumps(list(df.columns), separators=(',', ':')),
    }
    tbl = tbl.replace_schema_metadata({**(tbl.schema.metadata or {}), **kv})
    pq.write_table(tbl, out, row_group_size=4, compression='zstd', compression_level=3)


def main() -> None:
    here = dirname(__file__)
    rows = []
    for i, (path, kind, size, n_desc, n_children) in enumerate(NODES):
        depth = 0 if path == '.' else path.count('/') + 1
        parent = '' if path == '.' else ('.' if '/' not in path else path.rsplit('/', 1)[0])
        rows.append({
            'path': path, 'size': size, 'mtime': float(T0 + i * 3600), 'kind': kind, 'parent': parent,
            'uri': ROOT if path == '.' else f'{ROOT}/{path}',
            'n_desc': n_desc, 'n_children': n_children, 'depth': depth,
        })
    df = pd.DataFrame(rows).sort_values(['depth', 'path']).reset_index(drop=True)
    df.to_parquet(join(here, 'fixture.parquet'), index=False, row_group_size=4)
    write_v2(df, join(here, 'fixture-v2.parquet'))
    manifest = {
        'format': 'disk-tree-scan', 'version': 1, 'time': '2026-01-02T03:04:05',
        'path': ROOT, 'blob': 'fixture.parquet', 'size': 3600, 'n_children': 4, 'n_desc': 11,
        'mtime': T0, 'error_count': None, 'error_paths': None,
    }
    with open(join(here, 'fixture.parquet.scan.json'), 'w') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')
    print(df.to_string())

    # The `.groups.json` footer sidecar the serverless reader consumes instead
    # of parsing the thrift footer (`disk_tree.find.groups`; spec
    # `serverless-tier-reads.md`). Best-effort: this uv-script's shebang isolates
    # deps, so `disk_tree` imports only when run in the project venv — where a
    # full regen belongs. `groups.test.ts` asserts it reads identically to the
    # footer, so a stale sidecar fails loudly.
    try:
        from disk_tree.find.groups import write_groups
    except ImportError:
        print('disk_tree not importable — skipped fixture.groups.json (run in the project venv)')
    else:
        gs = write_groups(join(here, 'fixture.parquet'))
        print(f'groups → {gs.path} ({gs.n_groups} groups)')


if __name__ == '__main__':
    main()
