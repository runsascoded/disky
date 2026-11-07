"""`dt_cloud.static_append`: a base generation plus per-scan runs, read through the reader's merge (smallest
`vt` per row), equals the generation rebuilt over every scan — the open versions and deltas, the suffix rows,
every literal's first hits (so every drill path) and answers on every date, and the catalog byte for byte;
merged runs (the binary counter) likewise."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn

from test_static_catalog import _gen
from test_static_names import _brute_answer, _oracle, _read, fixture, scan_ids  # noqa: F401

V = 3
K = 2  # scans appended after the base
TERMS = ["gof", "5418", "nk080", "48.parquet", "48.parquet.crc", "pio", "a'b", "é54", "zzz", "par", "txt", "ab", "sh", "b1", "e54", "e"]


def _sx(dirs: list[Path]) -> list[tuple]:
    return sorted(r for d in dirs for f in sorted((d / "sx").glob("*.parquet")) for r in _read(f))


def _combine(rows: list[tuple]) -> list[tuple]:
    """Suffix-row tuples `(s, depth, path, usr, vf, vt, size, n_files)`, one per `(s, path, usr, vf)`, smallest `vt`."""
    best: dict[tuple, tuple] = {}
    for r in rows:
        k = (r[0], r[2], r[3], r[4])
        if k not in best or r[5] < best[k][5]:
            best[k] = r
    return sorted(best.values())


def _reader(root: Path, side) -> sn.Reader:
    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(root / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sn.Reader(fetch, lambda f: (root / f).stat().st_size, side)


def _catalog(d: Path) -> sc.Catalog:
    path = d / "cells.parquet"

    def fetch(lo: int, hi: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sc.Catalog(fetch, path.stat().st_size, pq.read_table(d / "index.parquet"))


@pytest.fixture(scope="module")
def runs(fixture, tmp_path_factory):  # noqa: F811
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("append")
    n = len(scans["scans"])
    full = _gen(root, scans, tmp / "full", V)
    base = _gen(root, {"bucket": "b", "scans": scans["scans"][:n - K]}, tmp / "base", V)
    con = base["con"]
    shards = sc.BaseShards(str(base["out"]), base["side"])
    ranges = base["ranges"]["ranges"]
    prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
    run_dirs, deltas, docs = [], [], []
    rg, sa.SX_RG = sa.SX_RG, 4  # several row groups per run shard
    try:
        for j in range(n - K, n):
            scan = scans["scans"][j]
            run = tmp / "runs" / scan["id"]
            for r in ranges:
                name = f"r{r['i']:04d}"
                docs.append(sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root)))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            files = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
            deltas.append(files)
            sa.delta_shards(con, files, run, target_rows=7)
            tiers = [base["final"], *(d / "catalog" for d in run_dirs)]
            sa.catalog_delta(con, tiers, shards, deltas, V, run / "catalog", tmp / "work" / scan["id"])
            run_dirs.append(run)
        merged_dir = tmp / "runs" / "merged"
        sa.merge_shards(run_dirs, merged_dir, target_rows=9)
        sa.merge_catalogs([d / "catalog" for d in run_dirs], merged_dir / "catalog", {"max_rows": V})
    finally:
        sa.SX_RG = rg
    return {"root": root, "scans": scans, "merged": merged, "full": full, "base": base, "runs": run_dirs, "merged_run": merged_dir,
            "docs": docs, "con": con, "tmp": tmp}


def test_append_open_equals_rebuild(runs):
    """Appending on the open coalesced versions alone: the open versions after each scan, and its delta (opened at D,
    closed at D), are exactly the rebuild's."""
    full = runs["full"]["build"]
    want = sorted(r for f in sorted((full / "cintervals").glob("*.parquet")) for r in _read(f))
    last = runs["runs"][-1]
    assert sorted(r for f in sorted((last / "copen").glob("*.parquet")) for r in _read(f)) == [r for r in want if r[4] == sn.OPEN]
    for run, scan in zip(runs["runs"], runs["scans"]["scans"][-K:]):
        D = scan["ts"]
        delta = [r for f in sorted((run / "cdelta").glob("*.parquet")) for r in _read(f)]
        # opened at D, open as of D (the rebuild may close it on a later scan)
        opened = sorted(r[:-1] for r in delta if r[-1] == 1)
        assert [(*r[:4], *r[5:]) for r in opened] == [(*r[:4], *r[5:]) for r in want if r[3] == D]
        assert all(r[4] == sn.OPEN for r in opened)
        # a close record is the version with its final `vt`: the rebuild's version closed at D, which may close later
        closed = sorted(r[:-1] for r in delta if r[-1] == -1)
        assert [r[:4] for r in closed] == [r[:4] for r in want if r[4] == D]
        assert all(r[4] == D for r in closed)
    assert sum(d["opened"] for d in runs["docs"]) == sum(1 for r in want if r[3] in {s["ts"] for s in runs["scans"]["scans"][-K:]})


