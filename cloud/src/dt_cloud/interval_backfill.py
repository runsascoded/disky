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
from click import IntRange, command, option

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
        files = listing_files(root, template, s["id"], tmp)
        if not files or ist.GENERATION not in pq.read_schema(files[0]).names:
            skipped.append(s["id"])
            continue
        t0 = monotonic()
        src = prepare_listing(con, tuple(files))
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
