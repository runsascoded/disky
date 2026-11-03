"""`static_append.merge_shards` (parallel, partitioned by three-character prefix) is byte-identical to
`merge_shards_serial` (one k-way merge into `write_run_shards`): every file it writes, with and without a plan."""
from __future__ import annotations

import json
import random
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_names as sn

PREFIXES = ["a", "ab", "abc", "abd", "b", "ba", "bab", "bac", "c", "é€x", "é€y", "zz", "zzz"]
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
OPEN = datetime(2100, 1, 1, tzinfo=timezone.utc)
HEAVY = "bab.x"


def _key_rows(rng: random.Random, n: int) -> list[tuple]:
    """`n` distinct suffix-row identities `(s, path, usr, vf)` over a few shared prefixes."""
    keys = set()
    while len(keys) < n:
        s = rng.choice(PREFIXES) + "".join(rng.choice("xyz.") for _ in range(rng.randrange(3)))
        keys.add((s, f"d{rng.randrange(6)}/{s}", rng.choice(["u1", "u2", "u3"]), rng.randrange(4)))
    return sorted(keys)


def _row(k: tuple, vt: datetime | None) -> dict:
    s, path, usr, vf = k
    h = sum(map(ord, path + usr)) + vf  # a version's other columns agree in every run holding it
    return {"s": s, "depth": h % 5, "path": path, "usr": usr, "vf": datetime.fromtimestamp(T0 + 86400 * vf, timezone.utc),
            "vt": vt or OPEN, "size": h * 7, "n_files": h % 3 + 1}


