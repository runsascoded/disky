"""Parity of the interval store against the per-scan path store (specs/interval-store.md §9): for sampled
`(P, D)` (scan ids from the deployment profile's `verify_scans`) the interval reader's view (`interval_read.Store.view`) against the same view computed from the
scan's own per-scan `path` sort — every tile, its bytes, objects, kind, age, last read, classes, owners
and `(other)` — and diffs between sampled dates; with each side's read cost (the interval read as made,
the per-scan read as the Worker would plan it from that scan's footer index).

    dt-cloud interval-store verify -P PROFILE -g GEN [-i TASK -n TASKS]   # Batch: TASKS tasks split the sampled scans
"""
from __future__ import annotations

import json
import math
import os
import sys
from functools import partial
from pathlib import Path
from time import monotonic

import pyarrow.parquet as pq

from . import interval_read as ir
from .static_names import q, read_json

err = partial(print, file=sys.stderr, flush=True)

W, H = 1280, 768

# ── The per-scan reference (independent SQL over the scan's own rows) ──────


def agg_sql(version: int) -> str:
    """Per `(depth, path)`: the reader's `merge` over a path's rows, from the per-scan file's own columns."""
    if version == 2:
        return """count(*) AS n, sum(size)::BIGINT AS b, sum(n_files)::BIGINT AS o, bool_or(kind <> 'file') AS is_dir, max(n_children) AS nc,
            sum(CASE WHEN mtime_mean IS NOT NULL THEN mtime_mean * size ELSE 0 END) AS wts,
            sum(CASE WHEN mtime_mean IS NOT NULL THEN size ELSE 0 END)::BIGINT AS wb, max(last_read) AS a,
            sum(coalesce(sum_storage_class_id_2, 0))::BIGINT AS c2, sum(coalesce(sum_storage_class_id_3, 0))::BIGINT AS c3,
            sum(coalesce(sum_storage_class_id_4, 0))::BIGINT AS c4"""
    return """count(*) AS n, sum(b)::BIGINT AS b, sum(o)::BIGINT AS o, true AS is_dir, NULL::BIGINT AS nc,
        sum(CASE WHEN wb > 0 THEN (wts / wb) * wb ELSE 0 END) AS wts, sum(coalesce(wb, 0))::BIGINT AS wb, max(a) AS a,
        sum(coalesce(c2, 0))::BIGINT AS c2, sum(coalesce(c3, 0))::BIGINT AS c3, sum(coalesce(c4, 0))::BIGINT AS c4"""


def size_col(version: int) -> str:
    return "size" if version == 2 else "b"


