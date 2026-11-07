"""`dt_cloud.tree_mem`: a compaction's `-M` is its whole process tree's budget — DuckDB's limit is what this process, the
cut's spawned workers and the Arrow side's buffers (each a stated number of row groups / batches × bytes a row) leave —
and a compaction under a small one stays within it, measured over the tree."""
from __future__ import annotations

import json
import subprocess
import sys
import tracemalloc
import warnings
from pathlib import Path
from threading import Event, Thread

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import interval_compact as ic
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import parquet_pages as pp
from dt_cloud import static_names as sn
from dt_cloud import tree_mem as tm

MIB = tm.MIB


def test_fold_split():
    """The fold: `jobs × FOLD_GROUPS × rg_rows × rb` Arrow bytes, DuckDB the rest past this process."""
    b = tm.Budget(total=1 << 30, base=200 * MIB, proc_base=50 * MIB)
    assert tm.FOLD_GROUPS == 6
    assert b.fold(2, 65536, 100) == (78_643_200, (1 << 30) - 200 * MIB - 78_643_200)
    # Inputs of 1,000 rows: no group holds more.
    assert b.fold(2, 65536, 100, rows=1000) == (1_200_000, (1 << 30) - 200 * MIB - 1_200_000)


def test_cut_plan_most_workers():
    """8 cores, 4 GiB, rows of 128 B: half of what this process leaves (1,948 MiB) holds 8 workers (80 MiB each, + a
    writer's 4 spool groups' and 3 served groups' worth of rows: 35 MiB) and 8 runs' or pieces' sort batches (2 × 8 ×
    64Ki rows: 128 MiB). DuckDB gets 4,096 − 200 − 8 × 80 − 408."""
    b = tm.Budget(total=4096 * MIB, base=200 * MIB, proc_base=80 * MIB)
    assert tm.part_bytes(65536, 8192, 128) == 35 * MIB
    assert b.cut(1, 8, 8192, 128, 65536) == tm.CutPlan(at_once=1, procs=8, spool_rows=65536, arrow=408 * MIB, duck=2848 * MIB)
    # Two sorts at once split the cores: 4 workers each.
    assert b.cut(2, 8, 8192, 128, 65536) == tm.CutPlan(at_once=2, procs=4, spool_rows=65536, arrow=2 * (4 * 35 + 64) * MIB,
                                                     duck=(4096 - 200 - 8 * 80 - 408) * MIB)
    # A small budget (412 MiB for the workers): fewer of them (131 MiB each with its sort batches); none fitting, one.
    small = tm.Budget(total=1024 * MIB, base=200 * MIB, proc_base=80 * MIB)
    assert small.cut(1, 8, 8192, 128, 65536) == tm.CutPlan(at_once=1, procs=3, spool_rows=65536, arrow=(3 * 35 + 48) * MIB,
                                                         duck=(1024 - 200 - 3 * 80 - 153) * MIB)
    tiny = tm.Budget(total=600 * MIB, base=200 * MIB, proc_base=80 * MIB)
    assert tiny.cut(1, 8, 8192, 128, 65536).procs == 1
    # Sorts of 10,000 rows: each count charged at most that (2 × 10,000 rows sorting; a worker's largest task the join's
    # 16 MiB buffer): 4 workers fit (96 MiB each).
    assert small.cut(1, 8, 8192, 128, 65536, rows=10_000) == tm.CutPlan(
        at_once=1, procs=4, spool_rows=65536, arrow=4 * tm.CONCAT_BUF + 2 * 10_000 * 128,
        duck=(1024 - 200 - 4 * 80) * MIB - 4 * tm.CONCAT_BUF - 2 * 10_000 * 128)


