#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow"]
# ///
"""The runs fixture (`staticRuns.test.ts`; specs/static-append.md): a base generation through
2026-09-01 and four runs, one per scan — 2026-10-01 and 2026-10-02 (date ids), then two scans on one day keyed by
scan id, 2026-10-03T0600 and 2026-10-03T1800 (specs/scan-ids-not-dates.md) — in the layouts `dt-cloud static-names runs` writes, over
`static-names/gen.py`'s versions plus some that open or close on the runs' dates, and a brute-force oracle.

A tier's contents are defined by cuts: `cut(T)` is the versions opened by `T`, a `vt` after `T` read as open.
- base: `cut(2026-09-01)`'s suffix rows, shards and catalog (`static-names/gen.py`'s writers);
- a run at `D` after `P`: the suffix rows of `cut(D)` not in `cut(P)` — the versions opened at `D`, and the
  close records (a version closed at `D`, its rows with their final `vt`); its catalog is the rows of
  `catalog(cut(D))` not in `catalog(cut(P))` (new cells, new or changed headers);
- `manifests/<scan>.json` per run lists the runs through it; `expected.json` is the oracle over every version on every scan,
  and `catalog-expected.json` the full catalog's cells per term (members only);
- a deferred carry (`static_merge`): `deltas/2026-10-01_2026-10-02` (level 1), the cut of 2026-10-02 not in the base's
  (what merging the two runs holds), published by the revision `manifests/2026-10-02.m001.json` of 2026-10-02's manifest.

Regenerate: `site/functions/_lib/fixtures/static-runs/gen.py` (uv runs it).
"""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("names_gen", HERE.parent / "static-names" / "gen.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

OPEN, A, S, O = g.OPEN, g.A, g.S, g.O
P = datetime(2026, 10, 2, tzinfo=timezone.utc)
AM, PM = datetime(2026, 10, 3, 6, tzinfo=timezone.utc), datetime(2026, 10, 3, 18, tzinfo=timezone.utc)
DATES = {"2026-08-01": A, "2026-09-01": S, "2026-10-01": O, "2026-10-02": P, "2026-10-03T0600": AM, "2026-10-03T1800": PM}
RUNS = list(DATES)[2:]
#: The base fixture's versions, `foo.txt` closing on P, plus versions opening on O and P: five `qqq-*` files cross V
#: on P (a new member whose whole history is in the last run).
VERSIONS = [
    *(v if v[0] != "bkt-a/Foo/foo.txt" else (*v[:3], P, *v[4:]) for v in g.VERSIONS),
    ("bkt-a/Foo/foo.txt", "", P, OPEN, 6, 1),
    ("bkt-b/zz/late-foo.csv", "", O, OPEN, 21, 1),
    ("bkt-b/zz/late-foo.csv", "dave", O, P, 22, 1),
    *((f"bkt-{'ab'[k % 2]}/q/qqq-{k}", "", P, OPEN, 30 + k, 1) for k in range(5)),
    # two scans on one day: one opens at 06:00 and closes at 18:00, the other opens at 18:00
    ("bkt-a/sub/sub-am.txt", "", AM, PM, 7, 1),
    ("bkt-b/sub/sub-pm.txt", "", PM, OPEN, 9, 1),
]
TERMS = [*g.TERMS, "qqq", "qqq-", "late", "e-foo", "q", "sub-"]


def cut(t: datetime) -> list[tuple]:
    return [(path, usr, vf, vt if vt <= t else OPEN, size, n) for path, usr, vf, vt, size, n in VERSIONS if vf <= t]


def tier(dst: Path, rows: list[dict], cells: list[dict]) -> None:
    lo = [r for r in rows if r["s"][:3] < g.SPLIT]
    hi = [r for r in rows if r["s"][:3] >= g.SPLIT]
    g.write(lo, dst / "sx" / "s0000.parquet")
    g.write(hi, dst / "sx" / "s0001.parquet")
    shards = {"shards": [{"i": 0, "lo": "   ", "hi": g.SPLIT, "rows": len(lo)}, {"i": 1, "lo": g.SPLIT, "hi": None, "rows": len(hi)}]}
    (dst / "shards.json").write_text(json.dumps(shards, indent=1) + "\n")
    write_cells(cells, dst / "catalog")


def write_cells(cell_rows: list[dict], out: Path) -> None:
    """`static-names/gen.py`'s catalog writer over given cell rows (sorted `(q, bucket, vf)`)."""
    out.mkdir(parents=True, exist_ok=True)
    t = pa.Table.from_pylist(cell_rows, schema=g.CELL_SCHEMA)
    with pq.ParquetWriter(out / "cells.parquet", g.CELL_SCHEMA, compression="zstd", use_dictionary=["bucket"], write_statistics=False) as w:
        for off in range(0, t.num_rows, g.CELL_RG):
            w.write_table(t.slice(off, g.CELL_RG), row_group_size=g.CELL_RG)
    md = pq.ParquetFile(out / "cells.parquet").metadata
    idx = {k: [] for k in g.INDEX_SCHEMA.names}
    for k in range(md.num_row_groups):
        rg = md.row_group(k)
        chunks, starts, ends = [], [], []
        for c in range(rg.num_columns):
            cc = rg.column(c)
            d = cc.dictionary_page_offset or 0
            start = min(d, cc.data_page_offset) if d else cc.data_page_offset
            chunks += [cc.data_page_offset, cc.total_compressed_size, d]
            starts.append(start)
            ends.append(start + cc.total_compressed_size)
        group = cell_rows[k * g.CELL_RG:(k + 1) * g.CELL_RG]
        for name, v in zip(g.INDEX_SCHEMA.names, (k, group[0]["q"], group[-1]["q"], min(starts), max(ends) - min(starts), rg.num_rows, chunks)):
            idx[name].append(v)
    pq.write_table(pa.table(idx, schema=g.INDEX_SCHEMA), out / "index.parquet", compression="zstd")
    meta = {"gen": "fixture", "cells_rows": len(cell_rows), "row_groups": md.num_row_groups, "bytes": (out / "cells.parquet").stat().st_size,
            "cell_rg": g.CELL_RG, "membership": {"max_rows": g.V, "max_bytes": None}}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")


def state(versions: list[tuple]) -> tuple[list[dict], list[dict]]:
    """A cut's suffix rows and catalog cells (`static-names/gen.py`'s functions over `versions`)."""
    g.VERSIONS = versions
    rows = g.suffix_rows()
    mem = g.members(rows)
    return rows, [c for q in sorted(mem) for c in g.cells(q, mem[q])]


def key(r: dict) -> tuple:
    return tuple(r.values())


def main() -> None:
    cuts = {d: state(cut(t)) for d, t in DATES.items()}
    tier(HERE / "base", *cuts["2026-09-01"])
    (HERE / "base" / "scans.json").write_text(json.dumps({"bucket": "fixture", "scans": [{"id": d} for d in ("2026-08-01", "2026-09-01")]}, indent=1) + "\n")
    runs = []
    (HERE / "manifests").mkdir(exist_ok=True)
    for prev, d in zip(list(DATES)[1:], RUNS):
        (rp, cp), (rd, cd) = cuts[prev], cuts[d]
        have_r, have_c = {key(r) for r in rp}, {key(c) for c in cp}
        rows = sorted((r for r in rd if key(r) not in have_r), key=lambda r: (r["s"], r["path"], r["usr"], r["vf"]))
        cells = [c for c in cd if key(c) not in have_c]
        tier(HERE / "deltas" / d, rows, cells)
        runs.append({"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d], "rows": len(rows)})
        (HERE / "manifests" / f"{d}.json").write_text(json.dumps({"gen": "fixture", "date": d, "base_scans": 2,
            "scans": list(DATES)[:2 + len(runs)], "runs": runs}, indent=1) + "\n")
    # The deferred carry of the first two runs, and the revision listing it in their place.
    (rp, cp), (rd, cd) = cuts["2026-09-01"], cuts["2026-10-02"]
    have_r, have_c = {key(r) for r in rp}, {key(c) for c in cp}
    rows = sorted((r for r in rd if key(r) not in have_r), key=lambda r: (r["s"], r["path"], r["usr"], r["vf"]))
    tier(HERE / "deltas" / "2026-10-01_2026-10-02", rows, [c for c in cd if key(c) not in have_c])
    merged = {"key": "deltas/2026-10-01_2026-10-02", "first": "2026-10-01", "last": "2026-10-02", "level": 1, "scans": RUNS[:2], "rows": len(rows)}
    (HERE / "manifests" / "2026-10-02.m001.json").write_text(json.dumps({"gen": "fixture", "date": "2026-10-02", "base_scans": 2,
        "scans": list(DATES)[:4], "runs": [merged], "rev": 1, "revises": "manifests/2026-10-02.json"}, indent=1) + "\n")
    g.VERSIONS = VERSIONS
    expected = {t: {d: g.oracle(t, day) for d, day in DATES.items()} for t in TERMS}
    (HERE / "expected.json").write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n")
    full = cuts[RUNS[-1]][1]
    cat = {t: [[c["bucket"], c["vf"], c["b"], c["o"]] for c in full if c["q"] == t] for t in TERMS}
    (HERE / "catalog-expected.json").write_text(json.dumps({t: v or None for t, v in cat.items()}, indent=1, ensure_ascii=False) + "\n")
    print(f"base {len(cuts['2026-09-01'][0])} rows; runs {[r['rows'] for r in runs]}")


if __name__ == "__main__":
    main()
