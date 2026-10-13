"""`dt_cloud.static_merge`: deferred carries. A scan's `publish` only adds its level-0 run; the counter's carries run apart,
each merged run published as a revision `manifests/<id>.m<NNN>.json` of the newest manifest. Over real runs (the
`test_static_append` fixture's scans, a one-scan base and four runs): the stack after each publish and merge, readers over
every manifest (old and revised) against the rebuild, a merge interrupted at each step leaving the store servable and
resuming, the lease, and a publish landing mid-merge (rebased)."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_merge as sm
from dt_cloud import static_names as sn

from test_static_append import TERMS, V, _catalog, _combine, _reader, _sx
from test_static_catalog import _gen
from test_static_names import _brute_answer, _oracle, fixture  # noqa: F401

N_RUNS = 4
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def _run(d: str, level: int = 0) -> dict:
    return {"key": f"deltas/{d}", "first": d, "last": d, "level": level, "scans": [d]}


def _merged(first: str, last: str, level: int, scans: list[str]) -> dict:
    return {"key": sa.run_key(first, last), "first": first, "last": last, "level": level, "scans": scans}


# ── Pure: names, the plan, rebase ──────────────────────────────────────────


def test_manifest_names_sort_by_scan_then_revision():
    """A revision sorts after its scan's own manifest and before every later scan's (a sub-daily one of the same day
    included), so the greatest key is the newest state for every reader."""
    names = ["2026-10-10T0600.json", "2026-10-09.m002.json", "2026-10-10.m010.json", "2026-10-09.json", "2026-10-10.json",
             "2026-10-09.m001.json", "2026-10-10.m009.json", "2026-10-11.json", "scans.json", "2026-10-10.m1.json", "x.json"]
    assert sm.manifest_keys([f"manifests/{n}" for n in names] + ["deltas/2026-10-09/meta.json"]) == [f"manifests/{n}" for n in [
        "2026-10-09.json", "2026-10-09.m001.json", "2026-10-09.m002.json", "2026-10-10.json", "2026-10-10.m009.json",
        "2026-10-10.m010.json", "2026-10-10T0600.json", "2026-10-11.json"]]
    assert [sm.parse_manifest(n) for n in ("2026-10-10.json", "2026-10-10T0600.m012.json", "2026-10-10.m1.json", "x.json")] == [
        ("2026-10-10", 0), ("2026-10-10T0600", 12), None, None]
    assert [sm.manifest_name("2026-10-10"), sm.manifest_name("2026-10-10", 7)] == ["2026-10-10.json", "2026-10-10.m007.json"]
    with pytest.raises(ValueError):
        sm.manifest_name("2026-10-10", 1000)


@pytest.mark.parametrize("before, want", [
    (None, "manifests/2026-10-10.m001.json"),
    ("2026-10-10T0600", "manifests/2026-10-10.m001.json"),
    ("2026-10-10", "manifests/2026-10-09.m002.json"),
    ("2026-10-09", None),
])
def test_latest_key_takes_revisions(before, want):
    """`publish`'s base (`_latest_manifest`): the newest manifest of a scan before the one being published, revisions too."""
    keys = ["manifests/2026-10-09.json", "manifests/2026-10-09.m001.json", "manifests/2026-10-09.m002.json", "manifests/2026-10-10.json",
            "manifests/2026-10-10.m001.json", "scans.json"]
    assert sa.latest_key(keys, before) == want


def _days(n: int) -> list[str]:
    return [f"2026-10-{d:02d}" for d in range(1, n + 1)]


def test_plan_carries_ends_where_push_run_does():
    """Runs pushed one by one with every carry made at once (`push_run`, the old `publish`) and runs pushed with carries
    deferred then planned (`plan_carries`) end at the same stack, after any number of scans."""
    for n in range(1, 20):
        eager: list[dict] = []
        for d in _days(n):
            eager, _ = sa.push_run(eager, {"key": f"deltas/{d}", "first": d, "last": d, "scans": [d]})
        after, _ = sm.plan_carries([_run(d) for d in _days(n)])
        assert [(r["key"], r["level"], r["scans"]) for r in after] == [(r["key"], r["level"], r["scans"]) for r in eager], n


def test_chained_carries_are_one_merge_of_every_input():
    """[L1, L0, L0]: the L0s would carry into an L1 and it into an L2; deferred, that is one merge of the three runs."""
    runs = [_merged("2026-10-01", "2026-10-02", 1, ["2026-10-01", "2026-10-02"]), _run("2026-10-03"), _run("2026-10-04")]
    after, merges = sm.plan_carries(runs)
    want = _merged("2026-10-01", "2026-10-04", 2, _days(4))
    assert after == [want]
    assert merges == [(runs, want)]


def test_a_backlog_plans_independent_merges():
    """Merges that fell behind: [L2, L0 × 5] → [L2, L2, L0] would be [L3, L0] — the plan replays the counter, so the
    backlog's carries are one merge into an L3 of the L2 and four L0s, the fifth L0 left."""
    l2 = _merged("2026-10-01", "2026-10-04", 2, _days(4))
    runs = [l2, *(_run(d) for d in _days(9)[4:])]
    after, merges = sm.plan_carries(runs)
    l3 = _merged("2026-10-01", "2026-10-08", 3, _days(8))
    assert after == [l3, _run("2026-10-09")]
    assert merges == [([l2, *runs[1:5]], l3)]
    # two merges at once: [L1, L0, L0 | L0, L0]? No: the counter folds left to right, so a 2-run carry and a 1-run tail
    after, merges = sm.plan_carries([_run(d) for d in _days(3)])
    assert after == [_merged("2026-10-01", "2026-10-02", 1, _days(2)), _run("2026-10-03")]
    assert [(len(i), o["key"]) for i, o in merges] == [(2, "deltas/2026-10-01_2026-10-02")]