class Scan:
    """One scan's per-scan `path` sort, queried in place (local copy) with DuckDB."""

    def __init__(self, con, file: str, version: int, key: str = ""):
        self.con, self.file, self.v, self.key = con, file, version, key
        self.src = f"read_parquet({q(file)})"

    def _aggs(self, where: str, having: str = "") -> list[dict]:
        cur = self.con.execute(f"SELECT depth, path, {agg_sql(self.v)} FROM {self.src} WHERE {where} GROUP BY depth, path {having}")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _same_path_objects(self, paths: list[tuple[int, str]], thr_min: float) -> dict[str, tuple]:
        """Object rows under `thr_min` at paths that are also directories (a key and a prefix of the
        same name): what the pre-aggregation filter dropped from those paths' totals."""
        if not paths:
            return {}
        self.con.execute("CREATE OR REPLACE TEMP TABLE want (depth INTEGER, path VARCHAR)")
        self.con.executemany("INSERT INTO want VALUES (?, ?)", paths)
        return {p: (b, o, wts, wb, c2, c3, c4) for p, b, o, wts, wb, c2, c3, c4 in self.con.execute(
            f"""SELECT s.path, sum(size)::BIGINT, sum(n_files)::BIGINT, sum(CASE WHEN mtime_mean IS NOT NULL THEN mtime_mean * size ELSE 0 END),
                sum(CASE WHEN mtime_mean IS NOT NULL THEN size ELSE 0 END)::BIGINT, sum(coalesce(sum_storage_class_id_2, 0))::BIGINT,
                sum(coalesce(sum_storage_class_id_3, 0))::BIGINT, sum(coalesce(sum_storage_class_id_4, 0))::BIGINT
                FROM {self.src} s JOIN want w ON s.depth = w.depth AND s.path = w.path WHERE s.kind = 'file' AND s.size < {thr_min!r}
                GROUP BY s.path""").fetchall()}

    def _users(self, paths: list[tuple[int, str]]) -> dict[str, dict[str, int]]:
        if not paths:
            return {}
        self.con.execute("CREATE OR REPLACE TEMP TABLE want (depth INTEGER, path VARCHAR)")
        self.con.executemany("INSERT INTO want VALUES (?, ?)", paths)
        out: dict[str, dict[str, int]] = {}
        for p, u, b in self.con.execute(f"""SELECT s.path, s.usr, sum(s.{size_col(self.v)})::BIGINT FROM {self.src} s JOIN want w ON s.depth = w.depth AND s.path = w.path
                WHERE s.usr IS NOT NULL AND s.usr <> '' GROUP BY s.path, s.usr""").fetchall():
            out.setdefault(p, {})[u] = b
        return out

    @staticmethod
    def to_agg(r: dict, ub: dict[str, int]) -> ir.Agg:
        a = ir.Agg(b=r["b"], o=r["o"], kind="dir" if r["is_dir"] else "file", nc=r["nc"], a=r["a"])
        if r["wb"] > 0:
            a.wts, a.wb = r["wts"], r["wb"]
        for k in ("c2", "c3", "c4"):
            if r[k]:
                a.cb[k[1]] = r[k]
        a.ub = ub
        return a

    def lookup(self, path: str) -> ir.Agg | None:
        rs = self._aggs(f"depth = {ir.depth_of(path)} AND path = {q(path)}")
        return self.to_agg(rs[0], self._users([(rs[0]["depth"], path)]).get(path, {})) if rs else None

    def view(self, path: str, w: int = W, h: int = H, *, max_depth: int | None = None, threshold: float | None = None,
             min_area: float = ir.MIN_AREA, atten: float = ir.ATTEN) -> dict:
        dP = ir.depth_of(path)
        if path == "":
            tops = self._aggs("depth = 1")
            users = self._users([(1, r["path"]) for r in tops])
            root = ir.Agg(kind="dir", nc=len(tops))
            reads = [r["a"] for r in tops if r["a"] is not None]
            root.a = max(reads) if reads else None
            for r in tops:
                a = self.to_agg(r, users.get(r["path"], {}))
                root.b += a.b; root.o += a.o; root.wts += a.wts; root.wb += a.wb
                for key in ("cb", "ub"):
                    for k, v in getattr(a, key).items():
                        getattr(root, key)[k] = getattr(root, key).get(k, 0) + v
        else:
            root = self.lookup(path)
            if root is None:
                return {"tree": None}
        if root.b <= 0:
            return {"tree": None}
        thr = threshold if threshold is not None else root.b * min_area / (w * h)
        thr_at = lambda d: thr * atten ** max(0, d - dP - 1)
        lo, hi = ir.p_range(path)
        d_hi = f" AND depth <= {dP + max_depth}" if max_depth is not None else ""
        # The per-depth threshold as SQL: `thr · atten^(depth − dP − 1)` (atten > 0). An object is one row
        # (one owner slice), so objects under the lowest threshold can't be tiles and are skipped before
        # the aggregation; a path that is both an object and a directory gets its small object rows back
        # below (`_same_path_objects`).
        thr_min = min(thr_at(dP + 1), thr_at(dP + max_depth) if max_depth is not None else thr_at(dP + 1))
        skip = f" AND (kind <> 'file' OR size >= {thr_min!r})" if self.v == 2 else ""
        rows = self._aggs(f"depth > {dP}{d_hi} AND path >= {q(lo)} AND path < {q(hi)}{skip}",
                          f"HAVING sum({size_col(self.v)}) >= {thr!r} * pow({atten!r}, greatest(0, depth - {dP} - 1))")
        if skip:
            extra = self._same_path_objects([(r["depth"], r["path"]) for r in rows if r["is_dir"]], thr_min)
            for r in rows:
                if r["path"] in extra:
                    b, o, wts, wb, c2, c3, c4 = extra[r["path"]]
                    r["b"] += b; r["o"] += o; r["wts"] += wts; r["wb"] += wb; r["c2"] += c2; r["c3"] += c3; r["c4"] += c4
        # SQL's float threshold vs the reader's: re-test in Python so the boundary is the same arithmetic.
        rows = [r for r in rows if r["b"] >= thr_at(r["depth"])]
        users = self._users([(r["depth"], r["path"]) for r in rows])
        kept = {r["path"]: self.to_agg(r, users.get(r["path"], {})) for r in rows}
        return {"tree": ir.tree(path, root, kept, thr_at), "threshold": thr, "kept": {p: (a.b, a.o, a.kind) for p, a in kept.items()},
                "root": (root.b, root.o)}

    def root_b(self, path: str) -> int:
        """P's total (`readRootAgg`): its own row's bytes, the root's the sum of the buckets'."""
        sc = size_col(self.v)
        where = "depth = 1" if path == "" else f"depth = {ir.depth_of(path)} AND path = {q(path)}"
        return int(self.con.execute(f"SELECT coalesce(sum({sc}), 0) FROM {self.src} WHERE {where}").fetchone()[0])

    def bysize_tiles(self, path: str, thr: float, atten: float = ir.ATTEN, max_depth: int | None = None) -> dict[str, int]:
        """What the per-scan Worker's `bysize` read keeps (`readSizeRects`): rows — owner slices — with
        `size ≥ thrAt(depth)`, so a tile's bytes are its slices over the threshold only."""
        dP = ir.depth_of(path)
        lo, hi = ir.p_range(path)
        d_hi = f" AND depth <= {dP + max_depth}" if max_depth is not None else ""
        sc = size_col(self.v)
        return {p: b for p, b in self.con.execute(
            f"""SELECT path, sum({sc})::BIGINT FROM {self.src} WHERE depth > {dP}{d_hi} AND path >= {q(lo)} AND path < {q(hi)}
                AND {sc} >= {thr!r} * pow({atten!r}, greatest(0, depth - {dP} - 1)) GROUP BY path""").fetchall()}


