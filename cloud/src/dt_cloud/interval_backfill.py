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


def listing_files(root: Store, template: str, scan: str, local: Path) -> list[str]:
    """Scan `scan`'s raw listing shards: the keys under `root` matching `template` (`{id}` the scan id, globs allowed), as
    local paths (a URL root's copied into `local`, keys kept)."""
    pat = template.replace("{id}", scan)
    lead = pat.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
    prefix = lead.rsplit("/", 1)[0] if "/" in lead else ""
    keys = sorted(k for k in root.keys(prefix) if fnmatch.fnmatchcase(k, pat))
    if not root.url:
        return [str(Path(root.root) / k) for k in keys]
    out = []
    for k in keys:
        dst = local / k
        if not (dst.is_file() and dst.stat().st_size == root.size(k)):
            root.get(k, dst)
        out.append(str(dst))
    return out


def scan_shards(root: Store, template: str, scan: str, tmp: Path, what: str) -> list[str] | None:
    """Scan `scan`'s raw listing shards with `generation` (`listing_files`), or None (logged, `tmp` removed) when nothing
    matches or no shard has the column. A template matching files with and without it is refused: a stray non-listing
    parquet (gcs's `dir-cache/`) must not decide the scan's fate."""
    files = listing_files(root, template, scan, tmp)
    without = [f for f in files if ist.GENERATION not in pq.read_schema(f).names]
    if without and len(without) < len(files):
        raise SystemExit(f"{scan}: {template!r} matches {len(files)} files, {len(without)} without a `{ist.GENERATION}` column "
                         f"({', '.join(without[:3])}): narrow it to the listing shards")
    if without or not files:
        err(f"{what}: {scan}: skipped, " + (f"no `{ist.GENERATION}` in its {len(files)} shards" if files else f"nothing matches {template!r}"))
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    return files


def gen_index(con, root: Store, template: str, scans: list[dict], work: Path) -> dict:
    """`work/GEN_INDEX`: per `(depth, path, generation)` over every scan whose listing (`template` under `root`) has a
    `generation` column, the first and last scan stamp it was listed at (`first_ts`, `last_ts`), sorted `(depth, path)`.
    A URL root's shards are copied in one scan at a time and removed after. Returns `{scans: ids read, skipped: ids
    without generations, rows}`."""
    from disk_tree.listing import prepare_listing

    con.execute("CREATE OR REPLACE TABLE lg (path VARCHAR, generation BIGINT, ts BIGINT)")
    read, skipped = [], []
    for s in scans:
        tmp = work / "listings" / s["id"]
        files = scan_shards(root, template, s["id"], tmp, "backfill")
        if files is None:
            skipped.append(s["id"])
            continue
        t0 = monotonic()
        src = prepare_listing(con, (files,))  # one source: per-file sources would keep each bucket's first shard only
        n = con.execute(f"""INSERT INTO lg SELECT DISTINCT bucket || '/' || name, {ist.GENERATION}, {int(s['ts'])}
            FROM {src} WHERE {ist.GENERATION} IS NOT NULL""").fetchone()[0]
        err(f"backfill: {s['id']}: {n:,} objects with a generation ({len(files)} shards, {monotonic() - t0:.1f}s)")
        read.append(s["id"])
        shutil.rmtree(tmp, ignore_errors=True)
    out = work / GEN_INDEX
    con.execute(f"""COPY (SELECT len(string_split(path, '/'))::UTINYINT AS depth, path, generation, min(ts) AS first_ts, max(ts) AS last_ts
        FROM lg GROUP BY path, generation ORDER BY depth, path, first_ts) TO {q(str(out))} (FORMAT PARQUET, ROW_GROUP_SIZE 65536)""")
    con.execute("DROP TABLE lg")
    rows = con.execute(f"SELECT count(*) FROM read_parquet({q(str(out))})").fetchone()[0]
    return {"scans": read, "skipped": skipped, "rows": rows}


