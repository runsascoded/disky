"""`path-index` sharded: per-bucket (or per key-range) parts, then a streaming merge into the single pass's bytes.

`dt-cloud path-index -p BUCKET [-K LO:HI]` aggregates one part of a scan: one bucket's listing, optionally only the
objects whose first key segment (the bucket's top-level dir or root file) is in `[LO, HI)`. A part's rows are exactly
the single pass's rows under that bucket, except a range part's bucket root, which holds only its range's share. Its
dir (the `-P` file's) holds the store's sorts cut as the single pass cuts them (no `.groups.*` sidecars, no search
sidecars) plus:

- `part.json`: the part's bucket / range, the snapshot's additive partials (totals, class sums, per-user bytes and
  classes, the scan's start, the access window), the sorts' names in write order, the row-group size;
- `roots.parquet`: the bucket root's rolled-up slices (`b`, `o`, `wts`, `wb`, classes, `a`, `mtime`, `created`) and
  its structural partials (own objects, direct subdirs, `n_desc`), from which a split bucket's root is recomputed;
- `age.parquet`: the age strata, grouped per part (they sum across parts);
- `pfx.parquet` (attributing): the part's attribution prefix map, for `attr.tsv`.

`dt-cloud path-index-merge` (`merge_path_index`) k-way merges each sort over the parts by the sort's own key (its
`sort` metadata), drops a split bucket's partial roots and inserts the recomputed ones, re-chunks to exactly the
row-group size, and writes through DuckDB's parquet writer with the parts' key-value metadata: the single pass's file,
byte for byte (DuckDB cuts and encodes a group the same from the same rows, given whole groups). Then the `.groups.*`
sidecars from the merged file, the search sidecars (`-S`), `attr.tsv`, `age.json` and `meta.json`.

The store has no row above a bucket (depth 1 is each bucket's root), so a per-bucket split needs nothing recomputed;
only a bucket split into key ranges has its root rebuilt. Row order interleaves across parts at every depth (depth 1
holds every bucket's root), hence a merge, not a concat; it is I/O-bound because parts interleave in long runs.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from bisect import bisect_right
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Sequence

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

err = partial(print, file=sys.stderr)

PART_JSON = "part.json"
ROOTS = "roots.parquet"
AGE = "age.parquet"
PFX = "pfx.parquet"
PART_VERSION = 1
#: Batches read from a part's sort.
READ_ROWS = 65536
#: Row groups per spooled chunk of a merged sort (the ordered COPY reads them as one parallel scan).
SPOOL_GROUPS = 128
SPOOL_GROUPS_ENV = "DT_PATH_INDEX_SPOOL_GROUPS"


def _q(s: str) -> str:
    return s.replace("'", "''")


@dataclass(frozen=True)
class Part:
    """One shard of a scan's `path-index`: a bucket, or the objects of a bucket whose first key segment is in
    `[lo, hi)` (an open side is None)."""

    bucket: str
    lo: str | None = None
    hi: str | None = None

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ValueError("a part needs its bucket")
        if self.lo is not None and self.hi is not None and not self.lo < self.hi:
            raise ValueError(f"part {self.bucket}: empty key range [{self.lo}, {self.hi})")

    @property
    def split(self) -> bool:
        return self.lo is not None or self.hi is not None

    @property
    def bucket_sql(self) -> str:
        """The bucket, quoted for a SQL string literal."""
        return _q(self.bucket)

    @property
    def id(self) -> str:
        return self.bucket if not self.split else f"{self.bucket}[{self.lo or ''}:{self.hi or ''})"

    def where(self) -> str:
        """The part's rows of the listing (`bucket`, `name` as `path-index` reshapes them), as a SQL predicate."""
        conds = [f"bucket = '{self.bucket_sql}'"]
        seg = "split_part(name, '/', 1)"
        if self.lo is not None:
            conds.append(f"{seg} >= '{_q(self.lo)}'")
        if self.hi is not None:
            conds.append(f"{seg} < '{_q(self.hi)}'")
        return " AND ".join(conds)

    def owns(self, seg: str) -> bool:
        """Whether a first key segment falls in this part's range."""
        return (self.lo is None or seg >= self.lo) and (self.hi is None or seg < self.hi)

    def asdict(self) -> dict:
        return {"bucket": self.bucket, "lo": self.lo, "hi": self.hi}


