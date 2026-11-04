"""The interval store's attribution table, over history (specs/attr-interval-table.md §4): the kept scans' per-scan
maps folded into a new generation, and the parity of the table against a scan's own map.

    dt-cloud interval-store attribution-convert [-g GEN] -o STORE [-s ROOT] [-n]       # attr.tsv → attribution.parquet
    dt-cloud interval-store backfill-attribution [-g GEN] -O NEW -o STORE [-s ROOT]    # NEW = GEN folded, with the table

`attribution-convert` writes, beside each of a generation's scans' `attr.tsv` that has no `attribution.parquet`, the same
map as that parquet (`extras.attribution_from_tsv`): the one place an `attr.tsv` is parsed, a migration step for scans
from before `path-index` wrote the parquet. The store's build, appends and backfill read only `attribution.parquet`.

`backfill-attribution` folds generation GEN (its base and the newest manifest's runs, as a compaction:
`interval_compact.fold_gen`) into NEW, and builds NEW's attribution table from every scan's `attribution.parquet` beside
its `path` sort (`build_attribution_range`; a scan without one is uncovered). Where GEN's runs already carried the table
(appends with `append.attribution` before the backfill), the table it builds must give, at every scan those runs cover,
exactly the rows they give, or it refuses. NEW's `scans.json` says `"attribution": true` and names each covered scan's
file; it is published as `backfill-generation` publishes (data first, `scans.json` last; `CURRENT.json` untouched):
verify it, then `interval-store current -s NEW` and the profile's `append.gen`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import monotonic

from click import IntRange, command, option

from . import interval_append as ia
from . import interval_compact as ic
from . import interval_store as ist
from . import static_names as sn
from . import tree_mem as tm
from .append_runner import LocalRunStore, latest_key
from .blobstore import Store, upload_new
from .static_local import _check_gen, workdir
from .static_names import err, q

PREFIX = ist.PREFIX
AT = ist.ATTRIBUTION
#: The backfill's record in the new generation.
RECORD = "attribution-backfill.json"


def generation_scans(store: Store, gen: str) -> list[dict]:
    """Every scan of generation `gen`: its base's entries, then its runs' (their pinned `deltas/<id>/scans.json`)."""
    prefix = f"{PREFIX}/{gen}"
    base = store.read_json(f"{prefix}/scans.json")
    key = latest_key([k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")])
    runs = store.read_json(f"{prefix}/{key}")["runs"] if key else []
    from .append_runner import run_key

    return [*base["scans"], *(store.read_json(f"{prefix}/{run_key(s, s)}/scans.json")["scans"][0] for r in runs for s in r["scans"])]


def convert(store: Store, gen: str, scans_root: Store, *, dry_run: bool = False) -> dict:
    """For each scan of `gen` whose `path` sort has an `attr.tsv` beside it and no `attribution.parquet`: that parquet,
    from the sidecar (`extras.attribution_from_tsv`). Returns per scan what was done."""
    from .extras import ATTR_FILE, attribution_from_tsv, write_attribution

    out: dict[str, str] = {}
    for s in generation_scans(store, gen):
        dst = ist.attribution_src(s)
        tsv = f"{dst.rsplit('/', 1)[0]}/{ATTR_FILE}" if "/" in dst else ATTR_FILE
        if scans_root.exists(dst):
            out[s["id"]] = "has"
            continue
        if not scans_root.exists(tsv):
            out[s["id"]] = "none"
            continue
        if dry_run:
            out[s["id"]] = "would convert"
            continue
        t = attribution_from_tsv(scans_root.read_bytes(tsv).decode())
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "attribution.parquet"
            write_attribution(t, p)
            scans_root.put(p, dst)
        out[s["id"]] = f"converted {t.num_rows}"
        err(f"attribution-convert {s['id']}: {tsv} → {dst} ({t.num_rows:,} prefixes)")
    return out


def _digests(con, rel: str, stamps: list[int]) -> list[list[int]]:
    con.execute(f"CREATE OR REPLACE TEMP TABLE __at AS {rel}")
    try:
        return ist._prefix_digests(con, "__at", ist.AT_HASH, stamps)
    finally:
        con.execute("DROP TABLE __at")


def backfill_attribution(store: Store, gen: str, new_gen: str, scans_root: str, work: Path, *, ranges_gen: str | None = None,
                         rg_rows: int = ist.SERVED_RG, threads: int = 4, mem: str = "4GB", jobs: int = 1) -> dict:
    """`gen` folded into `new_gen` with the attribution table built from every scan's `attribution.parquet` under
    `scans_root` (beside its `path` sort), checked against the table `gen`'s runs carry where they carry it, then
    published (data first, `scans.json` last). Returns a summary."""
    from .interval_backfill import mirror_source

    t0 = monotonic()
    new_prefix = f"{PREFIX}/{new_gen}"
    gen_dir = mirror_source(store, gen, new_gen, work, ranges_gen)
    base = json.loads((gen_dir / "scans.json").read_text())
    lstore = LocalRunStore(gen_dir, work / "lease", gen)
    newest = latest_key(lstore.keys("manifests/"))
    m = lstore.read_json(newest) if newest else None
    runs = m["runs"] if m else []
    had = ia.lineage_attributed(base, m)
    checked = sorted(ia.covered_scans(base, m)) if had else []
    out = work / "store" / new_prefix
    doc, rows, sorts, stats = ic.fold_gen(gen_dir, out, work, gen=gen, runs=runs, rg_rows=rg_rows, threads=threads, mem=mem, jobs=jobs)
    # Every scan's map: copied under the work dir's mount, its entry naming it.
    src, mount = Store(scans_root), work / "scans"
    for s in doc["scans"]:
        key = ist.attribution_src(s)
        if src.exists(key):
            dst = mount / key
            if not dst.is_file():
                src.get(key, dst)
            s[ist.ATTRIBUTION_SRC] = key
        else:
            s.pop(ist.ATTRIBUTION_SRC, None)
    covered = [s["id"] for s in doc["scans"] if s.get(ist.ATTRIBUTION_SRC)]
    if missing := sorted(set(checked) - set(covered)):
        raise SystemExit(f"backfill-attribution: {gen}'s table covers {', '.join(missing)}, whose `attribution.parquet` isn't under {scans_root}")
    doc = {**{k: v for k, v in doc.items() if k != AT}, AT: True}
    ranges = json.loads((out / "ranges.json").read_text())
    built = work / "attribution"
    shutil.rmtree(built, ignore_errors=True)
    # The fold's database is gone: the budget is measured again with what this process now holds.
    tree = tm.Budget.measure(sn.mem_bytes(mem))
    con = sn.connect(threads, tm.stage_mem(tree, f"backfill-attribution {gen}"), work / "duckdb-attribution")
    stamps = [s["ts"] for s in doc["scans"]]
    idx = {s["id"]: j for j, s in enumerate(doc["scans"])}
    bad, unequal = [], []
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        d = ist.build_attribution_range({**doc, "bucket": str(mount)}, ranges, r["i"], built, con, mount=str(mount))
        bad += [(r["i"], x["id"]) for x in d["scans"] if not x["eq"]]
        if had:
            # The runs' table (folded into `out` by `fold_gen`) against the one built, at each scan the runs covered.
            a = _digests(con, f"SELECT * FROM read_parquet({q(str(out / AT / f'{name}.parquet'))})", stamps)
            b = _digests(con, f"SELECT * FROM read_parquet({q(str(built / AT / f'{name}.parquet'))})", stamps)
            unequal += [(r["i"], sid) for sid in checked if a[idx[sid]] != b[idx[sid]]]
    if bad:
        raise RuntimeError(f"backfill-attribution: the versions don't reconstruct every covered scan: (range, scan) {bad}")
    if unequal:
        raise SystemExit(f"backfill-attribution: the table built from the scans' maps differs from {gen}'s at (range, scan) {unequal}")
    shutil.rmtree(out / AT, ignore_errors=True)
    shutil.copytree(built / AT, out / AT)
    for x in ("parquet", "groups.parquet", "json"):
        (out / "served" / f"{AT}.{x}").unlink(missing_ok=True)
    files = [str(p) for p in sorted((out / AT).glob("r*.parquet"))]
    with ist.plan_cut(con, tree, files, rg_rows=rg_rows, what=f"cut {AT}") as (procs, budget):
        sd = ist.write_served(con, files, AT, out / "served" / f"{AT}.parquet", ist.served_schema(AT), rg_rows=rg_rows, stamps=stamps,
                              procs=procs, budget=budget)
    (out / "served" / f"{AT}.json").write_text(json.dumps(sd, sort_keys=True) + "\n")
    (out / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    rec = {"from": gen, "manifest": newest, "runs": [r["key"] for r in runs], "scans": len(doc["scans"]), "covered": covered,
           "uncovered": [s["id"] for s in doc["scans"] if not s.get(ist.ATTRIBUTION_SRC)], "checked": checked}
    (out / RECORD).write_text(json.dumps(rec, indent=1) + "\n")
    from .interval_local import publish_order, publish_tier

    files = upload_new(store, out, new_prefix, {}, publish_order, tier=publish_tier)
    s = round(monotonic() - t0, 1)
    err(f"backfill-attribution {gen} → {new_gen}: {len(covered)} of {len(doc['scans'])} scans covered, {sd['rows']:,} attribution versions, "
        f"{len(checked)} checked against the runs, {len(files)} files in {s}s")
    return {**rec, "gen": new_gen, "rows": rows, "sorts": {**sorts, AT: {k: sd[k] for k in ("rows", "groups", "bytes")}}, "files": len(files), "s": s}


# ── Parity against a scan's own map (`interval-store verify`, `scan retire`'s marker) ──


def attribution_parity(con, scan_file: str, served: str, runs: list[str], ts: int) -> dict:
    """The attribution table at a scan (the served `attribution` sort, base and the runs holding it, combined per version,
    those live at `ts`) against the scan's own `attribution.parquet`, row for row: `rows` (the scan's), `store`, `missing`
    (scan rows the store doesn't give, as a multiset), `extra`."""
    cols = ist.qcols(["depth", "path", *ist.ATTRIBUTION_CHANGE])
    files = [f for f in (f"{served}/{AT}.parquet", *(f"{r}/{AT}.parquet" for r in runs)) if Path(f).exists()]
    union = " UNION ALL ".join(f"SELECT depth, path, vf, vt, {ist.qcols(ist.ATTRIBUTION_CHANGE)} FROM read_parquet({q(f)})" for f in files) \
        or ist.empty_sql(ist.AT_SCHEMA)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE __at_store AS SELECT {cols} FROM (SELECT * FROM ({union})
        QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1) WHERE vf <= {ts} AND {ts} < vt""")
    con.execute(f"CREATE OR REPLACE TEMP TABLE __at_scan AS SELECT depth::UTINYINT AS depth, path, {ist.qcols(ist.ATTRIBUTION_CHANGE)} FROM read_parquet({q(scan_file)})")
    n_scan, n_store = (int(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]) for t in ("__at_scan", "__at_store"))
    out = {"rows": n_scan, "store": n_store, "missing": ia._differ(con, "__at_scan", "__at_store", cols), "extra": ia._differ(con, "__at_store", "__at_scan", cols)}
    con.execute("DROP TABLE __at_store; DROP TABLE __at_scan")
    return out


# ── CLI ────────────────────────────────────────────────────────────────────


def _gen(store: Store, gen: str | None) -> str:
    if gen is not None:
        _check_gen(gen)
    else:
        gen, how = ic.resolve(store)
        if gen is None:
            raise SystemExit(f"{store.root}/{PREFIX}: no current generation ({how}): name one with -g")
    return ic.chain(store, gen)[-1]


@command("attribution-convert")
@option("-g", "--gen", help="The generation whose scans to convert (default: the store's current, followed to its newest successor)")
@option("-n", "--dry-run", is_flag=True, help="Report what would be converted; write nothing")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`)")
@option("-s", "--scans", "scans_root", help="Where the scans' `path` sorts (and their sidecars) are: a dir or URL (default: the store)")
def convert_cmd(gen, dry_run, store_root, scans_root) -> None:
    """Write `attribution.parquet` beside each of the generation's scans' `attr.tsv` that lacks one (the same map, typed):
    the backfill's input for scans from before `path-index` wrote it. Prints a JSON summary."""
    store = Store(store_root)
    gen = _gen(store, gen)
    print(json.dumps({"gen": gen, "scans": convert(store, gen, Store(scans_root or store_root), dry_run=dry_run)}, indent=1))


@command("backfill-attribution")
@option("-g", "--gen", help="The source generation (default: the store's current, followed to its newest successor); never rewritten")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="Key ranges, then served sorts, folded this many at a time (as `backfill-generation`); the output's bytes don't depend on it")
@option("-M", "--mem", default="4GB", help="Memory budget of the whole process tree (`tree_mem`): DuckDB gets what this process, the cut's workers and the Arrow buffers leave")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the readers' INDEX_R2 root")
@option("-O", "--to-gen", required=True, help="The new generation: `<store>/interval-store/TO_GEN/` (must not exist)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-R", "--ranges-gen", help="The generation holding GEN's key ranges and base range files, when GEN borrows them (a staged build); default GEN")
@option("-r", "--rg-rows", default=ist.SERVED_RG, type=IntRange(min=1), help="Rows per served row group")
@option("-s", "--scans", "scans_root", help="Where the scans' `attribution.parquet` files are (beside their `path` sorts): a dir or URL (default: the store)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); the source generation is mirrored into it")
def backfill_attribution_cmd(gen, jobs, mem, store_root, to_gen, threads, ranges_gen, rg_rows, scans_root, work) -> None:
    """Fold a generation into a new one with the attribution table over every scan's `attribution.parquet` (a scan without
    one uncovered), checked against the table the source's runs carry. The source and `CURRENT.json` are left as they
    are: verify the new generation, then `interval-store current -s TO_GEN`. Prints a JSON summary."""
    _check_gen(to_gen)
    store = Store(store_root)
    gen = _gen(store, gen)
    with tm.watch(mem, f"backfill-attribution {gen}"), workdir(work) as w:
        print(json.dumps(backfill_attribution(store, gen, to_gen, scans_root or store_root, w, ranges_gen=ranges_gen, rg_rows=rg_rows, threads=threads,
                                              mem=mem, jobs=jobs), indent=1))