def filler(index: str, ranges: dict, counts: dict[str, int]) -> ic.Fill:
    """The fold's hook (`interval_compact.Fill`): each object path version of the range takes the generation listed
    first among those listed within it (`index`: `gen_index`'s file), counted in `counts`."""
    by_name = {f"r{r['i']:04d}": r for r in ranges["ranges"]}

    def fill(con, name: str) -> None:
        preds = " OR ".join(f"({p})" for p in sn.range_preds(by_name[name]))
        g = ist.GENERATION
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __fill AS
            SELECT p.depth, p.path, p.vf, arg_min(g.{g}, g.first_ts) AS {g}
            FROM pv p JOIN (SELECT * FROM read_parquet({q(index)}) WHERE {preds}) g ON g.depth = p.depth AND g.path = p.path
            WHERE p.kind = 'file' AND g.first_ts < p.vt AND g.last_ts >= p.vf
            GROUP BY p.depth, p.path, p.vf""")
        con.execute(f"UPDATE pv SET {g} = f.{g} FROM __fill f WHERE pv.depth = f.depth AND pv.path = f.path AND pv.vf = f.vf")
        counts[name] = con.execute("SELECT count(*) FROM __fill").fetchone()[0]
        con.execute("DROP TABLE __fill")
    return fill


def backfill(store: Store, gen: str, new_gen: str, listing_root: str, template: str, work: Path, *, rg_rows: int = ist.SERVED_RG,
             threads: int = 4, mem: str = "4GB") -> dict:
    """Generation `gen` of `store` folded into `new_gen` with the objects' `generation` from the raw listings (`template`
    under `listing_root`), published to the store (data first, `scans.json` last; `CURRENT.json` untouched). Returns a
    summary."""
    t0 = monotonic()
    prefix, new_prefix = f"{PREFIX}/{gen}", f"{PREFIX}/{new_gen}"
    if store.listing(new_prefix):
        raise SystemExit(f"{store.root}/{new_prefix}/ exists: a generation is never rewritten")
    gen_dir = work / "store" / prefix
    before = mirror(store, prefix, gen_dir, keep=lambda rel: not rel.startswith(UNREAD))
    if f"{prefix}/scans.json" not in before:
        raise SystemExit(f"{store.root}/{prefix}/scans.json: no such generation")
    base = json.loads((gen_dir / "scans.json").read_text())
    if ist.has_generation(base):
        raise SystemExit(f"{store.root}/{prefix}: already carries `{ist.GENERATION}`")
    lstore = LocalRunStore(gen_dir, work / "lease", gen)
    newest = latest_key(lstore.keys("manifests/"))
    runs = lstore.read_json(newest)["runs"] if newest else []
    scans = [*base["scans"], *ic.run_scans(gen_dir, gen, runs)]
    con = sn.connect(threads, mem, work / "duckdb")
    idx = gen_index(con, Store(listing_root), template, scans, work)
    if not idx["scans"]:
        raise SystemExit(f"no listing under {listing_root} matching {template!r} has a `{ist.GENERATION}` column")
    ranges = json.loads((gen_dir / "ranges.json").read_text())
    counts: dict[str, int] = {}
    out = work / "store" / new_prefix
    doc, rows, sorts, stats = ic.fold_gen(gen_dir, out, work, gen=gen, runs=runs, rg_rows=rg_rows, threads=threads, mem=mem,
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
@option("-l", "--listing", "template", required=True, help="Key template of a scan's raw listing shards under -s: `{id}` the scan id, globs allowed (e.g. `listing/{id}/*/*.parquet`)")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the readers' INDEX_R2 root")
@option("-O", "--to-gen", required=True, help="The new generation: `<store>/interval-store/TO_GEN/` (must not exist)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-r", "--rg-rows", default=ist.SERVED_RG, type=IntRange(min=1), help="Rows per served row group")
@option("-s", "--listings", "listing_root", required=True, help="Where the raw listings are: a dir (or a mount of the bucket) or URL, the root of -l's keys")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); the source generation is mirrored into it")
def backfill_cmd(gen, template, mem, store_root, to_gen, threads, rg_rows, listing_root, work) -> None:
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
        print(json.dumps(backfill(store, gen, to_gen, listing_root, template, w, rg_rows=rg_rows, threads=threads, mem=mem), indent=1))


# ── Parity against the listings ────────────────────────────────────────────


#: `verify_listings`' table of the store's versions (`depth, path, vf, vt, kind, generation`), one row each.
VERSIONS = "__versions"


def store_versions(con, served: list[str]) -> None:
    """`VERSIONS` from the served `path` sorts (base, then runs): a version's rows across them are one, the smallest `vt`
    (a run's close record over the row it closes), as `interval_export.live_sql` reads them. Built once, filtered per scan."""
    union = " UNION ALL ".join(f"SELECT depth, path, vf, vt, kind, {ist.GENERATION} FROM read_parquet({q(f)})" for f in served)
    con.execute(f"""CREATE OR REPLACE TABLE {VERSIONS} AS SELECT * FROM ({union})
        QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1""")


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
    ids = list(scan_ids) or (store.read_json(f"{prefix}/{newest}")["scans"] if newest else list(ts_of))
    gens = lineage(store, gen)
    con = sn.connect(threads, mem, work / "duckdb")
    t0 = monotonic()
    store_versions(con, served)
    err(f"verify-generation: {con.execute(f'SELECT count(*) FROM {VERSIONS}').fetchone()[0]:,} versions of {gen} in {monotonic() - t0:.1f}s")
    out = {}
    for sid in ids:
        tmp = work / "listings" / sid
        files = scan_shards(Store(listing_root), template, sid, tmp, "verify-generation")
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
    return {"gen": gen, "scans": out}


@command("verify-generation")
@option("-g", "--gen", help="The generation (default: the store's current, followed to its newest successor); must carry `generation`")
@option("-l", "--listing", "template", required=True, help="Key template of a scan's raw listing shards under -s: `{id}` the scan id, globs allowed (e.g. `listing/{id}/*/shard-*.parquet`)")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-s", "--listings", "listing_root", required=True, help="Where the raw listings are: a dir (or a mount of the bucket) or URL, the root of -l's keys")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after)")
@argument("scan_ids", nargs=-1)
def verify_generation_cmd(gen, template, mem, store_root, threads, listing_root, work, scan_ids) -> None:
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
        print(json.dumps(verify_listings(store, gen, listing_root, template, w, list(scan_ids), threads=threads, mem=mem), indent=1))
