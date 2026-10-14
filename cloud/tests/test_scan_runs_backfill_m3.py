"""m3's scan-run backfill (`infra/aws/scan_runs_backfill.py`) over fixture R2 / Batch / CloudWatch inputs."""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

from click.testing import CliRunner

from dt_cloud.scan_runs import Output, Phase, Record, Run, SqliteSink, load_profile

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("scan_runs_backfill_m3", REPO / "infra/aws/scan_runs_backfill.py")
bf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bf  # its dataclasses resolve annotations through it
spec.loader.exec_module(bf)

PROFILE = load_profile(REPO / "infra/aws/scan-runs.json")
DDL = (REPO / "site/migrations/cw/0016_scan_runs.sql").read_text()
ARN = "arn:aws:batch:us-east-1:111:job-definition/disk-tree-m3-ingest"


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp())


def events(start: str, lines: list[tuple[int, str]]) -> list[dict]:
    """Log events at `start` + each line's offset (s)."""
    return [{"timestamp": (ts(start) + dt_) * 1000 + 250, "message": m} for dt_, m in lines]


def head(cap: str, scan: str, gen: str, extra: str = "host=m3 store=laptop d1=primary") -> str:
    return f"[ingest] capture={cap} date={scan} gen={gen} {extra} vcpu=4 mem=15.2 GiB"


CAP_A = "captures/m3/root/2026-10-13T22-41-47Z"   # Batch still lists its job
CAP_B = "captures/m3/root/2026-10-02T14-59-17Z"   # Batch forgot it; `_INGEST.json` names the job
CAP_C = "captures/m3/root/2026-10-03T10-15-03Z"   # no job record; failed after fetch
CAP_N = "captures/nas/mnt__user/2026-10-05T03-00-00Z"   # the NAS: a secondary store, ingested twice
OLD = "captures/Mac/Users__ryan/2026-09-29T16-26-51Z"   # before index-sync and capture-meta

