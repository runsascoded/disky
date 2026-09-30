"""`disk-tree tiers plan` (`find/tier_plan.py`): the reader's span selection
run offline over a `.groups.json`, and `disk-tree tiers` (cut) over a local
path or a URL.

The fixture is shaped to show the point of the `bysize` sort (spec
path-store.md §1.3): a flat directory of 20,000 objects whose sizes cycle
through 16 log2 buckets, beside a small dir, a nested dir and an empty object.
At 2048-row groups the `path` sort spreads `flat/`'s children over every
group, so a thresholded read decodes them all; on `bysize` the rows above the
threshold are one run at the head of the file.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from click.testing import CliRunner

from disk_tree.cli.base import cli
from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.find.tier_plan import Group, Query, count_matched, load_groups, parquet_of, plan, tier_of
from disk_tree.find.tiers import write_tiers
from disk_tree.listing import prepare_listing

TS = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)
N_FLAT = 20000
MiB = 1 << 20


def _rows() -> list[tuple[str, int]]:
    rows = [(f'flat/f{i:05d}', 1 << (i % 16)) for i in range(N_FLAT)]
    rows += [(f'small/s{k}', 1000 * (k + 1)) for k in range(3)]
    rows += [(f'nest/a/b/c{k}', MiB) for k in range(4)]
    rows += [('nest/a/z', MiB // 4), ('empty.bin', 0)]
    return rows


@pytest.fixture(scope='module')
def tiers(tmp_path_factory) -> Path:
    """Both sorts + sidecars of the fixture layer-2, at 2048-row groups, under one dir."""
    d = tmp_path_factory.mktemp('plan')
    rows = _rows()
    listing = d / 'l.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * len(rows),
        'name': [n for n, _ in rows],
        'size_bytes': [s for _, s in rows],
        'created': [TS] * len(rows),
        'storage_class_id': [1] * len(rows),
    }).to_parquet(listing)
    layer2 = str(d / 'l2.parquet')
    con = duckdb.connect()
    aggregate_listing_to_parquet(prepare_listing(con, (str(listing),)), bucket='b1', scheme='gcs', out_parquet=layer2, con=con)
    assert write_tiers(layer2, str(d / 'l2'), row_group_rows=2048, groups=True) == {
        str(d / 'l2.path.parquet'): 20015,
        str(d / 'l2.bysize.parquet'): 20015,
    }
    return d


def _stats(groups: list[Group]) -> list[tuple]:
    return [(g.rg, g.d_min, g.d_max, g.p_min, g.p_max, g.b_min, g.b_max, g.row_start, g.row_end) for g in groups]


def test_query_bounds():
    root = Query(path='.', thr=1)
    assert (root.depth, root.d_lo, root.d_hi, root.p_lo, root.p_hi) == (0, 1, None, '', '￿')
    assert Query(path='', thr=1).p_lo == ''
    q = Query(path='nest/a', thr=1024, atten=0.5, max_depth=2)
    assert (q.depth, q.d_lo, q.d_hi, q.p_lo, q.p_hi) == (2, 3, 4, 'nest/a/', 'nest/a0')
    # `view.ts`: `threshold * atten ** max(0, depth - dP - 1)`
    assert [q.thr_at(d) for d in (1, 2, 3, 4, 5)] == [1024, 1024, 1024, 512, 256]


def test_tier_and_parquet_from_the_sidecar_name():
    assert tier_of('/x/l2.path.groups.json') == 'path'
    assert tier_of('r2://b/p/gcs-b1.bysize-by-usr.groups.json') == 'bysize'
    assert parquet_of('/x/l2.bysize.groups.json') == '/x/l2.bysize.parquet'
    with pytest.raises(ValueError, match="can't tell the tier from the name \\('dirs'\\); pass --tier"):
        tier_of('/x/gcs-b1.dirs.groups.json')
    with pytest.raises(ValueError, match='not a groups sidecar'):
        tier_of('/x/l2.path.parquet')


def test_sidecars_describe_the_two_sorts(tiers: Path):
    # `path`: (depth, path). Group 0 holds the root, the 4 dirs and `empty.bin`
    # (depth 1, size 0 → b_min 0), then flat/'s first files; groups 1–8 are
    # flat/ children only (every group sees all 16 sizes: b_max 2^15, b_min 1);
    # group 9 closes with the tail of flat/, `nest/a`, `small/*` and the
    # depth-3/4 rows under nest/.
    assert _stats(load_groups(str(tiers / 'l2.path.groups.json'))) == [
        (0, 0, 2, '.', 'small', 0, 86381198, 0, 2048),
        (1, 2, 2, 'flat/f02043', 'flat/f04090', 1, 32768, 2048, 4096),
        (2, 2, 2, 'flat/f04091', 'flat/f06138', 1, 32768, 4096, 6144),
        (3, 2, 2, 'flat/f06139', 'flat/f08186', 1, 32768, 6144, 8192),
        (4, 2, 2, 'flat/f08187', 'flat/f10234', 1, 32768, 8192, 10240),
        (5, 2, 2, 'flat/f10235', 'flat/f12282', 1, 32768, 10240, 12288),
        (6, 2, 2, 'flat/f12283', 'flat/f14330', 1, 32768, 12288, 14336),
        (7, 2, 2, 'flat/f14331', 'flat/f16378', 1, 32768, 14336, 16384),
        (8, 2, 2, 'flat/f16379', 'flat/f18426', 1, 32768, 16384, 18432),
        (9, 2, 4, 'flat/f18427', 'small/s2', 1, 4456448, 18432, 20015),
    ]
    # `bysize`: (⌊log2 size⌋ desc, path). Group 0 is every row ≥ 2^14 — the
    # root, `flat`, nest/'s rows, then the 1250 flat/ children at 2^15 and the
    # head of the 2^14 run; each later group is a band of buckets, `small`
    # (6000 → 2^12) and its children (2^9…2^11) landing in groups 2–4; the
    # size-0 `empty.bin` closes the file.
    assert _stats(load_groups(str(tiers / 'l2.bysize.groups.json'))) == [
        (0, 0, 4, '.', 'nest/a/z', 16384, 86381198, 0, 2048),
        (1, 2, 2, 'flat/f00012', 'flat/f19998', 4096, 16384, 2048, 4096),
        (2, 1, 2, 'flat/f00011', 'small', 2048, 6000, 4096, 6144),
        (3, 2, 2, 'flat/f00009', 'small/s2', 512, 3000, 6144, 8192),
        (4, 2, 2, 'flat/f00007', 'small/s0', 128, 1000, 8192, 10240),
        (5, 2, 2, 'flat/f00006', 'flat/f19991', 64, 128, 10240, 12288),
        (6, 2, 2, 'flat/f00004', 'flat/f19990', 16, 64, 12288, 14336),
        (7, 2, 2, 'flat/f00002', 'flat/f19988', 4, 16, 14336, 16384),
        (8, 2, 2, 'flat/f00001', 'flat/f19986', 2, 4, 16384, 18432),
        (9, 1, 2, 'empty.bin', 'flat/f19985', 0, 2, 18432, 20015),
    ]


def _plans(tiers: Path, q: Query) -> dict[str, tuple[tuple[int, ...], int, int]]:
    """Per tier: (selected group ids, rows decoded, rows matched)."""
    out = {}
    for t in ('path', 'bysize'):
        p = plan(str(tiers / f'l2.{t}.groups.json'), q)
        assert p.tier == t
        assert p.total_groups == 10 and p.total_rows == 20015
        assert p.groups == len(p.selected) and p.rows == sum(
            g.rows for g in load_groups(str(tiers / f'l2.{t}.groups.json')) if g.rg in p.selected
        )
        out[t] = (p.selected, p.rows, p.matched)
    return out


def test_flat_dir_thresholded(tiers: Path):
    """The point: 1,250 of flat/'s 20,000 children clear 32 KiB. On `path`
    every group holds some (each 2048-path run cycles all 16 sizes: no b_max
    prune, and group 0 spans depths so it can't be path-pruned) — 10 groups,
    all 20,015 rows decoded for 1,250 answers. On `bysize` the 2^15 run is one
    group: 2,048 rows decoded."""
    assert _plans(tiers, Query(path='flat', thr=32768)) == {
        'path': (tuple(range(10)), 20015, 1250),
        'bysize': ((0,), 2048, 1250),
    }


def test_root_thresholded(tiers: Path):
    # 1,250 flat/ objects + `flat`, `nest`, `nest/a`, `nest/a/b`, `nest/a/z` (256 KiB) + c0…c3.
    assert _plans(tiers, Query(path='.', thr=32768)) == {
        'path': (tuple(range(10)), 20015, 1259),
        'bysize': ((0,), 2048, 1259),
    }


def test_small_dir_pays_the_fixed_cost_on_both(tiers: Path):
    """3 answers. `path`: the depth-spanning group 0 (unprunable by path) and
    group 9, where small/'s children are. `bysize`: the two groups whose
    path range reaches small/'s children (2^9…2^11) — the ~one-group-per-bucket
    floor the spec says a small subtree pays, which is why it reads `path`
    whole instead (§1.2)."""
    assert _plans(tiers, Query(path='small', thr=1)) == {
        'path': ((0, 9), 3631, 3),
        'bysize': ((3, 4), 4096, 3),
    }


def test_attenuated_nested_read(tiers: Path):
    """thr 1 MiB at depth 2, halving per level: `nest/a` (4.25 MiB) ✓; depth 3
    `nest/a/b` (4 MiB ≥ 512 KiB) ✓, `nest/a/z` (256 KiB) ✗; depth 4 c0…c3
    (1 MiB ≥ 256 KiB) ✓ — 6 rows. `path` keeps groups 0 and 9 (both span
    depths); `bysize` reads down to `thr_min = thr · 0.5^(4−2)` = 256 KiB:
    group 0 alone."""
    q = Query(path='nest', thr=MiB, atten=0.5)
    assert _plans(tiers, q) == {
        'path': ((0, 9), 3631, 6),
        'bysize': ((0,), 2048, 6),
    }
    # Capped at one level below nest/: only `nest/a` passes; `bysize`'s floor is thr itself.
    assert _plans(tiers, Query(path='nest', thr=MiB, atten=0.5, max_depth=1)) == {
        'path': ((0, 9), 3631, 1),
        'bysize': ((0,), 2048, 1),
    }


def test_count_matched_is_the_answer_set(tiers: Path):
    """The matched count is the read's true answer set from the tier's rows,
    the same on either sort (same rows)."""
    q = Query(path='flat', thr=32768)
    assert count_matched(str(tiers / 'l2.path.parquet'), q) == 1250
    assert count_matched(str(tiers / 'l2.bysize.parquet'), q) == 1250
    assert count_matched(str(tiers / 'l2.path.parquet'), Query(path='flat', thr=0)) == N_FLAT
    assert count_matched(str(tiers / 'l2.path.parquet'), Query(path='.', thr=0)) == 20014


def test_plan_without_counting(tiers: Path):
    p = plan(str(tiers / 'l2.bysize.groups.json'), Query(path='flat', thr=32768), count=False)
    assert (p.selected, p.rows, p.matched, p.waste) == ((0,), 2048, None, None)
    with pytest.raises(ValueError, match="unknown tier 'coarse'"):
        plan(str(tiers / 'l2.bysize.groups.json'), Query(path='flat', thr=1), tier='coarse')


def test_pre_b_min_sidecar_loads(tmp_path: Path, tiers: Path):
    """A sidecar written before `b_min` was appended (11 fields) still plans."""
    doc = json.loads((tiers / 'l2.bysize.groups.json').read_text())
    doc['groups'] = [g[:11] for g in doc['groups']]
    old = tmp_path / 'old.bysize.groups.json'
    old.write_text(json.dumps(doc))
    groups = load_groups(str(old))
    assert [g.b_min for g in groups] == [None] * 10
    assert [g.b_max for g in groups][:3] == [86381198, 16384, 6000]
    doc['groups'][0] = doc['groups'][0][:5]
    old.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='group entry has 5 fields, expected 12'):
        load_groups(str(old))


def test_cli_plan(tiers: Path):
    r = CliRunner().invoke(cli, ['tiers', 'plan', str(tiers / 'l2.bysize.groups.json'), 'flat', '32768'])
    assert r.exit_code == 0, r.output
    # The byte figures follow the codec (`$DISK_TREE_PARQUET_CODEC`, zstd by
    # default, snappy in the `codec` fixture): normalize them, assert the rest.
    assert re.sub(r'bytes \S+/\S+', 'bytes <b>', r.output).split('\n') == [
        "bysize: P=flat (depth 1) thr=32768 atten=1 depths 2..∞ paths ['flat/', 'flat0')",
        "  groups 1/10  rows 2,048/20,015  bytes <b>  matched 1,250  waste 39.0%",
        "",
    ]
    r = CliRunner().invoke(cli, ['tiers', 'plan', '-j', '-C', '-a', '0.5', '-d', '1', str(tiers / 'l2.path.groups.json'), 'nest', str(MiB)])
    assert r.exit_code == 0, r.output
    groups = load_groups(str(tiers / 'l2.path.groups.json'))
    assert json.loads(r.output) == {
        'tier': 'path',
        'path': 'nest', 'depth': 1, 'thr': float(MiB), 'atten': 0.5, 'max_depth': 1,
        'd_lo': 2, 'd_hi': 2, 'p_lo': 'nest/', 'p_hi': 'nest0',
        'groups': 2, 'rows': 3631, 'bytes': groups[0].bytes + groups[9].bytes,
        'total_groups': 10, 'total_rows': 20015, 'total_bytes': sum(g.bytes for g in groups),
        'matched': None, 'waste': None,
        'selected': [0, 9],
    }


def test_cli_cut_over_a_url_and_a_url_stem(tiers: Path, tmp_path: Path):
    """`disk-tree tiers L2` (no subcommand: `cut`) from a `file://` layer-2 —
    the URL branch copies the source through `blobfs.open_read` — to a
    `file://` stem, uploading each tier + sidecar; the report is per tier."""
    out = tmp_path / 'out'
    out.mkdir()
    r = CliRunner().invoke(cli, ['tiers', f'file://{tiers / "l2.parquet"}', '-g', '-r', '2048', '-s', f'file://{out / "cut"}', '-j'])
    assert r.exit_code == 0, r.output
    reports = json.loads(r.output)
    assert [(x['path'], x['rows'], x['groups']) for x in reports] == [
        (f'file://{out / "cut.path.parquet"}', 20015, 10),
        (f'file://{out / "cut.bysize.parquet"}', 20015, 10),
    ]
    assert [x['kv']['sort'] for x in reports] == ['depth,path', 'size_bucket desc,path']
    assert sorted(p.name for p in out.iterdir()) == [
        'cut.bysize.groups.json', 'cut.bysize.parquet', 'cut.path.groups.json', 'cut.path.parquet',
    ]
    assert [x['bytes'] for x in reports] == [(out / 'cut.path.parquet').stat().st_size, (out / 'cut.bysize.parquet').stat().st_size]
    # The uploaded tiers are the local cut, byte for byte identical in content.
    assert (out / 'cut.bysize.groups.json').read_text() == (tiers / 'l2.bysize.groups.json').read_text()
    # A local layer-2 with no stem lands beside it; one tier by name.
    local = tmp_path / 'local'
    local.mkdir()
    (local / 'scan.parquet').write_bytes((tiers / 'l2.parquet').read_bytes())
    r = CliRunner().invoke(cli, ['tiers', 'cut', str(local / 'scan.parquet'), '-t', 'bysize'])
    assert r.exit_code == 0, r.output
    assert r.output == f"{local / 'scan.bysize.parquet'}: 20,015 rows, 3 group(s), {_hr((local / 'scan.bysize.parquet').stat().st_size)}  [bucket=log2 sort=size_bucket desc,path tier=bysize]\n"
    assert sorted(p.name for p in local.iterdir()) == ['scan.bysize.parquet', 'scan.parquet']


def _hr(n: int) -> str:
    from disk_tree.cli.tiers import _hr
    return _hr(n)
