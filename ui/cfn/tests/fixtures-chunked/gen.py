#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pandas", "pyarrow"]
# ///
"""Deterministic hybrid-*chunked* fixture for the Pages Functions chunk-following
tests (`chunks.test.ts`). `HybridBackend` splits a depth-1 dir with ≥100K
descendants into its own `<uuid>.parquet` blob, leaving a summary row in the
parent stamped with `child_scan_id` (the chunk basename). We reproduce that in
miniature:

  chunked.parquet        root `/chunked`; `big` is a chunk stub (child_scan_id =
                         `chunk-big.parquet`, its subtree absent here), `small`
                         is a normal in-blob dir.
  chunk-big.parquet      `big`'s subtree, paths relative to the chunk root `.`
                         (no `child_scan_id` column — no nested chunks).
  chunked.parquet.scan.json  the manifest `getScans` lists (chunk blobs need none).

Regenerate with `./gen.py`."""
import json
from os.path import dirname, join

import pandas as pd

T0 = 1_700_000_000

#: main blob — path, kind, size, n_desc, n_children, child_scan_id
MAIN = [
    ('.',       'dir', 3000, 6, 2, None),
    ('big',     'dir', 2000, 3, 2, 'chunk-big.parquet'),  # chunk stub: subtree elsewhere
    ('small',   'dir', 1000, 2, 2, None),
    ('small/x', 'file', 600, 0, 0, None),
    ('small/y', 'file', 400, 0, 0, None),
]

#: chunk blob — `big`'s subtree, paths relative to the chunk root.
CHUNK = [
    ('.',   'dir', 2000, 3, 2),
    ('p',   'dir', 1200, 1, 1),
    ('q',   'file', 800, 0, 0),
    ('p/r', 'file', 1200, 0, 0),
]


def _depth(path: str) -> int:
    return 0 if path == '.' else path.count('/') + 1


def _parent(path: str) -> str:
    return '' if path == '.' else ('.' if '/' not in path else path.rsplit('/', 1)[0])


def main() -> None:
    here = dirname(__file__)

    main_rows = [{
        'path': p, 'size': size, 'mtime': float(T0 + i * 3600), 'kind': kind, 'parent': _parent(p),
        'uri': '/chunked' if p == '.' else f'/chunked/{p}',
        'n_desc': n_desc, 'n_children': n_children, 'depth': _depth(p), 'child_scan_id': child,
    } for i, (p, kind, size, n_desc, n_children, child) in enumerate(MAIN)]
    df = pd.DataFrame(main_rows).sort_values(['depth', 'path']).reset_index(drop=True)
    # Keep `child_scan_id` a nullable string column even though most rows are null.
    df['child_scan_id'] = df['child_scan_id'].astype('object')
    df.to_parquet(join(here, 'chunked.parquet'), index=False, row_group_size=4)

    chunk_rows = [{
        'path': p, 'size': size, 'mtime': float(T0 + i * 3600), 'kind': kind, 'parent': _parent(p),
        'uri': '/chunked/big' if p == '.' else f'/chunked/big/{p}',
        'n_desc': n_desc, 'n_children': n_children, 'depth': _depth(p),
    } for i, (p, kind, size, n_desc, n_children) in enumerate(CHUNK)]
    cdf = pd.DataFrame(chunk_rows).sort_values(['depth', 'path']).reset_index(drop=True)
    cdf.to_parquet(join(here, 'chunk-big.parquet'), index=False, row_group_size=4)

    manifest = {
        'format': 'disk-tree-scan', 'version': 1, 'time': '2026-01-02T03:04:05',
        'path': '/chunked', 'blob': 'chunked.parquet', 'size': 3000, 'n_children': 2, 'n_desc': 6,
        'mtime': T0, 'error_count': None, 'error_paths': None,
    }
    with open(join(here, 'chunked.parquet.scan.json'), 'w') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')
    print(df.to_string())
    print()
    print(cdf.to_string())


if __name__ == '__main__':
    main()
