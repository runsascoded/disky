"""`dt_cloud.static_runner`: the per-scan append chain's order (strictly by scan id, catching up on request), its stage
skipping (a rerun resumes at the first missing output), and the Batch jobs it submits."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from threading import Barrier, Lock

import pytest

from dt_cloud import static_runner as sd
from dt_cloud.static_merge import manifest_keys, manifest_name, parse_manifest, plan_carries, rebase
from dt_cloud.static_profile import Profile, load_profile, parse_compact_level
from dt_cloud.static_profile_examples import CW

GEN = "2026-10-09cw"
ROOT = f"static-names/{GEN}"
CFG = Profile(name="test", layouts=("x/{id}/p",), gen=GEN, project="proj", region="us-east1", image="img@sha256:0", sa="build@proj",
              r2_bucket="idx", bucket="data", scratch="scr", r2_sa="copy@proj",
              r2_secrets={"endpoint": "s-end", "key_id": "s-id", "secret": "s-key"}, append_tasks=4, hex_runs="off")
NOW = datetime(2026, 10, 9, 12, 34, 56, tzinfo=timezone.utc)


@pytest.mark.parametrize("have, published, scan, catch_up, want", [
    (["2026-10-08"], ["2026-10-09T1236"], "2026-10-09T1236", False, ["2026-10-09T1236"]),
    # gcs's real order: a sub-daily scan, then the cron's bare-date scan of the next day
    (["2026-10-09"], ["2026-10-09T1236", "2026-10-10"], "2026-10-10", True, ["2026-10-09T1236", "2026-10-10"]),
    (["2026-10-09"], ["2026-10-09T1236", "2026-10-10"], "2026-10-09T1236", False, ["2026-10-09T1236"]),
    # two scans on one day, then a third that is published but not asked for
    (["2026-10-08T1801"], ["2026-10-09T0001", "2026-10-09T0601", "2026-10-09T1202"], "2026-10-09T0601", True, ["2026-10-09T0001", "2026-10-09T0601"]),
    (["2026-10-08", "2026-10-09T0001"], ["2026-10-09T0601"], "2026-10-09T0001", False, []),
])
def test_pending_scans(have, published, scan, catch_up, want):
    assert sd.pending_scans(have, published, scan, catch_up) == want


@pytest.mark.parametrize("have, published, scan, msg", [
    (["2026-10-09"], ["2026-10-09T1236", "2026-10-10"], "2026-10-10",
     "1 earlier published scan(s) pending: 2026-10-09T1236 (append them first, or -c)"),
    (["2026-10-09"], ["2026-10-09T1236"], "2026-10-10", "2026-10-10 is not published (no path sort under the generation's layouts)"),
    (["2026-10-08", "2026-10-09T1236"], [], "2026-10-09T0600",
     "2026-10-09T0600 precedes the generation's newest scan 2026-10-09T1236 but was never appended: it can't join now"),
])
def test_pending_scans_refuses(have, published, scan, msg):
    with pytest.raises(sd.NotNext) as e:
        sd.pending_scans(have, published, scan, False)
    assert str(e.value) == msg


#: The stages submitted at once (`Runner.concurrently`), in the order `_chain` lists them.
CONCURRENT = ({"shards": 0, "catalog": 1}, {"drill": 0, "anchors": 1})


def canon(calls: list[tuple]) -> list[tuple]:
    """`calls` with each run of adjacent stages from one concurrent pair in `_chain`'s order (their recorded order is a
    race): a pair's jobs still sit between the stage before it and the one after."""
    out = []
    for c in calls:
        group = next((g for g in CONCURRENT if c[0] in g), None)
        j = len(out)
        while group and j and out[j - 1][0] in group and group[out[j - 1][0]] > group[c[0]]:
            j -= 1
        out.insert(j, c)
    return out