def test_cut_plan_charges_the_writers_metadata():
    """Each writer keeps its part's groups' metadata until it closes (`group_meta`: 20 columns of 2,304 B, 32 pages of
    20 × 48 B + a 100-byte path's min and max): 1 MiB of it a group, a sort of 1,000 groups holds 1,000 MiB of it
    whatever its workers, and DuckDB is left that much less; past `WORKER_SHARE`, fewer workers don't help (each holds
    more groups), so it's still all charged."""
    meta = tm.group_meta(20, 32, 100)
    assert meta == 20 * 2304 + 32 * (20 * 48 + 200) == 83_200
    b = tm.Budget(total=8192 * MIB, base=200 * MIB, proc_base=80 * MIB)
    rows = 1000 * 8192
    plan = b.cut(1, 8, 8192, 128, 65536, rows, meta=MIB)
    assert plan == tm.CutPlan(at_once=1, procs=8, spool_rows=65536, arrow=(408 + 1000) * MIB, duck=(8192 - 200 - 8 * 80 - 408 - 1000) * MIB)
    # Two sorts at once of up to that many rows each: both their metadata.
    two = b.cut(2, 8, 8192, 128, 65536, 2 * rows, meta=MIB, sort_rows=rows)
    assert two == tm.CutPlan(at_once=2, procs=4, spool_rows=65536, arrow=(2 * (4 * 35 + 64) + 2 * 1000) * MIB,
                             duck=(8192 - 200 - 8 * 80 - 408 - 2000) * MIB)
    # More metadata than half the budget: one worker, and DuckDB what's left.
    big = b.cut(1, 8, 8192, 128, 65536, 5 * rows, meta=MIB)
    assert (big.procs, big.arrow) == (1, (35 + 16 + 5000) * MIB)


def test_append_split():
    """A scan's append: its one writer's 6 range-file groups of the ranges' rows, DuckDB the rest past this process."""
    b = tm.Budget(total=1 << 30, base=200 * MIB, proc_base=50 * MIB)
    assert b.append(65536, 100) == (39_321_600, (1 << 30) - 200 * MIB - 39_321_600)


def test_too_small_names_the_least_budget():
    b = tm.Budget(total=300 * MIB, base=200 * MIB, proc_base=80 * MIB)
    with pytest.raises(tm.TooSmall, match=r"^memory budget 300 MiB too small for 1 fold jobs: this process 200 MiB, 0 workers × 80 MiB, "
                                          r"Arrow buffers 24 MiB leave DuckDB 76 MiB \(< 128\); needs ≥ 352 MiB$"):
        b.fold(1, 65536, 64)


def test_sorted_writer_holds_under_a_group_and_writes_the_same_bytes(tmp_path):
    """Batches of any sizes (one past several groups among them): pending rows never exceed one group, every group is
    exactly `rg_rows` rows but the last, and the file's bytes are the same however the rows were batched."""
    n, rg = 1000, 64
    t = pa.table({"depth": pa.array([i % 5 for i in range(n)], pa.uint8()), "path": [f"a/{i:05d}" for i in range(n)], "vf": list(range(n))})
    files = {}
    for name, sizes in {"whole": [n], "ones": [1] * n, "mixed": [3, 61, 1, 200, 7, 64, 64, 600]}.items():
        batches, at = [], 0
        for k in sizes:
            batches.append(t.slice(at, k).combine_chunks().to_batches()[0])
            at += k
        assert at == n
        out = tmp_path / f"{name}.parquet"
        with sn.SortedWriter(out, t.schema, rg) as w:
            for b in batches:
                w.write([b])
                assert w.n_pending < rg
        assert (w.rows, w.peak_pending) == (n, rg)
        files[name] = out.read_bytes()
        assert [pq.ParquetFile(out).metadata.row_group(g).num_rows for g in range(pq.ParquetFile(out).num_row_groups)] == [rg] * 15 + [40]
    assert files["ones"] == files["whole"] == files["mixed"]


def test_fold_fetches_a_group_at_a_time(monkeypatch):
    """`compact_range`'s reads are `RANGE_RG`-row batches: a writer's pinned batches stay within `FOLD_GROUPS`."""
    sizes = []
    monkeypatch.setattr(sn, "_batches", lambda con, sql, size=1 << 17: sizes.append(size) or iter(()))
    assert list(ic._fetch(None, "SELECT 1")) == []
    assert sizes == [ist.RANGE_RG]


