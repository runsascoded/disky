"""CoreWeave S3 plan-driven deletion (specs/staged-delete.md).

The flow is stage -> dispatch -> run. A **plan** is the staged set (or an
admin's curated list of prefixes); the plan *is* the eligibility decision, so
no classification / owner logic applies.

This module carries the plan model, the CAIOS boto3 client + the
versioning-enabled guard, and the **manifest builder**: it expands a plan's
prefixes into an object-level manifest from the pinned layer-2 parquet (the
canonical per-object scan output at `cw-l2/<date>/<bucket>.parquet`), so a run
only ever deletes what was reviewed. The executor (the boto3 delete loop)
consumes this manifest.

A key is swept iff some plan prefix covers it. The layer-2 parquet has no
ETag, so the run's overwrite guard keys off (size, mtime) captured here, not a
version id.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client



def sweep_endpoint() -> str:
    """The S3 endpoint a sweep deletes through (`$SWEEP_S3_ENDPOINT`; `$CW_ENDPOINT`
    deprecated). Some S3-compatible stores reject path-style requests and ignore
    the region; boto3 still requires both set (see `s3_client`)."""
    from .deploy import require
    return require("SWEEP_S3_ENDPOINT", "CW_ENDPOINT", what="the S3 endpoint a sweep deletes through")


def sweep_bucket() -> str:
    """The bucket a sweep deletes from when its plan names none (`$SWEEP_BUCKET`;
    `$CW_BUCKET` deprecated)."""
    from .deploy import require
    return require("SWEEP_BUCKET", "CW_BUCKET", what="the bucket a sweep deletes from")

# S3 `delete_objects` accepts up to 1000 keys per call.
DELETE_BATCH = 1000

# A plan prefix, once normalized to a relative key prefix: non-empty, no scheme,
# no leading slash, trailing slash, no `.`/`..` segments or backslashes.
PREFIX_RE = re.compile(r"^(?!/)(?![.]{1,2}/)[^\\]+/$")


class SweepError(Exception):
    """A caller-fixable sweep error (bad plan, versioning off, missing input)."""


@dataclass
class Plan:
    """A deletion plan: the prefixes to sweep.

    Prefixes are relative key prefixes (e.g. `marin/checkpoints/old/`), already
    stripped of any `s3://<bucket>/` scheme and normalized to a trailing slash.
    """

    name: str
    bucket: str
    sweep: list[str]
    plan_id: int | None = None

    def validate(self) -> None:
        if not self.sweep:
            raise SweepError(f"plan {self.name!r} has no sweep prefixes")
        for p in self.sweep:
            if not PREFIX_RE.match(p):
                raise SweepError(f"bad prefix {p!r} (want a relative key prefix ending in '/')")


def normalize_prefix(raw: str, bucket: str) -> str:
    """`s3://bucket/a/b/` or `/a/b` or `a/b` -> `a/b/` (relative, trailing slash)."""
    s = raw.strip()
    s = re.sub(r"^s3://", "", s)
    if s.startswith(f"{bucket}/"):
        s = s[len(bucket) + 1 :]
    s = s.lstrip("/")
    if not s.endswith("/"):
        s += "/"
    return s


def load_plan(path: str | Path) -> Plan:
    """Read a plan.json (as written by /api/plan-sweep/dispatch), normalizing prefixes."""
    d = json.loads(Path(path).read_text())
    bucket = d.get("bucket") or sweep_bucket()
    plan = Plan(
        name=d["name"],
        bucket=bucket,
        sweep=[normalize_prefix(p, bucket) for p in d.get("sweep", [])],
        plan_id=d.get("plan_id"),
    )
    plan.validate()
    return plan


def s3_client(endpoint: str | None = None) -> "S3Client":
    """boto3 S3 client for the sweep's store (`endpoint`, default
    `sweep_endpoint()`). Creds come from the env (Secret Manager on Batch:
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint or sweep_endpoint(),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "virtual"}, retries={"max_attempts": 10, "mode": "standard"}),
    )


def versioning_enabled(client: "S3Client", bucket: str) -> bool:
    """The delete-safety guard: a real sweep is refused unless the bucket has
    versioning `Status=Enabled`, so every delete writes a recoverable delete
    marker (CAIOS has no separate soft-delete; see specs/cw-sweep.md)."""
    resp = client.get_bucket_versioning(Bucket=bucket)
    return resp.get("Status") == "Enabled"


