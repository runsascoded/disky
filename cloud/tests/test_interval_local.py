"""`dt_cloud.interval_local`: the single-machine `interval-store build` writes the generation the staged path's stages
write over the same ranges (range files and served sorts, byte for byte), and `interval-store add` appends scans as runs
that are the staged path's appends byte for byte (the open versions refolded from the published generation, not kept),
read version for version as a full rebuild, and serve every view at every scan as the per-scan reference does. A URL
store gets the local store's files; an interrupted add's unpublished run is redone and its stale keys replaced. And
the store seam (`blobstore`): bare bucket names, the serving-R2 no-op, the staged chain's GCS-only profile."""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner
from test_interval_append import _rows, _views_equal, build_full, combine
from test_interval_store import V2_FROM, _scan_rows, _universe, _write

from dt_cloud import append_runner as ar
from dt_cloud import blobstore as bs
from dt_cloud import interval_append as ia
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn

#: Four scans in the base (three v1 indexes, one v2 store generation), then four appended (v2, a timed id among them).
DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-04T1236", "2026-08-05", "2026-08-07"]
BASE = 4
LAYOUT = "listing/{id}/path-index.parquet"
RG = 4


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("ivl")
    rng = random.Random(11)
    paths = _universe(rng)
    scans = []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        key = LAYOUT.replace("{id}", d)
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(_scan_rows(rng, paths, j), root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    return {"root": root, "scans": scans, "paths": paths}


def _build(world, store: Path | str, work: Path, gen: str = "g") -> dict:
    args = ["-g", gen, "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(work), "-k", "3", "-r", str(RG), "-p", "2", "-M", "1GB",
            "-t", DATES[BASE - 1]]
    res = CliRunner().invoke(il.build_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _add(world, store: Path | str, work: Path, gen: str = "g", *extra: str) -> dict:
    args = ["-g", gen, "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(work), "-r", str(RG), "-p", "2", "-M", "1GB", *extra]
    res = CliRunner().invoke(il.add_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _parquets(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*.parquet"))}


@pytest.fixture(scope="module")
def local(world, tmp_path_factory):
    """A local store: the base built over the first four scans (a copy kept), then the other four added."""
    tmp = tmp_path_factory.mktemp("ivl-store")
    store = tmp / "store"
    summary = _build(world, store, tmp / "work-build")
    gen = store / ist.PREFIX / "g"
    shutil.copytree(store, tmp / "base-only")
    out = _add(world, store, tmp / "work-add")
    return {"tmp": tmp, "store": store, "gen": gen, "base_only": tmp / "base-only", "summary": summary, "out": out}


@pytest.fixture(scope="module")
def staged(world, local, tmp_path_factory):
    """The staged path's stages over the local build's ranges: the base's range files and served sorts, then each
    appended scan's run from the previous run's open state (as `runs ranges` reads it from the scratch bucket)."""
    tmp = tmp_path_factory.mktemp("ivl-staged")
    root, scans = world["root"], world["scans"]
    ranges = json.loads((local["gen"] / "ranges.json").read_text())
    doc = {"bucket": str(root), "scans": scans[:BASE]}
    base = tmp / "base"
    con = duckdb.connect()
    for i in range(ranges["k"]):
        ist.build_range(doc, ranges, i, base, con, mount=str(root))
        ist.fold_range(str(base), i, base, con)
        ist.build_slices_range(doc, ranges, i, base, con, mount=str(root))
        ist.slice_totals_range(str(base), i, base, con)
    for sort, (sub, _, _) in ist.SORTS.items():
        ist.write_served(con, f"read_parquet('{base}/{sub}/r*.parquet')", sort, base / "served" / f"{sort}.parquet", ist.SUB_SCHEMA[sub], rg_rows=RG,
                         stamps=[s["ts"] for s in scans[:BASE]])
    runs, prev = {}, None
    for s in scans[BASE:]:
        out = tmp / "runs" / s["id"]
        for i in range(ranges["k"]):
            name = f"r{i:04d}"
            state = ia.run_state_sql(str(prev / "state"), name) if prev else ia.base_state_sql(str(base), name)
            ia.append_range(con, state, s, ranges["ranges"][i], name, out, bucket="b", mount=str(root))
        ia.cut_run(con, str(out), out / "served", rg_rows=RG)
        runs[s["id"]] = out
        prev = out
    return {"base": base, "runs": runs, "ranges": ranges}


def test_build_writes_the_staged_generation(world, local, staged):
    gen = local["gen"]
    for sub in ("pv", "rd", "pvl", "sv", "svt", "served"):
        got = {k: v for k, v in _parquets(gen).items() if k.startswith(f"{sub}/")}
        want = {k: v for k, v in _parquets(staged["base"]).items() if k.startswith(f"{sub}/")}
        assert sorted(got) == sorted(want) and got == want, sub
    assert sorted(p.name for p in (gen / "served").iterdir()) == sorted(
        [*(f"{s}{x}" for s in ist.SORTS for x in (".parquet", ".groups.parquet", ".json")), "stats.json"])
    assert json.loads((gen / "scans.json").read_text()) == {
        "bucket": str(world["root"]), "layouts": [LAYOUT],
        "scans": [{**s, "size": (world["root"] / s["src"]).stat().st_size} for s in world["scans"][:BASE]], "slices": True}
    s = local["summary"]
    assert (s["gen"], s["scans"], s["ranges"], s["slices"], sorted(s["stages"]), sorted(s["peaks"])) == (
        "g", BASE, staged["ranges"]["k"], True, ["cut", "slices", "versions"], ["cut", "slices", "versions"])


def test_build_refuses_an_existing_generation(world, local, tmp_path):
    res = CliRunner().invoke(il.build_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(local["store"]), "-w", str(tmp_path)])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{local['store']}/{ist.PREFIX}/g/ exists: a generation is never rewritten")


def test_add_publishes_each_scan_then_one_carry(world, local):
    ids = [s["id"] for s in world["scans"][BASE:]]
    gen = local["gen"]
    assert sorted(p.name for p in (gen / "manifests").iterdir()) == [*(f"{d}.json" for d in ids), f"{ids[-1]}.m001.json"]
    newest = json.loads((gen / "manifests" / f"{ids[-1]}.m001.json").read_text())
    assert [(r["key"], r["level"], r["scans"]) for r in newest["runs"]] == [(ar.run_key(ids[0], ids[-1]), 2, ids)]
    assert newest["scans"] == DATES
    out = local["out"]
    assert ([a["scan"] for a in out["added"]], [a["runs"] for a in out["added"]]) == (
        ids, [[f"deltas/{d}" for d in ids[:j + 1]] for j in range(len(ids))])
    assert [(m["inputs"], m["output"], m["manifest"]) for m in out["merge"]["merged"]] == [
        ([f"deltas/{d}" for d in ids], ar.run_key(ids[0], ids[-1]), f"manifests/{ids[-1]}.m001.json")]
    # the base's files are never rewritten
    base = local["base_only"] / ist.PREFIX / "g"
    assert {k: v for k, v in _parquets(gen).items() if (base / k).exists()} == _parquets(base)


def test_each_run_is_the_staged_append(world, local, staged):
    """The refolded open versions give each run the bytes the staged path's state chain gives it: deltas and served sorts."""
    k = staged["ranges"]["k"]
    for d, run in staged["runs"].items():
        got = _parquets(local["gen"] / "deltas" / d)
        assert sorted(got) == sorted([*(f"{t}/r{i:04d}.parquet" for t in ia.TABLES for i in range(k)),
                                      *(f"served/{s}{x}" for s in ia.RUN_SORTS for x in (".parquet", ".groups.parquet"))]), d
        assert got == {key: v for key, v in _parquets(run).items() if not key.startswith("state/")}, d


@pytest.mark.parametrize("t", ia.TABLES)
def test_base_and_runs_are_a_full_rebuild(world, local, tmp_path, t):
    """Base + the newest manifest's runs (the merged one), and base + the level-0 runs alone: `build`/`fold` over every
    scan, version for version."""
    gen = local["gen"]
    full = build_full(world["scans"], world["root"], tmp_path / "full")
    want = combine([_rows(full, t)], t)
    newest = json.loads((gen / "manifests" / f"{DATES[-1]}.m001.json").read_text())
    for runs in ([gen / r["key"] for r in newest["runs"]], [gen / "deltas" / d for d in DATES[BASE:]]):
        assert combine([_rows(gen, t), *(_rows(r, t) for r in runs)], t) == want


def test_every_view_at_every_scan_is_the_per_scan_readers(world, local):
    """The generation as the reader opens it (the base's served sorts, the newest manifest's runs' served sorts): at
    every scan, base or appended, every directory's view is the per-scan reference's, tile for tile."""
    from dt_cloud import interval_read as ir

    gen = local["gen"]
    newest = json.loads((gen / "manifests" / f"{DATES[-1]}.m001.json").read_text())
    runs = [gen / r["key"] / "served" for r in newest["runs"]]
    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    assert _views_equal(world, lambda s: ir.Store(gen / "served", runs), paths) > 0


def test_add_with_nothing_pending_is_a_nop(world, local, tmp_path):
    before = {p.relative_to(local["gen"]).as_posix(): p.read_bytes() for p in sorted(local["gen"].rglob("*")) if p.is_file()}
    out = _add(world, local["store"], tmp_path / "work")
    ids = DATES[BASE:]
    assert out == {"gen": "g", "after": DATES[-1], "added": [], "merge": None, "files": 0,
                   "prune": {"manifest": f"manifests/{ids[-1]}.m001.json", "grace": [f"manifests/{d}.json" for d in ids], "runs": [],
                             "objects": 0, "bytes": 0, "deleted": 0}}
    assert {p.relative_to(local["gen"]).as_posix(): p.read_bytes() for p in sorted(local["gen"].rglob("*")) if p.is_file()} == before


def test_an_unpublished_run_is_redone_and_its_stale_keys_replaced(world, local, tmp_path):
    """An earlier add died after uploading part of the first pending scan's run (no manifest): the rerun redoes the run,
    overwrites its keys and removes the ones it doesn't write; the generation ends as a clean add's."""
    store = tmp_path / "store"
    shutil.copytree(local["base_only"], store)
    run = store / ist.PREFIX / "g" / "deltas" / DATES[BASE]
    (run / "pvl").mkdir(parents=True)
    (run / "pvl" / "r0000.parquet").write_bytes(b"torn")
    (run / "junk.txt").write_text("an earlier attempt's\n")
    _add(world, store, tmp_path / "work")
    assert _parquets(store / ist.PREFIX / "g") == _parquets(local["gen"])
    assert (run / "junk.txt").exists() is False


def test_a_url_store_matches_a_local_one(world, local, tmp_path):
    """A URL store (fsspec `memory://`): build then add give the local store's data files and manifests."""
    import fsspec

    url = f"memory://{tmp_path.name}/store"
    _build(world, url, tmp_path / "work-build")
    _add(world, url, tmp_path / "work-add")
    fs = fsspec.filesystem("memory")
    got = {k.split("/store/", 1)[1]: fs.cat_file(k) for k in sorted(fs.find(f"/{tmp_path.name}/store"))}
    want = {f"{ist.PREFIX}/g/{p.relative_to(local['gen']).as_posix()}": p.read_bytes() for p in sorted(local["gen"].rglob("*")) if p.is_file()}
    assert sorted(got) == sorted(want)
    stable = lambda k: k.endswith(".parquet") or "/manifests/" in k or k.endswith("/scans.json")  # noqa: E731 — the rest carry timings
    assert {k: v for k, v in got.items() if stable(k)} == {k: v for k, v in want.items() if stable(k)}


def test_publish_order():
    keys = ["manifests/a.json", "deltas/a/meta.json", "scans.json", "deltas/a/served/path.parquet", "deltas/a/scans.json", "served/path.parquet",
            "manifests/a.m001.json", "deltas/a_b/meta.json"]
    assert il.publish_order(keys) == ["deltas/a/scans.json", "deltas/a/served/path.parquet", "served/path.parquet", "deltas/a/meta.json",
                                      "deltas/a_b/meta.json", "scans.json", "manifests/a.json", "manifests/a.m001.json"]


def test_open_state_is_the_staged_state(world, local, staged):
    """`open_state_sql` over the base and the published runs is the staged path's `state/` after the newest scan."""
    con = duckdb.connect()
    gen = local["gen"]
    newest = json.loads((gen / "manifests" / f"{DATES[-1]}.m001.json").read_text())
    last = staged["runs"][DATES[-1]]
    for i in range(staged["ranges"]["k"]):
        name = f"r{i:04d}"
        got = il.open_state_sql(str(gen), [str(gen / r["key"]) for r in newest["runs"]], name)
        want = ia.run_state_sql(str(last / "state"), name)
        for t in ia.TABLES:
            order = ", ".join(ia.IDENT[t])
            rows = lambda sql: con.execute(f"SELECT * FROM ({sql}) ORDER BY {order}").fetchall()  # noqa: E731
            assert rows(got[t]) == rows(want[t]), (name, t)


# ── The store seam ─────────────────────────────────────────────────────────


def test_store_url_and_serving():
    assert [bs.store_url(r) for r in ("oa-bucket", "gs://oa-bucket/", "r2://demo", "s3://b/p", "/abs/dir", "rel/dir", "./d")] == [
        "gs://oa-bucket", "gs://oa-bucket", "r2://demo", "s3://b/p", "/abs/dir", "rel/dir", "./d"]
    assert [bs.gs_bucket(r) for r in ("oa-bucket", "gs://oa-bucket", "gs://oa-bucket/sub", "r2://demo", "/d")] == ["oa-bucket", "oa-bucket", None, None, None]
    assert [bs.serves(r, b) for r, b in (("r2://demo", "demo"), ("r2://demo/", "demo"), ("r2://demo/sub", "demo"), ("gs://demo", "demo"),
                                           ("r2://demo", None), ("demo", "demo"))] == [True, True, False, False, False, False]


def test_r2_copies_are_no_ops_on_the_serving_bucket(monkeypatch):
    errs = []
    monkeypatch.setenv("R2_BUCKET", "demo")
    monkeypatch.setattr(ia, "err", errs.append)
    monkeypatch.setattr(ist, "err", errs.append)
    r = CliRunner().invoke(ia.cli, ["r2", "-b", "r2://demo", "-g", "g", "-R", "g", "-S", "r2://demo", "-d", "2026-08-04"])
    assert (r.exit_code, r.output) == (0, "")
    r = CliRunner().invoke(ist.cli, ["r2-copy", "-b", "r2://demo", "-g", "g"])
    assert (r.exit_code, r.output) == (0, "")
    assert errs == ["r2 2026-08-04: the store r2://demo is the serving R2 bucket; nothing to copy",
                    "r2-copy g: the store r2://demo is the serving R2 bucket; nothing to copy"]


def test_the_staged_chain_takes_a_gcs_bucket_by_name_or_url(tmp_path):
    def cfg(bucket: str):
        p = tmp_path / f"{len(list(tmp_path.iterdir()))}.json"
        p.write_text(json.dumps({"bucket": bucket, "append": {"gen": "g", "scratch": "gs://scr", "layouts": [LAYOUT], "region": "r", "image": "i",
                                                              "sa": "sa", "r2_bucket": "demo"}}))
        return ia.load_config(str(p), environ={})

    assert [(c.p.bucket, c.p.scratch) for c in (cfg("data"), cfg("gs://data"))] == [("data", "scr"), ("data", "scr")]
    with pytest.raises(SystemExit) as e:
        cfg("r2://demo")
    assert str(e.value) == ("interval store: the staged append mounts a GCS bucket; bucket 'r2://demo' is not one "
                            "(append on one machine: `dt-cloud interval-store add`)")


def test_run_store_over_a_dir_creates_once_and_leases(tmp_path):
    from datetime import datetime, timedelta, timezone

    s = bs.run_store(str(tmp_path / "data"), str(tmp_path / "scr"), "g", prefix="interval-store")
    s.create("manifests/a.json", "{}\n")
    with pytest.raises(FileExistsError):
        s.create("manifests/a.json", "{}\n")
    assert (s.keys("manifests/"), s.read_json("manifests/a.json")) == (["manifests/a.json"], {})
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert (s.lease("x", now, 60), s.lease("y", now, 60)) == (None, {"owner": "x", "at": now.isoformat()})
    assert s.lease("y", now + timedelta(seconds=61), 60) is None
    s.release("y")
    assert (tmp_path / "scr" / "interval-store" / "g" / "merge.lease.json").exists() is False


def test_add_prunes_the_folded_runs_after_the_grace(world, tmp_path):
    """The four scans appended one `add` at a time, as if `AGE` s apart (each `add` first ages the store's manifests by
    that much), grace `-G 600`: once a revision folds runs and is itself superseded past the grace, those runs leave the
    store but for `interval_append.FOLDED_RETAIN`; the newest manifest still reads as the full rebuild."""
    import os

    ids = [s["id"] for s in world["scans"][BASE:]]
    store = tmp_path / "store"
    _build(world, store, tmp_path / "work-build")
    gen = store / ist.PREFIX / "g"
    AGE, GRACE = 700, 600
    outs = []
    for d in ids:
        for p in (gen / "manifests").glob("*.json"):
            t = p.stat().st_mtime - AGE
            os.utime(p, (t, t))
        outs.append(_add(world, store, tmp_path / "work", "g", "-t", d, "-G", str(GRACE)))
    newest = json.loads(sorted((gen / "manifests").iterdir(), key=lambda p: ar.parse_manifest(p.name))[-1].read_text())
    live = {r["key"] for r in newest["runs"]}
    pruned = sorted(r["key"] for o in outs for r in o["prune"]["runs"])
    assert pruned == sorted(f"deltas/{d}" for d in ids[:2])
    for key in pruned:
        assert sorted(p.relative_to(gen / key).as_posix() for p in (gen / key).rglob("*") if p.is_file()) == sorted(
            [*(f"ranges/{p.name}" for p in (gen / key / "ranges").glob("*")), "scans.json"]), key
    for t in ia.TABLES:
        full = build_full(world["scans"], world["root"], tmp_path / f"full-{t}")
        assert combine([_rows(gen, t), *(_rows(gen / k, t) for k in sorted(live))], t) == combine([_rows(full, t)], t)


# ── A generation without the owner axis ────────────────────────────────────


def _unowned_world(root: Path, owned: tuple[str, ...] = ()) -> dict:
    """The world's scans (same generator and seed) with no owner named (cw-s3, r2.rbw.sh): `usr` NULL on every row of the
    v1 indexes, no `usr` column in the v2 sorts; but the `owned` scans, which keep theirs."""
    import pyarrow.parquet as pq

    rng = random.Random(11)
    paths = _universe(rng)
    scans = []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        key = LAYOUT.replace("{id}", d)
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        rows = _scan_rows(rng, paths, j)
        _write(rows if d in owned else [{**r, "usr": None} for r in rows], root / key, v)
        if v == 2 and d not in owned:
            pq.write_table(pq.read_table(root / key).drop_columns(["usr"]), root / key, row_group_size=7)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    return {"root": root, "scans": scans, "paths": paths}


@pytest.fixture(scope="module")
def unowned(tmp_path_factory):
    """A store over the unowned scans: the base built over the first four, then the other four added."""
    tmp = tmp_path_factory.mktemp("ivl-unowned")
    world = _unowned_world(tmp / "scans")
    store = tmp / "store"
    summary = _build(world, store, tmp / "work-build")
    out = _add(world, store, tmp / "work-add")
    return {"world": world, "tmp": tmp, "gen": store / ist.PREFIX / "g", "summary": summary, "out": out}


def test_an_unowned_generation_builds_and_appends_no_slices(unowned):
    """No scan names an owner: the build records `"slices": false` and builds no `sv/`, `svt/`, `sv-digest/` or slice sort;
    each run carries `pvl` and the folded sorts alone, and so does the merged run."""
    gen, world = unowned["gen"], unowned["world"]
    ids = DATES[BASE:]
    assert json.loads((gen / "scans.json").read_text()) == {
        "bucket": str(world["root"]), "layouts": [LAYOUT],
        "scans": [{**s, "size": (world["root"] / s["src"]).stat().st_size} for s in world["scans"][:BASE]], "slices": False}
    assert json.loads((gen / "served" / "stats.json").read_text())["slices"] is False
    s = unowned["summary"]
    assert (s["slices"], sorted(s["stages"]), sorted(s["sorts"])) == (False, ["cut", "versions"], ["bysize", "path", "reads"])
    assert sorted(p.name for p in gen.iterdir()) == ["deltas", "digest", "manifests", "pv", "pvl", "ranges.json", "rd", "scans.json", "served"]
    assert sorted(p.name for p in (gen / "served").iterdir()) == sorted(
        [*(f"{x}{e}" for x in ("path", "bysize", "reads") for e in (".parquet", ".groups.parquet", ".json")), "stats.json"])
    k = json.loads((gen / "ranges.json").read_text())["k"]
    merged = ar.run_key(ids[0], ids[-1])
    for key in [*(f"deltas/{d}" for d in ids), merged]:
        assert sorted(_parquets(gen / key)) == sorted([*(f"pvl/r{i:04d}.parquet" for i in range(k)),
                                                       *(f"served/{x}{e}" for x in ("path", "bysize") for e in (".parquet", ".groups.parquet"))]), key
    newest = json.loads((gen / "manifests" / f"{ids[-1]}.m001.json").read_text())
    assert [(r["key"], r["scans"]) for r in newest["runs"]] == [(merged, ids)]


def test_an_unowned_generation_is_a_full_rebuild(unowned, tmp_path):
    """Base + the newest manifest's run (merged), and base + the level-0 runs: `build`/`fold` over every scan, version for
    version (`pvl`), and every directory's view at every scan is the per-scan reference's."""
    from dt_cloud import interval_read as ir

    gen, world = unowned["gen"], unowned["world"]
    full = build_full(world["scans"], world["root"], tmp_path / "full")
    want = combine([_rows(full, "pvl")], "pvl")
    newest = json.loads((gen / "manifests" / f"{DATES[-1]}.m001.json").read_text())
    for runs in ([gen / r["key"] for r in newest["runs"]], [gen / "deltas" / d for d in DATES[BASE:]]):
        assert combine([_rows(gen, "pvl"), *(_rows(r, "pvl") for r in runs)], "pvl") == want
    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    assert _views_equal(world, lambda s: ir.Store(gen / "served", [gen / r["key"] / "served" for r in newest["runs"]]), paths) > 0


def test_a_scan_naming_owners_is_refused_by_an_unowned_generation(tmp_path):
    """The base names no owner; the first scan past it does: `add` refuses before appending anything (its owners need a
    new base generation), and the staged append's range task refuses it too."""
    owned = DATES[BASE]
    world = _unowned_world(tmp_path / "scans", owned=(owned,))
    store = tmp_path / "store"
    _build(world, store, tmp_path / "work-build")
    gen = store / ist.PREFIX / "g"
    before = {p.relative_to(gen).as_posix(): p.read_bytes() for p in sorted(gen.rglob("*")) if p.is_file()}
    args = ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(tmp_path / "work-add"), "-r", str(RG), "-p", "2", "-M", "1GB"]
    res = CliRunner().invoke(il.add_cmd, args)
    msg = (f"{owned}: names owners (`usr`), but the generation has no owner axis (`scans.json` `\"slices\": false`); "
           "its owners need a new base generation (`interval-store build`)")
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, msg)
    assert {p.relative_to(gen).as_posix(): p.read_bytes() for p in sorted(gen.rglob("*")) if p.is_file()} == before
    ranges = json.loads((gen / "ranges.json").read_text())
    s = world["scans"][BASE]
    with pytest.raises(SystemExit) as e:
        for i, r in enumerate(ranges["ranges"]):
            name = f"r{i:04d}"
            ia.append_range(duckdb.connect(), ia.base_state_sql(str(gen), name, False), s, r, name, tmp_path / "run", bucket="b",
                            mount=str(world["root"]), sliced=False)
    assert str(e.value) == msg


def test_the_staged_stages_agree_on_an_unowned_generation(unowned, tmp_path):
    """The staged commands over the unowned base (its `scans.json` without the flag, its `ranges.json`): the slice stages
    (`build-ranges -S`, `fold -S`, the slice sorts' `cut`) build nothing, the rest the local build's files byte for byte,
    and the cut records `"slices": false` in `scans.json`, as the local build does."""
    gen = unowned["gen"]
    store = tmp_path / "store"
    prefix = store / ist.PREFIX / "g"
    prefix.mkdir(parents=True)
    scans = json.loads((gen / "scans.json").read_text())
    (prefix / "scans.json").write_text(json.dumps({k: v for k, v in scans.items() if k != "slices"}, indent=1) + "\n")
    shutil.copy(gen / "ranges.json", prefix / "ranges.json")
    k = json.loads((gen / "ranges.json").read_text())["k"]
    common = ["-b", str(store), "-g", "g", "-o", str(tmp_path / "out"), "-T", str(tmp_path / "tmp"), "-p", "2", "-M", "1GB"]
    every = ",".join(str(i) for i in range(k))
    stages = [
        ["build-ranges", *common, "-r", every],
        ["build-ranges", *common, "-r", every, "-S"],
        ["fold", *common, "-i", "0", "-n", str(k)],
        ["fold", *common, "-i", "0", "-n", str(k), "-S"],
        ["cut", *common, "-r", str(RG), *(x for s in ist.SORTS for x in ("-s", s))],
        ["cut", *common, "-i", str(len(ist.SORTS))],
    ]
    for args in stages:
        res = CliRunner().invoke(ist.cli, args, catch_exceptions=False)
        assert res.exit_code == 0, (args, res.output)
    assert json.loads((prefix / "scans.json").read_text()) == scans
    assert json.loads((prefix / "served" / "stats.json").read_text())["slices"] is False
    base = {key: v for key, v in _parquets(gen).items() if not key.startswith(("deltas/", "manifests/"))}
    assert _parquets(prefix) == base


def test_verify_reads_a_non_gcs_store(world, local, tmp_path):
    """`interval-store verify` over a dir store (as over `r2://`): the served sorts and the per-scan sorts and footers
    through `blobstore.Store`, no GCS client; every sampled view and diff of the base's scans (`scans.json`'s) equals the
    per-scan path store's. The fixture's per-scan sorts have no footer sidecars, so their read cost is `null`."""
    sampled = DATES[:BASE]
    res = CliRunner().invoke(ist.cli, ["verify", "-b", str(local["store"]), "-g", "g", "-s", ",".join(sampled), "-o", str(tmp_path / "out"),
                                       "-T", str(tmp_path / "tmp"), "-i", "0"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    summaries = json.loads(res.stdout.strip().splitlines()[-1])
    assert [s["date"] for s in summaries] == sampled
    assert [(s["views"] == s["view_eq"], s["diffs"] == s["diff_eq"]) for s in summaries] == [(True, True)] * len(sampled)
    assert sum(s["views"] for s in summaries) > 0
    recs = [json.loads(ln) for ln in (tmp_path / "out" / "verify" / f"t00-{sampled[0]}.jsonl").read_text().splitlines()]
    assert sorted({r["ps_cost"] is None for r in recs if r["kind"] == "view" and "ps_cost" in r}) == [True]
