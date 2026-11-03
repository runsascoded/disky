import datetime as dt
import json
import random

import pytest
from click.testing import CliRunner

from dt_cloud.probe import (
    Resp,
    Result,
    Targets,
    cold_min_area,
    compare_requests,
    counts,
    record,
    request_paths,
    resolve_targets,
    run,
    scenarios,
    server_total,
    summarize,
    summary_line,
)

T = Targets(date="2026-10-01", prev="2026-09-30", bucket="bk-a", hit="ckpt")


def ok(body: object = None, ms: int = 100, cache: str | None = "miss", server_ms: int | None = 80) -> Resp:
    b = json.dumps(body).encode() if body is not None else b"{}"
    return Resp(200, ms, len(b), cache, server_ms, b)


def test_server_total():
    assert [
        server_total('auth;dur=22, scans;dur=11, points;dur=7037, total;dur=7200.4'),
        server_total('total;dur=12'),
        server_total('subtotal;dur=5'),
        server_total('cfCacheStatus;desc="DYNAMIC"'),
        server_total(None),
    ] == [7200, 12, None, None, None]


def test_scenarios():
    W = "cv=2&w=1408&h=896"
    D = "from=2026-09-30&to=2026-10-01"
    assert scenarios(T) == {
        "root": [
            f"/api/subtree?{W}&date=2026-10-01&path=&depth=1",
            f"/api/subtree?{W}&date=2026-10-01&path=",
            f"/api/diff?{W}&{D}&path=&depth=1",
            f"/api/diff?{W}&{D}&path=",
            "/api/series?path=",
        ],
        "bucket": [
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a&depth=1",
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a",
            f"/api/diff?{W}&{D}&path=bk-a&depth=1",
            f"/api/diff?{W}&{D}&path=bk-a",
            "/api/series?path=bk-a",
        ],
        "filter-hit": [
            f"/api/subtree?{W}&date=2026-10-01&path=&q=ckpt",
            f"/api/subtree?{W}&date=2026-10-01&path=&q=ckpt&full=1",
            f"/api/diff?{W}&{D}&path=&q=ckpt",
        ],
        "filter-miss": [
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a&q=zz-probe-no-match-zz",
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a&q=zz-probe-no-match-zz&full=1",
            f"/api/diff?{W}&{D}&path=bk-a&q=zz-probe-no-match-zz",
        ],
    }


def test_scenarios_cold_and_single_scan():
    s = scenarios(Targets("2026-10-01", None, "bk/x", "a b"), cold="12.000042")
    assert s["bucket"] == [
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=bk%2Fx&depth=1",
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=bk%2Fx",
        "/api/series?path=bk%2Fx",
    ]
    assert s["filter-hit"] == [
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=&q=a%20b",
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=&q=a%20b&full=1",
    ]


def test_cold_min_area():
    assert [cold_min_area(random.Random(s)) for s in (1, 2)] == ["12.140892", "12.905036"]


def test_resolve_targets():
    canned = {
        "/data/scans.json": ["2026-10-01", "2026-09-30", "2026-09-29"],
        "/api/subtree?date=2026-10-01&path=&depth=1&w=1408&h=896": {"tree": {"c": [{"n": "bk-small", "b": 5}, {"n": "bk-big", "b": 9}]}},
        "/api/subtree?date=2026-10-01&path=bk-big&depth=1&w=1408&h=896": {"tree": {"c": [{"n": "(other)", "b": 99}, {"n": "tmp", "b": 3}, {"n": "ckpt", "b": 7}]}},
    }
    seen = []

    def fetch(path: str) -> Resp:
        seen.append(path)
        return ok(canned[path])

    assert resolve_targets(fetch) == Targets("2026-10-01", "2026-09-30", "bk-big", "ckpt")
    assert resolve_targets(fetch, hit="given") == Targets("2026-10-01", "2026-09-30", "bk-big", "given")
    assert seen == [*canned, "/data/scans.json", "/api/subtree?date=2026-10-01&path=&depth=1&w=1408&h=896"]


