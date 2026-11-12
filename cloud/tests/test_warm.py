"""Specs for the cache warm-up plan (`dt_cloud.warm`): exact request lists."""
import re
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path

import pytest

from dt_cloud import warm as W

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
        "/api/subtree?cv=4&date=2026-09-15&path=&w=1280&h=768&depth=1",
        "/api/subtree?cv=4&date=2026-09-15&path=&w=1280&h=768",
        "/api/diff?cv=4&from=2026-09-14&to=2026-09-15&path=&w=1280&h=768&dm=delta&depth=1",
        "/api/diff?cv=4&from=2026-09-14&to=2026-09-15&path=&w=1280&h=768&dm=delta",
        "/api/diff?cv=4&from=2026-09-08&to=2026-09-15&path=&w=1280&h=768&dm=delta&depth=1",
        "/api/diff?cv=4&from=2026-09-08&to=2026-09-15&path=&w=1280&h=768&dm=delta",
    ]


def page_urls(src: str, env: dict[str, str]) -> list[str]:
    """Every `/api/subtree` and `/api/diff` URL template in the page's source, each `${…}` expression replaced by its
    value in `env` (a `KeyError` for an expression `env` doesn't give)."""
    return [re.sub(r"\$\{([^}]*)\}", lambda m: env[m[1]], t) for t in re.findall(r"`(/api/(?:subtree|diff)\?[^`]*)`", src)]


def test_the_warmed_urls_are_the_pages():
    """What `warm-cache` asks for a path, a width and a pair is, character for character, what `App.tsx` asks on the
    default view of that path (unscoped, unfiltered, the Δ diff map): its four URL templates, read from the source.
    A parameter the page adds, drops or reorders fails here until the warm-up follows (a diff without `dm=delta`
    is another cache key where `DIFF_DESCENT` is on; one without `depth=1` is not the page's first paint)."""
    src = (Path(__file__).resolve().parents[2] / "site" / "src" / "App.tsx").read_text()
    cv = re.search(r"^const API_CV = '(\d+)'$", src, re.M)
    assert cv is not None
    path = "ctbk%2Fgbfs%2Fagg%3D1m"  # `encodeURIComponent("ctbk/gbfs/agg=1m")`
    page = page_urls(src, {
        "API_CV": cv[1],
        "cutQs": "date=2026-09-15",
        "diffQs": "from=2026-09-14&to=2026-09-15",
        "encodeURIComponent(p)": path,
        "encodeURIComponent(diffPath)": path,
        "canW": "1536",
        "Math.round(canW * 0.6)": "922",
        "scopeQs": "",
        "fq ? '&full=1' : ''": "",
        "fq ? '' : '&depth=1'": "&depth=1",
        "dmMode": "delta",
    })
    urls = [
        "/api/diff?cv=4&from=2026-09-14&to=2026-09-15&path=ctbk%2Fgbfs%2Fagg%3D1m&w=1536&h=922&dm=delta",
        "/api/diff?cv=4&from=2026-09-14&to=2026-09-15&path=ctbk%2Fgbfs%2Fagg%3D1m&w=1536&h=922&dm=delta&depth=1",
        "/api/subtree?cv=4&date=2026-09-15&path=ctbk%2Fgbfs%2Fagg%3D1m&w=1536&h=922",
        "/api/subtree?cv=4&date=2026-09-15&path=ctbk%2Fgbfs%2Fagg%3D1m&w=1536&h=922&depth=1",
    ]
    warmed = W.plan("2026-09-15", ["2026-09-14", "2026-09-15"], ("ctbk/gbfs/agg=1m",), widths=(1536,), spans=())
    assert (sorted(page), warmed[0], sorted(warmed[1:])) == (urls, "/api/series?path=ctbk%2Fgbfs%2Fagg%3D1m", urls)


TOP = ("", "b1", "b2", "b3")


