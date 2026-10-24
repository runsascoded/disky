"""The Batch compaction stage (both stores' runners, `append_runner.Runner.compact_stage`) and its job
(`append_runner.run_compaction`, `r2_publish_gen`): the runners submit `compact` when the policy fires or `-K` forces
it (and `compact-r2` as the R2 account when the stages' account can't hold the R2 credentials), follow a compacted
generation's `successor.json` on their next run, and the job publishes GCS first, then R2 with the pointer last."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import interval_compact as ic
from dt_cloud import interval_store as ist
from dt_cloud import static_compact as sk
from dt_cloud import static_names as sn
from dt_cloud.blobstore import Store

from test_compact_level import _static_runner
from test_interval_append import COMMON, G1, Fake as IvFake
from test_interval_compact import DATES, LAYOUT, RG, add, build, make_world
from test_static_local import _build, _layout, _small_groups
from test_static_runner import GEN, ROOT, _py
from test_static_names import fixture, scan_ids  # noqa: F401


def _two_l0(f: IvFake) -> None:
    runs = [{"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d]} for d in ("2026-08-04", "2026-08-05")]
    f.keys[f"{G1}/manifests/2026-08-05.json"] = {"date": "2026-08-05", "runs": runs}


def test_interval_compact_now():
    """`-K`: the compaction stage runs with nothing due (`compact -K`); its reason is recorded."""
    f = IvFake(["2026-08-03"], compact_level=1)
    f.keys[f"{G1}/manifests/2026-08-04.json"] = {"date": "2026-08-04", "runs": [{"key": "deltas/2026-08-04", "first": "2026-08-04",
                                                                                 "last": "2026-08-04", "level": 0, "scans": ["2026-08-04"]}]}
    r = f.runner(merge_wait=None)
    r.compact_now = True
    r.carries()
    assert (f.calls(), r.compaction) == ([("compact", f"compact -C level:1 -K -r -j 1 -M 90GB -p 16 {COMMON} -m /gcs/data")], "forced (-K)")


def test_compact_jobs_run_long_on_standard_vms():
    """Both stores' `compact` job: STANDARD even on a spot profile (a reclaimed fold restarts from zero), no retries, and
    `LONG_RUN_S` rather than `MAX_RUN_S`; the interval store's folds `compact_jobs` sorts at a time (`-j`); `compact-r2` and
    every other stage keep the default."""
    r = IvFake(["2026-08-03"], compact_level=1, spot=True, compact_jobs=4).runner(merge_wait=None)

    def shape(spec):
        task = spec["taskGroups"][0]["taskSpec"]
        return (spec["labels"]["stage"], spec["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"], task["maxRetryCount"],
                task["maxRunDuration"])

    iv = r.compact_jobs("2026-08-04")
    sr = _static_runner(1).compact_jobs("2026-10-10")
    assert ([shape(s) for _, s in iv], [shape(s) for _, s in sr]) == (
        [("compact", "STANDARD", 0, f"{ar.LONG_RUN_S}s")], [("compact", "STANDARD", 0, f"{ar.LONG_RUN_S}s"), ("compact-r2", "SPOT", 3, f"{ar.MAX_RUN_S}s")])
    assert iv[0][1]["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1].split("dt_cloud.interval_append ")[1] == (
        f"compact -C level:1 -r -j 4 -M 90GB -p 16 {COMMON} -m /gcs/data")


def test_interval_follows_the_successor():
    """A generation compacted since the profile named it: the next run appends to its successor (its own ranges)."""
    f = IvFake(["2026-08-03", "2026-08-06"])
    _two_l0(f)
    f.keys[f"{G1}/successor.json"] = {"gen": "g1.c001"}
    f.keys["interval-store/g1.c001/ranges.json"] = {"k": 4}
    f.keys["interval-store/g1.c001/scans.json"] = {"scans": [{"id": d} for d in ("2026-08-03", "2026-08-04", "2026-08-05")]}
    r = f.runner(dry_run=True)
    r.run("2026-08-06")
    assert (r.p.gen, r.cfg.ranges_gen, [m for m in f.log if "compacted" in m]) == (
        "g1.c001", "g1.c001", ["g1 was compacted into g1.c001 (g1 → g1.c001): appending there"])


def test_static_compact_jobs_split_by_account():
    """The static profile's R2 account differs from the stages': `compact` (GCS only), then `compact-r2` as the R2
    account (no mount); the successor's generation is the runner's next. The drill fold runs a chunk per 6 vCPUs at once
    (`-j 2` on the 16-vCPU machine) unless the profile's `fold_jobs` says otherwise."""
    r = _static_runner(1)
    jobs = r.compact_jobs("2026-10-10")
    assert [(spec["labels"]["stage"], spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1],
             spec["allocationPolicy"]["serviceAccount"]["email"]) for _, spec in jobs] == [
        ("compact", _py("static_merge", "compact", "-g", GEN, "-S", "scr", "-C", "level:1", "-j", "2", "-M", "90GB", "-p", "16"), "build@proj"),
        ("compact-r2", _py("static_merge", "compact-r2", "-g", GEN, mount=False), "copy@proj"),
    ]
    r.cfg = replace(r.cfg, fold_jobs=5)
    assert r.compact_jobs("2026-10-10")[0][1]["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1] == _py(
        "static_merge", "compact", "-g", GEN, "-S", "scr", "-C", "level:1", "-j", "5", "-M", "90GB", "-p", "16")
    assert r.with_gen(f"{GEN}.c001").gen == f"{GEN}.c001"
    assert ROOT == f"static-names/{GEN}"