def test_run_and_summarize():
    resp = {
        "/a": ok(ms=1200, cache="hit", server_ms=None),
        "/b": ok(ms=3400),
        "/c": Resp(503, 22000, 17, None, None, b"error code: 1102"),
        "/d": ok(ms=900),
    }
    results = run(resp.__getitem__, {"one": ["/a", "/b"], "two": ["/c", "/d"]})
    assert results == [
        Result("one", "/a", 200, 1200, 2, "hit", None),
        Result("one", "/b", 200, 3400, 2, "miss", 80),
        Result("two", "/c", 503, 22000, 17, None, None, error="error code: 1102"),
        Result("two", "/d", 200, 900, 2, "miss", 80),
    ]
    assert summarize(results) == (False, [
        "  ✓ one            3.40s  2 reqs, 1 cached",
        "  ✗ two           22.00s  2 reqs, 0 cached, 1 failed",
        "      503 22.00s /c",
    ])
    assert summarize(results[:2], budget_ms=3000) == (False, ["  ✗ one            3.40s  2 reqs, 1 cached"])
    assert summarize(results[:2], budget_ms=4000) == (True, ["  ✓ one            3.40s  2 reqs, 1 cached"])


@pytest.mark.parametrize("status,passed", [(200, True), (409, True), (0, False), (301, False), (400, False), (401, False), (403, False), (404, False), (413, False), (429, False), (500, False)])
def test_probe_rejects_http_errors_including_auth_and_read_budget(status, passed):
    result = Result("one", "/a", status, 1000, 0, None, None)
    expected = ["  ✓ one            1.00s  1 reqs, 0 cached"] if passed else [
        "  ✗ one            1.00s  1 reqs, 0 cached, 1 failed", f"      {status} 1.00s /a",
    ]
    assert summarize([result]) == (passed, expected)


@pytest.mark.parametrize("status,body,error", [
    (200, b"<html>Sign in</html>", "invalid JSON response: <html>Sign in</html>"),
    (200, b"", "invalid JSON response: "),
    (403, b"denied", "denied"),
])
def test_probe_rejects_non_json_success_and_records_http_failure_body(status, body, error):
    results = run(lambda path: Resp(status, 1000, len(body), None, None, body), {"one": ["/api/subtree"]})
    assert results == [Result("one", "/api/subtree", status, 1000, len(body), None, None, error=error)]
    assert summarize(results) == (False, [
        "  ✗ one            1.00s  1 reqs, 0 cached, 1 failed",
        f"      {status} 1.00s /api/subtree",
    ])


NOT_INDEXED = json.dumps({"error": "This scan is not in the static name index yet. This is not a zero-match result.", "code": "scan-not-indexed"}).encode()
SKIP = "static-names skipped (busy): another coordinator holds the store's lease"


def refused(body: bytes = NOT_INDEXED, status: int = 400) -> Resp:
    return Resp(status, 300, len(body), None, None, body)


def test_not_indexed_after_a_recorded_static_names_skip_is_skipped():
    """The run's static-names step was skipped (its phase record's reason, `-i`): the search's `scan-not-indexed` refusal
    is a skip, named, and the probe passes; it never counts as a pass."""
    resp = {"/a": ok(ms=1000), "/q": refused(), "/q-full": refused()}
    results = run(resp.__getitem__, {"bucket": ["/a"], "filter-hit": ["/q", "/q-full"]}, unindexed=SKIP)
    assert results == [
        Result("bucket", "/a", 200, 1000, 2, "miss", 80),
        Result("filter-hit", "/q", 400, 300, len(NOT_INDEXED), None, None, error=NOT_INDEXED.decode(), skip=SKIP),
        Result("filter-hit", "/q-full", 400, 300, len(NOT_INDEXED), None, None, error=NOT_INDEXED.decode(), skip=SKIP),
    ]
    assert summarize(results) == (True, [
        "  ✓ bucket         1.00s  1 reqs, 0 cached",
        "  – filter-hit     0.30s  2 reqs, 0 cached, 2 skipped",
        f"      skipped 400 0.30s /q: {SKIP}",
        f"      skipped 400 0.30s /q-full: {SKIP}",
    ])
    assert (counts(results), summary_line(results)) == ((1, 2, 0), f"1 passed, 2 skipped ({SKIP}), 0 failed")


