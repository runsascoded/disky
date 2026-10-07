"""Subtract drained runs' logged identities from an already-validated DR."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import duckdb
import fsspec
import pyarrow.parquet as pq

from .sweep_manifest import ManifestProgress


def subtract_settled(
    source: str,
    runs: tuple[str, ...],
    dry: dict,
    files: dict[str, list[Path]],
    work: Path,
    progress: ManifestProgress,
) -> tuple[dict, dict[str, list[Path]], dict]:
    """Never re-list targets; exclude only identities reconciled to this DR.

    Failed decisions remain eligible. Cached DR parts are atomically replaced
    by their remainder after validating every exclusion's name/generation,
    size and directory. Original pinned artifacts stay untouched remotely.
    """
    from .sweep_reviewed import pin_path
    from .sweep_exec import err

    adjusted = deepcopy(dry)
    remaining = {}
    provenance: dict[str, Any] = {"runs": [], "buckets": {}}
    histories = []
    for index, run in enumerate(runs):
        progress.set_phase("resume-summary", run)
        fs, path = fsspec.core.url_to_fs(run, version_aware=True)
        pinned = pin_path(fs, f"{path}/deleted-summary.json")
        with fs.open(pinned) as fh:
            summary = json.load(fh)
        if summary.get("for_real") is not True or summary.get("plan") != run or summary.get("reviewed_source") != source:
            raise ValueError("resume requires a drained real run against this exact reviewed source")
        # An earlier resume chain must be supplied in full, not silently lost.
        if any(parent not in runs for parent in summary.get("resume_runs", [])):
            raise ValueError("resume requires every earlier run in the exclusion chain")
        histories.append((index, fs, path, summary))
        provenance["runs"].append({"run": run, "summary": pinned})
    for bucket, parts in files.items():
        directory = work / "resume" / bucket
        directory.mkdir(parents=True, exist_ok=False)
        with duckdb.connect() as db:
            db.execute("SET memory_limit='512MB'")
            db.execute("SET threads=4")
            db.execute("SET temp_directory = ?", [str(directory / "spill")])
            db.execute("CREATE TABLE settled(name VARCHAR, generation BIGINT, size_bytes BIGINT, dir VARCHAR)")
            pins = []
            for index, fs, path, summary in histories:
                entry = summary["buckets"].get(bucket)
                if entry is None:
                    continue
                remote = sorted(fs.glob(f"{path}/deleted/{bucket}/part-*.parquet"))
                single = f"{path}/deleted/{bucket}.parquet"
                if fs.exists(single):
                    remote.insert(0, single)
                if not remote and sum(entry.get("decisions", {}).values()):
                    raise ValueError(f"{bucket}: drained run is missing decision parts")

                def cache(task: tuple[int, str]) -> tuple[str, str]:
                    number, part = task
                    pinned = pin_path(fs, part)
                    local = directory / f"run-{index:02d}-part-{number:05d}.parquet"
                    progress.set_phase("resume-cache", part)
                    fs.get_file(pinned, str(local))
                    return str(local), pinned

                with ThreadPoolExecutor(max_workers=8) as pool:
                    cached = list(pool.map(cache, enumerate(remote)))
                if not cached:
                    continue
                local = [item[0] for item in cached]
                pins.extend(item[1] for item in cached)
                progress.set_phase("resume-reconcile", f"{bucket}: drained run {index + 1}/{len(histories)}")
                tally = db.execute("SELECT decision, count(*), coalesce(sum(size_bytes), 0) FROM read_parquet(?) GROUP BY decision", [local]).fetchall()
                actual = {decision: count for decision, count, _ in tally}
                expected = {key: value for key, value in entry.get("decisions", {}).items() if value}
                delete_bytes = sum(size for decision, _, size in tally if decision == "delete")
                if actual != expected or delete_bytes != entry.get("delete_bytes", 0):
                    raise ValueError(f"{bucket}: drained decision log does not match its final summary")
                db.execute("INSERT INTO settled SELECT name, generation, size_bytes, dir FROM read_parquet(?) WHERE decision IN ('delete', 'skipped_gone', 'skipped_overwritten')", [local])
            original = [str(part) for part in parts]
            progress.set_phase("resume-identities", bucket)
            db.execute("CREATE TABLE unique_settled AS SELECT DISTINCT * FROM settled")
            if db.execute("SELECT count(*) FROM unique_settled ANTI JOIN (SELECT name, generation, size_bytes, dir FROM read_parquet(?) WHERE decision='delete') USING(name, generation, size_bytes, dir)", [original]).fetchone()[0]:
                raise ValueError(f"{bucket}: exclusion identity is outside the reviewed delete set")
            excluded, excluded_bytes = db.execute("SELECT count(*), coalesce(sum(size_bytes), 0) FROM unique_settled").fetchone()
            before = adjusted["buckets"][bucket]
            if excluded > before["decisions"].get("delete", 0) or excluded_bytes > before.get("delete_bytes", 0):
                raise ValueError(f"{bucket}: exclusion totals exceed the reviewed delete set")
            if excluded:
                count = size = 0
                for number, part in enumerate(parts):
                    progress.set_phase("resume-filter", f"{bucket}: part {number + 1}/{len(parts)}")
                    temp = part.with_suffix(".remaining.parquet")
                    target = str(temp).replace("'", "''")
                    db.execute(f"COPY (SELECT original.* FROM read_parquet(?) original ANTI JOIN unique_settled USING(name, generation) WHERE original.decision='delete') TO '{target}' (FORMAT PARQUET, ROW_GROUP_SIZE 65536)", [str(part)])
                    count += pq.ParquetFile(temp).metadata.num_rows
                    size += db.execute("SELECT coalesce(sum(size_bytes), 0) FROM read_parquet(?)", [str(temp)]).fetchone()[0]
                    temp.replace(part)
                if count != before["decisions"].get("delete", 0) - excluded or size != before.get("delete_bytes", 0) - excluded_bytes:
                    raise ValueError(f"{bucket}: remainder totals failed reconciliation")
                before["decisions"] = {"delete": count}
                before["delete_bytes"] = size
            if before["decisions"].get("delete", 0):
                remaining[bucket] = parts
            provenance["buckets"][bucket] = {"parts": pins, "excluded_objects": excluded, "excluded_bytes": excluded_bytes}
            err(f"resume: {bucket}: {excluded:,} settled identities excluded; {before['decisions'].get('delete', 0):,} remain", flush=True)
    return adjusted, remaining, provenance