def test_row_bytes_is_the_widest_groups(tmp_path):
    """Per row: uncompressed bytes / rows (rounded up) + `STRING_T` a string column; the widest of the whole files' and of
    each group of ≥ `WIDE_ROWS` rows. Small groups count only in the whole: their bytes are mostly overhead."""
    n = ic.WIDE_ROWS
    f, small = tmp_path / "x.parquet", tmp_path / "small.parquet"
    pq.write_table(pa.table({"depth": pa.array([1] * n + [2] * n, pa.uint8()), "path": ["a"] * n + ["x" * 100] * n}), f, row_group_size=n,
                   compression="none", use_dictionary=False)
    pq.write_table(pa.table({"depth": pa.array([1, 1], pa.uint8()), "path": ["a", "b"]}), small, row_group_size=1)
    md, ms = pq.read_metadata(f), pq.read_metadata(small)
    wide = -(-md.row_group(1).total_byte_size // n) + ic.STRING_T
    assert md.row_group(1).total_byte_size > md.row_group(0).total_byte_size
    assert ic.row_bytes([str(f), str(tmp_path / "missing.parquet")]) == wide
    whole = -(-sum(m.row_group(g).total_byte_size for m in (md, ms) for g in range(m.num_row_groups)) // (2 * n + 2)) + ic.STRING_T
    assert ic.row_bytes([str(small)]) == -(-sum(ms.row_group(g).total_byte_size for g in range(2)) // 2) + ic.STRING_T
    assert whole < wide
    assert ic.row_bytes([str(f), str(small)]) == wide
    assert ic.row_bytes([]) == 1


def _many_groups(d: Path, parts: int, groups: int) -> list[Path]:
    """`parts` files of `groups` 4-row groups × 19 int columns each, page index on: footers of many column chunks."""
    out = []
    for i in range(parts):
        n = groups * 4
        p = d / f"part{i}.parquet"
        pq.write_table(pa.table({f"c{j}": pa.array(range(i * n, (i + 1) * n), pa.int64()) for j in range(19)}), p, row_group_size=4,
                       write_page_index=True, compression="zstd")
        out.append(p)
    return out


def _traced_peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_footers_stream_a_row_group_at_a_time(tmp_path):
    """The cut's join (`concat`) and its page-index reads hold a part's footer bytes and one row group's decoded tree,
    never the whole footer decoded (~40× its bytes as Python lists): over 2 × 1,000 groups × 19 chunks, the peak stays
    within 1.5× one part's footer, and the joined bytes are one writer's."""
    parts = _many_groups(tmp_path, 2, 1000)
    fb = len(pp.footer(parts[0]))
    whole = tmp_path / "whole.parquet"
    pq.write_table(pa.concat_tables([pq.read_table(p) for p in parts]), whole, row_group_size=4, write_page_index=True, compression="zstd")
    assert _traced_peak(lambda: pp.concat(parts, tmp_path / "out.parquet")) <= 1.5 * fb
    assert (tmp_path / "out.parquet").read_bytes() == whole.read_bytes()
    assert _traced_peak(lambda: sum(len(g) for g in pp.iter_chunk_indexes(parts[0]))) <= 1.5 * fb
    assert pp.chunk_indexes(parts[0]) == list(pp.iter_chunk_indexes(parts[0]))


# ── A cut worker, measured against its estimate ────────────────────────────

#: How far a cut worker's estimate (`tree_mem.worker_bytes`) may sit above what it is measured to hold: past it, the
#: estimate starves DuckDB of memory the workers never use.
WORKER_SLACK = 2


def _spool(path: Path, n: int) -> None:
    """A path sort's spool (`write_served`'s pass 1: its served columns and `__seg`), `n` rows in `CUT_SPOOL_RG`-row
    lz4 groups, ~70-byte paths."""
    schema = ist.served_schema("path", False, True)
    rng = np.random.default_rng(7)
    with pq.ParquetWriter(path, pa.schema([*schema, pa.field("__seg", pa.int32(), False)]), compression="lz4") as w:
        for a in range(0, n, ist.CUT_SPOOL_RG):
            idx = np.arange(a, min(n, a + ist.CUT_SPOOL_RG))
            cols = {}
            for f in schema:
                if f.name == "path":
                    col = [f"bucket-{i % 7}/some/longer/directory/name/{i:012d}/file-{i * 7919 % 100_000}.parquet" for i in idx]
                elif f.name == "kind":
                    col = np.where(idx % 3 == 0, "dir", "file").tolist()
                elif f.name == "us":
                    col = [f"user{i % 50}" for i in idx]
                elif f.name == "depth":
                    col = (idx % 20).astype(np.uint8)
                elif pa.types.is_floating(f.type):
                    col = rng.random(len(idx))
                else:
                    col = rng.integers(0, 1 << 30, len(idx))
                cols[f.name] = pa.array(col, f.type)
            cols["__seg"] = pa.array(np.zeros(len(idx), np.int32))
            w.write_table(pa.table(cols, schema=w.schema), row_group_size=ist.CUT_SPOOL_RG)


def _held() -> int:
    """This process's bytes for `_measured_part`: its proportional resident bytes (`tree_pss`, Linux), else its resident
    bytes (an upper bound there)."""
    return tm.tree_pss() if tm.has_procfs() else tm.own_bytes()


def _measured_part(spool: str, n: int, rg: int, page: int | None, out: str) -> tuple[int, int]:
    """In a spawned cut worker: what it holds once imported, and its peak while writing a part of `n` rows of `spool`
    (`cut_part`) in `rg`-row groups of `page`-row pages, sampled every 2 ms (and its `ru_maxrss` without procfs)."""
    import resource

    schema = ist.served_schema("path", False, True)
    base = _held()
    peak = [base]
    stop = Event()

    def sample():
        while not stop.wait(0.002):
            peak[0] = max(peak[0], _held())

    th = Thread(target=sample, daemon=True)
    th.start()
    try:
        ist.cut_part([(spool, 0, n)], [(0, o, min(rg, n - o)) for o in range(0, n, rg)], out, schema, "size", {"k": "v"}, page, rg)
    finally:
        stop.set()
        th.join()
    if not tm.has_procfs():
        peak[0] = max(peak[0], tm.peak_bytes())
    return base, max(peak[0], _held())


@pytest.mark.parametrize("rg, page", [(256, 8), (1024, 1024), (256, None), (1024, None)])
def test_a_cut_worker_holds_its_estimate(tmp_path, rg, page):
    """A real cut worker (spawned, `worker_init`'d, as `write_served`'s pool runs `cut_part`) writing a part of 400K rows
    from 64Ki-row spool groups: what it holds past its imports (PSS, sampled) is within `tree_mem.worker_bytes` of the
    plan's inputs — `rb` and the writer's metadata a group (`interval_store.writer_meta`) from the spool's footer — and
    that estimate is within `WORKER_SLACK` × it. Many small groups (32 pages each) make the writer's metadata the most
    of it; one-page groups, the spool's batches; a sort without pages keeps nothing a page (`writer_meta` with None)."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    n = 400_000
    spool = tmp_path / "spool.parquet"
    _spool(spool, n)
    with ProcessPoolExecutor(1, mp_context=get_context("spawn"), initializer=tm.worker_init) as ex:
        base, peak = ex.submit(_measured_part, str(spool), n, rg, page, str(tmp_path / "part.parquet")).result()
    measured = peak - base
    rb = ic.row_bytes([str(spool)]) + ist.SPOOL_EXTRA
    est = tm.worker_bytes(ist.CUT_SPOOL_RG, rg, rb, -(-n // rg), ist.writer_meta([str(spool)], rg, page))
    assert pq.read_metadata(tmp_path / "part.parquet").num_rows == n
    assert measured <= est <= WORKER_SLACK * measured, f"measured {measured / MIB:,.1f} MiB, estimated {est / MIB:,.1f} MiB"


# ── A compaction under a small budget, measured over its process tree ──────


#: Paths in each scan, scans in the base, scans appended as runs.
N_PATHS, BASE, RUNS = 600_000, 2, 2
DATES = [f"2026-09-{d:02d}" for d in range(1, BASE + RUNS + 1)]
LAYOUT = "listing/{id}/path-index.parquet"
#: The budget the compaction runs under, and the tolerance its measured peak gets (sampling every 10 ms; DuckDB's own
#: allocations past its buffer pool).
BUDGET, TOLERANCE = 768 * MIB, 0.10


def _scan(rng: np.random.Generator, j: int) -> pa.Table:
    """One v2 scan of `N_PATHS` paths (directories of 100 objects under 60 buckets), sizes drifting 5% a scan, 3% missing."""
    i = np.arange(N_PATHS)
    keep = rng.random(N_PATHS) >= 0.03
    i = i[keep]
    d = i // 100
    files = pa.array([f"b{x % 60:02d}/d{y:06d}/obj-{z:03d}.bin" for x, y, z in zip(d % 60, d, i % 100)])
    dirs = sorted({f"b{x % 60:02d}/d{y:06d}" for x, y in zip(d % 60, d)})
    size = 1000 + (i * 7919) % 100_000 + np.where(rng.random(len(i)) < 0.05 * j, rng.integers(1, 1000, len(i)), 0)
    n_dirs = len(dirs)
    t = pa.table({
        "path": pa.concat_arrays([pa.array(dirs), files]), "size": np.concatenate([np.full(n_dirs, 100 * 50_000), size]),
        "depth": pa.array(np.concatenate([np.full(n_dirs, 2), np.full(len(i), 3)]), pa.int32()),
        "kind": ["dir"] * n_dirs + ["file"] * len(i), "n_files": np.concatenate([np.full(n_dirs, 100), np.ones(len(i), int)]),
        "n_children": np.ones(n_dirs + len(i), int), "n_desc": np.ones(n_dirs + len(i), int), "mtime": np.full(n_dirs + len(i), 7),
        "mtime_mean": np.full(n_dirs + len(i), 1_700_000_000.25), "created": np.zeros(n_dirs + len(i), int),
        "last_read": pa.array(np.full(n_dirs + len(i), 5), pa.int32()),
        "sum_storage_class_id_2": np.zeros(n_dirs + len(i), int), "sum_storage_class_id_3": pa.nulls(n_dirs + len(i), pa.int64()),
        "sum_storage_class_id_4": np.zeros(n_dirs + len(i), int),
    })
    return t.sort_by([("depth", "ascending"), ("path", "ascending")])


@pytest.fixture(scope="module")
def generation(tmp_path_factory):
    """A base over two scans and two appended runs, uncompacted: what `add` would fold."""
    tmp = tmp_path_factory.mktemp("tree-mem")
    rng = np.random.default_rng(5)
    for j, d in enumerate(DATES):
        out = tmp / "scans" / LAYOUT.replace("{id}", d)
        out.parent.mkdir(parents=True)
        pq.write_table(_scan(rng, j), out, row_group_size=65536)
    common = ["-L", LAYOUT, "-s", str(tmp / "scans"), "-o", str(tmp / "store"), "-p", "4", "-M", "4GB"]
    for cmd, args in ((il.build_cmd, ["-g", "g", "-k", "4", "-t", DATES[BASE - 1], "-w", str(tmp / "wb")]),
                      *((il.add_cmd, ["-N", "-t", d, "-w", str(tmp / f"wa{d}")]) for d in DATES[BASE:])):
        res = CliRunner().invoke(cmd, [*common, *args], catch_exceptions=False)
        assert res.exit_code == 0, res.output
    gen_dir = tmp / "store" / ist.PREFIX / "g"
    newest = sorted((gen_dir / "manifests").iterdir(), key=lambda p: ar.parse_manifest(p.name))[-1]
    return {"tmp": tmp, "gen_dir": gen_dir, "runs": json.loads(newest.read_text())["runs"]}


FOLD = """
import json, sys
from pathlib import Path
from dt_cloud import interval_compact as ic
gen_dir, out, work, runs, mem = sys.argv[1:]
doc, rows, sorts, stats = ic.fold_gen(Path(gen_dir), Path(out), Path(work), gen="g", runs=json.loads(runs), threads=2, mem=mem, jobs=2)
print(json.dumps({"rows": rows, "sorts": {s: v["rows"] for s, v in sorts.items()}}))
"""


@pytest.mark.slow
@pytest.mark.skipif(not Path("/proc/self/smaps_rollup").exists(), reason="the tree's memory is read from procfs (Linux)")
def test_compaction_stays_within_its_budget(generation, tmp_path):
    """The fold of the base and both runs (two jobs, then the cut over every core) under a 768 MiB budget: the whole
    process tree's peak (sampled every 10 ms) stays within it, `TOLERANCE` aside; and the plan it logs says how it split."""
    g = generation
    proc = subprocess.Popen([sys.executable, "-c", FOLD, str(g["gen_dir"]), str(tmp_path / "out"), str(tmp_path / "work"), json.dumps(g["runs"]),
                             f"{BUDGET // MIB}MiB"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak, stop = [0], Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], tm.tree_pss(proc.pid))
            stop.wait(0.01)

    th = Thread(target=sample, daemon=True)
    th.start()
    out, log = proc.communicate()
    stop.set()
    th.join()
    assert proc.returncode == 0, log
    got = json.loads(out)
    plans = [line for line in log.splitlines() if line.startswith(("fold g: memory", "cut: memory"))]
    warnings.warn(f"compaction peak {peak[0] / MIB:,.0f} MiB of {BUDGET / MIB:,.0f}; rows {got['rows']}; {plans}", stacklevel=1)
    assert len(plans) == 2
    assert got["rows"]["pvl"] > N_PATHS
    assert peak[0] <= BUDGET * (1 + TOLERANCE), f"peak {peak[0] / MIB:,.0f} MiB over {BUDGET / MIB:,.0f}: {plans}"