def parse_range(spec: str) -> tuple[str | None, str | None]:
    """`LO:HI` (either side empty: open) → `(lo, hi)`."""
    lo, sep, hi = spec.partition(":")
    if not sep or ":" in hi:
        raise ValueError(f"key range {spec!r}: LO:HI (one colon; a side may be empty)")
    return lo or None, hi or None


def split_parts(bucket: str, bounds: Sequence[str]) -> list[Part]:
    """`bucket` cut at `bounds` (first key segments, strictly increasing): `len(bounds) + 1` parts tiling its keys."""
    bounds = list(bounds)
    if any(not isinstance(b, str) or not b or ":" in b or "/" in b for b in bounds):
        raise ValueError(f"{bucket}: split bounds are non-empty first key segments without ':' or '/' (got {bounds})")
    if any(not a < b for a, b in zip(bounds, bounds[1:])):
        raise ValueError(f"{bucket}: split bounds must increase strictly (got {bounds})")
    edges = [None, *bounds, None]
    return [Part(bucket, lo, hi) for lo, hi in zip(edges, edges[1:])]


def part_ids(bucket: str, n: int) -> list[str]:
    """A bucket's part ids: the bucket itself, or `<bucket>~<i>` for each of its `n` ranges (`~` is never in a bucket
    name)."""
    return [bucket] if n == 1 else [f"{bucket}~{i}" for i in range(n)]


# --- the snapshot's additive partials -----------------------------------------------------------------------------

def combine_partials(partials: Sequence[dict]) -> dict:
    """Parts' snapshot partials (`viz.write_path_index`'s, one per part) as the whole scan's: sums, the earliest start,
    the access window every part computed alike."""
    from .scan_started import earliest_started

    if not partials:
        raise ValueError("no parts")
    first = partials[0]
    for p in partials[1:]:
        for k in ("asof", "attr", "access"):
            if p[k] != first[k]:
                raise ValueError(f"parts disagree on {k}: {first[k]!r} vs {p[k]!r}")
    classes: list[int] = [0] * len(first["class_sums"])
    users: dict[str, int] = {}
    user_class: dict[str, dict[str, int]] = {}
    for p in partials:
        classes = [a + b for a, b in zip(classes, p["class_sums"])]
        for u, b in (p["users"] or {}).items():
            users[u] = users.get(u, 0) + b
        for u, cs in (p["user_class"] or {}).items():
            into = user_class.setdefault(u, {})
            for c, b in cs.items():
                into[c] = into.get(c, 0) + b
    return {
        "asof": first["asof"],
        "attr": first["attr"],
        "started": earliest_started([{"started": p["started"]} for p in partials if p["started"]]),
        "total_bytes": sum(p["total_bytes"] for p in partials),
        "total_objects": sum(p["total_objects"] for p in partials),
        "class_sums": classes,
        "access": first["access"],
        "users": users if first["attr"] else None,
        "user_class": user_class if first["attr"] else None,
    }


# --- the k-way merge ----------------------------------------------------------------------------------------------

def key_fn(sort: str, names: Sequence[str]) -> Callable:
    """`(batch, i) → key` ordering rows as `tiers.write_tiers` sorts them under its `sort` metadata: a column ascending
    with NULLs first, `size_bucket desc` / `tot_bucket desc` (`⌊log2⌋` of `size` / `tot`) descending with NULLs last.
    Strings compare by code point, which is UTF-8 byte order (DuckDB's)."""
    from disk_tree.find.tiers import BUCKET_KEY, TOT_BUCKET_KEY, size_bucket

    idx: list[tuple[int, bool]] = []
    for c in sort.split(","):
        col, desc = {BUCKET_KEY: ("size", True), TOT_BUCKET_KEY: ("tot", True)}.get(c, (c, False))
        if col not in names:
            raise ValueError(f"sort key {c!r}: no column {col!r} in {list(names)}")
        idx.append((list(names).index(col), desc))

    def key(batch, i: int) -> tuple:
        out = []
        for j, desc in idx:
            v = batch.column(j)[i].as_py()
            if desc:
                b = size_bucket(v)
                out.append((1,) if b is None else (0, -b))
            else:
                out.append((0,) if v is None else (1, v))
        return tuple(out)
    return key


