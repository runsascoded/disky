"""Pure, auditable selection of historical scans to remove.

The planner never reads a store or mutates a generation. Its change oracle compares
two complete scan states using one consistent definition of a changed item (for
example, object identities). Counts are the selection metric; byte counts are
optional diagnostics. This is a low-change heuristic, not Shannon entropy.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from heapq import heappop, heappush
from math import floor, isfinite, log

from .scan_id import check_order, check_scan_id, scan_epoch


def _nonnegative_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class ChangeMetric:
    """Exact differing-item count between snapshots; bytes are diagnostic only."""

    count: int
    bytes: int | None = None

    def __post_init__(self) -> None:
        _nonnegative_integer(self.count, "change count")
        if self.bytes is not None:
            _nonnegative_integer(self.bytes, "changed bytes")


ChangeOracle = Callable[[str, str], ChangeMetric | int]


@dataclass(frozen=True)
class RemovalStep:
    """One removal, with the surviving neighbors and exact pairwise measurements."""

    scan: str
    before: str
    after: str
    left: ChangeMetric
    right: ChangeMetric
    merged: ChangeMetric
    score: int


@dataclass(frozen=True)
class ThinningPlan:
    """Original/retained scans in time order, steps in greedy execution order."""

    policy: str
    scans: tuple[str, ...]
    retained: tuple[str, ...]
    pinned: tuple[str, ...]
    steps: tuple[RemovalStep, ...]

    @property
    def removed(self) -> tuple[str, ...]:
        return tuple(step.scan for step in self.steps)

    @property
    def protected(self) -> tuple[str, ...]:
        endpoints = (self.scans[0], self.scans[-1]) if self.scans else ()
        protected = set(self.pinned) | set(endpoints)
        return tuple(scan for scan in self.scans if scan in protected)


def _inputs(
    scans: Iterable[str],
    pinned: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...], set[str]]:
    ordered = tuple(check_order([check_scan_id(scan) for scan in scans]))
    pins = set(pinned)
    unknown = sorted(pins - set(ordered))
    if unknown:
        raise ValueError(f"pinned scans are absent from the source: {unknown!r}")
    protected = pins | ({ordered[0], ordered[-1]} if ordered else set())
    return ordered, tuple(scan for scan in ordered if scan in pins), protected


def _removal_count(
    count: int,
    protected_count: int,
    drop_count: int | None,
    target_count: int | None,
) -> int:
    if (drop_count is None) == (target_count is None):
        raise ValueError("provide exactly one of drop_count or target_count")
    if target_count is not None:
        target = _nonnegative_integer(target_count, "target_count")
        if target > count:
            raise ValueError(f"target_count {target} exceeds the source's {count} scans")
        drop_count = count - target
    drop = _nonnegative_integer(drop_count, "drop_count")
    removable = count - protected_count
    if drop > removable:
        raise ValueError(f"cannot remove {drop} scans: {removable} removable, {protected_count} protected")
    return drop


class _Changes:
    def __init__(self, oracle: ChangeOracle) -> None:
        self.oracle = oracle
        self.cache: dict[tuple[str, str], ChangeMetric] = {}

    def __call__(self, before: str, after: str) -> ChangeMetric:
        key = before, after
        if key not in self.cache:
            value = self.oracle(before, after)
            if not isinstance(value, ChangeMetric):
                value = ChangeMetric(value)
            self.cache[key] = value
        return self.cache[key]


@dataclass(frozen=True)
class RemovalCandidate:
    """One source scan's original neighbors and local change tradeoff."""

    scan: str
    before: str | None
    after: str | None
    left: ChangeMetric | None
    right: ChangeMetric | None
    score: int | None
    protected: bool
    reasons: tuple[str, ...]


def removal_candidates(
    scans: Iterable[str],
    change_oracle: ChangeOracle,
    *,
    pinned: Iterable[str] = (),
) -> tuple[RemovalCandidate, ...]:
    """Initial local metrics for manual review; this does not recommend a policy.

    Endpoints are included with their missing neighbor/metric as None. The
    max-adjacent count is a diagnostic for interior scans. Pins still get exact
    metrics so their information content is visible alongside their protection.
    """
    ordered, pins, protected = _inputs(scans, pinned)
    changes = _Changes(change_oracle)
    result = []
    for i, scan in enumerate(ordered):
        before = ordered[i - 1] if i else None
        after = ordered[i + 1] if i + 1 < len(ordered) else None
        left = changes(before, scan) if before is not None else None
        right = changes(scan, after) if after is not None else None
        reasons = tuple(reason for reason, applies in (
            ("first", before is None),
            ("last", after is None),
            ("pinned", scan in pins),
        ) if applies)
        result.append(RemovalCandidate(
            scan, before, after, left, right,
            max(left.count, right.count) if left is not None and right is not None else None,
            scan in protected, reasons,
        ))
    return tuple(result)