@pytest.mark.parametrize("merged", [False, True])
def test_suffix_rows_equal_rebuild(runs, merged):
    """Base ⊕ runs under the combine rule holds exactly the rebuild's suffix rows."""
    tiers = [runs["merged_run"]] if merged else runs["runs"]
    got = _combine(_sx([runs["base"]["out"], *tiers]))
    assert got == _sx([runs["full"]["out"]])
    if merged:
        assert _sx([runs["merged_run"]]) == _combine(_sx(runs["runs"]))


@pytest.mark.parametrize("merged", [False, True])
def test_reader_equals_rebuild(runs, merged):
    """For every literal: the tiered reader's first hits (path, owner, liveness, values) equal the rebuild's — so the
    hits under any drill path on any date are equal — and its per-bucket answers equal brute force on every date."""
    tiers = [runs["merged_run"]] if merged else runs["runs"]
    base = runs["base"]
    tiered = sa.TieredReader([_reader(base["out"], base["side"]), *(_reader(d, pq.read_table(d / "sidecar.parquet")) for d in tiers)])
    rebuilt = sa.TieredReader([_reader(runs["full"]["out"], runs["full"]["side"])])
    oracle = _oracle(runs["merged"])
    nonempty = 0
    for term in TERMS:
        if len(term) < 3:
            continue
        hits = tiered.hits(term)
        assert hits == rebuilt.hits(term), term
        nonempty += bool(hits)
        assert tiered.answer(term, scan_ids(runs["scans"]))["answers"] == {d: _brute_answer(oracle, term, d) for d in scan_ids(runs["scans"])}, term
    assert nonempty >= 8


def test_catalog_equals_rebuild(runs):
    """The base catalog merged with the runs' catalogs is the rebuilt catalog byte for byte; so is the base merged with
    the merged run's; and the tiered lookup answers every literal as the rebuilt catalog does."""
    con, tmp = runs["con"], runs["tmp"]
    full = runs["full"]["final"]
    for name, tiers in (("each", [d / "catalog" for d in runs["runs"]]), ("merged", [runs["merged_run"] / "catalog"])):
        out = tmp / "check" / name
        sa.merge_catalogs([runs["base"]["final"], *tiers], out)
        assert (out / "cells.parquet").read_bytes() == (full / "cells.parquet").read_bytes(), name
        assert (out / "index.parquet").read_bytes() == (full / "index.parquet").read_bytes(), name
    tiered = sa.TieredCatalog([_catalog(runs["base"]["final"]), *(_catalog(d / "catalog") for d in runs["runs"])])
    rebuilt = _catalog(full)
    members = sorted({r["q"] for r in pq.read_table(full / "cells.parquet").to_pylist() if r["bucket"] == ""})
    assert len(members) > 20
    for t in [*members, *TERMS]:
        assert tiered.answer(t, scan_ids(runs["scans"])) == rebuilt.answer(t, scan_ids(runs["scans"])), t