LOGS = {
    "disk-tree-m3/default/aaa": events("2026-10-13T22:51:00", [
        (0, head(CAP_A, "2026-10-13T2241", "202610132241")),
        (5, "[ingest] fetched 40 shards, 174M in 5s"),
        (25, "[rss] dir-stats: peak 0.8 GB"),
        (140, "wrote /work/snap/: age.json meta.json (1 TB, 7,937,424 objects)"),
        (142, "/work/snap/meta.json"),
        (143, "[ingest] path-index: 138s, peak RSS 5.72 GiB"),
        (143, "[ingest] outputs: index 255M, snap 100K"),
        (148, "[ingest] uploaded to s3://disk-tree/{…}/ in 5s"),
        (152, "index-sync: 2026-10-13T2241 [path] gen 202610132241 — 1099 row groups (remote)"),
        (155, "[ingest] footers → D1 disk-tree-m3-db in 7s"),
    ]),
    "disk-tree-m3/default/bbb": events("2026-10-02T15:06:00", [
        (0, head(CAP_B, "2026-10-02T1459", "202610021459")),
        (4, "[ingest] fetched 38 shards, 160M in 4s"),
        (100, "wrote /work/snap/: age.json meta.json"),
        (101, "/work/snap/meta.json"),
        (102, "[ingest] path-index: 98s, peak RSS 5 GiB"),
        (108, "[ingest] uploaded to s3://disk-tree/{…}/ in 6s"),
        (110, "index-sync: 2026-10-02T1459 [path] …"),
        (113, "[ingest] footers → D1 disk-tree-m3-db in 5s"),
    ]),
    "disk-tree-m3/default/ccc": events("2026-10-03T10:21:00", [
        (0, head(CAP_C, "2026-10-03T1015", "202610031015")),
        (6, "[ingest] fetched 38 shards, 160M in 6s"),
        (50, "[rss] dir-stats: peak 0.8 GB"),
    ]),
    "disk-tree-m3/default/nn1": events("2026-10-05T03:20:00", [
        (0, head(CAP_N, "2026-10-05T0300", "202610050300", "host=nas store=nas d1=nas")),
        (3, "[ingest] fetched 2 shards, 10M in 3s"),
        (20, "wrote /work/snap/: age.json meta.json"),
        (21, "/work/snap/meta.json"),
        (22, "[ingest] path-index: 19s, peak RSS 1 GiB"),
        (24, "[ingest] uploaded to s3://disk-tree/{…}/ in 2s"),
        (26, "index-sync: 2026-10-05T0300 [path] …"),
        (27, "[ingest] footers → D1 disk-tree-m3-db in 3s"),
    ]),
    "disk-tree-m3/default/nn2": events("2026-10-06T08:00:00", [
        (0, head(CAP_N, "2026-10-05T0300", "202610050300", "host=nas store=nas d1=nas")),
        (3, "[ingest] fetched 2 shards, 10M in 3s"),
        (20, "wrote /work/snap/: age.json meta.json"),
        (21, "/work/snap/meta.json"),
        (22, "[ingest] path-index: 19s, peak RSS 1 GiB"),
        (24, "[ingest] uploaded to s3://disk-tree/{…}/ in 2s"),
        (26, "index-sync: 2026-10-05T0300 [path] …"),
        (27, "[ingest] footers → D1 disk-tree-m3-db in 3s"),
    ]),
    "disk-tree-m3/default/old": events("2026-09-29T18:11:43", [
        (0, f"[ingest] capture={OLD} date=2026-09-29 gen=202609291811 store=laptop vcpu=4 mem=30.8 GiB"),
        (3, "[ingest] fetched 33 shards, 145M in 3s"),
        (9, "wrote /work/snap/: age.json meta.json (0 TB, 6,596,020 objects)"),
        (9, "[ingest] path-index: 6s, peak RSS 1.51 GiB"),
        (13, "[ingest] uploaded to s3://disk-tree/{listing/laptop/2026-09-29/index/202609291811,snapshots/laptop/2026-09-29}/ in 4s"),
    ]),
    "disk-tree-m3/default/zzz": events("2026-10-01T00:00:00", [(0, "some other task's line")]),
}

JOBS = [{
    "jobArn": "arn:aws:batch:us-east-1:111:job/job-a",
    "jobId": "job-a",
    "jobName": "ingest-m3-root-2026-10-13T22-41-47Z",
    "status": "SUCCEEDED",
    "statusReason": "Essential container in task exited",
    "createdAt": ts("2026-10-13T22:50:00") * 1000,
    "startedAt": ts("2026-10-13T22:50:58") * 1000,
    "stoppedAt": ts("2026-10-13T22:53:40") * 1000,
    "jobDefinition": f"{ARN}:15",
    "container": {"command": [f"r2://disk-tree/{CAP_A}"], "image": "111.dkr.ecr.us-east-1.amazonaws.com/disk-tree-m3@sha256:5973", "exitCode": 0,
                  "logStreamName": "disk-tree-m3/default/aaa"},
    "attempts": [{"container": {"logStreamName": "disk-tree-m3/default/aaa", "exitCode": 0},
                  "startedAt": ts("2026-10-13T22:50:58") * 1000, "stoppedAt": ts("2026-10-13T22:53:40") * 1000,
                  "statusReason": "Essential container in task exited"}],
}]

JOBDEFS = [
    {"jobDefinitionArn": f"{ARN}:15", "revision": 15, "containerProperties": {
        "resourceRequirements": [{"type": "VCPU", "value": "1"}, {"type": "MEMORY", "value": "8192"}],
        "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"}}},
]


def obj(key: str, size: int, at: str) -> dict:
    return {"key": key, "size": size, "last_modified": at}