# ── The per-scan read the Worker would make (cost only, from the scan's footer index) ──

V2_COLS = ["path", "depth", "usr", "kind", "size", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "last_read",
           "sum_storage_class_id_2", "sum_storage_class_id_3", "sum_storage_class_id_4"]
V1_COLS = ["path", "depth", "usr", "b", "o", "wts", "wb", "c2", "c3", "c4", "a"]


class Footer:
    """A per-scan sort's footer rows (`.groups.parquet`, else `.groups.json`) and its projected bytes."""

    def __init__(self, groups: list[dict], schema: list[dict], floor: int | None, cols: list[str]):
        self.groups, self.floor = groups, floor
        names = [e["name"] for e in schema[1:]]
        self.idx = [names.index(c) for c in cols if c in names]

    @classmethod
    def load(cls, bucket, key_parquet: str, version: int) -> "Footer | None":
        import io

        cols = V2_COLS if version == 2 else V1_COLS
        gp = key_parquet.removesuffix(".parquet") + ".groups.parquet"
        blob = bucket.blob(gp)
        if blob.exists():
            data = blob.download_as_bytes()
            t = pq.read_table(io.BytesIO(data))
            kv = {k.decode(): v.decode() for k, v in (pq.read_metadata(io.BytesIO(data)).metadata or {}).items()}
            return cls(t.to_pylist(), json.loads(kv["schema"]), int(kv["floor_bytes"]) if "floor_bytes" in kv else None, cols)
        gj = bucket.blob(key_parquet.removesuffix(".parquet") + ".groups.json")
        if not gj.exists():
            return None
        doc = json.loads(gj.download_as_bytes())
        fields = ("rg", "d_min", "d_max", "p_min", "p_max", "b_max", "u_min", "u_max", "row_start", "row_end", "rg_json", "b_min")
        return cls([dict(zip(fields, g)) for g in doc["groups"]], doc["schema"], doc.get("floor_bytes"), cols)

    def nbytes(self, g: dict) -> int:
        _, _, cols = json.loads(g["rg_json"])
        return sum(cols[i][1] for i in self.idx)

    def cost(self, gs: list[dict]) -> tuple[int, int, int]:
        return len(gs), sum(g["row_end"] - g["row_start"] for g in gs), sum(self.nbytes(g) for g in gs)