def test_runs_hold_only_new_cells(runs):
    """A run's catalog is new cells (dated at its scan, or a new member's whole history) and new or changed headers."""
    base = {(r["q"], r["bucket"], r["vf"]): r for r in pq.read_table(runs["base"]["final"] / "cells.parquet").to_pylist()}
    for run, scan in zip(runs["runs"], runs["scans"]["scans"][-K:]):
        rows = pq.read_table(run / "catalog" / "cells.parquet").to_pylist()
        cells = [r for r in rows if r["bucket"] != ""]
        heads = {r["q"] for r in rows if r["bucket"] == ""}
        assert {r["q"] for r in cells} <= heads
        for r in cells:
            assert (r["q"], r["bucket"], r["vf"]) not in base
            if r["vf"] != scan["ts"]:
                assert not any(k[0] == r["q"] for k in base), r  # an older cell only for a literal new to the catalog


def test_push_run_is_a_binary_counter():
    runs: list[dict] = []
    levels, merges = [], []
    days = [f"2026-10-{d:02d}" for d in range(9, 16)]
    for d in days:
        runs, m = sa.push_run(runs, {"key": sa.run_key(d, d), "first": d, "last": d, "scans": [d]})
        levels.append([r["level"] for r in runs])
        merges.append([(["/".join(x["key"] for x in ins)], out["key"]) for ins, out in m])
    assert levels == [[0], [1], [1, 0], [2], [2, 0], [2, 1], [2, 1, 0]]
    assert merges[3] == [(["deltas/2026-10-11/deltas/2026-10-12"], "deltas/2026-10-11_2026-10-12"),
                         (["deltas/2026-10-09_2026-10-10/deltas/2026-10-11_2026-10-12"], "deltas/2026-10-09_2026-10-12")]
    assert [r["scans"] for r in runs] == [days[:4], days[4:6], days[6:]]
    m = sa.manifest("g", ["2026-10-08"], runs)
    assert m["scans"] == ["2026-10-08", *days] and m["date"] == "2026-10-15" and m["base_scans"] == 1


def _push_all(runs: list[dict], ids: list[str], drilled: set[str]) -> tuple[list[list[tuple[str, int]]], list[list[str]], set[str]]:
    """Push each scan id's run (drilled iff its key is in `drilled`), as `publish` does; the levels and merges after each,
    and the drilled keys (merged runs whose inputs were drilled, added)."""
    drilled = set(drilled)
    levels, merges = [], []
    for d in ids:
        runs, m = sa.push_run(runs, {"key": sa.run_key(d, d), "first": d, "last": d, "scans": [d]}, drilled)
        for ins, out in m:  # in order: a merge's input may be the one before's output
            if all(r["key"] in drilled for r in ins):
                drilled.add(out["key"])
        levels.append([(r["key"], r["level"]) for r in runs])
        merges.append([out["key"] for _, out in m])
    return levels, merges, drilled


#: gcs's runs after 2026-10-09T1236 (its manifest lists the two level-0 runs apart).
LIVE = [{"key": "deltas/2026-10-09", "first": "2026-10-09", "last": "2026-10-09", "level": 0, "scans": ["2026-10-09"]},
        {"key": "deltas/2026-10-09T1236", "first": "2026-10-09T1236", "last": "2026-10-09T1236", "level": 0, "scans": ["2026-10-09T1236"]}]


