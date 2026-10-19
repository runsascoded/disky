"""The objects' native version added to an existing interval-store generation from the raw listings, without re-scanning
(specs/lean-scans.md §3.1): `dt-cloud interval-store backfill-generation -O NEW -o STORE -s ROOT -l TEMPLATE`.

A generation built before its scans' sorts carried `generation` (`path-index -G`) has object versions without it. The
raw listings since GCS's lister began recording it (2026-10-07) hold it per object. Each object version `[vf, vt)` takes
the generation of the first scan within it whose listing has one for the object (`build`'s carry over sorts that have
it: the version's first non-NULL); a version that ended before the first such listing — a replaced or deleted object,
which no future deletion can name — stays NULL, as does every directory.

The listings are read once each, folded into one table of `(path, generation)` with the first and last scan stamps
it was listed at (`gen-index.parquet` in the work dir, sorted `(depth, path)`): every scan's generation of a path is
one of its rows, and the rows overlapping a version are the generations listed within it (a generation is never listed
again after its object is replaced). Each key range then joins its path versions to that table.

The result is a **new generation** — never the source rewritten — folded like a compaction (`interval_compact.fold_gen`:
the base and the newest manifest's runs, range files and served sorts as `build` writes them, `"generation": true` in its
`scans.json`), plus `backfill.json` (`from`, the runs folded, the scans whose listings had generations, the versions
filled). Class columns of the source are read by position under `interval_store.CLASS_COLS`' names, so a deployment's
rename of them rides the same rebuild. Nothing else moves: `CURRENT.json` still names the source (verify the new
generation — `interval-store verify`, `export` — then `interval-store current -s NEW`; `add -g NEW` appends whatever
the source gained meanwhile), and the source is left for its owner to delete.
"""
from __future__ import annotations

import fnmatch
import json
import shutil
from pathlib import Path
from time import monotonic

import pyarrow.parquet as pq
from click import IntRange, argument, command, option

from . import interval_compact as ic
from . import interval_store as ist
from . import static_names as sn
from .append_runner import LocalRunStore, latest_key
from .blobstore import Store, mirror, upload_new
from .static_local import _check_gen, workdir
from .static_names import err, q

PREFIX = ist.PREFIX
#: The fold's record in the new generation.
BACKFILL = "backfill.json"
#: Where the listings' `(path, generation)` table lands in the work dir.
GEN_INDEX = "gen-index.parquet"
#: What the fold never reads from the source (`interval_local.UNREAD`).
UNREAD = ("served/", "rd/", "digest/", "sv-digest/", "verify/")


#: Concurrent footer reads / shard copies per scan (a URL root's: each a ranged read or a whole-object GET).
IO_WORKERS = 16
#: Default stall limit (seconds) for the listing reads: no shard read, copied or folded for this long exits nonzero (a
#: scan's fold into the index runs between beats: well under this even for gcs's 600M-object listings).
STALL_S = 1800


class Watchdog:
    """Exits the process (code 3) when nothing calls `beat` for `stall_s` seconds: a read hung on a FUSE mount whose
    object went away (gcs, 2026-10-18: 30 min at 0% CPU) fails the job instead of holding it."""

    def __init__(self, stall_s: float, what: str):
        import threading

        self.stall_s, self.what, self.last, self.stop = stall_s, what, monotonic(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True) if stall_s > 0 else None

    def beat(self) -> None:
        self.last = monotonic()

    def _run(self) -> None:
        import os

        while not self.stop.wait(min(30.0, self.stall_s)):
            if monotonic() - self.last > self.stall_s:
                err(f"{self.what}: no listing read for {self.stall_s:.0f}s: stalled (a shard gone under a mount?); exiting")
                os._exit(3)

    def __enter__(self) -> Watchdog:
        if self.thread:
            self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()


#: A no-op `Watchdog`.
NO_WATCH = Watchdog(0, "")


def shard_keys(root: Store, template: str, scan: str) -> list[str]:
    """Scan `scan`'s raw listing shard keys under `root` matching `template` (`{id}` the scan id, globs allowed)."""
    pat = template.replace("{id}", scan)
    lead = pat.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
    prefix = lead.rsplit("/", 1)[0] + "/" if "/" in lead else ""  # the scan's dir: `keys` lists a bare name's parent
    return sorted(k for k in root.keys(prefix) if fnmatch.fnmatchcase(k, pat))


