"""Reference reader for the change-interval path store (specs/interval-store.md §3): the treemap view and
the diff at any scan date, planned from each served sort's `.groups.parquet` the way the Worker plans
from footers, with the read cost counted; and the same views computed from a scan's own per-scan path
store, for parity.

The view is `view.ts`'s plain view (no lens, owner pool, classes or query): P's total sets the pixel
threshold, `thrAt(d) = thr · atten^(d − dP − 1)`, every path under P whose total clears its depth's
threshold is a tile, and each parent's untiled rest is its `(other)`. Totals are per path (a path's owner
slices summed), which is exact; see `perscan_bysize_view` for the per-scan `bysize` read's per-slice
threshold, which is not.
"""
from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .static_names import OPEN

MIN_AREA = 12
ATTEN = 2.0
HARD_CAP = 50_000
#: The columns a view decodes from the path versions (all but nothing: every column is a `Row` field).
VIEW_COLS = ["depth", "path", "vf", "vt", "kind", "size", "n_files", "n_children", "n_desc", "mtime", "wts", "wb", "c2", "c3", "c4", "us", "last_read"]


@dataclass
class Cost:
    """What a read touched: row groups decoded, the rows they hold, and their projected compressed bytes
    (what a range read fetches), per sort."""
    groups: dict[str, int] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    bytes: dict[str, int] = field(default_factory=dict)

    def add(self, sort: str, groups: int, rows: int, nbytes: int) -> None:
        self.groups[sort] = self.groups.get(sort, 0) + groups
        self.rows[sort] = self.rows.get(sort, 0) + rows
        self.bytes[sort] = self.bytes.get(sort, 0) + nbytes

    def total(self) -> dict:
        return {"groups": sum(self.groups.values()), "rows": sum(self.rows.values()), "bytes": sum(self.bytes.values()),
                "by_sort": {k: [self.groups[k], self.rows[k], self.bytes[k]] for k in sorted(self.groups)}}


def p_range(path: str) -> tuple[str, str]:
    """`[P/, P0)`: every path strictly under P (the root: everything)."""
    return ("", "￿") if path == "" else (path + "/", path + "0")


def depth_of(path: str) -> int:
    return 0 if path == "" else path.count("/") + 1


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


# ── A served sort ──────────────────────────────────────────────────────────


class Sort:
    """One served sort (`<sort>.parquet` + `.groups.parquet`): its groups' bounds in memory, row groups
    read on demand, and the projected bytes each read would fetch."""

    def __init__(self, path: str | Path, name: str):
        self.name = name
        self.path = str(path)
        gp = self.path.removesuffix(".parquet") + ".groups.parquet"
        self.groups = pq.read_table(gp).to_pylist()
        self.pf = pq.ParquetFile(self.path)
        names = self.pf.schema_arrow.names
        self.col_index = {c: i for i, c in enumerate(names)}

    def at_depth(self, depth: int, lo: str, hi: str) -> list[dict]:
        """Groups that can hold rows at `depth` with `lo ≤ path ≤ hi`: single-depth groups by binary
        search over their path ranges (they're sorted within a segment), plus every multi-depth group."""
        if not hasattr(self, "_by_depth"):
            self._by_depth: dict[int, list[dict]] = {}
            self._multi: list[dict] = []
            for g in self.groups:
                if g["d_min"] == g["d_max"]:
                    self._by_depth.setdefault(g["d_min"], []).append(g)
                else:
                    self._multi.append(g)
            for gs in self._by_depth.values():
                gs.sort(key=lambda g: g["p_min"])
            self._pmin = {d: [g["p_min"] for g in gs] for d, gs in self._by_depth.items()}
            self._pmax_prefix = {d: [] for d in self._by_depth}
            for d, gs in self._by_depth.items():
                m = ""
                for g in gs:
                    m = max(m, g["p_max"])
                    self._pmax_prefix[d].append(m)
        gs = self._by_depth.get(depth, [])
        out = [g for g in self._multi if g["d_min"] <= depth <= g["d_max"]]
        # Groups with p_min ≤ hi; of those, the ones whose p_max ≥ lo (p_max prefix-max skips a prefix).
        j = bisect.bisect_right(self._pmin[depth], hi) if gs else 0
        i = bisect.bisect_left(self._pmax_prefix.get(depth, []), lo, 0, j) if gs else 0
        out += [g for g in gs[i:j] if g["p_max"] >= lo]
        return out

    def nbytes(self, g: dict, columns: list[str]) -> int:
        _, _, cols = json.loads(g["rg_json"])
        return sum(cols[self.col_index[c]][1] for c in columns)

    def read(self, gs: list[dict], columns: list[str], cost: Cost) -> pa.Table:
        if not gs:
            return pa.table({c: pa.array([], self.pf.schema_arrow.field(c).type) for c in columns})
        cost.add(self.name, len(gs), sum(g["row_end"] - g["row_start"] for g in gs), sum(self.nbytes(g, columns) for g in gs))
        return self.pf.read_row_groups([g["rg"] for g in gs], columns=columns)


