"""Specs for the cache warm-up plan (`dt_cloud.warm`): exact request lists."""
from http.client import IncompleteRead, RemoteDisconnected

from dt_cloud import warm as W
import pytest

DATES = [f"2026-08-{d:02d}" for d in range(10, 32)] + [f"2026-09-{d:02d}" for d in range(1, 16)]


def test_nearest_prior_resolves_like_the_site():
    # 7 days back from 9/15 is 9/8 exactly; 30 days back is 8/16
    assert W.nearest_prior(DATES, "2026-09-15", 7) == "2026-09-08"
    assert W.nearest_prior(DATES, "2026-09-15", 30) == "2026-08-16"
    # a missing scan resolves to the nearest neighbour, never the scan itself
    dates = [d for d in DATES if d != "2026-09-08"]
    assert W.nearest_prior(dates, "2026-09-15", 7) in ("2026-09-07", "2026-09-09")
    assert W.nearest_prior(DATES, "2026-09-15", 0) == "2026-09-14"
    assert W.nearest_prior(["2026-09-15"], "2026-09-15", 1) is None


def test_plan_one_width():
    assert W.plan("2026-09-15", DATES, widths=(1280,), spans=(1, 7), paths=("",)) == [
        "/api/series?path=&split=roots",
        "/api/subtree?date=2026-09-15&path=&w=1280&h=768",
        "/api/diff?from=2026-09-14&to=2026-09-15&path=&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-14&to=2026-09-15&path=&w=1280&h=768",
        "/api/diff?from=2026-09-08&to=2026-09-15&path=&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-08&to=2026-09-15&path=&w=1280&h=768",
    ]


TOP = ("", "b1", "b2", "b3")


def test_plan_dedupes_pairs_and_counts():
    # the previous-scan pair and the 1d chip are the same pair: 5 spans + prev → 5 distinct pairs;
    # × (root + 3 buckets) × 5 widths
    paths = W.plan("2026-09-15", DATES, TOP)
    # plus one width-independent series per path, first
    assert len(paths) == len(TOP) + len(W.WIDTHS) * len(TOP) * (1 + 2 * 5)
    assert paths[:2] == ["/api/series?path=&split=roots", "/api/series?path=b1"]
    n = len(TOP)
    assert paths[n] == "/api/subtree?date=2026-09-15&path=&w=512&h=307"
    assert paths[n + 11] == "/api/subtree?date=2026-09-15&path=b1&w=512&h=307"
    assert paths[-1] == "/api/diff?from=2026-08-16&to=2026-09-15&path=b3&w=1920&h=1152"


def test_paths_from_warm_paths_else_the_sites_root_view(monkeypatch):
    monkeypatch.setenv("WARM_PATHS", ",b1,b1/hot")
    assert W.env_paths() == ("", "b1", "b1/hot")
    monkeypatch.delenv("WARM_PATHS")
    assert W.env_paths() is None
    # the root view's children, folds left out
    assert W.root_children({"tree": {"n": "all buckets", "c": [{"n": "b1"}, {"n": "b2"}, {"n": "(other)"}]}}) == ("", "b1", "b2")
    assert W.root_children({"tree": {"n": "all buckets"}}) == ("",)


# Sub-daily scan ids (`YYYY-MM-DDTHHMM`, the CoreWeave job): spans resolve on
# their instants like the site's `nearestScan`.
SUBDAILY = [f"2026-08-{d:02d}T{h}" for d in range(10, 32) for h in ("0001", "1201")] + [f"2026-09-{d:02d}T{h}" for d in range(1, 16) for h in ("0001", "1201")]


def test_subdaily_nearest_prior():
    # 7 days back from 9/15 12:01 is 9/8 12:01 exactly; 30 days back is 8/16 12:01
    assert W.nearest_prior(SUBDAILY, "2026-09-15T1201", 7) == "2026-09-08T1201"
    assert W.nearest_prior(SUBDAILY, "2026-09-15T1201", 30) == "2026-08-16T1201"
    # a missing scan resolves to the nearest neighbour (12 h either side), never the scan itself
    dates = [d for d in SUBDAILY if d != "2026-09-08T1201"]
    assert W.nearest_prior(dates, "2026-09-15T1201", 7) in ("2026-09-08T0001", "2026-09-09T0001")
    assert W.nearest_prior(SUBDAILY, "2026-09-15T1201", 0) == "2026-09-15T0001"
    assert W.nearest_prior(["2026-09-15T1201"], "2026-09-15T1201", 1) is None


def test_subdaily_plan_one_width():
    assert W.plan("2026-09-15T1201", SUBDAILY, widths=(1280,), spans=(1, 7), paths=("",)) == [
        "/api/series?path=&split=roots",
        "/api/subtree?date=2026-09-15T1201&path=&w=1280&h=768",
        "/api/diff?from=2026-09-15T0001&to=2026-09-15T1201&path=&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-15T0001&to=2026-09-15T1201&path=&w=1280&h=768",
        "/api/diff?from=2026-09-14T1201&to=2026-09-15T1201&path=&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-14T1201&to=2026-09-15T1201&path=&w=1280&h=768",
        "/api/diff?from=2026-09-08T1201&to=2026-09-15T1201&path=&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-08T1201&to=2026-09-15T1201&path=&w=1280&h=768",
    ]