def _compact_params(command: str) -> dict:
    """The `static_merge compact` job command `command`'s arguments as `compact` itself parses them (`-b`: the profile's
    bucket, from the job's env)."""
    from dt_cloud.static_merge import compact_cmd

    args = command.split("dt_cloud.static_merge compact ", 1)[1].split()
    return compact_cmd.make_context("compact", ["-b", "data", *args]).params


def test_a_fanned_out_compaction_gets_its_task_count_and_the_profile():
    """`measure_tasks` 3: the compaction runs `-D 3` (not `-F`: `compact_options`' `--read-from`, which refused `3`),
    which `compact` parses as its measure tasks, with the profile inline in `$STATIC_NAMES_PROFILE` (its helper jobs are
    built from it, as the runner's are), which loads back as the runner's own."""
    from dt_cloud.static_profile import load_profile

    r = _static_runner(1)
    r.cfg = replace(r.cfg, measure_tasks=3)
    spec = r.compact_jobs("2026-10-10")[0][1]["taskGroups"][0]["taskSpec"]
    cmd = spec["runnables"][0]["container"]["commands"][1]
    assert cmd == _py("static_merge", "compact", "-g", GEN, "-S", "scr", "-C", "level:1", "-j", "2", "-D", "3", "-M", "90GB", "-p", "16")
    params = _compact_params(cmd)
    assert (params["measure_tasks"], params["anchor_tasks"], params["read_from"]) == (3, 1, "gcs")
    assert load_profile({"STATIC_NAMES_PROFILE": spec["environment"]["variables"]["STATIC_NAMES_PROFILE"]}) == r.cfg


