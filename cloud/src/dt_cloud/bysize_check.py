"""Check a `bysize` sort's thresholded views against per-path sums over the
`path` sort (spec `bysize-path-total.md`).

A view at `P` (the store root `''`, or a path) draws every path under it
whose total — over all its owner slices — clears `thrAt(depth) = thr ·
atten^(depth − dP − 1)`, with `thr = total(P) · min_area / (w · h)` (the
site's pixel-budget threshold, `view.ts`). For each view this computes:

- **reference**, from the `path` sort alone (DuckDB): the paths under `P`
  whose summed slices clear the threshold, with their bytes per owner. A path
  over the threshold has some slice of at least `thr_min / K`, `K` the
  number of owner values (+1 for unowned), so the sums are taken over the
  paths holding such a slice — two scans, no hash table over every path;
- **candidate**, the site reader's answer from the `bysize` sort: the row
  groups the planner selects (`tier_plan.select_bysize` over the sort's
  `.groups.json`: `b_max ≥ ⌊thr_min⌋` and the path range), their rows under
  `P`, kept per path when the decoded slices' sum clears the threshold
  (`readSizeRects` on a sort keyed on the path's total);
- **per slice**: what a sort cut per slice returned (a slice kept when its
  own bytes clear the threshold) — the undercount the re-cut removes.

Paths are compared on total bytes and on bytes per owner, exactly.
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass, field
from functools import partial

import duckdb

err = partial(print, file=sys.stderr)

#: The site's defaults (`view.ts` `MIN_AREA_DEFAULT`, `ATTEN_DEFAULT`).
MIN_AREA = 12
ATTEN = 2.0
#: The path-order sentinel a root read uses as its upper bound (`tier_plan.PATH_MAX`).
PATH_MAX = "￿"

Slices = dict[tuple[int, str], dict[str, int]]


@dataclass
class ViewCheck:
    path: str
    thr: float
    #: Paths drawn by the reference / the candidate, and how many agree exactly (bytes and owners).
    ref_paths: int
    cand_paths: int
    equal: int
    #: Reference paths the per-slice read drew short or not at all, and the bytes it missed.
    slice_short: int
    slice_missing: int
    slice_missing_bytes: int
    #: The candidate's planned row groups and the rows they hold.
    groups: int
    rows: int
    diffs: list = field(default_factory=list)

    @property
    def exact(self) -> bool:
        return self.ref_paths == self.cand_paths == self.equal


def _rect(P: str) -> tuple[int, str, str]:
    """`(dLo, pLo, pHi)` of the subtree under `P`."""
    if P == "":
        return 1, "", PATH_MAX
    return P.count("/") + 2, f"{P}/", f"{P}0"


def _slices(rows) -> Slices:
    out: Slices = {}
    for d, p, u, s in rows:
        m = out.setdefault((int(d), p), {})
        k = u if u is not None else ""
        m[k] = m.get(k, 0) + int(s)
    return out


def check_views(
    path_sort: str,
    bysize: str,
    views: list[str] | None = None,
    *,
    w: int = 1280,
    h: int = 768,
    min_area: float = MIN_AREA,
    atten: float = ATTEN,
    con: duckdb.DuckDBPyConnection | None = None,
    max_diffs: int = 20,
) -> list[ViewCheck]:
    """One `ViewCheck` per view (default: the root and every depth-1 path).
    `path_sort` / `bysize` are local parquet files; `bysize`'s `.groups.json`
    sits beside it."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from disk_tree.find.groups import groups_path
    from disk_tree.find.tier_plan import Query, load_groups, select_bysize

    con = con or duckdb.connect()
    src = f"read_parquet('{path_sort}')"
    tops = con.execute(f"SELECT path, SUM(size) FROM {src} WHERE depth = 1 GROUP BY path ORDER BY path").fetchall()
    totals = {p: int(b) for p, b in tops}
    totals[""] = sum(totals.values())
    views = [""] + sorted(totals.keys() - {""}) if views is None else views
    for P in views:
        if P not in totals:
            totals[P] = int(con.execute(f"SELECT COALESCE(SUM(size), 0) FROM {src} WHERE depth = ? AND path = ?", [P.count("/") + 1, P]).fetchone()[0])
    thr = {P: totals[P] * min_area / (w * h) for P in views}
    k = int(con.execute(f"SELECT COUNT(DISTINCT usr) + 1 FROM {src}").fetchone()[0])
    floor = min(thr.values()) / k
    err(f"bysize-check: {len(views)} views, thr {min(thr.values()):,.0f}–{max(thr.values()):,.0f} B, K={k}, slice floor {floor:,.0f} B")
    # Reference: every slice of each path holding a slice ≥ floor (a superset of the paths any view draws).
    con.execute(f"CREATE OR REPLACE TEMP TABLE cand AS SELECT DISTINCT depth, path FROM {src} WHERE size >= {math.floor(floor)}")
    ref_rows = con.execute(f"SELECT s.depth, s.path, s.usr, s.size FROM {src} s SEMI JOIN cand USING (depth, path)").fetchall()
    ref_all = _slices(ref_rows)
    err(f"bysize-check: reference over {len(ref_all):,} candidate paths ({len(ref_rows):,} slices)")

    groups = load_groups(groups_path(bysize))
    pf = pq.ParquetFile(bysize)
    out: list[ViewCheck] = []
    for P in views:
        dLo, pLo, pHi = _rect(P)
        dP = dLo - 1

        def thr_at(d: int, P=P, dP=dP) -> float:
            return thr[P] * atten ** max(0, d - dP - 1)

        def under(d: int, p: str, dLo=dLo, pLo=pLo, pHi=pHi) -> bool:
            return d >= dLo and pLo <= p < pHi

        ref = {key: us for key, us in ref_all.items() if under(*key) and sum(us.values()) >= thr_at(key[0])}
        q = Query(path=P if P else ".", thr=thr[P], atten=atten, max_depth=None)
        sel = select_bysize(groups, q)
        t = pf.read_row_groups([g.rg for g in sel], columns=["depth", "path", "usr", "size"]) if sel else None
        if t is not None:
            t = t.filter(pc.and_(pc.greater_equal(t["depth"], dLo), pc.and_(pc.greater_equal(t["path"], pLo), pc.less(t["path"], pHi))))
            got = _slices(zip(*(t[c].to_pylist() for c in ("depth", "path", "usr", "size"))))
        else:
            got = {}
        cand = {key: us for key, us in got.items() if sum(us.values()) >= thr_at(key[0])}
        equal = sum(1 for key, us in cand.items() if ref.get(key) == us)
        diffs = [{"depth": key[0], "path": key[1], "ref": ref.get(key), "cand": cand.get(key)} for key in sorted(ref.keys() | cand.keys()) if ref.get(key) != cand.get(key)]
        # The per-slice read: slices whose own bytes clear the threshold.
        short = missing = missing_b = 0
        for key, us in ref.items():
            kept = {u: b for u, b in us.items() if b >= thr_at(key[0])}
            if not kept:
                missing += 1
                missing_b += sum(us.values())
            elif kept != us:
                short += 1
                missing_b += sum(us.values()) - sum(kept.values())
        out.append(ViewCheck(
            path=P, thr=thr[P], ref_paths=len(ref), cand_paths=len(cand), equal=equal,
            slice_short=short, slice_missing=missing, slice_missing_bytes=missing_b,
            groups=len(sel), rows=sum(g.rows for g in sel), diffs=diffs[:max_diffs],
        ))
    return out


def report(checks: list[ViewCheck]) -> str:
    return json.dumps([{**asdict(c), "exact": c.exact} for c in checks])
