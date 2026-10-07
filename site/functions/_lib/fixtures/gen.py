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
  plus the served `.groups.json` blobs (`index_footer.groups_blob`) and, for
  the v2 sorts, the cold footer tier `.groups.parquet`
  (`index_footer.write_groups_parquet`) at `FOOTER_ROWS` rows per footer
  group, so a retired generation's footer reads prune between groups.
- `v2-search/`: a store generation over names shaped for the filter view
  (`ttl` dirs nested in `ttl` dirs, case variants, `.safetensors` objects, a
  `ckpt…final` path that only a cross-segment regex matches, a Kelvin-sign
  `Key`, 6000 filler objects so the `path` sort spans several 2048-row groups)
  in two buckets, written with the search sidecars
  (`path-index.{rows,trigrams,rows-search}.parquet`, layout v2,
  specs/path-store-search.md) at `SEARCH_ROWS_RG`-row rows groups, 2048-row
  postings groups and `SEARCH_DIR_ROWS` rows per directory group — beside
  layout v1's `path-index.{names,search}.parquet`, kept as committed (the
  writer no longer emits them; their postings are v2's).
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
from dt_cloud.index_footer import extract, groups_blob, write_groups_parquet

TS = datetime(2026, 9, 1, tzinfo=timezone.utc)
N_FLAT = 8000
MiB = 1 << 20
#: Rows per footer group of the fixtures' `.groups.parquet`: one, so the v2
#: sorts' 4 row groups are 4 footer groups the reader must prune between.
FOOTER_ROWS = 1
SORTS = {'path': 'path-index', 'bysize': 'path-index-bysize'}
#: Rows per directory group of the search fixture's directory: two, so a
#: query's directory reads prune between groups.
SEARCH_DIR_ROWS = 2
#: Rows per rows-file group of the search fixture: 12 groups, so candidates
#: scatter across them and the row budget can cut between them.
SEARCH_ROWS_RG = 512
#: The search sidecars the writer emits (layout v2).
SEARCH_SIDECARS = ('rows', 'trigrams', 'rows-search')
#: Layout v1's own sidecars, kept as committed: the writer no longer emits
#: them, and the reader still serves them (`search.test.ts` runs both). Its
#: postings are v2's (same ids, same `postings_rg_rows`).
SEARCH_V1_FILES = ('names', 'search')
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
        summary = ix.write_index([('bk', l2)], join(tmp, 'out'), mem='1GB', threads=1, row_group_rows=2048)
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
        write_groups_parquet(files[variant], d1[variant]['schema'], d1[variant]['rows'], row_group_rows=FOOTER_ROWS)
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
    subtree is `alice`'s, `flat`'s `bob`'s, the rest unowned — so a lens view
    on a store generation (which writes no `user` sort; `path-index -U`) has
    rows to filter. `path` + `bysize` + the one user-first copy, `bysize-user`,
    as gcs writes them (`path-index -u bysize`), each one group at the
    writer's default row-group size."""
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
        ix.write_index([('bk', l2)], join(tmp, 'out'), mem='1GB', threads=1, sort_variants=(('usr',),), variant_tiers=('bysize',))
        shutil.os.makedirs(out_dir)
        files = {}
        for variant, stem in {**SORTS, 'bysize-user': 'path-index-bysize-by-user'}.items():
            dst = join(out_dir, f'{stem}.parquet')
            shutil.copy(join(tmp, 'out', f'{stem}.parquet'), dst)
            files[variant] = dst
    d1 = d1_json(files)
    for variant, stem in {**SORTS, 'bysize-user': 'path-index-bysize-by-user'}.items():
        write_text(join(out_dir, f'{stem}.groups.json'), groups_blob(d1[variant]['schema'], d1[variant]['rows']))
        write_groups_parquet(files[variant], d1[variant]['schema'], d1[variant]['rows'], row_group_rows=FOOTER_ROWS)
    write_text(join(out_dir, 'd1.json'), json.dumps(d1, separators=(',', ':')))


def search_rows() -> dict[str, list[tuple[str, int]]]:
    """`v2-search`'s objects per bucket (key, size)."""
    bk = [(f'fill/f{i:05d}', 1 << (i % 12)) for i in range(6000)]
    bk += [('fill/zz-ttl-a/x.bin', 5000), ('fill/zz-TTL-b', 7000)]
    bk += [('tmp/ttl=14d/run-a/ckpt/x.bin', 4 * MiB), ('tmp/ttl=14d/run-a/y.bin', MiB), ('tmp/ttl=7d/z.bin', 2 * MiB), ('tmp/scratch/q.bin', 3 * MiB)]
    bk += [('iris/TTL-misc/inner-ttl/w.bin', 300), ('iris/TTL-misc/v.bin', 200), ('iris/notes.txt', 50)]
    bk += [('models/llama/model-00001-of-00002.safetensors', 8 * MiB), ('models/llama/model-00002-of-00002.safetensors', 8 * MiB), ('models/llama/config.json', 900), ('models/tiny.safetensors', 10)]
    bk += [('ckpt/final/step-100/a.bin', 6000), ('runs/grug/swarm/ckpt-final.pt', 9000), ('runs/grug/swarmy/b.pt', 100)]
    bk += [('\u212aey/k.bin', 77), ('keys/k2.bin', 88)]
    zz = [('Checkpoints/ttl/a.bin', 10), ('data/x.parquet', 20)]
    return {'bk': bk, 'zz': zz}


def write_v2_search(here: str) -> None:
    out_dir = join(here, 'v2-search')
    v1 = {}
    for side in SEARCH_V1_FILES:
        with open(join(out_dir, f'path-index.{side}.parquet'), 'rb') as f:
            v1[side] = f.read()
    shutil.rmtree(out_dir, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect()
        sources = []
        for bucket, rows in search_rows().items():
            listing = join(tmp, f'{bucket}.listing.parquet')
            pd.DataFrame({
                'bucket': [bucket] * len(rows),
                'name': [n for n, _ in rows],
                'size_bytes': [s for _, s in rows],
                'created': [TS] * len(rows),
                'storage_class_id': [1] * len(rows),
            }).to_parquet(listing)
            l2 = join(tmp, f'{bucket}.l2.parquet')
            aggregate_listing_to_parquet(prepare_listing(con, (listing,)), bucket=bucket, scheme='s3', out_parquet=l2, con=con, mean_mtime=True)
            sources.append((bucket, l2))
        summary = ix.write_index(
            sources, join(tmp, 'out'), mem='1GB', threads=1, row_group_rows=2048,
            search=True, search_opts={'rows_rg_rows': SEARCH_ROWS_RG, 'postings_rg_rows': 2048, 'dir_rg_rows': SEARCH_DIR_ROWS},
        )
        print(json.dumps({'rows': summary['rows'], 'sorts': summary['sorts'], 'search': {k: v for k, v in summary['search'].items() if k != 'files'}}, indent=2), file=sys.stderr)
        shutil.os.makedirs(out_dir)
        files = {}
        for variant, stem in SORTS.items():
            dst = join(out_dir, f'{stem}.parquet')
            shutil.copy(join(tmp, 'out', f'{stem}.parquet'), dst)
            files[variant] = dst
        for side in SEARCH_SIDECARS:
            shutil.copy(join(tmp, 'out', f'path-index.{side}.parquet'), join(out_dir, f'path-index.{side}.parquet'))
    for side, data in v1.items():
        with open(join(out_dir, f'path-index.{side}.parquet'), 'wb') as f:
            f.write(data)
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
    write_v2_search(here)


if __name__ == '__main__':
    main()
