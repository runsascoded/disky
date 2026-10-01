"""The reader's parquet fixtures, and the D1 rows that serve them.

- `path-index-zstd.parquet` (kept as committed): a **version-1** path index —
  the pre-store `write_index`'s dir-only rows with the wire names
  (`b, o, wts, wb, c2..c4, a`), zstd — for `zstd.test.ts` and the v1 side of
  `pathStore.test.ts`. The current writer no longer produces this shape, so it
  is never regenerated here; only its D1 rows are re-extracted.
- `v2/`: a **version-2** store generation (specs/path-store.md phase 1) cut by
  the current `dt_cloud.index.write_index` — every row, objects included, the
  layer-2 column names, no wire aliases, both sorts — over a listing shaped
  like `tests/test_tiers_plan.py`'s: a flat dir of `N_FLAT` objects whose sizes
  cycle 16 log2 buckets (so a `path` group of consecutive children spans every
  bucket while `bysize` holds the answers in one run), a 3-object dir, a nested
  dir, an empty object. Row groups are 2048 rows so the fixture has several.
- `<stem>.d1.json` beside each: `{variant: {schema, rows}}` as `index-sync`
  puts them in `index_schema` / `index_row_groups` (`index_footer.extract`),
  plus the served `.groups.json` blobs (`index_footer.groups_blob`).
- `v2/plans.json`: `disk-tree tiers plan -j -C` over each v2 sidecar for a
  set of reads — the engine planner's group selection, which the reader's
  span queries must reproduce exactly (`pathStore.test.ts`).

Regenerate: `.venv/bin/python site/functions/_lib/fixtures/gen.py` from the repo root.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from os.path import dirname, join

import duckdb
import pandas as pd

import dt_cloud.index as ix
from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.listing import prepare_listing
from dt_cloud.index_footer import extract, groups_blob

TS = datetime(2026, 9, 1, tzinfo=timezone.utc)
N_FLAT = 8000
MiB = 1 << 20
SORTS = {'path': 'path-index', 'bysize': 'path-index-bysize'}
#: The reads cross-checked against the reader (path, thr, atten, max_depth).
PLANS = [
    ('', 32768, 1, None),
    ('bk/flat', 32768, 1, None),
    ('bk/flat', 32768, 2, None),
    ('bk/small', 1, 1, None),
    ('bk/nest', MiB, 0.5, None),
    ('bk/nest', MiB, 0.5, 1),
    ('bk/nest', MiB, 2, None),
]


def v2_rows() -> list[tuple[str, int]]:
    rows = [(f'flat/f{i:05d}', 1 << (i % 16)) for i in range(N_FLAT)]
    rows += [(f'small/s{k}', 1000 * (k + 1)) for k in range(3)]
    rows += [(f'nest/a/b/c{k}', MiB) for k in range(4)]
    rows += [('nest/a/z', MiB // 4), ('empty.bin', 0)]
    return rows


def write_text(path: str, text: str) -> None:
    """A text fixture, ending in one newline."""
    with open(path, 'w') as f:
        f.write(text.rstrip('\n') + '\n')


def d1_json(files: dict[str, str]) -> dict:
    """`{variant: {schema, rows}}` for a set of `variant → parquet` files."""
    out = {}
    for variant, parquet in files.items():
        schema, rows = extract(parquet)
        out[variant] = {'schema': schema, 'rows': rows}
    return out


def write_v2(here: str) -> None:
    out_dir = join(here, 'v2')
    shutil.rmtree(out_dir, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        listing = join(tmp, 'listing.parquet')
        rows = v2_rows()
        pd.DataFrame({
            'bucket': ['bk'] * len(rows),
            'name': [n for n, _ in rows],
            'size_bytes': [s for _, s in rows],
            'created': [TS] * len(rows),
            'storage_class_id': [1] * len(rows),
        }).to_parquet(listing)
        con = duckdb.connect()
        l2 = join(tmp, 'l2.parquet')
        aggregate_listing_to_parquet(prepare_listing(con, (listing,)), bucket='bk', scheme='s3', out_parquet=l2, con=con, mean_mtime=True)
        ix.ROW_GROUP_SIZE = 2048
        summary = ix.write_index([('bk', l2)], join(tmp, 'out'), mem='1GB', threads=1)
        print(json.dumps({'rows': summary['rows'], 'columns': summary['columns'], 'sorts': summary['sorts']}, indent=2), file=sys.stderr)
        shutil.os.makedirs(out_dir)
        files = {}
        for variant, stem in SORTS.items():
            dst = join(out_dir, f'{stem}.parquet')
            shutil.copy(join(tmp, 'out', f'{stem}.parquet'), dst)
            files[variant] = dst
    d1 = d1_json(files)
    for variant, stem in SORTS.items():
        write_text(join(out_dir, f'{stem}.groups.json'), groups_blob(d1[variant]['schema'], d1[variant]['rows']))
    write_text(join(out_dir, 'd1.json'), json.dumps(d1, separators=(',', ':')))
    plans = []
    for path, thr, atten, max_depth in PLANS:
        for variant, stem in SORTS.items():
            cmd = ['disk-tree', 'tiers', 'plan', '-j', '-C', '-t', variant, '-a', str(atten)]
            if max_depth is not None:
                cmd += ['-d', str(max_depth)]
            cmd += [join(out_dir, f'{stem}.groups.json'), path, str(thr)]
            plans.append(json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout))
    write_text(join(out_dir, 'plans.json'), json.dumps(plans, indent=1))


def write_v2_lens(here: str) -> None:
    """`v2-lens/`: the v2 generation with an owner label (`usr`) — `nest`'s
    subtree is `alice`'s, `flat`'s `bob`'s, the rest unclaimed — so a lens view
    on a store generation (which writes no `user` sort; `path-index -U`) has
    rows to filter. `path` + `bysize` only, as gcs writes them."""
    out_dir = join(here, 'v2-lens')
    shutil.rmtree(out_dir, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        listing = join(tmp, 'listing.parquet')
        rows = v2_rows()
        pd.DataFrame({
            'bucket': ['bk'] * len(rows),
            'name': [n for n, _ in rows],
            'size_bytes': [s for _, s in rows],
            'created': [TS] * len(rows),
            'storage_class_id': [1] * len(rows),
        }).to_parquet(listing)
        con = duckdb.connect()
        bare = join(tmp, 'bare.parquet')
        aggregate_listing_to_parquet(prepare_listing(con, (listing,)), bucket='bk', scheme='s3', out_parquet=bare, con=con, mean_mtime=True)
        l2 = join(tmp, 'l2.parquet')
        con.execute(f"""
            COPY (
              SELECT path,
                CASE WHEN path = 'nest' OR path LIKE 'nest/%' THEN 'alice'
                     WHEN path = 'flat' OR path LIKE 'flat/%' THEN 'bob' END AS usr,
                * EXCLUDE (path)
              FROM read_parquet('{bare}')
            ) TO '{l2}' (FORMAT parquet)
        """)
        ix.ROW_GROUP_SIZE = 2048
        ix.write_index([('bk', l2)], join(tmp, 'out'), mem='1GB', threads=1)
        shutil.os.makedirs(out_dir)
        files = {}
        for variant, stem in SORTS.items():
            dst = join(out_dir, f'{stem}.parquet')
            shutil.copy(join(tmp, 'out', f'{stem}.parquet'), dst)
            files[variant] = dst
    d1 = d1_json(files)
    for variant, stem in SORTS.items():
        write_text(join(out_dir, f'{stem}.groups.json'), groups_blob(d1[variant]['schema'], d1[variant]['rows']))
    write_text(join(out_dir, 'd1.json'), json.dumps(d1, separators=(',', ':')))


def write_v1(here: str) -> None:
    parquet = join(here, 'path-index-zstd.parquet')
    write_text(join(here, 'path-index-zstd.d1.json'), json.dumps(d1_json({'path': parquet}), separators=(',', ':')))


def main() -> None:
    here = dirname(__file__)
    write_v1(here)
    write_v2(here)
    write_v2_lens(here)


if __name__ == '__main__':
    main()
