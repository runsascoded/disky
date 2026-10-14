"""`dt_cloud.bench.score`: an engine's answers against a truth set, with an
injected fetch and a truth fixture."""

import datetime as dt
import json
import random
import urllib.parse

import pytest

from dt_cloud.bench.queryset import Case
from dt_cloud.bench.score import Answer, SubtreeEngine, Truth, line, md5_paths, record, run, score, tally
from dt_cloud.probe import Resp

DATE = "2026-10-01"


def view(v, roots, b, o, hit=False):
    return {"view": v, "root_hit": hit, "roots": len(roots), "md5": md5_paths(roots), "bytes": b, "objects": o, "excluded": 0, "excluded_md5": md5_paths([])}


TRUTH = {
    "ckpt": [view("", ["bk/a", "bk/b"], 30, 3), view("bk", ["bk"], 25, 2, hit=True)],
    "miss": [view("", ["bk/x"], 10, 1)],
    "wrong": [view("", ["bk/q"], 10, 1)],
    "down": [view("", ["bk/d"], 10, 1)],
    "approx": [view("", ["bk/p"], 10, 1)],
    "empty": [view("", ["bk/e"], 0, 1)],
    "wide": [view("", ["bk/w"], 10, 1)],
    "capped": [view("", ["bk/a", "bk/b", "bk/c"], 60, 3)],
}
CASES = [
    Case("ckpt", "ckpt -tmp", "simple", ("", "bk"), ""),
    Case("miss", "xyz", "simple", ("",), ""),
    Case("wrong", "qqq", "simple", ("",), ""),
    Case("down", "ddd", "simple", ("",), ""),
    Case("approx", "ppp.*", "regex", ("",), ""),
    Case("empty", "eee", "simple", ("",), ""),
    Case("wide", "www", "simple", ("",), ""),
]


@pytest.fixture
def truth(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"date": DATE, "queries": [{"id": k, "views": v} for k, v in TRUTH.items()]}))
    for k, vs in TRUTH.items():
        lists = {"miss": [["bk/x", 10, 1]], "wrong": [["bk/q", 10, 1]], "capped": [["bk/a", 10, 1], ["bk/b", 20, 1], ["bk/c", 30, 1]]}
        (tmp_path / f"{k}.json").write_text(json.dumps({"id": k, "views": [{**v, "list": lists.get(k)} for v in vs]}))
    return Truth(str(tmp_path))


def body(matches, b, o, **flags):
    d = {"tree": {"n": "x", "b": b, "o": o}, "tier": "search+path", **({"matches": matches} if matches is not None else {}), **flags}
    return json.dumps(d).encode()


ANSWERS = {
    ("ckpt -tmp", ""): (200, body(["bk/b", "bk/a"], 30, 3)),
    ("ckpt -tmp", "bk"): (200, body(None, 25, 2)),  # the view root matched: the plain view
    ("xyz", ""): (200, body([], 0, 0, partial=True, partialReason="search budget")),
    ("qqq", ""): (200, body(["bk/q"], 8, 1)),  # off by 2 B: past rounding
    ("ddd", ""): (503, b"over capacity"),
    ("ppp.*", ""): (200, body(["bk/p"], 10, 1, approximate=True, approximateReason="regex")),
    ("eee", ""): (200, body([], 0, 0)),
    ("www", ""): (413, b"query too wide: drill deeper or raise minArea"),
    ("cap", ""): (200, body(["bk/a"], 60, 3, matchesTotal=3, matchesTruncated=True)),
}


def fetch(path):
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query, keep_blank_values=True)
    status, b = ANSWERS[(qs["q"][0], qs["path"][0])]
    return Resp(status, 1200, len(b), "miss", 900, b)


def test_url():
    e = SubtreeEngine(fetch, DATE, params="ps=iv", cold=True, rng=random.Random(1))
    assert e.url(CASES[0], "bk/x y") == "/api/subtree?cv=2&w=1408&h=896&minArea=12.140892&date=2026-10-01&path=bk%2Fx%20y&q=ckpt%20-tmp&qs=simple&full=1&ps=iv"


