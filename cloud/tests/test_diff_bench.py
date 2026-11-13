"""`dt-cloud diff-bench`: the browser's request mix, the sign test, the paired summary, and the command end to end over a
fake pair of hosts (cold = the first ask of a key on a host, warm = the second)."""
from __future__ import annotations

import json
from math import isnan

from click.testing import CliRunner

from dt_cloud import diff_bench
from dt_cloud.diff_bench import CV, Req, Sample, answer_digest, browser_url, random_buckets, requests_for, sign_test, st_total, summarize


def test_requests_are_the_browsers_mix():
    assert requests_for([{"from": "f", "to": "t", "path": "m"}], ["d:", "d:m"], 1152, 307) == [
        Req("diff f→t /m", "diff:d1", "/api/diff", {"cv": CV, "from": "f", "to": "t", "path": "m", "w": 1152, "h": 307, "dm": "delta", "depth": 1}),
        Req("diff f→t /m", "diff:full", "/api/diff", {"cv": CV, "from": "f", "to": "t", "path": "m", "w": 1152, "h": 307, "dm": "delta"}),
        Req("subtree d /", "subtree:d1", "/api/subtree", {"cv": CV, "date": "d", "path": "", "w": 1152, "h": 307, "depth": 1}),
        Req("subtree d /", "subtree:full", "/api/subtree", {"cv": CV, "date": "d", "path": "", "w": 1152, "h": 307}),
        Req("subtree d /m", "subtree:d1", "/api/subtree", {"cv": CV, "date": "d", "path": "m", "w": 1152, "h": 307, "depth": 1}),
        Req("subtree d /m", "subtree:full", "/api/subtree", {"cv": CV, "date": "d", "path": "m", "w": 1152, "h": 307}),
    ]


def test_browser_url_encodes_as_the_page_does():
    """`encodeURIComponent`'s escapes, in the request's parameter order: `/` and `=` escaped, a space as `%20`, its
    unreserved marks (`-_.!~*'()`) as they are."""
    r = Req("subtree d /m", "subtree:d1", "/api/subtree", {"cv": CV, "date": "2026-09-15T1201", "path": "b k/a=1/it's (x)!~*-_.", "w": 1152, "h": 307, "depth": 1})
    assert browser_url(r) == f"/api/subtree?cv={CV}&date=2026-09-15T1201&path=b%20k%2Fa%3D1%2Fit's%20(x)!~*-_.&w=1152&h=307&depth=1"


def test_stable_reader_mode_is_explicit_in_requests_and_report_names():
    assert requests_for([{'from': 'f', 'to': 't', 'path': 'm'}], [], 1152, 307, mode='max') == [
        Req('diff f→t /m dm=max', 'diff:d1', '/api/diff', {'cv': CV, 'from': 'f', 'to': 't', 'path': 'm', 'w': 1152, 'h': 307, 'dm': 'max', 'depth': 1}),
        Req('diff f→t /m dm=max', 'diff:full', '/api/diff', {'cv': CV, 'from': 'f', 'to': 't', 'path': 'm', 'w': 1152, 'h': 307, 'dm': 'max'}),
    ]


def test_a_secondary_stores_requests_name_it():
    assert requests_for([{"from": "f", "to": "t", "path": "m"}], ["d:"], 640, 307, "meta") == [
        Req("diff f→t /m", "diff:d1", "/api/diff", {"cv": CV, "store": "meta", "from": "f", "to": "t", "path": "m", "w": 640, "h": 307, "dm": "delta", "depth": 1}),
        Req("diff f→t /m", "diff:full", "/api/diff", {"cv": CV, "store": "meta", "from": "f", "to": "t", "path": "m", "w": 640, "h": 307, "dm": "delta"}),
        Req("subtree d /", "subtree:d1", "/api/subtree", {"cv": CV, "store": "meta", "date": "d", "path": "", "w": 640, "h": 307, "depth": 1}),
        Req("subtree d /", "subtree:full", "/api/subtree", {"cv": CV, "store": "meta", "date": "d", "path": "", "w": 640, "h": 307}),
    ]