class Fake:
    """The data bucket as a dict of keys → JSON (or None), Batch as a recorder whose jobs write their stage's outputs.
    `calls` lists them, each concurrent pair in `_chain`'s order (`canon`). `fail`: stages whose job fails (Batch FAILED,
    nothing written); `silent`: stages whose job succeeds but writes nothing; `barrier`: stages whose job waits for all
    of them to be in flight at once (else `BrokenBarrierError`)."""

    def __init__(self, keys: dict, published: list[str], k: int = 8, fail: tuple = (), silent: tuple = (), barrier: tuple = ()):
        self.keys = {f"{ROOT}/scans.json": {"scans": [{"id": "2026-10-08T1801"}]}, f"{ROOT}/ranges.json": {"k": k}, **keys}
        self.pub, self._calls, self.lock = published, [], Lock()
        self.fail, self.silent = fail, silent
        self.barrier = (barrier, Barrier(len(barrier), timeout=5)) if barrier else ((), None)

    @property
    def calls(self) -> list[tuple]:
        return canon(self._calls)

    @calls.setter
    def calls(self, v: list[tuple]) -> None:
        self._calls = v

    def newest(self) -> str | None:
        ms = manifest_keys([k.removeprefix(f"{ROOT}/") for k in self.keys if k.startswith(f"{ROOT}/manifests/")])
        return f"{ROOT}/{ms[-1]}" if ms else None

    def runs(self, d: str) -> list[dict]:
        """`publish`'s runs: the newest earlier manifest's, then `d`'s at level 0."""
        ms = [k for k in manifest_keys([k.removeprefix(f"{ROOT}/") for k in self.keys if k.startswith(f"{ROOT}/manifests/")])
              if parse_manifest(k.removeprefix("manifests/"))[0] < d]
        prev = self.keys[f"{ROOT}/{ms[-1]}"]["runs"] if ms else []
        return [*prev, *_runs(d)]

    def publish(self, d: str) -> None:
        self._calls.append(("publish", d))
        self.keys[f"{ROOT}/manifests/{d}.json"] = {"date": d, "runs": self.runs(d)}

    def carry(self, level: int | None) -> None:
        """What `static_merge carry -L <level>` does: each due carry's run, then a revision of the newest manifest listing it."""
        while True:
            key = self.newest()
            m = self.keys[key]
            drilled = {r["key"] for r in m["runs"] if f"{ROOT}/{r['key']}/drill/meta.json" in self.keys}
            _, merges = plan_carries(m["runs"], drilled, level)
            if not merges:
                return
            ins, out = merges[0]
            self.keys[f"{ROOT}/{out['key']}/meta.json"] = None
            if all(r["key"] in drilled for r in ins):
                self.keys[f"{ROOT}/{out['key']}/drill/meta.json"] = None
            for t in ("anchors", "anchors/start"):  # `run_tiers`: names + anchors when every input carries them
                if all(f"{ROOT}/{r['key']}/{t}/meta.json" in self.keys for r in ins):
                    self.keys[f"{ROOT}/{out['key']}/{t}/meta.json"] = None
            scan, rev = parse_manifest(key.rsplit("/", 1)[-1])
            self.keys[f"{ROOT}/manifests/{manifest_name(scan, rev + 1)}"] = {**m, "runs": rebase(m["runs"], [r["key"] for r in ins], out)}

    def run_job(self, name: str, spec: dict, wait: float | None = None) -> None:
        stage = spec["labels"]["stage"]
        call = (stage, spec["taskGroups"][0]["taskCount"], spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1])
        with self.lock:
            self._calls.append((*call, wait) if stage == "merge" else call)
        if stage in self.barrier[0]:
            self.barrier[1].wait()
        if stage in self.fail:
            raise RuntimeError(f"Batch job {name}: FAILED")
        if stage in self.silent:
            return
        words = call[2].split()
        d = words[words.index("-d") + 1] if "-d" in words else None
        if stage in ("drill-merge", "anchors-merge"):
            # `static_merge tier`: the merged run's tier, from its scans' level-0 runs' (each must have it)
            key, tier = words[words.index("-r") + 1], words[words.index("-t") + 1]
            ms = manifest_keys([k.removeprefix(f"{ROOT}/") for k in self.keys if k.startswith(f"{ROOT}/manifests/")])
            run = next(r for m in reversed(ms) for r in self.keys[f"{ROOT}/{m}"]["runs"] if r["key"] == key)
            assert all(f"{ROOT}/deltas/{x}/{tier}/meta.json" in self.keys for x in run["scans"]), (key, tier)
            self.keys[f"{ROOT}/{key}/{tier}/meta.json"] = None
            if tier == "anchors" and all(f"{ROOT}/deltas/{x}/anchors/start/meta.json" in self.keys for x in run["scans"]):
                self.keys[f"{ROOT}/{key}/anchors/start/meta.json"] = None
            return
        out = {"append": [f"{ROOT}/deltas/{d}/dhist/r{i:04d}.parquet" for i in range(8)], "shards": [f"{ROOT}/deltas/{d}/sidecar.parquet"],
               "catalog": [f"{ROOT}/deltas/{d}/catalog/meta.json"], "drill": [f"{ROOT}/deltas/{d}/drill/meta.json"],
               "anchors": [f"{ROOT}/deltas/{d}/anchors/meta.json", *([f"{ROOT}/deltas/{d}/anchors/start/meta.json"] if f"{ROOT}/anchors/start/meta.json" in self.keys else [])]}.get(stage, [])
        for key in out:
            self.keys[key] = None
        if stage == "merge":
            # The job does its work whatever the wait; a wait of 0 (`runs add`'s default) stops watching it at once.
            self.carry(parse_compact_level("-L", words[words.index("-L") + 1]))
            if wait == 0:
                raise sd.StillRunning(f"Batch job {name}: still running after 0s")

    def daily(self, cfg: Profile = CFG, **kw) -> sd.Runner:
        return sd.Runner(
            cfg=cfg, exists=lambda key: key in self.keys, count=lambda p, s: sum(1 for x in self.keys if x.startswith(p) and x.endswith(s)),
            read_json=lambda key: self.keys[key], published=lambda layouts, start: [s for s in self.pub if s > start],
            run_job=self.run_job, prepare=lambda d: self._calls.append(("prepare", d)) or self.keys.__setitem__(f"{ROOT}/deltas/{d}/scans.json", None),
            prune=lambda d: self._calls.append(("prune", d)), publish=self.publish, list_keys=lambda p: [x for x in self.keys if x.startswith(p)],
            now=lambda: NOW, **{"log": lambda m: None, **kw})


def _py(module: str, *args: str, mount: bool = True) -> str:
    m = " -m /gcs/data" if mount else ""
    diagnostics = ""
    if module == "static_drill" and args[0] == "build":
        diagnostics = """trap 'rc=$?; printf "DRILL_EXIT code=%s\\n" "$rc"; for f in /sys/fs/cgroup/memory.events /sys/fs/cgroup/memory.events.local /sys/fs/cgroup/memory/memory.failcnt /sys/fs/cgroup/memory/memory.oom_control; do if [ -r "$f" ]; then printf "DRILL_CGROUP %s\\n" "$f"; cat "$f"; fi; done' EXIT; """
    elif module == "static_anchors" and args[0] == "run":
        diagnostics = """trap 'rc=$?; printf "ANCHOR_EXIT code=%s\\n" "$rc"; for f in /sys/fs/cgroup/memory.events /sys/fs/cgroup/memory.events.local /sys/fs/cgroup/memory/memory.failcnt /sys/fs/cgroup/memory/memory.oom_control; do if [ -r "$f" ]; then printf "ANCHOR_CGROUP %s\\n" "$f"; cat "$f"; fi; done' EXIT; """
    return f"set -euo pipefail; {diagnostics}mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.{module} {' '.join(args)}{m}"