OBJECTS = [
    obj(f"{CAP_A}/shard-00000.parquet", 100, "2026-10-13T22:45:00+00:00"),
    obj(f"{CAP_A}/_SUCCESS.json", 10, "2026-10-13T22:49:30+00:00"),
    obj(f"{CAP_A}/_INGEST.json", 5, "2026-10-13T22:49:31+00:00"),
    obj(f"{CAP_B}/shard-00000.parquet", 90, "2026-10-02T15:04:00+00:00"),
    obj(f"{CAP_B}/_SUCCESS.json", 10, "2026-10-02T15:05:23+00:00"),
    obj(f"{CAP_C}/_SUCCESS.json", 10, "2026-10-03T10:20:50+00:00"),
    obj(f"{CAP_N}/_SUCCESS.json", 10, "2026-10-05T03:10:00+00:00"),
    obj("listing/laptop/2026-10-13T2241/index/202610132241/path-index.parquet", 2000, "2026-10-13T22:53:30+00:00"),
    obj("listing/laptop/2026-10-13T2241/index/202610132241/path-index-bysize.parquet", 1000, "2026-10-13T22:53:30+00:00"),
    obj("snapshots/laptop/2026-10-13T2241/meta.json", 30, "2026-10-13T22:53:31+00:00"),
    obj("snapshots/laptop/2026-10-13T2241/age.json", 20, "2026-10-13T22:53:31+00:00"),
    obj("snapshots/laptop/2026-10-13T22410/meta.json", 7, "2026-10-13T22:53:31+00:00"),  # a prefix sibling: not this scan's
    obj("listing/nas/2026-10-05T0300/index/202610050300/path-index.parquet", 300, "2026-10-06T08:00:25+00:00"),
    obj("snapshots/nas/2026-10-05T0300/meta.json", 9, "2026-10-06T08:00:25+00:00"),
]
INGESTS = {
    CAP_A: {"state": "submitted", "job_id": "job-a"},
    CAP_B: {"state": "submitted", "job_id": "job-b", "submitted": "2026-10-02T15:05:30.214Z"},
}
INPUTS = bf.Inputs("disk-tree", OBJECTS, INGESTS, JOBS, JOBDEFS, LOGS)

RATE = 0.06197 * 0.3