def test_gcs_live_stack_carries_to_level_2_on_the_second_scan():
    """gcs after 2026-10-10: [L0 10-09, L1 T1236..10-10]. One more scan: no carry; a second: one merge of the L1 and both
    L0s into an L2 (sequentially it was an L1 merge, then an L2 one); 10-09 stays apart (the counter never merges down)."""
    live = [_run("2026-10-09"), _merged("2026-10-09T1236", "2026-10-10", 1, ["2026-10-09T1236", "2026-10-10"])]
    assert sm.plan_carries([*live, _run("2026-10-11")])[1] == []
    after, merges = sm.plan_carries([*live, _run("2026-10-11"), _run("2026-10-12")])
    l2 = _merged("2026-10-09T1236", "2026-10-12", 2, ["2026-10-09T1236", "2026-10-10", "2026-10-11", "2026-10-12"])
    assert after == [live[0], l2]
    assert merges == [([live[1], _run("2026-10-11"), _run("2026-10-12")], l2)]


def test_carries_keep_drill_parity_and_stop_short_of_compaction():
    runs = [_run(d) for d in _days(4)]
    after, merges = sm.plan_carries(runs, drilled={"deltas/2026-10-01", "deltas/2026-10-03", "deltas/2026-10-04"})
    assert [r["key"] for r in after] == ["deltas/2026-10-01", "deltas/2026-10-02", "deltas/2026-10-03_2026-10-04"]
    assert [[r["key"] for r in i] for i, _ in merges] == [["deltas/2026-10-03", "deltas/2026-10-04"]]
    # two L4s never carry into an L5 (a compaction's job)
    l4s = [_merged(f"2026-0{m}-01", f"2026-0{m}-16", 4, [f"2026-0{m}-{d:02d}" for d in range(1, 17)]) for m in (1, 2)]
    assert sm.plan_carries(l4s) == (l4s, [])
    assert sm.plan_carries(l4s, max_level=6)[0][0]["level"] == 5


def test_rebase():
    runs = [_run(d) for d in _days(4)]
    out = _merged("2026-10-02", "2026-10-03", 1, _days(3)[1:])
    assert sm.rebase(runs, ["deltas/2026-10-02", "deltas/2026-10-03"], out) == [runs[0], out, runs[3]]
    assert sm.rebase([runs[0], out, runs[3]], ["deltas/2026-10-02", "deltas/2026-10-03"], out) is None
    with pytest.raises(sm.Superseded):
        sm.rebase([runs[0], runs[2], runs[1]], ["deltas/2026-10-02", "deltas/2026-10-03"], out)


def test_tier_resources_sum_to_three_quarters_of_memory():
    """At once, the DuckDB tiers split 75% of the machine less 4 GiB per streaming tier, and its CPUs; one at a time, each
    gets all of it. n2-highmem-16: 128 GiB, 16 vCPUs."""
    ram = 128 << 30
    assert sm.tier_resources(["shards", "catalog", "drill", "anchors"], ram, 16, concurrent=True) == {
        "drill": {"mem": "44GB", "threads": 8}, "anchors": {"mem": "44GB", "threads": 8}}
    assert sm.tier_resources(["shards", "catalog", "drill"], ram, 16, concurrent=True) == {"drill": {"mem": "88GB", "threads": 16}}
    assert sm.tier_resources(["shards", "catalog", "drill", "anchors"], ram, 16, concurrent=False) == {
        "drill": {"mem": "96GB", "threads": 16}, "anchors": {"mem": "96GB", "threads": 16}}
    assert sm.tier_resources(["shards", "catalog"], ram, 16, concurrent=True) == {}


def test_run_tiers(tmp_path):
    dirs = [tmp_path / "a", tmp_path / "b"]
    for d in dirs:
        (d / "anchors").mkdir(parents=True)
    (dirs[0] / "anchors" / "meta.json").write_text("{}")
    assert sm.run_tiers(dirs, drilled=True) == ["shards", "catalog", "drill"]
    (dirs[1] / "anchors" / "meta.json").write_text("{}")
    assert sm.run_tiers(dirs, drilled=False) == ["shards", "catalog", "anchors"]


# ── Real runs ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def chain(fixture, tmp_path_factory):  # noqa: F811
    """A one-scan base generation and a run per later scan (unpublished) laid out as the bucket holds them, plus the full
    rebuild and the brute-force oracle."""
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("merge")
    n = len(scans["scans"])
    full = _gen(root, scans, tmp / "full", V)
    base = _gen(root, {"bucket": "b", "scans": scans["scans"][:n - N_RUNS]}, tmp / "base", V)
    gen = tmp / "gen"
    gen.mkdir()
    (gen / "scans.json").write_text(json.dumps({"bucket": "b", "scans": [{"id": s["id"]} for s in scans["scans"][:n - N_RUNS]]}) + "\n")
    con = base["con"]
    shards = sc.BaseShards(str(base["out"]), base["side"])
    ranges = base["ranges"]["ranges"]
    prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
    run_dirs, deltas = [], []
    rg, sa.SX_RG = sa.SX_RG, 4
    try:
        for scan in scans["scans"][n - N_RUNS:]:
            run = gen / sa.run_key(scan["id"], scan["id"])
            for r in ranges:
                name = f"r{r['i']:04d}"
                sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            deltas.append(sorted(str(f) for f in (run / "cdelta").glob("*.parquet")))
            sa.delta_shards(con, deltas[-1], run, target_rows=7)
            sa.catalog_delta(con, [base["final"], *(d / "catalog" for d in run_dirs)], shards, deltas, V, run / "catalog", tmp / "work" / scan["id"])
            run_dirs.append(run)
    finally:
        sa.SX_RG = rg
    for d in run_dirs:  # the scratch bucket's state, and the shard plan's input: not the run's served files
        shutil.rmtree(d / "dhist")
        shutil.rmtree(d / "copen")
    return {"gen": gen, "base": base, "full": full, "ids": [s["id"] for s in scans["scans"]], "runs": [s["id"] for s in scans["scans"][n - N_RUNS:]],
            "oracle": _oracle(merged), "tmp": tmp}


