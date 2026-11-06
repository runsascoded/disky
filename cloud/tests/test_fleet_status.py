"""`dt-cloud fleet-status`: each site's own API read into one table; an unreadable site is a row, never the end of it."""

import json

import pytest
from click import BadParameter
from click.testing import CliRunner

from dt_cloud import fleet_status
from dt_cloud.cli import main
from dt_cloud.site import SiteError


def run(status: str, kind: str = "scan", issues: list | None = None) -> dict:
    return {"run": {"status": status, "kind": kind}, "phase_issues": issues or []}


HEALTH = {"freshness": {"newest_scan": "2026-11-05T1242", "newest_scan_age": 43200}, "build": {"sha": "576e2b53abcdef", "time": "t"}}
RUNS = {"runs": [
    run("running"),
    run("succeeded"),
    run("nop"),
    run("succeeded", kind='compact'),
    run("succeeded", issues=[{"phase": "warm-cache"}]),
    run("succeeded", issues=[{"phase": "digest", "recovery": {"scan": "x"}}]),
    run("succeeded", issues=[{"phase": "probe", "passing": {"scan": "y"}}]),
    run("failed"),
]}


@pytest.fixture
def sites(monkeypatch):
    """`gcs` answers, `cw` refuses; the calls made, as `(url, token, path)`."""
    calls = []

    def get_json(url: str, token: str, path: str) -> dict:
        calls.append((url, token, path))
        if "cw" in url:
            raise SiteError("https://cw.test/api/health → HTTP 503: down", status=503)
        return HEALTH if path == "/api/health" else RUNS

    monkeypatch.setattr(fleet_status, "get_json", get_json)
    monkeypatch.setenv("GCS_T", "g")
    monkeypatch.setenv("CW_T", "c")
    monkeypatch.delenv("HF_T", raising=False)
    return calls


ARGS = ["fleet-status", "-s", "gcs=https://gcs.test=GCS_T", "-s", "cw=https://cw.test=CW_T", "-s", "hf=https://hf.test=HF_T"]


def test_one_line_per_site_with_the_unreadable_ones_saying_why(sites):
    result = CliRunner().invoke(main, ARGS)
    assert result.exit_code == 0
    assert result.output.rstrip().split("\n") == [
        "gcs  scan 2026-11-05T1242 · 12h ago · …✓~✓✓✗ · build 576e2b53",
        "cw   https://cw.test/api/health → HTTP 503: down",
        "hf   no token ($HF_T)",
    ]
    assert sites == [
        ("https://gcs.test", "g", "/api/health"),
        ("https://gcs.test", "g", "/api/scan-runs"),
        ("https://cw.test", "c", "/api/health"),
    ]


def test_n_keeps_the_newest_runs(sites):
    result = CliRunner().invoke(main, ["fleet-status", "-n", "2", "-s", "gcs=https://gcs.test=GCS_T", "-j"])
    assert json.loads(result.output) == [{
        "name": "gcs", "url": "https://gcs.test", "error": None, "scan": "2026-11-05T1242", "age_s": 43200,
        "runs": ["running", "ok"], "build": "576e2b53",
    }]


@pytest.mark.parametrize("seconds, text", [(None, "?"), (1800, "0h ago"), (43200, "12h ago"), (172800, "2d ago")])
def test_age(seconds, text):
    assert fleet_status.age(seconds) == text


def test_a_bad_site_argument_is_refused():
    with pytest.raises(BadParameter, match="want NAME=URL=TOKEN_VAR"):
        fleet_status.parse_site("gcs=https://gcs.test")