def test_push_run_merges_drilled_runs_with_each_other():
    """Every run carries `drill/` (the drill stage runs before publish): the counter carries as ever, and each merged run is
    drilled (`merge_drills` builds its drill), so the drill covers every scan."""
    levels, merges, drilled = _push_all(LIVE, ["2026-10-10", "2026-10-10T1200", "2026-10-11"],
                                        {"deltas/2026-10-09", "deltas/2026-10-09T1236", "deltas/2026-10-10", "deltas/2026-10-10T1200", "deltas/2026-10-11"})
    assert levels == [
        [("deltas/2026-10-09", 0), ("deltas/2026-10-09T1236_2026-10-10", 1)],
        [("deltas/2026-10-09", 0), ("deltas/2026-10-09T1236_2026-10-10", 1), ("deltas/2026-10-10T1200", 0)],
        [("deltas/2026-10-09", 0), ("deltas/2026-10-09T1236_2026-10-11", 2)],
    ]
    assert merges == [["deltas/2026-10-09T1236_2026-10-10"], [], ["deltas/2026-10-10T1200_2026-10-11", "deltas/2026-10-09T1236_2026-10-11"]]
    runs = [{"key": k, "scans": [k.removeprefix("deltas/")]} for k, _ in levels[-1]]
    runs[-1]["scans"] = ["2026-10-09T1236", "2026-10-10", "2026-10-10T1200", "2026-10-11"]
    assert sa.drill_scans(runs, drilled) == ["2026-10-09", "2026-10-09T1236", "2026-10-10", "2026-10-10T1200", "2026-10-11"]


def test_push_run_never_merges_a_drilled_run_with_one_without():
    """gcs 2026-10-09T1236: `deltas/2026-10-09` carried `drill/` and T1236 didn't; the counter merged them into a run without
    one, and the reader (which stops at the first run without one) lost 10-09's drill. Two runs that differ on it are never
    merged: the carry stops there, and the drill still covers every scan up to the first run without one."""
    levels, merges, drilled = _push_all(LIVE[:1], ["2026-10-09T1236", "2026-10-10"], {"deltas/2026-10-09", "deltas/2026-10-10"})
    assert levels == [
        [("deltas/2026-10-09", 0), ("deltas/2026-10-09T1236", 0)],
        [("deltas/2026-10-09", 0), ("deltas/2026-10-09T1236", 0), ("deltas/2026-10-10", 0)],
    ]
    assert merges == [[], []]
    assert sa.drill_scans(LIVE, drilled) == ["2026-10-09"]


def test_push_run_merges_runs_without_drill_as_ever():
    """A deployment without the drill stage: no run carries `drill/`, the counter carries as ever, the drill covers nothing past the base."""
    levels, merges, drilled = _push_all([], ["2026-10-09", "2026-10-09T1236", "2026-10-10"], set())
    assert levels == [[("deltas/2026-10-09", 0)], [("deltas/2026-10-09_2026-10-09T1236", 1)],
                      [("deltas/2026-10-09_2026-10-09T1236", 1), ("deltas/2026-10-10", 0)]]
    assert merges == [[], ["deltas/2026-10-09_2026-10-09T1236"], []]
    assert drilled == set()


def test_a_merged_run_is_whole(runs, tmp_path):
    """A merged run holds every file the readers need of a tier (its catalog's `meta.json` included: gcs's first merge,
    2026-10-09T1236, lacked it), and a manifest listing a run that lacks one is refused before it is written."""
    merged = runs["merged_run"]
    (merged / "meta.json").write_text("{}")
    have = {str(p.relative_to(merged.parent)) for p in merged.rglob("*") if p.is_file()}
    run = {"key": merged.name}
    assert sa.missing_files([run], lambda k: k in have) == []
    assert json.loads((merged / "catalog" / "meta.json").read_text())["membership"] == {"max_rows": V}
    assert sa.missing_files([run, {"key": "deltas/x"}], lambda k: k in have - {f"{merged.name}/catalog/meta.json"}) == [
        f"{merged.name}/catalog/meta.json", *(f"deltas/x/{f}" for f in sa.RUN_FILES)]


