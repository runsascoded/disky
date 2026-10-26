import json

import pytest
from click import UsageError
from click.testing import CliRunner

from dt_cloud import deploy_cost as dc
from dt_cloud.cli import main
from dt_cloud.scan_cost import timestamp

G = 2**30
NOW = timestamp("2026-10-23T12:00:00Z")
PRICES = dc.Prices(
    storage={"REGIONAL": 0.5, "COLDLINE": 0.25},
    machines={"n2-highmem-16": 1.0, "e2-standard-2": 0.5},
    spot_factor=0.25,
)


def point(day: int, value, kind: str = "doubleValue") -> dict:
    t = f"2026-10-{day}T12:00:00Z"
    return {"interval": {"startTime": t, "endTime": t}, "value": {kind: value}}


def series(bucket: str, type_: str, cls: str, points: list[dict]) -> dict:
    return {
        "metric": {"type": dc.METRIC, "labels": {"type": type_, "storage_class": cls}},
        "resource": {"labels": {"project_id": "p", "bucket_name": bucket, "location": "us-east1"}},
        "points": points,
    }


# Monitoring answers newest point first.
PAGES = [
    {"timeSeries": [
        series("bkt-b", "live-object", "COLDLINE", [point(23, 8 * G), point(22, 10 * G), point(21, 12 * G)]),
        series("bkt-a", "soft-deleted-object", "REGIONAL", [point(23, 4 * G), point(22, 2 * G)]),
    ], "nextPageToken": "t"},
    {"timeSeries": [
        series("bkt-a", "live-object", "REGIONAL", [point(23, 12 * G), point(22, 11 * G), point(21, 10 * G)]),
        series("bkt-b", "live-object", "WEIRD", [point(23, str(G), "int64Value")]),
    ]},
]


def job(
    name: str,
    labels: dict,
    run_s: int,
    machine: str | None = "n2-highmem-16",
    model: str | None = None,
    tasks: int = 1,
    parallelism: int | None = None,
    alloc_labels: dict | None = None,
    status_machine: str | None = None,
) -> dict:
    group: dict = {"name": f"projects/p/locations/us-east1/jobs/{name}/taskGroups/group0", "taskCount": str(tasks)}
    if parallelism is not None:
        group["parallelism"] = str(parallelism)
    policy = {k: v for k, v in (("machineType", machine), ("provisioningModel", model)) if v}
    status: dict = {"state": "SUCCEEDED", "runDuration": f"{run_s}.0s"}
    if status_machine:
        status["taskGroups"] = {"group0": {"instances": [{"machineType": status_machine, "provisioningModel": "STANDARD"}]}}
    return {
        "name": f"projects/p/locations/us-east1/jobs/{name}",
        "labels": labels,
        "allocationPolicy": {"labels": alloc_labels or {}, "instances": [{"policy": policy}] if policy else []},
        "taskGroups": [group],
        "status": status,
    }


SEL = {"app": "disky", "deployment": "d|d-old"}
JOBS = [
    # 4 tasks, 2 at a time: 2 VMs × 1 h
    job("scan-1", {"app": "disky", "deployment": "d", "component": "scan"}, 3600, tasks=4, parallelism=2),
    # labels on the allocation policy count; `d-old` is one of the selector's alternatives
    job("drill-1", {"app": "disky", "component": "drill"}, 7200, model="SPOT", alloc_labels={"deployment": "d-old"}),
    # no policy: the machine its status reports
    job("nf-1", {"app": "disky", "deployment": "d", "component": "scan"}, 1800, machine=None, status_machine="e2-standard-2"),
    job("odd-1", {"app": "disky", "deployment": "d", "component": "scan"}, 3600, machine="c3-standard-4"),
    job("other-1", {"app": "disky", "deployment": "other", "component": "scan"}, 3600),
    job("sn-drill-1", {}, 3600),
    job("sn-x-2", {"component": "x"}, 1800, tasks=2),
]


