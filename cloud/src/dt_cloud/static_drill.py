"""The heavy-term drilldown's per-scan runs (specs/done/static-append.md, "Drilldown runs (heavy terms)"): each scan's
`<run>/drill/`, in the base `drill/`'s layout and schemas (`static_roots`), read beside the base.

- **Roots**: a member's first-hit rows of the scan's version delta (opens; close records with their final `vt`), for
  each class canonical; a member new at the scan gets its whole history. Combine rule: rows equal on `(q, path, usr,
  vf)` are one root, the smallest `vt` wins.
- **Classes**: each tier's own alias map (`aliases.parquet`): the classes before the scan refined by each member's
  delta digest (classes only split). A reader uses `c_i = aliases_i[t] ?? t` per tier.
- **Rollups**: per heavy `(c, dir)` touched by the scan, its cells dated at the scan under a delta header (`kind` −1),
  or its whole history under a full header (`kind` 0) when an existing child enters or leaves the kept set, or the
  directory just became heavy. A reader walks the tiers newest first and stops after a full header.
- **Heavy**: `S(c, dir)`, the stored root rows under `dir` summed over the tiers, above R; sticky.
"""
from __future__ import annotations

import json
import shutil
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow.compute import and_, greater_equal, less
from click import Choice, group, option

from . import heartbeat
from . import static_names as sn
from . import static_roots as sr
from .checkpoint import Checkpoint, code_version, Parts
from .static_names import CODEC, OPEN, PREFIX, Progress, _batches, err, q
from .static_profile import data_bucket
from .resource_monitor import monitor_resources, resource_stage

KIND_FULL, KIND_KEPT, KIND_REST, KIND_DELTA = 0, 1, 2, -1  # the delta header sorts first, as a full one
KINDS = ("long", "short")
#: Roots / rollup rows per data file (files are cut at the next `q` boundary past this).
FILE_ROWS = 50_000_000
#: A run's row groups: smaller than the base's, since the reader's dispatch bound sums 2·`rg` over the tiers.
RUN_RG = 2048
#: Match PyArrow's former whole-table writer while bounding its Arrow work to fixed batches and row groups.
STATE_RG = 1 << 20
#: DuckDB's whole-table conversion used decimal 1M batches; preserve their boundaries for identical Parquet pages.
STATE_BATCH_ROWS = 1_000_000
#: Decoded row groups kept per group file by the builder's reads.
GROUP_CACHE = 2048
#: Index spans (`CachedGroupFile._span`: a run of index row groups' entries and key tuples) kept per group file.
SPAN_CACHE = 512
#: `dr` rows (at depth ≥ 2) per part of the aggregations over their ancestors (`_by_parts`).
EXPLODE_PART_ROWS = 1 << 23
#: Newly heavy directories' estimated root rows (summed over a chunk's directories, so nested ones count each time) built
#: at once (`newly_heavy_rollups`): their prior roots read under one prefetch plan, combined and rolled up in one statement each.
NEWLY_HEAVY_CHUNK_ROWS = 1 << 23
#: Settling's input rows (affected directories' touched and kept children, as Python rows) loaded at once.
SETTLE_CHUNK_ROWS = 1 << 22
#: Concurrent ranged reads when the builder prefetches the groups its loops will read (`build -j`); ≤ 1: no prefetch, the
#: loops fetch each miss in turn.
FETCH_THREADS = 64
#: Files `write_kind` writes at once from sorted chunks, each in a process of its own (each a pure function of its `q`
#: range; the same bytes at any count): the carry was write-bound after its reads were (gcs's 10-22 L3 carry: long 41–53
#: min, short 18), and a writer is Python per 8,192-row group (~6.7 M groups for a compaction's 55 B roots), so threads
#: share one GIL. None: every core (`write_jobs`).
WRITE_JOBS: int | None = None
#: Files `write_kind` sorts and writes at once from tables (each its own cursor's `ORDER BY` over its `q` range).
SORT_WRITERS = 4
#: Sorted-chunk files `_sorted_chunks` counts at a time (each its own cursor; DuckDB releases the GIL).
COUNT_THREADS = 16


def write_jobs() -> int:
    """`WRITE_JOBS`, or every core this process may run on."""
    import os

    if WRITE_JOBS is not None:
        return WRITE_JOBS
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def _writer_init() -> None:
    pa.set_cpu_count(1)
#: Prefetched raw bytes held at most, as a fraction of the machine's RAM (`build -C` overrides): beyond it, the loops run in
#: windows (each prefetched in turn) and the byte cache evicts least recently used.
CACHE_RAM_FRACTION = 1 / 8
MAX_CACHE_BYTES = 2 << 30
#: Prefetch coalescing: needed groups of one file at most this far apart are one request, of at most `COALESCE_SPAN` bytes.
COALESCE_GAP = 64 << 10
COALESCE_SPAN = 8 << 20
#: A drill tier's `meta.json` `dcount` when its `dcount` and newly-heavy sums count only the roots new to the stack (an
#: existing member's opens, every root of a new member: `day_rollups`' `fresh`); a merged tier's only when every input's
#: did. A tier without it (built before) counts the closes too: conservative for its readers, but not what a compaction's
#: incremental measurement sums (`static_measure`).
DCOUNT_FRESH = "fresh"
DCOUNT_SCHEMA = pa.schema([pa.field("q", pa.string(), nullable=False), pa.field("dir", pa.string(), nullable=False),
                           pa.field("rows", pa.int64(), nullable=False)])
ALIAS_SCHEMA = pa.schema([pa.field("q", pa.string(), nullable=False), pa.field("canonical", pa.string(), nullable=False)])
ROLLUP_COLS = "q VARCHAR, dir VARCHAR, kind TINYINT, child VARCHAR, vf BIGINT, b BIGINT, o BIGINT"
ROLLUP_COLS_NAMES = [c.split()[0] for c in ROLLUP_COLS.split(", ")]
#: `ROLLUP_COLS` as Arrow: a checkpoint's parts of rollup rows (settling's, the newly heavy directories').
ROLLUP_ARROW = pa.schema([("q", pa.string()), ("dir", pa.string()), ("kind", pa.int8()), ("child", pa.string()), ("vf", pa.int64()),
                          ("b", pa.int64()), ("o", pa.int64())])


def insert_rows(con, table: str, rows: list[tuple]) -> None:
    """`rows` appended to DuckDB table `table` (its columns, in order), as one Arrow table: `executemany` took minutes for a
    few hundred thousand rows (the probes' and settling's inserts were ~17% of gcs 10-11's short drill)."""
    if not rows:
        return
    schema = con.execute(f"SELECT * FROM {table} LIMIT 0").to_arrow_table().schema
    t = pa.table([pa.array(col, f.type) for col, f in zip(zip(*rows), schema)], schema=schema)
    con.register("_insert_rows", t)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM _insert_rows")
    finally:
        con.unregister("_insert_rows")


# ── Tiers ──────────────────────────────────────────────────────────────────


def ram_bytes() -> int:
    import os

    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


class ByteCache:
    """Raw (compressed) byte spans of group files, keyed `(group file, file, offset)`, least recently used evicted past
    `cap` bytes; shared by a build's group files. Only the calling thread touches it (fetch threads return bytes)."""

    def __init__(self, cap: int):
        self.cap, self.size = cap, 0
        self._d: OrderedDict[tuple, bytes] = OrderedDict()
        self.evicted = 0

    def __contains__(self, key) -> bool:
        return key in self._d

    def get(self, key) -> bytes | None:
        data = self._d.get(key)
        if data is not None:
            self._d.move_to_end(key)
        return data

    def pop(self, key) -> None:
        data = self._d.pop(key, None)
        if data is not None:
            self.size -= len(data)

    def put(self, key, data: bytes) -> None:
        if key in self._d:
            self.size -= len(self._d.pop(key))
        self._d[key] = data
        self.size += len(data)
        while self.size > self.cap and len(self._d) > 1:
            _, old = self._d.popitem(last=False)
            self.size -= len(old)
            self.evicted += 1


class CachedGroupFile(sr.GroupFile):
    """`static_roots.GroupFile` (two-level) with its index row groups, parsed footers and decoded data groups cached, and
    `rows_in` reading group by group: the builder probes many small ranges of the same files. With a `ByteCache` (`raw`),
    a fetch is first looked up there (`TierReads.prefetch` fills it); `io` counts the reads as without it (a decode of a
    group not decoded, a load of index row groups), `fetch_io` the requests actually made."""

    def __init__(self, *args, key: str, cache_groups: int = GROUP_CACHE, **kw):
        super().__init__(*args, **kw)
        self.key = key
        self._entries: dict[int, list[dict]] = {}
        self._peeked: dict[int, list[dict]] = {}
        self._md: dict[str, pq.FileMetaData] = {}
        self._groups: OrderedDict[tuple[str, int], tuple[pa.Table, list[tuple]]] = OrderedDict()
        self._spans: OrderedDict[tuple[int, int], tuple[list[dict], list[tuple], list[tuple]]] = OrderedDict()
        self.cap_groups = cache_groups
        self.raw: ByteCache | None = None
        self.fetch_io: Counter = Counter()
        if self.top is not None:
            self._top_lo = [(t["q_min"], t["k_min"]) for t in self.top]
            self._top_hi = [(t["q_max"], t["k_max"]) for t in self.top]
        self.io.update(groups_read=0, group_bytes=0)

    def _get(self, file: str, lo: int, hi: int) -> bytes:
        """Bytes `[lo, hi)` of `file`: the byte cache's span (prefetched), else a fetch (a miss)."""
        if self.raw is not None:
            data = self.raw.get((self, file, lo))
            if data is not None and len(data) == hi - lo:
                self.fetch_io["raw_hits"] += 1
                return data
        self.fetch_io["miss_fetches"] += 1
        self.fetch_io["miss_bytes"] += hi - lo
        return self.fetch(file, lo, hi)

    def _meta(self, file: str) -> pq.FileMetaData:
        if file not in self._md:
            self._md[file] = pq.ParquetFile(self.Spans(self.size_of(file), [self.footer(file)])).metadata
        return self._md[file]

    def _read_rgs(self, file: str, start: int, data: bytes, rgs: list[int]) -> dict[int, list[dict]]:
        pf = pq.ParquetFile(self.Spans(self.size_of(file), [self.footer(file), (start, data)]), metadata=self._meta(file))
        return {g: pf.read_row_group(g).to_pylist() for g in rgs}

    def _read_table(self, file: str, start: int, data: bytes, rg: int) -> pa.Table:
        return pq.ParquetFile(self.Spans(self.size_of(file), [self.footer(file), (start, data)]), metadata=self._meta(file)).read_row_group(rg)

    def _load(self, tops: list[dict]) -> None:
        missing = [t for t in tops if t["rg"] not in self._entries]
        if not missing:
            return
        start, end = missing[0]["offset"], missing[-1]["offset"] + missing[-1]["length"]
        self.io["index_reads"] += 1
        self.io["index_bytes"] += end - start
        if all(t["rg"] in self._peeked or (self.raw is not None and (self, self.index_file, t["offset"]) in self.raw) for t in missing):
            for t in missing:
                self._entries[t["rg"]] = self._peek_top(t)
                self._peeked.pop(t["rg"], None)
            return
        self.fetch_io["miss_fetches"] += 1
        self.fetch_io["miss_bytes"] += end - start
        data = self.fetch(self.index_file, start, end)
        self._entries.update(self._read_rgs(self.index_file, start, data, [t["rg"] for t in missing]))

    def tops(self, lo, hi) -> list[dict]:
        """The index row groups `select(lo, hi)` loads (no cap)."""
        from bisect import bisect_left

        a = bisect_left(self._top_hi, lo)
        b = max(a, bisect_left(self._top_lo, hi))
        return self.top[a:b]

    def missing_tops(self, lo, hi) -> list[dict]:
        return [t for t in self.tops(lo, hi) if t["rg"] not in self._entries and t["rg"] not in self._peeked]

    def _peek_top(self, t: dict) -> list[dict]:
        if t["rg"] in self._entries:
            return self._entries[t["rg"]]
        if t["rg"] not in self._peeked:
            data = self._get(self.index_file, t["offset"], t["offset"] + t["length"])
            self._peeked[t["rg"]] = self._read_rgs(self.index_file, t["offset"], data, [t["rg"]])[t["rg"]]
        return self._peeked[t["rg"]]

    def _span(self, a: int, b: int) -> tuple[list[dict], list[tuple], list[tuple]]:
        """The entries of index row groups `top[a:b]` and their last / first keys, built once per span (a per-call build
        over the ~1K entries a top row group holds was most of a probe's time). The newest `SPAN_CACHE` spans are kept: each
        holds its own key tuples, and a loop's distinct spans grow with its reads (unbounded, they were the likeliest holder
        of the ~16 GiB outside DuckDB that gcs 0701's long settling grew by)."""
        s = self._spans.get((a, b))
        if s is None:
            entries = [e for t in self.top[a:b] for e in self._peek_top(t)]
            s = self._spans[(a, b)] = (entries, [(e["q_max"], e["k_max"]) for e in entries], [(e["q_min"], e["k_min"]) for e in entries])
            if len(self._spans) > SPAN_CACHE:
                self._spans.popitem(last=False)
        else:
            self._spans.move_to_end((a, b))
        return s

    def _sel(self, a: int, b: int, lo, hi) -> list[dict]:
        from bisect import bisect_left

        entries, his, los = self._span(a, b)
        a2 = bisect_left(his, lo)
        return entries[a2:max(a2, bisect_left(los, hi))]

    def peek(self, lo, hi) -> list[dict]:
        """`select(lo, hi)`'s groups, leaving the reads' state and counts as they were (planning: the loops' own `select`
        loads the index row groups and counts them, as without a plan)."""
        from bisect import bisect_left

        if self.top is None:
            return super().select(lo, hi)[0]
        a = bisect_left(self._top_hi, lo)
        b = max(a, bisect_left(self._top_lo, hi))
        return self._sel(a, b, lo, hi) if a < b else []

    def select(self, lo, hi, cap: int | None = None):
        from bisect import bisect_left

        if self.top is None:
            return super().select(lo, hi, cap)
        a = bisect_left(self._top_hi, lo)
        b = max(a, bisect_left(self._top_lo, hi))
        if a == b:
            return [], 0
        inner = sum(t["rows"] for t in self.top[a + 1:b - 1])
        if cap is not None and inner > cap:
            return None, inner
        self._load(self.top[a:b])
        sel = self._sel(a, b, lo, hi)
        return sel, sum(g["rows"] for g in sel)

    def group_rows(self, g: dict, keep: bool = True) -> tuple[pa.Table, dict[str, list]]:
        """Group `g` decoded (cached): its rows as a table, and per `q` its rows' span `[s, e)` (the group is sorted on
        `(q, key)`) with their keys, listed when first bisected. Rows become Python dicts only when a range of them is returned
        (`rows_in`): converting whole groups was most of the probes' and settling's time. `keep=False` (a bulk read, each
        group read once): a group not cached is decoded without caching it, and its prefetched bytes are dropped."""
        import numpy as np

        k = (g["file"], g["rg"])
        if k in self._groups:
            self._groups.move_to_end(k)
            self.fetch_io["group_hits"] += 1
            return self._groups[k]
        data = self._get(g["file"], g["offset"], g["offset"] + g["length"])
        if not keep and self.raw is not None:
            self.raw.pop((self, g["file"], g["offset"]))
        self.io["groups_read"] += 1
        self.io["group_bytes"] += len(data)
        t = self._read_table(g["file"], g["offset"], data, g["rg"])
        d = t.column("q").combine_chunks().dictionary_encode()  # sorted: the dictionary is in order, the indices ascending
        idx, n = d.indices.to_numpy(), len(d.dictionary)
        starts, ends = np.searchsorted(idx, np.arange(n), "left"), np.searchsorted(idx, np.arange(n), "right")
        out = (t, {v: [int(s), int(e), None] for v, s, e in zip(d.dictionary.to_pylist(), starts, ends)})
        if not keep:
            return out
        self._groups[k] = out
        if len(self._groups) > self.cap_groups:
            self._groups.popitem(last=False)
        return self._groups[k]

    def _bounds(self, lo, hi):
        """Per group meeting `[lo, hi)` (one `q`: the builder's ranges are a member's): `(table, i, j)`, its rows `[i, j)`
        the ones in range."""
        from bisect import bisect_left

        if lo[0] != hi[0]:
            raise ValueError(f"{lo!r}, {hi!r}: a range within one q")
        groups, _ = self.select(lo, hi)
        for g in groups:
            t, runs = self.group_rows(g)
            run = runs.get(lo[0])
            if run is None:
                yield t, 0, 0
                continue
            s, e, keys = run
            if keys is None:
                keys = run[2] = t.column(self.key).slice(s, e - s).to_pylist()
            yield t, s + bisect_left(keys, lo[1]), s + bisect_left(keys, hi[1])

    def rows_in(self, lo, hi) -> list[dict]:
        """The rows with `lo ≤ (q, key) < hi` (each group is sorted on it: bisected)."""
        out = []
        for t, i, j in self._bounds(lo, hi):
            if j > i:
                out += t.slice(i, j - i).to_pylist()
        return out

    def has_in(self, lo, hi) -> bool:
        """Is some row in `[lo, hi)`? (`rows_in`'s groups decoded, as it does; no rows converted.)"""
        return any([j > i for _, i, j in self._bounds(lo, hi)])


