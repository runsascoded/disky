"""`dt-cloud scan step`: each step's commands (a fake runner records them), its skip-if-done, its gates, and the
dispatcher's fake / fail / soft semantics."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import scan_steps as S
from dt_cloud.cli import main
from dt_cloud.scan_profile import parse


def profile(tmp_path: Path, **kw):
    d = {"deployment": "fix", "executor": {"kind": "local"}, "work": str(tmp_path / "nf"), "data": str(tmp_path / "data"),
         "layout": {"layer2": "l2/{scan}/", "snapshots": "sub"},
         "sources": [{"uri": "s3://a", "endpoint": "https://s3.example.org", "procs": 4}, "s3://b"],
         "options": {"list": {"import": True, "bulk_args": ["-a"], "threads": 8},
                     "index": {"webdata": ["python", "web.py", "{web}", "{sources}", "-a", "{date}"], "mem": "16GB", "threads": 2},
                     "static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "c.yml"}}}
    d.update(kw)
    return parse(d)


class Runner:
    """A fake `subprocess.run`: records each argv (tmp paths as `<tmp>`), and plays the commands' side effects."""

    def __init__(self, tmp: Path, fail: tuple[str, ...] = (), stdout: dict[str, str] | None = None):
        self.tmp, self.fail, self.stdout, self.calls, self.envs = tmp, fail, stdout or {}, [], []

    def __call__(self, argv, env, stdout=None, text=True):
        cmd = " ".join(argv).replace(str(self.tmp), "<tmp>")
        self.calls.append(cmd)
        self.envs.append(env)
        key = " ".join(argv[:3])
        if argv[:2] == ["disk-tree", "import"]:
            scans = Path(env["DISK_TREE_ROOT"]) / "scans"
            scans.mkdir(parents=True)
            (scans / "u1.parquet").write_text(f"l2 of {argv[argv.index('-b') + 1]}")
        if argv[:2] == ["dt-cloud", "index-write"]:
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True)
            for n in ("path-index.parquet", "path-index-bysize.parquet", "age-pyramid-1.parquet"):
                (out / n).write_text(n)
        if argv[:3] == ["dt-cloud", "path-index", "-d"] and "-P" not in argv:  # snapshot JSONs only (hf's webdata)
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "meta.json").write_text("{}")
        if argv[:2] == ["python", "web.py"]:
            Path(argv[2]).mkdir(parents=True, exist_ok=True)
            (Path(argv[2]) / "meta.json").write_text("{}")
        return subprocess.CompletedProcess(argv, 1 if key in self.fail else 0, self.stdout.get(key, ""), None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("SCAN_RUNS_PROFILE", raising=False)
    return {"SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "1", "TMPDIR": str(tmp_path / "t"),
            "HOME": str(tmp_path / "home")}


def ctx(tmp_path, env, runner=None, **kw) -> S.Ctx:
    (tmp_path / "t").mkdir(exist_ok=True)
    c = S.make_ctx(profile(tmp_path, **kw), env)
    c.sh_impl = runner or Runner(tmp_path)
    return c


def test_list_imports_and_skips_when_done(tmp_path, env, monkeypatch):
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 16))
    c = ctx(tmp_path, env)
    S.list_source(c, "s3://a")
    assert c.sh_impl.calls == [
        "disk-tree bulk-list -a s3://a -E https://s3.example.org -o <tmp>/t/dt-scan/2026-10-16T1200/listing/a -P 4 -w 8 -x clear",
        "disk-tree import -e stream -j 16 -m -s s3 -t 2026-10-16T00:00:00+00:00 -l <tmp>/t/dt-scan/2026-10-16T1200/listing/a/shard-*.parquet -b a",
    ]
    assert (tmp_path / "data/l2/2026-10-16T1200/a.parquet").read_text() == "l2 of a"
    again = ctx(tmp_path, env)
    S.list_source(again, "s3://a")
    assert again.sh_impl.calls == []


def test_index_layer2_webdata_tiers_then_skip(tmp_path, env, monkeypatch):
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 16))
    l2 = tmp_path / "data/l2/2026-10-16T1200"
    l2.mkdir(parents=True)
    for b in ("a", "b"):
        (l2 / f"{b}.parquet").write_text(f"l2 of {b}")
    c = ctx(tmp_path, env)
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert c.sh_impl.calls == [
        f"python web.py {w}/web a={w}/l2-in/a.parquet b={w}/l2-in/b.parquet -a 2026-10-16",
        f"dt-cloud index-write -m 16GB -t 2 -o {w}/index a={w}/l2-in/a.parquet b={w}/l2-in/b.parquet",
    ]
    assert sorted(p.relative_to(tmp_path / "data").as_posix() for p in (tmp_path / "data").rglob("*") if p.is_file()) == [
        "l2/2026-10-16T1200/a.parquet",
        "l2/2026-10-16T1200/b.parquet",
        "l2/2026-10-16T1200/index/G1/age-pyramid-1.parquet",
        "l2/2026-10-16T1200/index/G1/path-index-bysize.parquet",
        "l2/2026-10-16T1200/index/G1/path-index.parquet",
        "snapshots/sub/2026-10-16T1200/meta.json",
    ]
    again = ctx(tmp_path, env)
    S.index(again, None)
    assert again.sh_impl.calls == [f"python web.py {w}/web a={w}/l2-in/a.parquet b={w}/l2-in/b.parquet -a 2026-10-16"]