def _runs(*ids: str) -> list[dict]:
    """Level-0 runs of scans `ids`, as `publish` lists them."""
    return [{"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d]} for d in ids]


def _had(d: str, *tiers: str) -> dict:
    """Run `deltas/<d>`'s `tiers` (`drill`, `anchors`, `anchors/start`) built, and with `anchors`, the base's anchors."""
    return {**{f"{ROOT}/deltas/{d}/{t}/meta.json": None for t in tiers}, **({f"{ROOT}/anchors/meta.json": None} if "anchors" in tiers else {})}


def _r2(d: str, runs: list[str], manifest: str | None = None) -> tuple:
    """The R2 job: each run's served files but its `drill/meta.json` and `anchors/meta.json`, then those; a check of them all;
    the manifest (`d`'s, or a revision) last, alone; then the runs a carry folded, off R2."""
    cmds = []
    for r in runs:
        cmds.append(_py("static_names", "r2-copy", "-g", f"{GEN}/deltas/{r}", "-x", "drill/meta.json", "-x", "anchors/meta.json",
                        "-x", "anchors/start/meta.json", mount=False))
        cmds.append(_py("static_names", "r2-copy", "-g", f"{GEN}/deltas/{r}", "-o", "drill/meta.json", mount=False))
        cmds.append(_py("static_names", "r2-copy", "-g", f"{GEN}/deltas/{r}", "-o", "anchors/start/meta.json", mount=False))
        cmds.append(_py("static_names", "r2-copy", "-g", f"{GEN}/deltas/{r}", "-o", "anchors/meta.json", mount=False))
    cmds.append(_py("static_names", "r2-verify", "-g", GEN, "-m", manifest or d, mount=False))
    cmds.append(_py("static_names", "r2-copy", "-g", GEN, "-o", f"manifests/{manifest or d}.json", mount=False))
    cmds.append(_py("static_merge", "prune", "-g", GEN, "-r", mount=False))
    return ("r2", 1, " && ".join(f"( {c} )" for c in cmds))


def _merge(wait: float | None = 0) -> tuple:
    """The merge stage's job: one task, `static_merge carry` on the mount, watched for `wait` s."""
    return ("merge", 1, _py("static_merge", "carry", "-g", GEN, "-L", "5"), wait)


def _chain(d: str, runs: list[str], drill: bool = False, anchors: bool = False) -> list[tuple]:
    g = f"-g {GEN} -d {d}"
    return [
        ("prepare", d),
        ("append", 4, _py("static_append", "append", g, "-n", "2")),
        ("shards", 1, _py("static_append", "shards", g)),
        ("catalog", 1, _py("static_append", "catalog", g)),
        *([("drill", 2, _py("static_drill", "build", g, "-k", "task", "-M", "60GB", "-p", "16"))] if drill else []),
        *([("anchors", 1, _py("static_anchors", "run", g, "-M", "90GB", "-p", "16"))] if anchors else []),
        ("publish", d),
        _r2(d, runs),
        ("prune", d),
    ]


def test_one_scan_runs_every_stage_in_order():
    f = Fake({}, ["2026-10-09T0001"])
    assert f.daily().run("2026-10-09T0001") == ["2026-10-09T0001"]
    assert f.calls == _chain("2026-10-09T0001", ["2026-10-09T0001"])


def test_catch_up_appends_each_pending_scan_in_order():
    """Two scans of one day are two runs: each its own chain, the second's manifest listing both."""
    f = Fake({}, ["2026-10-09T0001", "2026-10-09T0601"])
    with pytest.raises(sd.NotNext):
        f.daily().run("2026-10-09T0601")
    assert f.calls == []
    assert f.daily().run("2026-10-09T0601", catch_up=True) == ["2026-10-09T0001", "2026-10-09T0601"]
    assert f.calls == [*_chain("2026-10-09T0001", ["2026-10-09T0001"]), *_chain("2026-10-09T0601", ["2026-10-09T0001", "2026-10-09T0601"]),
                       _merge()]
    # the merge job (not waited on) went on to publish its revision: the two runs as one
    assert f.keys[f"{ROOT}/manifests/2026-10-09T0601.m001.json"]["runs"] == [
        {"key": "deltas/2026-10-09T0001_2026-10-09T0601", "first": "2026-10-09T0001", "last": "2026-10-09T0601", "level": 1,
         "scans": ["2026-10-09T0001", "2026-10-09T0601"]}]


def test_a_rerun_resumes_at_the_first_missing_output():
    d = "2026-10-09T0001"
    done = {f"{ROOT}/deltas/{d}/scans.json": None, **{f"{ROOT}/deltas/{d}/dhist/r{i:04d}.parquet": None for i in range(8)},
            f"{ROOT}/deltas/{d}/catalog/meta.json": None}
    f = Fake(done, [d])
    f.daily().run(d)
    chain = _chain(d, [d])
    assert f.calls == [chain[2], *chain[4:]]


def test_an_appended_scan_reruns_only_the_r2_copy_and_prune():
    d = "2026-10-09T0001"
    f = Fake({f"{ROOT}/manifests/{d}.json": {"date": d, "runs": _runs(d)}}, [d])
    assert f.daily().run(d) == []
    assert f.calls == _chain(d, [d])[5:]


DRILL = Profile(**{**CFG.__dict__, "drill": True})


def test_the_drill_stage_runs_after_shards_and_catalog_before_publish():
    """With the profile's `drill`: one job of two tasks (long ∥ short, the machine's memory and threads) once the run's
    shards and catalog are there, and publish only after it (its `meta.json` is what publish checks)."""
    d = "2026-10-09T0001"
    f = Fake({}, [d])
    assert f.daily(DRILL).run(d) == [d]
    assert f.calls == _chain(d, [d], drill=True)


def test_a_rerun_skips_a_built_drill():
    d = "2026-10-09T0001"
    done = {f"{ROOT}/deltas/{d}/scans.json": None, **{f"{ROOT}/deltas/{d}/dhist/r{i:04d}.parquet": None for i in range(8)},
            f"{ROOT}/deltas/{d}/sidecar.parquet": None, f"{ROOT}/deltas/{d}/catalog/meta.json": None, f"{ROOT}/deltas/{d}/drill/meta.json": None}
    f = Fake(done, [d])
    f.daily(DRILL).run(d)
    assert f.calls == _chain(d, [d], drill=True)[5:]


def test_an_appended_scan_without_its_drill_gets_it_then_the_r2_copy():
    """gcs 2026-10-09T1236: appended (its manifest lists `deltas/<id>` itself) before the drill stage existed: a rerun builds
    its drill, then copies it (its `meta.json` last) and prunes."""
    d = "2026-10-09T1236"
    runs = _runs("2026-10-09", d)
    f = Fake({f"{ROOT}/manifests/{d}.json": {"date": d, "runs": runs}, **_had("2026-10-09", "drill")}, [d])
    assert f.daily(DRILL).run(d) == []
    # both runs drilled now: the carry is due
    assert f.calls == [_chain(d, [], drill=True)[4], _r2(d, ["2026-10-09", d]), ("prune", d), _merge()]


def test_dry_run_submits_nothing():
    f = Fake({}, ["2026-10-09T0001"])
    f.daily(dry_run=True).run("2026-10-09T0001")
    assert f.calls == []


def test_r2_job_spec():
    """The R2 copy: as the copy account, its credentials from Secret Manager, no scratch mount, chained copies."""
    cmds = [_py("static_names", "r2-copy", "-g", GEN, mount=False)]
    spec = sd.job_spec(CFG, "sn-r2-x", 1, cmds, stage="r2", scratch=False, r2=True, machine="n2-highmem-4", ssd_gb=375)
    assert spec == {
        "taskGroups": [{
            "taskCount": 1, "parallelism": 1,
            "taskSpec": {
                "runnables": [{"container": {"imageUri": "img@sha256:0", "entrypoint": "bash", "commands": ["-c", cmds[0]],
                                             "volumes": ["/mnt/disks/gcs/data:/gcs/data:ro", "/mnt/disks/stage:/stage:rw"]}}],
                "environment": {
                    "variables": {"STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr", "R2_BUCKET": "idx"},
                    "secretVariables": {"R2_ACCESS_KEY_ID": "projects/proj/secrets/s-id/versions/latest",
                                        "R2_ENDPOINT": "projects/proj/secrets/s-end/versions/latest",
                                        "R2_SECRET_ACCESS_KEY": "projects/proj/secrets/s-key/versions/latest"},
                },
                "computeResource": {"cpuMilli": 4000, "memoryMib": 30800},
                "maxRetryCount": 3, "maxRunDuration": "14400s",
                "volumes": [{"gcs": {"remotePath": "data"}, "mountPath": "/mnt/disks/gcs/data", "mountOptions": ["--implicit-dirs"]},
                            {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
            },
        }],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": "n2-highmem-4", "provisioningModel": "SPOT", "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
                                      "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": "375"}, "deviceName": "stage"}]}}],
            "serviceAccount": {"email": "copy@proj"},
            "location": {"allowedLocations": ["regions/us-east1"]},
        },
        "labels": {"purpose": "static-names", "stage": "r2", "gen": "2026-10-09cw"},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def test_task_command_from_mounted_source():
    cfg = Profile(**{**CFG.__dict__, "src": ("/gcs/data/static-names/src/t/dt_cloud", "/gcs/data/static-names/src/pyrmts-r/pyrmts")})
    assert sd.task_command(cfg, "static_append", ["shards", "-d", "2026-10-09T0001"]) == (
        "set -euo pipefail; mkdir -p /stage/tmp /stage/out /stage/src && cp -r /gcs/data/static-names/src/t/dt_cloud "
        "/gcs/data/static-names/src/pyrmts-r/pyrmts /stage/src/ && cd /stage && PYTHONPATH=/stage/src python3 -u -m dt_cloud.static_append "
        "shards -d 2026-10-09T0001 -m /gcs/data")


