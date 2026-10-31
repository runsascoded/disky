"""Transitions (specs/diff-from-changes.md §1–2): one row per path per scan at which its diff columns changed, both
sides on the row, for a Δ-mode diff that reads only what changed between two scans.

A version opening at `b` (`vf = b`) is the transition's new side, a version closing at `b` (`vt = b`) its old side:
joined on `(depth, path, b)`, a side missing where the path appeared (no close) or went away (no open). A version open
at the generation's first scan is no transition, nor is a close at `OPEN`. Transitions whose two sides agree on every
diff column (a change in `mtime`, `n_desc`, owners, read day … only) are dropped: their Δ is zero in everything a
diff draws, and the ones kept still chain (each kept one's old side is the previous kept one's new side, in those
columns).

A scan that first lists objects after scans that listed directories only (a v1 index's successor, `scans.json`
`version`) opens a version for every object that already existed: those openings are the format's, not changes (their
bytes were already in their directories' sizes), so a file appearing at such a scan is no transition (`listed_from`).
Its directories' transitions there stay, and its own later ones too (a file deleted after it reads as deleted).

Sorted `(b, ⌊log2 max(size, size_prev)⌋ desc, path)`, size 0 last: a range read takes a block of `b`, and inside it a
size floor and a path range prune as `bysize` does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
from click import IntRange, command, option

from .interval_store import CLS
from .static_names import OPEN, connect, err

#: The columns a diff draws: what a transition carries on both sides.
DIFF_COLS = ["kind", "size", "n_files", *CLS]
#: Rows per row group (`SERVED_RG`'s size, the served sorts').
RG_ROWS = 8192


def listed_from(scans: list[dict]) -> list[int]:
    """The epochs (`ts`) of the scans that first list objects after scans that didn't (`version` 1: dirs only)."""
    return [s["ts"] for p, s in zip(scans, scans[1:]) if p.get("version", 1) == 1 and s.get("version", 1) >= 2]


def transitions_sql(src: str, first: int, objects_from: list[int] = ()) -> str:
    """Transitions of the versions in relation `src` (`depth, path, vf, vt` + `DIFF_COLS`); `first` = the
    generation's first scan's epoch; `objects_from` = `listed_from`'s epochs, where files appearing are no transition."""
    unlisted = f" OR (c.path IS NULL AND o.kind = 'file' AND b IN ({', '.join(map(str, objects_from))}))" if objects_from else ""
    new = ", ".join(f"o.{c}" for c in DIFF_COLS[1:])
    old = ", ".join(f"c.{c} AS {c}_prev" for c in DIFF_COLS[1:])
    same = " AND ".join(f"o.{c} = c.{c}" for c in DIFF_COLS)
    return f"""
        WITH v AS (SELECT depth, path, vf, vt, {", ".join(DIFF_COLS)} FROM {src}),
        o AS (SELECT depth, path, vf AS b, {", ".join(DIFF_COLS)} FROM v WHERE vf > {first}),
        c AS (SELECT depth, path, vt AS b, {", ".join(DIFF_COLS)} FROM v WHERE vt < {OPEN})
        SELECT depth, path, b, coalesce(o.kind, c.kind) AS kind, {new}, {old}
        FROM o FULL OUTER JOIN c USING (depth, path, b)
        WHERE NOT ((o.path IS NOT NULL AND c.path IS NOT NULL AND {same}){unlisted})
    """


def order_sql() -> str:
    """The `changes` sort's order: scan, then size bucket descending (size 0 last), then path."""
    top = "greatest(coalesce(size, 0), coalesce(size_prev, 0))"
    return f"b, CASE WHEN {top} > 0 THEN floor(log2({top}))::INT ELSE -1 END DESC, path"


def cut_changes(con, src: str, out: Path, first: int, objects_from: list[int] = (), *, rg_rows: int = RG_ROWS) -> dict:
    """Write `src`'s transitions to `out` in the `changes` order (zstd, `rg_rows`-row groups); their counts."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE t AS {transitions_sql(src, first, objects_from)}")
    rows, opens, closes, both = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE size IS NOT NULL), count(*) FILTER (WHERE size_prev IS NOT NULL),"
        " count(*) FILTER (WHERE size IS NOT NULL AND size_prev IS NOT NULL) FROM t").fetchone()
    con.execute(f"COPY (SELECT * FROM t ORDER BY {order_sql()}) TO '{out}' "
                f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {rg_rows})")
    con.execute("DROP TABLE t")
    return {"rows": rows, "appeared": rows - closes, "went": rows - opens, "changed": both,
            "bytes": out.stat().st_size, "groups": pq.ParquetFile(out).num_row_groups}


@command("changes")
@option("-s", "--scans", "scans_json", required=True, help="The generation's `scans.json`: its first scan (versions opened then are no transition) and where objects are first listed (`listed_from`)")
@option("-m", "--mem", default="16GB", help="DuckDB memory limit")
@option("-o", "--out", required=True, help="Output dir: one `<name>.parquet` per input")
@option("-t", "--threads", default=8, type=IntRange(min=1), help="DuckDB threads")
@option("-T", "--tmp", default=None, help="DuckDB spill dir")
@option("-r", "--rg-rows", default=RG_ROWS, type=IntRange(min=1), help="Rows per row group")
@option("-i", "--inputs", "inputs", multiple=True, required=True, help="A generation's version files (`pv/r####.parquet`), each cut on its own")
def changes_cmd(scans_json, mem, out, threads, tmp, rg_rows, inputs) -> None:
    """Cut each version file's transitions (the `changes` sort, specs/diff-from-changes.md) and report rows and bytes:
    one JSON line per input, then the totals."""
    doc = json.loads(Path(scans_json).read_text())
    scans = doc["scans"] if isinstance(doc, dict) else doc
    first, objects_from = scans[0]["ts"], listed_from(scans)
    o = Path(out)
    o.mkdir(parents=True, exist_ok=True)
    con = connect(threads, mem, tmp)
    tot: dict = {}
    for i in inputs:
        dst = o / Path(i).name
        s = {"input": i, **cut_changes(con, f"read_parquet('{i}')", dst, first, objects_from, rg_rows=rg_rows),
             "input_rows": pq.ParquetFile(i).metadata.num_rows, "input_bytes": Path(i).stat().st_size}
        print(json.dumps(s), flush=True)
        for k, v in s.items():
            if isinstance(v, int):
                tot[k] = tot.get(k, 0) + v
    err(f"changes: {len(inputs)} inputs")
    print(json.dumps({"total": tot}), flush=True)