def test_parse_series():
    assert dc.parse_series(PAGES) == [
        dc.Series("bkt-a", "live-object", "REGIONAL", ((timestamp("2026-10-21T12:00:00Z"), 10 * G), (timestamp("2026-10-22T12:00:00Z"), 11 * G), (NOW, 12 * G))),
        dc.Series("bkt-a", "soft-deleted-object", "REGIONAL", ((timestamp("2026-10-22T12:00:00Z"), 2 * G), (NOW, 4 * G))),
        dc.Series("bkt-b", "live-object", "COLDLINE", ((timestamp("2026-10-21T12:00:00Z"), 12 * G), (timestamp("2026-10-22T12:00:00Z"), 10 * G), (NOW, 8 * G))),
        dc.Series("bkt-b", "live-object", "WEIRD", ((NOW, G),)),
    ]


def test_slope():
    day = dc.DAY
    assert dc.slope_per_day([(0, 10.0), (day, 11.0), (2 * day, 12.0)]) == 1.0
    # deltas 4, -2, 4
    assert dc.slope_per_day([(0, 10.0), (day, 14.0), (2 * day, 12.0), (3 * day, 16.0)]) == 4.0
    # a one-off fill (+40) and cleanup (-30) don't set the rate
    assert dc.slope_per_day([(0, 10.0), (day, 11.0), (2 * day, 51.0), (3 * day, 52.0), (4 * day, 22.0), (5 * day, 23.0)]) == 1.0
    # an even count: the middle two's mean; a 2-day gap counts per day
    assert dc.slope_per_day([(0, 0.0), (day, 2.0), (3 * day, 10.0)]) == 3.0
    assert dc.slope_per_day([(day, 5.0)]) == 0.0


def bucket(points: int, policy: dc.BucketPolicy = dc.BucketPolicy()) -> dict:
    return {"points": points, "short": points < dc.MIN_POINTS, "soft_delete_off": policy.soft_delete_off, "ttl_days": policy.ttl_days, "partial_ttl": policy.partial_ttl}


@pytest.mark.parametrize("policies, bytes_, usd", [
    # bkt-a live 12 → 22 GiB; bkt-b's coldline would go negative: floored at 0; WEIRD (flat) is counted, unpriced
    ({}, 27 * G, 13.0),
    # soft delete off: bkt-a's soft-deleted 4 GiB drain
    ({"bkt-a": dc.BucketPolicy(soft_delete_off=True)}, 23 * G, 11.0),
    # bkt-b TTL'd: its coldline held at 8 GiB, not sloped to 0
    ({"bkt-b": dc.BucketPolicy(ttl_days=7.0)}, 35 * G, 15.0),
    # a partial TTL keeps the slope
    ({"bkt-b": dc.BucketPolicy(partial_ttl=True)}, 27 * G, 13.0),
])
def test_storage_report(policies, bytes_, usd):
    s = dc.storage_report(dc.parse_series(PAGES), PRICES, {"10d": 10.0}, policies)
    assert s == {
        "rows": [
            {"bucket": "bkt-a", "type": "live-object", "storage_class": "REGIONAL", "days": 3, "bytes": 12 * G, "slope_bytes_per_day": G, "usd_per_month": 6.0},
            {"bucket": "bkt-a", "type": "soft-deleted-object", "storage_class": "REGIONAL", "days": 2, "bytes": 4 * G, "slope_bytes_per_day": 0.0, "usd_per_month": 2.0},
            {"bucket": "bkt-b", "type": "live-object", "storage_class": "COLDLINE", "days": 3, "bytes": 8 * G, "slope_bytes_per_day": -2 * G, "usd_per_month": 2.0},
            {"bucket": "bkt-b", "type": "live-object", "storage_class": "WEIRD", "days": 1, "bytes": G, "slope_bytes_per_day": 0.0, "usd_per_month": None},
        ],
        "bytes": 25 * G,
        "bytes_by_type": {"live-object": 21 * G, "soft-deleted-object": 4 * G},
        "live_slope_bytes_per_day": -G,
        "usd_per_month": 10.0,
        "buckets": {"bkt-a": bucket(3, policies.get("bkt-a", dc.BucketPolicy())), "bkt-b": bucket(3, policies.get("bkt-b", dc.BucketPolicy()))},
        "unpriced_bytes": {"WEIRD": G},
        "horizons": {"10d": {"bytes": bytes_, "usd_per_month": usd}},
    }


def delete(**condition) -> dict:
    return {"action": {"type": "Delete"}, "condition": condition}


