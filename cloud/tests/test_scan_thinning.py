"""Scan selection is independent of store I/O and never estimates merged deltas."""
from __future__ import annotations

from itertools import product

import pytest

from dt_cloud.scan_thinning import (
    AgeBand, ChangeMetric, GeometricPlan, RemovalCandidate, RemovalStep, ThinningPlan,
    plan_geometric, plan_low_change, plan_manual, removal_candidates,
)


SCANS = tuple(f"2026-10-{day:02}" for day in range(1, 7))


def test_manual_selection_is_chronological_and_remeasures_surviving_neighbors():
    a, b, c, d, e = SCANS[:5]
    states = {a: set(), b: {"x"}, c: set(), d: {"y"}, e: {"y", "z"}}
    plan = plan_manual(SCANS[:5], lambda before, after: len(states[before] ^ states[after]), remove=[c, b])
    assert plan == ThinningPlan(
        "manual", (a, b, c, d, e), (a, d, e), (), (
            RemovalStep(b, a, c, ChangeMetric(1), ChangeMetric(1), ChangeMetric(0), 1),
            RemovalStep(c, a, d, ChangeMetric(0), ChangeMetric(1), ChangeMetric(1), 1),
        ),
    )


def test_manual_empty_selection_does_not_call_oracle():
    def changes(before: str, after: str) -> int:
        raise AssertionError(f"unexpected comparison: {before}, {after}")

    assert plan_manual(SCANS, changes, remove=[]) == ThinningPlan("manual", SCANS, SCANS, (), ())


@pytest.mark.parametrize("remove,pins,message", [
    ([SCANS[0]], (), f"cannot remove protected scans: {[SCANS[0]]!r}"),
    ([SCANS[-1]], (), f"cannot remove protected scans: {[SCANS[-1]]!r}"),
    ([SCANS[2]], (SCANS[2],), f"cannot remove protected scans: {[SCANS[2]]!r}"),
    (["2026-11-01"], (), "removed scans are absent from the source: ['2026-11-01']"),
])
def test_manual_invalid_selection_fails_before_measuring(remove, pins, message):
    def changes(before: str, after: str) -> int:
        raise AssertionError(f"unexpected comparison: {before}, {after}")

    with pytest.raises(ValueError) as error:
        plan_manual(SCANS, changes, remove=remove, pinned=pins)
    assert str(error.value) == message


def test_candidates_include_exact_local_metrics_and_protection_reasons():
    a, b, c = SCANS[:3]
    metrics = {(a, b): ChangeMetric(3, 5), (b, c): ChangeMetric(7, 0)}
    calls = []

    def changes(before: str, after: str) -> ChangeMetric:
        calls.append((before, after))
        return metrics[before, after]

    assert removal_candidates(SCANS[:3], changes, pinned=[b]) == (
        RemovalCandidate(a, None, b, None, ChangeMetric(3, 5), None, True, ("first",)),
        RemovalCandidate(b, a, c, ChangeMetric(3, 5), ChangeMetric(7, 0), 7, True, ("pinned",)),
        RemovalCandidate(c, b, None, ChangeMetric(7, 0), None, None, True, ("last",)),
    )
    assert calls == [(a, b), (b, c)]


def test_low_change_records_exact_metrics_and_protects_the_boundary():
    a, b, c, d, e = SCANS[:5]
    metrics = {
        (a, b): ChangeMetric(1, 100),
        (b, c): ChangeMetric(2, 5),
        (c, d): ChangeMetric(100, 1),
        (d, e): ChangeMetric(0, 0),
        (a, c): ChangeMetric(3, 105),
    }
    calls = []

    def changes(before: str, after: str) -> ChangeMetric:
        calls.append((before, after))
        return metrics[before, after]

    plan = plan_low_change(reversed(SCANS[:5]), changes, drop_count=1)
    assert plan == ThinningPlan(
        policy="low-change",
        scans=(a, b, c, d, e),
        retained=(a, c, d, e),
        pinned=(),
        steps=(RemovalStep(b, a, c, ChangeMetric(1, 100), ChangeMetric(2, 5), ChangeMetric(3, 105), 2),),
    )
    assert (plan.removed, plan.protected) == ((b,), (a, e))
    assert calls == [(a, b), (b, c), (c, d), (d, e), (a, c)]