def test_runs() -> None:
    a0, b0, c0, n0 = ts("2026-10-13T22:41:47"), ts("2026-10-02T14:59:17"), ts("2026-10-03T10:15:03"), ts("2026-10-05T03:00:00")
    assert bf.runs(PROFILE, INPUTS) == [
        Record(Run(
            run_id="old", scan="2026-09-29", kind="ingest", status="succeeded", source="backfill", store="primary",
            started_ts=ts("2026-09-29T16:26:51"), finished_ts=ts("2026-09-29T18:11:56"), spot=1, tasks=1, gen="202609291811",
        ), (
            Phase("fetch", 0, ts("2026-09-29T18:11:43"), ts("2026-09-29T18:11:46")),
            Phase("path-index", 1, ts("2026-09-29T18:11:46"), ts("2026-09-29T18:11:52")),
            Phase("upload", 2, ts("2026-09-29T18:11:52"), ts("2026-09-29T18:11:56")),
        )),
        Record(Run(
            run_id="job-b", scan="2026-10-02T1459", kind="ingest", status="succeeded", source="backfill", store="primary",
            started_ts=b0, finished_ts=ts("2026-10-02T15:07:53"), spot=1, tasks=1, gen="202610021459",
        ), (
            Phase("capture", 0, b0, ts("2026-10-02T15:05:23")),
            Phase("queue", 1, ts("2026-10-02T15:05:23"), ts("2026-10-02T15:06:00")),
            Phase("fetch", 2, ts("2026-10-02T15:06:00"), ts("2026-10-02T15:06:04")),
            Phase("path-index", 3, ts("2026-10-02T15:06:04"), ts("2026-10-02T15:07:40")),
            Phase("capture-meta", 4, ts("2026-10-02T15:07:40"), ts("2026-10-02T15:07:42")),
            Phase("upload", 5, ts("2026-10-02T15:07:42"), ts("2026-10-02T15:07:48")),
            Phase("index-sync", 6, ts("2026-10-02T15:07:48"), ts("2026-10-02T15:07:53")),
        ), (
            Output("capture", f"r2://disk-tree/{CAP_B}/", bytes=100, objects=2),
        )),
        Record(Run(
            run_id="ccc", scan="2026-10-03T1015", kind="ingest", status="failed", source="backfill", store="primary",
            failed_phase="path-index", error="the task log stops before the ingest finished",
            started_ts=c0, finished_ts=ts("2026-10-03T10:21:50"), spot=1, tasks=1, gen="202610031015",
        ), (
            Phase("capture", 0, c0, ts("2026-10-03T10:20:50")),
            Phase("queue", 1, ts("2026-10-03T10:20:50"), ts("2026-10-03T10:21:00")),
            Phase("fetch", 2, ts("2026-10-03T10:21:00"), ts("2026-10-03T10:21:06")),
        )),
        Record(Run(
            run_id="nn1", scan="2026-10-05T0300", kind="ingest", status="succeeded", source="backfill", store="nas",
            started_ts=n0, finished_ts=ts("2026-10-05T03:20:27"), spot=1, tasks=1, gen="202610050300",
        ), (
            Phase("capture", 0, n0, ts("2026-10-05T03:10:00")),
            Phase("queue", 1, ts("2026-10-05T03:10:00"), ts("2026-10-05T03:20:00")),
            Phase("fetch", 2, ts("2026-10-05T03:20:00"), ts("2026-10-05T03:20:03")),
            Phase("path-index", 3, ts("2026-10-05T03:20:03"), ts("2026-10-05T03:20:20")),
            Phase("capture-meta", 4, ts("2026-10-05T03:20:20"), ts("2026-10-05T03:20:22")),
            Phase("upload", 5, ts("2026-10-05T03:20:22"), ts("2026-10-05T03:20:24")),
            Phase("index-sync", 6, ts("2026-10-05T03:20:24"), ts("2026-10-05T03:20:27")),
        )),
        Record(Run(
            run_id="nn2", scan="2026-10-05T0300", kind="ingest", status="succeeded", source="backfill", store="nas",
            started_ts=n0, finished_ts=ts("2026-10-06T08:00:27"), spot=1, tasks=1, gen="202610050300",
        ), (
            Phase("capture", 0, n0, ts("2026-10-05T03:10:00")),
            Phase("queue", 1, ts("2026-10-05T03:10:00"), ts("2026-10-06T08:00:00")),
            Phase("fetch", 2, ts("2026-10-06T08:00:00"), ts("2026-10-06T08:00:03")),
            Phase("path-index", 3, ts("2026-10-06T08:00:03"), ts("2026-10-06T08:00:20")),
            Phase("capture-meta", 4, ts("2026-10-06T08:00:20"), ts("2026-10-06T08:00:22")),
            Phase("upload", 5, ts("2026-10-06T08:00:22"), ts("2026-10-06T08:00:24")),
            Phase("index-sync", 6, ts("2026-10-06T08:00:24"), ts("2026-10-06T08:00:27")),
        ), (
            Output("capture", f"r2://disk-tree/{CAP_N}/", bytes=10, objects=1),
            Output("index", "r2://disk-tree/listing/nas/2026-10-05T0300/index/202610050300/", bytes=300, objects=1, gen="202610050300"),
            Output("snapshot", "r2://disk-tree/snapshots/nas/2026-10-05T0300/", bytes=9, objects=1),
        )),
        Record(Run(
            run_id="job-a", scan="2026-10-13T2241", kind="ingest", status="succeeded", source="backfill", store="primary",
            started_ts=a0, finished_ts=ts("2026-10-13T22:53:40"), job_name="ingest-m3-root-2026-10-13T22-41-47Z", region="us-east-1",
            machine="fargate-arm64-1vcpu-8gib", spot=1, tasks=1, image="disk-tree-m3@sha256:5973",
            cost_usd=round(RATE * 162 / 3600, 4), gen="202610132241",
        ), (
            Phase("capture", 0, a0, ts("2026-10-13T22:49:30")),
            Phase("queue", 1, ts("2026-10-13T22:49:30"), ts("2026-10-13T22:50:58")),
            Phase("fetch", 2, ts("2026-10-13T22:50:58"), ts("2026-10-13T22:51:05")),
            Phase("path-index", 3, ts("2026-10-13T22:51:05"), ts("2026-10-13T22:53:20")),
            Phase("capture-meta", 4, ts("2026-10-13T22:53:20"), ts("2026-10-13T22:53:23")),
            Phase("upload", 5, ts("2026-10-13T22:53:23"), ts("2026-10-13T22:53:28")),
            Phase("index-sync", 6, ts("2026-10-13T22:53:28"), ts("2026-10-13T22:53:35")),
        ), (
            Output("capture", f"r2://disk-tree/{CAP_A}/", bytes=115, objects=3),
            Output("index", "r2://disk-tree/listing/laptop/2026-10-13T2241/index/202610132241/", bytes=3000, objects=2, gen="202610132241"),
            Output("snapshot", "r2://disk-tree/snapshots/laptop/2026-10-13T2241/", bytes=50, objects=2),
        )),
    ]


