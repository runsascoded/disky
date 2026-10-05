"""Churn between scans — the measurement behind keyframe + delta storage
(specs/storage-consolidation.md phase 3).

`scan_churn` compares two scans' path-store `path` sorts (or layer-2s): every
row is keyed by `(depth, path)` (plus the owner label column, when both files
carry one) and classified `added` / `removed` / `changed` / `same` per `kind`,
with per-column change counts. Optionally it writes the **delta** — B's rows
that were added or changed, plus key-only tombstones for removed rows, an `op`
column (`a` / `c` / `d`), sorted by key under the store's codec — and reports
its bytes, alone and for object rows only (dirs re-derivable from a keyframe +
the object delta).

`group_churn` reads the same numbers off a sealed over-time group (dir rows
only, `(b, o)` per path per scan as SCD-2 intervals): per scan boundary, the
paths whose interval ended and the next began (changed), began with no
predecessor (added), or left (removed).

Both run in DuckDB. `scan_churn` joins one `depth` at a time (both inputs are
`depth`-led sorts, so each slice is a row-group-pruned read), which bounds the
join to the largest depth slice; pass a connection with `memory_limit` /
`temp_directory` set for big inputs.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

#: Owner/label columns that, when both inputs carry one, are part of a row's key
#: (a pre-store index slices a dir by owner; a store generation's label sorts).
LABEL_COLUMNS = ("usr", "user")
KEY_COLUMNS = ("depth", "path")
OPS = {"a": "added", "d": "removed", "c": "changed", "s": "same"}


def _q(s: str | Path) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _columns(con: duckdb.DuckDBPyConnection, path: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({_q(path)}) LIMIT 0").fetchall()]


def _codec() -> str:
    from disk_tree.listing_format import duckdb_codec
    return duckdb_codec()


def scan_churn(
    a: str | Path,
    b: str | Path,
    *,
    out_dir: str | Path | None = None,
    columns: list[str] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict:
    """Rows of ``b`` vs ``a``, keyed `(depth, path[, label])`.

    ``columns``: the value columns compared (default: every non-key column both
    files share, `kind` included as the row's type, not compared). ``out_dir``:
    also write `delta.parquet` (all kinds) and `delta-objects.parquet` (`kind !=
    'dir'`) there. Returns::

        {"a_rows", "b_rows", "key": [...], "columns": [...],
         "by_kind": {kind: {"added", "removed", "changed", "same"}},
         "changed_columns": {kind: {col: n}},
         "delta": {"rows", "bytes", "objects_rows", "objects_bytes"}}   # with out_dir
    """
    a, b = str(a), str(b)
    con = con or duckdb.connect()
    ca, cb = _columns(con, a), _columns(con, b)
    for k in KEY_COLUMNS:
        if k not in ca or k not in cb:
            raise ValueError(f"scan_churn: key column {k!r} missing (a: {ca}, b: {cb})")
    labels = [c for c in LABEL_COLUMNS if c in ca and c in cb]
    key = [*KEY_COLUMNS, *labels]
    shared = [c for c in cb if c in ca and c not in key]
    cols = columns if columns is not None else shared
    missing = [c for c in cols if c not in shared]
    if missing:
        raise ValueError(f"scan_churn: columns {missing} not in both files")
    has_kind = "kind" in shared
    vcols = [c for c in cols if c != "kind"]
    on = " AND ".join(f"a.{_ident(k)} IS NOT DISTINCT FROM b.{_ident(k)}" for k in key)
    chg = {c: f"(a.{_ident(c)} IS DISTINCT FROM b.{_ident(c)})" for c in vcols}
    any_chg = " OR ".join(chg.values()) or "FALSE"
    kind = "coalesce(b.kind, a.kind)" if has_kind else "NULL"
    joined = (
        "SELECT "
        + ", ".join(f"coalesce(b.{_ident(k)}, a.{_ident(k)}) AS {_ident(k)}" for k in key)
        + f", {kind} AS kind, "
        + "CASE WHEN a.path IS NULL THEN 'a' WHEN b.path IS NULL THEN 'd' "
        + f"WHEN {any_chg} THEN 'c' ELSE 's' END AS op"
        + "".join(f", {e} AS {_ident('chg_' + c)}" for c, e in chg.items())
        + " FROM (SELECT * FROM read_parquet({a}) WHERE depth = {d}) a"
        + " FULL OUTER JOIN (SELECT * FROM read_parquet({b}) WHERE depth = {d}) b ON " + on
    )
    depths = [r[0] for r in con.execute(
        f"SELECT DISTINCT depth FROM (SELECT depth FROM read_parquet({_q(a)}) UNION ALL "
        f"SELECT depth FROM read_parquet({_q(b)})) ORDER BY depth"
    ).fetchall()]
    if not depths:
        raise ValueError(f"scan_churn: no rows in {a} or {b}")
    by_kind: dict[str, dict[str, int]] = {}
    changed_columns: dict[str, dict[str, int]] = {}
    keep = ", ".join(_ident(k) for k in key) + ", kind, op"
    for i, d in enumerate(depths):
        sql = joined.format(a=_q(a), b=_q(b), d=int(d))
        con.execute(f"CREATE OR REPLACE TEMP TABLE churn_slice AS {sql}")
        for k, op, n in con.execute("SELECT kind, op, count(*) FROM churn_slice GROUP BY ALL").fetchall():
            row = by_kind.setdefault(str(k), {v: 0 for v in OPS.values()})
            row[OPS[op]] += n
        if vcols:
            sums = ", ".join(f"count(*) FILTER (WHERE {_ident('chg_' + c)})" for c in vcols)
            for r in con.execute(f"SELECT kind, {sums} FROM churn_slice WHERE op = 'c' GROUP BY kind").fetchall():
                acc = changed_columns.setdefault(str(r[0]), {c: 0 for c in vcols})
                for c, n in zip(vcols, r[1:]):
                    acc[c] += n
        into = "INSERT INTO churn_j" if i else "CREATE OR REPLACE TEMP TABLE churn_j AS"
        con.execute(f"{into} SELECT {keep} FROM churn_slice WHERE op <> 's'")
    con.execute("DROP TABLE IF EXISTS churn_slice")
    out: dict = {
        "a_rows": con.execute(f"SELECT count(*) FROM read_parquet({_q(a)})").fetchone()[0],
        "b_rows": con.execute(f"SELECT count(*) FROM read_parquet({_q(b)})").fetchone()[0],
        "key": key,
        "columns": cols,
        "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
        "changed_columns": {k: changed_columns[k] for k in sorted(changed_columns)},
    }
    if out_dir is not None:
        out["delta"] = _write_delta(con, b, key, out_dir)
    con.execute("DROP TABLE IF EXISTS churn_j")
    return out


def _write_delta(con: duckdb.DuckDBPyConnection, b: str, key: list[str], out_dir: str | Path) -> dict:
    """B's added/changed rows + removed keys (tombstones), `op` first, sorted by key."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    on = " AND ".join(f"j.{_ident(k)} IS NOT DISTINCT FROM b.{_ident(k)}" for k in key)
    keys = ", ".join(f"j.{_ident(k)}" for k in key)
    order = ", ".join(_ident(k) for k in key)
    sel = (
        f"SELECT j.op, b.* FROM churn_j j JOIN read_parquet({_q(b)}) b ON {on} WHERE j.op IN ('a', 'c') "
        f"UNION ALL BY NAME SELECT j.op, {keys}, j.kind FROM churn_j j WHERE j.op = 'd'"
    )
    res: dict[str, int] = {}
    for name, where, pre in (
        ("delta.parquet", "", ""),
        ("delta-objects.parquet", " WHERE kind IS DISTINCT FROM 'dir'", "objects_"),
    ):
        path = out / name
        con.execute(f"COPY (SELECT * FROM ({sel}){where} ORDER BY {order}) TO {_q(path)} (FORMAT parquet, {_codec()})")
        res[f"{pre}rows"] = con.execute(f"SELECT count(*) FROM read_parquet({_q(path)})").fetchone()[0]
        res[f"{pre}bytes"] = os.path.getsize(path)
    return res


def group_churn(over_time: str | Path, *, con: duckdb.DuckDBPyConnection | None = None) -> dict:
    """Per-boundary dir churn of one sealed over-time group (`(depth, path, b, o,
    __scan_lo, __scan_hi)`, `__scan_hi` inclusive). Boundary `s` is scan s-1 → s.
    Returns ``{"rows", "paths", "scans", "per_scan": [{"scan", "present",
    "changed", "added", "removed"}, …]}``."""
    from .overtime import SCAN_HI, SCAN_LO

    con = con or duckdb.connect()
    src = f"read_parquet({_q(over_time)})"
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE ot_c AS SELECT {SCAN_LO} AS lo, {SCAN_HI} AS hi, "
        f"lag({SCAN_HI}) OVER (PARTITION BY depth, path ORDER BY {SCAN_LO}) AS prev_hi FROM {src}"
    )
    rows, paths = con.execute(f"SELECT count(*), count(DISTINCT (depth, path)) FROM {src}").fetchone()
    n = con.execute("SELECT coalesce(max(hi) + 1, 0) FROM ot_c").fetchone()[0]
    changed = dict(con.execute("SELECT lo, count(*) FROM ot_c WHERE lo > 0 AND prev_hi = lo - 1 GROUP BY lo").fetchall())
    added = dict(con.execute(
        "SELECT lo, count(*) FROM ot_c WHERE lo > 0 AND (prev_hi IS NULL OR prev_hi < lo - 1) GROUP BY lo"
    ).fetchall())
    present = dict(con.execute("SELECT s, count(*) FROM ot_c, range(lo, hi + 1) r(s) GROUP BY s").fetchall())
    con.execute("DROP TABLE ot_c")
    per_scan = [
        {
            "scan": s,
            "present": present.get(s, 0),
            "changed": changed.get(s, 0),
            "added": added.get(s, 0),
            "removed": present.get(s - 1, 0) + added.get(s, 0) - present.get(s, 0),
        }
        for s in range(1, n)
    ]
    return {"rows": rows, "paths": paths, "scans": n, "per_scan": per_scan}