def test_not_indexed_without_a_recorded_skip_is_failed():
    """No static-names skip in the run's record: the scan should be indexed, so its refusal is a failure, named as the
    index's lag (the healthcheck's probe: gcs's index held behind by its compaction, 10-22 → 10-24)."""
    results = run({"/a": ok(ms=1000), "/q": refused()}.__getitem__, {"filter-hit": ["/a", "/q"]})
    assert results == [
        Result("filter-hit", "/a", 200, 1000, 2, "miss", 80),
        Result("filter-hit", "/q", 400, 300, len(NOT_INDEXED), None, None, error=NOT_INDEXED.decode()),
    ]
    assert summarize(results) == (False, [
        "  ✗ filter-hit     1.00s  2 reqs, 0 cached, 1 failed",
        "      400 0.30s /q (scan-not-indexed)",
    ])
    assert summary_line(results) == "1 passed, 0 skipped, 1 failed (1 scan-not-indexed: the static name index lags this scan)"


Q = "/api/subtree?date=2026-10-01&path=&q=ckpt"
D = "/api/diff?from=2026-09-30&to=2026-10-01&path=&q=ckpt"


def filter_scans(scans: object) -> Resp:
    return ok({"scans": scans}, ms=5)


@pytest.mark.parametrize("unindexed", [None, SKIP])
def test_not_indexed_on_an_indexed_scan_is_failed_and_named_apart(unindexed):
    """The refusal on scans `/api/filter-scans` lists: not a lag but the reader declining over its tiers (cw 10-24 →
    10-29, 37 unmerged runs), failed even beside a recorded static-names skip; the index's list is fetched once."""
    seen = []
    resp = {Q: refused(), D: refused(), "/api/filter-scans": filter_scans(["2026-09-30", "2026-10-01"])}

    def fetch(path):
        seen.append(path)
        return resp[path]
    results = run(fetch, {"filter-hit": [Q, D]}, parallel=False, unindexed=unindexed)
    # the list is fetched beside the page load's requests, so in no fixed order among them
    assert sorted(seen) == sorted([Q, D, "/api/filter-scans"])
    assert results == [
        Result("filter-hit", Q, 400, 300, len(NOT_INDEXED), None, None, error=NOT_INDEXED.decode(), indexed=True),
        Result("filter-hit", D, 400, 300, len(NOT_INDEXED), None, None, error=NOT_INDEXED.decode(), indexed=True),
    ]
    assert summarize(results) == (False, [
        "  ✗ filter-hit     0.30s  2 reqs, 0 cached, 2 failed",
        f"      400 0.30s {Q} (scan-not-indexed on an indexed scan)",
        f"      400 0.30s {D} (scan-not-indexed on an indexed scan)",
    ])
    assert summary_line(results) == ("0 passed, 0 skipped, 2 failed (2 scan-not-indexed refused on an indexed scan (over the tiers' "
                                     "row budget? check the static index's unmerged runs))")
    assert record("https://x.test", T, results, dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc))["summary"] == {
        "passed": 0, "skipped": 0, "failed": 2, "lag": 0, "refused_indexed": 2, "line": summary_line(results)}


@pytest.mark.parametrize("scans, indexed", [(["2026-09-30"], False), (None, None)])
def test_not_indexed_on_a_scan_the_index_lacks_is_its_lag(scans, indexed):
    """The index doesn't list the scan (or has no list: the static filter off): the refusal is the index's lag, as
    before; with a recorded static-names skip, skipped. The diff names a scan it does list, and one it doesn't."""
    resp = {Q: refused(), D: refused(), "/api/filter-scans": filter_scans(scans)}
    results = run(resp.__getitem__, {"filter-hit": [Q, D]}, parallel=False)
    assert results == [
        Result("filter-hit", Q, 400, 300, len(NOT_INDEXED), None, None, error=NOT_INDEXED.decode(), indexed=indexed),
        Result("filter-hit", D, 400, 300, len(NOT_INDEXED), None, None, error=NOT_INDEXED.decode(), indexed=indexed),
    ]
    assert summary_line(results) == "0 passed, 0 skipped, 2 failed (2 scan-not-indexed: the static name index lags this scan)"
    assert [r.skip for r in run(resp.__getitem__, {"filter-hit": [Q, D]}, parallel=False, unindexed=SKIP)] == [SKIP, SKIP]