def test_cli_replays_saved_inputs_into_sqlite(tmp_path: Path) -> None:
    INPUTS.save(tmp_path / "inputs")
    assert sorted(p.name for p in (tmp_path / "inputs").iterdir()) == ["jobdefs.json", "jobs.json", "logs.json", "r2.json"]
    db = tmp_path / "runs.sqlite"
    SqliteSink(db, ddl=DDL)
    r = CliRunner().invoke(bf.main, ["-i", str(tmp_path / "inputs"), "-s", str(db), "-j", str(tmp_path / "runs.json")])
    assert (r.exit_code, r.exception) == (0, None)
    s = SqliteSink(db)
    assert [tuple(x.values()) for x in s.query("SELECT run_id, scan, store, status, source, failed_phase FROM scan_runs ORDER BY started_ts, run_id")] == [
        ("old", "2026-09-29", "primary", "succeeded", "backfill", None),
        ("job-b", "2026-10-02T1459", "primary", "succeeded", "backfill", None),
        ("ccc", "2026-10-03T1015", "primary", "failed", "backfill", "path-index"),
        ("nn1", "2026-10-05T0300", "nas", "succeeded", "backfill", None),
        ("nn2", "2026-10-05T0300", "nas", "succeeded", "backfill", None),
        ("job-a", "2026-10-13T2241", "primary", "succeeded", "backfill", None),
    ]
    assert s.query("SELECT count(*) n FROM scan_run_phases") == [{"n": 34}]
    assert [r["run"]["run_id"] for r in json.loads((tmp_path / "runs.json").read_text())] == ["old", "job-b", "ccc", "nn1", "nn2", "job-a"]
    # Idempotent: a second replay changes nothing.
    again = CliRunner().invoke(bf.main, ["-i", str(tmp_path / "inputs"), "-s", str(db)])
    assert (again.exit_code, s.query("SELECT count(*) n FROM scan_runs"), s.query("SELECT count(*) n FROM scan_run_phases")) == (0, [{"n": 6}], [{"n": 34}])


def test_machine_of() -> None:
    assert [bf.machine_of(d) for d in (JOBDEFS[0], None, {"containerProperties": {"resourceRequirements": [
        {"type": "VCPU", "value": "4"}, {"type": "MEMORY", "value": "16384"}]}})] == [
        "fargate-arm64-1vcpu-8gib", None, "fargate-x86-64-4vcpu-16gib",
    ]
