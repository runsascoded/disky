"""A drill (`staticDrill.test.ts`) whose root paths are not ASCII: what `Drill.rootsFloor` reads of a path chunk must
bound `hitsBytes`' string lengths (UTF-16 code units), not stand for them with UTF-8 bytes. `drill/` as
`../static-drill/gen.py` builds it (`dt_cloud.static_roots`, 4-row groups), over one scan: `bk/d/` holds `N` match
roots of `ckpt` (long) and `本` (short), named in CJK (3 UTF-8 bytes, one unit) with an emoji (4 bytes, two units).

Regenerate from the repo root:
`PYTHONPATH=cloud/src:src .venv/bin/python site/functions/_lib/fixtures/static-drill-utf8/gen.py`
"""
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('static_drill_gen', HERE.parent / 'static-drill' / 'gen.py')
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)

SCAN = '2026-10-04'
N = 12
LONG, SHORT = 'ckpt', '本'


def main() -> None:
    vf = sd.epoch(SCAN)
    paths = [f'bk/d/ckpt-日本語の検査点-\U0001F600-{i:02}' for i in range(N)]
    long_roots = [(LONG, 2, p, '', vf, sn.OPEN, 1000 + i, 1) for i, p in enumerate(paths)]
    short_roots = [(SHORT, 2, p, '', vf, sn.OPEN, 1000 + i, 1) for i, p in enumerate(paths)]
    out = HERE / 'drill'
    shutil.rmtree(out, ignore_errors=True)
    old = sr.ROOT_RG, sr.IDX_RG
    sr.ROOT_RG, sr.IDX_RG = sd.RG, sd.IDX_RG
    try:
        with tempfile.TemporaryDirectory() as tmp:
            con = sn.connect(1, '1GB', tmp)
            sd.build(con, long_roots, 'long', 's0000', out)
            sd.build(con, short_roots, 'short', 'g000', out)
        for kind in ('long', 'short'):
            sd.index(out, kind)
    finally:
        sr.ROOT_RG, sr.IDX_RG = old
    al = pa.table({'q': pa.array([LONG], pa.string()), 'canonical': pa.array([LONG], pa.string()),
                   'shard': pa.array([0], pa.int32()), 'n': pa.array([N], pa.int64())})
    pq.write_table(al, out / 'aliases.parquet', compression='zstd')
    meta = {'gen': 'fixture', 'R': sd.R, 'K': sd.K, 'rg': sd.RG, 'idx_rg': sd.IDX_RG, 'dispatch_rows': sd.R + 2 * sd.RG}
    for kind in ('long', 'short'):
        for sub in ('roots', 'rollups'):
            md = pq.ParquetFile(out / f'{kind}-{sub}-index.parquet').metadata
            meta[f'{kind}_{sub}'] = {'row_groups': md.num_rows, 'index_row_groups': md.num_row_groups}
    (out / 'meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    (HERE / 'scans.json').write_text(json.dumps({'bucket': 'fixture', 'scans': [{'id': SCAN}]}, indent=1) + '\n')


if __name__ == '__main__':
    main()
