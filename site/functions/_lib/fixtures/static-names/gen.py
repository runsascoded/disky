#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow"]
# ///
"""The static name index's fixture (`staticNames.test.ts`, `staticCatalog.test.ts`): suffix shards and a
catalog shaped like `dt-cloud static-names shards` / `catalog` write them
(specs/architecture/static-name-search.md), over made-up intervals, plus a brute-force first-hit
oracle's answers.

- `sx/s0000.parquet`, `sx/s0001.parquet`: one row per (lowercase name suffix of ≥ 3 characters,
  version), depth ≥ 1 (a bucket is depth 1, as in the scans), columns `s, depth, path, usr, vf, vt, size, n_files`, sorted
  `(s, path, usr, vf)`, zstd, `usr` dictionary-encoded, statistics on, timestamps in ms — the
  writer's settings, but `RG` rows per group (not 8192) so a term spans several groups. Shard 0
  holds suffixes whose first three characters sort below `SPLIT`, shard 1 the rest.
- `shards.json`: the shard plan (`i, lo, hi, rows`), as `plan-shards` writes it.
- `scans.json`: the generation's scans (`id`s only; the dates below).
- `catalog/cells.parquet`, `catalog/index.parquet`, `catalog/meta.json`: the catalog at `V` rows —
  members are the one- and two-character literals of any name and the literals whose suffix range
  holds more than `V` rows; per member a header `(q, '', 0, rows, n)` (`rows` −1 when short) and its
  running per-bucket totals `(q, bucket, vf, b, o)` at each change (epoch seconds), sorted `(q,
  bucket, vf)`, `CELL_RG`-row groups, `bucket` dictionary-encoded; the index holds per group its first
  and last `q`, byte span, rows and per column `data_page_offset, total_compressed_size,
  dictionary_page_offset | 0` (`static_catalog.write_cells`). Cells come from the versions'
  `+`/`−` events, not from the oracle.
- `expected.json`: per (term, date) `{bucket: [bytes, objects]}` from the oracle: for each live
  `(path, usr)` slice on the date (depth ≥ 1) whose lowercase name contains the term and whose
  lowercase parent path does not, its bucket gets its size and objects. It shares no code with
  the suffix layout, so the reader is checked against the query's definition.

Regenerate: `site/functions/_lib/fixtures/static-names/gen.py` (uv runs it).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).parent
RG = 4
SPLIT = "m"
#: The catalog's membership bound (suffix-range rows) and row-group size.
V = 4
CELL_RG = 4
OPEN = datetime(2106, 1, 1, tzinfo=timezone.utc)
D = {d: datetime.fromisoformat(d).replace(tzinfo=timezone.utc) for d in ("2026-08-01", "2026-09-01", "2026-10-01")}
SCHEMA = pa.schema([
    pa.field("s", pa.string(), nullable=False),
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("vt", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
])
A, S, O = D["2026-08-01"], D["2026-09-01"], D["2026-10-01"]

#: Versions `(path, usr, vf, vt, size, n_files)`. Exercises: bucket rows (depth 1, so `bkt` hits them), a `foo` dir covering `foo` descendants
#: (first hit), case variants, a name holding the term twice (deduped), versions opening and closing
#: between the dates, two owner slices of one path, a non-ASCII name, a term only in the parent (not a hit) and a long shared suffix (> 24 chars).
VERSIONS = [
    ("bkt-a", "", A, OPEN, 10_000, 100),
    ("bkt-a/Foo", "", A, S, 700, 7),
    ("bkt-a/Foo", "", S, OPEN, 900, 9),
    ("bkt-a/Foo/foo.txt", "", A, OPEN, 5, 1),
    ("bkt-a/Foo/bar", "", A, OPEN, 300, 3),
    ("bkt-a/data/foofoo.bin", "", A, OPEN, 40, 1),
    ("bkt-a/data/FOO-2.bin", "alice", S, OPEN, 60, 1),
    ("bkt-a/data/FOO-2.bin", "bob", A, O, 61, 1),
    ("bkt-b", "", A, OPEN, 20_000, 200),
    ("bkt-b/xfoox", "", A, S, 1_000, 10),
    ("bkt-b/xfoox/inner-foo", "", A, S, 3, 1),
    ("bkt-b/zz/Ñandú-foo.json", "", S, OPEN, 77, 1),
    ("bkt-b/zz/barfoo", "", A, OPEN, 8, 1),
    ("bkt-b/zz/abcdefghijklmnopqrstuvwxyz-0001.parquet", "", A, OPEN, 11, 1),
    ("bkt-b/zz/abcdefghijklmnopqrstuvwxyz-0002.parquet", "", S, OPEN, 12, 1),
    ("bkt-b/qux", "", A, OPEN, 500, 5),
    ("bkt-b/qux/mmm.txt", "", A, OPEN, 13, 1),
    ("bkt-b/qux/plain", "", A, O, 14, 1),
    ("bkt-a/runs/x-0003.parquet", "", A, OPEN, 15, 1),
    ("bkt-a/runs/x-0004.parquet", "", S, OPEN, 16, 1),
    ("bkt-b/runs/x-0005.parquet", "carol", A, S, 17, 1),
]
TERMS = ["foo", "foo.", "foo-2", "oof", "fooz", "qux", "mmm", "andú", "abcdefghijklmnopqrstuvwxyz-0001", "bkt", "zzz",
         "f", "fo", "o", "-", "zq", "bkt-a", "abcdefghijklmnopqrstuvwxyz-000", "abcdefghijklmnopqrstuvwxyz-0002.parquet", "parquet", ".parquet", "quet"]


def depth_of(path: str) -> int:
    """Path segments: a bucket is depth 1 (the scans' `depth`)."""
    return path.count("/") + 1


def split(path: str) -> tuple[str, str]:
    """`(parent, name)`; a bucket's parent is `''`."""
    return (path.rsplit("/", 1)[0], path.rsplit("/", 1)[1]) if "/" in path else ("", path)


def suffix_rows() -> list[dict]:
    rows = []
    for path, usr, vf, vt, size, n in VERSIONS:
        depth = depth_of(path)
        name = path.rsplit("/", 1)[-1].lower()
        if len(name) < 3:
            continue
        for p in range(len(name) - 2):
            rows.append(dict(s=name[p:], depth=depth, path=path, usr=usr, vf=vf, vt=vt, size=size, n_files=n))
    # Code-point order, as DuckDB sorts (the writer's `ORDER BY s, path, usr, vf`).
    return sorted(rows, key=lambda r: (r["s"], r["path"], r["usr"], r["vf"]))


def write(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    t = pa.Table.from_pylist(rows, schema=SCHEMA)
    with pq.ParquetWriter(out, SCHEMA, compression="zstd", use_dictionary=["usr"], write_statistics=True,
                          coerce_timestamps="ms", allow_truncated_timestamps=False) as w:
        for off in range(0, t.num_rows, RG):
            w.write_table(t.slice(off, RG), row_group_size=RG)


def oracle(term: str, day: datetime) -> dict:
    totals: dict[str, list[int]] = {}
    for path, usr, vf, vt, size, n in VERSIONS:
        if depth_of(path) < 1 or not (vf <= day < vt):
            continue
        parent, name = split(path)
        if term in name.lower() and term not in parent.lower():
            t = totals.setdefault(path.split("/", 1)[0], [0, 0])
            t[0] += size
            t[1] += n
    return dict(sorted(totals.items()))


def epoch(t: datetime) -> int:
    return int(t.timestamp())


def substrings(name: str, n: int) -> set[str]:
    return {name[i:i + n] for i in range(len(name) - n + 1)}


def members(rows: list[dict]) -> dict[str, int]:
    """Member → its range rows (−1 when short): every 1–2 character substring of a name, and every
    literal (a prefix of some suffix row) whose range holds more than `V` rows."""
    out = {q: -1 for path, *_ in VERSIONS for n in (1, 2) for q in substrings(split(path)[1].lower(), n)}
    prefixes = {r["s"][:n] for r in rows for n in range(3, len(r["s"]) + 1)}
    for q in prefixes:
        k = sum(r["s"].startswith(q) for r in rows)
        if k > V:
            out[q] = k
    return out


def cells(q: str, rows: int) -> list[dict]:
    """A member's header and cells, from its first hits' `+`/`−` events."""
    ev: dict[tuple[str, int], list[int]] = {}
    for path, usr, vf, vt, size, n in VERSIONS:
        parent, name = split(path)
        if q not in name.lower() or q in parent.lower():
            continue
        bucket = path.split("/", 1)[0]
        for t, sign in ((vf, 1), (vt, -1)):
            if t == OPEN:
                continue
            e = ev.setdefault((bucket, epoch(t)), [0, 0])
            e[0] += sign * size
            e[1] += sign * n
    body, run = [], {}
    for (bucket, t), (db, dn) in sorted(ev.items()):
        if db == 0 and dn == 0:
            continue
        b, o = run.get(bucket, (0, 0))
        run[bucket] = (b + db, o + dn)
        body.append(dict(q=q, bucket=bucket, vf=t, b=b + db, o=o + dn))
    return [dict(q=q, bucket="", vf=0, b=rows, o=len(body))] + body


CELL_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("bucket", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("b", pa.int64(), nullable=False),
    pa.field("o", pa.int64(), nullable=False),
])
INDEX_SCHEMA = pa.schema([
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("q_min", pa.string(), nullable=False),
    pa.field("q_max", pa.string(), nullable=False),
    pa.field("offset", pa.int64(), nullable=False),
    pa.field("length", pa.int64(), nullable=False),
    pa.field("rows", pa.int32(), nullable=False),
    pa.field("chunks", pa.list_(pa.int64()), nullable=False),
])


def write_catalog(rows: list[dict], out: Path) -> dict:
    mem = members(rows)
    # Code-point order on `(q, bucket, vf)`; the header's `bucket = ''` sorts first.
    cell_rows = [c for q in sorted(mem) for c in cells(q, mem[q])]
    out.mkdir(parents=True, exist_ok=True)
    t = pa.Table.from_pylist(cell_rows, schema=CELL_SCHEMA)
    with pq.ParquetWriter(out / "cells.parquet", CELL_SCHEMA, compression="zstd", use_dictionary=["bucket"], write_statistics=False) as w:
        for off in range(0, t.num_rows, CELL_RG):
            w.write_table(t.slice(off, CELL_RG), row_group_size=CELL_RG)
    md = pq.ParquetFile(out / "cells.parquet").metadata
    idx = {k: [] for k in INDEX_SCHEMA.names}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        chunks, starts, ends = [], [], []
        for c in range(rg.num_columns):
            cc = rg.column(c)
            d = cc.dictionary_page_offset or 0
            start = min(d, cc.data_page_offset) if d else cc.data_page_offset
            chunks += [cc.data_page_offset, cc.total_compressed_size, d]
            starts.append(start)
            ends.append(start + cc.total_compressed_size)
        group = cell_rows[g * CELL_RG:(g + 1) * CELL_RG]
        for k, v in zip(INDEX_SCHEMA.names, (g, group[0]["q"], group[-1]["q"], min(starts), max(ends) - min(starts), rg.num_rows, chunks)):
            idx[k].append(v)
    pq.write_table(pa.table(idx, schema=INDEX_SCHEMA), out / "index.parquet", compression="zstd")
    meta = {"gen": "fixture", "cells_rows": len(cell_rows), "row_groups": md.num_row_groups,
            "members_short": sum(v < 0 for v in mem.values()), "members_long": sum(v >= 0 for v in mem.values()),
            "bytes": (out / "cells.parquet").stat().st_size, "cell_rg": CELL_RG, "membership": {"max_rows": V, "max_bytes": None}}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return mem


def main() -> None:
    rows = suffix_rows()
    lo = [r for r in rows if r["s"][:3] < SPLIT]
    hi = [r for r in rows if r["s"][:3] >= SPLIT]
    write(lo, HERE / "sx" / "s0000.parquet")
    write(hi, HERE / "sx" / "s0001.parquet")
    shards = {"shards": [{"i": 0, "lo": "   ", "hi": SPLIT, "rows": len(lo), "prefixes": len({r["s"][:3] for r in lo})},
                         {"i": 1, "lo": SPLIT, "hi": None, "rows": len(hi), "prefixes": len({r["s"][:3] for r in hi})}]}
    (HERE / "shards.json").write_text(json.dumps(shards, indent=1) + "\n")
    (HERE / "scans.json").write_text(json.dumps({"bucket": "fixture", "scans": [{"id": d} for d in D]}, indent=1) + "\n")
    mem = write_catalog(rows, HERE / "catalog")
    (HERE / "members.json").write_text(json.dumps({t: mem.get(t) for t in TERMS}, indent=1, ensure_ascii=False) + "\n")
    expected = {t: {d: oracle(t, day) for d, day in D.items()} for t in TERMS}
    (HERE / "expected.json").write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(lo)} + {len(hi)} suffix rows; {len(mem)} catalog members")


if __name__ == "__main__":
    main()
