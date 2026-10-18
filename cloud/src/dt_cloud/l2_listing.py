"""A layer-2's object rows back to a layer-1 listing: `dt-cloud l2-listing -b BUCKET [-k PREFIX]… L2 OUT`.

For a scan whose raw listing is gone but whose layer-2 is kept (e.g. a meta store's scans before it kept listings), so
`path-index` (which reads listings) can re-cut it: a reprocessed scan (`scan head -R`) under `options.index.mode:
path-index` builds its listing this way when there is none (`scan_steps.path_index`). The mapping (cw's round-trip,
2026-10-18: `disk-tree import` of the result reproduced a 129,016-row layer-2 exactly):

- `bucket`: `-b`; `name`: `path` (a layer-2's paths are relative to its bucket); `size_bytes`: `size`
- `created`: `mtime` (import reads whole seconds, so it round-trips)
- `storage_class_id`: the `N` of the one `sum_storage_class_id_<N>` pivot equal to a file's `size`. A 0-byte file adds
  0 to every pivot whatever its class, so its own is unknowable: 0-byte files carry, in turn, the classes no sized file
  has (whose columns hold only zeros, so import writes them again), else NULL; NULL with no pivots
- `generation`: NULL (a layer-2 doesn't keep it)

Only `kind = 'file'` rows (import re-derives the dirs), sorted by name; `-k` keeps paths under the given prefixes."""
from __future__ import annotations

from pathlib import Path

from click import argument, command, option

from .static_names import err, q


def l2_listing(l2: str, out: str | Path, bucket: str, keep: tuple[str, ...] = ()) -> tuple[int, int]:
    """Write `l2`'s file rows as a listing of `bucket` to `out` (parquet); returns `(files, bytes)`."""
    import duckdb

    con = duckdb.connect()
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(l2)})").fetchall()}
    if "uri" in cols:
        raise SystemExit(f"{l2}: a v1 layer-2 (`uri`): recompress it to v2 first (`disk-tree recompress`)")
    pivots = sorted(c for c in cols if c.startswith("sum_storage_class_id_"))
    where = "kind = 'file'" + (" AND (" + " OR ".join(f"starts_with(path, {q(k)})" for k in keep) + ")" if keep else "")
    src = f"(SELECT * FROM read_parquet({q(l2)}) WHERE {where})"
    sized = ("CASE " + " ".join(f"WHEN {c} = size THEN {c.rsplit('_', 1)[1]}" for c in pivots) + " END") if pivots else "NULL"
    # A class only 0-byte files had: its column holds zeros, which any 0-byte file carries without changing a value.
    # Each such class goes to the 0-byte files in turn (one had it, so there are enough), so import writes its column.
    held = {r[0] for r in con.execute(f"SELECT DISTINCT {sized} FROM {src} WHERE size > 0").fetchall() if r[0] is not None}
    zero_only = [int(c.rsplit("_", 1)[1]) for c in pivots if int(c.rsplit("_", 1)[1]) not in held]
    zero = (f"[{', '.join(map(str, zero_only))}][(row_number() OVER (PARTITION BY size = 0 ORDER BY path) - 1) % {len(zero_only)} + 1]"
            if zero_only else "NULL")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT {q(bucket)} AS bucket, path AS name, size AS size_bytes, to_timestamp(mtime) AS created,
            (CASE WHEN size > 0 THEN {sized} ELSE {zero} END)::INTEGER AS storage_class_id, NULL::BIGINT AS generation
        FROM {src} ORDER BY name) TO {q(str(out))} (FORMAT PARQUET)""")
    n, b = con.execute(f"SELECT count(*), coalesce(sum(size_bytes), 0) FROM read_parquet({q(str(out))})").fetchone()
    return int(n), int(b)


@command("l2-listing")
@option("-b", "--bucket", required=True, help="The listing's bucket (the layer-2's root)")
@option("-k", "--keep", multiple=True, help="Keep only paths under this prefix (repeat)")
@argument("l2")
@argument("out")
def l2_listing_cmd(bucket, keep, l2, out) -> None:
    """A layer-2's file rows (L2) back to a layer-1 listing at OUT, which `disk-tree import` re-aggregates to the same
    layer-2 (`generation`, and a 0-byte file's class, aren't kept)."""
    n, b = l2_listing(l2, out, bucket, tuple(keep))
    err(f"{out}: {n:,} files, {b:,} B")
