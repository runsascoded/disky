"""The heavy-term drilldown fixture (`staticDrill.test.ts`): the static name index's `drill/` files (and the
base catalog the fleet root reads) over the two scans of `../static-filter/` (`2026-10-04`, `2026-10-05`),
built by `dt_cloud.static_roots` itself, so the Worker's reader is checked against the Python builder's
layout and against brute force from the objects.

- Versions of `(depth, path, usr)` from the two store generations' `path` sorts (`../static-filter/<date>/`),
  a version per unchanged `(size, n_files)` run, epoch seconds, open = 4291747200 (as `static-names`).
- `drill/`: `R` = 6 root rows, `K` = 3 kept children, 4-row data groups, 3-entry index row groups, so
  every path of the reader is crossed (roots reads, rollups with remainders, several index row groups,
  heavy ranges decided from the top file alone). Long members (`LONG`) are split over two shard files by
  `q`, and members with the same root set read their canonical's rows (`aliases.parquet`, as
  `drill-aliases` writes it: canonical = the set's least member); short members (`SHORT`) in one file.
  The concatenated indexes and their tops are cut exactly as `roots index` cuts them (`file` re-set as a
  nullable string column, sorted `(q_min, k_min)`).
- `catalog/`: every member's per-bucket cells (the fleet root's children), as `static_catalog` lays them out.
- `scans.json`: the base generation's scans.
- `expected.json`: brute force from the objects alone (`../static-filter/gen.py` `scan_objects`): per term,
  view path and scan, each child of the path holding a match root under it, `[bytes, objects]` (`views`),
  and the match roots themselves `[path, bytes, objects]` where there are at most `ROOTS_LISTED` (`roots`;
  null: more); null when the path itself matches.

Regenerate from the repo root:
`PYTHONPATH=cloud/src:src .venv/bin/python site/functions/_lib/fixtures/static-drill/gen.py`
"""
import importlib.util
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

HERE = Path(__file__).parent
SF = HERE.parent / 'static-filter'
spec = importlib.util.spec_from_file_location('static_filter_gen', SF / 'gen.py')
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)

SCANS = sf.SCANS
OPEN = sn.OPEN
R, K, RG, IDX_RG = 6, 3, 4, 3
CELL_RG = 4
#: `expected.json` lists a view's match roots when it has at most this many.
ROOTS_LISTED = 60
#: Long members (≥ 3 characters): light and heavy ones, and alias groups (equal root sets, e.g.
#: `afetensors` / `safetensor` / `safetensors`).
LONG = ['tomat', 'tomato', 'ttl', 'ckpt', 'ckpt-', 'safetensor', 'safetensors', 'afetensors', 'bin', '.bin', 'x.bin', 'f00', 'fil', 'data']
#: Short members (one or two characters).
SHORT = ['0', '.', 'f', 'a', 'x', 'om', 't']
#: Long members split over two shard files at this `q`.
SPLIT = 'f'


def epoch(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())


def versions() -> list[tuple]:
    """`(depth, path, usr, vf, vt, size, n_files)` over the scans (epoch seconds; `usr` '' = unowned)."""
    state: dict[tuple, list] = {}
    out = []
    for date in SCANS:
        rows = pq.read_table(SF / date / 'path-index.parquet', columns=['path', 'depth', 'usr', 'size', 'n_files']).to_pylist()
        now = {(r['depth'], r['path'], r['usr'] or ''): (r['size'], r['n_files']) for r in rows}
        for k, v in list(state.items()):
            if now.get(k) != (v[5], v[6]):
                v[4] = epoch(date)
                out.append(tuple(v))
                del state[k]
        for k, (size, n) in now.items():
            if k not in state:
                state[k] = [k[0], k[1], k[2], epoch(date), OPEN, size, n]
    return sorted(out + [tuple(v) for v in state.values()])


def is_root(t: str, path: str) -> bool:
    name = path.rsplit('/', 1)[-1].lower()
    parent = path.rsplit('/', 1)[0].lower() if '/' in path else ''
    return t in name and t not in parent


def roots_of(vs: list[tuple], terms: list[str]) -> list[tuple]:
    return sorted((t, *v) for v in vs if v[0] >= 1 for t in terms if is_root(t, v[1]))