def test_a_diffs_summary_is_asked_only_on_request():
    """`summary=1` (a diff's totals alone) is an API request no page makes: off the browser's mix, on with `summary`."""
    assert requests_for([{"from": "f", "to": "t", "path": "m"}], [], 1152, 307, summary=True) == [
        Req("diff f→t /m", "diff:d1", "/api/diff", {"cv": CV, "from": "f", "to": "t", "path": "m", "w": 1152, "h": 307, "dm": "delta", "depth": 1}),
        Req("diff f→t /m", "diff:full", "/api/diff", {"cv": CV, "from": "f", "to": "t", "path": "m", "w": 1152, "h": 307, "dm": "delta"}),
        Req("diff f→t /m", "diff:summary", "/api/diff", {"cv": CV, "from": "f", "to": "t", "path": "m", "w": 1152, "h": 307, "summary": 1}),
    ]


def test_random_buckets_are_distinct_and_taller_than_wide():
    bs = random_buckets(50)
    assert len(set(bs)) == 50
    assert [(w, h) for w, h in bs if not (640 <= w <= 2048 < 4097 and w < h <= 4096 and w % 128 == h % 128 == 0)] == []


def test_sign_test():
    assert sign_test([]) == 1.0
    assert sign_test([0, 0]) == 1.0
    assert sign_test([1, -1]) == 1.0
    assert sign_test([1] * 10) == 2 / 1024
    assert sign_test([1] * 9 + [-1]) == 2 * 11 / 1024
    assert sign_test([-1] * 9 + [1, 0]) == 2 * 11 / 1024


def test_st_total():
    assert st_total("auth;dur=5, total;dur=231.5, gcache;dur=2") == 231.5
    assert st_total("") is None


def test_answer_digest_ignores_the_readers_own_fields():
    d = {"bytes_before": 1, "bytes_after": 2, "objects_before": 3, "objects_after": 4, "rows": [{"path": "m"}]}
    assert answer_digest("/api/diff", {**d, "tier": "bysize", "lookups": 0}) == answer_digest("/api/diff", {**d, "tier": "descent"})
    assert answer_digest("/api/diff", d) != answer_digest("/api/diff", {**d, "rows": []})
    assert answer_digest("/api/diff", {"total_a": 1, "total_b": 2, "objects_a": 3, "objects_b": 4, "rows": [{"p": "m"}]}) == answer_digest("/api/diff", d)
    v = {"nodes": 2, "tree": {"name": "Root", "bytes": 5, "children": [{"name": "m", "bytes": 5}]}}
    assert answer_digest("/api/subtree", {**v, "index": "g1"}) == answer_digest("/api/subtree", {**v, "index": "g2"})
    assert answer_digest("/api/subtree", v) == answer_digest("/api/subtree", {**v, "tree": {**v["tree"], "name": "Root (dev)"}})
    assert answer_digest("/api/subtree", v) != answer_digest("/api/subtree", {**v, "tree": {**v["tree"], "children": [{"name": "m", "bytes": 4}]}})
    # A host still serving the short names (`api_names`) digests as its descriptive answer.
    assert answer_digest("/api/subtree", {"nodes": 2, "tree": {"n": "Root", "b": 5, "c": [{"n": "m", "b": 5}]}}) == answer_digest("/api/subtree", v)


def sample(kind, w, host, phase, total, cache="miss", digest="x", wall=0.2):
    return Sample("diff f→t /", kind, w, 307, host, phase, 200, wall, total, cache, digest)