def _eligible_query() -> str:
    """DuckDB SELECT of the file rows eligible under a plan.

    Reads the layer-2 parquet from the `L2` DuckDB variable; `$sweep` binds a
    list of relative key prefixes. A row is kept iff some sweep prefix covers
    its path.
    """
    return """
        SELECT
          path AS name,
          size AS size_bytes,
          mtime,
          CASE WHEN path LIKE '%/%' THEN regexp_replace(path, '/[^/]*$', '/') ELSE '' END AS dir
        FROM read_parquet(getvariable('L2'))
        WHERE kind = 'file'
          AND len(list_filter($sweep, p -> starts_with(path, p))) > 0
    """


def build_manifest(l2_path: str, plan: Plan, out_dir: str) -> dict:
    """Expand `plan` against the layer-2 parquet at `l2_path` into an object-level
    manifest under `out_dir` (`manifest/<bucket>.parquet` + `plan-summary.json`).

    Returns the summary dict. Deletes nothing; pure read + artifact write.
    """
    plan.validate()
    out = Path(out_dir)
    (out / "manifest").mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest" / f"{plan.bucket}.parquet"

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '8GB')}'")
    con.execute("SET VARIABLE L2 = ?", [l2_path])
    params = {"sweep": plan.sweep}
    con.execute(
        f"COPY ({_eligible_query()} ORDER BY name) TO '{manifest_path}' (FORMAT PARQUET)",
        params,
    )
    objects, byts = con.execute(
        f"SELECT count(*), coalesce(sum(size_bytes), 0) FROM read_parquet('{manifest_path}')"
    ).fetchone()

    summary = {
        "plan_id": plan.plan_id,
        "name": plan.name,
        "bucket": plan.bucket,
        "sweep": plan.sweep,
        "objects": int(objects),
        "bytes": int(byts),
        "manifest": str(manifest_path),
    }
    (out / "plan-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


# --- executor -------------------------------------------------------------

def prefix_free(prefixes: list[str]) -> list[str]:
    """Minimal prefix-free cover: drop any prefix nested under another (so we
    list each listing root once). Sorted, so an ancestor precedes its descendants."""
    out: list[str] = []
    for p in sorted(set(prefixes)):
        if not any(p != q and p.startswith(q) for q in out):
            out.append(p)
    return out


def eligible(key: str, sweep: list[str]) -> bool:
    """A key is swept iff some sweep prefix covers it (same rule as the manifest SQL)."""
    return any(key.startswith(p) for p in sweep)


def _dir_of(key: str) -> str:
    return key.rsplit("/", 1)[0] + "/" if "/" in key else ""


def _write_log(path: Path, rows: list[tuple]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    tbl = pa.table({
        "name": [r[0] for r in rows],
        "size_bytes": pa.array([r[1] for r in rows], pa.int64()),
        "mtime": pa.array([r[2] for r in rows], pa.int64()),
        "decision": [r[3] for r in rows],
        "dir": [r[4] for r in rows],
        "band": [r[5] for r in rows],
    })
    pq.write_table(tbl, path)


def execute_plan(
    run_dir: str,
    *,
    for_real: bool = False,
    client: "S3Client | None" = None,
    delete_workers: int = 16,
    mtime_tol: int = 1,
    require_versioning: bool = True,
) -> dict:
    """Execute the manifest under `run_dir` against CoreWeave S3 (boto3).

    Lists each curated root, merge-checks every live object against the reviewed
    manifest, and deletes only the reviewed keys whose (size, mtime) still match
    — new keys since the scan are left alone (drift), changed ones skipped
    (overwritten), vanished ones noted (gone). A real delete writes a recoverable
    delete marker (guarded by bucket versioning); a dry run touches nothing.
    `require_versioning=False` skips that guard — deletes are then PERMANENT —
    loudly, and the summary records `versioning_guard: false`.

    Writes a decision-log parquet + `<deleted|would-delete>-summary.json` under
    `run_dir` and returns the summary. Does NOT write D1 — the Functions layer
    reflects this summary into `deletion_runs`/`deletion_bands`.
    """
    started = int(time.time())
    run = Path(run_dir)
    plan_summary = json.loads((run / "plan-summary.json").read_text())
    bucket = plan_summary["bucket"]
    sweep = plan_summary["sweep"]
    roots = prefix_free(sweep)

    client = client or s3_client()
    if for_real and require_versioning and not versioning_enabled(client, bucket):
        raise SweepError(
            f"refusing real delete: bucket {bucket} versioning is not Status=Enabled "
            "(no recoverable delete marker; see specs/cw-sweep.md)"
        )
    if for_real and not require_versioning:
        print(f"WARNING: versioning guard disabled — deletes are permanent (bucket {bucket})", file=sys.stderr)

    manifest_path = run / "manifest" / f"{bucket}.parquet"
    manifest = {
        name: (size, mtime)
        for name, size, mtime in duckdb.connect()
        .execute(f"SELECT name, size_bytes, mtime FROM read_parquet('{manifest_path}') ORDER BY name")
        .fetchall()
    }

    counters = {k: 0 for k in ("deleted_objects", "deleted_bytes", "skipped_gone",
                               "skipped_overwritten", "drift_new", "delete_failed")}
    bands: dict[str, dict] = {}

    def band_rec(b: str) -> dict:
        return bands.setdefault(b, {k: 0 for k in ("bytes", "objects", "gone", "overwritten", "drift_new")})

    seen: set[str] = set()
    log_rows: list[tuple] = []
    to_delete: list[tuple[str, int, str]] = []

    for root in roots:
        token: str | None = None
        while True:
            kw = {"Bucket": bucket, "Prefix": root}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                band = next((p for p in sorted(roots, key=len, reverse=True) if key.startswith(p)), root)
                if key in manifest:
                    seen.add(key)
                    size, mtime = manifest[key]
                    live_mtime = int(obj["LastModified"].timestamp())
                    if obj["Size"] == size and abs(live_mtime - mtime) <= mtime_tol:
                        to_delete.append((key, size, band))
                        log_rows.append((key, size, mtime, "delete", _dir_of(key), band))
                    else:
                        counters["skipped_overwritten"] += 1
                        band_rec(band)["overwritten"] += 1
                        log_rows.append((key, size, mtime, "skipped_overwritten", _dir_of(key), band))
                elif eligible(key, sweep):
                    # live, under a swept prefix, but not in the reviewed manifest → new since scan
                    counters["drift_new"] += 1
                    band_rec(band)["drift_new"] += 1
                # else: outside the plan — expected, ignore
            token = resp.get("NextContinuationToken")
            if not resp.get("IsTruncated"):
                break

    for key, (size, mtime) in manifest.items():
        if key not in seen:
            counters["skipped_gone"] += 1
            band = next((p for p in sorted(roots, key=len, reverse=True) if key.startswith(p)), "")
            band_rec(band)["gone"] += 1
            log_rows.append((key, size, mtime, "skipped_gone", _dir_of(key), band))

    def _apply(batch: list[tuple[str, int, str]]) -> tuple[list, set]:
        resp = client.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k, _, _ in batch], "Quiet": True}
        )
        return batch, {e["Key"] for e in resp.get("Errors", [])}

    if for_real and to_delete:
        with ThreadPoolExecutor(max_workers=delete_workers) as ex:
            batches = [to_delete[i:i + DELETE_BATCH] for i in range(0, len(to_delete), DELETE_BATCH)]
            for batch, errs in ex.map(_apply, batches):
                for key, size, band in batch:
                    if key in errs:
                        counters["delete_failed"] += 1
                    else:
                        counters["deleted_objects"] += 1
                        counters["deleted_bytes"] += size
                        band_rec(band)["objects"] += 1
                        band_rec(band)["bytes"] += size
    else:
        for _key, size, band in to_delete:
            counters["deleted_objects"] += 1
            counters["deleted_bytes"] += size
            band_rec(band)["objects"] += 1
            band_rec(band)["bytes"] += size

    mode = "deleted" if for_real else "would-delete"
    log_dir = run / mode / bucket
    log_dir.mkdir(parents=True, exist_ok=True)
    _write_log(log_dir / "part-00000.parquet", sorted(log_rows))

    out_summary = {
        "plan_id": plan_summary.get("plan_id"),
        "name": plan_summary.get("name"),
        "bucket": bucket,
        "mode": "real" if for_real else "dry",
        "versioning_guard": require_versioning,
        **counters,
        "bands": [{"prefix": b, **v} for b, v in sorted(bands.items())],
        "started_ts": started,
        "finished_ts": int(time.time()),
        "log_dir": str(log_dir),
    }
    (run / f"{mode}-summary.json").write_text(json.dumps(out_summary, indent=2) + "\n")
    return out_summary