def test_reversal_recomputes_neighbors_instead_of_adding_old_deltas():
    a, b, c, d, e = SCANS[:5]
    states = {a: set(), b: {"x"}, c: set(), d: {"y"}, e: {"y", "z"}}
    calls = []

    def changes(before: str, after: str) -> int:
        calls.append((before, after))
        return len(states[before] ^ states[after])

    plan = plan_low_change(SCANS[:5], changes, target_count=3)
    assert plan == ThinningPlan(
        policy="low-change",
        scans=(a, b, c, d, e),
        retained=(a, d, e),
        pinned=(),
        steps=(
            RemovalStep(b, a, c, ChangeMetric(1), ChangeMetric(1), ChangeMetric(0), 1),
            RemovalStep(c, a, d, ChangeMetric(0), ChangeMetric(1), ChangeMetric(1), 1),
        ),
    )
    assert calls == [(a, b), (b, c), (c, d), (d, e), (a, c), (a, d)]


def test_pins_and_endpoints_survive_chronological_ties():
    a, b, c, d, e, f = SCANS
    plan = plan_low_change(SCANS, lambda before, after: 0, pinned=[c], drop_count=3)
    assert plan == ThinningPlan(
        policy="low-change",
        scans=SCANS,
        retained=(a, c, f),
        pinned=(c,),
        steps=(
            RemovalStep(b, a, c, ChangeMetric(0), ChangeMetric(0), ChangeMetric(0), 0),
            RemovalStep(d, c, e, ChangeMetric(0), ChangeMetric(0), ChangeMetric(0), 0),
            RemovalStep(e, c, f, ChangeMetric(0), ChangeMetric(0), ChangeMetric(0), 0),
        ),
    )
    assert plan.protected == (a, c, f)


@pytest.mark.parametrize("scans,pins,target", [
    ((), (), 0),
    (SCANS[:1], SCANS[:1], 1),
    (SCANS[:2], (), 2),
    (SCANS, SCANS[1:3], 6),
])
def test_no_removal_makes_no_oracle_calls(scans, pins, target):
    def changes(before: str, after: str) -> int:
        raise AssertionError(f"unexpected comparison: {before}, {after}")

    assert plan_low_change(scans, changes, target_count=target, pinned=pins) == ThinningPlan(
        "low-change", scans, scans, pins, (),
    )


@pytest.mark.parametrize("kwargs,message", [
    ({}, "provide exactly one of drop_count or target_count"),
    ({"drop_count": 0, "target_count": 3}, "provide exactly one of drop_count or target_count"),
    ({"drop_count": -1}, "drop_count must be a nonnegative integer, got -1"),
    ({"drop_count": True}, "drop_count must be a nonnegative integer, got True"),
    ({"drop_count": 1.5}, "drop_count must be a nonnegative integer, got 1.5"),
    ({"target_count": -1}, "target_count must be a nonnegative integer, got -1"),
    ({"target_count": 4}, "target_count 4 exceeds the source's 3 scans"),
    ({"target_count": 1}, "cannot remove 2 scans: 1 removable, 2 protected"),
    ({"drop_count": 2}, "cannot remove 2 scans: 1 removable, 2 protected"),
    ({"drop_count": 1, "pinned": SCANS[1:2]}, "cannot remove 1 scans: 0 removable, 3 protected"),
    ({"drop_count": 0, "pinned": SCANS[3:4]}, f"pinned scans are absent from the source: {[SCANS[3]]!r}"),
])
def test_invalid_request(kwargs, message):
    with pytest.raises(ValueError) as error:
        plan_low_change(SCANS[:3], lambda before, after: 0, **kwargs)
    assert str(error.value) == message


