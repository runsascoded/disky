"""Build an unpublished static-name generation over a retained scan subset.

Project canonical versions, then rebuild every suffix/catalog/drill/anchor artifact
from those projected versions. Reusing old derived roots would retain transitions
that the removed scans alone observed. Source files, pointers and successors are
never changed; publication and cutover belong to the caller.
"""
from __future__ import annotations

import json
from pathlib import Path
from time import monotonic

import pyarrow.parquet as pq

from . import static_anchors as an
from . import static_append as sa
from . import static_compact as sk
from . import static_local as sl
from . import static_names as sn
from .interval_thinning import project_sql
from .scan_id import check_order, check_scan_id


def _project_range(
    con,
    source: Path,
    out: Path,
    i: int,
    kept: list[int],
    rule,
) -> dict:
    """A canonical range projected/coalesced once, with fresh histogram and stats."""
    started = monotonic()
    name = f"r{i:04d}"
    src = f"read_parquet({sn.q(str(source))})"
    con.execute(f"CREATE OR REPLACE TABLE thinned_civ AS {project_sql(f'SELECT * FROM {src}', sn.CINTERVAL_SCHEMA.names, kept, key=('depth', 'path', 'usr'))}")
    rows = sn.write_sorted(
        sn._batches(con, "SELECT * FROM thinned_civ ORDER BY depth, path, usr, vf"),
        out / "cintervals" / f"{name}.parquet",
        sn.CINTERVAL_SCHEMA,
        sn.INTERVAL_RG,
        dictionary=["usr"],
    )
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(sn.hist_sql("thinned_civ", rule)).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=sn.CODEC)
    before_dirs, before_suffix = con.execute(sn.suffix_count_sql(src)).fetchone()
    after_dirs, after_suffix = con.execute(sn.suffix_count_sql("thinned_civ")).fetchone()
    stats = {
        "range": i,
        "versions": [pq.ParquetFile(source).metadata.num_rows, rows],
        "dir_versions": [before_dirs, after_dirs],
        "suffix_rows": [before_suffix, after_suffix],
        "s": round(monotonic() - started, 1),
    }
    (out / "cstats").mkdir(parents=True, exist_ok=True)
    (out / "cstats" / f"{name}.json").write_text(json.dumps(stats, sort_keys=True) + "\n")
    con.execute("DROP TABLE thinned_civ")
    return stats


def fold_thinned(
    gen_dir: Path,
    out: Path,
    work: Path,
    *,
    gen: str,
    new_gen: str,
    runs: list[dict],
    retained: list[str],
    threads: int = 4,
    mem: str = "4GB",
    chunk_rows: int = sl.CHUNK_ROWS,
    jobs: int = 1,
) -> dict:
    """Fold base + runs, project to retained snapshots, rebuild an offline candidate.

    ``runs`` is the source's selected manifest stack, oldest first (including
    merged runs). Per-scan canonical deltas and scan docs retained under
    ``deltas/<id>/`` are sufficient; raw listing/path-index files are not read.
    Both source endpoints must remain. Output must be absent/empty, and the
    private ``work/static-thinning`` directory must be new. This function does
    not resume or overwrite an existing candidate.

    Preserve the source's key ranges, suffix shard target, catalog membership
    bound, hex rule and drill R/K/floor. Anchored indexes are rebuilt when the
    source has them. Derived reconstruction can be expensive; schedule it as
    an offline job rather than a request handler.
    """
    started = monotonic()
    gen_dir, out, work = Path(gen_dir), Path(out), Path(work)
    source_path, out_path = gen_dir.resolve(), out.resolve()
    if gen == new_gen:
        raise ValueError("thinning requires a new generation name")
    if source_path == out_path or source_path in out_path.parents or out_path in source_path.parents:
        raise ValueError("source and candidate directories must not overlap")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"candidate output is not empty: {out}")
    private = work / "static-thinning"
    if private.resolve() == source_path or source_path in private.resolve().parents:
        raise ValueError("thinning work must be outside the source generation")
    if private.resolve() == out_path or out_path in private.resolve().parents:
        raise ValueError("thinning work must be outside the candidate output")
    if private.exists():
        raise FileExistsError(f"thinning work already exists: {private}")

    base = json.loads((gen_dir / "scans.json").read_text())
    entries = [*base["scans"], *sk.run_scans(gen_dir, gen, runs)]
    source_ids = [check_scan_id(entry["id"]) for entry in entries]
    if check_order(source_ids) != source_ids:
        raise ValueError("source scans must be in chronological order")
    ids = check_order([check_scan_id(scan) for scan in retained])
    if not ids:
        raise ValueError("retained scans must not be empty")
    unknown = sorted(set(ids) - set(source_ids))
    if unknown:
        raise ValueError(f"retained scans are absent from the source: {unknown!r}")
    if not source_ids or ids[0] != source_ids[0] or ids[-1] != source_ids[-1]:
        raise ValueError("retained scans must preserve both source endpoints")
    keep = set(ids)
    doc = {**base, "scans": [entry for entry in entries if entry["id"] in keep]}
    stamps = [entry["ts"] for entry in doc["scans"]]
    ranges = json.loads((gen_dir / "ranges.json").read_text())
    shards = json.loads((gen_dir / "shards.json").read_text())
    membership = json.loads((gen_dir / "catalog" / "meta.json").read_text())["membership"]
    drill = json.loads((gen_dir / "drill" / "meta.json").read_text())
    rule = sn.gen_rule(base)

    private.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)
    folded = private / "folded"
    con = sn.connect(threads, mem, private / "duckdb")
    try:
        days = [scan for run in runs for scan in run["scans"]]
        before_versions = sk.fold_ranges(con, gen_dir, days, ranges, folded, rule, jobs)
        stats = [
            _project_range(con, folded / "cintervals" / f"r{r['i']:04d}.parquet", out, r["i"], stamps, rule)
            for r in ranges["ranges"]
        ]
    finally:
        con.close()
    built = sl.build_gen(
        doc,
        "",
        out,
        private / "rebuild",
        gen=new_gen,
        rule=rule,
        shard_rows=shards["target_rows"],
        max_rows=membership["max_rows"],
        R=drill["R"],
        K=drill["K"],
        floor=drill.get("floor", sl.FLOOR),
        chunk_rows=chunk_rows,
        threads=threads,
        mem=mem,
        with_anchors=(gen_dir / an.ANCHORS / "meta.json").is_file(),
        ranges=ranges,
        canonical_ready=True,
    )
    (out / "manifests").mkdir()
    (out / "manifests" / f"{ids[-1]}.json").write_text(json.dumps(sa.manifest(new_gen, ids, []), indent=1) + "\n")
    summary = {
        "from": gen,
        "gen": new_gen,
        "runs": [run["key"] for run in runs],
        "source_scans": source_ids,
        "retained": ids,
        "removed": [scan for scan in source_ids if scan not in keep],
        "versions": [before_versions, sum(item["versions"][1] for item in stats)],
        "build": built,
        "s": round(monotonic() - started, 1),
    }
    (out / "thinning.json").write_text(json.dumps(summary, indent=1) + "\n")
    return summary
