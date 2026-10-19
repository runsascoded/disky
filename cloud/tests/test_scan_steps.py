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
            scans = Path(env["DISKY_ROOT"]) / "scans"
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
        if argv[:2] == ["dt-cloud", "dups"] and key not in self.fail:
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True, exist_ok=True)
            for n in ("dups.parquet", "dups.json"):
                (out / n).write_text(n)
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
        "dt-cloud publish-r2 -b <tmp>/data -a 2026-10-16T1200",
        f"dt-cloud index-sync -r -b r2://r2b -d <tmp>/data/{k} -g G1 -k {k} 2026-10-16T1200",
        f"dt-cloud index-sync -p -b r2://r2b -d <tmp>/data/{k} -g G1 -k {k} -M <tmp>/data/snapshots/sub 2026-10-16T1200",
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


def test_interval_store_adds_from_the_store_or_the_data_store(tmp_path, env, monkeypatch):
    """The one-machine `add` reads the scans' sorts from the store by default, and from the data store with
    `source: data` (so `publish-r2 -S` needn't copy them; specs/lean-scans.md §1.1); a bad `source` is refused."""
    from dt_cloud import scan_runs

    ran = []
    monkeypatch.setattr(scan_runs, "run_grouped", lambda cmd, timeout, grace, env=None, **kw: (ran.append(" ".join(cmd)), scan_runs.Ran(0, False, None))[1])
    env["R2_ARCHIVE"] = "r2://idx"
    for source in ({}, {"source": "data"}):
        c = gcs_ctx(tmp_path, dict(env), monkeypatch, **{"interval-store": {"mem": "12GB", **source}})
        c.sh_impl = Runner(tmp_path, stdout={"dt-cloud interval-store current": "g1\n"})
        S.interval_store(c, None)
    add = "dt-cloud interval-store add -L listing/{id}/index/{gen}/path-index.parquet -s {src} -o r2://idx -w <tmp>/t/dt-scan/2026-10-16T1200/interval-store -M 12GB -p 32"
    assert [r.replace(str(tmp_path), "<tmp>") for r in ran] == [add.replace("{src}", "r2://idx"), add.replace("{src}", "gs://dvx")]
    with pytest.raises(ValueError) as e:
        gcs_ctx(tmp_path, dict(env), monkeypatch, **{"interval-store": {"source": "r2"}})
    assert str(e.value) == "profile: options.interval-store.source: 'r2' (one of store, data)"


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
    """`options.static-names.gen`: `static-names add` in the task, the scans into R2's generation; `build` (+
    `build_args`) when R2 has none. On a local data dir (this one's), which holds only this scan, the scans are read
    from R2 itself, where publish-r2 put this one first."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 4))
    asked = []
    monkeypatch.setattr(S.Ctx, "store_has", lambda self, root, key: asked.append((root, key)) or has_gen)
    env.update(R2_ENDPOINT="https://r2.example.org", R2_BUCKET="r2b", R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s")
    c = ctx(tmp_path, env, **hf_profile())
    S.static_names(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    tail = [] if has_gen else ["-x", "16,8"]
    assert (asked, c.sh_impl.calls) == ([("r2://r2b", "static-names/2026-10-15hf/scans.json")], [" ".join([
        "dt-cloud static-names", *sub, "-g 2026-10-15hf -L listing/{id}/index/{gen}/path-index.parquet -s r2://r2b",
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


class Ends:
    """A fake run: records each phase's begin and end (name, rc, warn, msg)."""

    def __init__(self):
        self.calls = []

    def begin(self, name, began=None, gen=None):
        self.calls.append(("begin", name))

    def end(self, name, rc=0, warn=False, msg=None, note=None, **kw):
        self.calls.append(("end", name, rc, warn, msg))
        return 0 if warn else rc