@pytest.mark.parametrize("meta, policy", [
    ({}, dc.BucketPolicy(soft_delete_off=True)),
    ({"softDeletePolicy": {"retentionDurationSeconds": "604800"}}, dc.BucketPolicy()),
    # whole-bucket Delete rules: the shortest age bounds the live bytes
    ({"softDeletePolicy": {"retentionDurationSeconds": "0"}, "lifecycle": {"rule": [delete(age=30), delete(age=7, isLive=True)]}},
     dc.BucketPolicy(soft_delete_off=True, ttl_days=7.0)),
    # scoped only: not separable from the bucket-level metric
    ({"softDeletePolicy": {"retentionDurationSeconds": "604800"}, "lifecycle": {"rule": [delete(age=7, matchesPrefix=["tmp/"])]}},
     dc.BucketPolicy(partial_ttl=True)),
    # a whole-bucket rule subsumes a scoped one
    ({"softDeletePolicy": {"retentionDurationSeconds": "604800"}, "lifecycle": {"rule": [delete(age=3, matchesSuffix=[".tmp"]), delete(age=14)]}},
     dc.BucketPolicy(ttl_days=14.0)),
    # noncurrent-only Deletes and non-Delete actions bound nothing live
    ({"softDeletePolicy": {"retentionDurationSeconds": "604800"}, "lifecycle": {"rule": [
        delete(age=7, isLive=False), delete(age=1, numNewerVersions=2),
        {"action": {"type": "SetStorageClass", "storageClass": "COLDLINE"}, "condition": {"age": 30}},
    ]}}, dc.BucketPolicy()),
])
def test_bucket_policy(meta, policy):
    assert dc.bucket_policy(meta) == policy


def test_job_usage():
    assert [dc.job_usage(j) for j in JOBS[:3]] == [
        dc.JobUsage("scan-1", "us-east1", "scan", "n2-highmem-16", False, 2.0),
        dc.JobUsage("drill-1", "us-east1", "drill", "n2-highmem-16", True, 2.0),
        dc.JobUsage("nf-1", "us-east1", "scan", "e2-standard-2", False, 0.5),
    ]


def test_compute_report():
    assert dc.compute_report(JOBS, dc.parse_selector("app=disky,deployment=d|d-old"), PRICES, 2.0) == {
        "selector": SEL,
        "jobs": 4,
        "vm_hours": 5.5,
        "usd": 2.75,
        "usd_per_day": 1.375,
        "usd_per_month": 41.8515625,
        "components": {
            "drill": {"jobs": 1, "vm_hours": 2.0, "usd": 0.5, "usd_per_month": 7.609375},
            "scan": {"jobs": 3, "vm_hours": 3.5, "usd": 2.25, "usd_per_month": 34.2421875},
        },
        "machines": {
            "c3-standard-4": {"jobs": 1, "vm_hours": 1.0, "usd": 0.0, "usd_per_month": 0.0},
            "e2-standard-2": {"jobs": 1, "vm_hours": 0.5, "usd": 0.25, "usd_per_month": 3.8046875},
            "n2-highmem-16": {"jobs": 1, "vm_hours": 2.0, "usd": 2.0, "usd_per_month": 30.4375},
            "n2-highmem-16 spot": {"jobs": 1, "vm_hours": 2.0, "usd": 0.5, "usd_per_month": 7.609375},
        },
        "unpriced_vm_hours": {"c3-standard-4": 1.0},
        "unlabeled": {"sn": {"jobs": 2, "vm_hours": 2.0, "usd": 2.0, "usd_per_month": 30.4375}},
    }


def test_spans_and_selectors():
    assert [dc.parse_span(s) for s in ("30d", "2w", "6m", "1y", "1.5y")] == [30.0, 14.0, 182.625, 365.25, 547.875]
    with pytest.raises(UsageError):
        dc.parse_span("3x")
    assert dc.parse_selector("app=disky, deployment=a|b") == {"app": "disky", "deployment": "a|b"}
    assert dc.parse_selector(None) == {}
    with pytest.raises(UsageError):
        dc.parse_selector("app")
    assert [dc.matches(l, {"deployment": "a|b"}) for l in ({"deployment": "a"}, {"deployment": "b"}, {"deployment": "c"}, {})] == [True, True, False, False]