def test_run(truth):
    scores = run(SubtreeEngine(fetch, DATE), CASES, truth, repeat=2)
    assert [(s.id, s.view, s.verdict, s.roots, s.roots_want, s.missing, s.extra, s.b_err, s.ms) for s in scores] == [
        ("ckpt", "", "exact", 2, 2, None, None, 0.0, [1200, 1200]),
        ("ckpt", "bk", "exact", 1, 1, None, None, 0.0, [1200, 1200]),
        ("miss", "", "flagged", 0, 1, 1, 0, -1.0, [1200, 1200]),
        ("wrong", "", "FAIL", 1, 1, None, None, -0.2, [1200, 1200]),
        ("down", "", "error", None, 1, None, None, None, [1200, 1200]),
        ("approx", "", "exact*", 1, 1, None, None, 0.0, [1200, 1200]),
        ("empty", "", "exact", 0, 1, None, None, 0.0, [1200, 1200]),
        ("wide", "", "refused", None, 1, None, None, None, [1200, 1200]),
    ]
    assert [(s.status, s.reason) for s in scores[4::3]] == [(503, "over capacity"), (413, "query too wide: drill deeper or raise minArea")]
    assert tally(scores) == {"exact": 3, "flagged": 1, "FAIL": 1, "error": 1, "exact*": 1, "refused": 1}
    assert [line(s) for s in scores[2:4]] == [
        "flagged  miss                   (root)                                 0/1         -1/+0     -100%     -100% P       1.20s   0.90s",
        "FAIL     wrong                  (root)                                 1/1             —      -20%         0         1.20s   0.90s",
    ]
    rec = record("https://x", SubtreeEngine(fetch, DATE), DATE, "gs://t/", scores, cold=False, repeat=2, now=dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc))
    assert {k: rec[k] for k in ("ts", "base", "engine", "date", "truth", "cold", "repeat", "tally")} == {
        "ts": "2026-10-02T00:00:00Z", "base": "https://x", "engine": "subtree", "date": DATE, "truth": "gs://t/", "cold": False, "repeat": 2,
        "tally": {"exact": 3, "flagged": 1, "FAIL": 1, "error": 1, "exact*": 1, "refused": 1},
    }
    assert rec["results"][2]["reason"] == "search budget"


def test_bounded_match_list_only_verifies_totals(truth):
    scores = run(SubtreeEngine(fetch, DATE), [Case("capped", "cap", "simple", ("",), "")], truth)
    s = scores[0]
    assert [s.verdict, s.roots, s.roots_want, s.missing, s.extra, s.b_err, s.o_err, s.roots_truncated] == [
        "totals", 3, 3, None, 0, 0.0, 0.0, True,
    ]


@pytest.mark.parametrize("roots,count,b,listed,verdict,extra", [
    (["bk/a"], 3, 60, None, "totals", None),
    (["bk/x"], 3, 60, ["bk/a", "bk/b", "bk/c"], "FAIL", 1),
    (["bk/a"], 2, 60, ["bk/a", "bk/b", "bk/c"], "FAIL", 0),
    (["bk/a"], 3, 55, ["bk/a", "bk/b", "bk/c"], "FAIL", 0),
])
def test_bounded_match_list_validation(roots, count, b, listed, verdict, extra):
    answer = Answer(200, 10, 8, 100, roots, b, 3, n_roots=count, roots_truncated=True)
    result = score(Case("capped", "cap", "simple", ("",), ""), "", [answer], TRUTH["capped"][0], listed)
    assert (result.verdict, result.missing, result.extra) == (verdict, None, extra)


def test_truth_missing_query(truth):
    with pytest.raises(KeyError):
        run(SubtreeEngine(fetch, DATE), [Case("nope", "abc", "simple", ("",), "")], truth)