class _Stream:
    """A sorted run of batches with a cursor and its head row's key."""

    def __init__(self, batches: Iterator, key_of: Callable):
        self.it = iter(batches)
        self.key_of = key_of
        self.batch = None
        self.pos = 0
        self.head: tuple | None = None
        self._next()

    def _next(self) -> None:
        for b in self.it:
            if b.num_rows:
                self.batch, self.pos = b, 0
                self.head = self.key_of(b, 0)
                return
        self.batch, self.head = None, None

    def take(self, end: int):
        """The batch's rows `[pos, end)`, advancing past them."""
        out = self.batch.slice(self.pos, end - self.pos)
        self.pos = end
        if end == self.batch.num_rows:
            self._next()
        else:
            self.head = self.key_of(self.batch, end)
        return out

    def rest(self) -> Iterator:
        while self.batch is not None:
            yield self.take(self.batch.num_rows)


def kmerge(streams: Sequence[_Stream]) -> Iterator:
    """Every stream's rows in key order, as slices of their batches: the smallest head's stream yields its whole run
    up to the next head (one binary search per run, so cost tracks the runs, not the rows). Keys are unique across
    streams (a store row is one `(path, usr)` slice); a tie raises."""
    live = [s for s in streams if s.batch is not None]
    while live:
        if len(live) == 1:
            yield from live[0].rest()
            return
        live.sort(key=lambda s: s.head)
        s, bound = live[0], live[1].head
        b, n = s.batch, s.batch.num_rows
        if s.key_of(b, n - 1) < bound:
            end = n
        else:
            keys = _KeyView(b, s.key_of)
            end = bisect_right(keys, bound, s.pos, n)
            if s.key_of(b, end - 1) == bound:
                raise ValueError(f"row key {bound} in two parts: parts overlap")
        yield s.take(end)
        if s.batch is None:
            live.remove(s)


class _KeyView:
    """A batch's keys as a lazy sequence, for `bisect`."""

    def __init__(self, batch, key_of: Callable):
        self.batch, self.key_of = batch, key_of

    def __getitem__(self, i: int) -> tuple:
        return self.key_of(self.batch, i)

    def __len__(self) -> int:
        return self.batch.num_rows


def rechunk(batches: Iterator, rows: int, schema) -> Iterator:
    """`batches` as record batches of exactly `rows` rows (the last one shorter), each one contiguous: DuckDB's writer
    cuts its row groups where the single pass's ORDER BY cut them only when fed whole groups."""
    import pyarrow as pa

    buf: list = []
    have = 0

    def flush():
        t = pa.Table.from_batches(buf, schema=schema).combine_chunks()
        return t.to_batches()[0]

    for b in batches:
        while b.num_rows:
            take = min(rows - have, b.num_rows)
            buf.append(b.slice(0, take))
            have += take
            b = b.slice(take)
            if have == rows:
                yield flush()
                buf, have = [], 0
    if have:
        yield flush()


def _drop_roots(batch, buckets):
    """`batch` without the bucket-root rows of `buckets` (a split bucket's partial roots; depth 1, path = bucket)."""
    import pyarrow.compute as pc

    if buckets is None or not batch.num_rows or pc.min(batch.column("depth")).as_py() != 1:
        return batch
    root = pc.and_(pc.equal(batch.column("depth"), 1), pc.is_in(batch.column("path"), value_set=buckets))
    return batch.filter(pc.invert(root))