def live(g: dict, D: int) -> bool:
    """A group can hold a version live at D."""
    return g["vf_min"] <= D < g["vt_max"]


def live_rows(t: pa.Table, D: int) -> pa.Table:
    return t.filter(pc.and_(pc.less_equal(t["vf"], D), pc.greater(t["vt"], D)))


def combine(t: pa.Table) -> pa.Table:
    """Base + runs: rows with equal `(depth, path, vf)` are one version; the smallest `vt` is its newest
    information (a close record from a later run). A base-only read has nothing to combine."""
    if t.num_rows == 0:
        return t
    idx = pc.sort_indices(t, [("depth", "ascending"), ("path", "ascending"), ("vf", "ascending"), ("vt", "ascending")])
    t = t.take(idx)
    keep = [True] * t.num_rows
    d, p, vf = t["depth"].to_pylist(), t["path"].to_pylist(), t["vf"].to_pylist()
    for i in range(1, t.num_rows):
        if (d[i], p[i], vf[i]) == (d[i - 1], p[i - 1], vf[i - 1]):
            keep[i] = False
    return t.filter(pa.array(keep))


# ── Aggregates (the reader's `Agg`) ────────────────────────────────────────


@dataclass
class Agg:
    b: int = 0
    o: int = 0
    wts: float = 0.0
    wb: int = 0
    a: int | None = None
    cb: dict[str, int] = field(default_factory=dict)
    ub: dict[str, int] = field(default_factory=dict)
    kind: str | None = None
    nc: int | None = None


def us_map(us: str, size: int) -> dict[str, int]:
    """`us` (`interval_store.us_sql_from_raw`) → per-user bytes."""
    if us == "":
        return {}
    if us.startswith("["):
        return {u: b for u, b in json.loads(us)}
    return {us: size}


def agg_of(r: dict) -> Agg:
    a = Agg(b=r["size"], o=r["n_files"], kind=r["kind"], nc=None if r["n_children"] < 0 else r["n_children"],
            a=r["last_read"] if r.get("last_read", -1) >= 0 else None)
    if r["wb"] > 0:
        a.wts, a.wb = r["wts"], r["wb"]
    for k in ("c2", "c3", "c4"):
        if r[k]:
            a.cb[k[1]] = r[k]
    a.ub = us_map(r["us"], r["size"])
    return a


def subtract(parent: Agg, kids: list[Agg]) -> Agg:
    out = Agg(a=parent.a)
    out.b = parent.b - sum(k.b for k in kids)
    out.o = max(0, parent.o - sum(k.o for k in kids))
    out.wts = parent.wts - sum(k.wts for k in kids)
    out.wb = max(0, parent.wb - sum(k.wb for k in kids))
    for key in ("cb", "ub"):
        for k, v in getattr(parent, key).items():
            r = v - sum(getattr(kid, key).get(k, 0) for kid in kids)
            if r > 0:
                getattr(out, key)[k] = r
    return out


def node(name: str, a: Agg) -> dict:
    n = {"n": name, "k": a.kind or "dir", "b": a.b, "o": a.o}
    if a.wb:
        n["d"] = round(a.wts / a.wb / 86400)
    if a.a is not None:
        n["a"] = a.a
    if a.cb:
        n["cb"] = dict(sorted(a.cb.items(), key=lambda x: -x[1]))
    if a.ub:
        n["us"] = sorted(([u, b] for u, b in a.ub.items()), key=lambda x: (-x[1], x[0]))  # ties by name (the reader keeps row order)
    return n