def test_prices(tmp_path):
    assert (dc.PRICES.machines["e2-standard-2"], dc.PRICES.machines["n2-highmem-8"], dc.PRICES.machines["n2d-highmem-8"]) == (0.067006, 0.524056, 0.45592)
    p = tmp_path / "prices.yml"
    p.write_text("storage: {REGIONAL: 0.5}\nmachines: {c3-standard-4: 0.2}\nspot_factor: 0.5\n")
    got = dc.load_prices(p)
    assert (got.storage["REGIONAL"], got.storage["COLDLINE"], got.machines["c3-standard-4"], got.machines["e2-standard-2"], got.spot_factor) == (0.5, 0.004, 0.2, 0.067006, 0.5)
    p.write_text("disks: {}\n")
    with pytest.raises(UsageError):
        dc.load_prices(p)


@pytest.fixture
def fake_gcp(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(dc, "clock", lambda: NOW)
    monkeypatch.setattr(dc, "select_buckets", lambda *a: calls.append(("select_buckets", *a)) or ["bkt-b"])
    monkeypatch.setattr(dc, "fetch_buckets", lambda *a: calls.append(("fetch_buckets", *a)) or {
        "bkt-a": {"softDeletePolicy": {"retentionDurationSeconds": "0"}},
        "bkt-b": {"softDeletePolicy": {"retentionDurationSeconds": "604800"}, "lifecycle": {"rule": [delete(age=7)]}},
    })
    monkeypatch.setattr(dc, "fetch_series", lambda *a: calls.append(("fetch_series", *a)) or PAGES)
    monkeypatch.setattr(dc, "fetch_jobs", lambda *a: calls.append(("fetch_jobs", *a)) or JOBS)
    prices = tmp_path / "prices.yml"
    prices.write_text("storage: {REGIONAL: 0.5, COLDLINE: 0.25}\nmachines: {n2-highmem-16: 1.0, e2-standard-2: 0.5}\nspot_factor: 0.25\n")
    args = ["cost", "-b", "bkt-a", "-H", "10d", "-J", "app=disky,deployment=d|d-old", "-l", "deployment=d", "-n", "D",
            "-p", "p", "-P", str(prices), "-w", "2d"]
    return calls, args


SUMMARY = "We can leave D running for 10d for 26.8→33.3 GB of storage ($10.00→$13.00/mo) and $41.85→$41.85/mo on compute (list-price estimates; provisional: short window (< 14 daily points) for bkt-a, bkt-b)."


def test_cli(fake_gcp):
    calls, args = fake_gcp
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0, r.output
    start = NOW - 2 * dc.DAY
    assert calls == [
        ("select_buckets", "p", {"deployment": "d"}),
        ("fetch_series", "p", ["bkt-a", "bkt-b"], start, NOW),
        ("fetch_buckets", ["bkt-a", "bkt-b"]),
        ("fetch_jobs", "p", (), start),
    ]
    # Column padding aside, every line exactly.
    assert [" ".join(line.split()) for line in r.output.splitlines()] == [
        "D · p · 2-day window to 2026-10-23T12:00:00Z · approximate US list prices; estimates, not billed cost",
        "",
        "Storage (Cloud Monitoring `storage/v2/total_bytes`, daily means):",
        "bucket type class pts GB GB/day $/mo",
        "bkt-a live-object REGIONAL 3 12.9 +1.07 $6.00",
        "bkt-a soft-deleted-object REGIONAL 2 4.3 - $2.00",
        "bkt-b live-object COLDLINE 3 8.6 -2.15 $2.00",
        "bkt-b live-object WEIRD 1 1.1 +0.00 unpriced",
        "now: 26.8 GB (live-object 22.5 GB, soft-deleted-object 4.3 GB), $10.00/mo; live -1.07 GB/day",
        "10d: 33.3 GB, $13.00/mo",
        "bkt-a: short window (3 pts); soft delete off: soft-deleted bytes drain",
        "bkt-b: short window (3 pts); lifecycle Delete at 7d: live bytes held flat; soft-deleted bytes held",
        "UNPRICED: 1.1 GB of storage class WEIRD",
        "",
        "Compute (Batch jobs labeled app=disky,deployment=d|d-old, created in the window; VM-h = runDuration × min(taskCount, parallelism)):",
        "component jobs VM-h $/mo",
        "drill 1 2.0 $7.61",
        "scan 3 3.5 $34.24",
        "machine jobs VM-h $/mo",
        "c3-standard-4 1 1.0 $0.00",
        "e2-standard-2 1 0.5 $3.80",
        "n2-highmem-16 1 2.0 $30.44",
        "n2-highmem-16 spot 1 2.0 $7.61",
        "total: 4 jobs, 5.5 VM-h, $2.75 = $1.38/day = $41.85/mo (flat)",
        "UNPRICED: 1.0 VM-h on c3-standard-4",
        "NOT COUNTED: 2 jobs in the window (2.0 VM-h, $30.44/mo) carry none of the selector's keys; by name prefix:",
        "sn 2 2.0 $30.44",
        "",
        SUMMARY,
    ]


def test_cli_json(fake_gcp):
    _, args = fake_gcp
    r = CliRunner().invoke(main, [*args, "-j"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert (doc["summary"], doc["storage"]["buckets"], doc["storage"]["horizons"], doc["compute"]["usd_per_month"]) == (
        SUMMARY,
        {"bkt-a": bucket(3, dc.BucketPolicy(soft_delete_off=True)), "bkt-b": bucket(3, dc.BucketPolicy(ttl_days=7.0))},
        {"10d": {"bytes": 31 * G, "usd_per_month": 13.0}},
        41.8515625,
    )


def test_long_window():
    # 14 daily points growing 1 GiB/day, with a one-off 100 GiB fill on the 15th: the rate stays 1 GiB/day
    pts = [point(d, (d + (100 if d >= 15 else 0)) * G) for d in range(23, 9, -1)]
    doc = dc.report(name="X", project="p", now=NOW, window_days=14.0, horizons={"10d": 10.0}, prices=PRICES,
                    series=dc.parse_series([{"timeSeries": [series("bkt-c", "live-object", "REGIONAL", pts)]}]))
    lines = [" ".join(line.split()) for line in dc.render(doc).splitlines()]
    assert lines[4:] == [
        "bkt-c live-object REGIONAL 14 132.1 +1.07 $61.50",
        "now: 132.1 GB (live-object 132.1 GB), $61.50/mo; live +1.07 GB/day",
        "10d: 142.8 GB, $66.50/mo",
        "bkt-c: 14 daily points; soft-deleted bytes held",
        "",
        "We can leave X running for 10d for 132.1→142.8 GB of storage ($61.50→$66.50/mo) (list-price estimates).",
    ]


def test_cli_needs_a_target():
    r = CliRunner().invoke(main, ["cost", "-p", "p"])
    assert (r.exit_code, r.output.splitlines()[-1]) == (2, "Error: nothing to measure: pass -b / -l (storage) and/or -J (compute)")


def test_snapshot():
    policies = {"bkt-a": dc.BucketPolicy(soft_delete_off=True), "bkt-b": dc.BucketPolicy(ttl_days=7.0)}
    doc = dc.report(name="D", project="p", now=NOW, window_days=2.0, horizons={"10d": 10.0}, prices=PRICES,
                    series=dc.parse_series(PAGES), policies=policies, jobs=JOBS, selector=dc.parse_selector("app=disky,deployment=d|d-old"))
    snap = dc.snapshot(doc, PRICES)
    assert {k: v for k, v in snap.items() if k != "compute"} == {
        "version": 1,
        "cloud": "gcp",
        "date": "2026-10-23",
        "name": "D",
        "project": "p",
        "as_of": "2026-10-23T12:00:00Z",
        "window_days": 2,
        "horizons": {"10d": 10},
        "basis": "approximate US list prices; estimates, not billed cost",
        "summary": SUMMARY,
        "usd_per_month": {"storage": 10, "compute": round(41.8515625, 6), "total": round(51.8515625, 6)},
        "storage": {
            "bytes": 25 * G,
            "bytes_by_type": {"live-object": 21 * G, "soft-deleted-object": 4 * G},
            "live_slope_bytes_per_day": -G,
            "usd_per_month": 10,
            "unpriced_bytes": {"WEIRD": G},
            "horizons": {"10d": {"bytes": 31 * G, "usd_per_month": 13}},
            "rows": [
                {"bucket": "bkt-a", "type": "live-object", "storage_class": "REGIONAL", "days": 3, "bytes": 12 * G, "slope_bytes_per_day": G, "usd_per_month": 6},
                {"bucket": "bkt-a", "type": "soft-deleted-object", "storage_class": "REGIONAL", "days": 2, "bytes": 4 * G, "slope_bytes_per_day": 0, "usd_per_month": 2},
                {"bucket": "bkt-b", "type": "live-object", "storage_class": "COLDLINE", "days": 3, "bytes": 8 * G, "slope_bytes_per_day": -2 * G, "usd_per_month": 2},
                {"bucket": "bkt-b", "type": "live-object", "storage_class": "WEIRD", "days": 1, "bytes": G, "slope_bytes_per_day": 0, "usd_per_month": None},
            ],
            "buckets": [
                # soft delete off: its soft-deleted 4 GiB drain by the horizon; live grows 1 GiB/day
                {"bucket": "bkt-a", "bytes": 16 * G, "bytes_by_type": {"live-object": 12 * G, "soft-deleted-object": 4 * G},
                 "bytes_by_class": {"REGIONAL": 16 * G}, "live_slope_bytes_per_day": G, "usd_per_month": 8, "unpriced_bytes": 0,
                 "horizons": {"10d": {"bytes": 22 * G, "usd_per_month": 11}},
                 "points": 3, "soft_delete_off": True, "ttl_days": None, "partial_ttl": False, "short": True},
                # TTL'd: live held at 8 GiB coldline + 1 GiB unpriced, despite the -2 GiB/day slope
                {"bucket": "bkt-b", "bytes": 9 * G, "bytes_by_type": {"live-object": 9 * G},
                 "bytes_by_class": {"COLDLINE": 8 * G, "WEIRD": G}, "live_slope_bytes_per_day": -2 * G, "usd_per_month": 2, "unpriced_bytes": G,
                 "horizons": {"10d": {"bytes": 9 * G, "usd_per_month": 2}},
                 "points": 3, "soft_delete_off": False, "ttl_days": 7, "partial_ttl": False, "short": True},
            ],
        },
    }
    assert snap["compute"] == dc._num(dc.compute_report(JOBS, SEL, PRICES, 2.0))
    assert snap["compute"]["components"] == {
        "drill": {"jobs": 1, "vm_hours": 2, "usd": 0.5, "usd_per_month": round(7.609375, 6)},
        "scan": {"jobs": 3, "vm_hours": 3.5, "usd": 2.25, "usd_per_month": round(34.2421875, 6)},
    }


def test_snapshot_without_compute():
    doc = dc.report(name="S", project="p", now=NOW, window_days=2.0, horizons={"10d": 10.0}, prices=PRICES,
                    series=dc.parse_series(PAGES[1:]))
    snap = dc.snapshot(doc, PRICES)
    assert (snap["usd_per_month"], snap["compute"], [b["bucket"] for b in snap["storage"]["buckets"]]) == (
        {"storage": 6, "compute": 0, "total": 6}, None, ["bkt-a", "bkt-b"])


def test_cli_writes_the_snapshot_to_each_root(fake_gcp, tmp_path):
    _, args = fake_gcp
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (a / "2026-10-23.json").write_text("an earlier run's")
    r = CliRunner().invoke(main, [*args, "-o", str(a), "-o", str(b)])
    assert r.exit_code == 0, r.output
    assert r.stdout.split("\n") == [f"{a}/2026-10-23.json", f"{b}/2026-10-23.json", ""]
    doc = json.loads((a / "2026-10-23.json").read_text())
    assert (doc["version"], doc["date"], doc["summary"], [x["bucket"] for x in doc["storage"]["buckets"]], doc["usd_per_month"]) == (
        1, "2026-10-23", SUMMARY, ["bkt-a", "bkt-b"], {"storage": 10, "compute": round(41.8515625, 6), "total": round(51.8515625, 6)})
    assert (a / "2026-10-23.json").read_bytes() == (b / "2026-10-23.json").read_bytes()