def listing_files(root: Store, template: str, scan: str, local: Path, keys: list[str] | None = None, watch: Watchdog = NO_WATCH) -> list[str]:
    """Scan `scan`'s raw listing shards (`keys`, default `shard_keys`) as local paths: a dir root's in place, a URL
    root's copied into `local` (keys kept; in parallel; one already there at its size kept)."""
    keys = shard_keys(root, template, scan) if keys is None else keys
    if not root.url:
        return [str(Path(root.root) / k) for k in keys]
    from concurrent.futures import ThreadPoolExecutor

    def one(k: str) -> str:
        dst = local / k
        if not (dst.is_file() and dst.stat().st_size == root.size(k)):
            root.get(k, dst)
        watch.beat()
        return str(dst)
    with ThreadPoolExecutor(IO_WORKERS) as ex:
        return list(ex.map(one, keys))


def _has_generation(root: Store, key: str) -> bool:
    """Whether shard `key`'s schema has `generation`: its footer only (a URL root's by ranged reads, nothing copied)."""
    if not root.url:
        return ist.GENERATION in pq.read_schema(str(Path(root.root) / key)).names
    with root.open(key) as fh:
        return ist.GENERATION in pq.ParquetFile(fh).schema_arrow.names


def scan_shards(root: Store, template: str, scan: str, tmp: Path, what: str, watch: Watchdog = NO_WATCH) -> list[str] | None:
    """Scan `scan`'s raw listing shards with `generation`, as local paths (`listing_files`), or None (logged) when
    nothing matches or no shard has the column. Every shard's footer is read first (in parallel; a URL root's by ranged
    reads), so a scan without generations costs no copy. A template matching files with and without it is refused: a
    stray non-listing parquet (gcs's `dir-cache/`) must not decide the scan's fate."""
    from concurrent.futures import ThreadPoolExecutor

    keys = shard_keys(root, template, scan)

    def has(k: str) -> bool:
        out = _has_generation(root, k)
        watch.beat()
        return out
    with ThreadPoolExecutor(IO_WORKERS) as ex:
        flags = list(ex.map(has, keys))
    without = [k for k, f in zip(keys, flags) if not f]
    if without and len(without) < len(keys):
        raise SystemExit(f"{scan}: {template!r} matches {len(keys)} files, {len(without)} without a `{ist.GENERATION}` column "
                         f"({', '.join(without[:3])}): narrow it to the listing shards")
    if without or not keys:
        err(f"{what}: {scan}: skipped, " + (f"no `{ist.GENERATION}` in its {len(keys)} shards" if keys else f"nothing matches {template!r}"))
        return None
    return listing_files(root, template, scan, tmp, keys, watch)


def gen_index(con, root: Store, template: str, scans: list[dict], work: Path, *, since: str | None = None, stall_s: float = STALL_S) -> dict:
    """`work/GEN_INDEX`: every distinct `(depth, path, generation)` listed by a scan whose listing (`template` under
    `root`) has a `generation` column, with the first scan stamp it was listed at (`first_ts`), sorted `(depth, path)`.
    Folded scan by scan (`scans` oldest first): a scan adds only the pairs no earlier one listed, so the table holds the
    distinct versions (gcs: ~420M), not every scan's rows (19 × ~400M: 1.2 TiB spilled, then an OOM, 2026-10-19).
    A URL root's shards are copied in one scan at a time and removed after. Returns `{scans: ids read, skipped: ids
    without generations, rows}`. `since`: scans before it are skipped unread (a deployment's listings carry `generation`
    from a known scan on). The listing reads run under a `Watchdog` (`stall_s`; 0: none)."""
    from disk_tree.listing import prepare_listing

    con.execute("CREATE OR REPLACE TABLE lg (path VARCHAR, generation BIGINT, first_ts BIGINT)")
    read, skipped = [], []
    with Watchdog(stall_s, "backfill") as watch:
        for s in scans:
            if since is not None and s["id"] < since:
                skipped.append(s["id"])
                continue
            tmp = work / "listings" / s["id"]
            files = scan_shards(root, template, s["id"], tmp, "backfill", watch)
            if files is None:
                skipped.append(s["id"])
                continue
            t0 = monotonic()
            src = prepare_listing(con, (files,))  # one source: per-file sources would keep each bucket's first shard only
            g = ist.GENERATION
            n = con.execute(f"""INSERT INTO lg SELECT c.path, c.{g}, {int(s['ts'])} FROM (
                    SELECT DISTINCT bucket || '/' || name AS path, {g} FROM {src} WHERE {g} IS NOT NULL) c
                WHERE NOT EXISTS (SELECT 1 FROM lg WHERE lg.path = c.path AND lg.{g} = c.{g})""").fetchone()[0]
            err(f"backfill: {s['id']}: {n:,} (path, generation) pairs new ({len(files)} shards, {monotonic() - t0:.1f}s)")
            read.append(s["id"])
            shutil.rmtree(tmp, ignore_errors=True)
            watch.beat()
    out = work / GEN_INDEX
    t0 = monotonic()
    err(f"backfill: writing {GEN_INDEX} ({len(read)} scans folded; sorting)")
    con.execute(f"""COPY (SELECT len(string_split(path, '/'))::UTINYINT AS depth, path, generation, first_ts
        FROM lg ORDER BY depth, path, first_ts) TO {q(str(out))} (FORMAT PARQUET, ROW_GROUP_SIZE 65536)""")
    err(f"backfill: {GEN_INDEX} written in {monotonic() - t0:.0f}s")
    con.execute("DROP TABLE lg")
    rows = con.execute(f"SELECT count(*) FROM read_parquet({q(str(out))})").fetchone()[0]
    return {"scans": read, "skipped": skipped, "rows": rows}