def _ordered_kv(schema) -> dict[str, str]:
    """A file's key-value metadata in its written order (the footer's; `FileMetaData.metadata` sorts it)."""
    return {k.decode(): v.decode() for k, v in (schema.metadata or {}).items() if k != b"ARROW:schema"}


def root_rows(con, roots: Sequence[Path]) -> "pa.Table | None":
    """A split bucket's root rows (one per owner slice) from its parts' `roots.parquet`: the slices' rollups summed (max
    for `a`, `mtime`, `created`), the structural partials summed once per part, then the store's dir-row expressions
    (`viz.dir_exprs`) over them, as `_write_store` computes a dir row; `tot` = the path's total."""
    from .viz import store_select

    if not roots:
        return None
    files = "[" + ", ".join(f"'{_q(str(f))}'" for f in roots) + "]"
    return con.execute(f"""
        WITH r AS (SELECT * FROM read_parquet({files})),
        p AS (
          SELECT path, 1 AS depth, usr,
            sum(b)::BIGINT AS b, sum(o)::BIGINT AS o, sum(wts)::DECIMAL(38,0) AS wts, sum(wb)::BIGINT AS wb,
            sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4,
            max(a) AS a, max(mtime)::BIGINT AS mtime, max(created)::BIGINT AS created
          FROM r GROUP BY path, usr
        ),
        s AS (SELECT DISTINCT part, path, own_o, n_subdirs, n_desc FROM r),
        d AS (SELECT path, sum(own_o)::BIGINT AS own_o, sum(n_subdirs)::BIGINT AS n_subdirs FROM s GROUP BY path),
        n AS (SELECT path, sum(n_desc)::BIGINT AS n_desc FROM s GROUP BY path)
        SELECT *, CAST(sum(size) OVER (PARTITION BY path) AS BIGINT) AS tot
        FROM (SELECT {store_select(generation=True)} FROM p JOIN d USING (path) JOIN n USING (path))
        ORDER BY path, usr
    """).to_arrow_table()