def build(con, rows: list[tuple], kind: str, name: str, out: Path) -> None:
    con.execute('CREATE OR REPLACE TABLE rt (q VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT)')
    if rows:
        con.executemany('INSERT INTO rt VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
    sr.build_roots(con, 'rt', R, K, out / kind, name)


def index(out: Path, kind: str) -> None:
    """`roots index`'s concatenation for one kind: `file` relative to `drill/`, sorted, two levels."""
    for sub in ('roots', 'rollups'):
        tabs = []
        for f in sorted((out / kind / f'{sub}-index').glob('*.parquet')):
            t = pq.read_table(f)
            tabs.append(t.set_column(0, 'file', pa.array([f'{kind}/{x}' for x in t.column('file').to_pylist()], pa.string())))
        t = pa.concat_tables(tabs).sort_by([('q_min', 'ascending'), ('k_min', 'ascending')])
        top = sr.write_index_levels(t, out / f'{kind}-{sub}-index.parquet')
        pq.write_table(top, out / f'{kind}-{sub}-index.top.parquet', compression=sn.CODEC)
        shutil.rmtree(out / kind / f'{sub}-index')


CELL_SCHEMA = pa.schema([
    pa.field('q', pa.string(), nullable=False), pa.field('bucket', pa.string(), nullable=False),
    pa.field('vf', pa.int64(), nullable=False), pa.field('b', pa.int64(), nullable=False), pa.field('o', pa.int64(), nullable=False),
])
INDEX_SCHEMA = pa.schema([
    pa.field('rg', pa.int32(), nullable=False), pa.field('q_min', pa.string(), nullable=False), pa.field('q_max', pa.string(), nullable=False),
    pa.field('offset', pa.int64(), nullable=False), pa.field('length', pa.int64(), nullable=False), pa.field('rows', pa.int32(), nullable=False),
    pa.field('chunks', pa.list_(pa.int64()), nullable=False),
])


def catalog(roots: list[tuple], out: Path) -> None:
    """Every member's per-bucket cells from its roots' `+`/`−` events (`static_catalog`'s layout)."""
    by_q: dict[str, dict[tuple[str, int], list[int]]] = {}
    for t, _depth, path, _usr, vf, vt, size, n in roots:
        ev = by_q.setdefault(t, {})
        bucket = path.split('/', 1)[0]
        for at, sign in ((vf, 1), (vt, -1)):
            if at == OPEN:
                continue
            e = ev.setdefault((bucket, at), [0, 0])
            e[0] += sign * size
            e[1] += sign * n
    cell_rows = []
    for q in sorted(by_q):
        body, run = [], {}
        for (bucket, at), (db, dn) in sorted(by_q[q].items()):
            if db == 0 and dn == 0:
                continue
            b, o = run.get(bucket, (0, 0))
            run[bucket] = (b + db, o + dn)
            body.append(dict(q=q, bucket=bucket, vf=at, b=b + db, o=o + dn))
        cell_rows += [dict(q=q, bucket='', vf=0, b=-1 if len(q) <= 2 else len(by_q[q]), o=len(body))] + body
    out.mkdir(parents=True, exist_ok=True)
    t = pa.Table.from_pylist(cell_rows, schema=CELL_SCHEMA)
    with pq.ParquetWriter(out / 'cells.parquet', CELL_SCHEMA, compression='zstd', use_dictionary=['bucket'], write_statistics=False) as w:
        for off in range(0, t.num_rows, CELL_RG):
            w.write_table(t.slice(off, CELL_RG), row_group_size=CELL_RG)
    md = pq.ParquetFile(out / 'cells.parquet').metadata
    idx = {k: [] for k in INDEX_SCHEMA.names}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        chunks, starts, ends = [], [], []
        for c in range(rg.num_columns):
            cc = rg.column(c)
            d = cc.dictionary_page_offset or 0
            start = min(d, cc.data_page_offset) if d else cc.data_page_offset
            chunks += [cc.data_page_offset, cc.total_compressed_size, d]
            starts.append(start)
            ends.append(start + cc.total_compressed_size)
        group = cell_rows[g * CELL_RG:(g + 1) * CELL_RG]
        for k, v in zip(INDEX_SCHEMA.names, (g, group[0]['q'], group[-1]['q'], min(starts), max(ends) - min(starts), rg.num_rows, chunks)):
            idx[k].append(v)
    pq.write_table(pa.table(idx, schema=INDEX_SCHEMA), out / 'index.parquet', compression='zstd')
    meta = {'gen': 'fixture', 'cells_rows': len(cell_rows), 'row_groups': md.num_row_groups, 'bytes': (out / 'cells.parquet').stat().st_size,
            'cell_rg': CELL_RG, 'membership': {'max_rows': 999_998, 'max_bytes': None}}
    (out / 'meta.json').write_text(json.dumps(meta, indent=1) + '\n')


def brute(t: str, P: str, date: str) -> dict | None:
    """Per child of `P` holding a match root under it: `[bytes, objects]`, from the objects alone; None when
    `P` itself matches."""
    if t in P.lower():
        return None
    start = len(P.split('/')) if P else 0
    acc: dict[str, list[int]] = {}
    for bucket, objs in sf.scan_objects(date).items():
        for key, size, _ in objs:
            full = f'{bucket}/{key}'
            if P and not full.startswith(P + '/'):
                continue
            segs = full.split('/')
            for i in range(start, len(segs)):
                if t in segs[i].lower():
                    e = acc.setdefault(segs[start], [0, 0])
                    e[0] += size
                    e[1] += 1
                    break
    return dict(sorted(acc.items()))


def main() -> None:
    vs = versions()
    long_roots = roots_of(vs, LONG)
    short_roots = roots_of(vs, SHORT)
    sets: dict[str, frozenset] = {t: frozenset(r[1:] for r in long_roots if r[0] == t) for t in LONG}
    canon = {t: min(u for u in LONG if sets[u] == sets[t]) for t in LONG}
    out = HERE / 'drill'
    shutil.rmtree(out, ignore_errors=True)
    old = sr.ROOT_RG, sr.IDX_RG
    sr.ROOT_RG, sr.IDX_RG = RG, IDX_RG
    try:
        with tempfile.TemporaryDirectory() as tmp:
            con = sn.connect(1, '1GB', tmp)
            canonical = [r for r in long_roots if canon[r[0]] == r[0]]
            build(con, [r for r in canonical if r[0] < SPLIT], 'long', 's0000', out)
            build(con, [r for r in canonical if r[0] >= SPLIT], 'long', 's0001', out)
            build(con, short_roots, 'short', 'g000', out)
        for kind in ('long', 'short'):
            index(out, kind)
    finally:
        sr.ROOT_RG, sr.IDX_RG = old
    shard = {t: 0 if canon[t] < SPLIT else 1 for t in LONG}
    al = pa.table({'q': pa.array(sorted(LONG), pa.string()), 'canonical': pa.array([canon[t] for t in sorted(LONG)], pa.string()),
                   'shard': pa.array([shard[t] for t in sorted(LONG)], pa.int32()), 'n': pa.array([len(sets[t]) for t in sorted(LONG)], pa.int64())})
    pq.write_table(al, out / 'aliases.parquet', compression='zstd')
    meta = {'gen': 'fixture', 'R': R, 'K': K, 'rg': RG, 'idx_rg': IDX_RG, 'dispatch_rows': R + 2 * RG}
    for kind in ('long', 'short'):
        for sub in ('roots', 'rollups'):
            md = pq.ParquetFile(out / f'{kind}-{sub}-index.parquet').metadata
            meta[f'{kind}_{sub}'] = {'row_groups': md.num_rows, 'index_row_groups': md.num_row_groups}
    (out / 'meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    shutil.rmtree(HERE / 'catalog', ignore_errors=True)
    catalog(long_roots + short_roots, HERE / 'catalog')
    (HERE / 'scans.json').write_text(json.dumps({'bucket': 'fixture', 'scans': [{'id': d} for d in SCANS]}, indent=1) + '\n')
    dirs = sorted({v[1].rsplit('/', 1)[0] for v in vs if '/' in v[1]} | {v[1] for v in vs if v[0] == 1})
    paths = ['', *dirs, 'nope']
    terms = LONG + SHORT + ['nomatch', 'qq']
    expected = {t: {P: {d: brute(t, P, d) for d in SCANS} for P in paths} for t in terms}
    # The match roots themselves (`../static-filter/gen.py` `brute`), where they are few.
    roots = {t: {P: {d: r if r is None or len(r) <= ROOTS_LISTED else None for d in SCANS for r in [sf.brute(t, P, d)]} for P in paths} for t in terms}
    (HERE / 'expected.json').write_text(json.dumps({'paths': paths, 'aliases': {t: c for t, c in canon.items() if t != c}, 'views': expected, 'roots': roots}, separators=(',', ':')) + '\n')


if __name__ == '__main__':
    main()