class Tier:
    """One tier's drill files: the base generation's `drill/` or a run's `<run>/drill/`, under a local directory (or a
    bucket mount); `GcsTier` reads the same files by ranged GCS reads."""

    def __init__(self, root: Path | str, name: str | None = None, ranged=None):
        """`ranged`: `(bucket, prefix)` (a `google.cloud.storage` bucket and the tier's key prefix in it): read groups by ranged
        GCS reads, while everything else (DuckDB's reads, listings, metadata) goes through `root` (the bucket's mount)."""
        self.root = Path(root) if not isinstance(root, Path) else root
        self.name = name or str(root)
        self.ranged = ranged
        self.meta = json.loads(self.read("meta.json"))
        self._alias_table: pa.Table | None = None
        self._aliases: dict[str, str] | None = None
        self._gf: dict[tuple[str, str], CachedGroupFile | None] = {}
        self._sizes: dict[str, int] = {}

    # storage
    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def fetch(self, rel: str, lo: int, hi: int) -> bytes:
        """Bytes `[lo, hi)` of `rel` (thread-safe: the builder's prefetch calls it from a pool)."""
        if self.ranged is not None:
            b, prefix = self.ranged
            return b.blob(f"{prefix}/{rel}").download_as_bytes(start=lo, end=hi - 1, checksum=None)
        with open(self.root / rel, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    def size_of(self, rel: str) -> int:
        if rel not in self._sizes:
            self._sizes[rel] = (self.root / rel).stat().st_size
        return self._sizes[rel]

    def local(self, rel: str) -> str:
        """A path DuckDB can read."""
        return str(self.root / rel)

    def listdir(self, rel: str) -> list[str]:
        return [f"{rel}/{p.name}" for p in sorted((self.root / rel).glob("*.parquet"))]

    # contents
    @property
    def rg(self) -> int:
        return int(self.meta["rg"])

    def table(self, rel: str, columns: list[str] | None = None) -> pa.Table:
        return pq.read_table(pa.BufferReader(self.read(rel)), columns=columns)

    def alias_table(self) -> pa.Table:
        """`(q, canonical)` for every long member of the tier (empty without `aliases.parquet`)."""
        if self._alias_table is None:
            self._alias_table = (self.table("aliases.parquet", ["q", "canonical"]).cast(ALIAS_SCHEMA) if self.exists("aliases.parquet")
                                 else ALIAS_SCHEMA.empty_table())
        return self._alias_table

    def aliases(self) -> dict[str, str]:
        if self._aliases is None:
            t = self.alias_table()
            self._aliases = {k: v for k, v in zip(t.column("q").to_pylist(), t.column("canonical").to_pylist()) if k != v}
        return self._aliases

    def canon(self, t: str, kind: str) -> str:
        return self.aliases().get(t, t) if kind == "long" else t

    def group_file(self, kind: str, sub: str) -> CachedGroupFile | None:
        if (kind, sub) not in self._gf:
            top = f"{kind}-{sub}-index.top.parquet"
            self._gf[(kind, sub)] = CachedGroupFile(None, self.fetch, self.size_of, top=self.table(top), index_file=f"{kind}-{sub}-index.parquet",
                                                    key="path" if sub == "roots" else "dir") if self.exists(top) else None
        return self._gf[(kind, sub)]

    def release(self, kind: str) -> None:
        """Drop `kind`'s group files (their decoded groups, index entries and byte cache): a run's build reads one kind at a
        time, and the tiers outlive it."""
        for k in [k for k in self._gf if k[0] == kind]:
            del self._gf[k]

    def files(self, kind: str, sub: str) -> list[str]:
        return [self.local(f) for f in self.listdir(f"{kind}/{sub}")]

    def dcount(self, kind: str) -> str | None:
        rel = f"state/dcount-{kind}.parquet"
        return self.local(rel) if self.exists(rel) else None


class GcsTier(Tier):
    """A tier read from GCS (`gs://bucket/prefix/`): ranged reads, as `static_roots.gcs_drill`."""

    def __init__(self, bucket: str, prefix: str, name: str | None = None):
        from google.cloud import storage

        self.b, self.prefix = storage.Client().bucket(bucket), prefix.rstrip("/")
        self._blobs: dict[str, object] = {}
        self._tmp: Path | None = None
        super().__init__(Path(prefix), name or f"gs://{bucket}/{prefix}")

    def _blob(self, rel: str):
        if rel not in self._blobs:
            self._blobs[rel] = self.b.get_blob(f"{self.prefix}/{rel}")
        return self._blobs[rel]

    def read(self, rel: str) -> bytes:
        return self.b.blob(f"{self.prefix}/{rel}").download_as_bytes()

    def exists(self, rel: str) -> bool:
        return self._blob(rel) is not None

    def fetch(self, rel: str, lo: int, hi: int) -> bytes:
        return self._blob(rel).download_as_bytes(start=lo, end=hi - 1)

    def size_of(self, rel: str) -> int:
        return int(self._blob(rel).size)

    def local(self, rel: str) -> str:
        import tempfile

        if self._tmp is None:
            self._tmp = Path(tempfile.mkdtemp(prefix="drill-tier-"))
        p = self._tmp / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            self._blob(rel).download_to_filename(str(p))
        return str(p)

    def listdir(self, rel: str) -> list[str]:
        return sorted(x.name.removeprefix(self.prefix + "/") for x in self.b.client.list_blobs(self.b, prefix=f"{self.prefix}/{rel}/")
                      if x.name.endswith(".parquet"))


class TierReads:
    """A kind's roots across tiers, combined (smallest `vt` per `(path, usr, vf)`), through each tier's two-level index
    and its own canonical for the member."""

    def __init__(self, tiers: list[Tier], kind: str, threads: int = 0, cache_bytes: int | None = None):
        """`threads` > 1: `windows` prefetches the groups a loop will read, that many requests at once, into a byte cache of
        `cache_bytes` (default: min(2 GiB, `CACHE_RAM_FRACTION` of the RAM)); else the loops fetch each miss in turn."""
        self.kind = kind
        self.items = [(t, t.group_file(kind, "roots")) for t in tiers]
        self.stats: Counter = Counter()
        self.threads = threads
        self.fetch_io: Counter = Counter()
        self.raw = ByteCache(cache_bytes or min(MAX_CACHE_BYTES, int(ram_bytes() * CACHE_RAM_FRACTION))) if threads > 1 else None
        self.held = False
        for _, gf in self.items:
            if gf is not None:
                gf.raw = self.raw

    def rows(self, c: str, lo: str, hi: str, only: int | None = None) -> list[dict]:
        """Combined roots of member `c` with `lo ≤ path < hi` (`only`: one tier's)."""
        best: dict[tuple, dict] = {}
        for i, (t, gf) in enumerate(self.items):
            if gf is None or (only is not None and i != only):
                continue
            ci = t.canon(c, self.kind)
            for r in gf.rows_in((ci, lo), (ci, hi)):
                k = (r["path"], r["usr"], r["vf"])
                if k not in best or r["vt"] < best[k]["vt"]:
                    best[k] = r
        self.stats["rows"] += 1
        self.fetch_io["rows_out"] += len(best)
        return list(best.values())

    def any(self, c: str, lo: str, hi: str) -> bool:
        """Does some tier hold a root of `c` with `lo ≤ path < hi`? A group boundary key inside the range settles it from the
        index; otherwise the groups' rows are decoded."""
        self.stats["any"] += 1
        for t, gf in self.items:
            if gf is None:
                continue
            ci = t.canon(c, self.kind)
            L, H = (ci, lo), (ci, hi)
            sel, _ = gf.select(L, H)
            if not sel:
                continue
            if any(L <= (g["q_min"], g["k_min"]) < H or L <= (g["q_max"], g["k_max"]) < H for g in sel):
                self.stats["any_index"] += 1
                return True
            self.stats["any_decoded"] += 1
            if gf.has_in(L, H):
                return True
        return False

    def tables(self, c: str, lo: str, hi: str):
        """Raw tier slices for bulk insertion; the consumer must combine duplicate roots by minimum vt. Each group is read
        once (not cached), its prefetch windowed here; under `held` (`hold`: the caller prefetched a plan of several ranges,
        which may share groups) decoded groups are cached and their bytes kept, until the caller releases them."""
        self.stats["rows"] += 1
        if self.raw is not None:
            self.prefetch_index([(c, lo, hi)])
        for t, gf in self.items:
            if gf is not None:
                ci = t.canon(c, self.kind)
                # Each group is an item: even one huge directory is prefetched in bounded windows.
                groups, _ = gf.select((ci, lo), (ci, hi))
                for a, b in [(0, len(groups))] if self.held else self.windows([[(gf, g)] for g in groups]):
                    for g in groups[a:b]:
                        tab, runs = gf.group_rows(g, keep=self.held)
                        run = runs.get(ci)
                        if run is not None:
                            s, e, _ = run
                            part = tab.slice(s, e - s)
                            if g["q_min"] == ci == g["q_max"] and lo <= g["k_min"] and g["k_max"] < hi:
                                yield part
                            else:
                                keys = part.column(gf.key)
                                matched = part.filter(and_(greater_equal(keys, lo), less(keys, hi)))
                                if matched.num_rows:
                                    yield matched

    def cache_stats(self) -> dict:
        """What the reads' caches hold now: decoded groups (and their Arrow bytes), parsed index row groups, raw bytes."""
        groups = [v for _, gf in self.items if gf is not None for v in gf._groups.values()]
        return {"decoded_groups": len(groups), "decoded_bytes": sum(t.nbytes for t, _ in groups),
                "index_groups": sum(len(gf._entries) + len(gf._peeked) for _, gf in self.items if gf is not None),
                "index_spans": sum(len(gf._spans) for _, gf in self.items if gf is not None),
                "raw_bytes": self.raw.size if self.raw is not None else 0}

    def hold(self, queries: list[tuple]):
        """Prefetch `(c, lo, hi)` `rows` plans in windows (`plan` / `windows`), yielding `(a, b)`, with `tables` under `held`;
        then drop what they held: the plans' prefetched bytes and the decoded groups."""
        plans = self.plan(queries, "rows")
        self.held = True
        try:
            yield from self.windows(plans)
        finally:
            self.held = False
            if self.raw is not None:
                for gf, g in (x for p in plans for x in p):
                    self.raw.pop((gf, g["file"], g["offset"]))
            self.drop_decoded()

    def drop_decoded(self) -> None:
        """Drop the decoded groups (each group file's LRU; the index and raw bytes stay): between loops that read different
        ranges, so one loop's working set isn't carried into the next."""
        for _, gf in self.items:
            if gf is not None:
                gf._groups.clear()

    def close(self) -> None:
        """Free what the reads hold: the byte cache (whose keys hold the group files: a cycle), each group file's decoded
        groups and index entries."""
        if self.raw is not None:
            self.raw._d.clear()
            self.raw.size = 0
        for _, gf in self.items:
            if gf is not None:
                gf.raw = None
                gf._groups.clear()
                gf._entries.clear()
                gf._peeked.clear()
                gf._spans.clear()

    def io(self) -> dict:
        out: Counter = Counter(self.stats)
        for _, gf in self.items:
            if gf is not None:
                out.update(gf.io)
        return dict(out)

    def requests(self) -> dict:
        """The requests actually made: prefetched (coalesced spans) and misses (fetched in the loops); and the caches' hits (byte
        spans served from the prefetch, `raw_hits`; decoded groups reused, `group_hits`) and the prior roots `rows` returned."""
        out: Counter = Counter(self.fetch_io)
        for _, gf in self.items:
            if gf is not None:
                out.update(gf.fetch_io)
        if self.raw is not None:
            out["evicted"] = self.raw.evicted
        return dict(out)

    # planning: the groups a loop's reads decode, from the index alone (its row groups prefetched first)

    def _ranges(self, c: str, lo: str, hi: str, only: int | None = None):
        for i, (t, gf) in enumerate(self.items):
            if gf is not None and (only is None or i == only):
                ci = t.canon(c, self.kind)
                yield gf, (ci, lo), (ci, hi)

    def plan_rows(self, c: str, lo: str, hi: str, only: int | None = None) -> list[tuple]:
        """The groups `rows(c, lo, hi, only)` decodes, as `(group file, group)`."""
        return [(gf, g) for gf, L, H in self._ranges(c, lo, hi, only) for g in gf.peek(L, H)]

    def plan_any(self, c: str, lo: str, hi: str) -> list[tuple]:
        """The groups `any(c, lo, hi)` may decode: each tier's up to one whose index settles it. A superset only where a tier's
        decoded rows settle it (the later tiers' are planned, not read)."""
        out = []
        for gf, L, H in self._ranges(c, lo, hi):
            sel = gf.peek(L, H)
            if not sel:
                continue
            if any(L <= (g["q_min"], g["k_min"]) < H or L <= (g["q_max"], g["k_max"]) < H for g in sel):
                break
            out += [(gf, g) for g in sel]
        return out

    def plan(self, queries: list[tuple], how: str) -> list[list[tuple]]:
        """Each query's plan (`how`: `rows` / `any`), its index row groups prefetched first; empty plans without prefetch."""
        if self.raw is None:
            return [[] for _ in queries]
        self.prefetch_index(queries)
        f = self.plan_rows if how == "rows" else self.plan_any
        return [f(*qq) for qq in queries]

    def _pool(self):
        from concurrent.futures import ThreadPoolExecutor

        return ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="drill-fetch")

    def _fetch_spans(self, need: list[tuple]) -> None:
        """`need`: `(group file, file, offset, length)` not cached → the byte cache, one request per run of them in a file
        (`COALESCE_GAP` apart, at most `COALESCE_SPAN`), `threads` at once; footers of their files first."""
        by_file: dict[tuple, list[tuple[int, int]]] = {}
        for gf, f, off, n in need:
            by_file.setdefault((gf, f), []).append((off, n))
        feet = [(gf, f) for gf, f in by_file if f not in gf.foot]
        runs = []
        for (gf, f), spans in by_file.items():
            spans = sorted(set(spans))
            cur = [spans[0]]
            for off, n in spans[1:]:
                end = cur[-1][0] + cur[-1][1]
                if off - end <= COALESCE_GAP and off + n - cur[0][0] <= COALESCE_SPAN:
                    cur.append((off, n))
                else:
                    runs.append((gf, f, cur))
                    cur = [(off, n)]
            runs.append((gf, f, cur))

        def get(run):
            gf, f, spans = run
            lo, hi = spans[0][0], max(o + n for o, n in spans)
            return run, lo, gf.fetch(f, lo, hi)

        with self._pool() as pool:
            for (gf, f), foot in zip(feet, pool.map(lambda x: x[0].footer(x[1]), feet)):
                gf.foot[f] = foot
            for (gf, f, spans), lo, data in pool.map(get, runs):
                self.fetch_io["prefetch_fetches"] += 1
                self.fetch_io["prefetch_bytes"] += len(data)
                for off, n in spans:
                    self.raw.put((gf, f, off), data[off - lo:off - lo + n])

    def prefetch_index(self, queries: list[tuple]) -> None:
        """The index row groups of `queries` (`(c, lo, hi[, only])`, every tier's), loaded ahead, concurrently."""
        need = {(gf, gf.index_file, t["offset"], t["length"]) for qq in queries for gf, L, H in self._ranges(*qq) if gf.top is not None
                for t in gf.missing_tops(L, H) if (gf, gf.index_file, t["offset"]) not in self.raw}
        if need:
            self._fetch_spans(sorted(need, key=lambda x: (id(x[0]), x[1], x[2])))

    def windows(self, plans: list[list[tuple]]):
        """Prefetch the groups of each item's plan (`plan_rows` / `plan_any`), in order, and yield `(a, b)`: the items whose
        groups are now cached (their new bytes within the cache's cap; an item past it alone). Without prefetch: `(0, n)`."""
        n = len(plans)
        if self.raw is None or not n:
            if n:
                yield 0, n
            return
        a = 0
        while a < n:
            need: dict[tuple, int] = {}
            size, b = 0, a
            while b < n:
                new = {(gf, g["file"], g["offset"]): g["length"] for gf, g in plans[b]
                       if (gf, g["file"], g["offset"]) not in self.raw and (gf, g["file"], g["offset"]) not in need}
                add = sum(new.values())
                if b > a and size + add > self.raw.cap:
                    break
                need.update(new)
                size += add
                b += 1
            if need:
                self._fetch_spans(sorted(((gf, f, off, ln) for (gf, f, off), ln in need.items()), key=lambda x: (id(x[0]), x[1], x[2])))
            yield a, b
            a = b


# ── Reading base ⊕ runs (the Worker's logic) ───────────────────────────────


class TieredDrill:
    """A member's filtered view at `P` over tiers (base first): dispatch by the summed roots bound against `R + 2·Σ rg`;
    roots combined across tiers (smallest `vt`); rollups newest first, down to and including a full header."""

    def __init__(self, tiers: list[Tier], kind: str, rule=None):
        self.tiers, self.kind, self.rule = tiers, kind, rule
        self.R = int(tiers[0].meta["R"])
        self.thr = self.R + 2 * sum(t.rg for t in tiers)

    def view(self, term: str, P: str, dates: list[str]) -> dict:
        from .hex_runs import occurs

        t = term.lower()
        if occurs(t, P.lower(), self.rule):
            return {"q": t, "P": P, "source": "plain", "answers": None}
        cs = [tier.canon(t, self.kind) for tier in self.tiers]
        total, sels, heavy = 0, [], False
        for tier, c in zip(self.tiers, cs):
            gf = tier.group_file(self.kind, "roots")
            sel, ub = gf.select((c, P + "/"), (c, P + "0"), cap=self.thr) if gf is not None else ([], 0)
            total += ub
            if sel is None:
                heavy = True
                break
            sels.append(sel)
        out: dict = {"q": t, "P": P, "upper": total, "tiers": len(self.tiers)}
        io: Counter = Counter()
        if not heavy and total <= self.thr:
            best: dict[tuple, dict] = {}
            for tier, c, sel in zip(self.tiers, cs, sels):
                if not sel:
                    continue
                rows, i = tier.group_file(self.kind, "roots").read((c, P + "/"), (c, P + "0"), sel)
                io.update(i)
                for r in rows:
                    k = (r["path"], r["usr"], r["vf"])
                    if k not in best or r["vt"] < best[k]["vt"]:
                        best[k] = r
            rows = list(best.values())
            out.update(source="roots", io=dict(io), rows=len(rows), answers=sr.roots_answers(rows, P, dates))
            return out
        header, cells, n = None, [], 0
        for tier, c in reversed(list(zip(self.tiers, cs))):
            gf = tier.group_file(self.kind, "rollups")
            if gf is None:
                continue
            rows, i = gf.read((c, P), (c, P + "\x00"))
            io.update(i)
            if not rows:
                continue
            if rows[0]["kind"] not in (KIND_FULL, KIND_DELTA):
                raise RuntimeError(f"({t!r}, {P!r}) in {tier.name}: rollup rows without a header")
            n += len(rows)
            header = header or rows[0]
            cells += rows[1:]
            if rows[0]["kind"] == KIND_FULL:
                break
        if header is None:
            raise RuntimeError(f"({t!r}, {P!r}): {total:,} root rows bound, but no rollup")
        out.update(source="rollup", io=dict(io), rows=n, **sr.rollup_view(header, cells, dates))
        return out


