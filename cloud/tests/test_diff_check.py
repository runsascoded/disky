"""`dt-cloud diff-check`'s per-case findings (`diff_check.check`): totals, rows B draws against A's, rows B missed,
sums at every expanded row, and the tier."""
from __future__ import annotations

from dt_cloud.diff_check import bad_sums, check, timing


def row(p, d, s, a, b, oa, ob, x=False, k="dir"):
    return {"path": p, "depth": d, "kind": k, "status": s, "bytes_before": a, "bytes_after": b, "objects_before": oa, "objects_after": ob,
            **({"expanded": True} if x else {})}


def diff(rows, tier="descent", threshold=10, ta=100, tb=150, oa=10, ob=12):
    return {"rows": rows, "bytes_before": ta, "bytes_after": tb, "objects_before": oa, "objects_after": ob, "tier": tier, "threshold": threshold}


def short(d):
    """`d` as a site not yet redeployed serves it (the short names, `api_names`)."""
    keys = {"path": "p", "depth": "d", "kind": "k", "status": "s", "bytes_before": "a", "bytes_after": "b", "objects_before": "oa",
            "objects_after": "ob", "expanded": "x"}
    totals = {"bytes_before": "total_a", "bytes_after": "total_b", "objects_before": "objects_a", "objects_after": "objects_b"}
    return {**{totals.get(k, k): v for k, v in d.items() if k != "rows"}, "rows": [{keys[k]: v for k, v in r.items()} for r in d["rows"]]}


A = diff([row("m", 1, "changed", 60, 100, 6, 8, x=True), row("m/x", 2, "added", 0, 40, 0, 2), row("n", 1, "changed", 40, 50, 4, 4),
          row("m/(other)", 2, "unchanged", 60, 60, 6, 6)], tier="bysize")


def test_equal_readers_pass():
    b = diff([row("m", 1, "changed", 60, 100, 6, 8, x=True), row("m/x", 2, "added", 0, 40, 0, 2), row("n", 1, "changed", 40, 50, 4, 4)])
    assert check(A, b, "descent") == {"totals": True, "differ": [], "missed": [], "only_b": 0, "sums_a": [], "sums_b": [], "tier": "descent", "tier_ok": True, "ok": True}


def test_a_differing_row_a_missed_row_a_broken_sum_and_a_fallback_fail():
    b = diff([row("m", 1, "changed", 60, 100, 6, 8, x=True), row("n", 1, "changed", 40, 51, 4, 4)], tier="bysize")
    assert check(A, b, "descent") == {"totals": True, "differ": ["n"], "missed": ["m/x"], "only_b": 0, "sums_a": [], "sums_b": ["/"],
                                      "tier": "bysize", "tier_ok": False, "ok": False}


def test_bad_sums_names_the_rows_whose_children_dont_sum():
    assert bad_sums(diff([row("a", 1, "added", 0, 50, 0, 2, x=True), row("a/b", 2, "added", 0, 30, 0, 2)])) == ["a"]


def test_timing_keeps_the_totals_and_levels():
    assert timing("auth;dur=5, L1;dur=120;desc=\"3 parents\", L2;dur=40, total;dur=300, gcache;dur=2") == "L1=120 L2=40 total=300"


def test_timing_names_the_handoff():
    st = 'L1;dur=20, plan;dur=21;desc="1 to expand (max 2)", descent-rows;dur=6120;desc="6120 rows read", handoff;dur=310;desc="work: 6120 rows read (max 5000)", walk;dur=700, total;dur=1050'
    assert timing(st) == "L1=20 plan=21 descent-rows=6120 handoff[work]=310 walk=700 total=1050"


def test_a_row_b_cut_into_its_parents_other_is_excused_when_b_is_truncated():
    b = diff([row("m", 1, "changed", 60, 100, 6, 8, x=True), row("m/(other)", 2, "changed", 60, 100, 6, 8), row("n", 1, "changed", 40, 50, 4, 4)])
    assert check(A, {**b, "truncated": True}, "descent")["missed"] == []
    assert check(A, {**b, "truncated": False}, "descent")["missed"] == ["m/x"]


def test_a_short_named_body_reads_as_its_descriptive_one():
    b = diff([row("m", 1, "changed", 60, 100, 6, 8, x=True), row("n", 1, "changed", 40, 51, 4, 4)], tier="bysize")
    assert check(short(A), short(b), "descent") == check(A, b, "descent")
