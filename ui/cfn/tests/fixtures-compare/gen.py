#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pandas", "pyarrow"]
# ///
"""Two scans of `/cmp` for the on-the-fly compare Function tests
(`compare.test.ts`): `grew` grows, `shrank` shrinks, `gone` is removed, `new` is
added, `same` is unchanged. Each blob is a flat layer-2 scan (`(depth,path)`
sorted, 4-row groups); beside each, the `.scan.json` a `reduce --to`/`index
--to` would leave — two scans of the *same* path so `getScans` orders them
(time asc) as ids 1 (A) and 2 (B)."""
import json
from os.path import dirname, join

import pandas as pd

T0 = 1_700_000_000

# path, kind, size, n_desc, n_children  (mtime derived from index)
A = [
    ('.',        'dir', 730, 8, 4),
    ('grew',     'dir', 100, 1, 1),
    ('shrank',   'dir', 300, 1, 1),
    ('gone',     'dir', 250, 1, 1),
    ('same',     'dir',  80, 1, 1),
    ('grew/x',   'file', 100, 0, 0),
    ('shrank/y', 'file', 300, 0, 0),
    ('gone/z',   'file', 250, 0, 0),
    ('same/s',   'file',  80, 0, 0),
]
B = [
    ('.',        'dir', 1180, 8, 4),
    ('grew',     'dir',  400, 1, 1),
    ('shrank',   'dir',  100, 1, 1),
    ('new',      'dir',  600, 1, 1),
    ('same',     'dir',   80, 1, 1),
    ('grew/x',   'file', 400, 0, 0),
    ('shrank/y', 'file', 100, 0, 0),
    ('new/w',    'file', 600, 0, 0),
    ('same/s',   'file',  80, 0, 0),
]


def _depth(p): return 0 if p == '.' else p.count('/') + 1
def _parent(p): return '' if p == '.' else ('.' if '/' not in p else p.rsplit('/', 1)[0])


def write(nodes, blob, t0, out):
    # Constant mtime across both scans, so a size-equal node reads as *unchanged*
    # (a differing mtime would make it `touched`). `t0` only sets the manifest
    # time that orders the two scans.
    rows = [{
        'path': p, 'size': size, 'mtime': float(T0), 'kind': kind, 'parent': _parent(p),
        'uri': '/cmp' if p == '.' else f'/cmp/{p}',
        'n_desc': n_desc, 'n_children': n_children, 'depth': _depth(p),
    } for p, kind, size, n_desc, n_children in nodes]
    df = pd.DataFrame(rows).sort_values(['depth', 'path']).reset_index(drop=True)
    here = dirname(__file__)
    df.to_parquet(join(here, blob), index=False, row_group_size=4)
    manifest = {
        'format': 'disk-tree-scan', 'version': 1, 'time': out['time'],
        'path': '/cmp', 'blob': blob, 'size': nodes[0][2], 'n_children': nodes[0][4],
        'n_desc': nodes[0][3], 'mtime': t0, 'error_count': None, 'error_paths': None,
    }
    with open(join(here, blob + '.scan.json'), 'w') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')
    return df


def main():
    a = write(A, 'cmpA.parquet', T0, {'time': '2026-01-02T03:04:05'})
    b = write(B, 'cmpB.parquet', T0 + 86400, {'time': '2026-01-03T03:04:05'})
    print(a.to_string()); print(); print(b.to_string())


if __name__ == '__main__':
    main()