def test_a_listed_runs_drill_is_whole():
    """A listed run carrying `drill/` must hold every file its `meta.json` implies, else the manifest is refused."""
    meta = {"long_roots": {"files": 2}, "long_rollups": {"files": 1}, "short_roots": {"files": 1}, "short_rollups": {"files": 0}}
    run = {"key": "deltas/2026-10-09T1236"}
    files = [f"deltas/2026-10-09T1236/{f}" for f in sa.RUN_FILES]
    drill = [f"deltas/2026-10-09T1236/drill/{f}" for f in (
        "meta.json", "aliases.parquet",
        "long/roots/r0000.parquet", "long/roots/r0001.parquet", "long-roots-index.parquet", "long-roots-index.top.parquet",
        "long/rollups/r0000.parquet", "long-rollups-index.parquet", "long-rollups-index.top.parquet", "state/dcount-long.parquet",
        "short/roots/r0000.parquet", "short-roots-index.parquet", "short-roots-index.top.parquet",
        "short-rollups-index.parquet", "short-rollups-index.top.parquet", "state/dcount-short.parquet")]
    have = {*files, *drill}
    assert sa.missing_files([run], have.__contains__, lambda k: meta) == []
    assert sa.missing_files([run], (have - {drill[3], drill[-1]}).__contains__, lambda k: meta) == [drill[3], drill[-1]]
    assert sa.missing_files([run], set(files).__contains__) == []


def test_a_literal_crosses_v_on_an_append(runs):
    """The fixture exercises the hard case: a literal that became a member at a run's scan, its whole history in that
    run's catalog (cells dated before the scan)."""
    olds = 0
    for run, scan in zip(runs["runs"], runs["scans"]["scans"][-K:]):
        olds += sum(1 for r in pq.read_table(run / "catalog" / "cells.parquet").to_pylist() if r["bucket"] != "" and r["vf"] < scan["ts"])
    assert olds > 0


def test_compaction_equals_full_build(runs, tmp_path):
    """Base ⊕ runs compacted (the k-way merge cut to the full build's shard plan) is the full build byte for byte:
    every shard file, every sidecar; likewise through the merged run."""
    import json

    full = runs["full"]["out"]
    plan = json.loads((full / "shards.json").read_text()) if (full / "shards.json").exists() else None
    if plan is None:
        from dt_cloud import static_names as sn_

        plan = sn_.plan_shards(sorted((runs["full"]["build"] / "chist").glob("*.parquet")), target_rows=60, tasks=2)
    rg, sa.SX_RG = sa.SX_RG, 5  # the fixture's build row groups
    try:
        for name, tiers in (("each", runs["runs"]), ("merged", [runs["merged_run"]])):
            out = tmp_path / name
            sa.merge_shards([runs["base"]["out"], *tiers], out, plan=plan)
            want = sorted(p.name for p in (full / "sx").glob("*.parquet"))
            assert sorted(p.name for p in (out / "sx").glob("*.parquet")) == want
            for f in want:
                assert (out / "sx" / f).read_bytes() == (full / "sx" / f).read_bytes(), (name, f)
                assert (out / "sidecar" / f).read_bytes() == (full / "sidecar" / f).read_bytes(), (name, f)
    finally:
        sa.SX_RG = rg


def test_merge_cdeltas(runs, tmp_path):
    """Two days' version deltas merged: one row per version, the smallest `vt`, `op` 1 if it opened in either day."""
    files = [str(f) for d in runs["runs"] for f in sorted((d / "cdelta").glob("*.parquet"))]
    rows = [r for f in files for r in _read(Path(f))]
    want: dict[tuple, tuple] = {}
    for r in rows:
        k = r[:4]
        cur = want.get(k)
        want[k] = r if cur is None else (*r[:4], min(cur[4], r[4]), *r[5:7], max(cur[7], r[7]))
    n = sa.merge_cdeltas(files, tmp_path / "m.parquet")
    got = _read(tmp_path / "m.parquet")
    assert got == sorted(want.values(), key=lambda r: (r[0], r[1], r[2], r[3], r[7]))
    assert n == len(want) < len(rows)