def test_job_ids_are_batch_safe():
    assert [sd.job_id("append", s, NOW) for s in ("2026-10-09T1236", "2026-10-10")] == ["sn-append-2026-10-09-1236-123456", "sn-append-2026-10-10-123456"]


def test_profile_is_an_example_with_env_over_it():
    """cw's example (no generation of its own: by env), with a staged source tree and on-demand VMs by env; every
    other field the example's."""
    env = {"STATIC_NAMES_PROFILE": "cw", "STATIC_NAMES_GEN": "2026-10-10cw", "STATIC_NAMES_SPOT": "0", "STATIC_NAMES_SRC": "/gcs/a/x,/gcs/a/y", "R2_ENDPOINT": "https://e",
           "STATIC_NAMES_IMAGE": "registry/job@sha256:1"}
    p = load_profile(env)
    assert p == Profile(**{**CW.__dict__, "gen": "2026-10-10cw", "spot": False, "src": ("/gcs/a/x", "/gcs/a/y"), "r2_endpoint": "https://e", "image": "registry/job@sha256:1"})
    assert sd.ready(p) == p
    # The examples pin no image: the caller passes the one it runs.
    with pytest.raises(SystemExit) as e:
        sd.ready(load_profile({k: v for k, v in env.items() if k != "STATIC_NAMES_IMAGE"}))
    assert str(e.value) == "static names: no image in the deployment profile: set STATIC_NAMES_IMAGE (or STATIC_NAMES_PROFILE)"
    assert p.r2_env_secrets() == {"R2_ENDPOINT": "cw-s3-r2-endpoint", "R2_ACCESS_KEY_ID": "cw-s3-r2-access-key-id",
                                  "R2_SECRET_ACCESS_KEY": "cw-s3-r2-secret-access-key"}


def test_profile_by_env_alone_and_missing_fields(tmp_path):
    """No profile selected: nothing is assumed, a missing field is an error naming its variable; a JSON file works too."""
    with pytest.raises(SystemExit) as e:
        sd.ready(load_profile({"GCP_PROJECT": "p"}))
    assert str(e.value) == "static names: no gen in the deployment profile: set STATIC_NAMES_GEN (or STATIC_NAMES_PROFILE)"
    f = tmp_path / "mine.json"
    f.write_text('{"layouts": ["s/{id}/p.parquet"], "bucket": "b", "scratch": "s", "gen": "g", "region": "r", "image": "i", "sa": "a",'
                 ' "r2_bucket": "rb", "r2_secrets": {"key_id": "k", "secret": "x"}}')
    with pytest.raises(SystemExit) as e:
        sd.ready(load_profile({"STATIC_NAMES_PROFILE": str(f), "GCP_PROJECT": "p", "R2_ENDPOINT": "https://e"}))
    assert str(e.value) == "static names: no hex_runs in the deployment profile: set STATIC_NAMES_HEX_RUNS (or STATIC_NAMES_PROFILE)"
    with pytest.raises(SystemExit) as e:
        sd.ready(load_profile({"STATIC_NAMES_PROFILE": str(f), "GCP_PROJECT": "p", "R2_ENDPOINT": "https://e", "STATIC_NAMES_HEX_RUNS": "16"}))
    assert str(e.value) == "static names: STATIC_NAMES_HEX_RUNS: hex runs: '16' is neither MIN,TAIL (e.g. 16,8) nor off"
    f.write_text('{"layouts": ["s/{id}/p.parquet"], "bucket": "b", "scratch": "s", "gen": "g", "region": "r", "image": "i", "sa": "a",'
                 ' "r2_bucket": "rb", "r2_secrets": {"key_id": "k", "secret": "x"}, "hex_runs": "16,8"}')
    with pytest.raises(SystemExit) as e:
        sd.ready(load_profile({"STATIC_NAMES_PROFILE": str(f), "GCP_PROJECT": "p"}))
    assert str(e.value) == "static names: the R2 endpoint: set R2_ENDPOINT, or name its secret in STATIC_NAMES_R2_SECRETS (endpoint=…)"
    p = sd.ready(load_profile({"STATIC_NAMES_PROFILE": str(f), "GCP_PROJECT": "p", "R2_ENDPOINT": "https://e"}))
    assert (p.name, p.layouts, p.project, p.spot) == ("mine.json", ("s/{id}/p.parquet",), "p", True)
    with pytest.raises(SystemExit) as e:
        load_profile({"STATIC_NAMES_R2_SECRETS": "key_id=k,oops"})
    assert str(e.value) == "STATIC_NAMES_R2_SECRETS: 'oops' is not endpoint=|key_id=|secret=<secret name>"


def test_runs_follow_the_base_rule_and_log_a_differing_profile():
    """A base built without the hex-run rule keeps it for its runs (the stages read the base's `scans.json`); a profile
    that asks for the rule is logged once per `run`, and the chain is unchanged. A base that records the profile's rule
    logs nothing."""
    logs: list[str] = []
    f = Fake({}, ["2026-10-09T0001"])
    f.daily(Profile(**{**CFG.__dict__, "hex_runs": "16,8"}), log=logs.append).run("2026-10-09T0001")
    assert [m for m in logs if "hex_runs" in m] == [
        f"{GEN}: built with hex_runs off, the profile says {{'min': 16, 'tail': 8}}: its runs keep the generation's rule"
        " (the profile's applies to the next generation)"]
    assert f.calls == _chain("2026-10-09T0001", ["2026-10-09T0001"])
    logs.clear()
    g = Fake({f"{ROOT}/scans.json": {"scans": [{"id": "2026-10-08T1801"}], "hex_runs": {"min": 16, "tail": 8}}}, ["2026-10-09T0001"])
    g.daily(Profile(**{**CFG.__dict__, "hex_runs": "16,8"}), log=logs.append).run("2026-10-09T0001")
    assert [m for m in logs if "hex_runs" in m] == []