def test_an_anchors_fanned_out_compaction_gets_its_task_count_and_the_profile():
    """`anchor_tasks` 4: the compaction runs `-A 4` (parsed by `compact` as its anchor tasks), with the profile inline
    in `$STATIC_NAMES_PROFILE`, which loads back as the runner's own; with `measure_tasks` too, both flags."""
    from dt_cloud.static_profile import load_profile

    r = _static_runner(1)
    r.cfg = replace(r.cfg, anchor_tasks=4)
    spec = r.compact_jobs("2026-10-10")[0][1]["taskGroups"][0]["taskSpec"]
    cmd = spec["runnables"][0]["container"]["commands"][1]
    assert cmd == _py("static_merge", "compact", "-g", GEN, "-S", "scr", "-C", "level:1", "-j", "2", "-A", "4", "-M", "90GB", "-p", "16")
    params = _compact_params(cmd)
    assert (params["measure_tasks"], params["anchor_tasks"]) == (1, 4)
    assert load_profile({"STATIC_NAMES_PROFILE": spec["environment"]["variables"]["STATIC_NAMES_PROFILE"]}) == r.cfg
    r.cfg = replace(r.cfg, measure_tasks=3)
    assert r.compact_jobs("2026-10-10")[0][1]["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1] == _py(
        "static_merge", "compact", "-g", GEN, "-S", "scr", "-C", "level:1", "-j", "2", "-D", "3", "-A", "4", "-M", "90GB", "-p", "16")


# ── The job: `run_compaction` over local stand-ins of the data bucket ──────