@pytest.mark.parametrize("day", [0, 1])
def test_verify_terms_against_the_scan(runs, day):
    """`verify`'s checks (the stage run on the real run) all hold on the fixture: brute force from a run's scan file vs
    the base and the runs through it, tiered, per literal and under its drill roots; on the first run, also the day
    before vs the base alone."""
    root, scans = runs["root"], runs["scans"]["scans"]
    base = runs["base"]
    n = len(scans) - K + day
    scan, before, base_last = scans[n], scans[n - 1]["id"], scans[len(scans) - K - 1]["id"]
    run_dirs = runs["runs"][:day + 1]
    con = sn.connect(2, "1GB", runs["tmp"] / "vtmp")
    terms = [*TERMS, "b2", "e5418", "qz"]  # `qz`: a short literal no name holds
    report = sa.verify_terms(con, str(root / scan["src"]), scan["version"], scan["id"], before, terms,
                             sa.TieredReader([_reader(base["out"], base["side"]), *(_reader(d, pq.read_table(d / "sidecar.parquet")) for d in run_dirs)]),
                             sa.TieredCatalog([_catalog(base["final"]), *(_catalog(d / "catalog") for d in run_dirs)]),
                             _reader(base["out"], base["side"]), _catalog(base["final"]), base_last)
    con.execute("DROP TABLE sc")
    failed = {t: {k: v for k, v in d["equal"].items() if not v} for t, d in report["terms"].items() if not all(d["equal"].values())}
    assert failed == {}
    assert report["equal"] == report["checks"] >= len(terms)
    assert sorted({d["source"] for d in report["terms"].values()}) == ["absent-short", "catalog", "static"]
    assert sum(1 for d in report["terms"].values() for k in d["equal"] if k.startswith("root:")) >= 1
    assert all(("before" in d["equal"]) == (day == 0) for d in report["terms"].values())


@pytest.mark.parametrize("through", [0, 1])
def test_rebuild_open_equals_the_appended_state(runs, tmp_path, through):
    """A lost `state/<D>/copen` refolded from the base's `cintervals` and every run's `cdelta` through D (open rows,
    minus closes, plus opens) is the file the append wrote, byte for byte, per range."""
    con = runs["con"]
    run_dirs = runs["runs"][:through + 1]
    names = sorted(f.name for f in (run_dirs[-1] / "copen").glob("*.parquet"))
    assert names == [f"r{r['i']:04d}.parquet" for r in runs["base"]["ranges"]["ranges"]]
    for name in names:
        out = tmp_path / name
        n = sa.rebuild_open(con, str(runs["base"]["build"] / "cintervals" / name), [str(d / "cdelta" / name) for d in run_dirs], out)
        assert (n, out.read_bytes()) == (pq.ParquetFile(run_dirs[-1] / "copen" / name).metadata.num_rows, (run_dirs[-1] / "copen" / name).read_bytes())


def test_rebuild_open_without_runs_is_the_base_open_rows(runs, tmp_path):
    """No run yet: the base's open versions (the first append's `prev`)."""
    name = "r0000.parquet"
    base = runs["base"]["build"] / "cintervals" / name
    sa.rebuild_open(runs["con"], str(base), [], tmp_path / name)
    assert _read(tmp_path / name) == sorted(r for r in _read(base) if r[4] == sn.OPEN)


# ── prune: the newest complete state only ──────────────────────────────────

GEN = "g1"
DATA, SCR = "data-bucket", "scratch-bucket"
SP = f"{sn.PREFIX}/{GEN}/state"


class _Blob:
    def __init__(self, store: dict, bucket: str, name: str):
        self.store, self.bucket, self.name = store, bucket, name

    @property
    def size(self) -> int:
        return self.store[(self.bucket, self.name)]

    def exists(self) -> bool:
        return (self.bucket, self.name) in self.store

    def delete(self) -> None:
        from google.api_core.exceptions import NotFound

        if (self.bucket, self.name) not in self.store:
            raise NotFound(self.name)
        del self.store[(self.bucket, self.name)]


class _Bucket:
    def __init__(self, store: dict, name: str):
        self.store, self.name = store, name

    def blob(self, name: str) -> _Blob:
        return _Blob(self.store, self.name, name)


class _GCS:
    """The slice of `google.cloud.storage.Client` that `prune_state` uses, over `{(bucket, name): size}`."""

    def __init__(self, store: dict):
        self.store = store

    def list_blobs(self, bucket: str, prefix: str) -> list[_Blob]:
        return [_Blob(self.store, b, n) for b, n in sorted(self.store) if b == bucket and n.startswith(prefix)]

    def bucket(self, name: str) -> _Bucket:
        return _Bucket(self.store, name)