@pytest.mark.parametrize("resp", [
    refused(b"error code: 1102", 503),
    refused(status=500),
    Resp(0, 120000, 0, None, None, b""),
    refused(json.dumps({"error": "x", "code": "too-short"}).encode()),
])
def test_other_failures_stay_failed_beside_a_recorded_skip(resp):
    """5xx, a transport timeout, any other refusal: failed, even when the run's static-names step was skipped."""
    results = run(lambda path: resp, {"filter-hit": ["/q"]}, unindexed=SKIP)
    assert [r.skip for r in results] == [None]
    assert summary_line(results) == "0 passed, 0 skipped, 1 failed"
    assert summarize(results)[0] is False


def test_record():
    r = [Result("one", "/a", 200, 5, 2, None, None)]
    now = dt.datetime(2026, 10, 2, 1, 2, 3, tzinfo=dt.timezone.utc)
    assert record("https://x.test", T, r, now) == {
        "ts": "2026-10-02T01:02:03Z", "base": "https://x.test",
        "date": "2026-10-01", "prev": "2026-09-30", "bucket": "bk-a", "hit": "ckpt",
        "results": [{"scenario": "one", "path": "/a", "status": 200, "ms": 5, "bytes": 2, "cache": None, "server_ms": None, "server_timing": None, "error": None, "skip": None, "indexed": None}],
        "summary": {"passed": 1, "skipped": 0, "failed": 0, "lag": 0, "refused_indexed": 0, "line": "1 passed, 0 skipped, 0 failed"},
    }


def test_exact_requests_only_cold_key_endpoints_that_use_it():
    paths = ("/api/subtree?path=&o=unowned&minArea=12", "/api/diff?from=d1&to=d2&lens=user:jane", "/api/series?path=&o=unowned")
    assert request_paths(paths) == list(paths)
    assert request_paths(paths, "12.000042") == [
        "/api/subtree?path=&o=unowned&minArea=12.000042",
        "/api/diff?from=d1&to=d2&lens=user%3Ajane&minArea=12.000042",
        "/api/series?path=&o=unowned",
    ]


def test_complete_response_parity_not_just_root_totals():
    actual = {"/api/series": b'{"path":"", "points":[{"b":10,"o":2}]}', "/api/diff": b'{"total":10,"child":2}'}
    baseline = {"/api/series": b'{"points":[{"o":2,"b":10}],"path":""}', "/api/diff": b'{"total":10,"child":3}'}
    def response(body):
        return Resp(200, 10, len(body), "miss", 8, body)
    got, before, parity = compare_requests(lambda path: response(actual[path]), lambda path: response(baseline[path]), list(actual))
    assert got == [Result("requests", path, 200, 10, len(body), "miss", 8) for path, body in actual.items()]
    assert before == [Result("requests", path, 200, 10, len(body), "miss", 8) for path, body in baseline.items()]
    assert parity == [{"path": "/api/series", "equal": True}, {"path": "/api/diff", "equal": False}]


def test_preserves_phase_timings_and_bounded_failure_details():
    timing = "auth;dur=16, points;dur=7896, total;dur=8186"
    responses = {
        "/api/series": Resp(200, 8200, 2, "miss", 8186, b"{}", timing),
        "/api/diff": Resp(503, 200, 1005, None, None, b"x" * 1005),
    }
    assert run(responses.__getitem__, {"requests": list(responses)}, parallel=False) == [
        Result("requests", "/api/series", 200, 8200, 2, "miss", 8186, timing),
        Result("requests", "/api/diff", 503, 200, 1005, None, None, error="x" * 1000),
    ]


@pytest.mark.parametrize("path", ["https://other.test/api/series", "//other.test/api/series", "/data/meta.json", "/api/../auth", "/api/series#token"])
def test_exact_requests_refuse_other_origins_and_non_api_paths(path):
    with pytest.raises(ValueError, match="^probe requests must be relative /api/ paths without a fragment$"):
        request_paths((path,))