def test_widths_match_the_sites_canvas_snap():
    # The site snaps its canvas to these widths (`site/src/canvas.ts`), so a
    # width warmed here and not there (or vice versa) is a cache miss for everyone.
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "site" / "src" / "canvas.ts").read_text()
    m = re.search(r"WARMED_WIDTHS = \[([\d, ]+)\]", src)
    assert m is not None
    assert tuple(int(w) for w in m.group(1).split(",")) == W.WIDTHS


def test_top_paths_sends_a_user_agent(monkeypatch):
    # Cloudflare bans urllib's default User-Agent (error 1010 → 403), so the root-view fetch carries the same UA as
    # the warm requests (hf's first warm-cache, 2026-10-16, 403'd here before a single path was warmed)
    import io
    import json

    sent = []

    def urlopen(req, timeout):
        sent.append((req.full_url, dict(req.header_items())))
        return io.BytesIO(json.dumps({"tree": {"n": "", "c": [{"n": "buckets"}, {"n": "models"}]}}).encode())

    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    assert W.top_paths("https://x.test/", {"Authorization": "Bearer t"}, "2026-10-16T0801") == ("", "buckets", "models")
    assert sent == [(
        "https://x.test/api/subtree?date=2026-10-16T0801&path=&w=1280&h=768&depth=1",
        {"Authorization": "Bearer t", "User-agent": "gcs-usage-warm/1.0"},
    )]


def test_summary_counts_refusals_apart_from_failures():
    """A 409 (a view the site refuses, e.g. a diff from before a union store's first scan) is reported as refused, not failed."""
    res = [("/a", 200, 1.2, "miss"), ("/b", 409, 0.3, ""), ("/c", 409, 0.2, ""), ("/d", 500, 2.0, ""), ("/e", 0, 0.1, "URLError")]
    assert W.summary(res, "2026-10-16T0716") == "warm-cache: 1/5 warmed for 2026-10-16T0716 in 4s of request time; 2 refused (409); 2 failed"
    assert W.summary(res[:1], "d") == "warm-cache: 1/1 warmed for d in 1s of request time"


@pytest.mark.parametrize("statuses,code,summary", [
    ([200, 409], 0, "warm-cache: 1/2 warmed for 2026-10-20 in 2s of request time; 1 refused (409)"),
    ([200, 500], 1, "warm-cache: 1/2 warmed for 2026-10-20 in 2s of request time; 1 failed"),
    ([403], 1, "warm-cache: 0/1 warmed for 2026-10-20 in 1s of request time; 1 failed"),
])
def test_warm_cli_exits_nonzero_for_failed_requests(monkeypatch, statuses, code, summary):
    from click.testing import CliRunner
    from dt_cloud import cli

    monkeypatch.setenv("SITE_TOKEN", "test")
    monkeypatch.setattr(W, "scan_dates", lambda _: ["2026-10-20"])
    monkeypatch.setattr(W, "env_paths", lambda: ("",))
    monkeypatch.setattr(W, "warm", lambda *a, **kw: [("/a", status, 1.0, "") for status in statuses])
    logs = []
    monkeypatch.setattr(cli, "err", logs.append)
    result = CliRunner().invoke(cli.warm_cache, ["-r", "local", "-u", "https://site.test"])
    assert (result.exit_code, result.stdout, logs) == (code, "", [summary])


@pytest.mark.parametrize("error", [
    lambda: IncompleteRead(b"{", 9),  # the 18:02 run's 09-23 full diff: a body cut short of its Content-Length
    lambda: RemoteDisconnected("Remote end closed connection without response"),  # cw's 10-23T1801 warm-up
])
def test_warm_refetches_after_a_transport_error(monkeypatch, error):
    """A transport error is fetched again before the request counts as failed; one that recurs every time is a
    transport failure (status 0)."""
    import io

    class Broken(io.BytesIO):
        def read(self, *a):
            raise error()

    class Whole(io.BytesIO):
        status = 200
        headers = {"x-cache": "miss"}

    queue = {"/once": [Broken(), Whole(b"{}")], "/always": [Broken() for _ in range(W.TRANSPORT_RETRIES + 1)]}
    monkeypatch.setattr(W.urllib.request, "urlopen", lambda req, timeout: queue[req.full_url.removeprefix("https://x.test")].pop(0))
    res = W.warm("https://x.test", {}, ["/once", "/always"], jobs=1)
    assert [(p, s, tier) for p, s, _, tier in res] == [("/once", 200, "miss"), ("/always", 0, type(error()).__name__)]
    assert queue == {"/once": [], "/always": []}
