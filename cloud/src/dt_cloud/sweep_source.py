"""Reconstruct retired review inputs from the interval store for plan sweeps."""
from pathlib import Path

import pyarrow.parquet as pq

from .blobstore import Store
from .interval_export import scan_entry, store_gen
from .interval_store import PREFIX
from .static_names import connect, q


def review_source(
    store_root: str,
    scan: str,
    bucket: str,
    prefixes: list[str],
    exact: list[str],
    out: Path,
    *,
    gen: str | None = None,
    mem: str = '1GB',
    threads: int = 2,
) -> str:
    """Write only reviewed objects in bucket-relative L2 form, including available identities.

    Closed rows from later runs must participate in the fold before filtering
    by the reviewed time; otherwise an open base version could be resurrected.
    Each input row group is staged separately under the DuckDB memory limit.
    """
    store = Store(store_root)
    gen = store_gen(store, gen)
    root = f'{PREFIX}/{gen}'
    entry, runs = scan_entry(store, root, scan)
    if entry['version'] != 2:
        raise ValueError(f'{scan}: review source does not contain object rows')
    spans = [(bucket + '/' + p, bucket + '/' + p + '\U0010ffff') for p in prefixes]
    spans += [(bucket + '/' + p, bucket + '/' + p) for p in exact]
    tests = [*(f'starts_with(path, {q(bucket + "/" + p)})' for p in prefixes),
             *(f'path = {q(bucket + "/" + p)}' for p in exact)]
    where = ' OR '.join(tests) or 'false'
    out.parent.mkdir(parents=True, exist_ok=True)
    con = connect(threads, mem, out.parent / 'duckdb')
    initialized = False
    keys = [f'{root}/served/path.parquet', *(f'{root}/{r["key"]}/served/path.parquet' for r in runs)]
    identity = None
    try:
        for key in keys:
            groups = pq.read_table(store.open(key.removesuffix('.parquet') + '.groups.parquet'),
                                   columns=['rg', 'p_min', 'p_max', 'vf_min']).to_pylist()
            selected = [g['rg'] for g in groups if g['vf_min'] <= entry['ts'] and
                        any(g['p_max'] >= lo and g['p_min'] <= hi for lo, hi in spans)]
            if not selected:
                continue
            reader = pq.ParquetFile(store.open(key))
            available = set(reader.schema_arrow.names)
            field = 'version' if 'version' in available else 'generation' if 'generation' in available else None
            if initialized and field != identity:
                raise ValueError(f'{key}: inconsistent object identity schema')
            identity = field
            cols = ['depth', 'path', 'vf', 'vt', 'size', 'mtime', 'kind', *([field] if field else [])]
            for rg in selected:
                con.register('part', reader.read_row_group(rg, columns=cols))
                if not initialized:
                    con.execute('CREATE TABLE versions AS SELECT * FROM part WHERE false')
                    initialized = True
                con.execute(f'INSERT INTO versions SELECT * FROM part WHERE {where}')
                con.unregister('part')
        if not initialized:
            con.execute('CREATE TABLE versions(depth INTEGER, path VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, mtime BIGINT, kind VARCHAR)')
        token = f', {identity}' if identity else ''
        ts = int(entry['ts'])
        con.execute(f"""COPY (SELECT substr(path, {len(bucket) + 2}) AS path, size,
            nullif(mtime, -1) AS mtime, kind{token} FROM (
                SELECT * FROM versions QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1
            ) WHERE vf <= {ts} AND {ts} < vt AND kind = 'file' ORDER BY path)
            TO {q(str(out))} (FORMAT PARQUET)""")
    finally:
        con.close()
    return str(out)