def test_exact_request_cli_skips_discovery_and_records_details(monkeypatch):
    from dt_cloud.cli import probe
    from dt_cloud import probe as lib, site

    calls = []
    logs = []
    timing = "auth;dur=1, points;dur=9, total;dur=10"
    def fetch(path):
        calls.append(path)
        return Resp(200, 12, 2, "miss", 10, b"{}", timing)
    monkeypatch.setattr(site, "creds", lambda token, url: ("https://gcs.test", None))
    monkeypatch.setattr(lib, "http_fetch", lambda base, token: fetch)
    monkeypatch.setattr(lib, "cold_min_area", lambda rng: "12.000042")
    monkeypatch.setattr("dt_cloud.cli.err", logs.append)
    result = CliRunner().invoke(probe, ["-c", "-S", "-j", "-d", "2026-10-13T1300", "-R", "/api/subtree?path=&o=unowned", "-R", "/api/series?o=unowned"])
    assert result.exit_code == 0
    assert calls == ["/api/subtree?path=&o=unowned&minArea=12.000042", "/api/series?o=unowned"]
    rec = json.loads(result.stdout)
    assert dt.datetime.strptime(rec.pop("ts"), "%Y-%m-%dT%H:%M:%SZ").tzinfo is None
    assert rec == {
        "base": "https://gcs.test", "date": "2026-10-13T1300", "prev": None, "bucket": "", "hit": "", "cold": True,
        "results": [{"scenario": "requests", "path": path, "status": 200, "ms": 12, "bytes": 2, "cache": "miss", "server_ms": 10, "server_timing": timing, "error": None, "skip": None, "indexed": None} for path in calls],
        "summary": {"passed": 2, "skipped": 0, "failed": 0, "lag": 0, "refused_indexed": 0, "line": "2 passed, 0 skipped, 0 failed"},
    }
    assert logs == ["probe https://gcs.test: 2 exact requests; cold subtree/diff keys", "  ✓ requests       0.01s  2 reqs, 0 cached", "PASS: 2 passed, 0 skipped, 0 failed"]
    assert result.stderr == ""


@pytest.mark.parametrize("unindexed,code,verdict", [
    ((), 1, "FAIL: 1 passed, 0 skipped, 1 failed (1 scan-not-indexed: the static name index lags this scan)"),
    (("-i", SKIP), 0, f"PASS: 1 passed, 1 skipped ({SKIP}), 0 failed"),
])
def test_cli_not_indexed_verdict(monkeypatch, unindexed, code, verdict):
    """`-i REASON` (the scan step passes its run's static-names skip): a `scan-not-indexed` refusal is skipped, named
    in the verdict; without it, the refusal fails the probe."""
    from dt_cloud.cli import probe
    from dt_cloud import probe as lib, site

    logs = []
    resp = {"/api/series?path=": ok(ms=10), "/api/subtree?path=&q=ckpt": refused()}
    monkeypatch.setattr(site, "creds", lambda token, url: ("https://gcs.test", None))
    monkeypatch.setattr(lib, "http_fetch", lambda base, token: resp.__getitem__)
    monkeypatch.setattr("dt_cloud.cli.err", logs.append)
    result = CliRunner().invoke(probe, ["-S", *unindexed, *(a for p in resp for a in ("-R", p))])
    assert (result.exit_code, logs[-1]) == (code, verdict)


@pytest.mark.parametrize("option", ["-Q", "-T", "-x", "-k", "-q", "-s", "-r"])
def test_exact_request_cli_refuses_automatic_scenario_options(monkeypatch, option):
    from dt_cloud.cli import probe
    from dt_cloud import site

    monkeypatch.setattr(site, "creds", lambda token, url: ("https://gcs.test", None))
    value = "2" if option == "-r" else "value"
    result = CliRunner().invoke(probe, ["-R", "/api/series", option, value])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.rstrip().splitlines() == [
        "Usage: probe [OPTIONS]",
        "Try 'probe --help' for help.",
        "",
        "Error: exact-request mode cannot combine with query-set or automatic scenario options",
    ]