ANCHORS = Profile(**{**DRILL.__dict__, "anchors": True})


def test_the_anchors_stage_runs_beside_the_drill_and_its_meta_is_copied_last():
    d = "2026-10-09T0001"
    f = Fake({}, [d])
    f.daily(ANCHORS).run(d)
    assert f.calls == _chain(d, [d], drill=True, anchors=True)


def test_a_run_without_the_starts_with_catalog_gets_its_anchors_stage_again():
    """With the base's `anchors/start/` there, a run whose `anchors/meta.json` is there but not its `anchors/start/meta.json`
    reruns the stage (`anchors run` builds what's missing); once both are there it's done."""
    d = "2026-10-09T1236"
    runs = _runs("2026-10-09", d)
    keys = {f"{ROOT}/manifests/{d}.json": {"date": d, "runs": runs}, f"{ROOT}/deltas/{d}/drill/meta.json": None, f"{ROOT}/deltas/{d}/anchors/meta.json": None,
            f"{ROOT}/anchors/start/meta.json": None, **_had("2026-10-09", "drill", "anchors", "anchors/start")}
    f = Fake(keys, [d])
    assert f.daily(ANCHORS).run(d) == []
    assert f.calls == [_chain(d, [], drill=True, anchors=True)[5], _r2(d, ["2026-10-09", d]), ("prune", d), _merge()]
    assert f"{ROOT}/deltas/{d}/anchors/start/meta.json" in f.keys
    f.calls = []
    f.daily(ANCHORS, merge=False).run(d)
    # the newest manifest's copy: the merge's revision (`d`'s own lists runs the revision folded, which a prune may delete)
    assert f.calls == [_r2(d, [f"2026-10-09_{d}"], f"{d}.m001"), ("prune", d)]


def test_an_appended_scan_without_its_anchors_gets_them_then_the_r2_copy():
    d = "2026-10-09T1236"
    runs = _runs("2026-10-09", d)
    f = Fake({f"{ROOT}/manifests/{d}.json": {"date": d, "runs": runs}, f"{ROOT}/deltas/{d}/drill/meta.json": None, **_had("2026-10-09", "drill", "anchors")}, [d])
    assert f.daily(ANCHORS).run(d) == []
    assert f.calls == [_chain(d, [], drill=True, anchors=True)[5], _r2(d, ["2026-10-09", d]), ("prune", d), _merge()]


def _done(d: str, *stages: str) -> dict:
    """A run's outputs through shards ∥ catalog, and those of `stages` (`drill`, `anchors`)."""
    run = f"{ROOT}/deltas/{d}"
    out = {f"{run}/scans.json": None, **{f"{run}/dhist/r{i:04d}.parquet": None for i in range(8)}, f"{run}/sidecar.parquet": None,
           f"{run}/catalog/meta.json": None}
    return {**out, **{f"{run}/{s}/meta.json": None for s in stages}}


def test_drill_and_anchors_are_in_flight_at_once():
    """Each job waits at a two-party barrier: run one after the other, the first would wait alone and break it."""
    d = "2026-10-09T0001"
    logs = []
    f = Fake({}, [d], barrier=("drill", "anchors"))
    assert f.daily(ANCHORS, log=logs.append).run(d) == [d]
    assert f.calls == _chain(d, [d], drill=True, anchors=True)
    pair = [re.sub(r"\d+s$", "<n>s", m) for m in logs if m.startswith(f"{d} drill") or m.startswith(f"{d} anchors")]
    assert sorted(pair) == [
        f"{d} anchors: done in <n>s",
        f"{d} drill (long ∥ short) ∥ anchors: done in <n>s",
        f"{d} drill (long ∥ short) ∥ anchors: start",
        f"{d} drill (long ∥ short): done in <n>s",
    ]


def test_an_appended_scan_missing_both_gets_them_at_once():
    d = "2026-10-09T1236"
    runs = _runs("2026-10-09", d)
    f = Fake({f"{ROOT}/manifests/{d}.json": {"date": d, "runs": runs}, **_had("2026-10-09", "drill", "anchors")}, [d], barrier=("drill", "anchors"))
    assert f.daily(ANCHORS).run(d) == []
    chain = _chain(d, [], drill=True, anchors=True)
    assert f.calls == [chain[4], chain[5], _r2(d, ["2026-10-09", d]), ("prune", d), _merge()]


@pytest.mark.parametrize("done, missing", [("drill", 5), ("anchors", 4)])
def test_a_rerun_with_one_of_drill_and_anchors_built_runs_only_the_other(done, missing):
    d = "2026-10-09T0001"
    f = Fake(_done(d, done), [d])
    assert f.daily(ANCHORS).run(d) == [d]
    chain = _chain(d, [d], drill=True, anchors=True)
    assert f.calls == [chain[missing], *chain[6:]]


@pytest.mark.parametrize("fails, other", [("drill", "anchors"), ("anchors", "drill")])
def test_a_failed_drill_or_anchors_fails_the_run_after_the_other_ends_and_a_rerun_resumes_it(fails, other):
    """The other job runs to its end and its output is kept; nothing after the pair runs; a rerun submits only the failed one."""
    d = "2026-10-09T0001"
    f = Fake(_done(d), [d], fail=(fails,), barrier=("drill", "anchors"))
    with pytest.raises(RuntimeError) as e:
        f.daily(ANCHORS).run(d)
    assert str(e.value) == f"Batch job sn-{fails}-2026-10-09-0001-123456: FAILED"
    chain = _chain(d, [d], drill=True, anchors=True)
    assert f.calls == chain[4:6]
    assert (f"{ROOT}/deltas/{d}/{other}/meta.json" in f.keys, f"{ROOT}/deltas/{d}/{fails}/meta.json" in f.keys) == (True, False)
    f.calls, f.fail, f.barrier = [], (), ((), None)
    assert f.daily(ANCHORS).run(d) == [d]
    assert f.calls == [chain[4 if fails == "drill" else 5], *chain[6:]]


def test_both_failing_names_each():
    """The drill's job fails; the anchors' succeeds without writing its `meta.json` (the post-check)."""
    d = "2026-10-09T0001"
    f = Fake(_done(d), [d], fail=("drill",), silent=("anchors",))
    with pytest.raises(RuntimeError) as e:
        f.daily(ANCHORS).run(d)
    assert str(e.value) == (f"drill (long ∥ short): Batch job sn-drill-2026-10-09-0001-123456: FAILED; "
                            f"anchors: {ROOT}/deltas/{d}/anchors/meta.json: not written (the task succeeded)")
    assert f.calls == _chain(d, [d], drill=True, anchors=True)[4:6]