@pytest.mark.parametrize("scans,message", [
    ((SCANS[0], SCANS[0]), f"scans {SCANS[0]} and {SCANS[0]}: ids out of time order (stamps 1790812800, 1790812800)"),
    (("not-a-scan",), "not a scan id: 'not-a-scan' (want YYYY-MM-DD or YYYY-MM-DDTHHMM)"),
])
def test_invalid_scan_ids(scans, message):
    with pytest.raises(ValueError) as error:
        plan_low_change(scans, lambda before, after: 0, drop_count=0)
    assert str(error.value) == message


@pytest.mark.parametrize("count,byte_count,message", [
    (-1, None, "change count must be a nonnegative integer, got -1"),
    (True, None, "change count must be a nonnegative integer, got True"),
    (1.0, None, "change count must be a nonnegative integer, got 1.0"),
    (0, -1, "changed bytes must be a nonnegative integer, got -1"),
])
def test_invalid_metrics(count, byte_count, message):
    with pytest.raises(ValueError) as error:
        ChangeMetric(count, byte_count)
    assert str(error.value) == message


def test_invalid_oracle_count_is_not_silently_accepted():
    with pytest.raises(ValueError) as error:
        plan_low_change(SCANS[:3], lambda before, after: -1, drop_count=1)
    assert str(error.value) == "change count must be a nonnegative integer, got -1"


def test_exhaustive_small_histories_preserve_invariants_and_choose_global_minimum():
    """Many reversal/overlap patterns, checked against every surviving triple."""
    scans = SCANS[:5]
    for values in product(range(3), repeat=len(scans)):
        states = dict(zip(scans, values))

        def changes(before: str, after: str) -> int:
            return int(states[before] != states[after])

        for pins in ((), scans[2:3]):
            for drop in range(len(scans) - 2 - len(pins) + 1):
                plan = plan_low_change(scans, changes, pinned=pins, drop_count=drop)
                remaining = list(scans)
                for step in plan.steps:
                    candidates = [
                        (max(changes(a, b), changes(b, c)), b)
                        for a, b, c in zip(remaining, remaining[1:], remaining[2:])
                        if b not in pins
                    ]
                    assert (step.score, step.scan) == min(candidates)
                    i = remaining.index(step.scan)
                    assert step == RemovalStep(
                        step.scan,
                        remaining[i - 1],
                        remaining[i + 1],
                        ChangeMetric(changes(remaining[i - 1], step.scan)),
                        ChangeMetric(changes(step.scan, remaining[i + 1])),
                        ChangeMetric(changes(remaining[i - 1], remaining[i + 1])),
                        step.score,
                    )
                    remaining.remove(step.scan)
                assert plan.retained == tuple(remaining)
                assert len(plan.steps) == drop
                assert (plan.retained[0], plan.retained[-1]) == (scans[0], scans[-1])
                assert tuple(scan for scan in plan.retained if scan in pins) == pins


def test_geometric_age_bands_have_explicit_boundaries_and_extra_pins():
    scans = tuple(f"2026-10-{day:02}" for day in range(1, 18))
    day = 86400
    plan = plan_geometric(reversed(scans), recent_seconds=2 * day, pinned=[scans[4]])
    assert plan == GeometricPlan(
        scans=scans,
        retained=(scans[0], scans[4], scans[8], scans[12], scans[14], scans[15], scans[16]),
        pinned=(scans[4],),
        recent_seconds=2 * day,
        growth=2,
        samples_per_band=1,
        bands=(
            AgeBand(2 * day, 4 * day, scans[13:15], (scans[14],)),
            AgeBand(4 * day, 8 * day, scans[9:13], (scans[12],)),
            AgeBand(8 * day, 16 * day, scans[1:9], (scans[8],)),
            AgeBand(16 * day, 32 * day, scans[:1], scans[:1]),
        ),
    )
    assert plan.removed == tuple(scans[i] for i in (1, 2, 3, 5, 6, 7, 9, 10, 11, 13))
    assert plan.protected == (scans[0], scans[4], scans[-1])