def test_index_names_a_lost_input(tmp_path, env):
    c = ctx(tmp_path, env)
    with pytest.raises(S.StepFailed) as e:
        S.index(c, None)
    assert str(e.value) == (f"missing input {tmp_path}/data/l2/2026-10-16T1200/a.parquet (from `list`): a cached task's output "
                            "was lost; rerun the head with -F")


def test_index_imports_listings_when_list_does_not(tmp_path, env, monkeypatch):
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 2))
    for b in ("a", "b"):
        (tmp_path / f"data/lst/2026-10-16T1200/{b}").mkdir(parents=True)
    c = ctx(tmp_path, env, layout={"layer2": "l2/{scan}/", "snapshots": "sub", "listing": "lst/{scan}/"},
            options={"static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "c.yml"}})
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert c.sh_impl.calls == [
        "disk-tree import -e stream -j 2 -m -s s3 -t 2026-10-16T00:00:00+00:00 -l <tmp>/data/lst/2026-10-16T1200/a/shard-*.parquet -b a",
        "disk-tree import -e stream -j 2 -m -s s3 -t 2026-10-16T00:00:00+00:00 -l <tmp>/data/lst/2026-10-16T1200/b/shard-*.parquet -b b",
        f"dt-cloud index-write -t 2 -o {w}/index a={w}/l2/a/scans/u1.parquet b={w}/l2/b/scans/u1.parquet",
    ]


def test_serving_steps_gate_on_credentials(tmp_path, env):
    c = ctx(tmp_path, env)
    for f in (S.publish_r2, S.index_rows, S.index_sync, S.over_time, S.index_gc, S.warm_cache, S.probe, S.digest):
        f(c, None)
    assert c.sh_impl.calls == []


def test_serving_steps_commands(tmp_path, env):
    env.update(CLOUDFLARE_API_TOKEN=" tok\n", CLOUDFLARE_ACCOUNT_ID="acct", R2_ENDPOINT="https://r2.example.org",
               R2_BUCKET="r2b", R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s", SITE_TOKEN="st", SITE_URL="https://site.example.org",
               SLACK_BOT_TOKEN="x", SLACK_CHANNEL="C1")
    c = ctx(tmp_path, env, secrets={"CLOUDFLARE_API_TOKEN": "cf-token"})
    assert env["CLOUDFLARE_API_TOKEN"] == "tok"
    for f in (S.static_names, S.publish_r2, S.index_rows, S.index_sync, S.over_time, S.index_gc, S.warm_cache, S.probe, S.digest):
        f(c, None)
    k = "l2/2026-10-16T1200/index/G1"
    assert c.sh_impl.calls == [
        "dt-cloud static-names runs add -c 2026-10-16T1200",
        "dt-cloud publish-r2 -a 2026-10-16T1200",
        f"dt-cloud index-sync -r -d <tmp>/data/{k} -g G1 -k {k} 2026-10-16T1200",
        f"dt-cloud index-sync -p -d <tmp>/data/{k} -g G1 -k {k} -M <tmp>/data/snapshots/sub 2026-10-16T1200",
        "dt-cloud index-gc 2026-10-16T1200",
        "dt-cloud over-time-groups -g G1 -o <tmp>/t/dt-scan/2026-10-16T1200/over-time -p r2://r2b",
        "dt-cloud index-gc -R -m 2d -F <tmp>/data -F r2",
        "dt-cloud warm-cache -d 2026-10-16T1200 -r r2://r2b/snapshots/sub",
        "dt-cloud probe -c -u https://site.example.org -o <tmp>/data/probes/sub/",
        "dt-cloud digest -T t -C c.yml -P slack -r <tmp>/data/snapshots/sub",
    ]
    assert (tmp_path / "home/.config/disk-tree/buckets.yml").read_text() == (
        "buckets:\n  - uri: r2://r2b\n    endpoint_url: https://r2.example.org\n    profile: r2\n")
    assert (Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text(), env["LAYER2_PREFIX"], env["SCAN_ID"]) == (
        "[r2]\naws_access_key_id = k\naws_secret_access_key = s\n", "l2/{scan}/", "2026-10-16T1200")


def test_reproc_skips_side_effects(tmp_path, env):
    env.update(REPROC="1", SITE_TOKEN="st", SITE_URL="https://site.example.org", R2_ARCHIVE="r2://x")
    c = ctx(tmp_path, env)
    for f in (S.static_names, S.interval_store, S.warm_cache, S.probe):
        f(c, None)
    assert (c.sh_impl.calls, env["DT_RECORD"]) == ([], "off")


def test_interval_store_without_a_generation(tmp_path, env):
    env["R2_ARCHIVE"] = "r2://x"
    c = ctx(tmp_path, env, runner=Runner(tmp_path, stdout={"dt-cloud interval-store current": "\n"}))
    S.interval_store(c, None)
    assert c.sh_impl.calls == ["dt-cloud interval-store current -o r2://x"]


class FakeS3:
    def __init__(self, buckets: dict[str, int | Exception]):
        self.buckets = buckets

    def list_buckets(self):
        return {"Buckets": [{"Name": n} for n in self.buckets]}

    def list_objects_v2(self, Bucket, MaxKeys):
        v = self.buckets[Bucket]
        if isinstance(v, Exception):
            raise v
        return {"KeyCount": v}


def test_unscanned():
    s3 = FakeS3({"scanned": 5, "empty": 0, "new": 3, "denied": PermissionError("no")})
    assert S.unscanned(s3, {"scanned"}) == ["denied (check failed: ListObjectsV2: PermissionError: no)", "new"]


def test_run_step_fake_fail_unknown(tmp_path, monkeypatch):
    marks = tmp_path / "marks"
    env = {S.FAKE_ENV: str(marks), S.FAIL_ENV: "index"}
    assert [S.run_step("list", "s3://a", env), S.run_step("post-it", None, env), S.run_step("index", None, env),
            S.run_step("list", None, env), S.run_step("index-sync", None, env)] == [0, 2, 1, 2, 0]
    assert marks.read_text().split("\n") == ["list s3://a", "index-sync", ""]


def test_soft_step_failure_is_a_warning(tmp_path, env, monkeypatch):
    p = tmp_path / "p.yml"
    p.write_text(f"deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                 "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\nskip: [static-names, digest]\n"
                 "options: {list: {import: true}}\n")

    def boom(c, a):
        raise RuntimeError("D1 down")

    monkeypatch.setitem(S.STEPS, "index-sync", S.Step("index-sync", boom, soft=True))
    monkeypatch.setitem(S.STEPS, "index", S.Step("index", boom))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "t").mkdir()
    soft = CliRunner().invoke(main, ["scan", "step", "-p", str(p), "index-sync"])
    assert (soft.exit_code, soft.stderr.rstrip().split("\n")) == (0, [
        "scan step index-sync: RuntimeError: D1 down",
        "WARN: index-sync failed (exit 1); soft step, the scan goes on",
    ])
    hard = CliRunner().invoke(main, ["scan", "step", "-p", str(p), "index"])
    assert (hard.exit_code, type(hard.exception)) == (1, RuntimeError)


def test_identity_is_required(tmp_path):
    p = profile(tmp_path)
    with pytest.raises(S.StepFailed) as e:
        S.make_ctx(p, {"SNAP_ID": "2026-10-16"})
    assert str(e.value) == "the scan's identity is stamped by the head: $GEN, $DT_JOB_START unset"


def gcs_ctx(tmp_path, env, monkeypatch, **opts):
    """A gcs-shaped profile: `gs://` sources listed into the data bucket (FUSE-mounted at `<tmp>/mnt`), indexed by
    `path-index`."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 32))
    (tmp_path / "t").mkdir(exist_ok=True)
    p = parse({"deployment": "fix", "executor": {"kind": "google-batch", "project": "p", "region": "r", "image": "i"},
               "work": "gs://dvx/nf", "data": "gs://dvx",
               "layout": {"layer2": "listing/{scan}/", "snapshots": "", "listing": "listing/{scan}/"},
               "sources": ["gcs://east", "gcs://eu"], "steps": ["access-ingest", "healthcheck"],
               "options": {"list": {"weights": True, "threads": 10},
                           "index": {"mode": "path-index", "attribution": ["attr/a.parquet"], "access": "access/agg/*/*.parquet",
                                     "args": ["-u", "bysize"], "lifecycle": True, "rules": True},
                           "static-names": {"profile": "sn.json"},
                           "digest": {"template": "gcs", "config": "d.yml", "platforms": ["slack", "discord"]}, **opts}})
    c = S.make_ctx(p, {**env, "DT_DATA_MOUNT": str(tmp_path / "mnt")})
    c.sh_impl = Runner(tmp_path)
    return c


def test_list_weights_from_the_newest_earlier_listing(tmp_path, env, monkeypatch):
    root = tmp_path / "mnt/dvx/listing"
    for scan, done in (("2026-10-15T0701", True), ("2026-10-16T0701", True), ("2026-10-16T0900", False), ("2026-10-17T0701", True)):
        (root / scan / "east").mkdir(parents=True)
        if done:
            (root / scan / "east/_SUCCESS.json").write_text("{}")
    c = gcs_ctx(tmp_path, env, monkeypatch)
    S.list_source(c, "gcs://east")
    S.list_source(c, "gcs://eu")
    assert c.sh_impl.calls == [
        "disk-tree bulk-list gcs://east -o gs://dvx/listing/2026-10-16T1200/east -P 32 -w 10 -x clear "
        "-W <tmp>/mnt/dvx/listing/2026-10-16T0701/east/*.parquet",
        "disk-tree bulk-list gcs://eu -o gs://dvx/listing/2026-10-16T1200/eu -P 32 -w 10 -x clear",
    ]


def test_path_index_stages_aggregates_uploads_and_publishes(tmp_path, env, monkeypatch):
    mnt = tmp_path / "mnt/dvx"
    for b in ("east", "eu"):
        (mnt / f"listing/2026-10-16T1200/{b}").mkdir(parents=True)
        (mnt / f"listing/2026-10-16T1200/{b}/_SUCCESS.json").write_text("{}")
    (mnt / "access/agg/2026-10-15").mkdir(parents=True)
    (mnt / "access/agg/2026-10-15/x.parquet").write_text("")
    uploads = []

    def upload(files, dest):
        uploads.append(([f.name for f in files], dest))
        d = mnt / dest.removeprefix("gs://dvx/")
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f.name).write_text(f.read_text())

    monkeypatch.setattr(S, "upload", upload)
    c = gcs_ctx(tmp_path, env, monkeypatch)
    runner = c.sh_impl

    def run(argv, env, stdout=None, text=True):
        r = runner(argv, env, stdout, text)
        if argv[:2] == ["dt-cloud", "path-index"]:
            assert env["DUCKDB_TMP"] == f"{tmp_path}/t/dt-scan/2026-10-16T1200/stage/.duckdb-tmp"
            pi = Path(argv[argv.index("-P") + 1]).parent
            for n in ("path-index.parquet", "path-index-bysize.parquet", "path-index-bysize-by-user.parquet"):
                (pi / n).write_text(n)
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True)
            (out / "meta.json").write_text("{}")
        if argv[:3] == ["dt-cloud", "rules", "-o"]:
            Path(argv[3]).write_text("{}")
        return r

    c.sh_impl, calls = run, runner.calls
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    st = f"{w}/stage/dvx"
    assert calls == [
        f"dt-cloud stage -o {w}/stage /gcs/dvx/listing/2026-10-16T1200/east/*.parquet /gcs/dvx/listing/2026-10-16T1200/eu/*.parquet "
        "/gcs/dvx/listing/2026-10-16T1200/east/_SUCCESS.json /gcs/dvx/listing/2026-10-16T1200/eu/_SUCCESS.json "
        "/gcs/dvx/attr/a.parquet /gcs/dvx/access/agg/*/*.parquet",
        f"dt-cloud path-index -d 2026-10-16T1200 -l {st}/listing/2026-10-16T1200/east/*.parquet -l {st}/listing/2026-10-16T1200/eu/*.parquet "
        f"-a {st}/attr/a.parquet -x {st}/access/agg/*/*.parquet -o {w}/snap -c <tmp>/mnt/dvx/listing/2026-10-16T1200/dir-cache "
        f"-P {w}/path-index/path-index.parquet -u bysize",
        "dt-cloud lifecycle pull -k -b gs://east -b gs://eu -o <tmp>/mnt/dvx/snapshots/2026-10-16T1200/lifecycle.json",
        f"dt-cloud rules -o {w}/rules.json",
    ]
    assert uploads == [(["path-index-bysize-by-user.parquet", "path-index-bysize.parquet", "path-index.parquet"],
                        "gs://dvx/listing/2026-10-16T1200/index/G1")]
    assert sorted(p.relative_to(mnt).as_posix() for p in (mnt / "snapshots").rglob("*") if p.is_file()) == [
        "snapshots/2026-10-16T1200/meta.json",
        "snapshots/rules.json",
    ]
    calls.clear()
    S.index(c, None)
    assert calls == []


def test_gcs_extra_steps(tmp_path, env, monkeypatch):
    c = gcs_ctx(tmp_path, env, monkeypatch, **{"access-ingest": {"args": ["-j", "8"]}, "index-sync": {"retain": 30}})
    c.env.update(SITE_TOKEN="t", SITE_URL="https://site.example.org", SLACK_BOT_TOKEN="s", SLACK_CHANNEL="C1",
                 CLOUDFLARE_API_TOKEN="cf", CLOUDFLARE_ACCOUNT_ID="acct")
    S.access_ingest(c, None)
    S.index_sync(c, None)
    S.healthcheck(c, None)
    S.digest(c, None)
    k = "listing/2026-10-16T1200/index/G1"
    assert c.sh_impl.calls == [
        "dt-cloud access ingest -j 8",
        f"dt-cloud index-sync -p -d <tmp>/mnt/dvx/{k} -g G1 -k {k} -M <tmp>/mnt/dvx/snapshots 2026-10-16T1200",
        "dt-cloud index-gc -r 30 2026-10-16T1200",
        "dt-cloud healthcheck -d 2026-10-16T1200",
        "dt-cloud digest -T gcs -C d.yml -P slack -r gs://dvx/snapshots",
    ]


def test_publish_r2_barrier(tmp_path, env, monkeypatch):
    opts = {"publish-r2": {"args": ["-S"], "require": ["{snapshots}/{scan}/meta.json", "{index}/attr.tsv"],
                           "keys": ["snapshots/rules.json"], "barrier": True}}
    c = gcs_ctx(tmp_path, env, monkeypatch, **opts)
    c.env.update(R2_ENDPOINT="https://r2.example.org", R2_BUCKET="idx")
    S.publish_r2(c, None)
    assert c.sh_impl.calls == [
        "dt-cloud publish-r2 -S -a -R snapshots/2026-10-16T1200/meta.json -R listing/2026-10-16T1200/index/G1/attr.tsv 2026-10-16T1200",
        "dt-cloud publish-r2 -a -p snapshots/rules.json -R snapshots/rules.json 2026-10-16T1200",
    ]
    failing = gcs_ctx(tmp_path, env, monkeypatch, **opts)
    failing.env.update(R2_ENDPOINT="https://r2.example.org", R2_BUCKET="idx")
    failing.sh_impl = Runner(tmp_path, fail=("dt-cloud publish-r2 -S",))
    with pytest.raises(S.StepFailed) as e:
        S.publish_r2(failing, None)
    assert (e.value.hard, failing.sh_impl.calls) == (True, [
        "dt-cloud publish-r2 -S -a -R snapshots/2026-10-16T1200/meta.json -R listing/2026-10-16T1200/index/G1/attr.tsv 2026-10-16T1200",
    ])


def test_a_hard_failure_fails_a_soft_step(tmp_path, monkeypatch):
    def boom(ctx, arg):
        raise S.StepFailed(3, "barrier", hard=True)

    monkeypatch.setitem(S.STEPS, "boom", S.Step("boom", boom, soft=True))
    monkeypatch.setattr(S, "load_profile_env", lambda env, path=None: profile(tmp_path))
    (tmp_path / "t").mkdir()
    env = {"SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "1", "TMPDIR": str(tmp_path / "t"), "HOME": str(tmp_path / "home")}
    assert S.run_step("boom", None, env) == 3


def test_interval_store_staged_append(tmp_path, env, monkeypatch):
    from dt_cloud import scan_runs

    ran = []

    def run_grouped(cmd, timeout, grace, session=None, signal_grace=None, env=None):
        ran.append((cmd, timeout, env["INTERVAL_STORE_PROFILE"], env["INTERVAL_STORE_IMAGE"]))
        return scan_runs.Ran(0, False, None)

    monkeypatch.setattr(scan_runs, "run_grouped", run_grouped)
    c = gcs_ctx(tmp_path, env, monkeypatch, **{"interval-store": {"profile": "/app/job/interval-store.json", "merge_wait": 1800, "timeout": "60m"}})
    c.env["JOB_IMAGE"] = "img@sha256:abc"
    S.interval_store(c, None)
    assert ran == [(["dt-cloud", "interval-store", "append", "-c", "-w", "1800", "2026-10-16T1200"], 3600.0,
                    "/app/job/interval-store.json", "img@sha256:abc")]


def test_lifecycle_pull_takes_the_s3_sources_endpoint(tmp_path, env, monkeypatch):
    """The publish step's `lifecycle pull` passes the S3 sources' `endpoint` (`-E`), not the sweep's `$SWEEP_S3_ENDPOINT`."""
    (tmp_path / "t").mkdir(exist_ok=True)
    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": str(tmp_path / "w"), "data": str(tmp_path / "d"),
               "layout": {"layer2": "l2/{scan}/", "snapshots": ""},
               "sources": [{"uri": "s3://a", "endpoint": "https://s3.example"}, {"uri": "s3://b", "endpoint": "https://s3.example"}],
               "skip": ["static-names", "digest"], "options": {"list": {"import": True}, "index": {"lifecycle": True}}})
    c = S.make_ctx(p, env)
    c.sh_impl = runner = Runner(tmp_path)
    web = tmp_path / "web"
    web.mkdir()
    S._publish(c, web)
    assert runner.calls == [f"dt-cloud lifecycle pull -k -E https://s3.example -b a -b b -o {tmp_path}/d/snapshots/2026-10-16T1200/lifecycle.json"
                            .replace(str(tmp_path), "<tmp>")]