def _store(chain, tmp_path) -> sm.LocalRunStore:
    """A fresh copy of the chain's bucket (merges write into it)."""
    shutil.copytree(chain["gen"], tmp_path / "gen")
    return sm.LocalRunStore(tmp_path / "gen", tmp_path / "scratch", gen="g")


def _publish(store, d: str) -> dict:
    return sa.publish_run(store, d)


def _merge(store, tmp_path, **kw) -> dict:
    return sm.merge_pending(store, store.root, tmp=tmp_path / "work", owner="test", now=lambda: NOW, log=lambda m: None, **kw)


def _stack(store, key: str) -> list[tuple[str, int]]:
    return [(r["key"], r["level"]) for r in store.read_json(key)["runs"]]


def _summary(doc: dict) -> list[dict]:
    return [{k: v for k, v in m.items() if k not in ("tiers_s", "s")} for m in doc["merged"]]


def _rows(store, keys: list[str]) -> int:
    return len(_combine(_sx([store.root / k for k in keys])))


def _check_readers(chain, store, key: str) -> None:
    """The base plus manifest `key`'s runs, read as the readers do: every literal's per-bucket answers equal brute force on
    every scan it covers, its catalog's equal the rebuilt catalog's, and once it covers every scan its first hits equal
    the rebuild's."""
    m = store.read_json(key)
    dates = m["scans"]
    base = chain["base"]
    dirs = [store.root / r["key"] for r in m["runs"]]
    reader = sa.TieredReader([_reader(base["out"], base["side"]), *(_reader(d, pq.read_table(d / "sidecar.parquet")) for d in dirs)])
    catalog = sa.TieredCatalog([_catalog(base["final"]), *(_catalog(d / "catalog") for d in dirs)])
    rebuilt_reader = sa.TieredReader([_reader(chain["full"]["out"], chain["full"]["side"])])
    rebuilt_catalog = _catalog(chain["full"]["final"])
    for term in TERMS:
        got = catalog.answer(term, dates)
        if dates == chain["ids"]:
            assert got == rebuilt_catalog.answer(term, dates), (key, term)
        elif got is not None:  # a member then is one in the rebuild (membership only grows), with the same answers then
            assert got["answers"] == rebuilt_catalog.answer(term, dates)["answers"], (key, term)
        if len(term) < 3:
            continue
        assert reader.answer(term, dates)["answers"] == {d: _brute_answer(chain["oracle"], term, d) for d in dates}, (key, term)
        if dates == chain["ids"]:
            assert reader.hits(term) == rebuilt_reader.hits(term), (key, term)


def test_publish_adds_a_level_0_run_and_merges_run_apart(chain, tmp_path):
    """Four scans published with the carries deferred, merged after the third and the fourth: each publish only adds its
    run; the first merge folds the two oldest into an L1 (revision of the third's manifest), the second the L1 and the two
    newest L0s into one L2 (revision of the fourth's). Every manifest — scans' and revisions — reads exactly."""
    a, b, c, d = chain["runs"]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        doc = _publish(store, s)
        assert (doc["date"], doc["scans"]) == (s, [*chain["ids"][:1], *chain["runs"][:chain["runs"].index(s) + 1]])
    assert [_stack(store, f"manifests/{s}.json") for s in (a, b, c)] == [
        [(f"deltas/{a}", 0)], [(f"deltas/{a}", 0), (f"deltas/{b}", 0)], [(f"deltas/{a}", 0), (f"deltas/{b}", 0), (f"deltas/{c}", 0)]]
    assert sa.publish_run(store, d, dry_run=True)["carries_due"] == [[[f"deltas/{a}", f"deltas/{b}", f"deltas/{c}", f"deltas/{d}"], sa.run_key(a, d)]]

    ab = sa.run_key(a, b)
    assert _merge(store, tmp_path, dry_run=True) == {"manifest": f"manifests/{c}.json", "plan": [{"inputs": [f"deltas/{a}", f"deltas/{b}"], "output": ab, "level": 1}]}
    got = _merge(store, tmp_path)
    assert _summary(got) == [{"inputs": [f"deltas/{a}", f"deltas/{b}"], "output": ab, "level": 1, "scans": 2,
                              "rows": _rows(store, [f"deltas/{a}", f"deltas/{b}"]), "manifest": f"manifests/{c}.m001.json"}]
    assert got["manifest"] == f"manifests/{c}.m001.json"
    rev = store.read_json(f"manifests/{c}.m001.json")
    assert (rev["rev"], rev["revises"], rev["scans"], rev["date"]) == (1, f"manifests/{c}.json", store.read_json(f"manifests/{c}.json")["scans"], c)
    assert _stack(store, f"manifests/{c}.m001.json") == [(ab, 1), (f"deltas/{c}", 0)]
    assert _merge(store, tmp_path)["merged"] == []  # nothing due

    _publish(store, d)
    assert _stack(store, f"manifests/{d}.json") == [(ab, 1), (f"deltas/{c}", 0), (f"deltas/{d}", 0)]
    got = _merge(store, tmp_path)
    ad = sa.run_key(a, d)
    assert _summary(got) == [{"inputs": [ab, f"deltas/{c}", f"deltas/{d}"], "output": ad, "level": 2, "scans": 4,
                              "rows": _rows(store, [f"deltas/{s}" for s in chain["runs"]]), "manifest": f"manifests/{d}.m001.json"}]
    assert _stack(store, f"manifests/{d}.m001.json") == [(ad, 2)]
    # the merged run is whole and holds exactly the combined rows of its scans' runs
    assert sa.missing_files(store.read_json(f"manifests/{d}.m001.json")["runs"], store.exists) == []
    assert _sx([store.root / ad]) == _combine(_sx([store.root / f"deltas/{s}" for s in chain["runs"]]))
    assert json.loads((store.root / ad / "meta.json").read_text())["scans"] == chain["runs"]
    # every manifest ever published stays servable: nothing was deleted
    keys = sm.manifest_keys(store.keys("manifests/"))
    assert keys == [f"manifests/{s}" for s in (f"{a}.json", f"{b}.json", f"{c}.json", f"{c}.m001.json", f"{d}.json", f"{d}.m001.json")]
    for k in keys:
        assert sa.missing_files(store.read_json(k)["runs"], store.exists) == [], k
        _check_readers(chain, store, k)
    assert sa.latest_key(store.keys("manifests/")) == f"manifests/{d}.m001.json"
    assert not (tmp_path / "scratch" / "merge.lease.json").exists()