# ── Series: events ↔ running cells ─────────────────────────────────────────


def events_from_rows(rows) -> dict[int, list[int]]:
    """Roots → `{t: [Δbytes, Δobjects]}`: `+` at `vf`, `−` at `vt`."""
    ev: dict[int, list[int]] = {}
    for r in rows:
        e = ev.setdefault(r["vf"], [0, 0])
        e[0] += r["size"]
        e[1] += r["n_files"]
        if r["vt"] != OPEN:
            e = ev.setdefault(r["vt"], [0, 0])
            e[0] -= r["size"]
            e[1] -= r["n_files"]
    return ev


def events_from_cells(cells) -> dict[int, list[int]]:
    """Running cells `(vf, b, o)` → their changes."""
    ev, pb, po = {}, 0, 0
    for vf, b, o in sorted(cells):
        ev[vf] = [b - pb, o - po]
        pb, po = b, o
    return ev


def add_events(acc: dict[int, list[int]], ev: dict[int, list[int]], sign: int = 1) -> dict[int, list[int]]:
    for t, (b, o) in ev.items():
        e = acc.setdefault(t, [0, 0])
        e[0] += sign * b
        e[1] += sign * o
    return acc


def cells_from_events(ev: dict[int, list[int]]) -> list[tuple[int, int, int]]:
    """Events → running cells `(vf, b, o)`, one per time whose net change is nonzero (the rollup's cells)."""
    out, b, o = [], 0, 0
    for t in sorted(ev):
        db, do = ev[t]
        if db == 0 and do == 0:
            continue
        b, o = b + db, o + do
        out.append((t, b, o))
    return out


def live_on(rows: list[dict], D: int) -> tuple[int, int]:
    return (sum(r["size"] for r in rows if r["vf"] <= D < r["vt"]), sum(r["n_files"] for r in rows if r["vf"] <= D < r["vt"]))


def rank_key(peak: int, child: str) -> tuple[int, str]:
    """Kept-set order: larger peak bytes first, then the child's name (`rollup_sql`'s `peak DESC, child`)."""
    return (-peak, child)


# ── Full rollups of given heavy directories ────────────────────────────────


def full_rollups(con, rt: str, K: int, into: str) -> None:
    """Rollup rows into table `into` for each `(q, dir)` of `rt` — `(q, dir, k, path, usr, vf, vt, size, n_files)`: every
    root under `dir` (at depth `k`), combined — under a full header: `rollup_sql`'s rule (per child its running Σ, the K
    largest peaks kept by name, the rest summed into the remainder), each directory on its own."""
    child = "string_split(path, '/')[k + 1]"
    con.execute(f"""CREATE OR REPLACE TABLE fr_rev AS SELECT q, dir, child, t, sum(db) AS db, sum(dn) AS dn FROM (
            SELECT q, dir, {child} AS child, vf AS t, size::HUGEINT AS db, n_files::HUGEINT AS dn FROM {rt}
            UNION ALL SELECT q, dir, {child}, vt, -size::HUGEINT, -n_files::HUGEINT FROM {rt} WHERE vt <> {OPEN}
        ) GROUP BY q, dir, child, t""")
    run = "sum(db) OVER (PARTITION BY q, dir, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS b"
    con.execute(f"""CREATE OR REPLACE TABLE fr_keep AS SELECT q, dir, child FROM (
            SELECT q, dir, child, row_number() OVER (PARTITION BY q, dir ORDER BY peak DESC, child) AS r FROM (
                SELECT q, dir, child, max(b) AS peak FROM (SELECT q, dir, child, {run} FROM fr_rev) GROUP BY q, dir, child))
        WHERE r <= {K}""")
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} ({ROLLUP_COLS})")
    con.execute(f"""INSERT INTO {into}
        SELECT q, dir, {KIND_FULL}, '', (SELECT count(*) FROM fr_keep AS k WHERE k.q = h.q AND k.dir = h.dir), rows, children FROM (
            SELECT q, dir, count(*)::BIGINT AS rows, count(DISTINCT {child})::BIGINT AS children FROM {rt} GROUP BY q, dir) AS h
        UNION ALL
        SELECT q, dir, kind, child, t,
            sum(db) OVER (PARTITION BY q, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT,
            sum(dn) OVER (PARTITION BY q, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT
        FROM (SELECT q, dir, kind, child, t, sum(db) AS db, sum(dn) AS dn FROM (
                SELECT q, dir, {KIND_KEPT} AS kind, child, t, db, dn FROM fr_rev SEMI JOIN fr_keep USING (q, dir, child)
                UNION ALL SELECT q, dir, {KIND_REST}, '', t, db, dn FROM fr_rev ANTI JOIN fr_keep USING (q, dir, child))
            GROUP BY ALL HAVING sum(db) <> 0 OR sum(dn) <> 0)""")
    con.execute("DROP TABLE fr_rev; DROP TABLE fr_keep")


# ── One scan's run: delta roots ────────────────────────────────────────────