def filler(index: str, ranges: dict, counts: dict[str, int]) -> ic.Fill:
    """The fold's hook (`interval_compact.Fill`): each object path version `[vf, vt)` of the range takes the generation
    in effect at `vf` (the newest first listed at or before it), else, for a version that began before any listing
    had generations, the first listed within it; one first listed at or after `vt` never counts (`index`: `gen_index`'s
    file). A `(path, generation)` never comes back once replaced, so a generation's first listing bounds it. Counted in
    `counts`."""
    by_name = {f"r{r['i']:04d}": r for r in ranges["ranges"]}

    def fill(con, name: str) -> None:
        preds = " OR ".join(f"({p})" for p in sn.range_preds(by_name[name]))
        g = ist.GENERATION
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __fill AS
            SELECT p.depth, p.path, p.vf,
                   arg_min(g.{g}, CASE WHEN g.first_ts <= p.vf THEN -g.first_ts ELSE {1 << 40} + g.first_ts END) AS {g}
            FROM pv p JOIN (SELECT * FROM read_parquet({q(index)}) WHERE {preds}) g ON g.depth = p.depth AND g.path = p.path
            WHERE p.kind = 'file' AND g.first_ts < p.vt
            GROUP BY p.depth, p.path, p.vf""")
        con.execute(f"UPDATE pv SET {g} = f.{g} FROM __fill f WHERE pv.depth = f.depth AND pv.path = f.path AND pv.vf = f.vf")
        counts[name] = con.execute("SELECT count(*) FROM __fill").fetchone()[0]
        con.execute("DROP TABLE __fill")
    return fill


def backfill(store: Store, gen: str, new_gen: str, listing_root: str, template: str, work: Path, *, ranges_gen: str | None = None,
             since: str | None = None,
             stall_s: float = STALL_S, rg_rows: int = ist.SERVED_RG,
             threads: int = 4, mem: str = "4GB", jobs: int = 1) -> dict:
    """Generation `gen` of `store` folded into `new_gen` with the objects' `generation` from the raw listings (`template`
    under `listing_root`), published to the store (data first, `scans.json` last; `CURRENT.json` untouched). A staged
    build's generation may borrow its key ranges and base range files from `ranges_gen` (its deployment's `ranges_gen`):
    those are mirrored in beside its own, and the new generation holds its own copies. Returns a summary."""
    t0 = monotonic()
    prefix, new_prefix = f"{PREFIX}/{gen}", f"{PREFIX}/{new_gen}"
    if store.listing(new_prefix):
        raise SystemExit(f"{store.root}/{new_prefix}/ exists: a generation is never rewritten")
    gen_dir = work / "store" / prefix
    before = mirror(store, prefix, gen_dir, keep=lambda rel: not rel.startswith(UNREAD))
    if f"{prefix}/scans.json" not in before:
        raise SystemExit(f"{store.root}/{prefix}/scans.json: no such generation")
    if ranges_gen and ranges_gen != gen:
        if f"{prefix}/ranges.json" in before:
            raise SystemExit(f"{store.root}/{prefix}/ranges.json: the generation has its own ranges; drop -R {ranges_gen}")
        borrowed = mirror(store, f"{PREFIX}/{ranges_gen}", gen_dir, keep=lambda rel: rel == "ranges.json" or rel.split("/")[0] in ist.SUB_SCHEMA)
        if f"{PREFIX}/{ranges_gen}/ranges.json" not in borrowed:
            raise SystemExit(f"{store.root}/{PREFIX}/{ranges_gen}/ranges.json: no such ranges generation")
    elif f"{prefix}/ranges.json" not in before:
        raise SystemExit(f"{store.root}/{prefix}/ranges.json: none; a staged build's generation names its ranges with -R")
    base = json.loads((gen_dir / "scans.json").read_text())
    if ist.has_generation(base):
        raise SystemExit(f"{store.root}/{prefix}: already carries `{ist.GENERATION}`")
    lstore = LocalRunStore(gen_dir, work / "lease", gen)
    newest = latest_key(lstore.keys("manifests/"))
    runs = lstore.read_json(newest)["runs"] if newest else []
    scans = [*base["scans"], *ic.run_scans(gen_dir, gen, runs)]
    con = sn.connect(threads, mem, work / "duckdb")
    idx = gen_index(con, Store(listing_root), template, scans, work, since=since, stall_s=stall_s)
    if not idx["scans"]:
        raise SystemExit(f"no listing under {listing_root} matching {template!r} has a `{ist.GENERATION}` column")
    ranges = json.loads((gen_dir / "ranges.json").read_text())
    counts: dict[str, int] = {}
    out = work / "store" / new_prefix
    doc, rows, sorts, stats = ic.fold_gen(gen_dir, out, work, gen=gen, runs=runs, rg_rows=rg_rows, threads=threads, mem=mem, jobs=jobs,
                                          fill=filler(str(work / GEN_INDEX), ranges, counts))
    rec = {"from": gen, "manifest": newest, "runs": [r["key"] for r in runs], "scans": len(doc["scans"]), "listings": idx["scans"],
           "listed": idx["rows"], "filled": sum(counts.values())}
    (out / BACKFILL).write_text(json.dumps(rec, indent=1) + "\n")
    files = upload_new(store, out, new_prefix, {}, _publish_order)
    s = round(monotonic() - t0, 1)
    err(f"backfill {gen} → {new_gen}: {rec['filled']:,} object versions given a generation from {len(idx['scans'])} listings, "
        f"{stats['versions']:,} versions, {len(files)} files in {s}s")
    return {**rec, "gen": new_gen, "skipped": idx["skipped"], "rows": rows, "sorts": sorts, "files": len(files), "s": s}


def _publish_order(keys: list[str]) -> list[str]:
    from .interval_local import publish_order
    return publish_order(keys)


@command("backfill-generation")
@option("-g", "--gen", help="The source generation (default: the store's current, followed to its newest successor); never rewritten")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="Key ranges, then served sorts, folded this many at a time, on cursors of one DuckDB database (-p threads and -M memory shared): each ends in a serial parquet writer. The output's bytes don't depend on it")
@option("-l", "--listing", "template", required=True, help="Key template of a scan's raw listing shards under -s: `{id}` the scan id, globs allowed (e.g. `listing/{id}/*/*.parquet`)")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the readers' INDEX_R2 root")
@option("-O", "--to-gen", required=True, help="The new generation: `<store>/interval-store/TO_GEN/` (must not exist)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-R", "--ranges-gen", help="The generation holding GEN's key ranges and base range files, when GEN borrows them (a staged build: its deployment's `ranges_gen`); default GEN")
@option("-r", "--rg-rows", default=ist.SERVED_RG, type=IntRange(min=1), help="Rows per served row group")
@option("-s", "--listings", "listing_root", required=True, help="Where the raw listings are: a URL (`gs://…`: read through the API, a vanished shard an error) or a dir, the root of -l's keys; not a FUSE mount of a bucket being retired (a read can hang)")
@option("-S", "--since", help="Skip, unread, the scans before this id (their listings carry no `generation`)")
@option("-T", "--stall", "stall_s", default=STALL_S, type=IntRange(min=0), help="Exit 3 when no listing is read for this many seconds (0: never)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); the source generation is mirrored into it")
def backfill_cmd(gen, jobs, template, mem, store_root, to_gen, threads, ranges_gen, rg_rows, listing_root, since, stall_s, work) -> None:
    """Fold a generation into a new one whose object versions carry their GCS `generation`, read from the raw listings:
    each version takes the first one listed within it; versions that ended before any listing had one stay NULL. The
    source and `CURRENT.json` are left as they are: verify the new generation, then `interval-store current -s TO_GEN`.
    Prints a JSON summary."""
    _check_gen(to_gen)
    store = Store(store_root)
    if gen is not None:
        _check_gen(gen)
    else:
        gen, how = ic.resolve(store)
        if gen is None:
            raise SystemExit(f"{store.root}/{PREFIX}: no current generation ({how}): name one with -g")
    gen = ic.chain(store, gen)[-1]
    with workdir(work) as w:
        print(json.dumps(backfill(store, gen, to_gen, listing_root, template, w, ranges_gen=ranges_gen, since=since, stall_s=stall_s, rg_rows=rg_rows, threads=threads, mem=mem, jobs=jobs), indent=1))


# ── Parity against the listings ────────────────────────────────────────────


#: `verify_listings`' table of the store's versions (`depth, path, vf, vt, kind, generation`), one row each.
VERSIONS = "__versions"


def store_versions(con, served: list[str]) -> None:
    """`VERSIONS` from the served `path` sorts (base, then runs): a version's rows across them are one, the smallest `vt`
    (a run's close record over the row it closes), as `interval_export.live_sql` reads them. Built once, filtered per scan."""
    union = " UNION ALL ".join(f"SELECT depth, path, vf, vt, kind, {ist.GENERATION} FROM read_parquet({q(f)})" for f in served)
    if len(served) == 1:  # a base alone (a compacted or backfilled generation) has one row per version: no window, which
        # over gcs's 1.2B rows ran DuckDB out of memory (93 GiB), where a plain copy streams
        con.execute(f"CREATE OR REPLACE TABLE {VERSIONS} AS {union}")
        return
    # Base plus runs: the window runs only over the runs' rows and the base rows they share a version with (a few M on
    # gcs); the rest of the base (1.2B rows) streams through an anti-join against them, never a global window.
    sel = lambda f: f"SELECT depth, path, vf, vt, kind, {ist.GENERATION} FROM read_parquet({q(f)})"  # noqa: E731
    runs = " UNION ALL ".join(sel(f) for f in served[1:])
    con.execute(f"CREATE OR REPLACE TEMP TABLE __runs AS {runs}")
    con.execute(f"""CREATE OR REPLACE TABLE {VERSIONS} AS
        SELECT b.* FROM ({sel(served[0])}) b ANTI JOIN __runs r USING (depth, path, vf)
        UNION ALL
        SELECT * FROM (SELECT * FROM __runs UNION ALL SELECT b.* FROM ({sel(served[0])}) b SEMI JOIN __runs r USING (depth, path, vf))
        QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1""")
    con.execute("DROP TABLE __runs")


def listing_parity(con, files: list[str], ts: int) -> dict:
    """The objects' `generation` at a scan, the store against the scan's raw listing: the listed objects with one (a key
    that is also a folder in the store excepted: the store folds it into the folder), how many the store's version live
    at the scan (`VERSIONS`) gives the same, and how many it has no file for."""
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, (files,))
    g = ist.GENERATION
    con.execute(f"""CREATE OR REPLACE TEMP TABLE __own AS SELECT len(string_split(path, '/'))::UTINYINT AS depth, path, {g} FROM (
        SELECT bucket || '/' || name AS path, any_value({g}) AS {g} FROM {src} WHERE {g} IS NOT NULL GROUP BY 1 HAVING count(*) = 1)""")
    n, eq, missing = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE l.kind = 'file' AND l.{g} = o.{g}),
            count(*) FILTER (WHERE l.kind IS NULL)
        FROM __own o LEFT JOIN (SELECT depth, path, kind, {g} FROM {VERSIONS} WHERE vf <= {ts} AND {ts} < vt) l USING (depth, path)
        WHERE l.kind IS NULL OR l.kind = 'file'""").fetchone()
    con.execute("DROP TABLE __own")
    return {"objects": n, "equal": eq, "missing": missing, "against": "listing"}


def verify_listings(store: Store, gen: str, listing_root: str, template: str, work: Path, scan_ids: list[str] = (), *,
                    since: str | None = None, stall_s: float = STALL_S,
                    threads: int = 4, mem: str = "4GB") -> dict:
    """Each scan of generation `gen` (or `scan_ids`) whose raw listing has generations: `listing_parity` against the
    store, recorded on the scan's parity marker (`verify/<id>.json`, found in `gen` or one it was compacted from) as its
    `generation`, `pass` recomputed with it, written to `gen` (`views_gen`: where the views were compared). A scan with
    no marker gets none: its views were never checked. Returns per scan `{status, generation}`."""
    from .interval_store import download_served, verify_runs
    from .interval_verify import marker_key
    from .scan_retire import find_marker, lineage

    prefix = f"{PREFIX}/{gen}"
    doc = store.read_json(f"{prefix}/scans.json")
    if not ist.has_generation(doc):
        raise SystemExit(f"{store.root}/{prefix}: doesn't carry `{ist.GENERATION}`: nothing to compare")
    download_served(store, prefix, work / "served")
    runs = verify_runs(store, prefix, work / "runs", doc)
    served = [str(work / "served" / f"{ist.GENERATION_SORT}.parquet"), *(f"{r}/{ist.GENERATION_SORT}.parquet" for r in runs)]
    ts_of = {s["id"]: s["ts"] for s in doc["scans"]}
    from .interval_export import scan_entry

    newest = latest_key([k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")])
    ids = [i for i in (list(scan_ids) or (store.read_json(f"{prefix}/{newest}")["scans"] if newest else list(ts_of))) if since is None or i >= since]
    gens = lineage(store, gen)
    con = sn.connect(threads, mem, work / "duckdb")
    t0 = monotonic()
    store_versions(con, served)
    err(f"verify-generation: {con.execute(f'SELECT count(*) FROM {VERSIONS}').fetchone()[0]:,} versions of {gen} in {monotonic() - t0:.1f}s")
    out = {}
    with Watchdog(stall_s, "verify-generation") as watch:
        for sid in ids:
            tmp = work / "listings" / sid
            files = scan_shards(Store(listing_root), template, sid, tmp, "verify-generation", watch)
            if files is None:
                out[sid] = {"status": "no-generation", "generation": None}
                continue
            ts = ts_of[sid] if sid in ts_of else scan_entry(store, prefix, sid)[0]["ts"]
            t0 = monotonic()
            g = listing_parity(con, files, ts)
            shutil.rmtree(tmp, ignore_errors=True)
            err(f"verify-generation: {sid}: {g['equal']:,} of {g['objects']:,} listed generations equal ({g['missing']:,} without a file) "
                f"in {monotonic() - t0:.1f}s")
            m = find_marker(store, gens, sid)
            if m is None:
                out[sid] = {"status": "no-marker", "generation": g}
                continue
            ok = m["views"] > 0 and m["views"] == m["view_eq"] and m["diffs"] == m["diff_eq"] and g["objects"] > 0 and g["equal"] == g["objects"]
            store.write_text(marker_key(prefix, sid), json.dumps({**m, "gen": gen, "views_gen": m.get("views_gen", m["gen"]), "pass": ok,
                                                                   "generation": g}) + "\n")
            out[sid] = {"status": "pass" if ok else "fail", "generation": g}
            watch.beat()
    return {"gen": gen, "scans": out}


@command("verify-generation")
@option("-g", "--gen", help="The generation (default: the store's current, followed to its newest successor); must carry `generation`")
@option("-l", "--listing", "template", required=True, help="Key template of a scan's raw listing shards under -s: `{id}` the scan id, globs allowed (e.g. `listing/{id}/*/shard-*.parquet`)")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-s", "--listings", "listing_root", required=True, help="Where the raw listings are: a URL (`gs://…`: read through the API, a vanished shard an error) or a dir, the root of -l's keys; not a FUSE mount of a bucket being retired (a read can hang)")
@option("-S", "--since", help="Skip, unread, the scans before this id (their listings carry no `generation`)")
@option("-T", "--stall", "stall_s", default=STALL_S, type=IntRange(min=0), help="Exit 3 when no listing is read for this many seconds (0: never)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after)")
@argument("scan_ids", nargs=-1)
def verify_generation_cmd(gen, template, mem, store_root, threads, listing_root, since, stall_s, work, scan_ids) -> None:
    """Compare the objects' `generation` the store serves at each scan (SCAN_IDS, default every scan of the generation)
    with the scan's raw listing, and record it on the scan's parity marker (`pass` then needs every listed generation
    equal), so `scan retire -1` can retire the listing. Prints a JSON summary."""
    store = Store(store_root)
    if gen is not None:
        _check_gen(gen)
    else:
        gen, how = ic.resolve(store)
        if gen is None:
            raise SystemExit(f"{store.root}/{PREFIX}: no current generation ({how}): name one with -g")
    gen = ic.chain(store, gen)[-1]
    with workdir(work) as w:
        print(json.dumps(verify_listings(store, gen, listing_root, template, w, list(scan_ids), since=since, stall_s=stall_s, threads=threads, mem=mem), indent=1))