def test_digest_failure_fails_its_phase(tmp_path, env):
    """A failed digest (cw's under Nextflow: its plot path resolved under the task's cwd) never fails the run, but its
    phase ends failed with a warn alert, not done: /scans showed it done and the missed digest went unnoticed. The
    next platform still runs, and a clean run ends the phase done."""
    env.update(SLACK_BOT_TOKEN="x", SLACK_CHANNEL="C1", DISCORD_GCS_USAGE_WEBHOOK="w", DISCORD_BOT_TOKEN="b")
    hf = hf_profile()
    kw = {**hf, "options": {**hf["options"], "digest": {**hf["options"]["digest"], "platforms": ["slack", "discord"]}}}
    c = ctx(tmp_path, env, Runner(tmp_path, fail=("dt-cloud digest -T",)), **kw)
    c.run = Ends()
    S.digest(c, None)
    c.sh_impl, ok = Runner(tmp_path), c.run
    S.digest(c, None)
    assert (ok.calls, c.sh_impl.calls, c.failure) == ([
        ("begin", "digest"), ("end", "digest", 1, True, "slack usage-digest step failed"),
        ("begin", "digest"), ("end", "digest", 0, False, None),
    ], [
        "dt-cloud digest -T cw -C d.yml -P slack -r <tmp>/data/snapshots/hf",
        "dt-cloud digest -T cw -C d.yml -P discord -r <tmp>/data/snapshots/hf",
    ], None)


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
        f"dt-cloud index-sync -r -s meta -b r2://r2b -d <tmp>/data/{k} -g G1 -k {k} -v path -v age-pyramid-1h 2026-10-16T1200",
        f"dt-cloud index-sync -p -s meta -b r2://r2b -d <tmp>/data/{k} -g G1 -k {k} -v path -v age-pyramid-1h -M <tmp>/data/snapshots/sub 2026-10-16T1200",
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