def hf_profile(**kw):
    """hf's shape: one `hf://` source listed to the data store (its snapshot JSONs come from the listing), the one-machine
    static names on R2, and a Pages token beside the D1 one."""
    return dict(sources=[{"uri": "hf://ns", "threads": 3}],
                layout={"layer2": "listing/{scan}/", "snapshots": "hf", "listing": "raw/{scan}/"},
                options={"list": {"import_args": ["-e", "duckdb", "-n", "{nproc}", "-m", "-p", "storage_class_id"]},
                         "index": {"webdata": ["dt-cloud", "path-index", "-d", "{scan}", "-l", "{data}/raw/{scan}/ns/*.parquet", "-o", "{web}"],
                                   "args": ["-T", "{work}/duckdb-tmp"]},
                         "static-names": {"gen": "2026-10-15hf", "mem": "8GB", "build_args": ["-x", "16,8"]},
                         "digest": {"template": "cw", "config": "d.yml"}}, **kw)


def test_hf_list_then_index(tmp_path, env, monkeypatch):
    """The Hub lister takes no `-P`; `index` imports the listing from the data store, and the snapshot JSONs read it
    there (`{data}`)."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 4))
    c = ctx(tmp_path, env, **hf_profile())
    S.list_source(c, "hf://ns")
    (tmp_path / "data/raw/2026-10-16T1200/ns").mkdir(parents=True)
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert c.sh_impl.calls == [
        "disk-tree bulk-list hf://ns -o <tmp>/data/raw/2026-10-16T1200/ns -w 3 -x clear",
        "disk-tree import -e duckdb -n 4 -m -p storage_class_id -s hf -t 2026-10-16T00:00:00+00:00 -l <tmp>/data/raw/2026-10-16T1200/ns/shard-*.parquet -b ns",
        f"dt-cloud path-index -d 2026-10-16T1200 -l <tmp>/data/raw/2026-10-16T1200/ns/*.parquet -o {w}/web",
        f"dt-cloud index-write -t 4 -o {w}/index -T {w}/duckdb-tmp ns={w}/l2/ns/scans/u1.parquet",
    ]
    assert sorted(p.relative_to(tmp_path / "data").as_posix() for p in (tmp_path / "data").rglob("*") if p.is_file()) == [
        "listing/2026-10-16T1200/index/G1/age-pyramid-1.parquet",
        "listing/2026-10-16T1200/index/G1/path-index-bysize.parquet",
        "listing/2026-10-16T1200/index/G1/path-index.parquet",
        "listing/2026-10-16T1200/ns.parquet",
        "snapshots/hf/2026-10-16T1200/meta.json",
    ]


@pytest.mark.parametrize("has_gen, sub", [
    (True, ["add"]),
    (False, ["build"]),
])
def test_static_names_one_machine(tmp_path, env, monkeypatch, has_gen, sub):
    """`options.static-names.gen`: `static-names add` in the task, the data store's scans into R2's generation; `build`
    (+ `build_args`) when R2 has none."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 4))
    asked = []
    monkeypatch.setattr(S.Ctx, "store_has", lambda self, root, key: asked.append((root, key)) or has_gen)
    env.update(R2_ENDPOINT="https://r2.example.org", R2_BUCKET="r2b", R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s")
    c = ctx(tmp_path, env, **hf_profile())
    S.static_names(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    tail = [] if has_gen else ["-x", "16,8"]
    assert (asked, c.sh_impl.calls) == ([("r2://r2b", "static-names/2026-10-15hf/scans.json")], [" ".join([
        "dt-cloud static-names", *sub, "-g 2026-10-15hf -L listing/{id}/index/{gen}/path-index.parquet -s <tmp>/data",
        f"-o r2://r2b -w {w}/static-names -p 4 -M 8GB", *tail])])


def test_static_names_profile_checks():
    """One path or the other: the staged profile, or the one-machine `gen`; their keys don't mix."""
    from dt_cloud.scan_profile import ProfileError

    def problems(opts: dict) -> str:
        d = hf_profile()
        with pytest.raises(ProfileError) as e:
            parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "/d",
                   **d, "options": {**d["options"], "static-names": opts}})
        return str(e.value)
    assert [problems(o) for o in ({"gen": "g", "profile": "p.json"}, {"profile": "p.json", "mem": "8GB"}, {})] == [
        "profile: options.static-names.profile: not with gen (the one-machine path)",
        "profile: options.static-names.mem: only with gen (the one-machine path)",
        "profile: options.static-names.profile: required (the deployment's static-names profile; or env STATIC_NAMES_PROFILE, "
        "or gen: the one-machine path, or skip: [static-names])",
    ]


