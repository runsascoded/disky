"""The reader's span selection, run offline over a tier's ``.groups.json``
(spec path-store.md §5 phase 0/1: ``disk-tree tiers plan``).

A subtree read at ``P`` with threshold ``thr`` decodes the row groups whose
footer stats can't rule them out, then keeps the rows that pass. Which groups
those are is decided from the sidecar alone, so the cost of a view — groups,
rows decoded, bytes fetched — can be reported for any scan without a deploy,
and the two sorts compared on the same question:

- ``path`` mirrors ``site/functions/_lib/index.ts`` ``readRects`` exactly: one
  rect ``{dLo: dP+1, dHi: dP+max_depth, pLo: P/, pHi: P0}`` (``P0`` = ``P`` +
  ``'0'``, the character after ``'/'``); a group is a span when its depth range
  meets the rect and, for a single-depth group, its path range does too
  (``groupMatches``); ``b_max ≥ ⌊thrAt(dLo)⌋`` prunes at the span query and
  ``b_max ≥ thrAt(max(d_min, dLo))`` on the kept list, with
  ``thrAt(d) = thr · atten^(d − dP − 1)``.
- ``bysize`` is the store's predicate: ``b_max ≥ ⌊thr_min⌋ AND p_max ≥ P/ AND
  p_min < P0``, ``thr_min`` the smallest per-depth threshold the read applies
  — ``thrAt`` is monotone in depth, so the lower of its values at the
  shallowest and the deepest depth read (the deepest with ``atten < 1``, the
  shallowest with ``atten ≥ 1``). Rows within a bucket are path-sorted, so the
  path stats prune every group.

Both are sound for any group (min/max stats bound every row it holds); the
report's ``matched`` count — rows that actually satisfy the predicate, from
the parquet — is what the decoded rows are compared against.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from disk_tree import blobfs
from disk_tree.find.groups import GROUP_FIELDS, GROUPS_SUFFIX

#: The path-order sentinel the reader uses as a root read's upper bound.
PATH_MAX = '￿'
#: The tier a sidecar is for, from its name: `<stem>.<tier>[-by-…].groups.json`.
TIER_TAGS = ('path', 'bysize')


@dataclass(frozen=True)
class Group:
    rg: int
    d_min: int
    d_max: int
    p_min: str
    p_max: str
    b_max: int
    u_min: str | None
    u_max: str | None
    row_start: int
    row_end: int
    rg_json: str
    #: None on a sidecar written before `b_min` was appended.
    b_min: int | None = None

    @property
    def rows(self) -> int:
        return self.row_end - self.row_start

    @property
    def bytes(self) -> int:
        """Compressed bytes of every column chunk (what a whole-group range read fetches)."""
        _, _, cols = json.loads(self.rg_json)
        return sum(size for _, size, _ in cols)


def load_groups(sidecar: str) -> list[Group]:
    """The sidecar's groups (local or URL). A pre-`b_min` document loads with
    `b_min = None`; anything else off `GROUP_FIELDS` is refused."""
    doc = json.loads(blobfs.read_text(sidecar))
    if doc.get('v') != 1:
        raise ValueError(f"{sidecar}: unknown groups version {doc.get('v')!r}")
    out = []
    for arr in doc['groups']:
        if len(arr) not in (len(GROUP_FIELDS) - 1, len(GROUP_FIELDS)):
            raise ValueError(f"{sidecar}: group entry has {len(arr)} fields, expected {len(GROUP_FIELDS)}")
        out.append(Group(**dict(zip(GROUP_FIELDS, arr))))
    return out


def tier_of(sidecar: str) -> str:
    """`…/<stem>.<tier>[-by-<cols>].groups.json` → `<tier>`."""
    name = sidecar.rsplit('/', 1)[-1]
    if not name.endswith(GROUPS_SUFFIX):
        raise ValueError(f"not a groups sidecar: {sidecar}")
    tag = name[: -len(GROUPS_SUFFIX)].rsplit('.', 1)[-1].split('-by-', 1)[0]
    if tag not in TIER_TAGS:
        raise ValueError(f"{sidecar}: can't tell the tier from the name ({tag!r}); pass --tier")
    return tag


def parquet_of(sidecar: str) -> str:
    return sidecar[: -len(GROUPS_SUFFIX)] + '.parquet'


@dataclass(frozen=True)
class Query:
    """A subtree read: `P`'s children and below, thresholded."""
    path: str
    thr: float
    atten: float = 1.0
    max_depth: int | None = None

    @property
    def is_root(self) -> bool:
        return self.path in ('', '.')

    @property
    def depth(self) -> int:
        """`dP`: the root (`''` or `.`) is 0; else the segment count."""
        return 0 if self.is_root else self.path.count('/') + 1

    @property
    def p_lo(self) -> str:
        return '' if self.is_root else self.path + '/'

    @property
    def p_hi(self) -> str:
        return PATH_MAX if self.is_root else self.path + '0'

    @property
    def d_lo(self) -> int:
        return self.depth + 1

    @property
    def d_hi(self) -> int | None:
        return None if self.max_depth is None else self.depth + self.max_depth

    def thr_at(self, depth: int) -> float:
        """`view.ts`: `threshold * atten ** max(0, depth - dP - 1)`."""
        return self.thr * self.atten ** max(0, depth - self.depth - 1)


