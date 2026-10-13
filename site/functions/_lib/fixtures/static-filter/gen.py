"""The static filter's fixture (`staticFilter.test.ts`): two scans of one small store, each served two
ways, so the map's filter from the static name index can be checked against the filter from the
search sidecars and against brute force.

- `<date>/`: a version-2 store generation per scan (`path` + `bysize` sorts, 2048-row groups, the
  search sidecars at `../gen.py`'s settings, `d1.json`, `.groups.json`), cut by
  `dt_cloud.index.write_index` from per-owner layer-2s: each bucket's objects are aggregated once per
  owner and the results unioned with a `usr` column, so a directory holding two people's objects has
  two owner slices, as the real scans do.
- `sx/s0000.parquet`, `sx/s0001.parquet`, `shards.json`, `scans.json`: the static name index over
  both scans, shaped like `dt-cloud static-names` writes it (`../static-names/gen.py`): versions of
  `(depth, path, usr)` from the two generations' `path` sorts (a version per unchanged
  `(size, n_files)` run, `vt` the scan it changed or vanished at, open = 2106), one row per
  lowercase name suffix of ≥ 3 characters, sorted `(s, path, usr, vf)`, `RG`-row groups, two shards.
- `expected.json`: brute force straight from the objects: per term, view root and scan, the match
  roots (outermost paths under the root whose lowercase full path contains the term; none when the
  root itself matches) with their bytes and objects, sorted by path; and per term and root the
  series (Σ roots per scan).

Regenerate from the repo root (the engine and `dt_cloud` from this checkout):
`PYTHONPATH=cloud/src:src .venv/bin/python site/functions/_lib/fixtures/static-filter/gen.py`
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from os.path import dirname, join
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent.parent))
from gen import SEARCH_DIR_ROWS, SEARCH_ROWS_RG, SORTS, d1_json, write_text  # noqa: E402

import dt_cloud.index as ix  # noqa: E402
from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet  # noqa: E402
from disk_tree.listing import prepare_listing  # noqa: E402
from dt_cloud.index_footer import groups_blob  # noqa: E402

HERE = Path(__file__).parent
TS = datetime(2026, 9, 1, tzinfo=timezone.utc)
MiB = 1 << 20
A, B = '2026-10-04', '2026-10-05'
SCANS = (A, B)
RG = 16
SPLIT = 'm'
OPEN_MS = int(datetime(2106, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

#: Objects per scan: `bucket → [(key, size, owner | None)]`. `tomat` roots at every depth (a bucket, a
#: dir with two owners' objects, a file, a nested pair, case variants, a name holding it twice), changing between the scans.
BASE = {
    'bk': [(f'fill/f{i:05d}', 1 << (i % 12), None) for i in range(3000)] + [
        ('data/tomato/a.bin', 5000, 'alice'), ('data/tomato/b.bin', 3000, 'bob'),
        ('data/Tomatoes.csv', 700, None),
        ('data/raw/tomat-1/x.bin', 100, None), ('data/raw/tomat-1/TOMAT-inner/y.bin', 50, 'alice'),
        ('data/raw/plain.bin', 33, None),
        ('runs/x/ckpt-tomat.pt', 9000, 'alice'), ('runs/x/ckpt-2.pt', 1200, 'alice'), ('runs/x/tomat-tomat.bin', 77, None),
        ('models/llama/model.safetensors', 8 * MiB, 'bob'), ('models/tiny.safetensors', 10, None),
        ('tmp/ttl=7d/z.bin', 2 * MiB, None), ('tmp/ttl=14d/q.bin', MiB, 'bob'),
    ],
    'tomato-bk': [('a/b.bin', 100, None), ('c/tomat.txt', 10, 'carol')],
    'zz': [('Checkpoints/TOMAT/a.bin', 10, None), ('data/x.parquet', 20, None)],
}


def scan_objects(date: str) -> dict[str, list[tuple[str, int, str | None]]]:
    out = {b: list(objs) for b, objs in BASE.items()}
    if date == B:
        bk = [o for o in out['bk'] if o[0] not in ('data/Tomatoes.csv', 'tmp/ttl=7d/z.bin')]
        bk = [(k, 9500 if k == 'runs/x/ckpt-tomat.pt' else s, u) for k, s, u in bk]
        bk += [('data/tomato/c.bin', 2000, 'alice'), ('new-tomat/n.bin', 400, None)]
        out['bk'] = bk
        out['zz'] = [(k, 25 if k == 'data/x.parquet' else s, u) for k, s, u in out['zz']]
    return out


TERMS = ['tomat', 'ttl', 'ckpt', 'safetensors', 'x.bin', 'nomatch']
ROOTS = ['', 'bk', 'bk/data', 'bk/data/raw', 'bk/data/tomato', 'zz', 'tomato-bk', 'bk/runs/x']


def write_generation(date: str, out_dir: Path) -> list[dict]:
    """The scan's store generation; returns its `path` sort's rows (path, depth, usr, size, n_files)."""
    shutil.rmtree(out_dir, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect()
        sources = []
        for bucket, objs in scan_objects(date).items():
            parts = []
            for owner in sorted({u for _, _, u in objs}, key=lambda u: (u is not None, u or '')):
                mine = [(k, s) for k, s, u in objs if u == owner]
                listing = join(tmp, f'{bucket}.{owner}.listing.parquet')
                pd.DataFrame({
                    'bucket': [bucket] * len(mine), 'name': [k for k, _ in mine], 'size_bytes': [s for _, s in mine],
                    'created': [TS] * len(mine), 'storage_class_id': [1] * len(mine),
                }).to_parquet(listing)
                l2 = join(tmp, f'{bucket}.{owner}.l2.parquet')
                aggregate_listing_to_parquet(prepare_listing(con, (listing,)), bucket=bucket, scheme='s3', out_parquet=l2, con=con, mean_mtime=True)
                usr = 'NULL' if owner is None else f"'{owner}'"
                parts.append(f"SELECT path, {usr}::VARCHAR AS usr, * EXCLUDE (path) FROM read_parquet('{l2}')")
            l2 = join(tmp, f'{bucket}.l2.parquet')
            con.execute(f"COPY ({' UNION ALL '.join(parts)} ORDER BY depth, path, usr NULLS FIRST) TO '{l2}' (FORMAT parquet)")
            sources.append((bucket, l2))
        ix.write_index(
            sources, join(tmp, 'out'), mem='1GB', threads=1, row_group_rows=2048,
            search=True, search_opts={'rows_rg_rows': SEARCH_ROWS_RG, 'postings_rg_rows': 2048, 'dir_rg_rows': SEARCH_DIR_ROWS},
        )
        out_dir.mkdir(parents=True)
        files = {}
        for variant, stem in SORTS.items():
            dst = out_dir / f'{stem}.parquet'
            shutil.copy(join(tmp, 'out', f'{stem}.parquet'), dst)
            files[variant] = str(dst)
        for side in ('rows', 'trigrams', 'rows-search'):
            shutil.copy(join(tmp, 'out', f'path-index.{side}.parquet'), out_dir / f'path-index.{side}.parquet')
    d1 = d1_json(files)
    for variant, stem in SORTS.items():
        write_text(str(out_dir / f'{stem}.groups.json'), groups_blob(d1[variant]['schema'], d1[variant]['rows']))
    write_text(str(out_dir / 'd1.json'), json.dumps(d1, separators=(',', ':')))
    t = pq.read_table(files['path'], columns=['path', 'depth', 'usr', 'size', 'n_files']).to_pylist()
    return t


def ms(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


def versions(per_scan: dict[str, list[dict]]) -> list[dict]:
    """`(depth, path, usr)` versions over the scans: a new version when `(size, n_files)` changes, closed
    at the scan it changed or vanished at."""
    state: dict[tuple, dict] = {}
    out = []
    for date in SCANS:
        now = {(r['depth'], r['path'], r['usr'] or ''): (r['size'], r['n_files']) for r in per_scan[date]}
        for k, v in list(state.items()):
            if now.get(k) != (v['size'], v['n_files']):
                v['vt'] = ms(date)
                out.append(v)
                del state[k]
        for k, (size, n) in now.items():
            if k not in state:
                state[k] = {'depth': k[0], 'path': k[1], 'usr': k[2], 'vf': ms(date), 'vt': OPEN_MS, 'size': size, 'n_files': n}
    return out + list(state.values())


SX = pa.schema([
    pa.field('s', pa.string(), nullable=False), pa.field('depth', pa.uint8(), nullable=False),
    pa.field('path', pa.string(), nullable=False), pa.field('usr', pa.string(), nullable=False),
    pa.field('vf', pa.timestamp('ms', tz='UTC'), nullable=False), pa.field('vt', pa.timestamp('ms', tz='UTC'), nullable=False),
    pa.field('size', pa.int64(), nullable=False), pa.field('n_files', pa.int64(), nullable=False),
])


def write_shard(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [{**r, 'vf': datetime.fromtimestamp(r['vf'] / 1000, timezone.utc), 'vt': datetime.fromtimestamp(r['vt'] / 1000, timezone.utc)} for r in rows]
    t = pa.Table.from_pylist(rows, schema=SX)
    with pq.ParquetWriter(out, SX, compression='zstd', use_dictionary=['usr'], write_statistics=True, coerce_timestamps='ms') as w:
        for off in range(0, t.num_rows, RG):
            w.write_table(t.slice(off, RG), row_group_size=RG)


def write_static(per_scan: dict[str, list[dict]]) -> None:
    rows = []
    for v in versions(per_scan):
        name = v['path'].rsplit('/', 1)[-1].lower()
        for p in range(len(name) - 2):
            rows.append({'s': name[p:], **v})
    # Code-point order (the writer's `ORDER BY s, path, usr, vf`).
    rows.sort(key=lambda r: (r['s'], r['path'], r['usr'], r['vf']))
    lo = [r for r in rows if r['s'][:3] < SPLIT]
    hi = [r for r in rows if r['s'][:3] >= SPLIT]
    shutil.rmtree(HERE / 'sx', ignore_errors=True)
    write_shard(lo, HERE / 'sx' / 's0000.parquet')
    write_shard(hi, HERE / 'sx' / 's0001.parquet')
    shards = {'shards': [{'i': 0, 'lo': '   ', 'hi': SPLIT, 'rows': len(lo), 'prefixes': len({r['s'][:3] for r in lo})},
                         {'i': 1, 'lo': SPLIT, 'hi': None, 'rows': len(hi), 'prefixes': len({r['s'][:3] for r in hi})}]}
    write_text(str(HERE / 'shards.json'), json.dumps(shards, indent=1))
    write_text(str(HERE / 'scans.json'), json.dumps({'bucket': 'fixture', 'scans': [{'id': d} for d in SCANS]}, indent=1))


def brute(term: str, root: str, date: str) -> list[list] | None:
    """Match roots under `root` with their bytes and objects, from the objects alone; None when `root`
    itself matches (the whole view is matched)."""
    if term in root.lower():
        return None
    totals: dict[str, list[int]] = {}
    for bucket, objs in scan_objects(date).items():
        for key, size, _ in objs:
            full = f'{bucket}/{key}'
            if root and not full.startswith(root + '/'):
                continue
            segs = full.split('/')
            start = len(root.split('/')) if root else 0
            for i in range(start, len(segs)):
                if term in '/'.join(segs[:i + 1]).lower():
                    t = totals.setdefault('/'.join(segs[:i + 1]), [0, 0])
                    t[0] += size
                    t[1] += 1
                    break
    return [[p, b, o] for p, (b, o) in sorted(totals.items())]


def main() -> None:
    per_scan = {d: write_generation(d, HERE / d) for d in SCANS}
    write_static(per_scan)
    roots = {t: {r: {d: brute(t, r, d) for d in SCANS} for r in ROOTS} for t in TERMS}
    series = {t: {r: {d: [sum(x[1] for x in got), sum(x[2] for x in got)] for d in SCANS if (got := roots[t][r][d]) is not None} for r in ROOTS} for t in TERMS}
    write_text(str(HERE / 'expected.json'), json.dumps({'roots': roots, 'series': series}, indent=1))
    print(f"rows: {', '.join(f'{d} {len(r)}' for d, r in per_scan.items())}", file=sys.stderr)


if __name__ == '__main__':
    main()
