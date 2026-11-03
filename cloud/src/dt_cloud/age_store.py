"""Age histogram cells stored by the existing change-interval engine.

Each (tier, path, binstart) is a stable synthetic path, with `size=b` and
`n_files=o`. The interval store coalesces unchanged cells, including zero-byte
cells, and supplies its normal compaction, publication and range readers.
Build inputs are temporary; every newly covered scan is exported and compared
cell for cell before any remote manifest or current pointer is published.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from click import command, option

from . import interval_compact as ic
from . import interval_local as il
from . import static_names as sn
from .blobstore import Store, mirror
from .interval_export import export_scan, scan_entry, store_gen

BINS = ("1h", "3h", "6h", "12h", "1d", "2d", "4d", "8d")
INPUT_LAYOUT = "inputs/{id}/path-index.parquet"
MARKERS = "verified"
#: Every DuckDB step's default threads and memory: sized for hf (~270K cells); a deployment with a large finest tier
#: (gcs: tens of millions) passes its own (`-p` / `-M`, the step's `options.age-store.threads` / `mem`).
THREADS = 2
MEM = "2GB"


def cells(
    source: Store,
    key: str,
    output: Path,
    work: Path,
    *,
    threads: int = THREADS,
    mem: str = MEM,
) -> int:
    """One scan's eight pyramids as canonical path rows; no row per scan is retained."""
    con = sn.connect(threads, mem, work / "duckdb")
    try:
        queries = []
        for tier in BINS:
            src_key = key.rsplit("/", 1)[0] + f"/age-pyramid-{tier}.parquet"
            local = work / f"{tier}.parquet"
            source.get(src_key, local)
            queries.append(f"""SELECT {sn.q(tier)} || '/p=' || url_encode(path) || '/' || binstart::VARCHAR AS path,
                sum(b)::BIGINT AS size, sum(o)::BIGINT AS n_files
                FROM read_parquet({sn.q(str(local))}) GROUP BY path, binstart""")
        output.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"""COPY (SELECT path, 3::INTEGER AS depth, size, n_files,
            'file'::VARCHAR AS kind, 0::BIGINT AS mtime, 0::BIGINT AS n_desc, 0::BIGINT AS n_children
            FROM ({' UNION ALL '.join(queries)}) ORDER BY depth, path)
            TO {sn.q(str(output))} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        return con.execute(f"SELECT count(*) FROM read_parquet({sn.q(str(output))})").fetchone()[0]
    finally:
        con.close()


def verify(
    root: str,
    scan: str,
    expected: Path,
    work: Path,
    *,
    threads: int = THREADS,
    mem: str = MEM,
) -> dict:
    """Exact multiset equality of every histogram cell, before publication."""
    export_scan(root, scan, str(work / "export"), tiers=("path",), work=work / "read", threads=threads, mem=mem)
    con = sn.connect(threads, mem, work / "duckdb")
    try:
        a = f"SELECT path, size, n_files FROM read_parquet({sn.q(str(expected))})"
        b = f"SELECT path, size, n_files FROM read_parquet({sn.q(str(work / 'export/path-index.parquet'))})"
        diff = con.execute(f"SELECT count(*) FROM (({a} EXCEPT ALL {b}) UNION ALL ({b} EXCEPT ALL {a}))").fetchone()[0]
        if diff:
            raise ValueError(f"age-store {scan}: {diff} differing cells; nothing published")
        rows = con.execute(f"SELECT count(*) FROM ({a})").fetchone()[0]
        return {"scan": scan, "pass": True, "cells": rows}
    finally:
        con.close()


def covered(root: str, scan: str) -> bool:
    """Both a passing cell comparison and a published interval manifest must hold the scan."""
    store = Store(root)
    if ic.current(store) is None:
        return False
    gen = store_gen(store, None)
    marker = f"{MARKERS}/{scan}.json"
    if not store.exists(marker) or store.read_json(marker).get("pass") is not True:
        return False
    try:
        scan_entry(store, f"interval-store/{gen}", scan)
    except SystemExit:
        return False
    return True


def retire(
    source_root: str,
    layout: str,
    history: str,
    keep: int,
    *,
    execute: bool = False,
    site_store: str | None = "primary",
) -> dict:
    """Retire only histogram files whose scan passed full comparison and is published. D1 clears first."""
    from . import index_footer as F
    from .scan_retire import _tables, classify, template_rx

    if keep < 1:
        raise ValueError("age-store retention must keep at least one recent scan")
    source = Store(source_root)
    lead, rx = template_rx(layout.rsplit("/", 1)[0] + "/*")
    scans: dict[str, list[str]] = {}
    inventory = source.listing(lead)
    for key in inventory:
        match = rx.fullmatch(key)
        if match and classify(key.rsplit("/", 1)[-1]) == "age":
            scans.setdefault(match["id"], []).append(key)
    recent = set(sorted(scans)[-keep:])
    dates = [s for s in sorted(scans) if s not in recent and covered(history, s)]
    keys = sorted(k for s in dates for k in scans[s])
    deleted_rows = 0
    if execute and dates:
        if site_store is not None:
            tok, acct = F._creds()
            if not tok or not acct:
                raise ValueError("age-store retirement requires D1 credentials (or explicit --no-d1)")
            base = F._q(F.d1_variant("age", site_store))
            pyramid = F._q(F.d1_variant("age-pyramid-", site_store) + "%")
            for table, _ in _tables(acct, tok):
                for date in dates:
                    where = f"date = {F._q(date)} AND (variant = {base} OR variant LIKE {pyramid})"
                    if table == "index_schema":
                        where += f" AND store = {F._q(site_store)}"
                    deleted_rows += F.d1_delete(table, where, acct, tok)
        source.delete(keys)
    return {"root": source_root, "execute": execute, "scans": dates, "files": keys,
            "bytes": sum(inventory[k] for k in keys), "d1_rows": deleted_rows}


def update(
    source_root: str,
    layout: str,
    store_root: str,
    gen: str,
    work: Path,
    *,
    through: str | None = None,
    compact: str = "level:5",
    threads: int = THREADS,
    mem: str = MEM,
) -> dict:
    """Catch up all available scans, verify locally, then publish data → manifests → pointer. `threads` / `mem`: every
    DuckDB step's (the cells, the interval build or add, the verification)."""
    source, target = Store(source_root), Store(store_root)
    work.mkdir(parents=True, exist_ok=True)
    local = work / "store"
    # A previous failed verification may have advanced the local pointer. Only published state is authoritative.
    if local.exists():
        shutil.rmtree(local)
    current = ic.current(target)
    # An interrupted first publication can leave data without a current pointer. Rebuild that unpublished
    # generation locally; mirroring its partial files would make the native builder refuse the retry.
    before = mirror(target, "interval-store", local / "interval-store") if current else target.listing("interval-store")
    have: set[str] = set()
    if current:
        from .append_runner import latest_key, scans_of
        from .static_local import GenStore

        current = store_gen(Store(str(local)), None)
        prefix = f"interval-store/{current}"
        base = target.read_json(f"{prefix}/scans.json")
        gs = GenStore(target, prefix)
        latest = latest_key(gs.listing("manifests"))
        have = set(scans_of(base, gs.read_json(latest)["runs"] if latest else []))
    lead = sn.layout_glob(layout)[0].split("*", 1)[0].rsplit("/", 1)[0]
    selected = sn.pick_scans(((k, {"size": n}) for k, n in source.listing(lead).items()), [layout], through=through)
    pending = [s for s in selected if s["id"] not in have]
    if have and pending and pending[0]["id"] < max(have):
        raise ValueError("age-store: an older unindexed scan requires a new generation")
    if not pending:
        return {"gen": current, "added": [], "files": 0}
    inputs = work / "inputs"
    # find_scans reads the source root's keys; INPUT_LAYOUT is relative to work.
    for s in pending:
        cells(source, s["src"], inputs / s["id"] / "path-index.parquet", work / "cells" / s["id"], threads=threads, mem=mem)
    args = ["-L", INPUT_LAYOUT, "-s", str(work), "-o", str(local), "-w", str(work / "build"), "-p", str(threads), "-M", mem]
    if current:
        il.add_cmd.main([*args, "-C", compact], standalone_mode=False)
    else:
        il.build_cmd.main([*args, "-g", gen, "-k", "4"], standalone_mode=False)
    local_store = Store(str(local))
    results = []
    for s in pending:
        marker = verify(str(local), s["id"], inputs / s["id"] / "path-index.parquet", work / "verify" / s["id"], threads=threads, mem=mem)
        results.append(marker)
    for marker in results:
        target.write_bytes(f"{MARKERS}/{marker['scan']}.json", (json.dumps(marker) + "\n").encode())
    after = local_store.listing("interval-store")
    pointer = "interval-store/CURRENT.json"
    # Unpublished files are not trusted: corrected inputs can rebuild different bytes at the same size.
    changed = [k for k, n in after.items() if k != pointer and (not current or before.get(k) != n)]
    def publish_rank(key: str) -> tuple[int, str]:
        name = key.rsplit("/", 1)[-1]
        rank = 4 if name == "successor.json" else 3 if "/manifests/" in key else 2 if name == "scans.json" else 1 if name == "meta.json" else 0
        return rank, key

    for k in sorted(changed, key=publish_rank):
        target.put(local / k, k)
    target.write_bytes(pointer, local_store.read_bytes(pointer))
    # Native carries/compaction's grace governs these keys. Publication completed first.
    target.delete(sorted(set(before) - set(after)))
    shutil.rmtree(inputs)
    return {"gen": ic.current(local_store), "added": results, "files": len(changed)}