def tree(path: str, root: Agg, kept: dict[str, Agg], thr_at, root_name: str = "all buckets") -> dict:
    """`buildView`'s tree over the kept tiles: children by bytes, then `(other)` when the rest clears
    the first child's depth's threshold, `f` = the parent's children not drawn (where it knows them)."""
    kids: dict[str, list[str]] = {}
    for p in kept:
        par = parent_of(p)
        kids.setdefault(par if par in kept else path, []).append(p)

    def build(p: str, a: Agg) -> dict:
        n = node(root_name if p == "" else p.rsplit("/", 1)[-1], a)
        cps = kids.get(p, [])
        if not cps:
            return n
        n["c"] = sorted((build(cp, kept[cp]) for cp in cps), key=lambda x: -x["b"])
        rest = subtract(a, [kept[cp] for cp in cps])
        if rest.b > thr_at(depth_of(cps[0])):
            o = node("(other)", rest)
            o["f"] = max(0, a.nc - len(cps)) if a.nc is not None else None
            n["c"].append(o)
        return n

    return build(path, root)


def canon(n: dict) -> str:
    """A node without its children as canonical JSON (sorted keys, compact): what orders siblings that tie
    on bytes and name (`intervalStore.test.ts` `canon` is the same)."""
    return json.dumps({k: v for k, v in n.items() if k != "c"}, sort_keys=True, separators=(",", ":"))


def flatten(t: dict) -> dict[str, dict]:
    """A tree as `{key: node-without-children}`, keyed by the names from the root (an `(other)` under its
    parent). A tile whose parent isn't drawn hangs off the root, so two such tiles can share a key: the
    later ones get `#2`, `#3`… in their siblings' order (bytes, name, then `canon`)."""
    out: dict[str, dict] = {}

    def rec(n: dict, p: str) -> None:
        k, i = p, 1
        while k in out:
            i += 1
            k = f"{p}#{i}"
        out[k] = {x: v for x, v in n.items() if x != "c"}
        for c in sorted(n.get("c", []), key=lambda c: (-c["b"], c["n"], canon(c))):
            rec(c, f"{p}/{c['n']}")

    rec(t, "")
    return out


# ── The interval store ─────────────────────────────────────────────────────


