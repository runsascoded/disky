import json

from dt_cloud.session_latency import Row, Sample, render, route_of, samples, table

DAY1 = 1794441600000  # 2026-11-12T00:00:00Z
DAY2 = DAY1 + 86_400_000


def test_route_of():
    assert [route_of(u) for u in [
        "/api/subtree?cv=6&date=2026-11-13T0703&path=",
        "https://gcs.oa.dev/api/series?path=&split=roots",
        "/api/scans/2026-11-13T0703/outputs",
        "/api/scans/2026-11-13/",
        "/data/scans.json",
        "https://example.org/other",
    ]] == ["/api/subtree", "/api/series", "/api/scans/<scan>/outputs", "/api/scans/<scan>", None, None]


def test_samples_keep_answered_api_requests():
    batch = json.dumps([
        {"k": "api", "t": DAY1 + 5, "u": "/api/series?path=", "s": 200, "ms": 15890},
        {"k": "api", "t": DAY1 + 6, "u": "/api/subtree?path=", "s": 304, "ms": 40},
        {"k": "api", "t": DAY1 + 7, "u": "/api/diff?path=", "s": 500, "ms": 900},
        {"k": "api", "t": DAY1 + 8, "u": "/api/diff?path=", "s": 0, "ms": 3, "err": "abort"},
        {"k": "api", "t": DAY1 + 9, "u": "/api/actions", "s": 200},
        {"k": "pick", "t": DAY1 + 10, "n": "color"},
        {"k": "api", "t": DAY2 + 1, "u": "/auth/google", "s": 200, "ms": 12},
    ])
    assert samples([batch]) == [Sample("2026-11-12", "/api/series", 15890), Sample("2026-11-12", "/api/subtree", 40)]


def test_table_by_route_and_by_day():
    ss = [Sample("2026-11-12", "/api/series", ms) for ms in (100, 200, 300, 15890)] + [
        Sample("2026-11-13", "/api/series", 120), Sample("2026-11-13", "/api/diff", 50),
    ]
    assert table(ss) == [Row("/api/series", 5, 200, 15890, 15890), Row("/api/diff", 1, 50, 50, 50)]
    assert table([s for s in ss if s.route == "/api/series"], by="day") == [
        Row("2026-11-12", 4, 300, 15890, 15890), Row("2026-11-13", 1, 120, 120, 120),
    ]


def test_render():
    assert render([Row("/api/series", 5, 200, 15890, 15890)], "route") == [
        "route             n      p50      p90      max",
        "/api/series       5      200    15890    15890",
    ]