@command("age-store")
@option("-c", "--compact", default="level:5", help="Interval-store compaction policy")
@option("-D", "--no-d1", is_flag=True, help="Retire files without clearing D1 (copies after the first pass, or no D1 deployment)")
@option("-g", "--gen", required=True, help="Initial generation (the current pointer is followed after bootstrap)")
@option("-k", "--keep", default=2, type=int, help="Recent scans whose histogram inputs stay")
@option("-L", "--layout", required=True, help="Finest pyramid key template, with {id} and optional {gen}")
@option("-M", "--mem", default=MEM, help="DuckDB memory limit, every step (default sized for hf's ~270K cells; gcs's finest tier is far larger)")
@option("-o", "--out", required=True, help="Independent age store root, e.g. r2://bucket/history/primary-age")
@option("-p", "--threads", default=THREADS, type=int, help="DuckDB threads, every step")
@option("-r", "--retire-root", multiple=True, help="Root whose verified histogram copies may retire, including the source")
@option("-s", "--source", required=True, help="Root holding existing per-scan pyramids")
@option("-T", "--site-store", default="primary", help="D1 site store key")
@option("-t", "--through", help="Last scan to include")
@option("-w", "--work", required=True, type=Path, help="Local build and verification scratch directory")
@option("-x", "--execute", is_flag=True, help="Delete verified, superseded histogram files and footers; otherwise print the plan")
def age_store_cmd(
    compact: str,
    no_d1: bool,
    gen: str,
    keep: int,
    layout: str,
    mem: str,
    out: str,
    threads: int,
    retire_root: tuple[str, ...],
    source: str,
    site_store: str,
    through: str | None,
    work: Path,
    execute: bool,
) -> None:
    """Append age histogram changes using the canonical interval engine; verify before publishing."""
    result = update(source, layout, out, gen, work, through=through, compact=compact, threads=threads, mem=mem)
    result["retired"] = [retire(root, layout, out, keep, execute=execute,
                                site_store=None if no_d1 or i else site_store)
                         for i, root in enumerate(dict.fromkeys(retire_root))]
    print(json.dumps(result))