def test_a_stack_with_an_l0_below_an_l1_reads_exactly(chain, tmp_path):
    """gcs's `2026-10-08c` after 10-10: [L0 10-09, L1 T1236..10-10], then T1514's L0 on top — levels not monotone (an
    oldest run with a drill, the newer ones without: drill parity keeps it apart while the next two carry). The runs stay
    in scan order with contiguous spans, so every manifest reads exactly (brute force per scan, and the rebuild once it
    covers every scan), and the next scan's L0 carries nothing (L1 vs L0); the lone L0 is never merged with a later one."""
    a, b, c, d = chain["runs"]
    store = _store(chain, tmp_path)
    store.create(f"deltas/{a}/drill/meta.json", "{}")  # `a` drilled, `b`/`c` not: parity keeps `a` out of the carry
    for s in (a, b, c):
        _publish(store, s)
    bc = sa.run_key(b, c)
    got = _merge(store, tmp_path)
    assert [(m["inputs"], m["output"], m["level"], m["manifest"]) for m in got["merged"]] == [
        ([f"deltas/{b}", f"deltas/{c}"], bc, 1, f"manifests/{c}.m001.json")]
    assert _stack(store, f"manifests/{c}.m001.json") == [(f"deltas/{a}", 0), (bc, 1)]
    _publish(store, d)
    assert _stack(store, f"manifests/{d}.json") == [(f"deltas/{a}", 0), (bc, 1), (f"deltas/{d}", 0)]
    assert _merge(store, tmp_path, dry_run=True) == {"manifest": f"manifests/{d}.json", "plan": []}
    assert store.read_json(f"manifests/{d}.json")["scans"] == chain["ids"]
    keys = sm.manifest_keys(store.keys("manifests/"))
    assert keys == [f"manifests/{s}" for s in (f"{a}.json", f"{b}.json", f"{c}.json", f"{c}.m001.json", f"{d}.json")]
    for k in keys:
        _check_readers(chain, store, k)


def test_parallel_tier_merges_write_the_same_bytes(chain, tmp_path):
    """Each tier in its own process (`jobs` > 1) writes exactly what one process does."""
    a, b = chain["runs"][:2]
    run = _merged(a, b, 1, [a, b])
    dirs = [chain["gen"] / f"deltas/{s}" for s in (a, b)]
    one, two = tmp_path / "one", tmp_path / "two"
    sm.build_merged_run(dirs, run, one, drilled=False, rule=None, tmp=tmp_path / "t1", jobs=1, log=lambda m: None)
    sm.build_merged_run(dirs, run, two, drilled=False, rule=None, tmp=tmp_path / "t2", jobs=2, log=lambda m: None)
    files = sorted(p.relative_to(one).as_posix() for p in one.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(two).as_posix() for p in two.rglob("*") if p.is_file())
    assert [(f, (one / f).read_bytes() == (two / f).read_bytes()) for f in files] == [(f, True) for f in files]


class Boom(Exception):
    pass


class FlakyStore(sm.LocalRunStore):
    """Uploads `fail_after` files of a tree, then fails (the job killed mid-upload); or fails creating a revision."""

    def __init__(self, *a, fail_after: int | None = None, fail_create: bool = False, **kw):
        super().__init__(*a, **kw)
        self.fail_after, self.fail_create = fail_after, fail_create

    def upload(self, local: Path, prefix: str) -> list[dict]:
        if self.fail_after is None:
            return super().upload(local, prefix)
        files = sorted((p for p in local.rglob("*") if p.is_file()), key=lambda p: (p == local / "meta.json", p))
        for p in files[:self.fail_after]:
            (self.root / prefix / p.relative_to(local)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, self.root / prefix / p.relative_to(local))
        raise Boom(f"killed after {self.fail_after} files")

    def create(self, key: str, text: str) -> None:
        if self.fail_create and key.startswith("manifests/"):
            raise Boom(f"killed before {key}")
        super().create(key, text)


