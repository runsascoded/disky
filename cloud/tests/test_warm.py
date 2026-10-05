"""Specs for the cache warm-up plan (`dt_cloud.warm`): exact request lists."""
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