def test_geometric_multiple_samples_split_bands_by_time():
    scans = tuple(f"2026-10-{day:02}" for day in range(1, 18))
    plan = plan_geometric(scans, recent_seconds=2 * 86400, samples_per_band=2)
    assert plan.retained == tuple(scans[i] for i in (0, 4, 8, 10, 12, 13, 14, 15, 16))
    assert [band.representatives for band in plan.bands] == [
        (scans[13], scans[14]),
        (scans[10], scans[12]),
        (scans[4], scans[8]),
        (scans[0],),
    ]


def test_geometric_irregular_cadence_does_not_allocate_slots_by_rank():
    scans = (
        "2026-10-01",
        "2026-10-01T1200",
        "2026-10-01T1300",
        "2026-10-01T1400",
        "2026-10-02",
        "2026-10-02T2300",
        "2026-10-03",
    )
    plan = plan_geometric(scans, recent_seconds=86400, samples_per_band=2)
    assert plan.retained == (scans[0], scans[1], scans[4], scans[5], scans[6])
    assert plan.bands == (
        AgeBand(86400, 2 * 86400, scans[1:5], (scans[1], scans[4])),
        AgeBand(2 * 86400, 4 * 86400, scans[:1], scans[:1]),
    )


def test_geometric_arbitrary_growth_and_empty_bands():
    scans = ("2026-10-01", "2026-10-02", "2026-10-30", "2026-10-31")
    plan = plan_geometric(scans, recent_seconds=86400, growth=3)
    assert plan.retained == scans
    assert plan.bands == (
        AgeBand(86400, 3 * 86400, scans[2:3], scans[2:3]),
        AgeBand(27 * 86400, 81 * 86400, scans[:2], scans[1:2]),
    )


@pytest.mark.parametrize("scans,pins", [((), ()), (SCANS[:1], ()), (SCANS, SCANS[1:3])])
def test_geometric_all_recent_and_empty_sources(scans, pins):
    plan = plan_geometric(scans, recent_seconds=10 * 86400, pinned=pins)
    assert plan == GeometricPlan(scans, scans, pins, 10 * 86400, 2, 1, ())
    assert plan.removed == ()


@pytest.mark.parametrize("kwargs,message", [
    ({"recent_seconds": 0}, "recent_seconds must be positive"),
    ({"recent_seconds": -1}, "recent_seconds must be a nonnegative integer, got -1"),
    ({"recent_seconds": True}, "recent_seconds must be a nonnegative integer, got True"),
    ({"recent_seconds": 1, "samples_per_band": 0}, "samples_per_band must be positive"),
    ({"recent_seconds": 1, "samples_per_band": -2}, "samples_per_band must be a nonnegative integer, got -2"),
    ({"recent_seconds": 1, "growth": 1}, "growth must be finite and greater than one, got 1"),
    ({"recent_seconds": 1, "growth": True}, "growth must be finite and greater than one, got True"),
    ({"recent_seconds": 1, "growth": float("inf")}, "growth must be finite and greater than one, got inf"),
    ({"recent_seconds": 1, "growth": float("nan")}, "growth must be finite and greater than one, got nan"),
])
def test_geometric_invalid_parameters(kwargs, message):
    with pytest.raises(ValueError) as error:
        plan_geometric(SCANS, **kwargs)
    assert str(error.value) == message


@pytest.mark.parametrize("samples", [1, 2, 3])
@pytest.mark.parametrize("growth", [1.01, 1.5, 2, 3])
def test_geometric_representatives_are_bounded_and_all_removals_are_accounted_for(samples, growth):
    scans = tuple(f"2026-10-{day:02}" for day in range(1, 32))
    plan = plan_geometric(scans, recent_seconds=2 * 86400, growth=growth, samples_per_band=samples, pinned=scans[3:5])
    assert sorted((*plan.retained, *plan.removed)) == list(scans)
    assert len(set((*plan.retained, *plan.removed))) == len(scans)
    assert tuple(scan for scan in plan.retained if scan in plan.protected) == plan.protected
    assert (plan.retained[0], plan.retained[-1], plan.retained[-2]) == (scans[0], scans[-1], scans[-2])
    for band in plan.bands:
        assert len(band.representatives) <= samples
        assert tuple(scan for scan in plan.retained if scan in band.representatives) == band.representatives