def test_run_compaction_interval(tmp_path):
    """The interval store's job over a store dir (its own mount): not due → nothing; due → the next generation,
    published with its pointer, a rerun idempotent (the successor isn't due); the predecessor kept for the grace."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    for d in DATES[2:4]:
        add(world, store_dir, tmp_path / "wa", "-t", d, "-C", "none")
    store = Store(str(store_dir))
    c = ic.compactor(RG)
    kw = dict(threads=2, mem="1GB", log=lambda m: None, now=0.0)
    assert ar.run_compaction(store, c, "g", store_dir, tmp_path / "w1", policy=ar.parse_policy("x", "every:3s"), **kw) == {
        "gen": "g", "why": None, "compact": None, "predecessors": []}
    doc = ar.run_compaction(store, c, "g", store_dir, tmp_path / "w2", policy=ar.parse_policy("x", "every:2s"), **kw)
    assert (doc["gen"], doc["why"], doc["compact"]["gen"], doc["compact"]["current"], doc["compact"]["reads"]["source"],
            [(p["gen"], p["deleted"]) for p in doc["predecessors"]]) == ("g", "every 2s: 2 scans past the base", "g.c001", True, f"{store_dir} (mount)",
                                                                         [("g", 0)])
    assert json.loads((store_dir / ist.PREFIX / "CURRENT.json").read_text()) == {"gen": "g.c001"}
    again = ar.run_compaction(store, c, "g", store_dir, tmp_path / "w3", policy=ar.parse_policy("x", "every:2s"), **kw)
    assert (again["gen"], again["compact"]) == ("g.c001", None)


def test_r2_publish_gen_order(monkeypatch):
    """R2: the new generation's files (in parallel), then its liveness files in order, then `compaction.json`, checked;
    then the old generation's `successor.json`; then the store's `CURRENT.json`, last."""
    class Obj:
        def __init__(self, key):
            self.key, self.size = key, 1

    keys = {"p/g.c001/": ["p/g.c001/a.parquet", "p/g.c001/compaction.json", "p/g.c001/scans.json", "p/g.c001/served/b.parquet"],
            "p/g/successor.json": ["p/g/successor.json"], "p/CURRENT.json": ["p/CURRENT.json"]}
    copied: list[list[str]] = []
    monkeypatch.setattr(ar, "r2_objects", lambda bucket, prefixes: [Obj(k) for p in prefixes for k in keys[p]])
    monkeypatch.setattr(ar, "r2_copy", lambda bucket, objs, **kw: copied.append([o.key for o in objs]) or {"copied": len(objs), "bytes": len(objs)})
    monkeypatch.setattr(ar, "r2_missing", lambda objs, **kw: [])
    doc = ar.r2_publish_gen("data", "p", "g.c001", "g", log=lambda m: None)
    assert copied == [["p/g.c001/a.parquet", "p/g.c001/served/b.parquet"], ["p/g.c001/scans.json"], ["p/g.c001/compaction.json"],
                      ["p/g/successor.json"], ["p/CURRENT.json"]]
    assert {k: v for k, v in doc.items() if k != "s"} == {"objects": 4, "copied": 4, "bytes": 4}


def test_run_compaction_static(fixture, tmp_path):  # noqa: F811
    """The static store's job (`static_merge compact`'s) over a store dir: forced (`-K`) with runs, its pointer moved,
    no predecessor pruned (`Compactor.prune` off: readers pin `STATIC_GEN`); the CLI reads the same."""
    from dt_cloud import static_local as sl


    root, scans, _ = fixture
    ids = scan_ids(scans)
    store_dir = tmp_path / "store"
    with _small_groups():
        _build(root, scans, store_dir, tmp_path / "wb", through=ids[2])
        res = CliRunner().invoke(sl.add_cmd, ["-g", "g", "-L", _layout(scans), "-s", str(root), "-o", str(store_dir), "-w", str(tmp_path / "wa"),
                                              "-p", "2", "-M", "1GB", "-t", ids[3]], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        doc = ar.run_compaction(Store(str(store_dir)), sk.compactor(), "g", store_dir, tmp_path / "w1", policy=ar.NEVER, force=True, threads=2,
                                mem="1GB", log=lambda m: None)
    assert (doc["why"], doc["compact"]["gen"], doc["compact"]["current"], doc["predecessors"]) == ("forced (-K)", "g.c001", True, [])
    assert json.loads((store_dir / sn.PREFIX / "CURRENT.json").read_text()) == {"gen": "g.c001"}
    assert sorted(p.name for p in (store_dir / sn.PREFIX).iterdir()) == ["CURRENT.json", "g", "g.c001"]


# ── An append landing during a compaction (the fold runs for hours) ─────────


def _racing(c: ar.Compactor, during):
    """`c` with its fold running `during()` after folding: what lands on the old generation meanwhile."""
    def fold(*a, **kw):
        out = c.fold(*a, **kw)
        during()
        return out
    return replace(c, fold=fold)


def _gens(store_dir: Path) -> list[str]:
    """The generations holding any file (a local store's deletes leave empty dirs; a bucket has none)."""
    return sorted(p.name for p in (store_dir / ist.PREFIX).iterdir() if p.is_dir() and any(f.is_file() for f in p.rglob("*")))


def test_compaction_superseded_by_an_append_during_the_fold(tmp_path):
    """A scan appended to the old generation while it was folded: nothing published (no successor, no pointer, the new
    generation deleted, no prune); the next compaction folds it too."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    for d in DATES[2:4]:
        add(world, store_dir, tmp_path / "wa", "-t", d, "-C", "none")
    store = Store(str(store_dir))
    ar.set_current(store, ist.PREFIX, "g")
    logs: list[str] = []
    kw = dict(threads=2, mem="1GB", log=logs.append, now=0.0, force=True, policy=ar.NEVER)
    race = _racing(ic.compactor(RG), lambda: add(world, store_dir, tmp_path / "wr", "-t", DATES[4], "-C", "none"))
    doc = ar.run_compaction(store, race, "g", store_dir, tmp_path / "w1", **kw)
    newest, runs = ar.store_newest_runs(store, f"{ist.PREFIX}/g")
    assert (doc["compact"]["superseded"], ar.run_scans(runs)[-1], doc["compact"]["scans"], doc["predecessors"]) == (newest, DATES[4], 4, [])
    assert (_gens(store_dir), (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists(), ar.current(store, ist.PREFIX)) == (["g"], False, "g")
    again = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w2", **kw)
    assert (again["compact"]["gen"], again["compact"]["scans"], "superseded" in again["compact"]) == ("g.c001", 5, False)
    assert json.loads((store_dir / ist.PREFIX / ar.CURRENT).read_text()) == {"gen": "g.c001"}


def test_compaction_rolls_back_when_an_append_lands_with_its_successor(tmp_path):
    """A scan appended between the compaction's check and its `successor.json`: the check after it finds the scan, so
    the successor record and the new generation are removed again."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    add(world, store_dir, tmp_path / "wa", "-t", DATES[2], "-C", "none")
    ar.set_current(Store(str(store_dir)), ist.PREFIX, "g")

    class Racing(Store):
        def write_text(self, key: str, text: str) -> None:
            if key.endswith(f"/g/{ar.SUCCESSOR}"):
                add(world, store_dir, tmp_path / "wr", "-t", DATES[3], "-C", "none")
            super().write_text(key, text)

    doc = ar.run_compaction(Racing(str(store_dir)), ic.compactor(RG), "g", store_dir, tmp_path / "w1",
                            threads=2, mem="1GB", log=lambda m: None, now=0.0, force=True, policy=ar.NEVER)
    newest, runs = ar.store_newest_runs(Store(str(store_dir)), f"{ist.PREFIX}/g")
    assert (doc["compact"]["superseded"], ar.run_scans(runs)[-1]) == (newest, DATES[3])
    assert (_gens(store_dir), (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists(), ar.current(Store(str(store_dir)), ist.PREFIX)) == (["g"], False, "g")


def test_publish_scan_never_into_a_compacted_generation(tmp_path):
    """An append whose generation has a successor is refused before its manifest; one whose successor appears while it
    publishes fails after (its rerun appends to the successor)."""
    new = {"key": "runs/s2", "scans": ["s2"]}
    doc = lambda prev, runs: {"runs": runs}  # noqa: E731
    store = ar.LocalRunStore(tmp_path / "g", tmp_path / "scratch", gen="g")
    store.create("manifests/s1.json", json.dumps({"runs": [{"key": "runs/s1", "scans": ["s1"], "level": 0}]}))
    store.create(ar.SUCCESSOR, json.dumps({"gen": "g.c001"}))
    with pytest.raises(SystemExit) as e:
        ar.publish_scan(store, "s2", new, doc, lambda runs: [])
    assert (str(e.value), store.exists("manifests/s2.json")) == (
        "not publishing manifests/s2.json: generation g was compacted (successor.json); the rerun appends to its successor", False)

    class Racing(ar.LocalRunStore):
        def create(self, key: str, text: str) -> None:
            super().create(key, text)
            super().create(ar.SUCCESSOR, json.dumps({"gen": "h.c001"}))

    racing = Racing(tmp_path / "h", tmp_path / "scratch", gen="h")
    with pytest.raises(SystemExit) as e:
        ar.publish_scan(racing, "s2", new, doc, lambda runs: [])
    assert str(e.value) == ("manifests/s2.json published while generation h was compacted (successor.json appeared): when the "
                            "compaction kept its successor, that lacks scan s2; the rerun appends it there")


def test_compact_now_skips_the_due_carries():
    """`-K` with a carry due: the compaction folds every run, so the merge stage submits no carry (gcs, 2026-10-22: an L3
    static carry's drill tier spilled 1.2 TiB and failed, then held the forced compaction behind its retries)."""
    f = IvFake(["2026-08-03"], compact_level=5)
    _two_l0(f)
    r = f.runner(merge_wait=None)
    r.compact_now = True
    r.carries()
    assert (f.calls(), [m for m in f.log if m.startswith("merge")]) == (
        [("compact", f"compact -C level:5 -K -r -j 1 -M 90GB -p 16 {COMMON} -m /gcs/data")],
        ["merge: compaction forced (-K): it folds every run, so the due carries are skipped (1 merge)"])


def test_the_fan_outs_helper_job_is_a_real_stage_job_named_by_the_compactions_scan():
    """`helper_job` through the static runner's own `job()` (not a fake submitter: c001's race failed there, its plan's
    key passed as the scan id): stage `measure`, named and labelled by the compaction's newest scan, `n` tasks of
    `measure-shard` over the plan, on the `measure` stage's machine, no mount."""
    import re

    from dt_cloud.append_runner import duckdb_args
    from dt_cloud.static_merge import helper_job

    r = _static_runner(1)
    name, spec = helper_job(r, "gs://scr/static-names/compact-checkpoints/g.c001/abc/fold", "measured/plan-2.json", 15, "c0de", "2026-10-23T0702")
    assert re.fullmatch(r"[a-z0-9-]+-measure-2026-10-23-0702-\d{6}", name), name
    v = spec["taskGroups"][0]["taskSpec"]["environment"]["variables"]
    assert {k: v.get(k) for k in ("DT_RECORD_SCAN", "DT_RECORD_GEN", "DT_JOB_NAME")} == {"DT_RECORD_SCAN": "2026-10-23T0702",
                                                                                    "DT_RECORD_GEN": f"static-names/{r.cfg.gen}", "DT_JOB_NAME": name}
    group = spec["taskGroups"][0]
    assert (spec["labels"]["stage"], group["taskCount"], group["taskSpec"]["runnables"][0]["container"]["commands"][1]) == (
        "measure", 15, _py("static_merge", "measure-shard", "-c", "gs://scr/static-names/compact-checkpoints/g.c001/abc/fold", "-P", "measured/plan-2.json",
                           "-V", "c0de", *duckdb_args(r.cfg.machine_for("measure")), mount=False))


def test_the_fold_fan_outs_helper_job_is_a_real_stage_job_named_by_the_compactions_scan():
    """`fold_helper_job` through the static runner's own `job()` (no fake submitter): stage `fold-shard`, named and
    labelled by the compaction's newest scan (never the plan's key), `n` tasks of `fold-shard` over the plan on the
    stage's machine and local SSD (the profile's `machines` / `ssd_gbs` entries), `fold_jobs` units at a time, no
    mount, spot with retries, recorded under the scan; the arguments parse back through `fold-shard`'s own command."""
    import re

    from dt_cloud.append_runner import duckdb_args
    from dt_cloud.static_merge import FOLD_STAGE, fold_helper_job, fold_shard_cmd

    r = _static_runner(1)
    r.cfg = replace(r.cfg, machines={**r.cfg.machines, FOLD_STAGE: "n2-highmem-32"}, ssd_gbs={**r.cfg.ssd_gbs, FOLD_STAGE: 1500})
    url = "gs://scr/static-names/compact-checkpoints/g.c001/abc/fold"
    name, spec = fold_helper_job(r, url, "folded/plan-2.json", 15, "c0de", "2026-10-23T0702")
    assert re.fullmatch(r"[a-z0-9-]+-fold-shard-2026-10-23-0702-\d{6}", name), name
    group = spec["taskGroups"][0]
    policy = spec["allocationPolicy"]["instances"][0]["policy"]
    cmd = group["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert (spec["labels"]["stage"], group["taskCount"], cmd, policy["machineType"], policy["provisioningModel"], policy["disks"][0]["newDisk"]["sizeGb"],
            group["taskSpec"]["maxRetryCount"]) == (
        FOLD_STAGE, 15, _py("static_merge", "fold-shard", "-c", url, "-P", "folded/plan-2.json", "-V", "c0de", "-j", "5", *duckdb_args("n2-highmem-32"),
                            mount=False), "n2-highmem-32", "SPOT", "1500", 3)
    v = group["taskSpec"]["environment"]["variables"]
    assert {k: v.get(k) for k in ("DT_RECORD_SCAN", "DT_RECORD_GEN", "DT_JOB_NAME")} == {"DT_RECORD_SCAN": "2026-10-23T0702",
                                                                                    "DT_RECORD_GEN": f"static-names/{r.cfg.gen}", "DT_JOB_NAME": name}
    args = cmd.split("dt_cloud.static_merge fold-shard ", 1)[1].split()
    params = fold_shard_cmd.make_context("fold-shard", args).params
    assert params == {"checkpoint": url, "index": None, "jobs": 5, "mem": "180GB", "threads": 32, "plan": "folded/plan-2.json", "tmp": "/stage/tmp/fold-shard",
                      "code": "c0de"}


def test_a_fold_fanned_out_compaction_gets_its_task_count_and_the_profile():
    """`fold_tasks` 3: the compaction runs `-L 3`, which `compact` parses as its fold tasks (no collision with its other
    options or `compact_options`'), with the profile inline in `$STATIC_NAMES_PROFILE`, which loads back as the
    runner's own; with every fan-out on, all three flags."""
    from dt_cloud.static_profile import load_profile

    r = _static_runner(1)
    r.cfg = replace(r.cfg, fold_tasks=3)
    spec = r.compact_jobs("2026-10-10")[0][1]["taskGroups"][0]["taskSpec"]
    cmd = spec["runnables"][0]["container"]["commands"][1]
    assert cmd == _py("static_merge", "compact", "-g", GEN, "-S", "scr", "-C", "level:1", "-j", "2", "-L", "3", "-M", "90GB", "-p", "16")
    params = _compact_params(cmd)
    assert (params["fold_tasks"], params["measure_tasks"], params["anchor_tasks"], params["jobs"]) == (3, 1, 1, 2)
    assert load_profile({"STATIC_NAMES_PROFILE": spec["environment"]["variables"]["STATIC_NAMES_PROFILE"]}) == r.cfg
    assert load_profile({"STATIC_NAMES_PROFILE": "", "STATIC_NAMES_FOLD_TASKS": "4"}).fold_tasks == 4
    r.cfg = replace(r.cfg, measure_tasks=2, anchor_tasks=4)
    cmd = r.compact_jobs("2026-10-10")[0][1]["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert cmd == _py("static_merge", "compact", "-g", GEN, "-S", "scr", "-C", "level:1", "-j", "2", "-D", "2", "-A", "4", "-L", "3", "-M", "90GB", "-p", "16")
    params = _compact_params(cmd)
    assert (params["fold_tasks"], params["measure_tasks"], params["anchor_tasks"]) == (3, 2, 4)


def test_the_anchors_fan_outs_helper_job_is_a_real_stage_job_named_by_the_compactions_scan():
    """`anchor_helper_job` through the static runner's own `job()` (no fake submitter): stage `anchor-shard`, named and
    labelled by the compaction's newest scan (never the plan's key), `n` tasks of `anchor-shard` over the plan, on the
    `anchor-shard` stage's machine (the profile's `machines` entry when set), no mount, spot with retries."""
    import re

    from dt_cloud.append_runner import duckdb_args
    from dt_cloud.static_merge import ANCHOR_STAGE, anchor_helper_job

    r = _static_runner(1)
    r.cfg = replace(r.cfg, machines={**r.cfg.machines, ANCHOR_STAGE: "n2-highmem-32"})
    url = "gs://scr/static-names/compact-checkpoints/g.c001/abc"
    name, spec = anchor_helper_job(r, url, "anchored/plan-1.json", 7, "c0de", "2026-10-23T0702")
    assert re.fullmatch(r"[a-z0-9-]+-anchor-shard-2026-10-23-0702-\d{6}", name), name
    group = spec["taskGroups"][0]
    assert (spec["labels"]["stage"], group["taskCount"], group["taskSpec"]["runnables"][0]["container"]["commands"][1],
            spec["allocationPolicy"]["instances"][0]["policy"]["machineType"], spec["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"],
            group["taskSpec"]["maxRetryCount"]) == (
        ANCHOR_STAGE, 7, _py("static_merge", "anchor-shard", "-c", url, "-P", "anchored/plan-1.json", "-V", "c0de", *duckdb_args("n2-highmem-32"),
                             mount=False), "n2-highmem-32", "SPOT", 3)
    assert duckdb_args("n2-highmem-32") == ["-M", "180GB", "-p", "32"]
