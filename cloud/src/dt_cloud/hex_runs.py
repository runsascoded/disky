"""The static name index's hex-run rule (specs/static-hex-runs.md): interiors of long hex ids are not indexed.

On a lowercase string `l` (a name, or a parent path: `/` is not hex, so runs never cross a segment), a **hex run** is
a maximal substring of `[0-9a-f]` of at least `min` characters, `l[a..b]` (0-based, inclusive). A position `p` is
**inside** it iff `a < p ≤ b`; it is **opaque** iff also `p ≤ b − tail`, else it is in the run's **tail**.

- Suffix rows start at every position that is not opaque (the tail's included).
- A literal `q` **occurs** in `l` iff `l.find(q, p) == p` for some position `p` that is not inside a run, or that is in a
  run's tail and the occurrence extends past `b`.

Equivalently, with `before` = the hex characters just before `p` and `after` = those from `p` on, an occurrence of
length `m` at `p` is **dropped** iff `before ≥ 1 ∧ after ≥ 1 ∧ before + after ≥ min ∧ (after > tail ∨ m ≤ after)`.
That one test (`dropped`, `pos_ok_sql`) is what every builder, reader and brute force applies, so they agree exactly.

A rule of `None` is the full index (a generation built before the rule): `occurs` is plain `in`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HEX = "0123456789abcdef"


@dataclass(frozen=True)
class HexRule:
    min: int
    tail: int

    def __post_init__(self):
        if self.min < 2 or not 0 <= self.tail < self.min:
            raise ValueError(f"hex runs: bad rule min={self.min} tail={self.tail} (need min ≥ 2, 0 ≤ tail < min)")

    def to_json(self) -> dict:
        return {"min": self.min, "tail": self.tail}


def parse_rule(raw: str) -> HexRule | None:
    """`STATIC_NAMES_HEX_RUNS`: `MIN,TAIL` (e.g. `16,8`), or `off` (the full index)."""
    raw = raw.strip()
    if raw == "off":
        return None
    parts = raw.split(",")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f"hex runs: {raw!r} is neither MIN,TAIL (e.g. 16,8) nor off")
    return HexRule(int(parts[0]), int(parts[1]))


def rule_from_json(d: dict | None) -> HexRule | None:
    """A generation's recorded rule (`scans.json` / `catalog/meta.json` `hex_runs`); absent = the full index."""
    if d is None:
        return None
    if set(d) != {"min", "tail"}:
        raise ValueError(f"hex runs: bad recorded rule {d!r}")
    return HexRule(int(d["min"]), int(d["tail"]))


def rule_json(rule: HexRule | None) -> dict:
    """`{"hex_runs": …}` to merge into a generation's metadata, or `{}` for the full index."""
    return {"hex_runs": rule.to_json()} if rule else {}


def _hex_before(l: str, p: int) -> int:
    n = 0
    while p - n - 1 >= 0 and l[p - n - 1] in HEX:
        n += 1
    return n


def _hex_after(l: str, p: int) -> int:
    n = 0
    while p + n < len(l) and l[p + n] in HEX:
        n += 1
    return n


def dropped(l: str, p: int, m: int, rule: HexRule | None) -> bool:
    """Whether an occurrence of length `m` at 0-based `p` of `l` is dropped by the rule."""
    if rule is None:
        return False
    before, after = _hex_before(l, p), _hex_after(l, p)
    return before >= 1 and after >= 1 and before + after >= rule.min and (after > rule.tail or m <= after)


def opaque(l: str, p: int, rule: HexRule | None) -> bool:
    """Whether 0-based `p` starts no suffix row (inside a run, before its tail)."""
    if rule is None:
        return False
    before, after = _hex_before(l, p), _hex_after(l, p)
    return before >= 1 and after >= 1 and before + after >= rule.min and after > rule.tail


def kept_positions(l: str, rule: HexRule | None) -> list[int]:
    """The 0-based starts of `l`'s suffix rows: every position with ≥ 3 characters left that is not opaque."""
    return [p for p in range(len(l) - 2) if not opaque(l, p, rule)]


def first_occurrence(q: str, l: str, rule: HexRule | None) -> int:
    """The first 0-based position where `q` occurs in `l` under the rule, or −1."""
    p = l.find(q)
    while p >= 0 and dropped(l, p, len(q), rule):
        p = l.find(q, p + 1)
    return p


def occurs(q: str, l: str, rule: HexRule | None) -> bool:
    """Whether literal `q` occurs in `l` (both lowercase) under the rule."""
    return first_occurrence(q, l, rule) >= 0


_LEAD = re.compile(f"[{HEX}]*")


def hex_affected(q: str, rule: HexRule | None) -> bool:
    """Whether some occurrence of `q` can be dropped (so a response says so): `q` is hex throughout, or its leading
    hex prefix is longer than the tail. Any other literal matches exactly as in the full index."""
    if rule is None or not q:
        return False
    q = q.lower()
    lead = len(_LEAD.match(q).group(0))
    return lead == len(q) or lead > rule.tail


# ── SQL (DuckDB; positions 1-based) ────────────────────────────────────────

_HEXS = f"'{HEX}'"


def _before_sql(l: str, p: str) -> str:
    return f"(({p}) - 1 - length(rtrim(left({l}, ({p}) - 1), {_HEXS})))"