def test_the_cli_exits_1_on_a_drill_or_anchors_failure(monkeypatch):
    """`runs add`: a failure in the pair is a `RuntimeError`, which the CLI turns into exit 1."""
    from click.testing import CliRunner

    d = "2026-10-09T0001"
    f = Fake(_done(d), [d], fail=("anchors",))
    monkeypatch.setattr(sd, "ready", lambda p, gen: ANCHORS)
    monkeypatch.setattr(sd, "profile", lambda: ANCHORS)
    monkeypatch.setattr(sd, "gcs_runner", lambda cfg, **kw: f.daily(cfg))
    errs = []
    monkeypatch.setattr(sd, "err", errs.append)
    r = CliRunner().invoke(sd.add_cmd, [d])
    assert (r.exit_code, r.output) == (1, "")
    assert errs == [f"static-names runs add {d}: Batch job sn-anchors-2026-10-09-0001-123456: FAILED"]


# ── The merge stage (deferred carries, `static_merge`) ─────────────────────


SCANS = ["2026-10-09T0001", "2026-10-09T0601", "2026-10-09T1202", "2026-10-09T1801"]


def _levels(f: Fake, key: str) -> list[tuple[str, int]]:
    return [(r["key"], r["level"]) for r in f.keys[f"{ROOT}/manifests/{key}.json"]["runs"]]


def test_publish_adds_level_0_runs_and_the_merge_stage_carries_them_later():
    """Four scans with the merge stage off: each publish (local, no Batch job) only adds its level-0 run. The stage on its own
    then submits one merge job (the four runs into one L2) and, once it's done, the R2 job for the revision it published."""
    f = Fake({}, SCANS)
    for d in SCANS:
        assert f.daily(merge=False).run(d) == [d]
    assert [_levels(f, d) for d in SCANS] == [[(f"deltas/{d}", 0) for d in SCANS[:i + 1]] for i in range(4)]
    assert [c for c in f.calls if c[0] in ("publish", "merge")] == [("publish", d) for d in SCANS]
    f.calls = []
    f.daily(merge_wait=None).carries()
    a_d = f"{SCANS[0]}_{SCANS[-1]}"
    assert f.calls == [_merge(None), _r2(SCANS[-1], [a_d], f"{SCANS[-1]}.m001")]
    assert _levels(f, f"{SCANS[-1]}.m001") == [(f"deltas/{a_d}", 2)]


def test_a_waited_merge_reaches_r2_in_the_same_run():
    f = Fake({}, SCANS[:2])
    assert f.daily(merge_wait=None).run(SCANS[1], catch_up=True) == SCANS[:2]
    a_b = f"{SCANS[0]}_{SCANS[1]}"
    assert f.calls == [*_chain(SCANS[0], SCANS[:1]), *_chain(SCANS[1], SCANS[:2]), _merge(None), _r2(SCANS[1], [a_b], f"{SCANS[1]}.m001")]


def test_a_detached_merge_reaches_r2_with_the_next_scan():
    """`runs add`'s default: the merge job is submitted and not waited on. Its revision lands on GCS; the next scan's manifest
    builds on it, so that scan's R2 job copies the merged run; no carry is due then, and no revision to copy."""
    f = Fake({}, SCANS[:3])
    logs = []
    f.daily(log=logs.append).run(SCANS[1], catch_up=True)
    a_b = f"{SCANS[0]}_{SCANS[1]}"
    assert f.calls[-1] == _merge(0)
    assert [m for m in logs if "still running" in m] == [
        f"merge: Batch job sn-merge-{SCANS[1].lower().replace('t', '-')}-123456: still running after 0s; it publishes its revision on GCS when done, "
        "and R2 gets it with a later run"]
    assert _levels(f, f"{SCANS[1]}.m001") == [(f"deltas/{a_b}", 1)]
    f.calls = []
    assert f.daily().run(SCANS[2]) == [SCANS[2]]
    assert f.calls == _chain(SCANS[2], [a_b, SCANS[2]])
    assert _levels(f, SCANS[2]) == [(f"deltas/{a_b}", 1), (f"deltas/{SCANS[2]}", 0)]


@pytest.mark.parametrize("r2_sa, r2_env", [(None, True), ("build@proj", True), ("copy@proj", False)])
def test_the_merge_job_has_the_r2_env_when_it_runs_as_the_r2_account(r2_sa, r2_env):
    """The merge job mirrors its lease to R2 (`merging.json`, for /health) with the R2 copy's env, which it gets when the
    R2 account is the stages' own (gcs: none set; cw: the same): a job runs as one account, and the merge's writes the
    data bucket. Its name is its status record's holder."""
    f = Fake({}, SCANS[:2])
    specs = []
    run_job = f.run_job
    f.run_job = lambda name, spec, wait=None: specs.append((name, spec)) or run_job(name, spec, wait)
    f.daily(Profile(**{**CFG.__dict__, "r2_sa": r2_sa}), merge_wait=None).run(SCANS[1], catch_up=True)
    name, spec = next((n, s) for n, s in specs if s["labels"]["stage"] == "merge")
    r2 = {"R2_ACCESS_KEY_ID": "projects/proj/secrets/s-id/versions/latest", "R2_ENDPOINT": "projects/proj/secrets/s-end/versions/latest",
          "R2_SECRET_ACCESS_KEY": "projects/proj/secrets/s-key/versions/latest"}
    assert (spec["allocationPolicy"]["serviceAccount"]["email"], spec["taskGroups"][0]["taskSpec"]["environment"]) == ("build@proj", {
        "variables": {"STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr", **({"R2_BUCKET": "idx"} if r2_env else {}), "DT_JOB_NAME": name},
        **({"secretVariables": r2} if r2_env else {}),
    })


def test_a_failed_merge_never_fails_the_run():
    f = Fake({}, SCANS[:2], fail=("merge",))
    logs = []
    assert f.daily(log=logs.append, merge_wait=None).run(SCANS[1], catch_up=True) == SCANS[:2]
    assert f.calls[-1] == _merge(None)
    assert [m for m in logs if m.startswith("merge: failed")] == [
        f"merge: failed, not fatal (every listed run is whole; the next run plans again): Batch job sn-merge-{SCANS[1].lower().replace('t', '-')}-123456: FAILED"]
    assert sorted(k.rsplit("/", 1)[-1] for k in f.keys if "/manifests/" in k) == [f"{d}.json" for d in SCANS[:2]]
    # a rerun (already appended) plans the carry again
    f.calls, f.fail = [], ()
    assert f.daily(merge_wait=None).run(SCANS[1]) == []
    a_b = f"{SCANS[0]}_{SCANS[1]}"
    assert f.calls == [_r2(SCANS[1], SCANS[:2]), ("prune", SCANS[1]), _merge(None), _r2(SCANS[1], [a_b], f"{SCANS[1]}.m001")]


