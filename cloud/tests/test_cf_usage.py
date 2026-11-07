"""`dt-cloud cf-usage` over recorded responses (`fixtures/cf_usage/`, in the GraphQL Analytics and Analytics Engine
SQL APIs' documented shapes): the deployment's resources from its wrangler config, the parsers, the price math, the
attribution tables and the per-dataset failure notes."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.cf_usage import (
    CPU_QUANTILES,
    GRAPHQL_URL,
    PRICES,
    Deployment,
    ae_sql,
    build_report,
    cli,
    load_deployment,
    mean_cpu_us,
    render,
    snapshot,
)

FIX = Path(__file__).parent / "fixtures" / "cf_usage"
TODAY = dt.date(2026, 10, 21)

WRANGLER = """\
name = "my-deployment"
pages_build_output_dir = "dist"

[[d1_databases]]
binding = "DB"
database_name = "my-db"
database_id = "11111111-2222-4333-8444-555555555555"

[[kv_namespaces]]
binding = "CACHE_KV"
id = "0123456789abcdef0123456789abcdef"

[[r2_buckets]]
binding = "INDEX_R2"
bucket_name = "my-index"

[[analytics_engine_datasets]]
binding = "USAGE"
dataset = "disky_usage_mine"

[vars]
CF_ACCOUNT_ID = "acct123"
CF_PAGES_SCRIPT = "pages-worker--42"

[[env.preview.d1_databases]]
binding = "DB"
database_name = "my-db"
database_id = "11111111-2222-4333-8444-555555555555"