def _after_sql(l: str, p: str) -> str:
    return f"(length({l}) - ({p}) + 1 - length(ltrim(substring({l}, {p}), {_HEXS})))"


def has_run_sql(l: str, rule: HexRule) -> str:
    """Whether `l` holds a run at all (a cheap per-string gate before the per-position test)."""
    return f"regexp_matches({l}, '[0-9a-f]{{{rule.min}}}')"


def dropped_sql(l: str, p: str, m: str, rule: HexRule | None) -> str:
    """SQL: an occurrence of length `m` at 1-based `p` of `l` is dropped (`dropped`)."""
    if rule is None:
        return "false"
    b, a = _before_sql(l, p), _after_sql(l, p)
    return f"({b} >= 1 AND {a} >= 1 AND {b} + {a} >= {rule.min} AND ({a} > {rule.tail} OR ({m}) <= {a}))"


def opaque_sql(l: str, p: str, rule: HexRule | None) -> str:
    """SQL: 1-based `p` of `l` starts no suffix row (`opaque`)."""
    if rule is None:
        return "false"
    b, a = _before_sql(l, p), _after_sql(l, p)
    return f"({b} >= 1 AND {a} >= 1 AND {b} + {a} >= {rule.min} AND {a} > {rule.tail})"


def kept_sql(l: str, p: str, rule: HexRule | None) -> str:
    """SQL: 1-based `p` of `l` starts a suffix row (given ≥ 3 characters left)."""
    if rule is None:
        return "true"
    return f"(NOT {has_run_sql(l, rule)} OR NOT {opaque_sql(l, p, rule)})"


def first_sql(l: str, x: str, rule: HexRule | None) -> str:
    """SQL: the 1-based position of `x`'s first occurrence in `l` under the rule, 0 if none (`instr` without a rule)."""
    if rule is None:
        return f"instr({l}, {x})"
    m = f"length({x})"
    scan = (f"coalesce(list_min(list_filter(range(1, length({l}) - {m} + 2), "
            f"lambda p: substring({l}, p, {m}) = {x} AND NOT {dropped_sql(l, 'p', m, rule)})), 0)")
    return (f"(CASE WHEN instr({l}, {x}) = 0 THEN 0 WHEN NOT {has_run_sql(l, rule)} THEN instr({l}, {x}) "
            f"WHEN NOT {dropped_sql(l, f'instr({l}, {x})', m, rule)} THEN instr({l}, {x}) ELSE {scan} END)")


def occurs_sql(l: str, x: str, rule: HexRule | None) -> str:
    """SQL: `x` occurs in `l` under the rule (`contains` without one)."""
    if rule is None:
        return f"contains({l}, {x})"
    return f"({first_sql(l, x, rule)} > 0)"


def grams_sql(l: str, rule: HexRule | None) -> str:
    """SQL: the distinct one- and two-character literals occurring in `l` under the rule (a list)."""
    if rule is None:
        return (f"list_distinct(list_transform(range(1, length({l}) + 1), lambda p: substring({l}, p, 1))"
                f" || list_transform(range(1, length({l})), lambda p: substring({l}, p, 2)))")
    return (f"(CASE WHEN NOT {has_run_sql(l, rule)} THEN {grams_sql(l, None)} ELSE list_distinct("
            f"list_transform(list_filter(range(1, length({l}) + 1), lambda p: NOT {dropped_sql(l, 'p', '1', rule)}), lambda p: substring({l}, p, 1))"
            f" || list_transform(list_filter(range(1, length({l})), lambda p: NOT {dropped_sql(l, 'p', '2', rule)}), lambda p: substring({l}, p, 2))) END)")


# ── Anchored terms (`^q`, `q$`, `^q$`; `static_anchors`) ─────────────────────


def segment_occurs(seg: str, q: str, mode: str | None, rule: HexRule | None) -> bool:
    """Whether a lowercase segment holds `q` in `mode` (`start` | `end` | `exact`; None: contains) under the rule
    (`hexRuns.ts` `segmentOccurs`). An occurrence at a segment's start has no hex digit before it, so it is never
    dropped: `^q` and `^q$` are `startswith` and `==` under any rule. `q$`'s one candidate occurrence ends the segment
    and is tested by `dropped` like any other (a proper suffix of a trailing run is dropped, as is a `q` with more than
    `tail` leading hex digits starting inside a run), so only `hex_affected` literals can lose `q$` matches."""
    if mode == "start":
        return seg.startswith(q)
    if mode == "exact":
        return seg == q
    if mode == "end":
        return seg.endswith(q) and not dropped(seg, len(seg) - len(q), len(q), rule)
    return occurs(q, seg, rule)


def end_dropped_sql(l: str, k: str, rule: HexRule | None) -> str:
    """SQL: the occurrence of `k` that ends `l` (given `ends_with(l, k)`) is dropped (`segment_occurs`' `end` test). An
    occurrence starting at a non-hex character is never dropped, so that is checked first."""
    if rule is None:
        return "false"
    return f"(contains({_HEXS}, left({k}, 1)) AND {dropped_sql(l, f'length({l}) - length({k}) + 1', f'length({k})', rule)})"