def _day(day: str, copen: int = 2, done: int = 2, size: int = 100) -> dict:
    """A day's state objects in the scratch bucket: the first `copen` / `done` of 2 ranges."""
    return {
        **{(SCR, f"{SP}/{day}/copen/r{i:04d}.parquet"): size for i in range(copen)},
        **{(SCR, f"{SP}/{day}/done/r{i:04d}.json"): 1 for i in range(done)},
    }


def _published(*days: str) -> dict:
    return {(DATA, f"{sn.PREFIX}/{GEN}/manifests/{d}.json"): 1 for d in days}


# Never touched: another generation's state, the scratch bucket's other prefixes, the data bucket's runs.
OTHERS = {
    (SCR, f"{sn.PREFIX}/g0/state/2026-10-01/copen/r0000.parquet"): 5,
    (SCR, f"{sn.PREFIX}/{GEN}/sxmap/r0000.parquet"): 5,
    (DATA, f"{sn.PREFIX}/{GEN}/deltas/2026-10-09/cdelta/r0000.parquet"): 5,
}


def test_prune_keeps_only_the_newest_complete_state():
    store = {**_day("2026-10-09"), **_day("2026-10-10", size=7), **_day("2026-10-11"), **_day("2026-10-12", copen=1, done=0),
             **_published("2026-10-10", "2026-10-11"), **OTHERS}
    keep = {**_day("2026-10-11"), **_day("2026-10-12", copen=1, done=0), **_published("2026-10-10", "2026-10-11"), **OTHERS}
    gcs = _GCS(store)
    assert sa.prune_state(gcs, GEN, "2026-10-11", 2, bucket=DATA, scratch=SCR) == {
        "date": "2026-10-11", "keep": ["2026-10-11", "2026-10-12"],
        "delete": [{"day": "2026-10-09", "objects": 4, "bytes": 202}, {"day": "2026-10-10", "objects": 4, "bytes": 16}],
        "deleted": 8,
    }
    assert store == keep
    # Idempotent: the rerun finds nothing before the day.
    assert sa.prune_state(gcs, GEN, "2026-10-11", 2, bucket=DATA, scratch=SCR) == {"date": "2026-10-11", "keep": ["2026-10-11", "2026-10-12"], "delete": [], "deleted": 0}
    assert store == keep


def test_prune_dry_run_deletes_nothing():
    store = {**_day("2026-10-09"), **_day("2026-10-10"), **_published("2026-10-10"), **OTHERS}
    before = dict(store)
    assert sa.prune_state(_GCS(store), GEN, "2026-10-10", 2, bucket=DATA, scratch=SCR, dry_run=True) == {
        "date": "2026-10-10", "keep": ["2026-10-10"], "delete": [{"day": "2026-10-09", "objects": 4, "bytes": 202}], "deleted": 0,
    }
    assert store == before


def test_prune_with_one_state_is_a_noop():
    store = {**_day("2026-10-09"), **_published("2026-10-09"), **OTHERS}
    before = dict(store)
    assert sa.prune_state(_GCS(store), GEN, "2026-10-09", 2, bucket=DATA, scratch=SCR) == {"date": "2026-10-09", "keep": ["2026-10-09"], "delete": [], "deleted": 0}
    assert store == before


@pytest.mark.parametrize("day, published, msg", [
    (_day("2026-10-10", copen=1, done=1), True, "state/2026-10-10 incomplete: 1 of 2 ranges without copen, 1 of 2 ranges without done"),
    (_day("2026-10-10", done=1), True, "state/2026-10-10 incomplete: 1 of 2 ranges without done"),
    (_day("2026-10-10"), False, "state/2026-10-10 incomplete: no manifest"),
    ({}, True, "state/2026-10-10 incomplete: 2 of 2 ranges without copen, 2 of 2 ranges without done"),
])
def test_prune_refuses_while_the_day_is_incomplete(day, published, msg):
    """Nothing is deleted before the day's state is whole and its run published: the earlier state may be the only
    complete one, the one a rerun of the day's append reads."""
    store = {**_day("2026-10-09"), **day, **_published("2026-10-09", *(["2026-10-10"] if published else [])), **OTHERS}
    before = dict(store)
    with pytest.raises(sa.StateIncomplete) as e:
        sa.prune_state(_GCS(store), GEN, "2026-10-10", 2, bucket=DATA, scratch=SCR)
    assert (str(e.value), store) == (msg, before)


