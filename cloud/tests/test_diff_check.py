"""`dt-cloud diff-check`'s per-case findings (`diff_check.check`): totals, rows B draws against A's, rows B missed,
sums at every expanded row, and the tier."""
from __future__ import annotations

from dt_cloud.diff_check import bad_sums, check, timing


def row(p, d, s, a, b, oa, ob, x=False, k="dir"):
    return {"p": p, "d": d, "k": k, "s": s, "a": a, "b": b, "oa": oa, "ob": ob, **({"x": True} if x else {})}


def diff(rows, tier="descent", threshold=10, ta=100, tb=150, oa=10, ob=12):
    return {"rows": rows, "total_a": ta, "total_b": tb, "objects_a": oa, "objects_b": ob, "tier": tier, "threshold": threshold}


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


def test_a_row_b_cut_into_its_parents_other_is_excused_when_b_is_truncated():
    b = diff([row("m", 1, "changed", 60, 100, 6, 8, x=True), row("m/(other)", 2, "changed", 60, 100, 6, 8), row("n", 1, "changed", 40, 50, 4, 4)])
    assert check(A, {**b, "truncated": True}, "descent")["missed"] == []
    assert check(A, {**b, "truncated": False}, "descent")["missed"] == ["m/x"]