@pytest.mark.parametrize("where", ["build", "upload", "revision"])
def test_an_interrupted_merge_leaves_the_store_servable_and_resumes(chain, tmp_path, where):
    """A merge killed while building, midway through uploading, or before its revision: the newest manifest is the one
    before, every run it lists whole, its answers exact; the lease is released. A rerun completes it (reusing a merged dir
    that got wholly uploaded) and the result is the uninterrupted merge's."""
    a, b, c = chain["runs"][:3]
    shutil.copytree(chain["gen"], tmp_path / "gen")
    store = FlakyStore(tmp_path / "gen", tmp_path / "scratch", gen="g", fail_after=5 if where == "upload" else None)
    for s in (a, b, c):
        _publish(store, s)
    store.fail_create = where == "revision"
    before = store.listing("manifests/")
    builds = []

    def build(*args, **kw):
        builds.append(args[1]["key"])
        if where == "build" and len(builds) == 1:
            raise Boom("killed while building")
        return sm.build_merged_run(*args, **kw)

    with pytest.raises(Boom):
        _merge(store, tmp_path, build=build)
    assert store.listing("manifests/") == before
    newest = sa.latest_key(store.keys("manifests/"))
    assert newest == f"manifests/{c}.json"
    assert sa.missing_files(store.read_json(newest)["runs"], store.exists) == []
    _check_readers(chain, store, newest)
    assert not (tmp_path / "scratch" / "merge.lease.json").exists()
    ab = sa.run_key(a, b)
    assert sm.complete(store, _merged(a, b, 1, [a, b])) == (where == "revision")
    assert len(store.keys(f"{ab}/")) == {"build": 0, "upload": 5, "revision": len(store.keys(f"{ab}/"))}[where]

    store.fail_after, store.fail_create = None, False
    got = _merge(store, tmp_path, build=build)
    assert [(m["output"], m["manifest"]) for m in got["merged"]] == [(ab, f"manifests/{c}.m001.json")]
    assert builds == [ab, *([] if where == "revision" else [ab])]
    ref = _store(chain, tmp_path / "ref")
    for s in (a, b, c):
        _publish(ref, s)
    _merge(ref, tmp_path / "ref")
    assert store.listing(f"{ab}/") == ref.listing(f"{ab}/")
    assert [(k, (store.root / k).read_bytes() == (ref.root / k).read_bytes()) for k in store.keys(f"{ab}/")] == [(k, True) for k in store.keys(f"{ab}/")]
    _check_readers(chain, store, f"manifests/{c}.m001.json")


def test_a_merge_refuses_a_dir_holding_foreign_keys(chain, tmp_path):
    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)
    store.create(f"{sa.run_key(a, b)}/stray.json", "{}")
    with pytest.raises(RuntimeError) as e:
        _merge(store, tmp_path)
    assert str(e.value) == f"{sa.run_key(a, b)}/ holds 1 objects this merge doesn't write (e.g. {sa.run_key(a, b)}/stray.json): not merging into it"
    assert sa.latest_key(store.keys("manifests/")) == f"manifests/{c}.json"


def test_a_publish_landing_mid_merge_is_rebased_onto(chain, tmp_path):
    """The fourth scan publishes while the L1 merge builds (its manifest lists the four L0s): the merge's revision is
    written on the fourth's manifest instead, then the next carry (the L1 and the two L0s) is planned from it."""
    a, b, c, d = chain["runs"]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)

    def build(*args, **kw):
        if not store.exists(f"manifests/{d}.json"):
            _publish(store, d)
        return sm.build_merged_run(*args, **kw)

    got = _merge(store, tmp_path, build=build)
    ab, ad = sa.run_key(a, b), sa.run_key(a, d)
    assert [(m["output"], m["manifest"]) for m in got["merged"]] == [(ab, f"manifests/{d}.m001.json"), (ad, f"manifests/{d}.m002.json")]
    assert _stack(store, f"manifests/{d}.json") == [(f"deltas/{s}", 0) for s in (a, b, c, d)]
    assert _stack(store, f"manifests/{d}.m001.json") == [(ab, 1), (f"deltas/{c}", 0), (f"deltas/{d}", 0)]
    assert _stack(store, f"manifests/{d}.m002.json") == [(ad, 2)]
    assert not store.exists(f"manifests/{c}.m001.json")
    _check_readers(chain, store, f"manifests/{d}.m002.json")


def test_one_merger_at_a_time(chain, tmp_path):
    """A held lease: nothing merges, nothing is written; a stale one (older than a Batch task can run) is taken over."""
    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)
    held = {"owner": "other", "at": (NOW - timedelta(hours=1)).isoformat()}
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "merge.lease.json").write_text(json.dumps(held))
    listing = store.listing("")
    assert _merge(store, tmp_path) == {"held": held, "merged": [], "manifest": f"manifests/{c}.json"}
    assert store.listing("") == listing
    stale = {"owner": "other", "at": (NOW - timedelta(seconds=sm.LEASE_S + 1)).isoformat()}
    (tmp_path / "scratch" / "merge.lease.json").write_text(json.dumps(stale))
    assert [m["manifest"] for m in _merge(store, tmp_path)["merged"]] == [f"manifests/{c}.m001.json"]
    assert not (tmp_path / "scratch" / "merge.lease.json").exists()