def test_plan_dedupes_pairs_and_counts():
    # the previous-scan pair and the 1d chip are the same pair: 5 spans + prev → 5 distinct pairs, each asked its first
    # paint (`depth=1`) and its full walk (no `summary=1` twin: no page asks it); × (root + 3 buckets) × 5 widths, each
    # with its one level (`depth=1`) and its full view
    paths = W.plan("2026-09-15", DATES, TOP)
    # plus one width-independent series per path, first
    assert len(paths) == len(TOP) + len(W.WIDTHS) * len(TOP) * (2 + 2 * 5)
    assert paths[:2] == ["/api/series?path=&split=roots", "/api/series?path=b1"]
    n = len(TOP)
    assert paths[n] == "/api/subtree?cv=4&date=2026-09-15&path=&w=512&h=307&depth=1"
    assert paths[n + 1] == "/api/subtree?cv=4&date=2026-09-15&path=&w=512&h=307"
    assert paths[n + 12] == "/api/subtree?cv=4&date=2026-09-15&path=b1&w=512&h=307&depth=1"
    assert paths[-1] == "/api/diff?cv=4&from=2026-08-16&to=2026-09-15&path=b3&w=1920&h=1152&dm=delta"


def test_paths_from_warm_paths_else_the_sites_root_view(monkeypatch):
    monkeypatch.setenv("WARM_PATHS", ",b1,b1/hot")
    assert W.env_paths() == ("", "b1", "b1/hot")
    monkeypatch.delenv("WARM_PATHS")
    assert W.env_paths() is None
    # the root view's children, folds left out
    assert W.root_children({"tree": {"name": "all buckets", "children": [{"name": "b1"}, {"name": "b2"}, {"name": "(other)"}]}}) == ("", "b1", "b2")
    assert W.root_children({"tree": {"name": "all buckets"}}) == ("",)


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
        "/api/subtree?cv=4&date=2026-09-15T1201&path=&w=1280&h=768&depth=1",
        "/api/subtree?cv=4&date=2026-09-15T1201&path=&w=1280&h=768",
        "/api/diff?cv=4&from=2026-09-15T0001&to=2026-09-15T1201&path=&w=1280&h=768&dm=delta&depth=1",
        "/api/diff?cv=4&from=2026-09-15T0001&to=2026-09-15T1201&path=&w=1280&h=768&dm=delta",
        "/api/diff?cv=4&from=2026-09-14T1201&to=2026-09-15T1201&path=&w=1280&h=768&dm=delta&depth=1",
        "/api/diff?cv=4&from=2026-09-14T1201&to=2026-09-15T1201&path=&w=1280&h=768&dm=delta",
        "/api/diff?cv=4&from=2026-09-08T1201&to=2026-09-15T1201&path=&w=1280&h=768&dm=delta&depth=1",
        "/api/diff?cv=4&from=2026-09-08T1201&to=2026-09-15T1201&path=&w=1280&h=768&dm=delta",
    ]


def test_widths_match_the_sites_canvas_snap():
    # The site snaps its canvas to these widths (`site/src/canvas.ts`), so a
    # width warmed here and not there (or vice versa) is a cache miss for everyone.
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
        return io.BytesIO(json.dumps({"tree": {"name": "", "children": [{"name": "buckets"}, {"name": "models"}]}}).encode())

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


def test_warm_refetches_after_a_server_error(monkeypatch, capsys):
    """A 5xx is fetched again after a backoff, its body logged (cw's cold `/api/series?split=roots`); one that recurs every
    time fails with its status; a 4xx is a definite answer, never refetched."""
    import io
    from email.message import Message

    class Whole(io.BytesIO):
        status = 200
        headers = {"x-cache": "miss"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def http(code):
        return W.urllib.error.HTTPError("https://x.test", code, "no", Message(), io.BytesIO(b"busy\n"))

    queue = {
        "/once": [http(503), Whole(b"{}")],
        "/always": [http(503) for _ in range(W.SERVER_RETRIES + 1)],
        "/refused": [http(409)],
    }

    def urlopen(req, timeout):
        got = queue[req.full_url.removeprefix("https://x.test")].pop(0)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    sleeps: list[float] = []
    monkeypatch.setattr(W.time, "sleep", sleeps.append)
    res = W.warm("https://x.test", {}, ["/once", "/always", "/refused"], jobs=1)
    assert [(p, s, tier) for p, s, _, tier in res] == [("/once", 200, "miss"), ("/always", 503, ""), ("/refused", 409, "")]
    assert sleeps == [W.SERVER_RETRY_S, W.SERVER_RETRY_S, 2 * W.SERVER_RETRY_S]
    assert queue == {"/once": [], "/always": [], "/refused": []}
    again = [re.sub(r" +[\d.]+s", " <t>", line) for line in capsys.readouterr().err.splitlines() if " again " in line]
    assert again == ["warm: 503 <t> again /once: busy ", "warm: 503 <t> again /always: busy ", "warm: 503 <t> again /always: busy "]