def group_matches(g: Group, q: Query, b_min: float = 0) -> bool:
    """`index.ts` `groupMatches` for one rect (no lens): the depth rect, the
    path rect for single-depth groups, and the `⌊b_min⌋` floor."""
    if b_min > 0 and g.b_max < math.floor(b_min):
        return False
    d_hi = q.d_hi if q.d_hi is not None else 10**9
    if not (g.d_max >= q.d_lo and g.d_min <= d_hi):
        return False
    return g.d_min != g.d_max or (g.p_max >= q.p_lo and g.p_min <= q.p_hi)


def select_path(groups: list[Group], q: Query) -> list[Group]:
    """`readRects` over one rect: the span query, then the per-group threshold
    at the group's shallowest depth in the rect."""
    spans = [g for g in groups if group_matches(g, q, q.thr_at(q.d_lo) if q.thr > 0 else 0)]
    if q.thr <= 0:
        return spans
    return [g for g in spans if g.b_max >= q.thr_at(max(g.d_min, q.d_lo))]


def select_bysize(groups: list[Group], q: Query) -> list[Group]:
    """The store's predicate (spec §2.1): `b_max ≥ ⌊thr_min⌋ AND p_max ≥ P/ AND p_min < P0`,
    `thr_min` the lowest threshold any depth the read covers applies — at the
    deepest depth (the tier's own `d_max` when unbounded) for `atten < 1`, at
    `d_lo` for `atten ≥ 1`, where deeper rows need *more* bytes."""
    d_hi = q.d_hi if q.d_hi is not None else max((g.d_max for g in groups), default=q.d_lo)
    thr_min = min(q.thr_at(q.d_lo), q.thr_at(max(d_hi, q.d_lo)))
    return [
        g for g in groups
        if (thr_min <= 0 or g.b_max >= math.floor(thr_min)) and g.p_max >= q.p_lo and g.p_min < q.p_hi
    ]


SELECTORS = {'path': select_path, 'bysize': select_bysize}


def count_matched(parquet: str, q: Query) -> int:
    """Rows that satisfy the read — `depth ∈ [dLo, dHi]`, `path ∈ [P/, P0)`,
    `size ≥ thrAt(depth)` — from the tier's own rows (a pushdown read of
    `depth`/`path`/`size`; local or URL)."""
    filters = [('depth', '>=', q.d_lo), ('path', '>=', q.p_lo), ('path', '<', q.p_hi)]
    if q.d_hi is not None:
        filters.append(('depth', '<=', q.d_hi))
    t = blobfs.read_table(parquet, columns=['depth', 'size'], filters=filters)
    if q.thr <= 0:
        return t.num_rows
    depths = t.column('depth').to_pylist()
    sizes = t.column('size').to_pylist()
    return sum(1 for d, s in zip(depths, sizes) if s is not None and s >= q.thr_at(d))


@dataclass(frozen=True)
class Plan:
    tier: str
    query: Query
    selected: tuple[int, ...]
    rows: int
    bytes: int
    total_groups: int
    total_rows: int
    total_bytes: int
    #: rows satisfying the predicate; None when the parquet wasn't counted
    matched: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def groups(self) -> int:
        return len(self.selected)

    @property
    def waste(self) -> float | None:
        """Decoded rows that aren't answers, as a fraction of decoded rows."""
        if self.matched is None or not self.rows:
            return None
        return 1 - self.matched / self.rows

    def asdict(self) -> dict:
        q = self.query
        return {
            'tier': self.tier,
            'path': q.path, 'depth': q.depth, 'thr': q.thr, 'atten': q.atten, 'max_depth': q.max_depth,
            'd_lo': q.d_lo, 'd_hi': q.d_hi, 'p_lo': q.p_lo, 'p_hi': q.p_hi,
            'groups': self.groups, 'rows': self.rows, 'bytes': self.bytes,
            'total_groups': self.total_groups, 'total_rows': self.total_rows, 'total_bytes': self.total_bytes,
            'matched': self.matched, 'waste': self.waste,
            'selected': list(self.selected),
        }


def plan(
    sidecar: str,
    q: Query,
    tier: str | None = None,
    parquet: str | None = None,
    count: bool = True,
) -> Plan:
    """Select the groups a read of `q` touches on `tier` (default: from the
    sidecar's name) and, with `count`, open the parquet (default: beside the
    sidecar) to count the rows that actually pass."""
    tier = tier or tier_of(sidecar)
    if tier not in SELECTORS:
        raise ValueError(f"unknown tier {tier!r}; choose from {list(SELECTORS)}")
    groups = load_groups(sidecar)
    sel = SELECTORS[tier](groups, q)
    matched = count_matched(parquet or parquet_of(sidecar), q) if count else None
    return Plan(
        tier=tier, query=q,
        selected=tuple(g.rg for g in sel),
        rows=sum(g.rows for g in sel), bytes=sum(g.bytes for g in sel),
        total_groups=len(groups), total_rows=sum(g.rows for g in groups), total_bytes=sum(g.bytes for g in groups),
        matched=matched,
    )