def test_publish_refuses_a_rewrite_and_a_run_without_its_files(chain, tmp_path):
    a, b = chain["runs"][:2]
    store = _store(chain, tmp_path)
    _publish(store, a)
    with pytest.raises(SystemExit) as e:
        _publish(store, a)
    assert str(e.value) == "manifests/" + f"{a}.json exists: manifests are never rewritten"
    (store.root / f"deltas/{b}/catalog/index.parquet").unlink()
    with pytest.raises(SystemExit) as e:
        _publish(store, b)
    assert str(e.value) == f"not publishing manifests/{b}.json: listed runs lack ['deltas/{b}/catalog/index.parquet']"
    assert sm.manifest_keys(store.keys("manifests/")) == [f"manifests/{a}.json"]


def test_anchors_and_drill_readers_take_the_newest_revision(chain, tmp_path):
    """The Python readers that find the newest manifest on a mount (`static_anchors`) take revisions as the Worker does."""
    from dt_cloud.static_anchors import _manifest_runs

    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path / "static-names")
    for s in (a, b, c):
        _publish(store, s)
    _merge(store, tmp_path)
    (tmp_path / "static-names" / "g").symlink_to(store.root)
    assert [r["key"] for r in _manifest_runs(str(tmp_path), "g")] == [sa.run_key(a, b), f"deltas/{c}"]
    # an inner scan's earlier tiers (`anchors run` backfilling it): the newest manifest of an earlier scan's
    assert [[r["key"] for r in _manifest_runs(str(tmp_path), "g", before=x)] for x in (a, b, c)] == [
        [], [f"deltas/{a}"], [f"deltas/{a}", f"deltas/{b}"]]


def test_a_revision_is_refused_while_a_run_it_lists_lacks_a_file(chain, tmp_path):
    """A merged dir missing a reader file (here its catalog index) is never listed: the revision is refused, the newest
    manifest stays the one before."""
    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)

    def build(dirs, run, outp, **kw):
        doc = sm.build_merged_run(dirs, run, outp, **kw)
        (outp / "catalog" / "index.parquet").unlink()
        return doc

    with pytest.raises(RuntimeError) as e:
        _merge(store, tmp_path, build=build)
    ab = sa.run_key(a, b)
    assert str(e.value) == f"not publishing manifests/{c}.m001.json: listed runs lack ['{ab}/catalog/index.parquet']"
    assert sa.latest_key(store.keys("manifests/")) == f"manifests/{c}.json"


class RacingStore(sm.LocalRunStore):
    """A scan's manifest lands right after the merge creates its revision (before it re-checks which manifest is newest):
    `stale` — built from the manifest before the revision (its publish read it earlier); else published then, on the revision."""

    racer: tuple[str, dict | None] | None = None

    def create(self, key: str, text: str) -> None:
        super().create(key, text)
        if self.racer and key.startswith("manifests/") and ".m" in key:
            (d, doc), self.racer = self.racer, None
            if doc is None:
                sa.publish_run(self, d)
            else:
                super().create(f"deltas/{d}/meta.json", json.dumps({"scans": [d]}))
                super().create(f"manifests/{d}.json", json.dumps(doc))


@pytest.mark.parametrize("stale", [True, False])
def test_a_publish_landing_right_after_a_revision(chain, tmp_path, stale):
    """The revision of the third scan's manifest is shadowed at once by the fourth's. Built from the third's own manifest
    (stale), it lists the unmerged runs: the merge sees its revision isn't newest and writes a revision of the fourth's.
    Published on the revision, it lists the merged run already: nothing more to write."""
    a, b, c, d = chain["runs"]
    shutil.copytree(chain["gen"], tmp_path / "gen")
    store = RacingStore(tmp_path / "gen", tmp_path / "scratch", gen="g")
    for s in (a, b, c):
        _publish(store, s)
    store.racer = (d, sa.publish_run(store, d, dry_run=True)["manifest"] if stale else None)
    got = _merge(store, tmp_path, max_merges=1)
    ab = sa.run_key(a, b)
    assert [(m["output"], m["manifest"]) for m in got["merged"]] == [(ab, f"manifests/{d}.m001.json" if stale else None)]
    assert sm.manifest_keys(store.keys("manifests/")) == [f"manifests/{n}" for n in (
        f"{a}.json", f"{b}.json", f"{c}.json", f"{c}.m001.json", f"{d}.json", *([f"{d}.m001.json"] if stale else []))]
    assert _stack(store, sa.latest_key(store.keys("manifests/"))) == [(ab, 1), (f"deltas/{c}", 0), (f"deltas/{d}", 0)]


# ── Backfilling a merged run's tier ────────────────────────────────────────


class OrderStore(sm.LocalRunStore):
    """Records each uploaded key in order."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.uploaded: list[str] = []

    def upload(self, local, prefix, last=("meta.json",)):
        out = super().upload(local, prefix, last)
        self.uploaded += [f["key"] for f in out]
        return out


def _fake_merge(name, dirs, outp, run, rule, tmp, mem, threads):
    """A tier merge's shape: the drill's files (or names + anchors, with the starts-with catalog), `meta.json` naming
    the inputs."""
    files = {"drill": ["drill/long/roots/r0000.parquet", "drill/aliases.parquet", "drill/meta.json"],
             "anchors": ["names/s0000.parquet", "names/sidecar.parquet", "anchors/meta.json", "anchors/start/meta.json", "anchors/rollups/long-s0000.parquet"]}[name]
    for f in files:
        (outp / f).parent.mkdir(parents=True, exist_ok=True)
        (outp / f).write_text(json.dumps({"inputs": [d.name for d in dirs], "run": run, "mem": mem, "threads": threads}) + "\n")
    return name, 0.0, {"files": len(files)}


def _bf_store(tmp_path, *built: str, tier: str = "drill") -> OrderStore:
    """cw's stack: `manifests/c.json` lists the L1 `deltas/a_b` and the L0 `deltas/c`; `built` runs hold `tier`."""
    store = OrderStore(tmp_path / "gen", tmp_path / "scratch", gen="g")
    a, b, c = "2026-10-09T1801", "2026-10-10T0001", "2026-10-10T0601"
    store.create(f"manifests/{c}.json", json.dumps({"date": c, "runs": [_merged(a, b, 1, [a, b]), _run(c)]}))
    marker = sm.BACKFILL_TIERS[tier]["last"][-1]
    for k in built:
        store.create(f"{k}/{marker}", "{}")
    return store