def test_summarize_pairs_cold_misses_only():
    s = [sample("diff:d1", 1152, "a", "cold", 100), sample("diff:d1", 1152, "b", "cold", 130),
         sample("diff:d1", 1280, "a", "cold", 120), sample("diff:d1", 1280, "b", "cold", 110),
         # An edge-answered cold sample: its bucket was used before, the pair is dropped.
         sample("diff:d1", 1408, "a", "cold", None, cache="hit"), sample("diff:d1", 1408, "b", "cold", 500),
         sample("diff:d1", 1152, "a", "warm", None, cache="hit", wall=0.1), sample("diff:d1", 1152, "b", "warm", None, cache="hit", wall=0.3),
         sample("diff:full", 1152, "a", "cold", 100, digest="x"), sample("diff:full", 1152, "b", "cold", 100, digest="y")]
    kinds, disagree, warmed = summarize(s, "a", "b")
    assert [(k.kind, k.n, k.a_med, k.b_med, k.d_mean, k.d_med, k.b_slower, k.p) for k in kinds] == [
        ("diff:d1", 2, 110, 120, 10, 10, 1, 1.0),
        ("diff:full", 1, 100, 100, 0, 0, 0, 1.0),
    ]
    # No warm pair for `diff:full`: its warm medians are NaN.
    assert [(k.warm_a, k.warm_b) for k in kinds[:1]] == [(0.1, 0.3)] and all(isnan(x) for x in (kinds[1].warm_a, kinds[1].warm_b))
    assert disagree == [("diff f→t /", "diff:full", (1152, 307))]
    assert warmed == 1


class Hosts:
    """Two fake hosts: a key's first ask is a compute (`miss`, `total` A 100 / B `b_ms`), every later one an edge hit."""

    def __init__(self, b_ms, b_rows=None):
        self.b_ms, self.b_rows, self.seen, self.calls = b_ms, b_rows, set(), []

    def __call__(self, host, q, token, retries=6, route="/api/diff"):
        key = (host, route, json.dumps(q, sort_keys=True))
        cold = key not in self.seen
        self.seen.add(key)
        self.calls.append((host, route, q.get("w"), q.get("depth"), q.get("dm"), q.get("summary"), "cold" if cold else "warm"))
        rows = self.b_rows if host == "b" and self.b_rows is not None else [{"p": "m"}]
        body = ({"total_a": 1, "total_b": 2, "objects_a": 1, "objects_b": 1, "rows": rows} if route == "/api/diff"
                else {"nodes": rows, "tree": {}})
        ms = 100 if host == "a" else self.b_ms
        return 200, body, 0.25, f"total;dur={ms}" if cold else "", "miss" if cold else "hit"


def run(monkeypatch, hosts, *args):
    monkeypatch.setattr(diff_bench, "fetch", hosts)
    monkeypatch.setenv("SITE_TOKEN", "t")
    return CliRunner().invoke(diff_bench.diff_bench_cmd, ["-a", "a", "-b", "b", *args])


CASES = json.dumps([{"from": "f", "to": "t", "path": ""}, {"from": "g", "to": "t", "path": "m"}])


def test_command_equal_hosts_pass(monkeypatch):
    h = Hosts(100)
    r = run(monkeypatch, h, "-w", "1152,1280", "-v", "d:", CASES)
    assert r.exit_code == 0, r.output
    assert r.stdout.split("\n") == [
        "A a  B b  canvases 1152×307 1280×307",
        "request         n cold ms A      B  Δ mean  Δ med  B slower      p warm s A      B",
        "diff:d1         4       100    100    +0.0   +0.0       0/4  1.000    0.250  0.250",
        "diff:full       4       100    100    +0.0   +0.0       0/4  1.000    0.250  0.250",
        "subtree:d1      2       100    100    +0.0   +0.0       0/2  1.000    0.250  0.250",
        "subtree:full    2       100    100    +0.0   +0.0       0/2  1.000    0.250  0.250",
        "0 errors, 0 disagreeing answers, 0 cold samples the edge answered (dropped)",
        "",
    ]
    # Each request cold on both hosts (order alternating), then warm on both in the same order.
    assert h.calls[:8] == [
        ("a", "/api/diff", 1152, 1, "delta", None, "cold"), ("b", "/api/diff", 1152, 1, "delta", None, "cold"),
        ("a", "/api/diff", 1152, 1, "delta", None, "warm"), ("b", "/api/diff", 1152, 1, "delta", None, "warm"),
        ("b", "/api/diff", 1152, None, "delta", None, "cold"), ("a", "/api/diff", 1152, None, "delta", None, "cold"),
        ("b", "/api/diff", 1152, None, "delta", None, "warm"), ("a", "/api/diff", 1152, None, "delta", None, "warm"),
    ]
    assert len(h.calls) == 2 * 2 * 2 * (2 * 2 + 2)