def long_day_roots(con, prev: pa.Table, new_members: list[str], sx_files: list[str], history: pa.Table | None,
                   chunk_rows: int = 1 << 24, log=err, rule=None, ck: Checkpoint | None = None) -> dict:
    """The long members' delta at a scan: digests of every member's delta roots (over the run's suffix rows `sx_files`)
    refine the previous classes `prev` `(q, canonical)` → table `cls (q, canonical)`; each class canonical's delta roots
    → table `dr` (`ROOT_COLS`). `new_members` (members first at this scan) are classes of their own, their roots their
    whole history (`member_roots` over `history`, their tiered suffix rows `(s, depth, path, usr, vf, vt, size, n_files)`
    in epoch seconds). Also table `newq (q)`. `ck`: the digests (table `dg`) are saved, or restored when saved."""
    t0 = monotonic()
    con.register("prev_in", prev.select(["q", "canonical"]))
    con.execute("CREATE OR REPLACE TABLE pm AS SELECT q, canonical FROM prev_in")
    con.unregister("prev_in")
    con.execute("CREATE OR REPLACE TABLE newq (q VARCHAR)")
    if new_members:
        insert_rows(con, "newq", [(m,) for m in new_members])
    con.execute("CREATE OR REPLACE TABLE mq AS SELECT q FROM pm")
    chunks = [(f, w) for f in sx_files for w in sr.chunk_wheres(f, chunk_rows)]
    if ck and ck.done("digests"):
        ck.load(con, "digests")
    else:
        con.execute(f"DROP TABLE IF EXISTS dg; CREATE TABLE dg ({sr.DIGEST_COLS})")
        for k, (f, where) in enumerate(chunks):
            sr.member_roots(con, sr.sx_rows_sql(f"(SELECT * FROM read_parquet({q(f)}) WHERE {where})"), "mq", "dg", agg="digest", rule=rule)
            log(f"drill long: digests, chunk {k + 1}/{len(chunks)} in {monotonic() - t0:.0f}s")
        if ck:
            ck.save(con, "digests", ("dg",))
    con.execute("""CREATE OR REPLACE TABLE cls AS
        SELECT q, min(q) OVER (PARTITION BY canonical, n, h1, h2) AS canonical FROM (
            SELECT pm.q, pm.canonical, coalesce(d.n, 0) AS n, coalesce(d.h1, 0) AS h1, coalesce(d.h2, 0) AS h2
            FROM pm LEFT JOIN (SELECT q, sum(n) AS n, sum(h1) AS h1, sum(h2) AS h2 FROM dg GROUP BY q) AS d USING (q))
        UNION ALL SELECT q, q FROM newq""")
    con.execute("CREATE OR REPLACE TABLE dm AS SELECT DISTINCT canonical AS q FROM cls WHERE canonical NOT IN (SELECT q FROM newq)")
    con.execute(f"DROP TABLE IF EXISTS dr; CREATE TABLE dr ({sr.ROOT_COLS})")
    for k, (f, where) in enumerate(chunks):
        sr.member_roots(con, sr.sx_rows_sql(f"(SELECT * FROM read_parquet({q(f)}) WHERE {where})"), "dm", "dr", rule=rule)
        log(f"drill long: delta roots, chunk {k + 1}/{len(chunks)} in {monotonic() - t0:.0f}s")
    if new_members and history is not None and history.num_rows:
        con.register("hist_in", history)
        sr.member_roots(con, "SELECT * FROM hist_in", "newq", "dr", rule=rule)
        con.unregister("hist_in")
    split = con.execute("SELECT count(DISTINCT p.canonical), count(DISTINCT c.canonical) FROM pm AS p JOIN cls AS c USING (q)").fetchone()
    doc = {"members": con.execute("SELECT count(*) FROM cls").fetchone()[0], "classes_before": split[0], "classes": split[1],
           "members_new": len(new_members), "rows": con.execute("SELECT count(*) FROM dr").fetchone()[0],
           "rows_new_members": con.execute("SELECT count(*) FROM dr WHERE q IN (SELECT q FROM newq)").fetchone()[0]}
    for t in ("pm", "mq", "dg", "dm"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    log(f"drill long: {doc['members']:,} members, {doc['classes_before']:,} → {doc['classes']:,} classes (+{len(new_members)} new), "
        f"{doc['rows']:,} roots in {monotonic() - t0:.0f}s")
    return doc


def short_day_roots(con, cdelta_files: list[str], rule=None) -> dict:
    """The one- and two-character literals' delta roots at a scan, from its version delta → table `dr`."""
    con.execute(f"DROP TABLE IF EXISTS dr; CREATE TABLE dr ({sr.ROOT_COLS})")
    con.execute("CREATE OR REPLACE TABLE newq (q VARCHAR)")
    if cdelta_files:
        lst = "[" + ", ".join(q(f) for f in cdelta_files) + "]"
        sr.short_roots(con, f"SELECT depth, path, usr, vf, vt, size, n_files FROM read_parquet({lst})", "dr", rule=rule)
    return {"rows": con.execute("SELECT count(*) FROM dr").fetchone()[0]}


def history_rows(readers, terms: list[str]) -> pa.Table:
    """Tiered suffix rows (the base and runs' `static_names.Reader`s) of each literal's range, combined (smallest `vt`),
    as `(s, depth, path, usr, vf, vt, size, n_files)` with epoch-second stamps."""
    from .static_append import combine_rows
    from .static_names import _ms

    tops = [t for t in sorted(set(terms)) if not any(t != u and t.startswith(u) for u in terms)]  # a range holds its extensions'
    rows = []
    for t in tops:
        for r in readers:
            rows += r.rows(t)[0]
    out = combine_rows(rows)
    return pa.table({
        "s": pa.array([r["s"] for r in out], pa.string()), "depth": pa.array([r["depth"] for r in out], pa.uint8()),
        "path": pa.array([r["path"] for r in out], pa.string()), "usr": pa.array([r["usr"] for r in out], pa.string()),
        "vf": pa.array([_ms(r["vf"]) // 1000 for r in out], pa.int64()), "vt": pa.array([_ms(r["vt"]) // 1000 for r in out], pa.int64()),
        "size": pa.array([r["size"] for r in out], pa.int64()), "n_files": pa.array([r["n_files"] for r in out], pa.int64()),
    })


def history_table(con, dirs: list[str], terms: list[str]) -> pa.Table:
    """`history_rows` read by DuckDB straight from the suffix files: each tier dir's (`sx/`, `sidecar.parquet`) files whose
    `s` span meets a literal's range, range-filtered (row groups pruned by their statistics), combined (smallest `vt`)."""
    tops = [t for t in sorted(set(terms)) if not any(t != u and t.startswith(u) for u in terms)]
    parts = []
    for d in dirs:
        side = pq.read_table(f"{d}/sidecar.parquet", columns=["file", "s_min", "s_max"]).to_pylist()
        span: dict[str, list[str]] = {}
        for g in side:
            e = span.setdefault(g["file"], [g["s_min"], g["s_max"]])
            e[0], e[1] = min(e[0], g["s_min"]), max(e[1], g["s_max"])
        for f, (lo, hi) in sorted(span.items()):
            for t in tops:
                if hi >= t and lo < t + "\U0010ffff":
                    parts.append(f"""SELECT s, depth, path, usr, epoch(vf)::BIGINT AS vf, epoch(vt)::BIGINT AS vt, size, n_files
                        FROM read_parquet({q(f"{d}/{f}")}) WHERE s >= {q(t)} AND s < {q(t + "\U0010ffff")} AND starts_with(s, {q(t)})""")
    if not parts:
        return history_rows([], [])
    return con.execute(f"""SELECT s, any_value(depth)::UTINYINT AS depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
            any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY s, path, usr, vf ORDER BY s, path, usr, vf""").to_arrow_table()


# ── One scan's run: rollups ────────────────────────────────────────────────


def _cmap(con, prior: list[Tier], kind: str) -> None:
    """`cmap (c, tier, ci)`: each class canonical at the scan (`cls`, long; every `dr` literal, short) → its canonical in
    each prior tier."""
    con.execute("CREATE OR REPLACE TABLE cmap (c VARCHAR, tier INTEGER, ci VARCHAR)")
    canon = "(SELECT DISTINCT canonical AS c FROM cls)" if kind == "long" else "(SELECT DISTINCT q AS c FROM dr)"
    for i, t in enumerate(prior):
        if kind == "long":
            con.register("al_in", t.alias_table())
            con.execute(f"INSERT INTO cmap SELECT c.c, {i}, coalesce(a.canonical, c.c) FROM {canon} AS c LEFT JOIN al_in AS a ON a.q = c.c")
            con.unregister("al_in")
        else:
            con.execute(f"INSERT INTO cmap SELECT c, {i}, c FROM {canon}")


def _explode(where: str = "") -> str:
    """`dr`'s rows at depth ≥ 2 once per ancestor directory: `(q, depth, path, vf, vt, size, n_files, k, dir, child)`."""
    return f"""SELECT q, depth, path, vf, vt, size, n_files, k, array_to_string(segs[1:k], '/') AS dir, segs[k + 1] AS child FROM (
            SELECT q, depth, path, vf, vt, size, n_files, string_split(path, '/') AS segs, unnest(range(1, depth)) AS k
            FROM dr WHERE depth >= 2 {where})"""


def _explode_parts(con) -> int:
    """How many parts (by `hash(q)`) the aggregations over `_explode` run in: one per `EXPLODE_PART_ROWS` of `dr`'s rows at
    depth ≥ 2, each exploded once per ancestor. Every group key leads with `q`, so a part's groups are whole."""
    rows = con.execute("SELECT count(*) FROM dr WHERE depth >= 2").fetchone()[0]
    return max(1, -(-rows // EXPLODE_PART_ROWS))


def _by_parts(con, table: str, parts: int, select) -> None:
    """Table `table` as `select(where)`'s rows over `parts` disjoint parts of `q` (`where` restricts `dr`'s rows to one), run
    one after another: an aggregation's working set is a part's, not the whole exploded `dr`'s (gcs 2026-10-16T0701's short
    kind: DuckDB couldn't pin a block at its 60 GB limit computing `tch` in one pass)."""
    for i in range(parts):
        w = f"AND hash(q) % {parts} = {i}" if parts > 1 else ""
        con.execute(f"{'CREATE OR REPLACE TABLE' if i == 0 else 'INSERT INTO'} {table} {'AS ' if i == 0 else ''}{select(w)}")


def _nop_step(name: str, t: float, **counts: int) -> float:
    return monotonic()


def day_rollups(con, prior: list[Tier], kind: str, D: int, *, R: int, K: int, floor: int, reads: TierReads, log=err,
                ck: Checkpoint | None = None, step=_nop_step) -> dict:
    """A run's rollups and `dcount` for one kind at scan `D` (epoch s), from tables `dr` (the scan's delta roots per class
    canonical; a new member's whole history), `newq` (new members), `pnew (depth, path)` (paths opened at the scan with
    no earlier version), `meas (q, dir, rows)` (the base's root rows per directory, those with ≥ `floor`) and the prior
    tiers' rollups and `dcount`s. Writes tables `rup` (rollup rows) and `dcnt` (`DCOUNT_SCHEMA`); returns counts. `ck`: each
    step that reads the prior tiers (probes, settling, base counts, newly heavy directories) saves its output, and is
    skipped (its output restored) when saved; the SQL between them reruns (deterministic, over the same tables).
    `step(name, t, **counts)` reports the sub-steps (`drill/rollups/<kind>`: up to the newly heavy directories; `drill/newly-heavy/<kind>`)."""
    t0 = monotonic()
    doc: Counter = Counter()
    _cmap(con, prior, kind)
    # today's rows under each directory
    parts = _explode_parts(con)
    _by_parts(con, "tdir", parts, lambda w: f"""SELECT q, dir, any_value(k) AS k, count(*)::BIGINT AS rows,
            count(*) FILTER (WHERE vf = {D})::BIGINT AS opens FROM ({_explode(w)}) GROUP BY q, dir""")
    # the roots new to the stack under each directory: an existing member's opens (a close record restates a root an
    # earlier tier holds), every root of a new member (its whole history is new). What heaviness (`S`) and `dcount`
    # sum, so S is the exact root count, not one inflated by the closes (specs/static-incremental-compaction.md)
    con.execute("""CREATE OR REPLACE TABLE tdir AS SELECT *, CASE WHEN q IN (SELECT q FROM newq) THEN rows ELSE opens END AS fresh
        FROM tdir""")
    # the prior state of the touched directories: each tier's rows for the member's canonical there, from the newest full header on
    con.execute(f"CREATE OR REPLACE TABLE ru (tier INTEGER, {ROLLUP_COLS})")
    for i, t in enumerate(prior):
        files = t.files(kind, "rollups")
        if files:
            lst = "[" + ", ".join(q(f) for f in files) + "]"
            con.execute(f"""INSERT INTO ru SELECT {i}, r.q, r.dir, r.kind, r.child, r.vf, r.b, r.o FROM read_parquet({lst}) AS r
                SEMI JOIN (SELECT DISTINCT ci AS q FROM cmap WHERE tier = {i}) AS m USING (q)""")
    con.execute("""CREATE OR REPLACE TABLE st AS SELECT m.c AS q, r.tier, r.dir, r.kind, r.child, r.vf, r.b, r.o
        FROM ru AS r JOIN cmap AS m ON r.tier = m.tier AND r.q = m.ci SEMI JOIN tdir AS t ON t.q = m.c AND t.dir = r.dir""")
    con.execute("DROP TABLE ru")
    con.execute(f"""CREATE OR REPLACE TABLE stf AS SELECT st.* FROM st
        JOIN (SELECT q, dir, max(tier) AS ft FROM st WHERE kind = {KIND_FULL} GROUP BY q, dir) AS f USING (q, dir) WHERE st.tier >= f.ft""")
    con.execute("DROP TABLE st")
    con.execute(f"""CREATE OR REPLACE TABLE hdr AS SELECT q, dir, arg_max(vf, tier) AS kept_n, arg_max(b, tier) AS hb, arg_max(o, tier) AS ho
        FROM stf WHERE kind IN ({KIND_FULL}, {KIND_DELTA}) GROUP BY q, dir""")
    con.execute(f"""CREATE OR REPLACE TABLE kc AS SELECT q, dir, child, arg_max(b, vf) AS b, arg_max(o, vf) AS o, max(b) AS peak
        FROM stf WHERE kind = {KIND_KEPT} GROUP BY q, dir, child""")
    con.execute(f"CREATE OR REPLACE TABLE rc AS SELECT q, dir, arg_max(b, vf) AS b, arg_max(o, vf) AS o FROM stf WHERE kind = {KIND_REST} GROUP BY q, dir")
    bad = con.execute(f"""SELECT count(*) FROM hdr LEFT JOIN (SELECT q, dir, count(*) AS n FROM kc GROUP BY q, dir) AS k USING (q, dir)
        WHERE coalesce(k.n, 0) <> hdr.kept_n OR hdr.kept_n <> least(hdr.ho, {K})""").fetchone()[0]
    if bad:
        raise RuntimeError(f"{bad} heavy directories whose kept children are not min(K, children) named cells")
    doc["heavy_touched"] = con.execute("SELECT count(*) FROM hdr").fetchone()[0]
    # per child of each touched heavy directory: today's change, and whether it is new
    _by_parts(con, "tch", parts, lambda w: f"""SELECT e.q, e.dir, e.child, bool_or(e.k + 1 = e.depth) AS rootchild,
            sum(CASE WHEN e.vf = {D} THEN e.size ELSE -e.size END)::BIGINT AS db,
            sum(CASE WHEN e.vf = {D} THEN e.n_files ELSE -e.n_files END)::BIGINT AS dn,
            count(*) FILTER (WHERE e.vf <> {D})::BIGINT AS n_close,
            count(*) FILTER (WHERE e.vf = {D} AND pn.path IS NULL)::BIGINT AS n_open_old
        FROM ({_explode("AND q NOT IN (SELECT q FROM newq) " + w)}) AS e SEMI JOIN hdr USING (q, dir)
        LEFT JOIN pnew AS pn ON pn.depth = e.depth AND pn.path = e.path
        GROUP BY e.q, e.dir, e.child""")
    con.execute("""CREATE OR REPLACE TABLE nk AS SELECT t.*, (h.ho > h.kept_n) AS has_rem FROM tch AS t ANTI JOIN kc USING (q, dir, child)
        JOIN hdr AS h USING (q, dir)""")
    # probes: a directory child of a directory with a remainder, every root under it today a new path: did a tier hold one?
    probes = con.execute("""SELECT q, dir, child FROM nk WHERE n_close = 0 AND n_open_old = 0 AND NOT rootchild AND has_rem
        ORDER BY q, dir, child""").fetchall()
    if ck and ck.done("probes"):
        ck.load(con, "probes")
    else:
        _probe(con, probes, reads, kind, log)
        if ck:
            ck.save(con, "probes", ("pr",))
    doc["probes"] = len(probes)
    doc["probes_existed"] = con.execute("SELECT count(*) FILTER (WHERE existed) FROM pr").fetchone()[0]
    con.execute("""CREATE OR REPLACE TABLE nkc AS SELECT nk.*,
            (n_close = 0 AND n_open_old = 0 AND (rootchild OR NOT has_rem OR NOT pr.existed)) AS is_new
        FROM nk LEFT JOIN pr USING (q, dir, child)""")
    odd = con.execute("SELECT count(*) FROM nkc WHERE NOT is_new AND NOT has_rem").fetchone()[0]
    if odd:
        raise RuntimeError(f"{odd} children with earlier roots outside the kept set of a directory without a remainder")
    # the kept set's K-th peak before today (`pk_prev`) and a lower bound on it after today (`pk_low`)
    con.execute("""CREATE OR REPLACE TABLE kt AS SELECT kc.q, kc.dir, min(kc.peak) AS pk_prev, min(greatest(kc.peak, kc.b + coalesce(t.db, 0))) AS pk_low
        FROM kc LEFT JOIN tch AS t USING (q, dir, child) GROUP BY kc.q, kc.dir""")
    # children that could change the kept set: existing ones outside it whose value on D can reach the new K-th, new ones
    con.execute(f"""CREATE OR REPLACE TABLE cand AS SELECT nkc.q, nkc.dir, nkc.child, nkc.is_new FROM nkc
        JOIN hdr AS h USING (q, dir) LEFT JOIN kt USING (q, dir) LEFT JOIN rc USING (q, dir)
        WHERE (NOT is_new AND nkc.db > 0 AND h.kept_n = {K} AND least(coalesce(rc.b, 0), kt.pk_prev) + nkc.db >= kt.pk_low)
           OR (is_new AND h.kept_n = {K} AND nkc.db >= kt.pk_low)""")
    con.execute(f"""CREATE OR REPLACE TABLE aff AS SELECT DISTINCT q, dir FROM cand
        UNION SELECT n.q, n.dir FROM nkc AS n JOIN hdr AS h USING (q, dir) WHERE n.is_new AND h.kept_n < {K}
            GROUP BY n.q, n.dir, h.kept_n HAVING h.kept_n + count(*) > {K}""")
    doc["candidates"] = con.execute("SELECT count(*) FROM cand WHERE NOT is_new").fetchone()[0]
    doc["affected"] = con.execute("SELECT count(*) FROM aff").fetchone()[0]
    # unaffected: the kept set is unchanged but for new children joining it (when they all fit)
    con.execute(f"CREATE OR REPLACE TABLE rup ({ROLLUP_COLS})")
    con.execute(f"""CREATE OR REPLACE TABLE joins AS SELECT q, dir, child, db, dn FROM nkc
        WHERE is_new AND (q, dir) IN (SELECT q, dir FROM hdr WHERE kept_n < {K}) AND (q, dir) NOT IN (SELECT q, dir FROM aff)""")
    con.execute(f"""INSERT INTO rup
        SELECT h.q, h.dir, {KIND_DELTA}, '', least(h.ho + coalesce(n.new, 0), {K}), h.hb + t.opens, h.ho + coalesce(n.new, 0)
            FROM hdr AS h JOIN tdir AS t USING (q, dir)
            LEFT JOIN (SELECT q, dir, count(*) FILTER (WHERE is_new) AS new FROM nkc GROUP BY q, dir) AS n USING (q, dir)
            WHERE (h.q, h.dir) NOT IN (SELECT q, dir FROM aff)
        UNION ALL SELECT t.q, t.dir, {KIND_KEPT}, t.child, {D}, kc.b + t.db, kc.o + t.dn FROM tch AS t JOIN kc USING (q, dir, child)
            WHERE (t.db <> 0 OR t.dn <> 0) AND (t.q, t.dir) NOT IN (SELECT q, dir FROM aff)
        UNION ALL SELECT q, dir, {KIND_KEPT}, child, {D}, db, dn FROM joins WHERE db <> 0 OR dn <> 0
        UNION ALL SELECT n.q, n.dir, {KIND_REST}, '', {D}, coalesce(rc.b, 0) + n.db, coalesce(rc.o, 0) + n.dn FROM (
                SELECT q, dir, sum(db)::BIGINT AS db, sum(dn)::BIGINT AS dn FROM nkc ANTI JOIN joins USING (q, dir, child) GROUP BY q, dir) AS n
            LEFT JOIN rc USING (q, dir) WHERE (n.db <> 0 OR n.dn <> 0) AND (n.q, n.dir) NOT IN (SELECT q, dir FROM aff)""")
    t1 = monotonic()
    doc["restated"] = _settle(con, D, K, reads, kind, log, ck)
    reads.drop_decoded()
    release_heap()
    if doc["affected"]:
        log(f"drill {kind}: {doc['affected']:,} directories settled one by one, {doc['restated']:,} restated in {monotonic() - t1:.0f}s")
    # directories that become heavy: S = base rows (measured, else counted) + the prior runs' + today's
    con.execute("CREATE OR REPLACE TABLE dc (tier INTEGER, q VARCHAR, dir VARCHAR, rows BIGINT)")
    for i, t in enumerate(prior):
        f = t.dcount(kind)
        if f:
            con.execute(f"INSERT INTO dc SELECT {i}, q, dir, rows FROM read_parquet({q(f)})")
    con.execute("""CREATE OR REPLACE TABLE nh AS SELECT t.q, t.dir, t.k, t.fresh AS today,
            CASE WHEN t.q IN (SELECT q FROM newq) THEN 0 ELSE m.rows END AS base, coalesce(r.rows, 0) AS runs
        FROM tdir AS t ANTI JOIN hdr USING (q, dir) LEFT JOIN meas AS m USING (q, dir)
        LEFT JOIN (SELECT m.c AS q, d.dir, sum(d.rows)::BIGINT AS rows FROM dc AS d JOIN cmap AS m ON d.tier = m.tier AND d.q = m.ci GROUP BY ALL) AS r
            USING (q, dir)""")
    unknown = con.execute(f"SELECT q, dir FROM nh WHERE base IS NULL AND {floor - 1} + runs + today > {R} ORDER BY q, dir").fetchall()
    if ck and ck.done("counts"):
        ck.load(con, "counts")
    else:
        _base_counts(con, unknown, reads, kind, log)
        if ck:
            ck.save(con, "counts", ("bx",))
    doc["base_counted"] = len(unknown)
    con.execute(f"""CREATE OR REPLACE TABLE newh AS SELECT nh.q, nh.dir, nh.k, coalesce(nh.base, bx.rows) + nh.runs + nh.today AS rows
        FROM nh LEFT JOIN bx USING (q, dir) WHERE coalesce(nh.base, bx.rows) + nh.runs + nh.today > {R}""")
    doc["heavy_new"] = con.execute("SELECT count(*) FROM newh").fetchone()[0]
    t2 = step(f"drill/rollups/{kind}", t0, heavy_touched=doc["heavy_touched"], affected=doc["affected"], restated=doc["restated"])
    count = 0
    if doc["heavy_new"]:
        reads.drop_decoded()
        release_heap()
        resource_stage(f"{kind}: newly-heavy start", directories=doc["heavy_new"], **duckdb_resources(con), **reads.cache_stats())
        count = newly_heavy_rollups(con, reads, kind, K, log=log, ck=ck)
        log(f"drill {kind}: {doc['heavy_new']:,} newly heavy, {count:,} earlier tier roots inserted in {monotonic() - t0:.0f}s ({reads.io()})")
    step(f"drill/newly-heavy/{kind}", t2, directories=doc["heavy_new"], prior_roots=count)
    con.execute("CREATE OR REPLACE TABLE dcnt AS SELECT q, dir, fresh AS rows FROM tdir ANTI JOIN hdr USING (q, dir) ANTI JOIN newh USING (q, dir) WHERE fresh > 0")
    for t in ("tdir", "stf", "hdr", "kc", "rc", "tch", "nk", "nkc", "pr", "kt", "cand", "aff", "joins", "dc", "nh", "bx", "newh", "cmap"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    doc["rollup_rows"] = con.execute("SELECT count(*) FROM rup").fetchone()[0]
    doc["dcount_rows"] = con.execute("SELECT count(*) FROM dcnt").fetchone()[0]
    log(f"drill {kind}: {doc['heavy_touched']:,} heavy touched, {doc['candidates']:,} candidates, {doc['restated']:,} restated, "
        f"{doc['heavy_new']:,} newly heavy, {doc['probes']:,} probes, {doc['rollup_rows']:,} rollup rows in {monotonic() - t0:.0f}s")
    return dict(doc)


def _probe(con, probes: list[tuple], reads: TierReads, kind: str, log=err) -> None:
    """Table `pr (q, dir, child, existed)`: whether a prior tier holds a root under each probed child."""
    con.execute("CREATE OR REPLACE TABLE pr (q VARCHAR, dir VARCHAR, child VARCHAR, existed BOOLEAN)")
    if not probes:
        return
    t1 = monotonic()
    ranges = [(c, f"{d}/{x}/", f"{d}/{x}0") for c, d, x in probes]
    existed = []
    for a, b in reads.windows(reads.plan(ranges, "any")):
        existed += [reads.any(*r) for r in ranges[a:b]]
    insert_rows(con, "pr", [(c, d, x, e) for (c, d, x), e in zip(probes, existed)])
    log(f"drill {kind}: {len(probes):,} probes in {monotonic() - t1:.0f}s ({reads.io()}; requests {reads.requests()})")


def _base_counts(con, unknown: list[tuple], reads: TierReads, kind: str, log=err) -> None:
    """Table `bx (q, dir, rows)`: the base's root rows under each directory the measurement's floor left out."""
    con.execute("CREATE OR REPLACE TABLE bx (q VARCHAR, dir VARCHAR, rows BIGINT)")
    if not unknown:
        return
    t1 = monotonic()
    ranges = [(c, d + "/", d + "0", 0) for c, d in unknown]
    counts = []
    for a, b in reads.windows(reads.plan(ranges, "rows")):
        counts += [len(reads.rows(c, lo, hi, only=o)) for c, lo, hi, o in ranges[a:b]]
    insert_rows(con, "bx", [(c, d, n) for (c, d), n in zip(unknown, counts)])
    log(f"drill {kind}: {len(unknown):,} base counts below the measurement's floor in {monotonic() - t1:.0f}s")


def newly_heavy_chunks(rows: list[int], chunk_rows: int) -> list[tuple[int, int]]:
    """Consecutive `[a, b)` spans of the newly heavy directories whose estimated rows (`newh.rows`) sum to at most
    `chunk_rows`; a directory over it alone."""
    out, a, n = [], 0, 0
    for i, r in enumerate(rows):
        if i > a and n + r > chunk_rows:
            out.append((a, i))
            a, n = i, 0
        n += r
    if a < len(rows):
        out.append((a, len(rows)))
    return out


def chunk_tops(dirs: list[tuple[str, str]]) -> list[int]:
    """The indexes of `dirs` (`(q, dir)`) not under another of the same `q`: the ranges read once for the chunk."""
    have = set(dirs)
    return [i for i, (c, d) in enumerate(dirs)
            if not any((c, d[:j]) in have for j in range(len(d)) if d[j] == "/")]


def newly_heavy_rollups(
    con,
    reads: TierReads,
    kind: str,
    K: int,
    log=err,
    ck: Checkpoint | None = None,
    chunk_rows: int | None = None,
) -> int:
    """The newly heavy directories' full rollups (table `newh (q, dir, k, rows)`, `rows` the stored root rows estimated
    under the directory) into `rup`, in chunks of consecutive `(q, dir)` of at most `chunk_rows` estimated rows (a
    directory over it alone; default `NEWLY_HEAVY_CHUNK_ROWS`), never a global expanded GROUP BY.

    Per chunk: the prior tiers' roots of each top range (a directory not under another of the chunk's, same member) are
    read once, the chunk's groups prefetched together (`TierReads.hold`, concurrent, not one directory's
    round trips at a time), today's rows (`dr`; a new member's whole history) taken per top range, and each directory's
    roots cut from them by range; combined (rows equal on `(q, dir, path, usr, vf)` are one root, the smallest `vt`) and
    rolled up (`full_rollups`) in one statement each for the chunk. Each directory's rows depend on it alone, so the rows
    equal a directory-at-a-time loop's at any `chunk_rows`.
    `ck`: the rollup rows so far are saved as parts (`checkpoint.Parts`, per chunk), and a rerun appends the saved ones and
    continues at the first directory they don't cover. Returns the prior roots this call read (a resumed loop's: those of
    the chunks it built)."""
    directories = con.execute("""SELECT q, dir, k, q IN (SELECT q FROM newq) AS is_new, rows FROM newh ORDER BY q, dir""").fetchall()
    parts = Parts(ck, "heavy", ROLLUP_ARROW)
    con.execute(f"CREATE TABLE IF NOT EXISTS rup ({ROLLUP_COLS})")
    if parts.loaded is not None:
        _append_arrow(con, "rup", parts.loaded)
    if parts.start == len(directories):
        return 0
    resource_stage(f"{kind}: newly-heavy prepare", directories=len(directories))
    con.execute("""CREATE OR REPLACE TABLE newh_today AS SELECT d.* FROM dr AS d
        SEMI JOIN (SELECT DISTINCT q FROM newh) AS h USING (q) ORDER BY q, path""")
    total = 0
    t0 = monotonic()
    con.execute(f"CREATE OR REPLACE TABLE nhr ({ROLLUP_COLS})")
    todo = directories[parts.start:]
    chunks = [(a + parts.start, b + parts.start) for a, b in newly_heavy_chunks([r[4] for r in todo], chunk_rows or NEWLY_HEAVY_CHUNK_ROWS)]
    for n, (a, b) in enumerate(chunks):
        chunk = directories[a:b]
        progress = dict(directory=b, directories=len(directories), chunk=n + 1, chunks=len(chunks), chunk_directories=b - a)
        con.execute("CREATE OR REPLACE TABLE nhc (q VARCHAR, dir VARCHAR, k INTEGER, lo VARCHAR, hi VARCHAR)")
        insert_rows(con, "nhc", [(c, d, k, d + "/", d + "0") for c, d, k, _, _ in chunk])
        tops = [chunk[i] for i in chunk_tops([(c, d) for c, d, *_ in chunk])]
        con.execute("CREATE OR REPLACE TABLE rtr (q VARCHAR, dir VARCHAR, k INTEGER, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT)")
        resource_stage(f"{kind}: newly-heavy history", **progress)
        old = [(c, d, k) for c, d, k, is_new, _ in tops if not is_new]
        count = 0
        for x, y in reads.hold([(c, d + "/", d + "0") for c, d, _ in old]):
            count += insert_prior_roots(con, reads, old[x:y], stage=f"{kind}: newly-heavy history")
        total += count
        resource_stage(f"{kind}: newly-heavy combine", **progress, inserted_rows=count)
        con.execute(f"CREATE OR REPLACE TABLE tday ({sr.ROOT_COLS})")
        for c, d, *_ in tops:
            con.execute(f"INSERT INTO tday SELECT * FROM newh_today WHERE q = {q(c)} AND path >= {q(d + '/')} AND path < {q(d + '0')}")
        con.execute("""CREATE OR REPLACE TABLE rtn AS SELECT h.q, h.dir, any_value(h.k) AS k, r.path, r.usr, r.vf, min(r.vt) AS vt,
                any_value(r.size) AS size, any_value(r.n_files) AS n_files FROM (
                SELECT q, path, usr, vf, vt, size, n_files FROM rtr UNION ALL
                SELECT q, path, usr, vf, vt, size, n_files FROM tday) AS r
            JOIN nhc AS h ON r.q = h.q AND r.path >= h.lo AND r.path < h.hi
            GROUP BY h.q, h.dir, r.path, r.usr, r.vf""")
        con.execute("DROP TABLE rtr; DROP TABLE tday")
        resource_stage(f"{kind}: newly-heavy rollups", **progress, inserted_rows=count)
        full_rollups(con, "rtn", K, "nhr")
        con.execute("DROP TABLE rtn; DROP TABLE nhc")
        if ck:
            parts.add(con.execute("SELECT * FROM nhr").to_arrow_table(), b)
        con.execute("INSERT INTO rup SELECT * FROM nhr; DELETE FROM nhr")
        log(f"drill {kind}: newly-heavy directories {b}/{len(directories)} (chunk {n + 1}/{len(chunks)}: {b - a} directories, "
            f"{len(old)} ranges read), {total:,} prior roots, {monotonic() - t0:.0f}s")
    parts.flush(len(directories))
    con.execute("DROP TABLE newh_today; DROP TABLE nhr")
    return total


def _append_arrow(con, table: str, t: pa.Table) -> None:
    con.register("_append_in", t)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM _append_in")
    finally:
        con.unregister("_append_in")


def _insert_root_batch(con, tables: list[pa.Table]) -> None:
    con.register("rtr_in", pa.concat_tables(tables))
    try:
        con.execute("INSERT INTO rtr SELECT * FROM rtr_in")
    finally:
        con.unregister("rtr_in")


def duckdb_resources(con) -> dict:
    """Buffer-managed allocations and spill, sampled on the owning thread, never concurrently with SQL."""
    memory, spill = con.execute("SELECT coalesce(sum(memory_usage_bytes), 0), coalesce(sum(temporary_storage_bytes), 0) FROM duckdb_memory()").fetchone()
    return {"duckdb_bytes": int(memory), "duckdb_spill_bytes": int(spill)}


def release_heap() -> None:
    """Return the C heap's free pages to the OS (glibc `malloc_trim`; elsewhere nothing): after a step that built and freed
    many Python rows, so RSS reflects what is held, and the next step's DuckDB has the room."""
    import ctypes
    import ctypes.util

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        trim = libc.malloc_trim
    except (OSError, AttributeError):
        return
    trim(0)


def insert_prior_roots(
    con,
    reads: TierReads,
    directories: list[tuple[str, str, int]],
    batch_rows: int = 65536,
    stage: str = "newly-heavy history",
) -> int:
    """Stream historical roots into rtr. rtn combines tiers by min(vt), as with the former in-memory gather."""
    if batch_rows <= 0:
        raise ValueError("batch_rows must be positive")
    tables, buffered, total = [], 0, 0
    for i, (c, d, k) in enumerate(directories):
        for t in reads.tables(c, d + "/", d + "0"):
            offset = 0
            while offset < t.num_rows:
                n = min(t.num_rows - offset, batch_rows - buffered)
                part = t.slice(offset, n)
                tables.append(pa.table({
                    "q": pa.repeat(c, n), "dir": pa.repeat(d, n), "k": pa.repeat(pa.scalar(k, pa.int32()), n),
                    **{name: part.column(name) for name in ("path", "usr", "vf", "vt", "size", "n_files")},
                }))
                buffered += n
                offset += n
                if buffered == batch_rows:
                    _insert_root_batch(con, tables)
                    total += buffered
                    tables, buffered = [], 0
        resource_stage(stage, directory=i + 1, directories=len(directories), inserted_rows=total, buffered_rows=buffered, **duckdb_resources(con))
    if buffered:
        _insert_root_batch(con, tables)
        total += buffered
    resource_stage(stage, directories=len(directories), inserted_rows=total, buffered_rows=0)
    return total


def _settle(con, D: int, K: int, reads: TierReads, kind: str = "", log=err, ck: Checkpoint | None = None,
            chunk_rows: int | None = None) -> int:
    """The touched heavy directories whose kept set may change (`aff`): rank the kept children (peaks after today), the new
    children (their value today) and the existing ones that could enter (their exact value on D); keep the top
    `min(K, children)`. When an existing child moves in or out, restate the directory's whole history under a full header;
    else a delta header and today's cells. Appends to `rup`; returns the restated count. Logs each step: the inputs, the
    plan, and the loop (fetch vs settle seconds, rows and groups read, the caches' hits; the slowest directories). `ck`: the
    rows so far are saved as parts (`checkpoint.Parts`; each directory's rows depend on it alone), and a rerun takes the
    saved ones and settles only the directories they don't cover.

    The directories are settled in chunks of about `chunk_rows` input rows (their touched and kept children), each chunk's
    inputs loaded, settled and appended to `rup` before the next: the inputs are Python rows, and loading every directory's
    at once held ~65 GiB on gcs 2026-10-16's short kind (1,253 directories). Each directory's rows depend on it alone, so
    the output is the same at any `chunk_rows` (default `SETTLE_CHUNK_ROWS`)."""
    t0 = monotonic()
    aff = con.execute("SELECT q, dir FROM aff ORDER BY q, dir").fetchall()
    if not aff:
        return 0
    parts = Parts(ck, "settle", ROLLUP_ARROW)
    restated = 0
    if parts.loaded is not None:
        loaded = list(zip(*(c.to_pylist() for c in parts.loaded.columns)))
        restated += _settled(con, loaded)
        del loaded
    if parts.start == len(aff):
        return restated
    start = parts.start
    chunk_rows = chunk_rows or SETTLE_CHUNK_ROWS
    sizes = dict(((c, d), n) for c, d, n in con.execute("""SELECT q, dir, count(*) FROM (
            SELECT q, dir FROM nkc SEMI JOIN aff USING (q, dir) UNION ALL SELECT q, dir FROM kc SEMI JOIN aff USING (q, dir))
        GROUP BY q, dir""").fetchall())
    chunks = _settle_chunks(aff[start:], sizes, chunk_rows)
    log(f"drill {kind}: settling {len(aff) - start:,} directories, {sum(sizes.values()):,} input rows, in {len(chunks):,} chunks of ≤ {chunk_rows:,} rows "
        f"(largest directory {max(sizes.values(), default=0):,})")
    io0, rq0 = Counter(reads.io()), Counter(reads.requests())
    t_fetch = t_settle = 0.0
    slow: list[tuple] = []
    done, t_log = 0, monotonic()
    for j, chunk in enumerate(chunks):
        out: list[tuple] = []
        saved = 0
        t1 = monotonic()
        con.execute("CREATE OR REPLACE TABLE affc (q VARCHAR, dir VARCHAR)")
        insert_rows(con, "affc", chunk)
        kept: dict[tuple, list] = {}
        for c, d, child, b, o, peak, db, dn in con.execute("""SELECT kc.q, kc.dir, kc.child, kc.b, kc.o, kc.peak, coalesce(t.db, 0), coalesce(t.dn, 0)
                FROM kc SEMI JOIN affc USING (q, dir) LEFT JOIN tch AS t USING (q, dir, child) ORDER BY ALL""").fetchall():
            kept.setdefault((c, d), []).append((child, b, o, peak, db, dn))
        others: dict[tuple, list] = {}
        for c, d, child, root, db, dn, new in con.execute("SELECT q, dir, child, rootchild, db, dn, is_new FROM nkc SEMI JOIN affc USING (q, dir) ORDER BY ALL").fetchall():
            others.setdefault((c, d), []).append((child, root, db, dn, new))
        cands = {(c, d, x) for c, d, x in con.execute("SELECT q, dir, child FROM cand SEMI JOIN affc USING (q, dir) WHERE NOT is_new").fetchall()}
        hdr = {(c, d): (kn, hb, ho) for c, d, kn, hb, ho in con.execute("SELECT q, dir, kept_n, hb, ho FROM hdr SEMI JOIN affc USING (q, dir)").fetchall()}
        opens = {(c, d): n for c, d, n in con.execute("SELECT q, dir, opens FROM tdir SEMI JOIN affc USING (q, dir)").fetchall()}
        rest = {(c, d): (b, o) for c, d, b, o in con.execute("SELECT q, dir, b, o FROM rc SEMI JOIN affc USING (q, dir)").fetchall()}
        con.execute("DROP TABLE affc")
        heavy_tot = _heavy_totals(con, sorted({(c, f"{d}/{x}") for c, d, x in cands}))
        t2 = monotonic()
        ctx = _settle_ctx(con, chunk, cands)
        n_today = sum(len(v) for v in ctx["today"].values())
        log(f"drill {kind}: settling {len(chunk):,} directories ({start + done + 1:,}–{start + done + len(chunk):,}), {len(cands):,} candidates: "
            f"inputs in {t2 - t1:.0f}s, {n_today:,} of today's roots under the candidates in {monotonic() - t2:.0f}s")
        t2 = monotonic()
        plans = _settle_plan(chunk, others, cands, heavy_tot, reads)
        if reads.raw is not None:
            need = {(id(gf), g["file"], g["offset"]): g["length"] for p in plans for gf, g in p}
            log(f"drill {kind}: settling plan: {sum(map(len, plans)):,} group reads, {len(need):,} distinct groups, "
                f"{sum(need.values()):,} bytes, in {monotonic() - t2:.0f}s")
        it = reads.windows(plans)
        while True:
            t2 = monotonic()
            ab = next(it, None)
            t_fetch += monotonic() - t2
            if ab is None:
                break
            a, b = ab
            t2 = monotonic()
            for key, s, stats in _settle_dirs(chunk[a:b], kept, others, cands, hdr, opens, rest, heavy_tot, ctx, D, K, reads, out):
                slow = sorted([*slow, (s, key, stats)], key=lambda x: x[0], reverse=True)[:5]
                done += 1
                if ck:
                    parts.add(out[saved:], start + done)
                    saved = len(out)
                if start + done == len(aff) or monotonic() - t_log >= 60:
                    t_log = monotonic()
                    log(f"drill {kind}: settled {start + done:,}/{len(aff):,} directories, {t_fetch:.0f}s fetching, {t_settle + monotonic() - t2:.0f}s settling")
            t_settle += monotonic() - t2
        restated += _settled(con, out)
        del out, kept, others, cands, hdr, opens, rest, heavy_tot, ctx, plans
        release_heap()
        resource_stage(f"{kind}: settle", chunk=j + 1, chunks=len(chunks), settled=start + done, **duckdb_resources(con), **reads.cache_stats())
    io = Counter(reads.io())
    io.subtract(io0)
    rq = Counter(reads.requests())
    rq.subtract(rq0)
    hits, decodes = rq["group_hits"], io["groups_read"]
    log(f"drill {kind}: settling loop: {t_fetch:.0f}s fetching ({rq['prefetch_fetches']:,} requests, {rq['prefetch_bytes']:,} bytes; "
        f"{rq['miss_fetches']:,} misses, {rq['evicted']:,} evicted), {t_settle:.0f}s settling: {io['rows']:,} reads returning "
        f"{rq['rows_out']:,} prior roots, {decodes:,} groups decoded ({io['group_bytes']:,} bytes), decoded-group hit rate "
        f"{hits / max(1, hits + decodes):.1%}; {monotonic() - t0:.0f}s in all")
    for s, (c, d), st in slow:
        log(f"drill {kind}: settling, slow: {c!r} {d!r} {s:.1f}s, {st['candidates']:,} candidates, {st['rows_out']:,} prior roots, "
            f"{st['groups_read']:,} groups decoded ({st['group_bytes']:,} bytes)")
    parts.flush(start + done)
    return restated


def _settle_chunks(aff: list[tuple], sizes: dict[tuple, int], chunk_rows: int) -> list[list[tuple]]:
    """`aff` in order, cut into runs of directories whose input rows (`sizes`) sum to at most `chunk_rows` (a directory over
    it alone is a chunk of its own)."""
    chunks: list[list[tuple]] = []
    n = 0
    for key in aff:
        m = sizes.get(key, 0)
        if not chunks or (n + m > chunk_rows and chunks[-1]):
            chunks.append([])
            n = 0
        chunks[-1].append(key)
        n += m
    return chunks


def _settled(con, out: list[tuple]) -> int:
    """Settling's rows appended to `rup`; returns the restated count."""
    if out:
        insert_rows(con, "rup", out)
    return sum(1 for r in out if r[2] == KIND_FULL)


def _settle_plan(aff: list[tuple], others: dict, cands: set, heavy_tot: dict, reads: TierReads) -> list[list[tuple]]:
    """Per affected directory, the groups settling it reads: each candidate child's roots (`_value_on`; `_series` reads a
    subset), but a heavy one's (its cells)."""
    if reads.raw is None:
        return [[] for _ in aff]
    per = [[(c, *_child_range(d, x, root)) for x, root, _, _, new in others.get((c, d), [])
            if not new and (c, d, x) in cands and (root or (c, f"{d}/{x}") not in heavy_tot)] for c, d in aff]
    reads.prefetch_index([r for rs in per for r in rs])
    return [[g for r in rs for g in reads.plan_rows(*r)] for rs in per]


def _settle_dirs(aff, kept, others, cands, hdr, opens, rest, heavy_tot, ctx, D: int, K: int, reads: TierReads, out: list[tuple]):
    """Settle each of `aff` in turn (rows appended to `out`), yielding `(key, seconds, stats)` per directory: its candidates,
    the prior roots its reads returned, and the groups they decoded (bytes)."""
    for key in aff:
        t0, io0, rows0 = monotonic(), reads.io(), reads.fetch_io["rows_out"]
        _settle_dir(key, kept, others, cands, hdr, opens, rest, heavy_tot, ctx, D, K, reads, out)
        io = reads.io()
        yield key, monotonic() - t0, {"candidates": sum(1 for x, *_, new in others.get(key, []) if not new and (*key, x) in cands),
                                      "rows_out": reads.fetch_io["rows_out"] - rows0,
                                      "groups_read": io.get("groups_read", 0) - io0.get("groups_read", 0),
                                      "group_bytes": io.get("group_bytes", 0) - io0.get("group_bytes", 0)}


def _settle_dir(key, kept, others, cands, hdr, opens, rest, heavy_tot, ctx, D: int, K: int, reads: TierReads, out: list[tuple]) -> None:
    c, d = key
    kn, hb, ho = hdr[key]
    ks, ot = kept.get(key, []), others.get(key, [])
    pool = [(rank_key(max(peak, b + db), child), child, "kept") for child, b, o, peak, db, dn in ks]
    for child, root, db, dn, new in ot:
        if new:
            pool.append((rank_key(db, child), child, "new"))
        elif (c, d, child) in cands:
            pool.append((rank_key(_value_on(ctx, c, d, child, root, D, db, dn, reads, heavy_tot)[0], child), child, "in"))
    o_new = ho + sum(1 for *_, new in ot if new)
    top = sorted(pool)[:min(K, o_new)]
    keep = {child for _, child, _ in top}
    leaving = [child for child, *_ in ks if child not in keep]
    entering = [child for _, child, origin in top if origin == "in"]
    if leaving or entering:
        out += _restate(ctx, c, d, D, keep, ks, ot, leaving, hb + opens[key], o_new, reads)
        return
    out.append((c, d, KIND_DELTA, "", len(keep), hb + opens[key], o_new))
    for child, b, o, peak, db, dn in ks:
        if db or dn:
            out.append((c, d, KIND_KEPT, child, D, b + db, o + dn))
    rb, ro = rest.get(key, (0, 0))
    rdb = rdn = 0
    for child, root, db, dn, new in ot:
        if child in keep:
            if db or dn:
                out.append((c, d, KIND_KEPT, child, D, db, dn))
        else:
            rdb, rdn = rdb + db, rdn + dn
    if rdb or rdn:
        out.append((c, d, KIND_REST, "", D, rb + rdb, ro + rdn))


def _settle_ctx(con, aff: list[tuple], cands: set[tuple]) -> dict:
    """What settling reads, loaded in one query each (a lookup per directory would scan `dr` and `stf`): the prior cells of the
    affected directories and of the candidates' own directories, which of those are heavy, and today's rows under each candidate."""
    con.execute("CREATE OR REPLACE TABLE sk (q VARCHAR, dir VARCHAR)")
    insert_rows(con, "sk", [*aff, *sorted({(c, f"{d}/{x}") for c, d, x in cands})])
    cells: dict[tuple, dict[tuple, list]] = {}
    for c, d, kind, child, vf, b, o in con.execute(f"""SELECT q, dir, kind, child, vf, b, o FROM stf SEMI JOIN (SELECT DISTINCT q, dir FROM sk) USING (q, dir)
            WHERE kind IN ({KIND_KEPT}, {KIND_REST})""").fetchall():
        cells.setdefault((c, d), {}).setdefault((kind, child), []).append((vf, b, o))
    heavy = {(c, d) for c, d in con.execute("SELECT q, dir FROM hdr SEMI JOIN (SELECT DISTINCT q, dir FROM sk) USING (q, dir)").fetchall()}
    con.execute("DROP TABLE sk")
    return {"cells": cells, "heavy": heavy, "today": _today_rows(con, cands)}


def _today_rows(con, cands: set[tuple]) -> dict[tuple, list]:
    """Today's roots (`dr`) under each candidate child `(c, d, x)`, `(path, usr, vf, vt, size, n_files)` in that order, keyed
    by both of `_child_range`'s ranges: `(c, p, p\\0)` (the path `p = d/x` itself) and `(c, p/, p0)` (below it).

    Each root of a candidate's member is cut at the depths of that member's candidate directories and matched on `(q, dir,
    child)` by equality: rows × those depths. (A join on the ranges themselves, `q` equal and `lo ≤ path < hi`, compared every
    root of a member with every one of its candidates' ranges: on gcs 10-14, 110M long roots against 2 × 374K ranges, ~41 of
    the long kind's 50 settling minutes on 7 cores.)"""
    today: dict[tuple, list] = {(c, *_child_range(d, x, root)): [] for c, d, x in cands for root in (True, False)}
    if not cands:
        return today
    con.execute("CREATE OR REPLACE TABLE sc_ (q VARCHAR, dir VARCHAR, child VARCHAR)")
    insert_rows(con, "sc_", sorted(cands))
    t = con.execute("""SELECT e.q, e.dir, e.child, len(e.segs) = e.k + 1 AS root, e.path, e.usr, e.vf, e.vt, e.size, e.n_files FROM (
            SELECT d.*, s.k, array_to_string(d.segs[1:s.k], '/') AS dir, d.segs[s.k + 1] AS child FROM (
                SELECT q, path, usr, vf, vt, size, n_files, string_split(path, '/') AS segs FROM dr SEMI JOIN (SELECT DISTINCT q FROM sc_) USING (q)) AS d
            JOIN (SELECT DISTINCT q, len(string_split(dir, '/')) AS k FROM sc_) AS s ON s.q = d.q AND s.k < len(d.segs)) AS e
        SEMI JOIN sc_ USING (q, dir, child)
        ORDER BY e.q, e.dir, e.child, root, e.path, e.usr, e.vf, e.vt, e.size, e.n_files""").to_arrow_table()
    con.execute("DROP TABLE sc_")
    for c, d, x, root, *row in zip(*(col.to_pylist() for col in t.columns)):
        today[(c, *_child_range(d, x, root))].append(tuple(row))
    return today


def _heavy_totals(con, keys: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, int]]:
    """Those `(c, path)` that are touched heavy directories: their total before today (Σ the newest cell of each kept child
    and of the remainder)."""
    if not keys:
        return {}
    con.execute("CREATE OR REPLACE TABLE hk (q VARCHAR, dir VARCHAR)")
    insert_rows(con, "hk", keys)
    heavy = {(c, d) for c, d in con.execute("SELECT q, dir FROM hdr SEMI JOIN hk USING (q, dir)").fetchall()}
    tot = {(c, d): (int(b), int(o)) for c, d, b, o in con.execute("""SELECT q, dir, sum(b), sum(o) FROM (
            SELECT q, dir, b, o FROM kc SEMI JOIN hk USING (q, dir) UNION ALL SELECT q, dir, b, o FROM rc SEMI JOIN hk USING (q, dir))
        GROUP BY q, dir""").fetchall()}
    con.execute("DROP TABLE hk")
    return {k: tot.get(k, (0, 0)) for k in heavy}


def _child_range(d: str, x: str, root: bool) -> tuple[str, str]:
    p = f"{d}/{x}"
    return (p, p + "\x00") if root else (p + "/", p + "0")


def _combined(ctx: dict, c: str, lo: str, hi: str, reads: TierReads) -> list[dict]:
    """Member `c`'s roots with `lo ≤ path < hi`: the prior tiers' and today's, combined (smallest `vt`)."""
    best = {(r["path"], r["usr"], r["vf"]): r for r in reads.rows(c, lo, hi)}
    for path, usr, vf, vt, size, n in ctx["today"][(c, lo, hi)]:
        k = (path, usr, vf)
        if k not in best or vt < best[k]["vt"]:
            best[k] = {"path": path, "usr": usr, "vf": vf, "vt": vt, "size": size, "n_files": n}
    return list(best.values())


def _value_on(ctx: dict, c: str, d: str, x: str, root: bool, D: int, db: int, dn: int, reads: TierReads, heavy_tot: dict) -> tuple[int, int]:
    """Child `x` of `(c, d)`'s value on `D`: a heavy directory's total before today plus today's change; else from its roots."""
    if not root and (c, f"{d}/{x}") in heavy_tot:
        b, o = heavy_tot[(c, f"{d}/{x}")]
        return b + db, o + dn
    return live_on(_combined(ctx, c, *_child_range(d, x, root), reads), D)


def _series(ctx: dict, c: str, d: str, x: str, root: bool, D: int, db: int, dn: int, reads: TierReads) -> dict[int, list[int]]:
    """Child `x` of `(c, d)`'s whole history as events: a heavy directory's from its cells (the prior tiers' from the newest
    full header, plus today's change), else from its roots."""
    p = f"{d}/{x}"
    if not root and (c, p) in ctx["heavy"]:
        ev: dict[int, list[int]] = {}
        for cs in ctx["cells"].get((c, p), {}).values():
            add_events(ev, events_from_cells(cs))
        return add_events(ev, {D: [db, dn]}) if db or dn else ev
    return events_from_rows(_combined(ctx, c, *_child_range(d, x, root), reads))


def _restate(ctx, c, d, D, keep, ks, ot, leaving, b_rows, o_new, reads) -> list[tuple]:
    """`(c, d)`'s whole rollup under a full header, the kept set now `keep`."""
    cells = ctx["cells"].get((c, d), {})
    series: dict[str, dict[int, list[int]]] = {}
    for child, b, o, peak, db, dn in ks:
        ev = events_from_cells(cells.get((KIND_KEPT, child), []))
        series[child] = add_events(ev, {D: [db, dn]}) if db or dn else ev
    rem = events_from_cells(cells.get((KIND_REST, ""), []))
    for child, root, db, dn, new in ot:  # today's remainder under the old kept set: every child outside it
        if db or dn:
            add_events(rem, {D: [db, dn]})
    for child, root, db, dn, new in ot:
        if child in keep:
            series[child] = {D: [db, dn]} if new else _series(ctx, c, d, child, root, D, db, dn, reads)
            add_events(rem, series[child], -1)
    for child in leaving:
        add_events(rem, series[child])
    out = [(c, d, KIND_FULL, "", len(keep), b_rows, o_new)]
    for child in sorted(keep):
        out += [(c, d, KIND_KEPT, child, vf, b, o) for vf, b, o in cells_from_events(series[child])]
    return out + [(c, d, KIND_REST, "", vf, b, o) for vf, b, o in cells_from_events(rem)]


# ── Writing a tier ─────────────────────────────────────────────────────────


def _cut(con, table: str, file_rows: int) -> list[tuple[str | None, str | None]]:
    """`table`'s `q` ranges `[lo, hi)` of about `file_rows` rows each, cut at `q` boundaries."""
    return _cut_counts(con.execute(f"SELECT q, count(*) FROM {table} GROUP BY q ORDER BY q").fetchall(), file_rows)


def _cut_counts(counts, file_rows: int) -> list[tuple[str | None, str | None]]:
    """`_cut` over `counts` `(q, rows)` in `q` order."""
    bounds, n = [None], 0
    for qq, c in counts:
        if n >= file_rows:
            bounds.append(qq)
            n = 0
        n += c
    if n == 0 and len(bounds) == 1:
        return []
    return list(zip(bounds, [*bounds[1:], None]))


def _sorted_chunks(con, files: list[Path], threads: int = COUNT_THREADS) -> tuple[list[tuple[str, int]], list[tuple[Path, str, str]]]:
    """Files each sorted, `q`-disjoint and in `q` order (`merge_tiers`' folded ranges): their `(q, rows)` in `q` order, and
    each file with its first and last `q`. Counted `threads` files at a time (a compaction's are ~13 K measured ranges)."""
    def one(f: Path) -> list:
        cur = con.cursor()
        try:
            return cur.execute(f"SELECT q, count(*) FROM read_parquet({q(str(f))}) GROUP BY q ORDER BY q").fetchall()
        finally:
            cur.close()

    if threads > 1 and len(files) > 1:
        with ThreadPoolExecutor(min(threads, len(files))) as ex:
            got_all = list(ex.map(one, files))
    else:
        got_all = [con.execute(f"SELECT q, count(*) FROM read_parquet({q(str(f))}) GROUP BY q ORDER BY q").fetchall() for f in files]
    counts, spans = [], []
    for f, got in zip(files, got_all):
        if got:
            counts += got
            spans.append((f, got[0][0], got[-1][0]))
    return counts, spans


def _chunk_batches(spans: list[tuple[Path, str, str]], names: list[str], lo: str | None, hi: str | None, size: int = 1 << 17):
    """The rows in `q` range `[lo, hi)` of `spans` (`_sorted_chunks`'), in file order: already in order, so read, not sorted."""
    for f, first, last in spans:
        if (hi is not None and first >= hi) or (lo is not None and last < lo):
            continue
        whole = (lo is None or first >= lo) and (hi is None or last < hi)
        for b in pq.ParquetFile(f).iter_batches(batch_size=size, columns=names):
            b = pa.RecordBatch.from_arrays([b.column(c) for c in names], names=names)
            if not whole:
                qc = b.column("q")
                mask = (and_(greater_equal(qc, lo), less(qc, hi)) if lo is not None and hi is not None
                        else greater_equal(qc, lo) if lo is not None else less(qc, hi))
                b = b.filter(mask)
            if b.num_rows:
                yield b


def _overlapping(spans: list[tuple[Path, str, str]], lo: str | None, hi: str | None) -> list[tuple[Path, str, str]]:
    """The `spans` holding rows in `q` range `[lo, hi)` (what `_chunk_batches` reads; the rest it skips)."""
    return [s for s in spans if not ((hi is not None and s[1] >= hi) or (lo is not None and s[2] < lo))]


def _write_chunk_file(spans, schema: pa.Schema, lo: str | None, hi: str | None, out: Path, name: str, key: str, dic: list[str],
                      rg: int | None) -> tuple[int, pa.Table, int]:
    """One `write_kind` file from sorted chunks: rows, its group index, bytes."""
    n, t = sr.write_indexed(_chunk_batches(spans, schema.names, lo, hi), out / name, schema, key, name, dic, rg)
    return n, t, (out / name).stat().st_size


def write_kind(con, out: Path, kind: str, roots: str = "dr", rollups: str = "rup", file_rows: int = FILE_ROWS, rg: int | None = None,
               chunks: dict[str, list[Path]] | None = None, jobs: int | None = None) -> dict:
    """Tables `roots` (`ROOT_COLS`) and `rollups` (`ROLLUP_COLS`) → `out/<kind>/{roots,rollups}/r####.parquet` (sorted, `ROOT_RG`-row
    groups, files cut at `q` boundaries) and the two-level indexes `out/<kind>-{roots,rollups}-index{,.top}.parquet`.
    `chunks` `{sub: files}`: a sub's rows as files already in its order, `q`-disjoint and in `q` order (`merge_tiers`' folded
    ranges), streamed in turn instead of sorted again (gcs's 10-22 L3 carry: 69 of its drill's 185 min in this sort), `jobs`
    files at a time (default `write_jobs()`), each in a spawned process reading only its own `q` range of the chunks; the
    same bytes (the row-group size is resolved here: a process sees the modules' defaults)."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    from .static_append import _Inline

    doc = {}
    rg = rg or sr.ROOT_RG
    jobs = write_jobs() if jobs is None else jobs
    for sub, table, order, schema, key, dic in (("roots", roots, "q, path, usr, vf", sr.ROOT_SCHEMA, "path", ["q", "usr"]),
                                               ("rollups", rollups, "q, dir, kind, child, vf", sr.ROLLUP_SCHEMA, "dir", ["q", "dir", "child"])):
        cols = ", ".join(schema.names)
        idx, rows, nbytes = [], 0, 0
        heartbeat.set_stage(f"drill/write/{kind}/{sub}: cut")
        if chunks is not None and sub in chunks:
            counts, spans = _sorted_chunks(con, chunks[sub])
            cuts = _cut_counts(counts, file_rows)
        else:
            cuts = _cut(con, table, file_rows)
        prog = heartbeat.Progress(f"drill/write/{kind}/{sub}", len(cuts), unit="files")
        names = [f"{kind}/{sub}/r{i:04d}.parquet" for i in range(len(cuts))]
        if chunks is not None and sub in chunks:
            procs = max(1, min(jobs, len(cuts)))
            pool = _Inline() if procs == 1 else ProcessPoolExecutor(procs, mp_context=get_context("spawn"), initializer=_writer_init)
            with pool:
                futs = [pool.submit(_write_chunk_file, _overlapping(spans, lo, hi), schema, lo, hi, out, name, key, dic, rg)
                        for (lo, hi), name in zip(cuts, names)]
                for f in futs:
                    n, t, size = f.result()
                    rows, nbytes = rows + n, nbytes + size
                    idx.append(t)
                    prog.tick(rows=n, nbytes=size)
        else:
            def one(lo, hi, name):
                where = " AND ".join(x for x in (f"q >= {q(lo)}" if lo is not None else "", f"q < {q(hi)}" if hi is not None else "") if x) or "true"
                cur = con.cursor()
                try:
                    batches = _batches(cur, f"SELECT {cols} FROM {table} WHERE {where} ORDER BY {order}")
                    n, t = sr.write_indexed(prog.batches(batches), out / name, schema, key, name, dic, rg)
                finally:
                    cur.close()
                prog.tick(nbytes=(out / name).stat().st_size)
                return n, t, (out / name).stat().st_size

            # each file a sort of its own `q` range on a cursor (one DuckDB pool), its writer in a thread: the same bytes
            with ThreadPoolExecutor(max(1, min(SORT_WRITERS, len(cuts)))) as ex:
                for n, t, size in ex.map(lambda c: one(*c[0], c[1]), zip(cuts, names)):
                    rows, nbytes = rows + n, nbytes + size
                    idx.append(t)
        prog.done()
        t = pa.concat_tables(idx) if idx else sr.GROUP_INDEX_SCHEMA.empty_table()
        top = sr.write_index_levels(t, out / f"{kind}-{sub}-index.parquet")
        pq.write_table(top, out / f"{kind}-{sub}-index.top.parquet", compression=CODEC)
        doc[f"{kind}_{sub}"] = {"files": len(idx), "row_groups": t.num_rows, "rows": rows, "bytes": nbytes,
                                "index_bytes": (out / f"{kind}-{sub}-index.parquet").stat().st_size, "index_row_groups": top.num_rows,
                                "top_bytes": (out / f"{kind}-{sub}-index.top.parquet").stat().st_size}
    return doc


def write_state(con, out: Path, kind: str, table: str = "dcnt", queries: list[str] | None = None) -> None:
    """Table `table` (`q, dir, rows`) → `out/state/dcount-<kind>.parquet` sorted, in `STATE_RG`-row groups; `queries`: the
    sorted rows as consecutive queries instead (`merge_tiers`' `q` ranges, in order), one stream."""
    (out / "state").mkdir(parents=True, exist_ok=True)
    with pq.ParquetWriter(out / "state" / f"dcount-{kind}.parquet", DCOUNT_SCHEMA, compression=CODEC) as writer:
        pending = []
        rows = 0
        wrote = False
        sqls = queries if queries is not None else [f"SELECT q, dir, rows FROM {table} ORDER BY q, dir"]
        for batch in (b for sql in sqls for b in _batches(con, sql, STATE_BATCH_ROWS)):
            offset = 0
            while offset < batch.num_rows:
                take = min(STATE_RG - rows, batch.num_rows - offset)
                pending.append(batch.slice(offset, take))
                offset += take
                rows += take
                if rows == STATE_RG:
                    writer.write_table(pa.Table.from_batches(pending).cast(DCOUNT_SCHEMA), row_group_size=STATE_RG)
                    pending, rows, wrote = [], 0, True
        if pending:
            writer.write_table(pa.Table.from_batches(pending).cast(DCOUNT_SCHEMA), row_group_size=STATE_RG)
            wrote = True
        if not wrote:
            writer.write_table(DCOUNT_SCHEMA.empty_table())


def write_aliases(con, out: Path, table: str = "cls") -> None:
    pq.write_table(con.execute(f"SELECT q, canonical FROM {table} ORDER BY q").to_arrow_table().cast(ALIAS_SCHEMA), out / "aliases.parquet",
                   compression=CODEC)


def tier_files(meta: dict) -> list[str]:
    """Every file of a run's `drill/` (relative to it), per its `meta.json`: what `write_kind`, `write_state` and
    `write_aliases` wrote for both kinds, and the meta itself."""
    return ["meta.json", *(f for kind in KINDS for f in kind_files(meta, kind))]


def kind_files(meta: dict, kind: str) -> list[str]:
    """One kind's files of a run's `drill/` (the long kind's with the alias map)."""
    out = ["aliases.parquet"] if kind == "long" else []
    for sub in ("roots", "rollups"):
        out += [f"{kind}/{sub}/r{i:04d}.parquet" for i in range(meta[f"{kind}_{sub}"]["files"])]
        out += [f"{kind}-{sub}-index.parquet", f"{kind}-{sub}-index.top.parquet"]
    return [*out, f"state/dcount-{kind}.parquet"]


def base_meta(R: int, K: int, rg: int | None = None) -> dict:
    rg = rg or sr.ROOT_RG
    return {"R": R, "K": K, "rg": rg, "idx_rg": sr.IDX_RG, "dispatch_rows": R + 2 * rg}


# ── One scan's run, both kinds ─────────────────────────────────────────────


def build_day(con, prior: list[Tier], out: Path, *, D: int, R: int, K: int, floor: int, sx_files: list[str], cdelta_files: list[str],
              new_members: list[str], history: pa.Table | None, meas: dict[str, str], tier: dict, chunk_rows: int = 1 << 24,
              file_rows: int = FILE_ROWS, rg: int | None = None, kinds: tuple[str, ...] = KINDS, log=err, rule=None,
              threads: int = FETCH_THREADS, cache_bytes: int | None = None, checkpoint: Checkpoint | None = None, step=_nop_step) -> dict:
    """A scan's `drill/` in `out`: each kind's delta roots, rollups, indexes and `dcount` (long: and the run's alias map), and
    with both kinds `meta.json` last (one kind: its part, `meta.<kind>.json`; `join_meta` makes `meta.json` of the two). Needs
    table `pnew (depth, path)`; `meas[kind]` is a query of the base's `(q, dir, rows)` at ≥ `floor` rows. `threads`,
    `cache_bytes`: the prior tiers' reads' prefetch (`TierReads`); the output is the same without it. `checkpoint`: each
    kind's steps are saved under its `<kind>/` (`roots`: the delta roots and classes; the long kind's `digests`; then
    `day_rollups`' steps; `written`: a kind's files, when another kind follows it here), and a rerun resumes after the last
    one saved; the files are the same (`meta`'s times and `reads` counts are the rerun's). Each kind's reads of the prior
    tiers are released when it is done (`Tier.release`). `step(name, t, **counts)` reports each kind's sub-steps
    (`drill/roots/<kind>`, `day_rollups`', `drill/write/<kind>`)."""
    t0 = monotonic()
    out.mkdir(parents=True, exist_ok=True)
    meta: dict = {**tier, **base_meta(R, K, rg), "D": D, "floor": floor, "dcount": DCOUNT_FRESH}
    for i, kind in enumerate(kinds):
        t1 = monotonic()
        ck = checkpoint.sub(kind) if checkpoint else None
        if ck and ck.done("written"):
            meta.update(ck.load_files("written", out))
            continue
        km: dict = {}
        resource_stage(f"{kind}: delta roots")
        if ck and ck.done("roots"):
            km.update(ck.load(con, "roots"))
        else:
            if kind == "long":
                km["classes"] = long_day_roots(con, prior[-1].alias_table(), new_members, sx_files, history, chunk_rows, log, rule, ck)
                km["new_members_list"] = new_members
            else:
                km["short_delta"] = short_day_roots(con, cdelta_files, rule)
            if ck:
                ck.save(con, "roots", ("dr", "newq", "cls") if kind == "long" else ("dr", "newq"), km)
        if kind == "long":
            write_aliases(con, out)
        t = step(f"drill/roots/{kind}", t1, roots=con.execute("SELECT count(*) FROM dr").fetchone()[0])
        con.execute(f"CREATE OR REPLACE TABLE meas AS {meas[kind]}")
        reads = TierReads(prior, kind, threads, cache_bytes)
        resource_stage(f"{kind}: rollups")
        km[f"{kind}_day"] = day_rollups(con, prior, kind, D, R=R, K=K, floor=floor, reads=reads, log=log, ck=ck, step=step)
        km[f"{kind}_day"]["reads"] = reads.io()
        log(f"drill {kind}: requests {reads.requests()}")
        reads.close()
        del reads
        for tr in prior:
            tr.release(kind)
        t = monotonic()
        resource_stage(f"{kind}: write")
        km.update(write_kind(con, out, kind, file_rows=file_rows, rg=meta["rg"]))
        write_state(con, out, kind)
        step(f"drill/write/{kind}", t, roots=km[f"{kind}_roots"]["rows"], rup=km[f"{kind}_rollups"]["rows"])
        km[f"{kind}_day"]["s"] = round(monotonic() - t1, 1)
        meta.update(km)
        for t in ("dr", "rup", "dcnt", "meas", "newq"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        if ck and i < len(kinds) - 1:  # a rerun past this kind (on a fresh disk) restores its files
            ck.save_files("written", out, kind_files(km, kind), km)
    con.execute("DROP TABLE IF EXISTS cls")
    meta["s"] = round(monotonic() - t0, 1)
    if tuple(kinds) == KINDS:
        (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    else:
        (out / f"meta.{kinds[0]}.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


def join_meta(parts: dict[str, dict]) -> dict:
    """The run's `meta.json` from the kinds' parts (built apart): their shared fields, each kind's own, and both times."""
    out: dict = {}
    for kind in KINDS:
        for k, v in parts[kind].items():
            if k == "s":
                out[f"s_{kind}"] = v
            elif k not in out:
                out[k] = v
            elif out[k] != v and not k.startswith(kind):
                raise ValueError(f"meta parts disagree on {k!r}: {out[k]!r} vs {v!r}")
    out["s"] = max(out["s_long"], out["s_short"])
    return out


def pnew_sql(cdelta_files: list[str], history_files: list[str]) -> str:
    """Paths opened in the scan's version delta with no version before it (in the base's `cintervals` or an earlier run's `cdelta`)."""
    cd = "[" + ", ".join(q(f) for f in cdelta_files) + "]"
    hist = "[" + ", ".join(q(f) for f in history_files) + "]"
    return f"""SELECT DISTINCT depth, path FROM read_parquet({cd}) AS o WHERE op = 1
        AND NOT EXISTS (SELECT 1 FROM read_parquet({hist}) AS h WHERE h.depth = o.depth AND h.path = o.path)"""


def meas_sql(kind: str, files: list[str], top_files: list[str] = ()) -> str:
    """The base measurement's `(q, dir, rows)` (`roots-measure/dirs/`; short: `short/dirs/` plus the depth-1 partials of
    `short/top/`, summed)."""
    if not files:
        return "SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS dir, NULL::BIGINT AS rows WHERE false"
    lst = "[" + ", ".join(q(f) for f in files) + "]"
    sql = f'SELECT q, dir, "rows" FROM read_parquet({lst})'
    if top_files:
        tl = "[" + ", ".join(q(f) for f in top_files) + "]"
        sql += f' UNION ALL SELECT q, dir, sum("rows")::BIGINT FROM read_parquet({tl}) GROUP BY q, dir'
    return sql


# ── Merging tiers (the binary counter) ─────────────────────────────────────


#: A drill merge's chunks: about this many roots and rollup rows per merged-`q` range (`merge_tiers`).
MERGE_CHUNK_ROWS = 1 << 24


def _bounds(counts: list[tuple[str, int]], chunk_rows: int) -> list[str]:
    """Each `q` range's first literal (the first's is `''`) over `counts` `(q, rows)` in `q` order: ranges of at most
    `chunk_rows` rows, a literal with more a range of its own."""
    bounds, n = [""], 0
    for qq, c in counts:
        if n and n + c > chunk_rows:
            bounds.append(qq)
            n = 0
        n += c
    return bounds


def _spilled(files: list[Path], cols: str) -> str:
    return (f"SELECT {cols} FROM read_parquet([{', '.join(q(str(f)) for f in files)}], hive_partitioning = false)" if files
            else None)


def merge_tiers(con, tiers: list[Tier], out: Path, tier: dict, file_rows: int = FILE_ROWS, *, work: Path | None = None,
                chunk_rows: int = MERGE_CHUNK_ROWS, spill_parts: int | None = None, log=err) -> dict:
    """Runs' drills (oldest first) merged into one tier in `out`: the newest alias map; each input's rows re-keyed to the
    merged canonicals (a class only splits, so its canonical at an input holds the merged canonical's rows there); roots
    combined (smallest `vt`), rollups by the newest-full-header rule (the merged header full if an included input's was),
    `dcount` summed (heavy directories dropped).

    Chunked by merged `q` range, so the working set is `chunk_rows` rows, not the inputs' (gcs's 10-22 L3 carry, 8 runs
    / 78.5 GiB, spilled 1.2 TiB and filled its SSD folding them in one table): every fold is per `q` (roots per
    `(q, path, usr, vf)`, rollups per `(q, dir)`, `dcount` per `(q, dir)` against that `q`'s rollups), so each input's
    re-keyed rows are spilled once by range (zstd parquet under `work`, in at most `spill_parts` partitions of
    consecutive ranges: `static_roots.ChunkSpill`), each range folded on its own, and the writers read the folded ranges
    (`write_kind` per file range, `write_state` range by range, in `q` order). The ranges are planned from the inputs'
    roots and rollups indexes (`static_roots.index_estimates`), not by counting their rows. Byte-identical at any
    `chunk_rows` and `spill_parts`. `log`: a progress line per range at most every 30 s."""
    out.mkdir(parents=True, exist_ok=True)
    if len({t.rg for t in tiers}) != 1:
        raise ValueError(f"inputs' row groups differ: {[t.rg for t in tiers]}")
    own = work is None
    work = Path(work if work is not None else out.parent / f".{out.name}.merge")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    meta: dict = {**tier, **{k: tiers[-1].meta[k] for k in ("R", "K", "rg", "idx_rg", "dispatch_rows")}}
    if all(t.meta.get("dcount") == DCOUNT_FRESH for t in tiers):
        meta["dcount"] = DCOUNT_FRESH
    resource_stage("merge/aliases")
    con.register("al_last", tiers[-1].alias_table())
    con.execute("CREATE OR REPLACE TABLE cls AS SELECT q, canonical FROM al_last")
    con.unregister("al_last")
    write_aliases(con, out)
    for kind in KINDS:
        resource_stage(f"merge/{kind}/classes", inputs=len(tiers))
        con.execute("CREATE OR REPLACE TABLE cmap (c VARCHAR, tier INTEGER, ci VARCHAR)")
        if kind != "long":  # the short literals (the long kind's classes come from the alias maps)
            lits = [f"SELECT DISTINCT q FROM read_parquet([{', '.join(q(f) for f in t.files(kind, s))}])" for t in tiers for s in ("roots", "rollups")
                    if t.files(kind, s)]
            lits += [f"SELECT DISTINCT q FROM read_parquet({q(t.dcount(kind))})" for t in tiers if t.dcount(kind)]
            con.execute(f"CREATE OR REPLACE TABLE lit AS {' UNION '.join(lits) if lits else 'SELECT NULL::VARCHAR AS q WHERE false'}")
        for i, t in enumerate(tiers):
            if kind == "long":
                con.register("al_in", t.alias_table())
                # merged canonical c → its canonical at input i (the members of c's merged class map there to one canonical)
                con.execute(f"""INSERT INTO cmap SELECT DISTINCT m.canonical, {i}, coalesce(a.canonical, m.canonical) FROM cls AS m
                    LEFT JOIN al_in AS a ON a.q = m.canonical""")
                con.unregister("al_in")
            else:
                con.execute(f"INSERT INTO cmap SELECT q, {i}, q FROM lit")
        # each input's rows re-keyed (`cmap`): roots and rollups per merged `q`, then the ranges planned over their counts
        sources = []
        for i, t in enumerate(tiers):
            for sub in ("roots", "rollups"):
                if files := t.files(kind, sub):
                    lst = "[" + ", ".join(q(f) for f in files) + "]"
                    cols = ("m.c AS q, 0::UTINYINT AS depth, r.path, r.usr, r.vf, r.vt, r.size, r.n_files" if sub == "roots"
                            else f"{i} AS tier, m.c AS q, r.dir, r.kind, r.child, r.vf, r.b, r.o")
                    sources.append((sub, i, f"SELECT {cols} FROM read_parquet({lst}) AS r JOIN cmap AS m ON m.tier = {i} AND m.ci = r.q"))
            if t.dcount(kind):
                sources.append(("dcount", i, f"""SELECT m.c AS q, r.dir, r.rows FROM read_parquet({q(t.dcount(kind))}) AS r
                    JOIN cmap AS m ON m.tier = {i} AND m.ci = r.q"""))
        resource_stage(f"merge/{kind}/plan", inputs=len(tiers))
        # each input's rows per class estimated from its indexes, then per merged `q` (`cmap`, as the sources re-key them)
        con.execute("CREATE OR REPLACE TABLE mest (tier INTEGER, q VARCHAR, n BIGINT)")
        for i, t in enumerate(tiers):
            sr.index_estimates(con, [t.local(f"{kind}-{sub}-index.parquet") for sub in ("roots", "rollups") if t.files(kind, sub)],
                               f"SELECT ci AS q FROM cmap WHERE tier = {i}", "mest_i")
            con.execute(f"INSERT INTO mest SELECT {i}, q, n FROM mest_i")
        counts = con.execute("""SELECT m.c, sum(e.n)::BIGINT FROM mest AS e JOIN cmap AS m ON m.tier = e.tier AND m.ci = e.q
            GROUP BY m.c ORDER BY m.c""").fetchall()
        con.execute("DROP TABLE mest; DROP TABLE IF EXISTS mest_i")
        bounds = _bounds(counts, chunk_rows)
        con.execute("CREATE OR REPLACE TABLE qb (lo VARCHAR, ch INTEGER)")
        con.executemany("INSERT INTO qb VALUES (?, ?)", [(lo, k) for k, lo in enumerate(bounds)])
        kw = work / kind
        spills = {sub: sr.ChunkSpill(con, kw / sub, len(bounds), spill_parts) for sub in ("roots", "rollups", "dcount")}
        # every source row's `q` is a merged canonical (`cmap.c`): their ranges, assigned once
        con.execute("CREATE OR REPLACE TABLE qk AS SELECT DISTINCT c AS q FROM cmap")
        for sub, i, sql in sources:
            resource_stage(f"merge/{kind}/read/{sub}", tier=i, inputs=len(tiers))
            spills[sub].write(sr.ranged(f"({sql})", "qb", "ch", keys="SELECT q FROM qk"), f"t{i:03d}")
        folded = kw / "folded"
        for d in ("roots", "rollups", "dcount"):
            (folded / d).mkdir(parents=True)
        prog = Progress(f"merge {tier['key']}: drill/{kind}/fold", len(bounds), log)
        n_rows = 0
        for k in range(len(bounds)):
            resource_stage(f"merge/{kind}/fold", chunk=k, chunks=len(bounds))
            got = {sub: sp.files(k) for sub, sp in spills.items()}
            dr = _spilled(got["roots"], f"q, depth, path, usr, vf, vt, size, n_files")
            con.execute(f"""CREATE OR REPLACE TABLE dr AS SELECT q, any_value(depth) AS depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
                any_value(n_files) AS n_files FROM ({dr or f'SELECT * FROM (SELECT NULL::VARCHAR AS q, 0::UTINYINT AS depth, NULL::VARCHAR AS path, NULL::VARCHAR AS usr, NULL::BIGINT AS vf, NULL::BIGINT AS vt, NULL::BIGINT AS size, NULL::BIGINT AS n_files) WHERE false'}) GROUP BY q, path, usr, vf""")
            ru = _spilled(got["rollups"], f"tier, {', '.join(ROLLUP_COLS_NAMES)}")
            con.execute(f"CREATE OR REPLACE TABLE ru (tier INTEGER, {ROLLUP_COLS})")
            if ru:
                con.execute(f"INSERT INTO ru {ru}")
            con.execute(f"""CREATE OR REPLACE TABLE rup AS
                WITH f AS (SELECT q, dir, coalesce(max(tier) FILTER (WHERE kind = {KIND_FULL}), -1) AS ft FROM ru GROUP BY q, dir),
                     inc AS (SELECT ru.* FROM ru JOIN f USING (q, dir) WHERE ru.tier >= f.ft),
                     h AS (SELECT q, dir, bool_or(kind = {KIND_FULL}) AS is_full, arg_max(vf, tier) AS vf, arg_max(b, tier) AS b, arg_max(o, tier) AS o
                           FROM inc WHERE kind IN ({KIND_FULL}, {KIND_DELTA}) GROUP BY q, dir)
                SELECT q, dir, (CASE WHEN is_full THEN {KIND_FULL} ELSE {KIND_DELTA} END)::TINYINT AS kind, '' AS child, vf, b, o FROM h
                UNION ALL SELECT q, dir, kind, child, vf, b, o FROM inc WHERE kind IN ({KIND_KEPT}, {KIND_REST})""")
            dc = _spilled(got["dcount"], "q, dir, rows")
            con.execute(f"""CREATE OR REPLACE TABLE dcnt AS SELECT q, dir, sum(rows)::BIGINT AS rows FROM ({dc or 'SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS dir, 0::BIGINT AS rows WHERE false'}) AS dc
                ANTI JOIN (SELECT DISTINCT q, dir FROM rup) AS h USING (q, dir) GROUP BY q, dir""")
            rows = 0
            for table, d, order in (("dr", "roots", "q, path, usr, vf"), ("rup", "rollups", "q, dir, kind, child, vf"), ("dcnt", "dcount", "q, dir")):
                c = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if c:
                    con.execute(f"COPY (SELECT * FROM {table} ORDER BY {order}) TO {q(str(folded / d / f'c{k:06d}.parquet'))} (FORMAT parquet, COMPRESSION zstd)")
                rows += c
            n_rows += rows
            prog.tick(k, rows)
        for sp in spills.values():
            sp.close()
        resource_stage(f"merge/{kind}/write")
        # each range's folded files are sorted and the ranges in `q` order (`c{k:06d}`): streamed in turn, not sorted again
        meta.update(write_kind(con, out, kind, file_rows=file_rows, rg=meta["rg"],
                               chunks={d: sorted((folded / d).glob("*.parquet")) for d in ("roots", "rollups")}))
        resource_stage(f"merge/{kind}/state")
        write_state(con, out, kind, queries=[f"SELECT q, dir, rows FROM read_parquet({q(str(f))}) ORDER BY q, dir"
                                             for f in sorted((folded / "dcount").glob("*.parquet"))])
        for t in ("dr", "ru", "rup", "dcnt", "cmap", "lit", "qb", "qk"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        shutil.rmtree(kw)
    con.execute("DROP TABLE IF EXISTS cls")
    if own:
        shutil.rmtree(work, ignore_errors=True)
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


# ── A whole drill from roots (tests; the reference a run is checked against) ──


def _count(con, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def build_drill(con, out: Path, *, long_roots: str | None, short_roots: str | None, aliases: pa.Table, R: int, K: int, meta: dict | None = None,
                rollups: dict[str, str] | None = None, sorted_roots: dict[str, list[Path]] | None = None, step=None) -> dict:
    """A tier holding every root: tables `long_roots` (canonical members only) and `short_roots` (`ROOT_COLS`) → `out`
    (`static_roots.build_roots` per kind, its rows written as `write_kind` writes them), with `aliases` and `meta.json`.
    `rollups` `{kind: table}` (`ROLLUP_COLS`): a kind's rollup cells already cut (by `q` range, as `static_local`'s
    chunked build does), written as they are. `sorted_roots` `{kind: files}` (with that kind's `rollups`): its roots as
    files each sorted, `q`-disjoint and in `q` order (`static_local.measure_ranges`'), streamed (`write_kind`'s `chunks`)
    instead of sorting a roots table again; the same bytes. `step(name, t, **counts)` (optional) reports each kind's
    rollup and write (`drill/rollup/<kind>`, `drill/write/<kind>`)."""
    out.mkdir(parents=True, exist_ok=True)
    doc = {**base_meta(R, K), **(meta or {})}
    con.register("al_in", aliases)
    con.execute("CREATE OR REPLACE TABLE cls AS SELECT q, canonical FROM al_in")
    con.unregister("al_in")
    write_aliases(con, out)
    t = monotonic()
    for kind, table in (("long", long_roots), ("short", short_roots)):
        if rollups and kind in rollups:
            chunks = {"roots": sorted_roots[kind]} if sorted_roots and kind in sorted_roots else None
            doc.update(write_kind(con, out, kind, roots=table, rollups=rollups[kind], chunks=chunks))
            if step:
                t = step(f"drill/write/{kind}", t)
            continue
        con.execute(f"CREATE OR REPLACE TABLE dr AS SELECT * FROM {table}")
        con.execute(f"CREATE OR REPLACE TABLE rup AS {sr.rollup_sql(con, 'dr', R, K)}")
        if step:
            t = step(f"drill/rollup/{kind}", t, roots=_count(con, "dr"), heavy_dirs=_count(con, "hv"), rup=_count(con, "rup"))
        for tb in ("rev2", "rkeep", "hv"):
            con.execute(f"DROP TABLE IF EXISTS {tb}")
        doc.update(write_kind(con, out, kind))
        if step:
            t = step(f"drill/write/{kind}", t)
    con.execute("DROP TABLE IF EXISTS dr; DROP TABLE IF EXISTS rup; DROP TABLE IF EXISTS cls")
    (out / "meta.json").write_text(json.dumps(doc, indent=1) + "\n")
    return doc


# ── CLI ────────────────────────────────────────────────────────────────────


@group("drill")
def cli() -> None:
    """The drilldown's per-scan runs (specs/done/static-append.md, "Drilldown runs (heavy terms)")."""


def _gcs():
    from google.cloud import storage

    return storage.Client()


def _pooled_gcs(threads: int):
    """A GCS client whose connection pool holds `threads` connections (the default 10 would serialize the prefetch)."""
    from requests.adapters import HTTPAdapter

    client = _gcs()
    n = max(threads, 10)
    client._http.mount("https://", HTTPAdapter(pool_connections=n, pool_maxsize=n))
    return client


def _prior_runs(bucket: str, gen: str, date: str) -> list[dict]:
    """The live runs before `date` (the newest earlier manifest's)."""
    from .static_append import _state

    return _state(bucket, gen, date)[1]


def _new_members(mount: str, prefix: str, run: str, prev: pa.Table) -> list[str]:
    """Long literals whose catalog header is new in the run and that the previous alias map lacks."""
    import duckdb

    cells = f"{mount}/{prefix}/{run}/catalog/cells.parquet"
    heads = {r[0] for r in duckdb.connect().execute(f"SELECT q FROM read_parquet({q(cells)}) WHERE bucket = '' AND length(q) >= 3").fetchall()}
    return sorted(heads - set(prev.column("q").to_pylist()))


@cli.command("build")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-c", "--chunk-rows", default=1 << 24, type=int, help="Suffix rows per member-loop chunk")
@option("-C", "--cache-gb", type=float, help=f"Prefetched group bytes held at most (default: min(2 GiB, {CACHE_RAM_FRACTION:g} of the RAM))")
@option("-d", "--scan", "date", required=True, help="The run's scan id")
@option("-f", "--floor", "floor_rows", default=10_000, type=int, help="The base measurement's floor (`roots measure -f`)")
@option("-F", "--fuse-reads", is_flag=True, help="Read the prior tiers' groups through the mount, not by ranged GCS reads")
@option("-g", "--gen", required=True, help="Base generation")
@option("-j", "--fetch-threads", default=FETCH_THREADS, type=int, help="Concurrent group reads of the prior tiers (≤ 1: no prefetch)")
@option("-k", "--kind", default="both", type=Choice(["both", "long", "short", "task"]),
        help="Build one kind (`task`: long for Batch task 0, short for task 1): the two run as parallel tasks, the second to finish writes `meta.json`")
@option("-K", "--keep", "K", default=256, type=int, help="Children kept by name per heavy directory")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--dry-run", is_flag=True, help="Build locally; upload nothing")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="Heavy: more stored root rows under the directory")
@option("-S", "--checkpoint", "ckpt_url", envvar="STATIC_NAMES_DRILL_CHECKPOINT",
        help="Save each step under this store root (a dir or `gs://` / `r2://` / `s3://` URL; $STATIC_NAMES_DRILL_CHECKPOINT), so a "
             "rerun (a reclaimed spot task's retry) resumes after the last one saved; cleared once the drill is uploaded")
@option("-t", "--to", "to_url", help="Write here instead (`gs://bucket/prefix`, e.g. the scratch bucket for a trial), not `deltas/<D>/drill/`")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@monitor_resources
def build_cmd(bucket, chunk_rows, cache_gb, date, floor_rows, fuse_reads, gen, fetch_threads, kind, K, mount, mem, dry_run, out, threads, R,
              ckpt_url, to_url, tmp) -> None:
    """The scan's run's `drill/` (`deltas/<scan id>/drill/`, written once: refused if its files are there): delta roots, classes,
    rollups, indexes, `dcount`, `meta.json` last. Prior tiers: the base's `drill/` and the live runs' (the newest earlier
    manifest's; each must have its drill). `-S`: resumable (specs/drill-preemption.md): the task's steps are saved under
    `<root>/drill/<gen>/<scan id>/<kinds>/`."""
    import os

    kinds = KINDS if kind == "both" else (KINDS[int(os.environ.get("BATCH_TASK_INDEX", "0"))],) if kind == "task" else (kind,)
    from .static_append import run_key
    from .static_names import connect, read_json, scan_epoch, upload_tree

    prefix = f"{PREFIX}/{gen}"
    run = run_key(date, date)
    dst_bucket, dst = (to_url[5:].split("/", 1) if to_url else (bucket, f"{prefix}/{run}/drill"))
    dst = dst.rstrip("/")
    db = _gcs().bucket(dst_bucket)
    ck = drill_checkpoint(ckpt_url, gen, date, kinds, work=tmp) if ckpt_url else None
    if not dry_run and kind == "task" and (db.blob(f"{dst}/meta.json").exists() or db.blob(f"{dst}/meta.{kinds[0]}.json").exists()):
        # a rerun task (its part done, the other's maybe not): nothing to build; join if both parts are there
        joined = db.blob(f"{dst}/meta.json").exists() or _join_parts(db, dst)
        err(f"gs://{dst_bucket}/{dst}/: {kinds[0]} already built{'' if joined else ' (the other kind not yet)'}")
        if ck:  # reclaimed after its upload, before the clear
            ck.clear()
        return
    if not dry_run and (db.blob(f"{dst}/meta.json").exists() or any(db.blob(f"{dst}/meta.{k}.json").exists() for k in kinds)
                        or (kinds == KINDS and any(True for _ in _gcs().list_blobs(dst_bucket, prefix=dst + "/", max_results=1)))):
        raise SystemExit(f"gs://{dst_bucket}/{dst}/: {'/'.join(kinds)} already built (a run's drill is written once)")
    runs = _prior_runs(bucket, gen, date)
    base = Path(mount) / prefix
    gb = None if fuse_reads else _pooled_gcs(fetch_threads).bucket(bucket)
    prior = [Tier(base / rel, name, ranged=None if gb is None else (gb, f"{prefix}/{rel}"))
             for rel, name in [("drill", "base"), *((f"{r['key']}/drill", r["key"]) for r in runs)]]
    for t in prior[1:]:
        if t.meta.get("last") != t.name.split("/")[-1].split("_")[-1]:
            raise SystemExit(f"{t.name}: its drill is for {t.meta.get('last')}")
    sx = [str(p) for p in sorted((base / run / "sx").glob("*.parquet"))]
    cdelta = [str(p) for p in sorted((base / run / "cdelta").glob("*.parquet"))]
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    if len(cdelta) != ranges["k"]:
        raise SystemExit(f"{len(cdelta)} of {ranges['k']} cdelta ranges")
    days = [s for r in runs for s in r["scans"]]
    hist = [str(p) for p in sorted((base / "cintervals").glob("*.parquet"))]
    hist += [str(p) for d in days for p in sorted((base / run_key(d, d) / "cdelta").glob("*.parquet"))]
    con = connect(threads, mem, tmp)
    if ck and ck.done("inputs"):
        new_members = ck.load(con, "inputs")["new_members"]
        history = con.execute("SELECT * FROM nmh").to_arrow_table() if new_members and "long" in kinds else None
    else:
        t0 = monotonic()
        resource_stage("new paths")
        con.execute(f"CREATE OR REPLACE TABLE pnew AS {pnew_sql(cdelta, hist)}")
        err(f"drill build {date}: {con.execute('SELECT count(*) FROM pnew').fetchone()[0]:,} new paths in {monotonic() - t0:.0f}s")
        new_members = _new_members(mount, prefix, run, prior[-1].alias_table())
        history = None
        if new_members and "long" in kinds:
            resource_stage("new member history", new_members=len(new_members))
            history = history_table(con, [str(base), *(str(base / r["key"]) for r in runs), str(base / run)], new_members)
            err(f"drill build {date}: {len(new_members)} new members, {history.num_rows:,} history suffix rows")
        if ck:
            save_inputs(con, ck, new_members, history)
    con.execute("DROP TABLE IF EXISTS nmh")
    meas ={"long": meas_sql("long", [str(p) for p in sorted((base / "roots-measure" / "dirs").glob("*.parquet"))]),
            "short": meas_sql("short", [str(p) for p in sorted((base / "roots-measure" / "short" / "dirs").glob("*.parquet"))],
                              [str(p) for p in sorted((base / "roots-measure" / "short" / "top").glob("*.parquet"))])}
    outp = Path(out) / "drill"
    shutil.rmtree(outp, ignore_errors=True)
    meta = build_day(con, prior, outp, D=scan_epoch(date), R=R, K=K, floor=floor_rows, sx_files=sx, cdelta_files=cdelta,
                     new_members=new_members, history=history, meas=meas, chunk_rows=chunk_rows, rg=RUN_RG, kinds=kinds,
                     rule=sn.gen_rule_at(bucket, gen), threads=fetch_threads, cache_bytes=int(cache_gb * 2 ** 30) if cache_gb else None,
                     tier={"gen": gen, "first": date, "last": date, "level": 0, "scans": [date], "prior": [t.name for t in prior]},
                     checkpoint=ck)
    if not dry_run:
        held = {f.name: f.read_bytes() for f in outp.glob("meta*.json")}
        for f in outp.glob("meta*.json"):
            f.unlink()
        upload_tree(outp, dst_bucket, dst)
        if "meta.json" in held:
            db.blob(f"{dst}/meta.json").upload_from_string(held["meta.json"], if_generation_match=0)
        else:
            (name, body), = held.items()
            db.blob(f"{dst}/{name}").upload_from_string(body, if_generation_match=0)
            _join_parts(db, dst)
        err(f"drill build {date} ({'/'.join(kinds)}): → gs://{dst_bucket}/{dst}/")
    if ck:
        ck.clear()
        ck.close()
    print(json.dumps(meta, indent=1))


def drill_checkpoint(root: str, gen: str, scan: str, kinds: tuple[str, ...], **kw) -> Checkpoint:
    """A run drill build's checkpoint (`build -S`): `<root>/drill/<gen>/<scan id>/<kinds>/` — one per Batch task (the long
    and short kinds' tasks each clear only their own)."""
    from .blobstore import store_url

    return Checkpoint(store_url(root), f"drill/{gen}/{scan}/{'-'.join(kinds)}", code=code_version(), **kw)


def save_inputs(con, ck: Checkpoint, new_members: list[str], history: pa.Table | None) -> None:
    """The build's inputs before `build_day` (`ck`'s step `inputs`): table `pnew`, the new members and their history
    (table `nmh`, dropped after)."""
    tables = ["pnew"]
    if history is not None:
        con.register("nmh_in", history)
        con.execute("CREATE OR REPLACE TABLE nmh AS SELECT * FROM nmh_in")
        con.unregister("nmh_in")
        tables.append("nmh")
    ck.save(con, "inputs", tables, {"new_members": new_members})
    con.execute("DROP TABLE IF EXISTS nmh")


def _join_parts(db, dst: str) -> bool:
    """When both kinds' parts are there, write `meta.json` (once: a race with the other task leaves the first)."""
    from google.api_core.exceptions import PreconditionFailed

    parts = {}
    for k in KINDS:
        blob = db.blob(f"{dst}/meta.{k}.json")
        if not blob.exists():
            return False
        parts[k] = json.loads(blob.download_as_bytes())
    try:
        db.blob(f"{dst}/meta.json").upload_from_string(json.dumps(join_meta(parts), indent=1) + "\n", if_generation_match=0)
    except PreconditionFailed:
        pass
    return True


def _tiers(bucket: str, gen: str, runs: str | None) -> list[Tier]:
    """The base drill and run drills: `runs` comma-separated run keys or `gs://` drill URLs (oldest first), `-` for none,
    default the newest manifest's runs up to the first without a drill."""
    from .static_append import _latest_manifest

    prefix = f"{PREFIX}/{gen}"
    if runs == "-":
        keys = []
    elif runs:
        keys = runs.split(",")
    else:
        m = _latest_manifest(bucket, gen)
        # The tiers stop at the first run without a drill, as the Worker's (a later run's drill alone would miss its scans).
        keys = []
        for r in (m["runs"] if m else []):
            if not _gcs().bucket(bucket).blob(f"{prefix}/{r['key']}/drill/meta.json").exists():
                break
            keys.append(r["key"])
    tiers: list[Tier] = [GcsTier(bucket, f"{prefix}/drill", "base")]
    for k in keys:
        if k.startswith("gs://"):
            b, p = k[5:].split("/", 1)
            tiers.append(GcsTier(b, p.rstrip("/"), k))
        else:
            tiers.append(GcsTier(bucket, f"{prefix}/{k}/drill", k))
    return tiers


@cli.command("query")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-c", "--cases", "cases_file", required=True, help="JSON lines `{q, P}`")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan date; repeat")
@option("-g", "--gen", required=True, help="Base generation")
@option("-r", "--runs", help="Run keys or `gs://` drill URLs (comma-separated, oldest first; default: the newest manifest's runs up to the first without a drill); `-` for the base alone")
def query_cmd(bucket, cases_file, dates, gen, runs) -> None:
    """Answer drill cases over the base and its runs (the Worker's logic, GCS ranged reads): one JSON line per case."""
    tiers = _tiers(bucket, gen, runs)
    drills = {kind: TieredDrill(tiers, kind, sn.gen_rule_at(bucket, gen)) for kind in KINDS}
    for t, P in sr._cases(cases_file):
        t0 = monotonic()
        doc = drills["short" if len(t) <= 2 else "long"].view(t, P, list(dates))
        doc["s"] = round(monotonic() - t0, 3)
        print(json.dumps(doc), flush=True)


@cli.command("cases")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Base generation")
@option("-n", "--per-term", default=6, type=int, help="Directories per term: half the run's rollups (restated or new first), half its root parents")
@option("-r", "--runs", required=True, help="The run's drill: a run key or a `gs://` drill URL")
@option("-t", "--terms-file", required=True, help="Literals, one per line (a path or gs:// URL)")
@option("-x", "--new-members", default=8, type=int, help="Also this many of the run's new members (with their top directories)")
def cases_cmd(bucket, gen, per_term, runs, terms_file, new_members) -> None:
    """Verification cases `{q, P}` (JSON lines) for what a run changed, deterministic: per term, the directories whose rollup
    the run restated or built (full header) or updated (delta header), and parents of the run's roots (light reads)."""
    from .static_names import read_text

    tier = _tiers(bucket, gen, runs)[-1]
    terms = sorted({x.strip().lower() for x in read_text(terms_file).splitlines() if x.strip()})
    born = [r for r in tier.meta.get("new_members_list", [])][:new_members]
    out: list[tuple[str, str]] = []
    for t in [*terms, *born]:
        kind = "short" if len(t) <= 2 else "long"
        c = tier.canon(t, kind)
        heads: list[tuple[int, str]] = []
        gf = tier.group_file(kind, "rollups")
        if gf is not None:
            heads = sorted({(r["kind"], r["dir"]) for r in gf.read((c, ""), (c, "\U0010ffff"))[0] if r["kind"] in (KIND_FULL, KIND_DELTA)},
                           key=lambda x: (x[0] != KIND_FULL, -x[1].count("/"), x[1]))
        parents = []
        gr = tier.group_file(kind, "roots")
        if gr is not None:
            seen = Counter(r["path"].rsplit("/", 1)[0] for r in gr.read((c, ""), (c, "\U0010ffff"))[0] if "/" in r["path"])
            parents = [p for p, _ in sorted(seen.items(), key=lambda x: (-x[1], x[0])) if t not in p.lower()]
        picked = [d for _, d in heads if t not in d.lower()][:per_term - per_term // 2]
        picked += [p for p in parents if p not in picked][:per_term - len(picked)]
        out += [(t, P) for P in picked]
    for t, P in sorted(set(out)):
        print(json.dumps({"q": t, "P": P}))


if __name__ == "__main__":
    cli()