def plan_manual(
    scans: Iterable[str],
    change_oracle: ChangeOracle,
    *,
    remove: Iterable[str],
    pinned: Iterable[str] = (),
) -> ThinningPlan:
    """Remove the explicit user selection in chronological, auditable order.

    No automatic ranking changes the selection. Each step measures its current
    surviving neighbors exactly, including their merged endpoint difference.
    Unknown, duplicate, endpoint and pinned removals fail before oracle calls.
    """
    ordered, pins, protected = _inputs(scans, pinned)
    selected = check_order([check_scan_id(scan) for scan in remove])
    unknown = sorted(set(selected) - set(ordered))
    if unknown:
        raise ValueError(f"removed scans are absent from the source: {unknown!r}")
    forbidden = sorted(set(selected) & protected)
    if forbidden:
        raise ValueError(f"cannot remove protected scans: {forbidden!r}")
    changes = _Changes(change_oracle)
    previous = {scan: ordered[i - 1] for i, scan in enumerate(ordered) if i}
    following = {scan: ordered[i + 1] for i, scan in enumerate(ordered) if i + 1 < len(ordered)}
    steps = []
    for scan in selected:
        before, after = previous[scan], following[scan]
        left, right = changes(before, scan), changes(scan, after)
        steps.append(RemovalStep(scan, before, after, left, right, changes(before, after), max(left.count, right.count)))
        following[before], previous[after] = after, before
    removed = set(selected)
    return ThinningPlan("manual", ordered, tuple(scan for scan in ordered if scan not in removed), pins, tuple(steps))


def plan_low_change(
    scans: Iterable[str],
    change_oracle: ChangeOracle,
    *,
    drop_count: int | None = None,
    target_count: int | None = None,
    pinned: Iterable[str] = (),
) -> ThinningPlan:
    """Greedily drop the middle of the least-changing surviving triple.

    For A, B, C the score is ``max(changes(A, B), changes(B, C))``. Ties
    remove the earliest B. Endpoints and explicit pins are never candidates.
    After removing B, ``changes(A, C)`` is measured exactly and neighboring
    scores are updated. Adding the old counts would be wrong for reversals.

    Inputs are sorted/validated as canonical scan IDs. Exactly one of
    ``drop_count`` and ``target_count`` is required; an impossible request
    raises rather than silently returning fewer removals. Oracle calls are
    cached per pair, with O(n + removals) distinct pairs and O(n log n) heap
    work. A zero-removal plan makes no oracle calls.
    """
    ordered, pins, protected = _inputs(scans, pinned)
    drop = _removal_count(len(ordered), len(protected), drop_count, target_count)
    if not drop:
        return ThinningPlan("low-change", ordered, ordered, pins, ())

    changes = _Changes(change_oracle)
    previous = [i - 1 for i in range(len(ordered))]
    following = [i + 1 for i in range(len(ordered))]
    alive = [True] * len(ordered)
    revisions = [0] * len(ordered)
    heap: list[tuple[int, int, int]] = []

    def candidate(i: int) -> None:
        if not 0 <= i < len(ordered) or ordered[i] in protected:
            return
        revisions[i] += 1
        left = changes(ordered[previous[i]], ordered[i])
        right = changes(ordered[i], ordered[following[i]])
        heappush(heap, (max(left.count, right.count), i, revisions[i]))

    for i in range(1, len(ordered) - 1):
        candidate(i)

    steps: list[RemovalStep] = []
    while len(steps) < drop:
        score, i, revision = heappop(heap)
        if not alive[i] or revision != revisions[i]:
            continue
        a, c = previous[i], following[i]
        before, scan, after = ordered[a], ordered[i], ordered[c]
        steps.append(RemovalStep(
            scan=scan,
            before=before,
            after=after,
            left=changes(before, scan),
            right=changes(scan, after),
            merged=changes(before, after),
            score=score,
        ))
        alive[i] = False
        following[a], previous[c] = c, a
        candidate(a)
        candidate(c)

    retained = tuple(scan for i, scan in enumerate(ordered) if alive[i])
    return ThinningPlan("low-change", ordered, retained, pins, tuple(steps))