def _table(rows: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(sorted(rows, key=lambda r: (r["s"], r["path"], r["usr"], r["vf"])), schema=sn.SX_SCHEMA)


def _run(d: Path, rows: list[dict], target_rows: int) -> Path:
    sa.write_run_shards(_table(rows).to_batches(max_chunksize=5), d, target_rows)
    return d


def _mid_prefix_cuts(rows: list[dict], n: int) -> list[int]:
    """`n` row offsets that each split a three-character prefix's rows."""
    p3 = [r["s"][:3] for r in _table(rows).to_pylist()]
    return [i for i in range(1, len(p3)) if p3[i] == p3[i - 1]][::4][:n]


def _split_run(d: Path, rows: list[dict], cuts: list[int], sidecar: bool) -> Path:
    """A run whose shard files are cut at the given rows, with or without `sidecar.parquet`."""
    t = _table(rows)
    side = []
    for k, (a, b) in enumerate(zip([0, *cuts], [*cuts, t.num_rows])):
        stats: list = []
        dst = d / "sx" / f"s{k:04d}.parquet"
        sn.write_sorted(t.slice(a, b - a).to_batches(), dst, sn.SX_SCHEMA, sa.SX_RG,
                        on_group=lambda g: stats.append((g.column("s")[0].as_py(), g.column("s")[-1].as_py(), g.num_rows)),
                        dictionary=["usr"])
        side.append(sn.sidecar_rows(dst, f"sx/{dst.name}", stats))
    if sidecar:
        pq.write_table(pa.concat_tables(side), d / "sidecar.parquet")
    return d


def _files(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


@pytest.fixture
def small_rg():
    rg, sa.SX_RG = sa.SX_RG, 4
    prg, sa.PIECE_RG = sa.PIECE_RG, 3
    try:
        yield
    finally:
        sa.SX_RG, sa.PIECE_RG = rg, prg


@pytest.fixture
def inputs(tmp_path, small_rg):
    """Seven runs (oldest first) over one universe of versions: opens, close records of earlier runs' opens (a smaller
    `vt`, the same row otherwise), repeats of the same row; one run with a prefix split across its files and no
    sidecar, one with a sidecar, one empty, and one of a heavy suffix (`HEAVY`, a third of the rows: the key ranges
    split inside it)."""
    rng = random.Random(7)
    heavy = sorted({(HEAVY, f"h{i % 13}/{HEAVY}", f"u{i % 2}", i % 5) for i in range(130)})
    keys = sorted([*_key_rows(rng, 240), *heavy])
    runs = []
    for j in range(3):
        rows = [_row(k, None) for k in rng.sample(keys, 70)]
        closes = [_row(k, datetime.fromtimestamp(T0 + 86400 * (10 + j), timezone.utc)) for k in rng.sample(keys, 25)]
        seen = {(r["s"], r["path"], r["usr"], r["vf"]) for r in rows}
        rows += [r for r in closes if (r["s"], r["path"], r["usr"], r["vf"]) not in seen]
        runs.append(_run(tmp_path / f"run{j}", rows, target_rows=11))
    split = [_row(k, None) for k in rng.sample(keys, 60)]
    runs.append(_split_run(tmp_path / "split", split, _mid_prefix_cuts(split, 4), sidecar=False))
    side = [_row(k, None) for k in rng.sample(keys, 50)]
    runs.append(_split_run(tmp_path / "split-side", side, _mid_prefix_cuts(side, 3), sidecar=True))
    runs.append(_run(tmp_path / "empty", [], target_rows=11))
    runs.append(_run(tmp_path / "heavy", [_row(k, None) for k in heavy], target_rows=11))
    return runs


def test_ranges_split_a_heavy_suffix(inputs):
    """With 40 ranges, some bounds fall inside the heavy suffix's rows (its row groups' `path` statistics bound it)."""
    bounds = sa._bounds(sa._input_groups(inputs), 40)
    assert len([b for b in bounds if b[0] == HEAVY]) > 1


@pytest.mark.parametrize("run, n", [(3, 5), (4, 4)])
def test_split_input_spans_a_prefix(inputs, run, n):
    """The fixture's hard case: every file boundary of the split inputs falls inside one prefix's rows."""
    files = sorted((inputs[run] / "sx").glob("*.parquet"))
    last = [pq.read_table(f).column("s")[-1].as_py()[:3] for f in files[:-1]]
    first = [pq.read_table(f).column("s")[0].as_py()[:3] for f in files[1:]]
    assert len(files) == n
    assert last == first


@pytest.mark.parametrize("jobs", [1, 4])
@pytest.mark.parametrize("ranges", [1, 3, 7, 40])
@pytest.mark.parametrize("target_rows", [0, 1, 9, 25, 10_000])
def test_no_plan_equals_serial(inputs, tmp_path, jobs, ranges, target_rows):
    want, got = tmp_path / "want", tmp_path / "got"
    doc_want = sa.merge_shards_serial(inputs, want, target_rows)
    doc_got = sa.merge_shards(inputs, got, target_rows, jobs=jobs, ranges=ranges, tmp=tmp_path / "pieces")
    assert doc_got == doc_want
    assert _files(got) == _files(want)
    assert list((tmp_path / "pieces").iterdir()) == []


@pytest.mark.parametrize("target_rows", [0, 1, 9, 25])
def test_run_shards_cut_by_rows_not_batches(tmp_path, small_rg, target_rows):
    """`write_run_shards`' cuts depend on the rows alone: one batch, or a batch per row, writes the same bytes."""
    rng = random.Random(3)
    t = _table([_row(k, None) for k in _key_rows(rng, 90)])
    docs = [sa.write_run_shards(t.to_batches(max_chunksize=m), tmp_path / str(m), target_rows) for m in (1, 7, 1000)]
    assert docs[0] == docs[1] == docs[2]
    assert _files(tmp_path / "1") == _files(tmp_path / "7") == _files(tmp_path / "1000")


def test_no_plan_cuts_several_shards(inputs, tmp_path):
    want, got = tmp_path / "want", tmp_path / "got"
    sa.merge_shards_serial(inputs, want, 9)
    sa.merge_shards(inputs, got, 9, jobs=4, ranges=5)
    shards = json.loads((want / "shards.json").read_text())["shards"]
    assert len(shards) > 5
    assert _files(got) == _files(want)


@pytest.mark.parametrize("jobs", [1, 4])
def test_plan_equals_serial(inputs, tmp_path, jobs):
    """Cut to a plan, including shards whose prefixes hold no rows (written anyway)."""
    sa.merge_shards_serial(inputs, tmp_path / "cut", 20)
    cut = json.loads((tmp_path / "cut" / "shards.json").read_text())["shards"]
    los = sorted({"", *(sh["lo"] for sh in cut[1:]), "a~", "b", "\U0010ffff"})
    rows = [0] * len(los)
    every = pa.concat_tables(pq.read_table(f) for f in sorted((tmp_path / "cut" / "sx").glob("*.parquet")))
    for p3 in pc.utf8_slice_codeunits(every.column("s"), 0, 3).to_pylist():
        rows[bisect_right(los, p3) - 1] += 1
    plan = {"target_rows": 20, "shards": [{"i": k, "lo": lo, "rows": n} for k, (lo, n) in enumerate(zip(los, rows))]}
    assert (rows[los.index("a~")], rows[-1], sum(rows)) == (0, 0, every.num_rows)
    want, got = tmp_path / "want", tmp_path / "got"
    doc_want = sa.merge_shards_serial(inputs, want, plan=plan)
    doc_got = sa.merge_shards(inputs, got, plan=plan, jobs=jobs)
    assert doc_got == doc_want
    assert _files(got) == _files(want)


@pytest.mark.parametrize("jobs", [1, 4])
def test_empty_inputs(tmp_path, small_rg, jobs):
    runs = [_run(tmp_path / f"e{j}", [], 11) for j in range(2)]
    want, got = tmp_path / "want", tmp_path / "got"
    doc_want = sa.merge_shards_serial(runs, want, 9)
    doc_got = sa.merge_shards(runs, got, 9, jobs=jobs, ranges=3)
    assert doc_got == doc_want == {"rows": 0, "shards": 0, "row_groups": 0, "bytes": 0}
    assert _files(got) == _files(want)


def test_plan_wrong_rows_refused(inputs, tmp_path):
    sa.merge_shards_serial(inputs, tmp_path / "cut", 20)
    plan = json.loads((tmp_path / "cut" / "shards.json").read_text())
    plan["shards"][1]["rows"] += 1
    with pytest.raises(RuntimeError, match=r"^shard 1: "):
        sa.merge_shards(inputs, tmp_path / "got", plan=plan, jobs=1)
