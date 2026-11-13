"""Which served sorts have pages (`interval_store.served_page_rows`, specs/diff-progressive.md §3b): the level sort, and no
other. A sort without them is the file the cut wrote before 2b.1. Through a build, appends, carries, a compaction,
`backfill-level` and `cut -s`.

From 2b.1 until this rule the cut paged every sort, so a store holds runs (and maybe a base) of that kind beside the ones
cut since. Such a store appends, checks, carries and compacts as one cut without pages throughout: only the readers open
a served sort, and they take each group as they find it."""
from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from test_interval_append import _views_equal
from test_interval_level import _add, _build, _cli, _newest_runs
from test_interval_local import BASE, DATES, RG, world  # noqa: F401 — the shared scans

from dt_cloud import interval_level as ilv
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud import tree_mem as tm
from dt_cloud.parquet_pages import chunk_indexes, page_locations

#: An unpaged sort's statistics; a paged one's add `path` (its pages' bounds).
UNPAGED_STATS = {"depth", "vf", "vt"}
#: A run's sorts in a store with an owner axis and no attribution table (`world`'s), the level sort aside; a base's add `reads`.
SORTS = {"path", "bysize", "slices", "slices-bytotal", "slices-bysize-user"}


def _want(dirs: int, paged: bool = False, level: bool = False) -> list[dict[str, bool]]:
    """What `_paged` is over a base's `served/` then `dirs - 1` runs': every sort `paged`, the level sort (with `level`)
    always."""
    lv = {ilv.LEVEL: True} if level else {}
    return [dict(sorted({**dict.fromkeys(SORTS | ({"reads"} if i == 0 else set()), paged), **lv}.items())) for i in range(dirs)]


def _indexed(f: Path) -> bool:
    """Whether served sort `f` has a page index: every chunk of every group (a sort is one or the other, never between),
    its sidecar's compact tuples agreeing."""
    has = {bool(ix.offset_index_length) for g in chunk_indexes(f) for ix in g}
    assert len(has) == 1, f
    tuples = {len(c) for r in pq.read_table(f.with_name(f.name.removesuffix(".parquet") + ".groups.parquet")).to_pylist() for c in json.loads(r["rg_json"])[2]}
    # A tuple's page-index ranges (4) follow its 3 offsets.
    assert tuples == ({7} if True in has else {3}), f
    return True in has


def _stats(f: Path) -> set[str]:
    md = pq.read_metadata(f)
    rg = md.row_group(0)
    return {rg.column(c).path_in_schema for c in range(md.num_columns) if rg.column(c).is_stats_set}


def _paged(d: Path) -> dict[str, bool]:
    """Each served sort in dir `d` (a base's or a run's `served/`) with rows: whether it has a page index, and `path`'s
    statistics only with one."""
    got = {p.stem: _indexed(p) for p in sorted(d.glob("*.parquet")) if not p.name.endswith(".groups.parquet") and pq.read_metadata(p).num_row_groups}
    for sort, paged in got.items():
        if sort != ilv.LEVEL:
            assert _stats(d / f"{sort}.parquet") & {"path"} == ({"path"} if paged else set()), (d, sort)
    return got