def test_digest_uploads_with_the_pages_token(tmp_path, env):
    """A deployment whose D1 token can't deploy Pages (hf) passes `CF_PAGES_TOKEN`: the digest's wrangler gets it as
    `CLOUDFLARE_API_TOKEN`; without it, the ambient one."""
    env.update(SLACK_BOT_TOKEN="x", SLACK_CHANNEL="C1", CLOUDFLARE_API_TOKEN="d1", CF_PAGES_TOKEN="pages\n")
    c = ctx(tmp_path, env, secrets={"CF_PAGES_TOKEN": "pages-secret"}, **hf_profile())
    S.digest(c, None)
    del env["CF_PAGES_TOKEN"]
    S.digest(c, None)
    assert [(cmd, e["CLOUDFLARE_API_TOKEN"]) for cmd, e in zip(c.sh_impl.calls, c.sh_impl.envs)] == [
        ("dt-cloud digest -T cw -C d.yml -P slack -r <tmp>/data/snapshots/hf", "pages"),
        ("dt-cloud digest -T cw -C d.yml -P slack -r <tmp>/data/snapshots/hf", "d1"),
    ]


def test_digest_root_from_the_profile(tmp_path, env):
    """`options.digest.root` (`{snapshots}` filled) is the snapshots root the digest reads and keeps its thread state
    beside (r2's live state is on R2, its data store on GCS); unset, the data store's."""
    env.update(SLACK_BOT_TOKEN="x", SLACK_CHANNEL="C1")
    hf = hf_profile()
    c = ctx(tmp_path, env, **{**hf, "options": {**hf["options"], "digest": {**hf["options"]["digest"], "root": "r2://idx/{snapshots}"}}})
    S.digest(c, None)
    assert c.sh_impl.calls == ["dt-cloud digest -T cw -C d.yml -P slack -r r2://idx/snapshots/hf"]