def plan_rects(f: Footer, d_lo: int, d_hi: int, lo: str, hi: str, thr_at) -> list[dict]:
    out = []
    for g in f.groups:
        if g["d_max"] < d_lo or g["d_min"] > d_hi:
            continue
        if g["d_min"] == g["d_max"] and (g["p_max"] < lo or g["p_min"] > hi):
            continue
        if g["b_max"] < thr_at(max(g["d_min"], d_lo)):
            continue
        out.append(g)
    return out


def plan_size(f: Footer, lo: str, hi: str, thr_min: float) -> list[dict]:
    return [g for g in f.groups if g["b_max"] >= math.floor(thr_min) and g["p_max"] >= lo and g["p_min"] < hi]


def perscan_cost(footers: dict[str, Footer], version: int, path: str, thr: float, root_b: int, *, atten: float = ir.ATTEN,
                 max_depth: int | None = None) -> dict:
    """The per-scan Worker's reads for a plain view: P's root row(s), then the subtree from whichever
    store sort plans fewer rows (v2), or the coarsest v1 tier whose floor the threshold clears."""
    dP = ir.depth_of(path)
    lo, hi = ir.p_range(path)
    d_lo, d_hi = dP + 1, (dP + max_depth if max_depth is not None else 10_000)
    thr_at = lambda d: thr * atten ** max(0, d - dP - 1)
    root_d, root_lo, root_hi = (1, "", "￿") if path == "" else (dP, path, path)
    tot = {"groups": 0, "rows": 0, "bytes": 0}

    def add(name: str, gs: list[dict], f: Footer) -> None:
        n, r, b = f.cost(gs)
        tot["groups"] += n; tot["rows"] += r; tot["bytes"] += b
        tot.setdefault("by_sort", {})[name] = [n, r, b]

    if version == 2:
        fp, fs = footers["path"], footers["bysize"]
        add("root", plan_rects(fp, root_d, root_d, root_lo, root_hi, lambda d: 0), fp)
        pp = plan_rects(fp, d_lo, d_hi, lo, hi, thr_at)
        sp = plan_size(fs, lo, hi, min(thr_at(d_lo), thr_at(d_hi if d_hi < 10_000 else d_lo)))
        held = lambda gs: sum(g["row_end"] - g["row_start"] for g in gs)
        name, gs, f = ("bysize", sp, fs) if held(sp) < held(pp) else ("path", pp, fp)
        add(name, gs, f)
        tot["served"] = name
        return tot
    tiers = [(n, footers[n]) for n in ("coarse16", "coarse20", "coarse24") if footers.get(n)]
    root_tier = next(((n, f) for n, f in tiers if plan_rects(f, root_d, root_d, root_lo, root_hi, lambda d: 0)), ("path", footers["path"]))
    add("root", plan_rects(root_tier[1], root_d, root_d, root_lo, root_hi, lambda d: 0), root_tier[1])
    pick = next(((n, f) for n, f in tiers if f.floor is not None and thr >= f.floor), ("path", footers["path"]))
    add(pick[0], plan_rects(pick[1], d_lo, d_hi, lo, hi, thr_at), pick[1])
    tot["served"] = pick[0]
    return tot


# ── Comparison ─────────────────────────────────────────────────────────────


def compare(got: dict | None, want: dict | None, *, with_f: bool) -> list[str]:
    """Differences between two trees, flattened: every tile and `(other)`, every field (`f` only where
    the scan knows its children: a v1 scan's `(other)` count is whatever its read happened to see)."""
    if got is None or want is None:
        return [] if got is None and want is None else [f"tree: {'missing' if got is None else 'present'} vs reference"]
    g, w = ir.flatten(got), ir.flatten(want)
    out = []
    for p in sorted(set(g) | set(w)):
        if p not in g or p not in w:
            out.append(f"{p}: {'missing' if p not in g else 'extra'}")
            continue
        a, b = dict(g[p]), dict(w[p])
        if not with_f:
            a.pop("f", None); b.pop("f", None)
        if a != b:
            out.append(f"{p}: {a} != {b}")
    return out