def test_backfill_tier_merges_the_scans_runs_with_its_markers_last(tmp_path):
    a, b = "2026-10-09T1801", "2026-10-10T0001"
    key = f"deltas/{a}_{b}"
    store = _bf_store(tmp_path, f"deltas/{a}", f"deltas/{b}")
    doc = sm.backfill_tier(store, store.root, key, "drill", tmp=tmp_path / "t", mem="90GB", threads=16, merge=_fake_merge, log=lambda m: None)
    assert doc == {"key": key, "tier": "drill", "inputs": [f"deltas/{a}", f"deltas/{b}"], "files": 3, "doc": {"files": 3}}
    assert store.uploaded == [f"{key}/drill/aliases.parquet", f"{key}/drill/long/roots/r0000.parquet", f"{key}/drill/meta.json"]
    assert store.read_json(f"{key}/drill/meta.json") == {"inputs": [a, b], "mem": "90GB", "threads": 16,
                                                         "run": {"key": key, "first": a, "last": b, "level": 1, "scans": [a, b]}}
    # done: a no-op
    assert sm.backfill_tier(store, store.root, key, "drill", tmp=tmp_path / "t", merge=_fake_merge, log=lambda m: None) is None


def test_backfill_tier_anchors_markers_last_in_order(tmp_path):
    a, b = "2026-10-09T1801", "2026-10-10T0001"
    key = f"deltas/{a}_{b}"
    store = _bf_store(tmp_path, f"deltas/{a}", f"deltas/{b}", tier="anchors")
    sm.backfill_tier(store, store.root, key, "anchors", tmp=tmp_path / "t", merge=_fake_merge, log=lambda m: None)
    assert store.uploaded == [f"{key}/{f}" for f in ("anchors/rollups/long-s0000.parquet", "names/s0000.parquet", "names/sidecar.parquet",
                                                    "anchors/start/meta.json", "anchors/meta.json")]


def test_backfill_tier_refuses(tmp_path):
    a, b, c = "2026-10-09T1801", "2026-10-10T0001", "2026-10-10T0601"
    key = f"deltas/{a}_{b}"
    bf = lambda store, k: sm.backfill_tier(store, store.root, k, "drill", tmp=tmp_path / "t", merge=_fake_merge, log=lambda m: None)  # noqa: E731
    got = []
    for store, k in ((_bf_store(tmp_path / "1", f"deltas/{a}"), key), (_bf_store(tmp_path / "2"), f"deltas/{c}"), (_bf_store(tmp_path / "3"), "deltas/x_y")):
        with pytest.raises(ValueError) as e:
            bf(store, k)
        got.append(str(e.value))
    store = _bf_store(tmp_path / "4", f"deltas/{a}", f"deltas/{b}")
    store.create(f"{key}/drill/stray.parquet", "")
    with pytest.raises(RuntimeError) as e:
        bf(store, key)
    got.append(str(e.value))
    assert got == [
        f"{key}: its scans' runs lack drill/meta.json: ['deltas/{b}'] (build them first, oldest first)",
        f"deltas/{c}: a level-0 run (its drill is built, not merged)",
        "deltas/x_y: no manifest lists it",
        f"{key}/ holds 1 drill objects this backfill doesn't write (e.g. {key}/drill/stray.parquet): not writing into it",
    ]
    assert not store.exists(f"{key}/drill/meta.json")


# ── The merge status record (`merging.json` on R2, for /health) ────────────


class RecordingStatus:
    """A `MergeStatus` that keeps every write (`("put", doc)` / `("delete",)`) and the record as R2 would hold it; `fail`:
    every write raises (R2 down)."""

    def __init__(self, fail: bool = False):
        self.events: list[tuple] = []
        self.doc: dict | None = None
        self.fail = fail

    def put(self, doc: dict) -> None:
        if self.fail:
            raise ConnectionError("r2 unreachable")
        self.events.append(("put", json.loads(json.dumps(doc))))
        self.doc = json.loads(json.dumps(doc))

    def delete(self) -> None:
        if self.fail:
            raise ConnectionError("r2 unreachable")
        self.events.append(("delete",))
        self.doc = None


T0 = int(NOW.timestamp())


def _record(merges: list[dict], done: list[dict], manifest: str, **kw) -> dict:
    return {"holder": "sn-merge-x", "owner": "test", "state": "merging", "started_ts": T0, "updated_ts": T0, "heartbeat_s": None,
            "lease_s": sm.LEASE_S, "manifest": manifest, "merges": merges, "done": done, **kw}


