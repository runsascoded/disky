"""A scan's index checked against what it was built from (`dt-cloud age-pyramid -c`, `dt-cloud index-verify`): the age
pyramids against the `path` sort's dated files, and the `path` sort, the snapshot JSONs, the age pyramids and the
`bysize` sort against the listings. A scan step runs them after `path-index` (`options.index.age_pyramids` /
`verify`), so a wrong index fails before anything publishes it."""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import duckdb

from .index import AGE_PYRAMID_BINS, write_age_pyramid


class Mismatch(ValueError):
    """A check that found the index disagreeing with its input."""


def connect(mem: str | None = None, threads: int | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    if mem:
        con.execute(f"SET memory_limit='{mem}'")
    if threads:
        con.execute(f"SET threads={int(threads)}")
    return con


def check_age_pyramid(con: duckdb.DuckDBPyConnection, path_sort: Path, out: Path, bins: Sequence[str] = AGE_PYRAMID_BINS) -> None:
    """Each bin's tier against the `path` sort's dated files (`kind = 'file' AND mtime > 0`): its depth-1 totals are the
    buckets at or above the tier's floor (`coarse_floor` in its metadata), its depth-0 row the whole population."""
    from pyarrow.parquet import read_metadata

    population = con.execute(
        "SELECT split_part(path, '/', 1), sum(size)::BIGINT, count(*) FROM read_parquet(?) WHERE kind = 'file' AND mtime > 0 GROUP BY 1 ORDER BY 1",
        [str(path_sort)],
    ).fetchall()
    root = (sum(r[1] for r in population), sum(r[2] for r in population))
    for b in bins:
        tier = out / f"age-pyramid-{b}.parquet"
        floor = int(read_metadata(tier).metadata[b"coarse_floor"])
        expected = [(bucket, size, count) for bucket, size, count in population if size >= floor]
        actual = con.execute(
            "SELECT path, sum(b)::BIGINT, sum(o)::BIGINT FROM read_parquet(?) WHERE depth = 1 GROUP BY path ORDER BY path", [str(tier)],
        ).fetchall()
        fleet = con.execute("SELECT coalesce(sum(b), 0)::BIGINT, coalesce(sum(o), 0)::BIGINT FROM read_parquet(?) WHERE depth = 0", [str(tier)]).fetchone()
        if actual != expected or fleet != root:
            raise Mismatch(f"age pyramid {b}: depth-1 {actual} / root {fleet}; the path sort's dated files give {expected} / {root}")


def build_age_pyramid(path_sort: Path, out: Path, *, mem: str | None = None, threads: int | None = None, check: bool = False) -> dict:
    """`age-pyramid-<bin>.parquet` per bin under `out` from the `path` sort's file rows (`index.write_age_pyramid`), then
    (`check`) `check_age_pyramid`. Returns the writer's summary."""
    con = connect(mem, threads)
    try:
        summary = write_age_pyramid(con, str(path_sort), out)
        if check:
            check_age_pyramid(con, path_sort, out)
        return summary
    finally:
        con.close()


def verify_index(
    path_sort: Path,
    listings: Sequence[str],
    snapshots: Path,
    buckets: Sequence[str],
    *,
    scan: str,
    gen: str | None = None,
    mem: str | None = None,
    threads: int | None = None,
) -> dict:
    """The scan's index against its listings (`bucket, size_bytes, created`), raising `Mismatch` on the first
    disagreement:
    - every listed bucket is one of `buckets`, and each one's bytes / objects equal its depth-1 row of the `path` sort
      (`size`, `n_files`; an empty bucket has no row);
    - the snapshot's `meta.json` totals (`total_bytes`, `total_objects`) equal the listings';
    - its `age.json` histogram sums to the dated listing population (`created IS NOT NULL`);
    - the age pyramids beside the sort, when written (`check_age_pyramid`);
    - the `bysize` sort beside it, when written: the root and every depth-1 view equal the `path` sort's
      (`bysize_check.check_views`).

    Returns the report written as `verification.json`: `{scan, [generation,] buckets: {b: {bytes, objects}}, totals}`."""
    con = connect(mem, threads)
    try:
        src = "read_parquet([" + ", ".join(f"'{g}'" for g in listings) + "], union_by_name = true)"
        counts = con.execute(f"SELECT bucket, sum(size_bytes)::BIGINT, count(*) FROM {src} GROUP BY bucket ORDER BY bucket").fetchall()
        expected = {b: {"bytes": 0, "objects": 0} for b in buckets}
        for b, size, n in counts:
            if b not in expected:
                raise Mismatch(f"listing bucket {b!r} is not one of the scan's ({', '.join(buckets)})")
            expected[b] = {"bytes": int(size), "objects": int(n)}
        tops = con.execute("SELECT path, size, n_files FROM read_parquet(?) WHERE depth = 1 ORDER BY path", [str(path_sort)]).fetchall()
        indexed = {p: {"bytes": int(size), "objects": int(n)} for p, size, n in tops}
        listed = {b: v for b, v in expected.items() if v["objects"]}
        if indexed != listed:
            raise Mismatch(f"per-bucket path totals {indexed} ≠ the listings' {listed}")
        totals = {"bytes": sum(v["bytes"] for v in expected.values()), "objects": sum(v["objects"] for v in expected.values())}
        meta = json.loads((snapshots / "meta.json").read_text())
        if {"bytes": meta["total_bytes"], "objects": meta["total_objects"]} != totals:
            raise Mismatch(f"snapshot totals {meta['total_bytes']} B / {meta['total_objects']} objects ≠ the listings' {totals}")
        age = json.loads((snapshots / "age.json").read_text())
        dated = con.execute(f"SELECT coalesce(sum(size_bytes), 0)::BIGINT, count(*) FROM {src} WHERE created IS NOT NULL").fetchone()
        hist = (sum(r["b"] for r in age), sum(r["o"] for r in age))
        if hist != tuple(dated):
            raise Mismatch(f"age histogram {hist} ≠ the dated listing population {tuple(dated)}")
        if (path_sort.parent / f"age-pyramid-{AGE_PYRAMID_BINS[0]}.parquet").exists():
            check_age_pyramid(con, path_sort, path_sort.parent)
        bysize = path_sort.parent / "path-index-bysize.parquet"
        if bysize.exists():
            from .bysize_check import check_views

            if not all(c.exact for c in check_views(str(path_sort), str(bysize), con=con)):
                raise Mismatch("bysize views ≠ the path sort's totals")
        return {"scan": scan, **({"generation": gen} if gen else {}), "buckets": expected, "totals": totals}
    finally:
        con.close()