def test_prune_plan_rejects_a_non_day_dir():
    with pytest.raises(ValueError) as e:
        sa.prune_plan([(f"{SP}/latest/copen/r0000.parquet", 1)], f"{sn.PREFIX}/{GEN}", 1, True, "2026-10-10")
    assert str(e.value) == f"{SP}/latest/copen/r0000.parquet: 'latest' is not a scan id"


def test_prune_keeps_only_the_newest_complete_state_of_sub_daily_scans():
    """A deployment scanning every 6 h (cw): states are keyed by scan id to the minute, and an earlier scan of the same
    day is an earlier state like any other."""
    store = {**_day("2026-10-09T1801"), **_day("2026-10-10T0001", size=7), **_day("2026-10-10T0601"),
             **_day("2026-10-10T1202", copen=1, done=0), **_published("2026-10-10T0001", "2026-10-10T0601"), **OTHERS}
    keep = {**_day("2026-10-10T0601"), **_day("2026-10-10T1202", copen=1, done=0), **_published("2026-10-10T0001", "2026-10-10T0601"), **OTHERS}
    assert sa.prune_state(_GCS(store), GEN, "2026-10-10T0601", 2, bucket=DATA, scratch=SCR) == {
        "date": "2026-10-10T0601", "keep": ["2026-10-10T0601", "2026-10-10T1202"],
        "delete": [{"day": "2026-10-09T1801", "objects": 4, "bytes": 202}, {"day": "2026-10-10T0001", "objects": 4, "bytes": 16}],
        "deleted": 8,
    }
    assert store == keep


def test_prune_keeps_every_run_and_manifest_after_merges():
    """Deferred carries (`static_merge`): revisions and merged runs beside the scans' manifests and level-0 runs. Prune deletes
    only earlier open-version states in the scratch bucket: every run any manifest lists (the superseded level-0 runs, whose
    `cdelta` rebuilds a state, included) and every manifest stays. A scan published only through a revision's runs still
    needs its own `manifests/<id>.json` to count as published."""
    runs = {(DATA, f"{sn.PREFIX}/{GEN}/{k}/{f}"): 3 for k in ("deltas/2026-10-10", "deltas/2026-10-11", "deltas/2026-10-10_2026-10-11")
            for f in ("meta.json", "sx/s0000.parquet", "catalog/meta.json")}
    manifests = {(DATA, f"{sn.PREFIX}/{GEN}/manifests/{n}"): 1 for n in ("2026-10-10.json", "2026-10-11.json", "2026-10-11.m001.json")}
    store = {**_day("2026-10-10"), **_day("2026-10-11"), **runs, **manifests, **OTHERS}
    assert sa.prune_state(_GCS(store), GEN, "2026-10-11", 2, bucket=DATA, scratch=SCR) == {
        "date": "2026-10-11", "keep": ["2026-10-11"], "delete": [{"day": "2026-10-10", "objects": 4, "bytes": 202}], "deleted": 4}
    assert store == {**_day("2026-10-11"), **runs, **manifests, **OTHERS}
    without = {k: v for k, v in store.items() if k[1] != f"{sn.PREFIX}/{GEN}/manifests/2026-10-11.json"}
    with pytest.raises(sa.StateIncomplete) as e:
        sa.prune_state(_GCS(without), GEN, "2026-10-11", 2, bucket=DATA, scratch=SCR)
    assert str(e.value) == "state/2026-10-11 incomplete: no manifest"