def test_the_status_record_is_written_refreshed_and_removed(chain, tmp_path, monkeypatch):
    """Two carries in one job (the fourth scan publishes while the first builds): the record is written as the first
    starts (its plan, the job's name as holder), rewritten when its revision lands (the next carry planned, the first
    done), and removed once nothing is due. A job killed mid-merge leaves the record as R2 held it then: still
    `merging`, its `updated_ts` aging (with no heartbeat refreshing it), which /health flags as stuck."""
    monkeypatch.setenv("DT_JOB_NAME", "sn-merge-x")
    a, b, c, d = chain["runs"]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)
    status = RecordingStatus()
    mid: list[dict | None] = []

    def build(*args, **kw):
        mid.append(status.doc)  # what R2 holds while the run builds (and after a kill here)
        if not store.exists(f"manifests/{d}.json"):
            _publish(store, d)
        return sm.build_merged_run(*args, **kw)

    ticks = iter(range(100))  # the lease's clock read, then one per record write
    got = sm.merge_pending(store, store.root, tmp=tmp_path / "work", owner="test", now=lambda: NOW + timedelta(seconds=next(ticks)), log=lambda m: None,
                           build=build, status=status, heartbeat_s=None)
    ab, ad = sa.run_key(a, b), sa.run_key(a, d)
    assert [(m["output"], m["manifest"]) for m in got["merged"]] == [(ab, f"manifests/{d}.m001.json"), (ad, f"manifests/{d}.m002.json")]
    first = _record([{"inputs": [f"deltas/{a}", f"deltas/{b}"], "output": ab, "level": 1, "first": a, "last": b, "scans": 2}], [], f"manifests/{c}.json",
                    started_ts=T0 + 1, updated_ts=T0 + 1)
    second = _record([{"inputs": [ab, f"deltas/{c}", f"deltas/{d}"], "output": ad, "level": 2, "first": a, "last": d, "scans": 4}],
                     [{"output": ab, "manifest": f"manifests/{d}.m001.json"}], f"manifests/{d}.m001.json", started_ts=T0 + 1, updated_ts=T0 + 2)
    assert status.events == [("put", first), ("put", second), ("delete",)]
    assert mid == [first, second]
    assert status.doc is None


def test_the_heartbeat_refreshes_the_record_while_a_run_builds(chain, tmp_path):
    """A long build: the record's `updated_ts` advances every `heartbeat_s` (nothing else changes), and stops with the merge
    (the delete is the last write)."""
    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)
    status = RecordingStatus()
    ticks = iter(range(10_000))

    def build(*args, **kw):
        import time
        time.sleep(0.3)
        return sm.build_merged_run(*args, **kw)

    sm.merge_pending(store, store.root, tmp=tmp_path / "work", owner="test", now=lambda: NOW + timedelta(seconds=next(ticks)), log=lambda m: None,
                     build=build, status=status, heartbeat_s=0.02)
    puts = [e[1] for e in status.events if e[0] == "put"]
    assert status.events[-1] == ("delete",)
    assert len(puts) >= 3
    assert [{k: v for k, v in p.items() if k != "updated_ts"} for p in puts] == [{k: v for k, v in puts[0].items() if k != "updated_ts"}] * len(puts)
    beats = [p["updated_ts"] for p in puts]
    assert beats == sorted(set(beats))
    assert puts[0]["started_ts"] == puts[0]["updated_ts"] < beats[-1]


def test_a_failed_merge_marks_the_record_failed_and_keeps_it(chain, tmp_path):
    """A merge that raises (the lease released): the record stays on R2, `state: failed` with the error, until the next
    merge takes the lease — which writes its own, and removes it when done."""
    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)
    status = RecordingStatus()

    def boom(*args, **kw):
        raise Boom("killed while building")

    with pytest.raises(Boom):
        _merge(store, tmp_path, build=boom, status=status, heartbeat_s=None)
    ab = sa.run_key(a, b)
    plan = [{"inputs": [f"deltas/{a}", f"deltas/{b}"], "output": ab, "level": 1, "first": a, "last": b, "scans": 2}]
    merging = _record(plan, [], f"manifests/{c}.json", holder="test")
    assert status.events == [("put", merging), ("put", {**merging, "state": "failed", "error": "Boom: killed while building"})]
    assert not (tmp_path / "scratch" / "merge.lease.json").exists()
    _merge(store, tmp_path, status=status, heartbeat_s=None)
    assert (status.events[2:], status.doc) == ([("put", merging), ("delete",)], None)


def test_status_write_failures_never_fail_the_merge(chain, tmp_path):
    """R2 unreachable: every status write fails, each logged; the merge publishes exactly what it does without a status."""
    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)
    logs = []
    got = sm.merge_pending(store, store.root, tmp=tmp_path / "work", owner="test", now=lambda: NOW, log=logs.append,
                           status=RecordingStatus(fail=True), heartbeat_s=None)
    assert [m["manifest"] for m in got["merged"]] == [f"manifests/{c}.m001.json"]
    assert _stack(store, f"manifests/{c}.m001.json") == [(sa.run_key(a, b), 1), (f"deltas/{c}", 0)]
    assert [m for m in logs if m.startswith("merge status")] == [
        "merge status: write failed, not fatal: r2 unreachable", "merge status: delete failed, not fatal: r2 unreachable"]


def test_a_held_lease_leaves_the_record_alone(chain, tmp_path):
    """Another holder's merge: this job neither writes nor removes the record."""
    a, b, c = chain["runs"][:3]
    store = _store(chain, tmp_path)
    for s in (a, b, c):
        _publish(store, s)
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "merge.lease.json").write_text(json.dumps({"owner": "other", "at": (NOW - timedelta(hours=1)).isoformat()}))
    status = RecordingStatus()
    assert _merge(store, tmp_path, status=status, heartbeat_s=None)["merged"] == []
    assert status.events == []