def test_command_times_the_summary_with_s(monkeypatch):
    h = Hosts(100)
    r = run(monkeypatch, h, "-w", "1152", "-s", CASES)
    assert r.exit_code == 0, r.output
    assert r.stdout.split("\n") == [
        "A a  B b  canvases 1152×307",
        "request         n cold ms A      B  Δ mean  Δ med  B slower      p warm s A      B",
        "diff:d1         2       100    100    +0.0   +0.0       0/2  1.000    0.250  0.250",
        "diff:full       2       100    100    +0.0   +0.0       0/2  1.000    0.250  0.250",
        "diff:summary    2       100    100    +0.0   +0.0       0/2  1.000    0.250  0.250",
        "0 errors, 0 disagreeing answers, 0 cold samples the edge answered (dropped)",
        "",
    ]
    # Each case's third request is its summary (no `dm`, no `depth`), cold on both hosts then warm on both; the
    # hosts' order alternates by request.
    assert [c for c in h.calls if c[5] == 1] == [
        (host, "/api/diff", 1152, None, None, 1, phase)
        for hosts in (("a", "b"), ("b", "a")) for phase in ("cold", "warm") for host in hosts
    ]


def test_command_selects_the_stable_reader(monkeypatch):
    h = Hosts(100)
    result = run(monkeypatch, h, '-m', 'max', '-w', '1152', CASES)
    assert result.exit_code == 0, result.output
    assert [call[4] for call in h.calls] == ['max'] * 16


def test_command_slower_b_exits_2(monkeypatch):
    r = run(monkeypatch, Hosts(130), "-w", "1152,1280,1408", "-p", "0.2", "-s", CASES)
    assert r.exit_code == 2, r.output
    assert r.stdout.split("\n")[2:6] == [
        "diff:d1         6       100    130   +30.0  +30.0       6/6  0.031    0.250  0.250",
        "diff:full       6       100    130   +30.0  +30.0       6/6  0.031    0.250  0.250",
        "diff:summary    6       100    130   +30.0  +30.0       6/6  0.031    0.250  0.250",
        "0 errors, 0 disagreeing answers, 0 cold samples the edge answered (dropped)",
    ]
    assert r.stdout.split("\n")[6] == "slower on B (p < 0.2): diff:d1, diff:full, diff:summary"


def test_command_disagreeing_answers_exit_1(monkeypatch):
    r = run(monkeypatch, Hosts(100, b_rows=[]), "-w", "1152", "-v", "d:m")
    assert r.exit_code == 1, r.output
    assert r.stdout.split("\n")[-4:] == [
        "0 errors, 2 disagreeing answers, 0 cold samples the edge answered (dropped)",
        "  differ: subtree:d1 subtree d /m 1152×307",
        "  differ: subtree:full subtree d /m 1152×307",
        "",
    ]


def test_command_refuses_one_host(monkeypatch):
    monkeypatch.setenv("SITE_TOKEN", "t")
    r = CliRunner().invoke(diff_bench.diff_bench_cmd, ["-a", "x", "-b", "x", "[]"])
    assert (r.exit_code, r.output) == (1, "A and B share an edge cache: B's cold reads would be A's warm ones\n")


def test_command_does_not_hide_first_request_failures_with_retries(monkeypatch, tmp_path):
    import csv

    attempts = []

    def fetch(host, q, token, retries=6, route='/api/diff'):
        attempts.append(retries)
        return (503, 'not ready', 0.01, '', '-') if retries == 1 else (200, {}, 0.01, '', '-')

    report = tmp_path / 'samples.tsv'
    result = run(monkeypatch, fetch, '-w', '1152', '-v', 'd:', '-o', str(report))
    assert result.exit_code == 1, result.output
    assert attempts == [1] * 8
    with report.open() as f:
        assert [int(row['status']) for row in csv.DictReader(f, delimiter='\t')] == [503] * 8
    assert result.stdout.splitlines()[-1] == '8 errors, 0 disagreeing answers, 4 cold samples the edge answered (dropped)'