[[env.preview.r2_buckets]]
binding = "INDEX_R2"
bucket_name = "my-index"
"""

DEP = Deployment(
    account="acct123",
    project="my-deployment",
    buckets=("my-index",),
    databases=("11111111-2222-4333-8444-555555555555",),
    namespaces=("0123456789abcdef0123456789abcdef",),
    dataset="disky_usage_mine",
    script="pages-worker--42",
)

GQL_FIXTURE = {
    "pagesFunctionsInvocationsAdaptiveGroups": "functions",
    "r2OperationsAdaptiveGroups": "r2_ops",
    "r2StorageAdaptiveGroups": "r2_storage",
    "d1AnalyticsAdaptiveGroups": "d1",
    "kvOperationsAdaptiveGroups": "kv_ops",
    "kvStorageAdaptiveGroups": "kv_storage",
}


class Recorded:
    """A `Fetch` answering from the fixtures; `override` maps a fixture name to another (e.g. an error)."""

    def __init__(self, override: dict[str, str] | None = None):
        self.override = override or {}
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, url: str, token: str, body: bytes, content_type: str):
        if url == GRAPHQL_URL:
            q = json.loads(body)
            name = next(f for ds, f in GQL_FIXTURE.items() if ds in q["query"])
            self.calls.append((url, token, name))
            assert q["variables"] == {
                "accountTag": "acct123",
                "since": "2026-10-19T00:00:00Z",
                "until": "2026-10-21T00:00:00Z",
                "sinceDate": "2026-10-19",
                "untilDate": "2026-10-21",
                "buckets": ["my-index"],
                "databases": ["11111111-2222-4333-8444-555555555555"],
                "namespaces": ["0123456789abcdef0123456789abcdef"],
                "scripts": ["pages-worker--42-production", "pages-worker--42-preview"],
            }
        else:
            assert url == "https://api.cloudflare.com/client/v4/accounts/acct123/analytics_engine/sql"
            assert content_type == "text/plain"
            name = "ae_caller" if "blob4 AS caller" in body.decode() else "ae_route"
            self.calls.append((url, token, name))
        return json.loads((FIX / f"{self.override.get(name, name)}.json").read_text())


def test_load_deployment(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    p = tmp_path / "wrangler.toml"
    p.write_text(WRANGLER)
    assert load_deployment(p) == DEP
    assert load_deployment(p, account="other").account == "other"


def test_ae_sql():
    assert ae_sql("disky_usage_mine", dt.date(2026, 10, 19), dt.date(2026, 10, 21), ("caller", "via")) == (
        "SELECT toDate(timestamp) AS day, blob4 AS caller, blob3 AS via, SUM(_sample_interval) AS requests, "
        "SUM(_sample_interval * double2) AS wall_ms, SUM(_sample_interval * double3) AS r2_reads, "
        "SUM(_sample_interval * double4) AS r2_bytes, SUM(_sample_interval * double5) AS d1_queries, "
        "SUM(_sample_interval * double6) AS d1_rows_read, SUM(_sample_interval * double7) AS kv_reads, "
        "SUM(_sample_interval * double8) AS d1_rows_written "
        "FROM disky_usage_mine WHERE timestamp >= toDateTime('2026-10-19 00:00:00') AND timestamp < toDateTime('2026-10-21 00:00:00') "
        "AND blob8 = 'prod' GROUP BY day, caller, via ORDER BY day, caller, via FORMAT JSON"
    )
    assert ae_sql("d", dt.date(2026, 10, 19), dt.date(2026, 10, 20), ("route",), host=None) == (
        "SELECT toDate(timestamp) AS day, blob1 AS route, SUM(_sample_interval) AS requests, "
        "SUM(_sample_interval * double2) AS wall_ms, SUM(_sample_interval * double3) AS r2_reads, "
        "SUM(_sample_interval * double4) AS r2_bytes, SUM(_sample_interval * double5) AS d1_queries, "
        "SUM(_sample_interval * double6) AS d1_rows_read, SUM(_sample_interval * double7) AS kv_reads, "
        "SUM(_sample_interval * double8) AS d1_rows_written "
        "FROM d WHERE timestamp >= toDateTime('2026-10-19 00:00:00') AND timestamp < toDateTime('2026-10-20 00:00:00') "
        "GROUP BY day, route ORDER BY day, route FORMAT JSON"
    )


UNITS = list(PRICES)


def test_report_units_costs_overage():
    fetch = Recorded()
    r = build_report(DEP, "tok", 2, fetch=fetch, today=TODAY, top=2)
    assert [c[2] for c in fetch.calls] == ["functions", "r2_ops", "r2_storage", "d1", "kv_ops", "kv_storage", "ae_caller", "ae_route"]
    assert {c[1] for c in fetch.calls} == {"tok"}
    assert (r.since, r.until, r.notes) == ("2026-10-19", "2026-10-21", [])
    # Units, in `PRICES` order: requests (production + preview; `pages-worker--7` excluded), CPU ms (requests × the
    # quantiles' trapezoid mean µs / 1000, capped at the wall time: 10-19 = 60K × 3,510 µs + preview's 40K × 875 µs
    # capped at its 10 s of wall; 10-20 = 200K × 1,312.5 µs), R2 A
    # (PutObject; ListObjects), R2 B (GetObject + HeadObject; DeleteObject free), R2 GB, D1 rows read / written,
    # KV reads, KV writes (write; delete + list), KV GB.
    assert r.units == {
        "2026-10-19": pytest.approx(dict(zip(UNITS, [1e5, 220_600, 20_000, 5e6, 401.0, 3e9, 1e6, 5e4, 3000, 2.0]))),
        "2026-10-20": pytest.approx(dict(zip(UNITS, [2e5, 262_500, 1000, 6e6, 403.0, 5e9, 2e6, 7e4, 150, 2.5]))),
    }
    assert r.costs == {
        "2026-10-19": pytest.approx(dict(zip(UNITS, [0.03, 0.004412, 0.09, 1.8, 401 * 0.015 / 30, 3.0, 1.0, 0.025, 0.015, 2 * 0.5 / 30]))),
        "2026-10-20": pytest.approx(dict(zip(UNITS, [0.06, 0.00525, 0.0045, 2.16, 403 * 0.015 / 30, 5.0, 2.0, 0.035, 0.00075, 2.5 * 0.5 / 30]))),
    }
    # Two days against the monthly included amounts × 2/30; storage is the window's mean level.
    usd = {u: o["usd"] for u, o in r.overage.items()}
    assert usd == pytest.approx(dict(zip(UNITS, [
        0, 0, 0,
        (11e6 - 10e6 / 15) / 1e6 * 0.36,
        (402 - 10 / 15) * 0.015 * 2 / 30,
        (8e9 - 25e9 / 15) / 1e6 * 0.001,
        0, 0, 0,
        (2.25 - 1 / 15) * 0.5 * 2 / 30,
    ])))
    assert r.overage["r2_class_b"] == pytest.approx({"total": 11e6, "included": 10e6 / 15, "over": 11e6 - 10e6 / 15, "usd": 3.72})
    # Attribution: summed over days, top 2 by requests, the rest folded; string sums parsed.
    assert r.by_caller == [
        {"caller": "session", "requests": 2000.0, "wall_ms": 560000.0, "r2_reads": 7800.0, "r2_bytes": 156e6, "d1_queries": 4000.0, "d1_rows_read": 200000.0, "kv_reads": 500.0, "d1_rows_written": 400.0},
        {"caller": "sheet sync", "requests": 300.0, "wall_ms": 150000.0, "r2_reads": 9000.0, "r2_bytes": 180e6, "d1_queries": 600.0, "d1_rows_read": 900000.0, "kv_reads": 0.0, "d1_rows_written": 2000.0},
        {"caller": "(other)", "requests": 70.0, "wall_ms": 5400.0, "r2_reads": 0.0, "r2_bytes": 0.0, "d1_queries": 20.0, "d1_rows_read": 20.0, "kv_reads": 0.0, "d1_rows_written": 0.0},
    ]
    assert r.by_route == [
        {"route": "/api/subtree", "requests": 900.0, "wall_ms": 450000.0, "r2_reads": 4800.0, "r2_bytes": 96e6, "d1_queries": 1800.0, "d1_rows_read": 100000.0, "kv_reads": 300.0, "d1_rows_written": 10.0},
        {"route": "page", "requests": 400.0, "wall_ms": 8000.0, "r2_reads": 0.0, "r2_bytes": 0.0, "d1_queries": 400.0, "d1_rows_read": 400.0, "kv_reads": 0.0, "d1_rows_written": 0.0},
    ]


def test_render():
    r = build_report(DEP, "tok", 2, fetch=Recorded(), today=TODAY, top=2)
    assert render(r).split("\n") == [
        "my-deployment: 2026-10-19 … 2026-10-21 (UTC, until exclusive)",
        "",
        "day\tFunctions requests\tFunctions CPU ms (est.)\tR2 Class A ops\tR2 Class B ops\tR2 storage GB-mo\tD1 rows read\tD1 rows written\tKV reads\tKV writes/deletes/lists\tKV storage GB-mo\t$ list",
        "2026-10-19\t100K\t221K\t20K\t5M\t401\t3G\t1M\t50K\t3K\t2\t6.20",
        "2026-10-20\t200K\t262K\t1K\t6M\t403\t5G\t2M\t70K\t150\t2.5\t9.51",
        "",
        "window vs plan (pro rata):",
        "  Functions requests: 300K of 667K included → $0.00 over",
        "  Functions CPU ms (est.): 483K of 2M included → $0.00 over",
        "  R2 Class A ops: 21K of 66.7K included → $0.00 over",
        "  R2 Class B ops: 11M of 667K included → $3.72 over",
        "  R2 storage GB-mo: 402 of 0.667 included → $0.40 over",
        "  D1 rows read: 8G of 1.67G included → $6.33 over",
        "  D1 rows written: 3M of 3.33M included → $0.00 over",
        "  KV reads: 120K of 667K included → $0.00 over",
        "  KV writes/deletes/lists: 3.15K of 66.7K included → $0.00 over",
        "  KV storage GB-mo: 2.25 of 0.0667 included → $0.07 over",
        "",
        "by caller:",
        "caller\trequests\tR2 reads\tR2 bytes\tD1 rows read\tD1 rows written\tKV reads\tms/req",
        "session\t2K\t7.8K\t156M\t200K\t400\t500\t280",
        "sheet sync\t300\t9K\t180M\t900K\t2K\t0\t500",
        "(other)\t70\t0\t0\t20\t0\t0\t77",
        "",
        "by route:",
        "route\trequests\tR2 reads\tR2 bytes\tD1 rows read\tD1 rows written\tKV reads\tms/req",
        "/api/subtree\t900\t4.8K\t96M\t100K\t10\t300\t500",
        "page\t400\t0\t0\t400\t0\t0\t20",
    ]


def test_unauthorized_dataset_is_a_note_not_a_failure():
    r = build_report(DEP, "tok", 2, fetch=Recorded({"d1": "graphql_unauthorized"}), today=TODAY)
    assert r.notes == ["d1: not authorized for that account"]
    assert [(d, us["d1_rows_read"], us["r2_class_b"]) for d, us in r.units.items()] == [("2026-10-19", 0.0, 5e6), ("2026-10-20", 0.0, 6e6)]


def test_unmatched_script_and_missing_resources():
    dep = Deployment(account="acct123", project="nope", buckets=(), databases=(), namespaces=(), dataset=None)
    calls: list[str] = []

    def f(url, token, body, ct):
        calls.append(json.loads(body)["query"].split("(limit")[0].rsplit(None, 1)[-1])
        return json.loads((FIX / "functions.json").read_text())

    r = build_report(dep, "tok", 2, fetch=f, today=TODAY)
    assert calls == ["pagesFunctionsInvocationsAdaptiveGroups"]
    assert r.notes == [
        "functions: no CF_PAGES_SCRIPT for 'nope' (the Pages API's `production_script_name` without `-production`); the account's total",
        "attribution: no analytics_engine_datasets in the wrangler config",
    ]
    assert {d: us["functions_requests"] for d, us in r.units.items()} == {"2026-10-19": 1e5, "2026-10-20": 200999.0}


def test_cli_needs_the_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CF_ANALYTICS_TOKEN", raising=False)
    p = tmp_path / "wrangler.toml"
    p.write_text(WRANGLER)
    res = CliRunner().invoke(cli, ["-c", str(p)])
    assert (res.exit_code, res.output.strip().split("\n")[-1]) == (
        2, "Error: set $CF_ANALYTICS_TOKEN: a Cloudflare API token with Account Analytics: Read on the account",
    )


def test_mean_cpu_us():
    # The quantile function by trapezoids from (0, 0) to (0.999, p99.9), held to 1.
    q = dict(zip([n for n, _ in CPU_QUANTILES], [1000, 2000, 4000, 8000, 10000, 20000, 40000]))
    assert mean_cpu_us(q) == pytest.approx(125 + 375 + 750 + 900 + 450 + 600 + 270 + 40)
    assert mean_cpu_us(dict.fromkeys([n for n, _ in CPU_QUANTILES], 1000)) == pytest.approx(875)


def test_functions_split_by_script_env():
    r = build_report(DEP, "tok", 2, fetch=Recorded(), today=TODAY)
    assert r.functions == {
        "2026-10-19": {"preview": {"requests": 40000.0, "cpu_ms": 10000.0}, "production": {"requests": 60000.0, "cpu_ms": pytest.approx(210600)}},
        "2026-10-20": {"production": {"requests": 200000.0, "cpu_ms": 262500.0}},
    }


def _units(vals: list[float]) -> dict[str, float]:
    return dict(zip(UNITS, vals))


PLAN = {u: {"label": p.label, "usd": p.usd, "per": int(p.per), "included": int(p.included), "storage": u in ("r2_storage_gb", "kv_storage_gb")}
        for u, p in PRICES.items()}


def test_snapshot_shape():
    r = build_report(DEP, "tok", 2, fetch=Recorded(), today=TODAY)
    assert snapshot(r, "2026-10-20", top=1) == {
        "version": 1,
        "date": "2026-10-20",
        "project": "my-deployment",
        "units": _units([200000, 262500, 1000, 6000000, 403, 5000000000, 2000000, 70000, 150, 2.5]),
        "usd": _units([0.06, 0.00525, 0.0045, 2.16, 0.2015, 5, 2, 0.035, 0.00075, 0.041667]),
        "plan": PLAN,
        "functions": {"production": {"requests": 200000, "cpu_ms": 262500}},
        # the day's rows only, most requests first; past `top`, folded (`via` empty)
        "by_caller": [
            {"caller": "session", "via": "session", "requests": 800, "wall_ms": 200000, "r2_reads": 3000, "r2_bytes": 60000000,
             "d1_queries": 1600, "d1_rows_read": 80000, "kv_reads": 200, "d1_rows_written": 0},
            {"caller": "(other)", "via": "", "requests": 350, "wall_ms": 155000, "r2_reads": 9000, "r2_bytes": 180000000,
             "d1_queries": 600, "d1_rows_read": 900000, "kv_reads": 0, "d1_rows_written": 2000},
        ],
        "by_route": [
            {"route": "page", "requests": 400, "wall_ms": 8000, "r2_reads": 0, "r2_bytes": 0, "d1_queries": 400,
             "d1_rows_read": 400, "kv_reads": 0, "d1_rows_written": 0},
        ],
        "notes": [],
    }
    # A day with no usage at all: every unit present, zero.
    empty = snapshot(r, "2026-10-18")
    assert (empty["units"], empty["usd"], empty["functions"], empty["by_caller"], empty["by_route"]) == (
        _units([0] * 10), _units([0] * 10), {}, [], [])


def test_cli_writes_the_missing_days_to_each_root(tmp_path, monkeypatch):
    import dt_cloud.cf_usage as M

    monkeypatch.setenv("CF_ANALYTICS_TOKEN", "tok\n")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    fetch = Recorded()
    monkeypatch.setattr(M, "http_fetch", fetch)
    orig = M.window
    monkeypatch.setattr(M, "window", lambda days, today=None: orig(days, TODAY))
    cfg = tmp_path / "wrangler.toml"
    cfg.write_text(WRANGLER)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (a / "2026-10-20.json").write_text("kept")
    res = CliRunner().invoke(cli, ["-c", str(cfg), "-d", "2", "-o", str(a), "-o", str(b)])
    assert res.exit_code == 0, res.output
    assert res.stdout.split("\n") == [f"{a}/2026-10-19.json", f"{b}/2026-10-19.json", f"{b}/2026-10-20.json", ""]
    assert {c[1] for c in fetch.calls} == {"tok"}
    assert (a / "2026-10-20.json").read_text() == "kept"
    r = build_report(DEP, "tok", 2, fetch=Recorded(), today=TODAY, host=None)
    assert json.loads((b / "2026-10-19.json").read_text()) == snapshot(r, "2026-10-19")
    assert (b / "2026-10-19.json").read_bytes() == (a / "2026-10-19.json").read_bytes()
    # Rerun: nothing missing, nothing fetched.
    fetch.calls.clear()
    res = CliRunner().invoke(cli, ["-c", str(cfg), "-d", "2", "-o", str(a), "-o", str(b)])
    assert (res.exit_code, res.stdout, fetch.calls) == (0, "", [])