def test_the_merge_cli_exits_1_on_a_failure_and_runs_add_skips_the_stage_with_M(monkeypatch):
    from click.testing import CliRunner

    f = Fake({}, SCANS[:2])
    f.daily(merge=False).run(SCANS[1], catch_up=True)
    monkeypatch.setattr(sd, "ready", lambda p, gen: CFG)
    monkeypatch.setattr(sd, "profile", lambda: CFG)
    seen = []
    monkeypatch.setattr(sd, "gcs_runner", lambda cfg, **kw: seen.append(kw) or f.daily(cfg, merge=kw.get("merge", True), merge_wait=kw.get("merge_wait")))
    errs = []
    monkeypatch.setattr(sd, "err", errs.append)
    f.fail = ("merge",)
    r = CliRunner().invoke(sd.merge_cmd, [])
    assert (r.exit_code, errs) == (1, [f"static-names runs merge: Batch job sn-merge-{SCANS[1].lower().replace('t', '-')}-123456: FAILED"])
    f.fail, f.calls = (), []
    r = CliRunner().invoke(sd.merge_cmd, ["-w", "600"])
    assert r.exit_code == 0
    assert f.calls == [_merge(600.0), _r2(SCANS[1], [f"{SCANS[0]}_{SCANS[1]}"], f"{SCANS[1]}.m001")]
    f.calls = []
    r = CliRunner().invoke(sd.add_cmd, ["-M", SCANS[1]])
    assert (r.exit_code, f.calls) == (0, [_r2(SCANS[1], [f"{SCANS[0]}_{SCANS[1]}"], f"{SCANS[1]}.m001"), ("prune", SCANS[1])])
    assert seen[-1] == {"dry_run": False, "verify_terms": None, "merge": False, "merge_wait": 0}


def test_batch_runner_stops_watching_after_its_wait(monkeypatch):
    """`wait`: the job keeps running (nothing is cancelled); the runner stops polling once the wait is up."""
    from dt_cloud import batch, gcp

    submitted, polls = [], []
    monkeypatch.setattr(batch, "submit_job", lambda spec, name, region: submitted.append(name))
    states = iter(["RUNNING", "RUNNING", "SUCCEEDED"])
    monkeypatch.setattr(gcp, "batch_job", lambda name, project, region: polls.append(name) or {"status": {"state": next(states)}})
    run = sd.BatchRunner(CFG, lambda m: None, delay=0, max_delay=0)
    with pytest.raises(sd.StillRunning) as e:
        run("j1", {}, wait=0)
    assert (str(e.value), submitted, polls) == ("Batch job j1: still running after 0s", ["j1"], [])
    run("j2", {})
    assert (submitted, polls) == (["j1", "j2"], ["j2", "j2", "j2"])


# ── Backfill: every listed run's drill and anchors ─────────────────────────

#: cw's 2026-10-10: a catch-up by an image from before cw's `drill` (and deferred carries) appended 1801 and 0001, carried
#: inline into the level-1 run `deltas/1801_0001` (no `drill/`); then 0601, appended by the current image, whose drill
#: failed (its build reads the merged run's drill).
CW_SCANS = ["2026-10-09T1801", "2026-10-10T0001", "2026-10-10T0601"]
CW_L1 = {"key": f"deltas/{CW_SCANS[0]}_{CW_SCANS[1]}", "first": CW_SCANS[0], "last": CW_SCANS[1], "level": 1, "scans": CW_SCANS[:2]}


def _cw_keys(*built: str, tiers: tuple[str, ...] = ("drill",)) -> dict:
    """cw's manifests, with `built` (run keys) holding `tiers`."""
    a, b, c = CW_SCANS
    return {
        f"{ROOT}/manifests/{a}.json": {"date": a, "runs": _runs(a)},
        f"{ROOT}/manifests/{b}.json": {"date": b, "runs": [CW_L1]},
        f"{ROOT}/manifests/{c}.json": {"date": c, "runs": [CW_L1, *_runs(c)]},
        **{f"{ROOT}/{k}/{t}/meta.json": None for k in built for t in tiers},
    }


def _drill_build(d: str) -> tuple:
    return ("drill", 2, _py("static_drill", "build", f"-g {GEN} -d {d}", "-k", "task", "-M", "60GB", "-p", "16"))


def _tier_merge(key: str, tier: str = "drill") -> tuple:
    return (f"{tier}-merge", 1, _py("static_merge", "tier", "-g", GEN, "-r", key, "-t", tier, "-M", "90GB", "-p", "16"))


def test_a_merged_run_without_its_drill_is_backfilled_oldest_first_then_copied_to_r2():
    """The rerun cw needs (`runs add 2026-10-10T0601`, already appended): 1801's drill (over the base), 0001's (over 1801's:
    the manifest before it lists `deltas/1801`), the merged run's from those two, then 0601's (over the merged run's);
    then the R2 job copies both listed runs, each `drill/meta.json` last; no carry is due (`[L1, L0]`)."""
    a, b, c = CW_SCANS
    f = Fake(_cw_keys(), CW_SCANS)
    assert f.daily(DRILL).run(c) == []
    assert f.calls == [_drill_build(a), _drill_build(b), _tier_merge(CW_L1["key"]), _drill_build(c), _r2(c, [f"{a}_{b}", c]), ("prune", c)]
    assert sorted(k for k in f.keys if k.endswith("/drill/meta.json")) == [f"{ROOT}/deltas/{x}/drill/meta.json" for x in (a, f"{a}_{b}", b, c)]


def test_the_next_scan_backfills_first_then_runs_its_chain_and_the_carry():
    """With 1201 published, `runs add -c 2026-10-10T1201`: the backfill, then 1201's own chain (its drill over the now
    drilled stack), its R2 job listing every run; then the carry `[L1, L0, L0]` → L2 is due."""
    a, b, c = CW_SCANS
    d = "2026-10-10T1201"
    f = Fake(_cw_keys(), [*CW_SCANS, d])
    assert f.daily(DRILL).run(d, catch_up=True) == [d]
    assert f.calls == [_drill_build(a), _drill_build(b), _tier_merge(CW_L1["key"]), _drill_build(c),
                       *_chain(d, [f"{a}_{b}", c, d], drill=True), _merge()]