def meta_ctx(tmp_path, env, **opts):
    """A second store's profile (`store: meta`): the steps that serve it, and the ones that can't skipped."""
    return ctx(tmp_path, env, store="meta", skip=["interval-store", "warm-cache", "probe"],
               options={"list": {"import": True}, "static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "c.yml"}, **opts})


def test_store_passes_through(tmp_path, env):
    """`store: meta`: every command that files rows per store gets `-s meta` (both index-sync halves, the per-scan and
    the files index-gc, the over-time groups); `index-sync.variants` reach both halves; `index-gc.dry` lists only."""
    env.update(CLOUDFLARE_API_TOKEN="tok", CLOUDFLARE_ACCOUNT_ID="acct", R2_ENDPOINT="https://r2.example.org", R2_BUCKET="r2b",
               R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s")
    c = meta_ctx(tmp_path, env, **{"index-sync": {"variants": ["path", "age-pyramid-1h"], "retain": 7}, "index-gc": {"dry": True, "grace": "36h"}})
    for f in (S.index_rows, S.index_sync, S.over_time, S.index_gc):
        f(c, None)
    k = "l2/2026-10-16T1200/index/G1"
    assert c.sh_impl.calls == [
        f"dt-cloud index-sync -r -s meta -d <tmp>/data/{k} -g G1 -k {k} -v path -v age-pyramid-1h 2026-10-16T1200",
        f"dt-cloud index-sync -p -s meta -d <tmp>/data/{k} -g G1 -k {k} -v path -v age-pyramid-1h -M <tmp>/data/snapshots/sub 2026-10-16T1200",
        "dt-cloud index-gc -s meta -r 7 2026-10-16T1200",
        "dt-cloud over-time-groups -s meta -g G1 -o <tmp>/t/dt-scan/2026-10-16T1200/over-time -p r2://r2b",
        "dt-cloud index-gc -s meta -R -n -m 36h -F <tmp>/data -F r2",
    ]


def test_index_gc_dry_on_the_primary(tmp_path, env):
    env.update(CLOUDFLARE_API_TOKEN="tok", CLOUDFLARE_ACCOUNT_ID="acct")
    c = ctx(tmp_path, env, options={"list": {"import": True}, "static-names": {"profile": "sn.json"},
                                    "digest": {"template": "t", "config": "c.yml"}, "index-gc": {"dry": True}})
    S.index_gc(c, None)
    assert c.sh_impl.calls == ["dt-cloud index-gc -R -n -m 2d -F <tmp>/data"]


def test_the_run_records_into_the_profiles_store(tmp_path, env, monkeypatch):
    """A task's phases upsert the head's run row, store included: a second store's profile names it, over the scan-runs
    profile's default; the primary's leaves that profile's as is."""
    runs = tmp_path / "scan-runs.json"
    runs.write_text('{"store": "primary"}')
    monkeypatch.setenv("SCAN_RUNS_PROFILE", str(runs))
    env.update(SCAN_RUNS_PROFILE=str(runs), DT_RUN_ID="run-1")
    assert (meta_ctx(tmp_path, dict(env)).run.profile.store, ctx(tmp_path, dict(env)).run.profile.store) == ("meta", "primary")


def test_every_bound_secret_is_stripped(tmp_path, env):
    """A step's env strips each ENV any call binds (profile, step, source), not only the profile-wide ones."""
    env.update(AWS_ACCESS_KEY_ID=" src\n", PAGES="p\n", OTHER=" kept\n")
    c = ctx(tmp_path, env, secrets={"CLOUDFLARE_API_TOKEN": "cf"},
            sources=[{"uri": "s3://a", "secrets": {"AWS_ACCESS_KEY_ID": "src-key"}}, "s3://b"],
            options={"list": {"import": True}, "static-names": {"profile": "sn.json"},
                     "digest": {"template": "t", "config": "c.yml", "secrets": {"PAGES": "pages-token"}}})
    assert {k: c.env[k] for k in ("AWS_ACCESS_KEY_ID", "PAGES", "OTHER")} == {"AWS_ACCESS_KEY_ID": "src", "PAGES": "p", "OTHER": " kept\n"}


def test_path_index_age_pyramids_and_verify(tmp_path, env, monkeypatch):
    """`options.index.age_pyramids` + `verify` under path-index: the age tiers from the `path` sort (checked), then the
    index checked against the staged listings, `verification.json` beside the tiers; all uploaded with
    `path-index.parquet` last."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 8))
    (tmp_path / "t").mkdir(exist_ok=True)
    mnt = tmp_path / "mnt/dvx"
    for b in ("east", "eu"):
        (mnt / f"listing/2026-10-16T1200/{b}").mkdir(parents=True)
        (mnt / f"listing/2026-10-16T1200/{b}/_SUCCESS.json").write_text("{}")
    uploads = []
    monkeypatch.setattr(S, "upload", lambda files, dest: uploads.append(([f.name for f in files], dest)))
    p = parse({"deployment": "fix", "executor": {"kind": "google-batch", "project": "p", "region": "r", "image": "i"},
               "work": "gs://dvx/nf", "data": "gs://dvx", "store": "meta", "skip": ["interval-store", "warm-cache", "probe"],
               "layout": {"layer2": "meta-l2/{scan}/", "snapshots": "meta", "listing": "listing/{scan}/"},
               "sources": ["gcs://east", "gcs://eu"],
               "options": {"index": {"mode": "path-index", "args": ["-U"], "mem": "12GB", "age_pyramids": True, "verify": True},
                           "static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "d.yml"}}})
    c = S.make_ctx(p, {**env, "DT_DATA_MOUNT": str(tmp_path / "mnt")})
    runner = Runner(tmp_path)

    def run(argv, env, stdout=None, text=True):
        r = runner(argv, env, stdout, text)
        if argv[:2] == ["dt-cloud", "path-index"]:
            pi = Path(argv[argv.index("-P") + 1]).parent
            for n in ("path-index.parquet", "path-index-bysize.parquet"):
                (pi / n).write_text(n)
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True)
            (out / "meta.json").write_text("{}")
        if argv[:2] == ["dt-cloud", "age-pyramid"]:
            (Path(argv[argv.index("-o") + 1]) / "age-pyramid-1h.parquet").write_text("1h")
        if argv[:2] == ["dt-cloud", "index-verify"]:
            Path(argv[argv.index("-o") + 1]).write_text("{}")
        return r

    c.sh_impl = run
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    st = f"{w}/stage/dvx/listing/2026-10-16T1200"
    assert runner.calls == [
        f"dt-cloud stage -o {w}/stage /gcs/dvx/listing/2026-10-16T1200/east/*.parquet /gcs/dvx/listing/2026-10-16T1200/eu/*.parquet "
        "/gcs/dvx/listing/2026-10-16T1200/east/_SUCCESS.json /gcs/dvx/listing/2026-10-16T1200/eu/_SUCCESS.json",
        f"dt-cloud path-index -d 2026-10-16T1200 -l {st}/east/*.parquet -l {st}/eu/*.parquet -o {w}/snap "
        f"-c <tmp>/mnt/dvx/listing/2026-10-16T1200/dir-cache -P {w}/path-index/path-index.parquet -U",
        f"dt-cloud age-pyramid -c -m 12GB -t 8 -o {w}/path-index {w}/path-index/path-index.parquet",
        f"dt-cloud index-verify -m 12GB -t 8 -d 2026-10-16T1200 -g G1 -b east -b eu -l {st}/east/*.parquet -l {st}/eu/*.parquet "
        f"-s {w}/snap -o {w}/path-index/verification.json {w}/path-index/path-index.parquet",
    ]
    assert uploads == [(["age-pyramid-1h.parquet", "path-index-bysize.parquet", "verification.json", "path-index.parquet"],
                        "gs://dvx/meta-l2/2026-10-16T1200/index/G1")]


def test_store_has_reads_with_the_steps_credentials(tmp_path, env, monkeypatch):
    """An in-process store read sees the step's `AWS_*` (`r2_profile`'s credentials file) as `ctx.sh`'s subprocesses do,
    and `os.environ` is restored after (r2, 2026-10-17T0311: `ProfileNotFound: … (r2)` from `static-names`)."""
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    monkeypatch.setenv("AWS_PROFILE", "outer")
    seen = []

    class Store:
        def __init__(self, root):
            self.root = root

        def exists(self, key):
            seen.append((self.root, key, os.environ.get("AWS_SHARED_CREDENTIALS_FILE"), os.environ.get("AWS_PROFILE")))
            return True

    import dt_cloud.blobstore as B
    monkeypatch.setattr(B, "Store", Store)
    c = ctx(tmp_path, {**env, "AWS_SHARED_CREDENTIALS_FILE": "/t/r2-credentials", "AWS_PROFILE": "step"})
    assert c.store_has("r2://idx", "static-names/G/scans.json") is True
    assert seen == [("r2://idx", "static-names/G/scans.json", "/t/r2-credentials", "step")]
    assert (os.environ.get("AWS_SHARED_CREDENTIALS_FILE"), os.environ.get("AWS_PROFILE")) == (None, "outer")