def cases_for(scan: Scan, date: str) -> list[dict]:
    """The sampled views of one scan: the root (and its first level), two buckets, and dirs at depths
    2–8 picked by a hash of their path and the date (big ones and arbitrary ones), plus a depth-1 band."""
    def pick(where: str, k: int) -> list[str]:
        sc = size_col(scan.v)
        kind = " AND kind = 'dir'" if scan.v == 2 else ""
        return [r[0] for r in scan.con.execute(
            f"SELECT path FROM {scan.src} WHERE {where}{kind} AND {sc} > 0 GROUP BY path ORDER BY hash(path || {q(date)}) LIMIT {k}").fetchall()]

    sc = size_col(scan.v)
    cases = [{"path": ""}, {"path": "", "max_depth": 1}]
    cases += [{"path": p} for p in pick("depth = 1", 2)]
    cases += [{"path": p} for p in pick(f"depth = 2 AND {sc} >= 1e12", 2)]
    cases += [{"path": p} for p in pick(f"depth BETWEEN 3 AND 5 AND {sc} >= 1e10", 2)]
    cases += [{"path": p} for p in pick("depth BETWEEN 3 AND 5", 1)]
    cases += [{"path": p} for p in pick("depth BETWEEN 6 AND 9", 2)]
    big = pick(f"depth = 2 AND {sc} >= 1e12", 1)
    cases += [{"path": p, "max_depth": 1} for p in big]
    return cases


def run_date(store: ir.Store, scan: Scan, date: str, ts: int, prev: tuple[Scan, str, int] | None, out, bucket) -> dict:
    """Every sampled case of one scan (and its diffs against `prev`), one JSON line each; `bucket` holds
    the per-scan footers."""
    summary = {"date": date, "views": 0, "view_eq": 0, "diffs": 0, "diff_eq": 0}
    footers = _footers(bucket, scan.key, scan.v)
    cases = cases_for(scan, date)
    for c in cases:
        t0 = monotonic()
        want = scan.view(c["path"], max_depth=c.get("max_depth"))
        t_ref = monotonic() - t0
        t0 = monotonic()
        got = store.view(ts, c["path"], W, H, max_depth=c.get("max_depth"))
        t_iv = monotonic() - t0
        diffs = compare(got["tree"], want["tree"], with_f=scan.v == 2 or c["path"] == "")
        rec = {"kind": "view", "date": date, **c, "eq": not diffs, "diffs": diffs[:20], "n_diffs": len(diffs),
               "tiles": len(want.get("kept") or {}), "threshold": want.get("threshold"),
               "iv_cost": got["cost"], "iv_served": got.get("served"), "t_ref": round(t_ref, 2), "t_iv": round(t_iv, 2)}
        if want.get("tree") is not None:
            rec["ps_cost"] = perscan_cost(footers, scan.v, c["path"], want["threshold"], want["root"][0], max_depth=c.get("max_depth"))
            if scan.v == 2:
                bys = scan.bysize_tiles(c["path"], want["threshold"], max_depth=c.get("max_depth"))
                ex = {p: v[0] for p, v in want["kept"].items()}
                rec["ps_bysize_off"] = {"tiles_missing": sum(1 for p in ex if p not in bys), "tiles_extra": sum(1 for p in bys if p not in ex),
                                        "bytes_differ": sum(1 for p in ex if p in bys and bys[p] != ex[p]),
                                        "bytes_short": sum(ex[p] - bys.get(p, 0) for p in ex)}
        out.write(json.dumps(rec) + "\n")
        out.flush()
        summary["views"] += 1
        summary["view_eq"] += rec["eq"]
        err(f"  {date} {c}: eq={rec['eq']} tiles={rec['tiles']} iv={got['cost']['groups']}g/{got['cost']['rows']}r ref {t_ref:.1f}s iv {t_iv:.1f}s"
            + (f" — {diffs[:3]}" if diffs else ""))
    if prev:
        pscan, pdate, pts = prev
        if pscan.v == scan.v:
            for c in [x for x in cases if "max_depth" not in x][:4]:
                path = c["path"]
                ra, rb = pscan.root_b(path), scan.root_b(path)
                if ra <= 0 or rb <= 0:
                    continue
                # `buildDiff`: one floor for both sides, from the larger root.
                thr = max(ra, rb) * ir.MIN_AREA / (W * H)
                wa, wb = pscan.view(path, threshold=thr), scan.view(path, threshold=thr)
                want = ir.diff(wa, wb, pscan.lookup, scan.lookup)
                ga, gb = store.view(pts, path, W, H, threshold=thr), store.view(ts, path, W, H, threshold=thr)
                cost = ir.Cost()
                got = ir.diff(ga, gb, lambda p: store.lookup(pts, p, cost), lambda p: store.lookup(ts, p, cost))
                ch_cost = ir.Cost()
                n_changed = store.changes(pts, ts, path, ch_cost)
                eq = got == want
                rec = {"kind": "diff", "from": pdate, "to": date, "path": path, "eq": eq, "rows": len(want),
                       "changed": sum(1 for r in want if r[2] != "unchanged"),
                       "mismatch": [list(x) for x in sorted(set(got) ^ set(want))][:20],
                       "iv_cost": {"views": [ga["cost"], gb["cost"]], "lookups": cost.total()},
                       "changes": {"versions": n_changed, "cost": ch_cost.total()}}
                out.write(json.dumps(rec) + "\n")
                out.flush()
                summary["diffs"] += 1
                summary["diff_eq"] += eq
                err(f"  diff {pdate}→{date} {path!r}: eq={eq} rows={len(want)} changed={rec['changed']}")
    return summary


