"""`dt_cloud.bench.truth`: match roots, exclusions and net totals per view
(the Worker's filter semantics), by names-first and by scan, on the site's
v2-search fixture store (`site/functions/_lib/fixtures/v2-search`)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dt_cloud.bench import truth as bt
from dt_cloud.bench.query import compile_query, parse, parse_simple
from dt_cloud.bench.queryset import Case, parse_set
from dt_cloud.bench.truth import Cand, view_truth

FIX = Path(__file__).resolve().parents[2] / "site/functions/_lib/fixtures/v2-search"
PATH_F = str(FIX / "path-index.parquet")
NAMES_F = str(FIX / "path-index.names.parquet")


def vt(cands, view, q, tot=(0, 0), qs="simple"):
    v = view_truth(cands, view, compile_query(parse(q, qs)), tot)
    return v.root_hit, v.roots, v.excluded


C = [
    Cand("b/run", 100, 10, False, False),
    Cand("b/run/ckpt", 60, 6, True, False),
    Cand("b/run/ckpt/tmp", 20, 2, True, True),
    Cand("b/run/ckpt/x/tmp-2", 5, 1, True, True),
    Cand("b/run/ckpt/x/tmp-2/tmp", 1, 1, True, True),
    Cand("b/ckpt.pt", 7, 1, True, False),
    Cand("b/tmp/ckpt", 9, 1, True, True),
]


def test_view_truth_roots_and_exclusions():
    # Roots: the outermost pos ∧ ¬neg candidates; excluded: the outermost neg
    # paths under them; each root net of what's excluded under it.
    assert vt(C, "", "ckpt -tmp") == (
        False,
        [("b/ckpt.pt", 7, 1), ("b/run/ckpt", 35, 3)],
        [("b/run/ckpt/tmp", 20, 2), ("b/run/ckpt/x/tmp-2", 5, 1)],
    )
    assert vt(C, "b/run", "ckpt -tmp") == (False, [("b/run/ckpt", 35, 3)], [("b/run/ckpt/tmp", 20, 2), ("b/run/ckpt/x/tmp-2", 5, 1)])
    # The view root matches: it is the single root, net of exclusions under it.
    assert vt(C, "b/run/ckpt", "ckpt -tmp", (60, 6)) == (True, [("b/run/ckpt", 35, 3)], [("b/run/ckpt/tmp", 20, 2), ("b/run/ckpt/x/tmp-2", 5, 1)])
    assert vt([replace(c, neg=False) for c in C], "b/run/ckpt", "ckpt", (60, 6)) == (True, [("b/run/ckpt", 60, 6)], [])
    # Only negatives at the store root: everything, less the outermost excluded.
    assert vt(C, "", "-tmp", (500, 50)) == (True, [("", 466, 46)], [("b/run/ckpt/tmp", 20, 2), ("b/run/ckpt/x/tmp-2", 5, 1), ("b/tmp/ckpt", 9, 1)])
    # The view root is excluded itself: nothing.
    assert vt(C, "b/tmp", "-tmp", (9, 1)) == (False, [], [])


def test_view_truth_non_monotone_regex():
    # A regex can hold on a path and not on its descendants (or ancestors):
    # roots are the outermost matches below the view root, ancestors above it
    # don't count.
    cands = [Cand("b/x.json", 3, 1, True, False), Cand("b/x.json/y.json", 2, 1, True, False), Cand("b/d/z.json", 1, 1, True, False)]
    assert vt(cands, "", "\\.json$", qs="regex") == (False, [("b/d/z.json", 1, 1), ("b/x.json", 3, 1)], [])
    assert vt(cands, "b/x.json", "\\.json$", (3, 1), qs="regex") == (True, [("b/x.json", 3, 1)], [])
    assert vt(cands, "b/d", "\\.json$", (1, 1), qs="regex") == (False, [("b/d/z.json", 1, 1)], [])


def test_names_first_ok():
    assert [bt.names_first_ok(parse_simple(q)) for q in ("ckpt -tmp", "-tmp", "abc/", "xyz -abc/", "tmp/*", "abc|-b")] == [True, True, False, False, False, True]
    assert bt.names_first_ok(parse("x", "regex")) is False


QUERIES = {
    "views": ["", "bk/tmp", "bk/runs/grug"],
    "queries": [
        {"id": "ckpt", "q": "ckpt"},
        {"id": "ckpt-ttl", "q": "ckpt -ttl"},
        {"id": "tmp-ttl", "q": "tmp -ttl"},
        {"id": "not-ttl", "q": "-ttl"},
        {"id": "grug", "q": "grug"},
        {"id": "swarm-start", "q": "/swarm"},
        {"id": "swarm-end", "q": "swarm/"},
        {"id": "shards", "q": "model-*-of-00002"},
        {"id": "ckpts", "q": "Checkpoints|llama"},
        {"id": "re-st", "q": "\\.safetensors$", "qs": "regex", "views": [""]},
        {"id": "re-llama", "q": "llama$", "qs": "regex", "views": ["", "bk/models"]},
    ],
}


@pytest.fixture(scope="module")
def truths():
    cases = parse_set(QUERIES)
    con = bt.connect(threads=2, mem="1GB")
    nf = bt.compute(cases, PATH_F, NAMES_F, [c.id for c in cases], con)
    return {t.id: t for t in nf}


def roots(t, view):
    return next((v.roots, v.excluded) for v in t.views if v.view == view)


def test_compute_fixture(truths):
    T = 29339231
    assert {k: [(v.view, len(v.roots), v.bytes, v.objects, len(v.excluded)) for v in t.views] for k, t in truths.items()} == {
        "ckpt": [("", 3, 6000 + 9000 + 4194304, 3, 0), ("bk/tmp", 1, 4194304, 1, 0), ("bk/runs/grug", 1, 9000, 1, 0)],
        "ckpt-ttl": [("", 2, 6000 + 9000, 2, 0), ("bk/tmp", 0, 0, 0, 0), ("bk/runs/grug", 1, 9000, 1, 0)],
        "tmp-ttl": [("", 1, 3145728, 1, 2), ("bk/tmp", 1, 3145728, 1, 2), ("bk/runs/grug", 0, 0, 0, 0)],
        "not-ttl": [("", 1, T - 500 - 5242880 - 2097152 - 10 - 7000 - 5000, 6020 - 8, 6), ("bk/tmp", 1, 3145728, 1, 2), ("bk/runs/grug", 1, 9100, 2, 0)],
        "grug": [("", 1, 9100, 2, 0), ("bk/tmp", 0, 0, 0, 0), ("bk/runs/grug", 1, 9100, 2, 0)],
        "swarm-start": [("", 2, 9100, 2, 0), ("bk/tmp", 0, 0, 0, 0), ("bk/runs/grug", 2, 9100, 2, 0)],
        "swarm-end": [("", 1, 9000, 1, 0), ("bk/tmp", 0, 0, 0, 0), ("bk/runs/grug", 1, 9000, 1, 0)],
        "shards": [("", 2, 2 * 8388608, 2, 0), ("bk/tmp", 0, 0, 0, 0), ("bk/runs/grug", 0, 0, 0, 0)],
        "ckpts": [("", 2, 16778116 + 10, 4, 0), ("bk/tmp", 0, 0, 0, 0), ("bk/runs/grug", 0, 0, 0, 0)],
        "re-st": [("", 3, 2 * 8388608 + 10, 3, 0)],
        "re-llama": [("", 1, 16778116, 3, 0), ("bk/models", 1, 16778116, 3, 0)],
    }
    assert roots(truths["not-ttl"], "") == (
        [("", T - 500 - 5242880 - 2097152 - 10 - 7000 - 5000, 6012)],
        [("bk/fill/zz-TTL-b", 7000, 1), ("bk/fill/zz-ttl-a", 5000, 1), ("bk/iris/TTL-misc", 500, 2), ("bk/tmp/ttl=14d", 5242880, 2), ("bk/tmp/ttl=7d", 2097152, 1), ("zz/Checkpoints/ttl", 10, 1)],
    )
    assert roots(truths["ckpt"], "") == ([("bk/ckpt", 6000, 1), ("bk/runs/grug/swarm/ckpt-final.pt", 9000, 1), ("bk/tmp/ttl=14d/run-a/ckpt", 4194304, 1)], [])
    assert roots(truths["swarm-end"], "") == ([("bk/runs/grug/swarm/ckpt-final.pt", 9000, 1)], [])


def test_methods_agree(truths):
    # Names-first where the index can plan the query, scan otherwise; every
    # planned query is also scanned (`check`), and the two agree.
    assert {k: (t.stats["method"], t.check and t.check["identical"]) for k, t in truths.items()} == {
        "ckpt": ("names-first", True),
        "ckpt-ttl": ("names-first", True),
        "tmp-ttl": ("names-first", True),
        "not-ttl": ("names-first", True),
        "grug": ("names-first", True),
        "swarm-start": ("names-first", True),
        "swarm-end": ("scan", None),
        "shards": ("names-first", True),
        "ckpts": ("names-first", True),
        "re-st": ("scan", None),
        "re-llama": ("scan", None),
    }


def test_write_layout(truths, tmp_path):
    bt.write([truths["tmp-ttl"], truths["re-st"]], str(tmp_path), {"date": "2026-10-01"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["re-st.json", "summary.json", "tmp-ttl.json"]
    s = json.loads((tmp_path / "summary.json").read_text())
    assert [q["id"] for q in s["queries"]] == ["tmp-ttl", "re-st"]
    assert s["queries"][0]["views"][0] == {
        "view": "", "root_hit": False, "roots": 1, "md5": bt.md5_paths(["bk/tmp"]), "bytes": 3145728, "objects": 1,
        "excluded": 2, "excluded_md5": bt.md5_paths(["bk/tmp/ttl=14d", "bk/tmp/ttl=7d"]),
    }
    d = json.loads((tmp_path / "tmp-ttl.json").read_text())
    assert (d["views"][0]["list"], d["views"][0]["excluded_list"]) == ([["bk/tmp", 3145728, 1]], [["bk/tmp/ttl=14d", 5242880, 2], ["bk/tmp/ttl=7d", 2097152, 1]])
    assert bt.ViewTruth("", False, [("a", 1, 1), ("b", 1, 1)], []).to_json(list_max=1)["list"] is None
    # Appending: other queries and the first run's meta stay; same ids are replaced.
    bt.write([truths["grug"], truths["re-st"]], str(tmp_path), {"date": "2026-10-01", "s": 2}, append=True)
    s = json.loads((tmp_path / "summary.json").read_text())
    assert ([q["id"] for q in s["queries"]], s["date"], s["appended"]) == (["tmp-ttl", "grug", "re-st"], "2026-10-01", [{"date": "2026-10-01", "s": 2}])


def test_case_defaults():
    assert parse_set({"views": ["", "bk/"], "queries": [{"id": "a", "q": "abc", "why": " x "}, {"id": "b", "q": "x$", "qs": "regex", "views": ["bk"]}]}) == [
        Case("a", "abc", "simple", ("", "bk"), "x"),
        Case("b", "x$", "regex", ("bk",), ""),
    ]
    with pytest.raises(ValueError):
        parse_set({"queries": [{"id": "A b", "q": "x"}]})