def _parquets(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*.parquet"))}


def _dirs(gen: Path, runs: list[str]) -> list[Path]:
    return [gen / "served", *(gen / "deltas" / r / "served" for r in runs)]


@contextmanager
def writer_2b1():
    """The cut as it was from 2b.1 until pages went back to the level sort alone: every sort paged (2-row pages here)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ist, "SERVED_PAGE_ROWS", 2)
        mp.setattr(ist, "served_page_rows", lambda sort: ist.SERVED_PAGE_ROWS)
        yield


@pytest.fixture(scope="module")
def plain(world, tmp_path_factory):  # noqa: F811
    """A base without the level sort, the rest added (one carry merges them)."""
    tmp = tmp_path_factory.mktemp("plain")
    store = tmp / "store"
    _build(world, store, tmp / "wb")
    _add(world, store, tmp / "wa")
    gen = store / ist.PREFIX / "g"
    return {"store": store, "gen": gen, "markers": {p.stem: json.loads(p.read_text()) for p in sorted((gen / "verify").glob("*.json"))}}


@pytest.fixture(scope="module")
def leveled(world, tmp_path_factory):  # noqa: F811
    """A `-l` base, the rest added (one carry merges them)."""
    tmp = tmp_path_factory.mktemp("lvl")
    store = tmp / "store"
    _build(world, store, tmp / "wb", "-l")
    _add(world, store, tmp / "wa")
    return {"store": store, "gen": store / ist.PREFIX / "g"}


@pytest.fixture(scope="module")
def mixed(world, tmp_path_factory):  # noqa: F811
    """A store as a deployment's is when this rule reaches it: the base and its first two runs cut by 2b.1's writer, then a
    run cut now beside them (`three`: no carry yet), then the last scan and the carry over all four runs."""
    tmp = tmp_path_factory.mktemp("mixed")
    store = tmp / "store"
    with writer_2b1():
        _build(world, store, tmp / "wb")
        _add(world, store, tmp / "w1", "-t", DATES[BASE + 1], "-N")
    _add(world, store, tmp / "w2", "-t", DATES[BASE + 2], "-N")
    shutil.copytree(store, tmp / "three")
    _add(world, store, tmp / "w3")
    gen = store / ist.PREFIX / "g"
    return {"tmp": tmp, "store": store, "gen": gen, "three": tmp / "three" / ist.PREFIX / "g",
            "markers": {p.stem: json.loads(p.read_text()) for p in sorted((gen / "verify").glob("*.json"))}}


def test_without_the_level_sort_nothing_has_pages(plain):
    """Base, level-0 runs and the merged run alike: every sort as the cut wrote it before 2b.1, `path` included."""
    gen = plain["gen"]
    merged = [r.name for r in _newest_runs(gen)]
    assert merged == [f"{DATES[BASE]}_{DATES[-1]}"]
    assert [_paged(d) for d in _dirs(gen, [*DATES[BASE:], *merged])] == _want(6)


def test_with_the_level_sort_only_it_has_pages(leveled):
    gen = leveled["gen"]
    merged = [r.name for r in _newest_runs(gen)]
    assert merged == [f"{DATES[BASE]}_{DATES[-1]}"]
    assert [_paged(d) for d in _dirs(gen, [*DATES[BASE:], *merged])] == _want(6, level=True)


def test_a_compaction_and_a_backfill_page_only_the_level_sort(world, leveled, plain, tmp_path):  # noqa: F811
    store = tmp_path / "l" / "store"
    shutil.copytree(leveled["store"], store)
    out = _add(world, store, tmp_path / "l" / "w", "-K")
    assert [_paged(store / ist.PREFIX / out["compact"]["gen"] / "served")] == _want(1, level=True)
    # `backfill-level` adds the level sort to a base and its runs, and touches nothing else of theirs.
    store = tmp_path / "b" / "store"
    shutil.copytree(plain["store"], store)
    before = _parquets(store)
    _cli(ilv.backfill_cmd, ["-g", "g", "-o", str(store), "-w", str(tmp_path / "b" / "w"), "-r", str(RG), "-p", "2", "-M", "1GB"])
    gen = store / ist.PREFIX / "g"
    assert [_paged(d) for d in _dirs(gen, [r.name for r in _newest_runs(gen)])] == _want(2, level=True)
    assert {k: v for k, v in _parquets(store).items() if f"/{ilv.LEVEL}." not in k} == before


def test_cut_s_writes_a_sort_by_the_rule(plain, tmp_path):
    """The staged path's `cut -s <sort>`, as `cut_gen` cuts it: the file the generation holds, byte for byte."""
    out = tmp_path / "cut"
    store = plain["store"]
    res = CliRunner().invoke(ist.cli, ["cut", "-b", str(store), "-g", "g", "-m", str(store), "-o", str(out), "-T", str(tmp_path / "t"), "-p", "2", "-M", "1GB", "-U",
                                       "-r", str(RG), "-s", "path"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert _paged(out / "served") == {"path": False}
    assert _parquets(out / "served") == {k: v for k, v in _parquets(plain["gen"] / "served").items() if k.startswith("path.")}


def test_a_store_holds_files_of_both_kinds_and_reads_as_its_scans(world, mixed):  # noqa: F811
    """Base and two runs with a page index, a run without, no carry yet: what the newest manifest lists. The Python reader
    (`interval-store verify`'s) answers every view at every scan as the per-scan reference does."""
    from dt_cloud import interval_read as ir

    gen = mixed["three"]
    runs = [r.name for r in _newest_runs(gen)]
    assert runs == DATES[BASE:BASE + 3]
    assert [_paged(d) for d in _dirs(gen, runs)] == [*_want(3, paged=True), _want(2)[1]]
    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    through = {**world, "scans": world["scans"][:BASE + 3]}
    assert _views_equal(through, lambda s: ir.Store(gen / "served", [gen / "deltas" / r / "served" for r in runs]), paths) > 0


def test_appends_onto_paged_files_check_and_carry_as_onto_unpaged_ones(mixed, plain):
    """Every appended scan's parity marker (`scan retire`'s gate: rows, not bytes) is the unpaged store's, and so is every
    file but the served sorts 2b.1's writer left: the deltas, the runs cut since, and the carry over runs of both kinds,
    which reads their deltas and is cut now."""
    assert mixed["markers"] == plain["markers"]
    assert [(d, m["pass"]) for d, m in mixed["markers"].items()] == [(d, True) for d in DATES[BASE:]]
    got, want = _parquets(mixed["gen"]), _parquets(plain["gen"])
    assert sorted(got) == sorted(want)
    old = [f"{d}served/{s}{x}" for d, w in zip(("", *(f"deltas/{r}/" for r in DATES[BASE:BASE + 2])), _want(3)) for s in w for x in (".groups.parquet", ".parquet")]
    assert sorted(k for k in got if got[k] != want[k]) == sorted(old)
    merged = f"deltas/{DATES[BASE]}_{DATES[-1]}/served"
    assert _paged(mixed["gen"] / merged) == _want(2)[1]


def test_a_compaction_over_both_kinds_is_a_fresh_build(world, mixed, tmp_path):  # noqa: F811
    """Its range files are folded from the base's and the runs' deltas, its sorts cut from those: a build over every
    scan, served sorts and sidecars byte for byte, none with a page index."""
    store = tmp_path / "store"
    shutil.copytree(mixed["store"], store)
    out = _add(world, store, tmp_path / "w", "-K")
    full = tmp_path / "full"
    _build(world, full, tmp_path / "wf", through=DATES[-1])
    got = store / ist.PREFIX / out["compact"]["gen"] / "served"
    assert [_paged(got)] == _want(1)
    assert _parquets(got) == _parquets(full / ist.PREFIX / "g" / "served")


def test_an_unpaged_sort_is_the_writer_before_pages(tmp_path):
    """The bytes of pyarrow's writer as `write_served` called it before 2b.1 (`data_page_size`, `write_batch_size` left alone),
    long paths taking a chunk past its 1 MiB default into two pages, and no page index in the sidecar's tuples."""
    n = 8192
    t = pa.table({"depth": pa.array([3] * n, pa.uint8()), "path": [f"bucket/{'x' * 200}/{i:08d}" for i in range(n)],
                  "kind": ["file"] * n, "us": pa.array([None] * n, pa.string()), "vf": pa.array(range(n), pa.int64()), "vt": pa.array(range(n), pa.int64())})
    got, ref = tmp_path / "got.parquet", tmp_path / "ref.parquet"
    with ist._served_writer(got, t.schema, None) as w:
        w.write_table(t, row_group_size=n)
    with pq.ParquetWriter(ref, t.schema, compression="zstd", use_dictionary=["kind", "us"], write_statistics=["depth", "vf", "vt"]) as w:
        w.write_table(t, row_group_size=n)
    assert got.read_bytes() == ref.read_bytes()
    assert _stats(got) == UNPAGED_STATS
    assert [bool(ix.offset_index_length) for ix in chunk_indexes(got)[0]] == [False] * 6
    # Where those pages fall: the same writer with its index on (it doesn't move a page), a batch of 1,024 values at a time.
    indexed = tmp_path / "indexed.parquet"
    with pq.ParquetWriter(indexed, t.schema, compression="zstd", use_dictionary=["kind", "us"], write_statistics=["depth", "vf", "vt"], write_page_index=True) as w:
        w.write_table(t, row_group_size=n)
    assert [p.first_row_index for p in page_locations(indexed, chunk_indexes(indexed)[0][1])] == [0, 5120]


def test_the_plan_charges_each_sorts_pages(leveled, monkeypatch, tmp_path):
    """`cut_gen`'s plan: what the costliest `at_once` sorts' writers keep, each by its own columns and pages (none of the
    unpaged sorts'), as the `meta` a group `Budget.cut` multiplies by `at_once` sorts of the most groups."""
    got = []
    real = tm.Budget.cut

    def cut(self, at_once, cores, rg_rows, rb, spool_rows, rows=None, *, meta=0, sort_rows=None):
        got.append({"at_once": at_once, "meta": meta, "sort_rows": sort_rows})
        return real(self, at_once, cores, rg_rows, rb, spool_rows, rows, meta=meta, sort_rows=sort_rows)

    monkeypatch.setattr(tm.Budget, "cut", cut)
    gen = leveled["gen"]
    scans = json.loads((gen / "scans.json").read_text())
    tree = tm.Budget.measure(sn.mem_bytes("1GB"))
    con = duckdb.connect()
    ist.cut_gen(con, gen, scans, rg_rows=RG, jobs=2, tree=tree, served=tmp_path / "served")
    has_gen, churn = ist.has_generation(scans), ist.has_churn(scans)
    todo = ist.served_sorts(scans["slices"], ist.attributed(scans), True)
    from dt_cloud.interval_compact import input_rows

    def files(s: str) -> list[str]:
        return [str(p) for p in (gen / ist.SORTS[s][0]).glob("r*.parquet")]

    pvl = sorted(files("path"))
    extra = ilv.extra_rows(duckdb.connect(), pvl)
    rows = {s: input_rows(files(s)) + (extra if s == ilv.LEVEL else 0) for s in todo}
    charge = {}
    for s in todo:
        cols = len(ist.served_schema(s, has_gen, churn))
        groups = -(-rows[s] // RG)
        # The level sort's pages charge a page's entries; the rest nothing a page.
        charge[s] = groups * (ist.writer_meta(files(s), RG, 256, cols=cols) if s == ilv.LEVEL else tm.group_meta(cols, 0))
    most = max(rows.values())
    want = -(-sum(sorted(charge.values(), reverse=True)[:2]) // (2 * -(-most // RG)))
    assert got == [{"at_once": 2, "meta": want, "sort_rows": most}]