def test_list_r2_source_defaults_to_the_deployments_endpoint(tmp_path, env, monkeypatch):
    """An `r2://` source without `endpoint` lists through `$R2_ENDPOINT` (the deployment's own R2, a secret where the
    profile is public: hf's meta store lists its serving bucket); an explicit `endpoint` wins."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 2))
    sources = [{"uri": "r2://idx"}, {"uri": "r2://other", "endpoint": "https://other.example"}]
    c = ctx(tmp_path, {**env, "R2_ENDPOINT": "https://r2.example"}, sources=sources)
    S.list_source(c, "r2://idx")
    S.list_source(c, "r2://other")
    assert c.sh_impl.calls == [
        "disk-tree bulk-list -a r2://idx -E https://r2.example -o <tmp>/t/dt-scan/2026-10-16T1200/listing/idx -P 2 -w 8 -x clear",
        "disk-tree import -e stream -j 2 -m -s r2 -t 2026-10-16T00:00:00+00:00 -l <tmp>/t/dt-scan/2026-10-16T1200/listing/idx/shard-*.parquet -b idx",
        "disk-tree bulk-list -a r2://other -E https://other.example -o <tmp>/t/dt-scan/2026-10-16T1200/listing/other -P 2 -w 8 -x clear",
        "disk-tree import -e stream -j 2 -m -s r2 -t 2026-10-16T00:00:00+00:00 -l <tmp>/t/dt-scan/2026-10-16T1200/listing/other/shard-*.parquet -b other",
    ]


def test_retire_dry_run_by_default_then_each_copy(tmp_path, env):
    """`retire` (listed in `steps`): the data store with D1 (when the CF creds are set) and the backup, then each copy
    without either; a dry run unless `execute`. The layout is the other steps' `<layer2>index/{gen}/path-index.parquet`."""
    env.update(CLOUDFLARE_API_TOKEN="tok", CLOUDFLARE_ACCOUNT_ID="acct", R2_ARCHIVE="r2://idx")
    opts = {"list": {"import": True}, "static-names": {"gen": "s1"}, "digest": {"template": "t", "config": "c.yml"}}
    c = ctx(tmp_path, env, data="gs://d", steps=["retire"], options={**opts, "retire": {"keep": 3, "layer2": True, "copies": ["r2"], "backup": "gs://bak"}})
    S.retire(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200/retire"
    common = f"-G s1 -k 3 -L l2/{{id}}/index/{{gen}}/path-index.parquet -l l2/{{id}}/*.parquet -o r2://idx -S r2://idx -w {w}"
    assert c.sh_impl.calls == [
        f"dt-cloud scan retire -b gs://bak {common} -s gs://d",
        f"dt-cloud scan retire {common} -R -D -s r2://idx",
    ]
    x = ctx(tmp_path, env, data="gs://d", steps=["retire"], options={**opts, "retire": {"keep": 2, "execute": True, "markers_only": True, "store": "gs://iv"}})
    S.retire(x, None)
    assert x.sh_impl.calls == [f"dt-cloud scan retire -G s1 -k 2 -L l2/{{id}}/index/{{gen}}/path-index.parquet -m -o gs://iv -S r2://idx -w {w} -x -s gs://d"]


def test_retire_local_data_retires_the_archive(tmp_path, env):
    """A local data store (m3's: the task's ephemeral disk, R2 the store of record) holds only this scan: the archive is
    the first root, with D1, and a `copies: [r2]` is that same root, not a second pass."""
    env.update(CLOUDFLARE_API_TOKEN="tok", CLOUDFLARE_ACCOUNT_ID="acct", R2_ARCHIVE="r2://idx")
    opts = {"list": {"import": True}, "static-names": {"gen": "s1"}, "digest": {"template": "t", "config": "c.yml"}, "retire": {"keep": 7, "copies": ["r2"]}}
    c = ctx(tmp_path, env, steps=["retire"], options=opts)
    S.retire(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200/retire"
    assert c.sh_impl.calls == [f"dt-cloud scan retire -G s1 -k 7 -L l2/{{id}}/index/{{gen}}/path-index.parquet -o r2://idx -S r2://idx -w {w} -s r2://idx"]


NL_OPTS = {"list": {"import": True}, "static-names": {"gen": "s1"}, "digest": {"template": "t", "config": "c.yml"}}


def test_non_live_measures_each_bucket_source(tmp_path, env):
    """`non-live` (listed in `steps`): `dt-cloud non-live` per bucket source with the profile's classes, into
    `<layer2>non-live/<bucket>.parquet`; an `r2://` source through `$R2_ENDPOINT`; an `hf://` one skipped (its held bytes
    are in its listing); no classes, nothing run."""
    sources = [{"uri": "s3://a", "endpoint": "https://s3.example.org"}, "r2://b", "hf://ns"]
    c = ctx(tmp_path, {**env, "R2_ENDPOINT": "https://r2.example"}, steps=["non-live"], sources=sources,
            options={"non-live": {"classes": ["mpu"]}, **NL_OPTS})
    S.non_live(c, None)
    out = "<tmp>/data/l2/2026-10-16T1200/non-live"
    snap = "<tmp>/data/snapshots/sub/2026-10-16T1200/non-live.json"
    assert c.sh_impl.calls == [
        f"dt-cloud non-live -c mpu -E https://s3.example.org -j {snap} -o {out}/a.parquet s3://a",
        f"dt-cloud non-live -c mpu -E https://r2.example -j {snap} -o {out}/b.parquet r2://b",
    ]
    n = ctx(tmp_path, env, steps=["non-live"], options={"non-live": {"classes": []}, **NL_OPTS})
    S.non_live(n, None)
    assert n.sh_impl.calls == []


def test_non_live_passes_each_source_its_providers_classes(tmp_path, env):
    """A mixed deployment (hf's meta: a GCS data bucket + an R2 bucket) lists the union: each source measures only the
    classes its provider has, and one with none (R2 asked for `soft-deleted` only) is skipped, never refused per run."""
    sources = ["gcs://data", "r2://idx", {"uri": "s3://v", "endpoint": "https://s3.example.org"}]
    c = ctx(tmp_path, {**env, "R2_ENDPOINT": "https://r2.example"}, steps=["non-live"], sources=sources,
            options={"non-live": {"classes": ["soft-deleted", "delete-marker"]}, **NL_OPTS})
    S.non_live(c, None)
    out = "<tmp>/data/l2/2026-10-16T1200/non-live"
    snap = "<tmp>/data/snapshots/sub/2026-10-16T1200/non-live.json"
    snap = "<tmp>/data/snapshots/sub/2026-10-16T1200/non-live.json"
    assert c.sh_impl.calls == [
        f"dt-cloud non-live -c soft-deleted -j {snap} -o {out}/data.parquet gcs://data",
        f"dt-cloud non-live -c delete-marker -E https://s3.example.org -j {snap} -o {out}/v.parquet s3://v",
    ]


def test_dups_reads_the_size_sort_and_writes_beside_the_index(tmp_path, env):
    """`dups` (listed in `steps`): `dt-cloud dups` over the scan's `bysize` sort (else its `path` sort) with the
    profile's options, both files copied beside the index (`dups.json` the skip mark); a second run skips."""
    key = tmp_path / "data/l2/2026-10-16T1200/index/G1"
    key.mkdir(parents=True)
    (key / "path-index.parquet").write_text("p")
    (key / "path-index-bysize.parquet").write_text("b")
    c = ctx(tmp_path, env, steps=["dups"], options={**NL_OPTS, "dups": {"floor": 1024, "check": 50, "mem": "8GB"}})
    S.dups(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert c.sh_impl.calls == [f"dt-cloud dups -d 2026-10-16T1200 -f 1024 -g 50 -m 8GB -T {w}/dups-tmp -o {w}/dups "
                               "<tmp>/data/l2/2026-10-16T1200/index/G1/path-index-bysize.parquet"]
    assert sorted(f.name for f in key.iterdir()) == ["dups.json", "dups.parquet", "path-index-bysize.parquet", "path-index.parquet"]
    again = ctx(tmp_path, env, steps=["dups"], options=NL_OPTS)
    S.dups(again, None)
    assert again.sh_impl.calls == []


def test_dups_falls_back_to_the_path_sort_and_never_fails_the_run(tmp_path, env):
    key = tmp_path / "data/l2/2026-10-16T1200/index/G1"
    key.mkdir(parents=True)
    (key / "path-index.parquet").write_text("p")
    c = ctx(tmp_path, env, Runner(tmp_path, fail=("dt-cloud dups -d",)), steps=["dups"], options=NL_OPTS)
    S.dups(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert c.sh_impl.calls == [f"dt-cloud dups -d 2026-10-16T1200 -T {w}/dups-tmp -o {w}/dups <tmp>/data/l2/2026-10-16T1200/index/G1/path-index.parquet"]
    assert sorted(f.name for f in key.iterdir()) == ["path-index.parquet"]


def test_retire_layer1_passes_the_listing_layouts_shards(tmp_path, env):
    """`layer1: true`: the raw listing shards under `layout.listing` (`-1`); without a `layout.listing`, none (warned)."""
    opts = {"static-names": {"gen": "s1"}, "digest": {"template": "t", "config": "c.yml"}, "retire": {"keep": 2, "layer1": True, "store": "gs://iv"}}
    c = ctx(tmp_path, env, steps=["retire"], layout={"layer2": "l2/{scan}/", "snapshots": "sub", "listing": "listing/{scan}/"}, options=opts)
    S.retire(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200/retire"
    assert c.sh_impl.calls == [f"dt-cloud scan retire -G s1 -k 2 -L l2/{{id}}/index/{{gen}}/path-index.parquet -1 listing/{{id}}/*/shard-*.parquet "
                               f"-o gs://iv -S gs://iv -w {w} -R -s <tmp>/data"]
    n = ctx(tmp_path, env, steps=["retire"], options={**opts, "list": {"import": True}})
    S.retire(n, None)
    assert n.sh_impl.calls == [f"dt-cloud scan retire -G s1 -k 2 -L l2/{{id}}/index/{{gen}}/path-index.parquet -o gs://iv -S gs://iv -w {w} -R -s <tmp>/data"]


def test_store_gc_collects_the_interval_stores_lineage_on_each_copy(tmp_path, env):
    """`store-gc` (listed in `steps`): `store-gc -t interval-store -l` on the interval store and on `$R2_ARCHIVE` when
    that's another copy; a dry run unless `execute`."""
    opts = {**NL_OPTS, "interval-store": {"store": "gs://iv"}}
    c = ctx(tmp_path, {**env, "R2_ARCHIVE": "r2://idx"}, steps=["store-gc"], options={**opts, "store-gc": {"grace": 60}})
    S.store_gc(c, None)
    assert c.sh_impl.calls == ["dt-cloud store-gc -t interval-store -l -G 60 -o gs://iv", "dt-cloud store-gc -t interval-store -l -G 60 -o r2://idx"]
    x = ctx(tmp_path, env, steps=["store-gc"], options={**opts, "store-gc": {"execute": True}})
    S.store_gc(x, None)
    assert x.sh_impl.calls == ["dt-cloud store-gc -t interval-store -l -x -o gs://iv"]


def test_retire_failure_warns_and_needs_its_keep(tmp_path, env):
    c = ctx(tmp_path, env, runner=Runner(tmp_path, fail=("dt-cloud scan retire",)), steps=["retire"],
            options={"list": {"import": True}, "static-names": {"gen": "s1", "store": "r2://idx"}, "digest": {"template": "t", "config": "c.yml"},
                     "retire": {"keep": 2, "store": "r2://idx"}})
    S.retire(c, None)
    assert c.sh_impl.calls == ["dt-cloud scan retire -G s1 -k 2 -L l2/{id}/index/{gen}/path-index.parquet -o r2://idx -S r2://idx "
                               "-w <tmp>/t/dt-scan/2026-10-16T1200/retire -R -s <tmp>/data"]
    with pytest.raises(ValueError) as e:
        profile(tmp_path, steps=["retire"], options={"list": {"import": True}, "static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "c.yml"}, "retire": {}})
    assert str(e.value).splitlines()[1:] == [
        "  options.retire.keep: required, ≥ 1 (the newest scans never retired)",
        "  options.retire.static_gen: required unless options.static-names.gen names the static index's generation",
    ]


class AggRunner(Runner):
    """`Runner`, and `access r2-agg` writes its `-o` (unless `empty`: a collector with no reads before the scan)."""

    def __init__(self, tmp: Path, empty: bool = False, **kw):
        super().__init__(tmp, **kw)
        self.empty = empty

    def __call__(self, argv, env, stdout=None, text=True):
        if argv[:3] == ["dt-cloud", "access", "r2-agg"] and not self.empty:
            out = Path(argv[argv.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"agg as of {argv[argv.index('-d') + 1]}")
        return super().__call__(argv, env, stdout, text)


R2_BASE = {"list": {"import": True}, "static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "c.yml"}}


def r2_ctx(tmp_path, env, runner=None):
    c = ctx(tmp_path, env, runner or AggRunner(tmp_path), steps=["access-r2"], options={
        **R2_BASE, "access-r2": {"state": "r2://logs/state.json"}})
    c.env["R2_ENDPOINT"] = "https://acct.r2.example.org"
    return c


def test_access_r2_aggregates_the_collectors_hours_to_the_scan_into_one_key(tmp_path, env):
    """`r2-agg -s` (the collector's state names each hour's current aggregate) cut at the scan, written to the task's
    work dir and then over the data store's one key (the aggregate is cumulative: every read before the scan)."""
    c = r2_ctx(tmp_path, env)
    S.access_r2(c, None)
    assert c.sh_impl.calls == ["dt-cloud access r2-agg -d 2026-10-16T1200 -o <tmp>/t/dt-scan/2026-10-16T1200/access-r2/agg.parquet -s r2://logs/state.json"]
    assert c.sh_impl.envs[0]["DISK_TREE_R2_ENDPOINT_URL"] == "https://acct.r2.example.org"
    assert sorted(p.relative_to(tmp_path / "data").as_posix() for p in (tmp_path / "data").rglob("*") if p.is_file()) == ["access/r2/agg.parquet"]
    assert (tmp_path / "data/access/r2/agg.parquet").read_text() == "agg as of 2026-10-16T1200"


def test_access_r2_keeps_the_last_aggregate_when_there_is_nothing_new_or_it_fails(tmp_path, env):
    """No reads before the scan (nothing written), or a failure: the earlier aggregate stays (valid as of its scan), and
    the step never fails the run."""
    key = tmp_path / "data/access/r2/agg.parquet"
    key.parent.mkdir(parents=True)
    key.write_text("agg as of 2026-10-15T1200")
    S.access_r2(r2_ctx(tmp_path, env, AggRunner(tmp_path, empty=True)), None)
    S.access_r2(r2_ctx(tmp_path, env, AggRunner(tmp_path, fail=("dt-cloud access r2-agg",))), None)
    assert key.read_text() == "agg as of 2026-10-15T1200"


def test_access_r2_needs_the_state(tmp_path):
    with pytest.raises(ValueError) as e:
        profile(tmp_path, steps=["access-r2"], options={**R2_BASE, "access-r2": {}})
    assert str(e.value) == "profile: options.access-r2.state: required (the collector archive's `state.json`, e.g. `r2://<archive>/state.json`)"


def test_cpu_quota_reads_cgroup_v2_then_v1(tmp_path) -> None:
    v2, v1, none, unlimited = (tmp_path / d for d in ("v2", "v1", "none", "max"))
    (v1 / "cpu").mkdir(parents=True)
    for d in (v2, none, unlimited):
        d.mkdir()
    (v2 / "cpu.max").write_text("150000 100000\n")
    (unlimited / "cpu.max").write_text("max 100000\n")
    (v1 / "cpu/cpu.cfs_quota_us").write_text("200000\n")
    (v1 / "cpu/cpu.cfs_period_us").write_text("100000\n")
    assert [S.cpu_quota(d) for d in (v2, v1, none, unlimited)] == [2, 2, None, None]


def test_nproc_is_capped_by_the_cgroup_quota(tmp_path, monkeypatch) -> None:
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    monkeypatch.setattr(S.os, "sched_getaffinity", lambda _: set(range(8)), raising=False)
    capped = S.Ctx.__new__(S.Ctx)
    capped.env = {"DT_CGROUP": str(tmp_path)}
    free = S.Ctx.__new__(S.Ctx)
    free.env = {"DT_CGROUP": str(tmp_path / "absent")}
    assert [capped.nproc, free.nproc] == [2, 8]


def test_path_index_reproc_rebuilds_a_missing_listing_from_the_layer_2(tmp_path, env, monkeypatch):
    """REPROC (`scan head -R`) of a scan whose listing is gone but whose layer-2 is kept (a meta store's older scans):
    that bucket's listing is rebuilt from the layer-2 (`dt-cloud l2-listing`) on local disk and read from there, not
    staged; a bucket with its listing reads it as always. Not under REPROC, a missing listing still fails."""
    mnt = tmp_path / "mnt/dvx"
    (mnt / "listing/2026-10-16T1200/east").mkdir(parents=True)
    (mnt / "listing/2026-10-16T1200/east/_SUCCESS.json").write_text("{}")
    (mnt / "listing/2026-10-16T1200/eu.parquet").write_text("l2 of eu")
    monkeypatch.setattr(S, "upload", lambda files, dest: None)
    c = gcs_ctx(tmp_path, {**env, "REPROC": "1"}, monkeypatch, index={"mode": "path-index"})
    runner = c.sh_impl

    def run(argv, env, stdout=None, text=True):
        r = runner(argv, env, stdout, text)
        if argv[:2] == ["dt-cloud", "path-index"]:
            (Path(argv[argv.index("-P") + 1]).parent / "path-index.parquet").write_text("")
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True)
            (out / "meta.json").write_text("{}")
        return r

    c.sh_impl = run
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert runner.calls == [
        f"dt-cloud l2-listing -b eu <tmp>/mnt/dvx/listing/2026-10-16T1200/eu.parquet {w}/l2-listing/eu/shard-00000.parquet",
        f"dt-cloud stage -o {w}/stage /gcs/dvx/listing/2026-10-16T1200/east/*.parquet /gcs/dvx/listing/2026-10-16T1200/east/_SUCCESS.json",
        f"dt-cloud path-index -d 2026-10-16T1200 -l {w}/stage/dvx/listing/2026-10-16T1200/east/*.parquet -l {w}/l2-listing/eu/*.parquet "
        f"-o {w}/snap -c <tmp>/mnt/dvx/listing/2026-10-16T1200/dir-cache -P {w}/path-index/path-index.parquet",
    ]
    fresh = gcs_ctx(tmp_path, env, monkeypatch, index={"mode": "path-index"})
    with pytest.raises(S.StepFailed):
        S.index(fresh, None)


def test_path_index_on_a_local_data_dir_reads_in_place_and_copies_out(tmp_path, env, monkeypatch):
    """The local executor's shape (one machine, no FUSE): `data:` a dir, read in place (no `stage`), the snapshot
    annotated (`options.index.annotate`), the tiers copied into it, `path-index.parquet` last."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 2))
    (tmp_path / "t").mkdir(exist_ok=True)
    data = tmp_path / "data"
    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": str(tmp_path / "work"), "data": str(data),
               "layout": {"layer2": "listing/laptop/{scan}/", "snapshots": "laptop", "listing": "capture/{scan}/"},
               "sources": ["r2://disk-tree"], "skip": ["static-names", "digest"],
               "options": {"index": {"mode": "path-index", "args": ["-g"], "age_pyramids": True,
                                     "annotate": ["disk-tree", "capture-meta", "-c", "r2://disk-tree/snapshots/laptop/{scan}/meta.json",
                                                  "{listing}/_SUCCESS.json", "{snap}/meta.json"]}}})
    (data / "capture/2026-10-16T1200/disk-tree").mkdir(parents=True)
    (data / "capture/2026-10-16T1200/disk-tree/_SUCCESS.json").write_text("{}")
    c = S.make_ctx(p, env)
    runner = Runner(tmp_path)
    copied = []
    real_copy = S.copy_atomic
    monkeypatch.setattr(S, "copy_atomic", lambda src, dst: (copied.append(dst.relative_to(data).as_posix()), real_copy(src, dst)))

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
            (Path(argv[argv.index("-o") + 1]) / "age-pyramid-1d.parquet").write_text("")
        return r

    c.sh_impl = run
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert runner.calls == [
        f"dt-cloud path-index -d 2026-10-16T1200 -l <tmp>/data/capture/2026-10-16T1200/disk-tree/*.parquet -o {w}/snap "
        f"-c <tmp>/data/capture/2026-10-16T1200/dir-cache -P {w}/path-index/path-index.parquet -g",
        f"disk-tree capture-meta -c r2://disk-tree/snapshots/laptop/2026-10-16T1200/meta.json "
        f"<tmp>/data/capture/2026-10-16T1200/disk-tree/_SUCCESS.json {w}/snap/meta.json",
        f"dt-cloud age-pyramid -c -t 2 -o {w}/path-index {w}/path-index/path-index.parquet",
    ]
    assert copied == [
        "listing/laptop/2026-10-16T1200/index/G1/age-pyramid-1d.parquet",
        "listing/laptop/2026-10-16T1200/index/G1/path-index-bysize.parquet",
        "listing/laptop/2026-10-16T1200/index/G1/path-index.parquet",
        "snapshots/laptop/2026-10-16T1200/meta.json",
    ]
    runner.calls.clear()
    S.index(c, None)
    assert runner.calls == []


def test_list_adopts_a_sources_listing_from_the_env(tmp_path, env, monkeypatch):
    """`sources[].listing`: `list` fetches that listing (`{env:…}` filled) instead of listing the bucket, the deployment's
    R2 endpoint handed to the reader; an unset variable fails the step."""
    (tmp_path / "t").mkdir(exist_ok=True)
    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": str(tmp_path / "work"), "data": str(tmp_path / "data"),
               "layout": {"layer2": "listing/laptop/{scan}/", "snapshots": "laptop", "listing": "capture/{scan}/"},
               "sources": [{"uri": "r2://disk-tree", "listing": "{env:DT_CAPTURE}"}], "skip": ["static-names", "digest"],
               "options": {"index": {"mode": "path-index"}}})
    seen = []
    runner = Runner(tmp_path)

    def run(argv, env, stdout=None, text=True):
        seen.append(env.get("DISKY_R2_ENDPOINT_URL"))
        return runner(argv, env, stdout, text)

    c = S.make_ctx(p, {**env, "DT_CAPTURE": "r2://disk-tree/captures/m3/root/2026-10-16T12-00-01Z", "R2_ENDPOINT": "https://r2.example"})
    c.sh_impl = run
    S.list_source(c, "r2://disk-tree")
    assert (runner.calls, seen) == (
        ["dt-cloud fetch-listing -o <tmp>/data/capture/2026-10-16T1200/disk-tree r2://disk-tree/captures/m3/root/2026-10-16T12-00-01Z"],
        ["https://r2.example"],
    )
    unset = S.make_ctx(p, env)
    unset.sh_impl = run
    with pytest.raises(S.StepFailed) as e:
        S.list_source(unset, "r2://disk-tree")
    assert str(e.value) == "{env:DT_CAPTURE}: $DT_CAPTURE unset"