# --- undo / purge (Phase 3) ------------------------------------------------

def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _deleted_keys(run: Path, bucket: str) -> list[str]:
    """Keys a real run actually deleted, from its decision-log part-files."""
    parts = sorted((run / "deleted" / bucket).glob("part-*.parquet"))
    if not parts:
        return []
    glob = str(run / "deleted" / bucket / "part-*.parquet")
    rows = duckdb.connect().execute(
        f"SELECT name FROM read_parquet('{glob}') WHERE decision = 'delete' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _list_versions(client: "S3Client", bucket: str, prefix: str):
    """Paginate `list_object_versions` under a prefix (Versions[] + DeleteMarkers[])."""
    key_marker: str | None = None
    ver_marker: str | None = None
    while True:
        kw: dict = {"Bucket": bucket, "Prefix": prefix}
        if key_marker:
            kw["KeyMarker"] = key_marker
        if ver_marker:
            kw["VersionIdMarker"] = ver_marker
        page = client.list_object_versions(**kw)
        yield page
        if not page.get("IsTruncated"):
            return
        key_marker = page.get("NextKeyMarker")
        ver_marker = page.get("NextVersionIdMarker")


def undo_run(run_dir: str, *, client: "S3Client | None" = None,
             prefixes: list[str] | None = None, dry_run: bool = False) -> dict:
    """Undo a real run: remove the delete markers it wrote, so the prior version
    is current again (CAIOS's recoverable-delete). Must run before `purge`."""
    run = Path(run_dir)
    plan_summary = json.loads((run / "plan-summary.json").read_text())
    bucket = plan_summary["bucket"]
    roots = prefix_free(plan_summary["sweep"])
    client = client or s3_client()

    keys = set(_deleted_keys(run, bucket))
    if prefixes:
        keys = {k for k in keys if any(k.startswith(p) for p in prefixes)}

    to_restore: list[tuple[str, str]] = []  # (key, delete-marker versionId)
    for root in roots:
        for page in _list_versions(client, bucket, root):
            for dm in page.get("DeleteMarkers", []):
                if dm.get("IsLatest") and dm["Key"] in keys:
                    to_restore.append((dm["Key"], dm["VersionId"]))

    counters = {"restored": 0, "restore_failed": 0, "skipped": len(keys) - len({k for k, _ in to_restore})}
    if dry_run:
        counters["restored"] = len(to_restore)
    else:
        for batch in _chunks(to_restore, DELETE_BATCH):
            resp = client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k, "VersionId": v} for k, v in batch], "Quiet": True}
            )
            errs = {e["Key"] for e in resp.get("Errors", [])}
            for k, _v in batch:
                counters["restore_failed" if k in errs else "restored"] += 1

    summary = {"mode": "undo", "bucket": bucket, "dry_run": dry_run,
               "candidates": len(keys), **counters, "finished_ts": int(time.time())}
    (run / "undo-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def purge_run(run_dir: str, *, client: "S3Client | None" = None, dry_run: bool = False) -> dict:
    """Permanently drop every version of a real run's deleted keys — the second,
    irreversible stage that actually reclaims space (after the undo hold). Removes
    both the delete markers and the shadowed noncurrent data versions."""
    run = Path(run_dir)
    plan_summary = json.loads((run / "plan-summary.json").read_text())
    bucket = plan_summary["bucket"]
    roots = prefix_free(plan_summary["sweep"])
    client = client or s3_client()

    keys = set(_deleted_keys(run, bucket))
    victims: list[tuple[str, str, int]] = []  # (key, versionId, size) for every version of a deleted key
    for root in roots:
        for page in _list_versions(client, bucket, root):
            for v in page.get("Versions", []):
                if v["Key"] in keys:
                    victims.append((v["Key"], v["VersionId"], int(v.get("Size", 0))))
            for dm in page.get("DeleteMarkers", []):
                if dm["Key"] in keys:
                    victims.append((dm["Key"], dm["VersionId"], 0))

    counters = {"purged_versions": 0, "purge_failed": 0, "purged_bytes": 0}
    if dry_run:
        counters["purged_versions"] = len(victims)
        counters["purged_bytes"] = sum(sz for _k, _v, sz in victims)
    else:
        for batch in _chunks(victims, DELETE_BATCH):
            resp = client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k, "VersionId": v} for k, v, _ in batch], "Quiet": True}
            )
            errs = {e["Key"] for e in resp.get("Errors", [])}
            for k, _v, sz in batch:
                if k in errs:
                    counters["purge_failed"] += 1
                else:
                    counters["purged_versions"] += 1
                    counters["purged_bytes"] += sz

    summary = {"mode": "purge", "bucket": bucket, "dry_run": dry_run,
               "keys": len(keys), **counters, "finished_ts": int(time.time())}
    (run / "purge-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