@dataclass(frozen=True)
class AgeBand:
    """An occupied half-open age band and its policy-selected representatives.

    Age is seconds before the newest source scan. Pins/endpoints can retain
    additional scans in this band without consuming a representative slot.
    """

    start_age: float
    end_age: float
    scans: tuple[str, ...]
    representatives: tuple[str, ...]


@dataclass(frozen=True)
class GeometricPlan:
    """A geometric target set; unlike low-change thinning it has no N-drop goal."""

    scans: tuple[str, ...]
    retained: tuple[str, ...]
    pinned: tuple[str, ...]
    recent_seconds: int
    growth: float
    samples_per_band: int
    bands: tuple[AgeBand, ...]
    policy: str = "geometric"

    @property
    def removed(self) -> tuple[str, ...]:
        kept = set(self.retained)
        return tuple(scan for scan in self.scans if scan not in kept)

    @property
    def protected(self) -> tuple[str, ...]:
        endpoints = (self.scans[0], self.scans[-1]) if self.scans else ()
        protected = set(self.pinned) | set(endpoints)
        return tuple(scan for scan in self.scans if scan in protected)


def plan_geometric(
    scans: Iterable[str],
    *,
    recent_seconds: int,
    growth: float = 2,
    samples_per_band: int = 1,
    pinned: Iterable[str] = (),
) -> GeometricPlan:
    """Keep recent scans densely, then sample exponentially wider age bands.

    All scans younger than ``recent_seconds`` survive. Older ages occupy
    ``[recent_seconds * growth**k, recent_seconds * growth**(k+1))``.
    Each band is split into ``samples_per_band`` equal-duration bins; retain
    the newest scan in each occupied bin. This bounds the regular samples
    per band even under irregular scan cadence. Endpoints/pins are retained
    in addition. The anchor is the newest input scan, never wall-clock time.

    There is no mutation or change-oracle call. Empty bands are omitted from
    the audit; only occupied bands are computed, so a growth factor near one
    does not require iterating through every empty band.
    """
    recent = _nonnegative_integer(recent_seconds, "recent_seconds")
    if not recent:
        raise ValueError("recent_seconds must be positive")
    samples = _nonnegative_integer(samples_per_band, "samples_per_band")
    if not samples:
        raise ValueError("samples_per_band must be positive")
    if isinstance(growth, bool) or not isinstance(growth, (float, int)) or not isfinite(growth) or growth <= 1:
        raise ValueError(f"growth must be finite and greater than one, got {growth!r}")
    ordered, pins, protected = _inputs(scans, pinned)
    if not ordered:
        return GeometricPlan(ordered, ordered, pins, recent, growth, samples, ())

    newest = scan_epoch(ordered[-1])
    kept = set(protected)
    occupied: dict[int, list[tuple[str, int]]] = {}
    bounds: dict[int, tuple[float, float]] = {}

    def edges(k: int) -> tuple[float, float]:
        try:
            start = recent * growth ** k
            end = start * growth
        except OverflowError as error:
            raise ValueError("geometric age-band boundary overflow") from error
        if not isfinite(start) or not isfinite(end) or end <= start:
            raise ValueError("geometric age-band boundaries must be finite and distinct")
        return start, end

    for scan in ordered:
        age = newest - scan_epoch(scan)
        if age < recent:
            kept.add(scan)
            continue
        k = max(0, floor(log(age / recent) / log(growth)))
        start, end = edges(k)
        # Logs can round exact boundary ages into the neighboring exponent.
        while age < start and k:
            k -= 1
            start, end = edges(k)
        while age >= end:
            k += 1
            start, end = edges(k)
        occupied.setdefault(k, []).append((scan, age))
        bounds[k] = start, end

    bands = []
    for k in sorted(occupied):
        start, end = bounds[k]
        slots: dict[int, str] = {}
        for scan, age in occupied[k]:
            slot = min(samples - 1, floor((age - start) / (end - start) * samples))
            slots[slot] = scan  # Chronological iteration leaves the newest.
        representatives = tuple(sorted(slots.values()))
        kept.update(representatives)
        bands.append(AgeBand(start, end, tuple(scan for scan, age in occupied[k]), representatives))

    retained = tuple(scan for scan in ordered if scan in kept)
    return GeometricPlan(ordered, retained, pins, recent, growth, samples, tuple(bands))