def _footers(bucket, key: str, version: int) -> dict[str, Footer]:
    d = key.rsplit("/", 1)[0]
    if version == 2:
        return {"path": Footer.load(bucket, key, 2), "bysize": Footer.load(bucket, f"{d}/path-index-bysize.parquet", 2)}
    out = {"path": Footer.load(bucket, key, 1)}
    for e in (16, 20, 24):
        out[f"coarse{e}"] = Footer.load(bucket, f"{d}/path-index-coarse{e}.parquet", 1)
    return out


def verify_task(sampled: list[str], task: int, tasks: int, served: str, scans: dict, out_path: Path, tmp: Path, mount: str | None) -> list[dict]:
    """This task's share of the `sampled` scan ids (contiguous, so each diff's previous scan is local),
    each scan's per-scan `path` sort copied to local disk first."""
    import duckdb
    from google.cloud import storage

    by_id = {s["id"]: s for s in scans["scans"]}
    per = math.ceil(len(sampled) / tasks)
    mine = sampled[max(0, task * per - 1):(task + 1) * per]  # the one before this task's share: its diffs' `from`
    first_own = sampled[task * per] if task * per < len(sampled) else None
    store = ir.Store(served)
    con = duckdb.connect()
    con.execute(f"SET threads=16; SET memory_limit='80GB'; SET temp_directory={q(str(tmp / 'spill'))}")
    bucket = storage.Client().bucket(scans["bucket"])
    prev = None
    summaries = []
    with out_path.open("w") as out:
        for date in mine:
            s = by_id[date]
            local = tmp / f"{date}.parquet"
            t0 = monotonic()
            if mount:
                import shutil

                shutil.copyfile(f"{mount}/{s['src']}", local)
            else:
                bucket.blob(s["src"]).download_to_filename(str(local))
            err(f"{date}: per-scan path sort copied ({local.stat().st_size:,} B, {monotonic() - t0:.0f}s)")
            scan = Scan(con, str(local), s["version"], s["src"])
            if date == first_own or (first_own and sampled.index(date) > sampled.index(first_own)):
                summaries.append(run_date(store, scan, date, s["ts"], prev, out, bucket))
            if prev:
                os.unlink(prev[0].file)
            prev = (scan, date, s["ts"])
    return summaries