def test_a_partly_backfilled_stack_resumes_at_the_first_missing_drill():
    """1801's and 0001's built (an earlier attempt), the merge not: the rerun merges, then builds 0601's."""
    a, b, c = CW_SCANS
    f = Fake(_cw_keys(f"deltas/{a}", f"deltas/{b}"), CW_SCANS)
    f.daily(DRILL).run(c)
    assert f.calls == [_tier_merge(CW_L1["key"]), _drill_build(c), _r2(c, [f"{a}_{b}", c]), ("prune", c)]


def test_a_complete_stack_is_a_no_op():
    a, b, c = CW_SCANS
    f = Fake(_cw_keys(CW_L1["key"], f"deltas/{c}"), CW_SCANS)
    assert f.daily(DRILL).run(c) == []
    assert f.calls == [_r2(c, [f"{a}_{b}", c]), ("prune", c)]
    # without the profile's `drill`, nothing is backfilled either
    f, f.calls = Fake(_cw_keys(), CW_SCANS), []
    f.daily().run(c)
    assert f.calls == [_r2(c, [f"{a}_{b}", c]), ("prune", c)]


@pytest.mark.parametrize("fails", ["drill", "drill-merge"])
def test_a_failed_backfill_fails_the_run_and_a_rerun_resumes_it(fails):
    """Fatal, as the drill stage is: nothing after it runs (no R2 copy, no prune, no carry); the rerun resumes at the failed
    step (the steps before it kept)."""
    a, b, c = CW_SCANS
    f = Fake(_cw_keys(), CW_SCANS, fail=(fails,))
    with pytest.raises(RuntimeError) as e:
        f.daily(DRILL).run(c)
    stage = f"{fails}-{b.lower().replace('t', '-')}" if fails == "drill-merge" else f"drill-{a.lower().replace('t', '-')}"
    assert str(e.value) == f"Batch job sn-{stage}-123456: FAILED"
    first = [_drill_build(a), _drill_build(b), _tier_merge(CW_L1["key"])]
    assert f.calls == first[:3 if fails == "drill-merge" else 1]
    f.calls, f.fail = [], ()
    f.daily(DRILL).run(c)
    assert f.calls == [*first[2 if fails == "drill-merge" else 0:], _drill_build(c), _r2(c, [f"{a}_{b}", c]), ("prune", c)]


def test_a_merge_job_that_writes_no_drill_fails_the_check():
    a, b, c = CW_SCANS
    f = Fake(_cw_keys(f"deltas/{a}", f"deltas/{b}"), CW_SCANS, silent=("drill-merge",))
    with pytest.raises(RuntimeError) as e:
        f.daily(DRILL).run(c)
    assert (str(e.value), f.calls) == (f"{ROOT}/deltas/{a}_{b}: its drill not written (the merge job succeeded)", [_tier_merge(CW_L1["key"])])


def test_anchors_are_backfilled_beside_the_drill():
    """With the profile's `anchors` and the base's: the anchors' chain (each scan's `anchors run`, the merged run's
    `static_merge tier -t anchors`) beside the drill's, each oldest first."""
    a, b, c = CW_SCANS
    f = Fake({**_cw_keys(), f"{ROOT}/anchors/meta.json": None}, CW_SCANS)
    f.daily(ANCHORS).run(c)
    anchors = lambda d: ("anchors", 1, _py("static_anchors", "run", f"-g {GEN} -d {d}", "-M", "90GB", "-p", "16"))  # noqa: E731
    fam = lambda t: [x for x in f.calls if x[0] in (t, f"{t}-merge")]  # noqa: E731
    assert (fam("drill"), fam("anchors"), f.calls[-2:]) == (
        [_drill_build(a), _drill_build(b), _tier_merge(CW_L1["key"]), _drill_build(c)],
        [anchors(a), anchors(b), _tier_merge(CW_L1["key"], "anchors"), anchors(c)],
        [_r2(c, [f"{a}_{b}", c]), ("prune", c)])
    # no anchors on the base: none backfilled
    f = Fake(_cw_keys(CW_L1["key"], f"deltas/{c}"), CW_SCANS)
    f.daily(ANCHORS).run(c)
    assert f.calls == [_r2(c, [f"{a}_{b}", c]), ("prune", c)]


def test_runs_merge_backfills_then_copies_then_carries(monkeypatch):
    """`runs merge`: the backfill, the R2 job for the newest manifest (its runs' drills, `meta.json` last), then the merge
    stage (none due here); a failing backfill exits 1."""
    from click.testing import CliRunner

    a, b, c = CW_SCANS
    f = Fake(_cw_keys(), CW_SCANS, fail=("drill-merge",))
    monkeypatch.setattr(sd, "ready", lambda p, gen: DRILL)
    monkeypatch.setattr(sd, "profile", lambda: DRILL)
    monkeypatch.setattr(sd, "gcs_runner", lambda cfg, **kw: f.daily(cfg, merge_wait=kw.get("merge_wait")))
    errs = []
    monkeypatch.setattr(sd, "err", errs.append)
    r = CliRunner().invoke(sd.merge_cmd, [])
    assert (r.exit_code, errs) == (1, [f"static-names runs merge: Batch job sn-drill-merge-{b.lower().replace('t', '-')}-123456: FAILED"])
    f.fail, f.calls = (), []
    r = CliRunner().invoke(sd.merge_cmd, [])
    assert (r.exit_code, f.calls) == (0, [_tier_merge(CW_L1["key"]), _drill_build(c), _r2(c, [f"{a}_{b}", c])])


def test_a_folded_run_an_inner_scan_was_built_over_is_backfilled_before_that_scan():
    """Four scans carried inline into one L2 by an old image: the newest manifest lists only `deltas/a_d`, but the manifest
    before c listed `deltas/a_b`, which c's build reads as its earlier tier: a_b's drill is merged before c's is built."""
    a, b, c, d = SCANS
    l1 = {"key": f"deltas/{a}_{b}", "first": a, "last": b, "level": 1, "scans": [a, b]}
    l2 = {"key": f"deltas/{a}_{d}", "first": a, "last": d, "level": 2, "scans": SCANS}
    f = Fake({f"{ROOT}/manifests/{a}.json": {"date": a, "runs": _runs(a)}, f"{ROOT}/manifests/{b}.json": {"date": b, "runs": [l1]},
              f"{ROOT}/manifests/{c}.json": {"date": c, "runs": [l1, *_runs(c)]}, f"{ROOT}/manifests/{d}.json": {"date": d, "runs": [l2]}}, SCANS)
    assert f.daily(DRILL).backfill_plan("drill") == [("build", a), ("build", b), ("merge", l1["key"]), ("build", c), ("build", d), ("merge", l2["key"])]
    f.daily(DRILL).run(d)
    assert f.calls == [_drill_build(a), _drill_build(b), _tier_merge(l1["key"]), _drill_build(c), _drill_build(d), _tier_merge(l2["key"]),
                       _r2(d, [f"{a}_{d}"]), ("prune", d)]