def merge_sort(
    name: str,
    part_files: Sequence[Path],
    out: Path,
    rows: int,
    split: Sequence[str],
    roots: Sequence[Path],
    groups: bool,
) -> tuple[int, int]:
    """One sort's parts k-way merged into `out` (exactly `rows`-row groups, the parts' metadata, DuckDB's writer),
    with `split` buckets' partial roots replaced by the recomputed ones (`roots`); `.groups.*` sidecars when `groups`.
    Returns `(rows, row groups)`."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq
    from disk_tree import listing_format as lf

    pfs = [pq.ParquetFile(f) for f in part_files]
    schema = pfs[0].schema_arrow
    kv = _ordered_kv(schema)
    bare = schema.remove_metadata()
    for f, pf in zip(part_files, pfs):
        if not pf.schema_arrow.remove_metadata().equals(bare):
            raise ValueError(f"{name}: {f}'s schema differs from {part_files[0]}'s")
        if _ordered_kv(pf.schema_arrow) != kv:
            raise ValueError(f"{name}: {f}'s metadata differs from {part_files[0]}'s")
    key_of = key_fn(kv["sort"], bare.names)
    drop = pa.array(sorted(split), pa.string()) if split else None

    def batches(pf):
        for b in pf.iter_batches(batch_size=READ_ROWS):
            yield pa.RecordBatch.from_arrays(_drop_roots(b, drop).columns, schema=bare)

    streams = [_Stream(batches(pf), key_of) for pf in pfs]
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = true")
    extra = root_rows(con, roots)
    if extra is not None:
        t = extra.select(bare.names).cast(bare)
        order = sorted(range(t.num_rows), key=lambda i: key_of(t, i))
        streams.append(_Stream(t.take(order).to_batches(), key_of))
    # The merged order spooled as whole-group lz4 chunks, then one ordered COPY over them: DuckDB's parquet reader
    # carries batch indices, so the writer encodes groups in parallel and still in order (an Arrow stream encodes on
    # one thread: ~3× slower than the merge itself).
    spool = out.with_name(f".{out.name}.spool")
    shutil.rmtree(spool, ignore_errors=True)
    spool.mkdir(parents=True)
    chunks: list[Path] = []
    writer = None
    spool_groups = int(os.environ.get(SPOOL_GROUPS_ENV) or SPOOL_GROUPS)
    n_chunk = 0
    for b in rechunk(kmerge(streams), rows, bare):
        if writer is None or n_chunk >= spool_groups:
            if writer is not None:
                writer.close()
            chunks.append(spool / f"{len(chunks):06d}.parquet")
            writer = pq.ParquetWriter(chunks[-1], bare, compression="lz4", use_dictionary=False, write_statistics=False)
            n_chunk = 0
        writer.write_batch(b, row_group_size=rows)
        n_chunk += 1
    if writer is not None:
        writer.close()
    kv_sql = ", ".join("'{}': '{}'".format(_q(k), _q(v)) for k, v in kv.items())
    tmp = out.with_name(out.name + ".tmp")
    src = "read_parquet([" + ", ".join(f"'{_q(str(c))}'" for c in chunks) + "])" if chunks else "merged"
    if not chunks:
        con.register("merged", bare.empty_table())
    con.execute(f"COPY (SELECT * FROM {src}) TO '{_q(str(tmp))}' "
                f"(FORMAT PARQUET, {lf.duckdb_codec()}, ROW_GROUP_SIZE {rows}, KV_METADATA {{{kv_sql}}})")
    con.close()
    shutil.rmtree(spool)
    os.replace(tmp, out)
    md = pq.read_metadata(out)
    if groups:
        from disk_tree.find.groups import write_groups

        write_groups(str(out), footer_parquet=True)
    return md.num_rows, md.num_row_groups


def _pfx(parts: Sequence[tuple[Path, Part]]) -> "pd.DataFrame":
    """The scan's attribution prefix map from its parts': each key from a part holding its bucket and first segment
    (path-glob and bucket-glob rules expand against the part's own listing), else (a key under no part's bucket: the
    attribution parquets' and literal rules', alike in every part) from any part."""
    import pandas as pd

    frames = {d: pd.read_parquet(d / PFX) for d, _ in parts}
    out: dict[str, dict] = {}
    owner: dict[str, bool] = {}
    for d, part in parts:
        for row in frames[d].to_dict("records"):
            k = row["key"]
            bucket, _, rest = k.partition("/")
            # a bucket-level key is every part of its bucket's; a deeper one its first segment's part's
            owns = bucket == part.bucket and (not rest or part.owns(rest.split("/", 1)[0]))
            if k in out and owner[k] and owns and (out[k]["user"], out[k]["source"]) != (row["user"], row["source"]):
                raise ValueError(f"attribution key {k!r}: parts holding it disagree ({out[k]} vs {row})")
            if k not in out or (owns and not owner[k]):
                out[k], owner[k] = row, owns
    cols = list(next(iter(frames.values())).columns)
    return pd.DataFrame(list(out.values()), columns=cols)


def read_part(d: Path) -> tuple[Part, dict]:
    doc = json.loads((d / PART_JSON).read_text())
    if doc.get("version") != PART_VERSION:
        raise ValueError(f"{d / PART_JSON}: part version {doc.get('version')} (this merge reads {PART_VERSION})")
    return Part(**doc["part"]), doc


def check_tiling(parts: Sequence[Part]) -> None:
    """Each bucket's parts tile its keys: one whole-bucket part, or ranges from open to open, each starting where the
    previous ended (a missing or overlapping range would drop or double rows)."""
    by: dict[str, list[Part]] = {}
    for p in parts:
        by.setdefault(p.bucket, []).append(p)
    for b, ps in by.items():
        ps = sorted(ps, key=lambda p: (p.lo is not None, p.lo or ""))
        edges = [ps[0].lo] + [x for p, q in zip(ps, ps[1:]) for x in (p.hi, q.lo)] + [ps[-1].hi]
        if edges[0] is not None or edges[-1] is not None or any(edges[i] != edges[i + 1] or edges[i] is None
                                                                  for i in range(1, len(edges) - 1, 2)):
            raise ValueError(f"{b}: parts {[(p.lo, p.hi) for p in ps]} don't tile its keys")


def merge_path_index(
    part_dirs: Sequence[Path],
    out_dir: Path,
    path_index: Path,
    *,
    search: bool = False,
    procs: int | None = None,
) -> dict:
    """The scan's `path-index` outputs from its parts (`path-index -p`): the sorts beside `path_index` (+ `.groups.*`
    sidecars unless the parts were `-O`, search sidecars with `search`, `attr.tsv` when attributing) and `age.json` /
    `meta.json` under `out_dir`, each what the single pass over all the parts' listings writes. `procs`: sorts merged
    at once (default: all). Returns the meta."""
    import duckdb

    from .extras import write_extras
    from .viz import age_rows, snapshot_meta

    if path_index.name != "path-index.parquet":
        raise ValueError(f"the store's `path` sort is served as path-index.parquet; got {path_index}")
    loaded = [(Path(d), *read_part(Path(d))) for d in part_dirs]
    if not loaded:
        raise ValueError("no parts to merge")
    check_tiling([p for _, p, _ in loaded])
    first = loaded[0][2]
    for d, _, doc in loaded[1:]:
        for k in ("sorts", "row_group_rows", "path_only"):
            if doc[k] != first[k]:
                raise ValueError(f"{d}: {k} {doc[k]!r} differs from {loaded[0][0]}'s {first[k]!r}")
    counts: dict[str, int] = {}
    for _, p, _ in loaded:
        counts[p.bucket] = counts.get(p.bucket, 0) + 1
    split = sorted(b for b, n in counts.items() if n > 1)
    roots = [d / ROOTS for d, p, _ in loaded if p.bucket in split]
    path_index.parent.mkdir(parents=True, exist_ok=True)
    names = list(first["sorts"])
    jobs = {v: (first["sorts"][v], [d / first["sorts"][v] for d, _, _ in loaded], path_index.parent / first["sorts"][v],
                first["row_group_rows"], split, roots, not first["path_only"]) for v in names}
    n_procs = max(1, min(procs or len(jobs), len(jobs)))
    if n_procs == 1:
        done = {v: merge_sort(*a) for v, a in jobs.items()}
    else:
        with ProcessPoolExecutor(n_procs) as ex:
            futs = {v: ex.submit(merge_sort, *a) for v, a in jobs.items()}
            done = {v: f.result() for v, f in futs.items()}
    sorts: dict[str, dict] = {}
    for v in names:
        n, g = done[v]
        sorts[v] = {"file": str(jobs[v][2]), "rows": n, "groups": g}
        err(f"{v}: {n:,} rows, {g:,} row groups → {jobs[v][2]} (merged from {len(loaded)} parts)")
    if search:
        from disk_tree.find.search import write_search

        st = write_search(sorts["path"]["file"], con=duckdb.connect())
        err(f"search: {st.names:,} names, {st.rows:,} rows, {st.postings:,} trigram postings → {st.files['search']}")
    whole = combine_partials([doc["partial"] for _, _, doc in loaded])
    write_extras(_pfx([(d, p) for d, p, _ in loaded]) if whole["attr"] else None, path_index.parent)

    con = duckdb.connect()
    files = "[" + ", ".join(f"'{_q(str(d / AGE))}'" for d, _, _ in loaded) + "]"
    keys = 'day, d1, "user", a' if whole["attr"] else "day, d1, a"
    age = con.execute(f"""
        SELECT {keys}, sum(bytes)::BIGINT AS bytes, sum(objects)::BIGINT AS objects
        FROM read_parquet({files}) GROUP BY ALL ORDER BY ALL
    """).fetchall()
    meta = snapshot_meta(whole, sorts)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "age.json").write_text(json.dumps(age_rows(age, whole["attr"]), separators=(",", ":")) + "\n")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