class Store:
    """The served sorts of one generation (a dir holding `path`, `bysize`, `reads` `.parquet` + `.groups.parquet`)."""

    def __init__(self, root: str | Path, runs: list[str | Path] = ()):
        root = Path(root)
        self.path = Sort(root / "path.parquet", "path")
        self.bysize = Sort(root / "bysize.parquet", "bysize")
        # The read days are a column of the path versions (`interval_store.fold`); `reads` stays an
        # optional standalone sort.
        self.reads = Sort(root / "reads.parquet", "reads") if (root / "reads.parquet").exists() else None
        # Per-scan runs (`interval_append`, oldest first): each its own `path` and `bysize`, read beside the base.
        self.runs = [(Sort(Path(r) / "path.parquet", "path"), Sort(Path(r) / "bysize.parquet", "bysize")) for r in runs]

    def tiers(self, name: str) -> list[tuple[Sort, bool]]:
        """A sort's tiers: the base's, then each run's (`True`: a run)."""
        i = ("path", "bysize").index(name)
        return [(getattr(self, name), False), *((r[i], True) for r in self.runs)]

    @staticmethod
    def may_hold(g: dict, D: int, run: bool) -> bool:
        """A group can matter at D: the base's when a version in it is live then (a later run's close record only ever
        lowers a `vt`); a run's when a version in it opened by then — a close record is not live, yet it ends a
        version another tier holds as live."""
        return g["vf_min"] <= D if run else live(g, D)

    def read_tiers(self, plans: list[tuple[Sort, list[dict]]], cols: list[str], cost: Cost, D: int) -> pa.Table:
        """The planned groups of every tier, combined (one row per version, the smallest `vt`), live at D."""
        ts = [s.read(sorted(gs, key=lambda g: g["rg"]), cols, cost) for s, gs in plans]
        ts = [t for t in ts if t.num_rows] or ts[:1]
        return live_rows(combine(pa.concat_tables(ts) if len(ts) > 1 else ts[0]), D)

    # The reads, planned as `index.ts` plans them, with liveness at D added to every group test.
    def point(self, D: int, depth: int, lo: str, hi: str, cost: Cost, sort: Sort | None = None, cols=VIEW_COLS) -> pa.Table:
        """Rows at one depth with `lo ≤ path ≤ hi`, live at D."""
        tiers = [(sort, False)] if sort is not None else self.tiers("path")
        t = self.read_tiers([(s, [g for g in s.at_depth(depth, lo, hi) if self.may_hold(g, D, run)]) for s, run in tiers], cols, cost, D)
        return t.filter(pc.and_(pc.equal(t["depth"], depth), pc.and_(pc.greater_equal(t["path"], lo), pc.less_equal(t["path"], hi))))

    def plan_path(self, D: int, d_lo: int, d_hi: int, lo: str, hi: str, thr_at) -> list[tuple[Sort, list[dict]]]:
        """`planRects` on `path`, per tier: groups meeting the depth rect (and the path range when the group is one
        depth), whose biggest row clears the threshold at their shallowest depth in the read."""
        out = []
        for s, run in self.tiers("path"):
            gs = []
            for g in s.groups:
                if not self.may_hold(g, D, run) or g["d_max"] < d_lo or g["d_min"] > d_hi:
                    continue
                if g["d_min"] == g["d_max"] and (g["p_max"] < lo or g["p_min"] >= hi):
                    continue
                if g["b_max"] < thr_at(max(g["d_min"], d_lo)):
                    continue
                gs.append(g)
            out.append((s, gs))
        return out

    def plan_bysize(self, D: int, lo: str, hi: str, thr_min: float) -> list[tuple[Sort, list[dict]]]:
        """`planSizeRects`, per tier: groups whose path range meets `[lo, hi)` and whose top size clears the read's
        lowest threshold."""
        return [(s, [g for g in s.groups if self.may_hold(g, D, run) and g["b_max"] >= math.floor(thr_min) and g["p_max"] >= lo and g["p_min"] < hi])
                for s, run in self.tiers("bysize")]

    def subtree(self, D: int, path: str, d_hi: int, thr_at, cost: Cost) -> tuple[pa.Table, str]:
        """Every live row under P at depths `dP+1..d_hi` with `size ≥ thrAt(depth)`, from whichever sort's
        plan holds fewer rows (`planSubtree`)."""
        dP = depth_of(path)
        lo, hi = p_range(path)
        d_lo = dP + 1
        thr_min = min(thr_at(d_lo), thr_at(d_hi if d_hi < 10_000 else d_lo))
        pp = self.plan_path(D, d_lo, d_hi, lo, hi, thr_at)
        sp = self.plan_bysize(D, lo, hi, thr_min)
        held = lambda plan: sum(g["row_end"] - g["row_start"] for _, gs in plan for g in gs)
        name, plan = ("bysize", sp) if held(sp) < held(pp) else ("path", pp)
        t = self.read_tiers(plan, VIEW_COLS, cost, D)
        rows = [r for r in t.to_pylist() if d_lo <= r["depth"] <= d_hi and lo <= r["path"] < hi and r["size"] >= thr_at(r["depth"])]
        return rows, name

    def last_read(self, D: int, paths: list[str], cost: Cost) -> dict[str, int]:
        """`last_read` at D of each path: the reads sort's groups whose range holds one, one decode each."""
        by_depth: dict[int, list[str]] = {}
        for p in paths:
            by_depth.setdefault(depth_of(p), []).append(p)
        gs = {}
        for d, ps in by_depth.items():
            ps.sort()
            for g in self.reads.groups:
                if not live(g, D) or not (g["d_min"] <= d <= g["d_max"]):
                    continue
                i = bisect.bisect_left(ps, g["p_min"])
                if i < len(ps) and ps[i] <= g["p_max"]:
                    gs[g["rg"]] = g
        t = live_rows(self.reads.read([gs[k] for k in sorted(gs)], ["depth", "path", "vf", "vt", "last_read"], cost), D)
        want = set(paths)
        return {p: lr for p, lr in zip(t["path"].to_pylist(), t["last_read"].to_pylist()) if p in want}

    def view(self, D: int, path: str, w: int, h: int, *, min_area: float = MIN_AREA, atten: float = ATTEN, max_depth: int | None = None,
             threshold: float | None = None) -> dict:
        """The plain view of P at D (`buildView`), its read cost, and the sort that served the subtree."""
        cost = Cost()
        dP = depth_of(path)
        if path == "":
            roots = self.point_depth(D, 1, cost)
            root = Agg(kind="dir", nc=len(roots))
            reads = [r["last_read"] for r in roots if r["last_read"] >= 0]
            root.a = max(reads) if reads else None
            for r in roots:
                a = agg_of(r)
                root.b += a.b; root.o += a.o; root.wts += a.wts; root.wb += a.wb
                for key in ("cb", "ub"):
                    for k, v in getattr(a, key).items():
                        getattr(root, key)[k] = getattr(root, key).get(k, 0) + v
        else:
            rs = self.point(D, dP, path, path, cost).to_pylist()
            if not rs:
                return {"tree": None, "cost": cost.total()}
            root = agg_of(rs[0])
        if root.b <= 0:
            return {"tree": None, "cost": cost.total()}
        thr = threshold if threshold is not None else root.b * min_area / (w * h)
        thr_at = lambda d: thr * atten ** max(0, d - dP - 1)
        d_hi = dP + max_depth if max_depth is not None else 10_000
        rows, served = self.subtree(D, path, d_hi, thr_at, cost)
        kept = {r["path"]: agg_of(r) for r in rows}
        if len(kept) > HARD_CAP:
            raise ValueError(f"{len(kept)} tiles over HARD_CAP")
        return {"tree": tree(path, root, kept, thr_at), "threshold": thr, "served": served, "kept": {p: (a.b, a.o, a.kind) for p, a in kept.items()},
                "root": (root.b, root.o), "cost": cost.total()}

    def point_depth(self, D: int, depth: int, cost: Cost) -> list[dict]:
        return self.point(D, depth, "", "￿", cost).to_pylist()

    def lookup(self, D: int, path: str, cost: Cost) -> Agg | None:
        rs = self.point(D, depth_of(path), path, path, cost).to_pylist()
        return agg_of(rs[0]) if rs else None

    def changes(self, D1: int, D2: int, path: str, cost: Cost) -> int:
        """Versions under P that opened or closed in `(D1, D2]` — the diff's changed paths — from `path`
        groups whose open or close stamps can fall in the span: the read a change-only diff would make."""
        lo, hi = p_range(path)
        gs = [g for g in self.path.groups if ((D1 < g["vf_max"] and g["vf_min"] <= D2) or (D1 < g["vt_max"] and g["vt_min"] <= D2))
              and g["p_max"] >= lo and g["p_min"] < hi]
        t = self.path.read(gs, ["depth", "path", "vf", "vt"], cost)
        m = pc.and_(pc.and_(pc.greater_equal(t["path"], lo), pc.less(t["path"], hi)),
                    pc.or_(pc.and_(pc.greater(t["vf"], D1), pc.less_equal(t["vf"], D2)), pc.and_(pc.greater(t["vt"], D1), pc.less_equal(t["vt"], D2))))
        return t.filter(m).num_rows


# ── Diff (both sides' kept tiles at one threshold, one-sided paths looked up) ──


def diff(side_a: dict, side_b: dict, lookup_a, lookup_b) -> list[tuple]:
    """`(path, k, s, a_b, b_b, a_o, b_o)` over the union of both views' tiles: a path tiled on one side
    only is looked up on the other (absent = 0), as the diff walk's point lookups do."""
    ka, kb = side_a["kept"], side_b["kept"]
    out = []
    for p in sorted(set(ka) | set(kb)):
        a = ka.get(p)
        b = kb.get(p)
        if a is None:
            x = lookup_a(p)
            a = (x.b, x.o, x.kind) if x else None
        if b is None:
            x = lookup_b(p)
            b = (x.b, x.o, x.kind) if x else None
        ab, ao = (a[0], a[1]) if a else (0, 0)
        bb, bo = (b[0], b[1]) if b else (0, 0)
        s = "added" if a is None else "removed" if b is None else "changed" if (ab, ao) != (bb, bo) else "unchanged"
        out.append((p, (b or a)[2], s, ab, bb, ao, bo))
    return out
