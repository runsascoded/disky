"""Ground truth for the filter bench (specs/done/filter-query-service.md §6 phase 1):
each query's exact match roots and net totals under each view root, computed
from a store generation's `path` sort.

Two methods, which must agree (`--check`):

- **names-first** (phase 0's method, specs/done/filter-query-service-p0.md §A.2):
  the query's candidate last segments (`query.plan_positive` /
  `plan_negative`, the search index's lemma) are matched against the v1
  vocabulary (`path-index.names.parquet`), mapped to `path`-sort row groups by
  its `rgs`, and only those rows are read;
- **scan**: every row of the `path` sort, testing the full-path predicate. For a
  monotone query (no regex) only the rows where a half of the predicate
  *becomes* true are kept (it holds there but not at the parent), which are
  exactly the outermost ones; a regex keeps every row it matches. A query the
  index can't plan (a term ending in `/`, a regex) is only answerable this way.

Either way the rows found (summed over owner slices) are **candidates**:
paths with their full-path `pos` / `neg` flags. `view_truth` turns them into
one view's answer with the Worker's semantics (`site/functions/_lib/view.ts`,
specs/path-store-search.md §1): the match roots strictly under the view root
(or the root alone, when the query matches it), the outermost excluded paths
under them, and each root's total net of its excluded paths.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .query import Ast, Branch, Pred, compile_query, lit, monotone, neg_sql, plan_negative, plan_positive, plan_sql, pos_everywhere, pos_sql

LIST_MAX = 50_000  # roots / excluded paths listed per view (above: count + md5 only)
NAME = "regexp_extract(path, '[^/]*$')"


def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Cand:
    """A path the query's halves may start at: totals over owner slices, and
    whether `pos` / `neg` hold on its full path."""

    path: str
    b: int
    o: int
    pos: bool
    neg: bool


def parent(p: str) -> str:
    i = p.rfind("/")
    return "" if i < 0 else p[:i]


def under(p: str, view: str) -> bool:
    """Strictly under the view root ('' = the store root)."""
    return p != view and (view == "" or p.startswith(view + "/"))


def has_ancestor(p: str, s: set[str], view: str) -> bool:
    """Some strict ancestor of `p` strictly under `view` is in `s`
    (`filter.ts` `matchRoots`' inner walk)."""
    q = parent(p)
    while len(q) > len(view):
        if q in s:
            return True
        q = parent(q)
    return False


def md5_paths(paths: list[str]) -> str:
    return hashlib.md5("\n".join(sorted(paths)).encode()).hexdigest()


@dataclass
class ViewTruth:
    view: str
    root_hit: bool  # the query matches the view root: it is the only match root
    roots: list[tuple[str, int, int]]  # (path, net bytes, net objects), by path
    excluded: list[tuple[str, int, int]]  # (path, bytes, objects), by path

    @property
    def bytes(self) -> int:
        return sum(r[1] for r in self.roots)

    @property
    def objects(self) -> int:
        return sum(r[2] for r in self.roots)

    def summary(self) -> dict:
        return {
            "view": self.view,
            "root_hit": self.root_hit,
            "roots": len(self.roots),
            "md5": md5_paths([r[0] for r in self.roots]),
            "bytes": self.bytes,
            "objects": self.objects,
            "excluded": len(self.excluded),
            "excluded_md5": md5_paths([e[0] for e in self.excluded]),
        }

    def to_json(self, list_max: int = LIST_MAX) -> dict:
        return {
            **self.summary(),
            "list": [list(r) for r in self.roots] if len(self.roots) <= list_max else None,
            "excluded_list": [list(e) for e in self.excluded] if len(self.excluded) <= list_max else None,
        }


def view_truth(cands: list[Cand], view: str, pred: Pred, view_tot: tuple[int, int]) -> ViewTruth:
    """One view's answer from the query's candidates. `view_tot` is the view
    root's own (bytes, objects), used when the query matches it."""
    u = [c for c in cands if under(c.path, view)]
    hit = pred(view)
    if hit:
        roots = {view: view_tot}
    else:
        s = {c.path for c in u if c.pos and not c.neg}
        roots = {c.path: (c.b, c.o) for c in u if c.path in s and not has_ancestor(c.path, s, view)}

    def root_for(e: str) -> str | None:
        """The match root `e` is strictly under (`view.ts` `rootFor`)."""
        if e == view:
            return None
        q = parent(e)
        while len(q) >= len(view):
            if q in roots:
                return q
            if q == "":
                return None
            q = parent(q)
        return None

    n = {c.path for c in u if c.neg}
    cut: dict[str, list[int]] = {}
    excluded = []
    for c in u:
        if not c.neg or has_ancestor(c.path, n, view):
            continue
        r = root_for(c.path)
        if r is None:
            continue
        excluded.append((c.path, c.b, c.o))
        acc = cut.setdefault(r, [0, 0])
        acc[0] += c.b
        acc[1] += c.o
    net = [(p, b - cut.get(p, [0, 0])[0], o - cut.get(p, [0, 0])[1]) for p, (b, o) in roots.items()]
    return ViewTruth(view, hit, sorted(net), sorted(excluded))


# --- DuckDB: candidates ------------------------------------------------------------


def connect(threads: int = 16, mem: str = "100GB", tmp: str | None = None):
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET threads = {threads}")
    con.execute(f"SET memory_limit = '{mem}'")
    if tmp:
        con.execute(f"SET temp_directory = '{tmp}'")
    con.execute("SET preserve_insertion_order = false")
    return con


def trivial(b: Branch) -> bool:
    """A branch every name passes (`tmp/*`: an anchored, empty-literal glob):
    names-first would read everything."""
    return b.op == "regex" and not b.arg.removeprefix("^").replace("[^/]*", "")


def names_first_ok(ast: Ast) -> bool:
    """Whether the index's lemma bounds this query's candidates: every
    positive term plannable (or `pos` everywhere true), every negative too."""
    pp = None if pos_everywhere(ast) else plan_positive(ast)
    if not pos_everywhere(ast) and pp is None:
        return False
    np_ = plan_negative(ast) if ast.neg else []
    if ast.neg and np_ is None:
        return False
    return not any(trivial(b) for b in [*(pp or []), *(np_ or [])])


def load_vocab(con, names_file: str) -> int:
    t0 = time.monotonic()
    con.execute(f"CREATE OR REPLACE TABLE vocab AS SELECT name, lower(name) AS l, rgs FROM read_parquet({lit(names_file)})")
    n = con.execute("SELECT count(*) FROM vocab").fetchone()[0]
    err(f"vocab: {n} names in {time.monotonic() - t0:.1f}s")
    return n


def _cands(rows: list[tuple]) -> list[Cand]:
    return [Cand(p, int(b), int(o), bool(ps), bool(ng)) for p, b, o, ps, ng in rows]


def names_first(con, path_file: str, ast: Ast, rg_batch: int = 512, scan_frac: float = 0.3) -> tuple[list[Cand], dict]:
    """Candidates via the vocabulary (`load_vocab` first): names passing the
    plan → their `rgs` → those row groups' rows with a candidate name. Falls
    back to scanning the file (name-filtered) when a name has no `rgs` (over
    the writer's cap) or the groups are over `scan_frac` of the file."""
    import pyarrow.parquet as pq

    t0 = time.monotonic()
    pp = None if pos_everywhere(ast) else plan_positive(ast)
    np_ = plan_negative(ast) if ast.neg else None
    cond = f"{plan_sql(pp)} OR {plan_sql(np_)}"
    con.execute(f"CREATE OR REPLACE TEMP TABLE cn AS SELECT name, rgs FROM vocab WHERE {cond}")
    n_names, n_null = con.execute("SELECT count(*), count(*) FILTER (rgs IS NULL) FROM cn").fetchone()
    rgs = sorted({int(x) for (r,) in con.execute("SELECT rgs FROM cn WHERE rgs IS NOT NULL").fetchall() for x in r.split(",")})
    pf = pq.ParquetFile(path_file)
    n_rg = pf.metadata.num_row_groups
    flags = f"{pos_sql(ast)} AS p, {neg_sql(ast)} AS n"
    select = f"""SELECT path, size, n_files, {flags} FROM (
        SELECT path, size, n_files, lower(path) AS lp FROM {{src}} WHERE {NAME} IN (SELECT name FROM cn))"""
    con.execute("CREATE OR REPLACE TEMP TABLE cr (path VARCHAR, size BIGINT, n_files BIGINT, p BOOLEAN, n BOOLEAN)")
    how = "rgs"
    if n_null or len(rgs) > scan_frac * n_rg:
        how = "scan"
        con.execute(f"INSERT INTO cr SELECT * FROM ({select.format(src=f'read_parquet({lit(path_file)})')}) WHERE p OR n")
    else:
        for i in range(0, len(rgs), rg_batch):
            tbl = pf.read_row_groups(rgs[i : i + rg_batch], columns=["path", "size", "n_files"], use_threads=True)
            con.register("rg_rows", tbl)
            con.execute(f"INSERT INTO cr SELECT * FROM ({select.format(src='rg_rows')}) WHERE p OR n")
            con.unregister("rg_rows")
    rows = con.execute("SELECT path, sum(size), sum(n_files), any_value(p), any_value(n) FROM cr GROUP BY path").fetchall()
    stats = {"method": "names-first", "map": how, "names": n_names, "null_rgs": n_null, "rgs": len(rgs), "rgs_total": n_rg, "cands": len(rows), "s": round(time.monotonic() - t0, 2)}
    return _cands(rows), stats


def scan(con, path_file: str, asts: dict[str, Ast]) -> tuple[dict[str, list[Cand]], dict]:
    """Candidates of several queries in one pass over the `path` sort. A
    monotone query keeps the rows where `pos` or `neg` becomes true (holds
    there, not at the parent); a regex query keeps every row `pos` holds on."""
    if not asts:
        return {}, {}
    t0 = time.monotonic()
    ids = list(asts)
    cols, keep = [], []
    for i, k in enumerate(ids):
        a = asts[k]
        cols += [f"{pos_sql(a)} AS p{i}", f"{neg_sql(a)} AS n{i}"]
        if monotone(a):
            par = pos_sql(a, "NULL", "lpar"), neg_sql(a, "NULL", "lpar")
            cols += [f"{par[0]} AS pp{i}", f"{par[1]} AS np{i}"]
            keep.append(f"(p{i} AND NOT pp{i}) OR (n{i} AND NOT np{i})")
        else:
            keep.append(f"p{i}")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE sc AS SELECT * FROM (
        SELECT path, size, n_files, {', '.join(cols)} FROM (
            SELECT path, size, n_files, lp, regexp_extract(lp, '^(.*)/', 1) AS lpar FROM (
                SELECT path, size, n_files, lower(path) AS lp FROM read_parquet({lit(path_file)})))
    ) WHERE {' OR '.join(f'({k})' for k in keep)}""")
    out = {}
    for i, k in enumerate(ids):
        rows = con.execute(f"SELECT path, sum(size), sum(n_files), any_value(p{i}), any_value(n{i}) FROM sc WHERE {keep[i]} GROUP BY path").fetchall()
        out[k] = _cands(rows)
    return out, {"method": "scan", "queries": ids, "s": round(time.monotonic() - t0, 2)}


def view_totals(con, path_file: str, views: list[str]) -> dict[str, tuple[int, int]]:
    """Each view root's own (bytes, objects); '' = Σ over the buckets."""
    out = {}
    src = f"read_parquet({lit(path_file)})"
    for v in views:
        where = "depth = 1" if v == "" else f"path = {lit(v)}"
        b, o = con.execute(f"SELECT coalesce(sum(size), 0), coalesce(sum(n_files), 0) FROM {src} WHERE {where}").fetchone()
        out[v] = (int(b), int(o))
    return out


# --- the job -------------------------------------------------------------------------


@dataclass
class QueryTruth:
    id: str
    q: str
    qs: str
    ast: Ast
    stats: dict
    views: list[ViewTruth]
    check: dict | None = field(default=None)

    def to_json(self, list_max: int = LIST_MAX) -> dict:
        return {"id": self.id, "q": self.q, "qs": self.qs, "ast": self.ast.to_json(), "stats": self.stats, "views": [v.to_json(list_max) for v in self.views], **({"check": self.check} if self.check else {})}

    def summary(self) -> dict:
        return {"id": self.id, "q": self.q, "qs": self.qs, "method": self.stats.get("method"), "views": [v.summary() for v in self.views], **({"check": self.check} if self.check else {})}


def compute(
    cases: list,
    path_file: str,
    names_file: str | None,
    check: list[str],
    con,
) -> list[QueryTruth]:
    """Every case's truth under each of its views (`queryset.Case`s); the
    cases in `check` are also computed by `scan` and compared."""
    from .query import parse

    asts = {c.id: parse(c.q, c.qs) for c in cases}
    for c in cases:
        if asts[c.id] is None:
            raise ValueError(f"{c.id}: {c.q!r} is not a filter")
    views = sorted({v for c in cases for v in c.views})
    tots = view_totals(con, path_file, views)
    nf = {c.id for c in cases if names_file and names_first_ok(asts[c.id])}
    scanned, sstats = scan(con, path_file, {c.id: asts[c.id] for c in cases if c.id not in nf or c.id in check})
    err(f"scan: {sstats}")
    if nf:
        load_vocab(con, names_file)
    out = []
    for c in cases:
        ast = asts[c.id]
        pred = compile_query(ast)
        if c.id in nf:
            cands, stats = names_first(con, path_file, ast)
        else:
            cands, stats = scanned[c.id], {"method": "scan", "cands": len(scanned[c.id]), "scan_s": sstats.get("s")}
        t0 = time.monotonic()
        vts = [view_truth(cands, v, pred, tots[v]) for v in c.views]
        stats["views_s"] = round(time.monotonic() - t0, 2)
        chk = None
        if c.id in check and c.id in nf:
            alt = [view_truth(scanned[c.id], v, pred, tots[v]) for v in c.views]
            same = [a.summary() == b.summary() for a, b in zip(vts, alt)]
            chk = {"method": "scan", "identical": all(same), "views": [a.summary() for a in alt]}
        qt = QueryTruth(c.id, c.q, c.qs, ast, stats, vts, chk)
        err(json.dumps(qt.summary()))
        out.append(qt)
    return out


def write(truths: list[QueryTruth], out: str, meta: dict, append: bool = False) -> None:
    """`<out>/<id>.json` per query (lists included) and `<out>/summary.json`
    (counts, md5s, totals). `out` is a local dir or a `gs://` prefix. With
    `append`, an existing summary keeps its other queries (and its meta, with
    this run's under `appended`); these replace any with the same id."""
    import fsspec

    base = out.rstrip("/")
    for t in truths:
        with fsspec.open(f"{base}/{t.id}.json", "w") as f:
            json.dump(t.to_json(), f)
    queries = [t.summary() for t in truths]
    fs, path = fsspec.core.url_to_fs(f"{base}/summary.json")
    if append and fs.exists(path):
        with fs.open(path, "r") as f:
            old = json.load(f)
        ids = {q["id"] for q in queries}
        queries = [q for q in old.pop("queries") if q["id"] not in ids] + queries
        meta = {**old, "appended": [*old.get("appended", []), meta]}
    with fsspec.open(f"{base}/summary.json", "w") as f:
        json.dump({**meta, "queries": queries}, f, indent=1)


def local_or_download(uri: str, dst: Path) -> str:
    """A `gs://` file copied to `dst` (parallel chunked GETs), else as is."""
    if not uri.startswith("gs://"):
        return uri
    from google.cloud import storage
    from google.cloud.storage import transfer_manager as tm

    bucket, _, key = uri[5:].partition("/")
    blob = storage.Client().bucket(bucket).blob(key)
    blob.reload()
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    tm.download_chunks_concurrently(blob, str(dst), chunk_size=64 << 20, max_workers=32)
    err(f"downloaded {uri} ({blob.size} B) in {time.monotonic() - t0:.1f}s")
    return str(dst)
