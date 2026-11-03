"""CoreWeave S3 plan-driven deletion (specs/done/staged-delete.md).

The flow is stage -> dispatch -> run. A **plan** is the staged set (or an
admin's curated list of prefixes); the plan *is* the eligibility decision, so
no classification / owner logic applies.

This module carries the plan model, the CAIOS boto3 client, and the
**manifest builder**: it expands a plan's prefixes into an object-level
manifest from the pinned layer-2 parquet (the
canonical per-object scan output at `cw-l2/<date>/<bucket>.parquet`), so a run
only ever deletes what was reviewed. The executor (the boto3 delete loop)
consumes this manifest.

A key is swept iff some plan prefix covers it. Its reviewed opaque `version`
is the ETag used in the atomic delete precondition, alongside a size/mtime
check at LIST. Unknown identities cannot authorize deletion.

A plan item staged "as of" another scan than the pinned one (plan.json
`as_of`) holds back what changed since: a key under it is swept only if that
scan's layer-2 has it with the same size, mtime and object version.
What is held back is counted as `skipped_after_as_of`.

A plan's `objects` are exact items (specs/done/file-assign.md): relative keys that
match `path == key` only — never `key.bak` or `key/…`. The kind is which list
an item is in; an exact key ending in `/` is refused.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

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
# An exact item, once normalized to a relative key: non-empty, no leading or
# trailing slash, no empty / `.` / `..` segments or backslashes.
OBJECT_RE = re.compile(r"^(?!/)[^\\]+(?<!/)$")


class SweepError(Exception):
    """A caller-fixable sweep error (bad plan, missing input)."""


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
    #: The scan each item was staged against, by relative prefix or key (absent = the pinned scan).
    as_of: dict[str, str] = field(default_factory=dict)
    #: Exact items: relative keys, each matching only itself.
    objects: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.sweep and not self.objects:
            raise SweepError(f"plan {self.name!r} has no items")
        for p in self.sweep:
            if not PREFIX_RE.match(p):
                raise SweepError(f"bad prefix {p!r} (want a relative key prefix ending in '/')")
        for k in self.objects:
            if not OBJECT_RE.match(k) or any(seg in ("", ".", "..") for seg in k.split("/")):
                raise SweepError(f"bad object {k!r} (want a relative key not ending in '/')")


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


def normalize_key(raw: str, bucket: str) -> str:
    """`s3://bucket/a/b.mp3` or `/a/b.mp3` or `a/b.mp3` -> `a/b.mp3` (relative;
    the trailing slash is kept, so `validate` refuses a folder-shaped key)."""
    s = re.sub(r"^s3://", "", raw.strip())
    if s.startswith(f"{bucket}/"):
        s = s[len(bucket) + 1 :]
    return s.lstrip("/")


def load_plan(path: str | Path) -> Plan:
    """Read a plan.json (as written by /api/plan-sweep/dispatch), normalizing prefixes."""
    d = json.loads(Path(path).read_text())
    bucket = d.get("bucket") or sweep_bucket()
    raw_objects = list(d.get("objects") or [])
    as_of = {
        (normalize_key(p, bucket) if p in raw_objects else normalize_prefix(p, bucket)): str(scan)
        for p, scan in (d.get("as_of") or {}).items()
    }
    plan = Plan(
        name=d["name"],
        bucket=bucket,
        sweep=[normalize_prefix(p, bucket) for p in d.get("sweep", [])],
        plan_id=d.get("plan_id"),
        as_of=as_of,
        objects=sorted({normalize_key(k, bucket) for k in raw_objects}),
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


def _eligible_query(version: str = "NULL::VARCHAR") -> str:
    """DuckDB SELECT of the file rows eligible under a plan.

    Reads the layer-2 parquet from the `L2` DuckDB variable; `$sweep` binds a
    list of relative key prefixes, `$exact` the exact keys. A row is kept iff
    some sweep prefix covers its path or it is an exact key.
    """
    return f"""
        SELECT
          path AS name,
          size AS size_bytes,
          mtime,
          {version} AS version,
          CASE WHEN path LIKE '%/%' THEN regexp_replace(path, '/[^/]*$', '/') ELSE '' END AS dir
        FROM read_parquet(getvariable('L2'))
        WHERE kind = 'file'
          AND (len(list_filter($sweep, p -> starts_with(path, p))) > 0 OR list_contains($exact, path))
    """


def build_manifest(
    l2_path: str,
    plan: Plan,
    out_dir: str,
    date: str | None = None,
    l2_for: Callable[[str], str] | None = None,
) -> dict:
    """Expand `plan` against the layer-2 parquet at `l2_path` (scan `date`) into
    an object-level manifest under `out_dir` (`manifest/<bucket>.parquet` +
    `plan-summary.json`). An item staged as of another scan keeps only the keys
    that scan's layer-2 (`l2_for(scan)`) has with the same mtime.

    Returns the summary dict. Deletes nothing; pure read + artifact write.
    """
    plan.validate()
    out = Path(out_dir)
    (out / "manifest").mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest" / f"{plan.bucket}.parquet"

    held = {p: scan for p, scan in plan.as_of.items() if (p in plan.sweep or p in plan.objects) and scan != date}
    if held and l2_for is None:
        raise SweepError(f"plan items staged as of other scans ({', '.join(sorted(set(held.values())))}) need their layer-2s")
    current = [p for p in plan.sweep if p not in held]
    current_x = [k for k in plan.objects if k not in held]

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '8GB')}'")
    con.execute("SET VARIABLE L2 = ?", [l2_path])
    params: dict = {"sweep": plan.sweep, "exact": plan.objects}
    from disk_tree.object_version import version_sql

    def identity(source: str) -> str:
        return version_sql({r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()})

    query = _eligible_query(identity("read_parquet(getvariable('L2'))"))
    if held:
        # A held key (under a held item, under no current one) stays only if
        # its item's `as_of` layer-2 vouches for it: same path, same mtime.
        by_scan: dict[str, tuple[list[str], list[str]]] = {}
        for p, scan in sorted(held.items()):
            by_scan.setdefault(scan, ([], []))[0 if p in plan.sweep else 1].append(p)
        refs = []
        for i, (scan, (prefixes, keys)) in enumerate(sorted(by_scan.items())):
            con.execute(f"SET VARIABLE AS_OF_{i} = ?", [l2_for(scan)])
            params[f"held_{i}"] = prefixes
            params[f"held_x_{i}"] = keys
            source = f"read_parquet(getvariable('AS_OF_{i}'))"
            refs.append(
                f"SELECT path, size, mtime, {identity(source)} AS version FROM {source} WHERE kind = 'file'"
                f" AND (len(list_filter($held_{i}, p -> starts_with(path, p))) > 0 OR list_contains($held_x_{i}, path))"
            )
        params["held"] = sorted(p for p in held if p in plan.sweep)
        params["held_x"] = sorted(k for k in held if k not in plan.sweep)
        params["current"] = current
        params["current_x"] = current_x
        query = f"""
            WITH e AS ({query}), ref AS ({' UNION ALL '.join(refs)})
            SELECT e.*, (
              (len(list_filter($held, p -> starts_with(e.name, p))) > 0 OR list_contains($held_x, e.name))
              AND len(list_filter($current, p -> starts_with(e.name, p))) = 0
              AND NOT list_contains($current_x, e.name)
              AND NOT EXISTS (SELECT 1 FROM ref WHERE ref.path = e.name AND ref.mtime = e.mtime
                AND ref.size = e.size_bytes AND ref.version IS NOT DISTINCT FROM e.version)
            ) AS skip,
              EXISTS (SELECT 1 FROM ref WHERE ref.path = e.name) AS reviewed_path_exists
            FROM e
        """
    else:
        query = f"SELECT *, false AS skip, false AS reviewed_path_exists FROM ({query})"
    con.execute(f"CREATE TEMP TABLE m AS {query}", params)
    con.execute(f"COPY (SELECT * EXCLUDE (skip, reviewed_path_exists) FROM m WHERE NOT skip ORDER BY name) TO '{manifest_path}' (FORMAT PARQUET)")
    preserved_path = manifest_path.with_name(f"{plan.bucket}-preserved.parquet")
    con.execute(f"""COPY (SELECT name, CASE WHEN reviewed_path_exists THEN 'changed_since_review'
        ELSE 'new_since_review' END AS reason FROM m WHERE skip ORDER BY name)
        TO '{preserved_path}' (FORMAT PARQUET)""")
    objects, byts, skipped, skipped_bytes, new_objects, changed_objects = con.execute(
        "SELECT count(*) FILTER (WHERE NOT skip), coalesce(sum(size_bytes) FILTER (WHERE NOT skip), 0),"
        " count(*) FILTER (WHERE skip), coalesce(sum(size_bytes) FILTER (WHERE skip), 0),"
        " count(*) FILTER (WHERE skip AND NOT reviewed_path_exists),"
        " count(*) FILTER (WHERE skip AND reviewed_path_exists) FROM m"
    ).fetchone()

    summary = {
        "plan_id": plan.plan_id,
        "name": plan.name,
        "bucket": plan.bucket,
        "sweep": plan.sweep,
        **({"approved_objects": plan.objects} if plan.objects else {}),
        "as_of": dict(sorted(held.items())),
        "objects": int(objects),
        "bytes": int(byts),
        "skipped_after_as_of": {"objects": int(skipped), "bytes": int(skipped_bytes),
                                "new_objects": int(new_objects), "changed_objects": int(changed_objects)},
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


def eligible(key: str, sweep: list[str], objects: list[str] | set[str] = ()) -> bool:
    """A key is swept iff some sweep prefix covers it or it is an exact item
    (same rule as the manifest SQL)."""
    return key in objects or any(key.startswith(p) for p in sweep)


def band_for(key: str, sweep: list[str], objects: set[str], default: str = "") -> str:
    """The band a key is accounted to: its exact item, else the longest sweep
    prefix covering it (`default` when none does)."""
    if key in objects:
        return key
    return max((p for p in sweep if key.startswith(p)), key=len, default=default)


def listing_roots(plan_summary: dict) -> list[str]:
    """Where to list: the prefix-free cover of the sweep prefixes and the
    exact keys (a key lists as `Prefix=key`, which also returns `key.bak` —
    callers match exact items by equality)."""
    return prefix_free([*plan_summary["sweep"], *plan_summary.get("approved_objects", [])])


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
) -> dict:
    """Execute the manifest under `run_dir` against CoreWeave S3 (boto3).

    Lists each curated root, merge-checks every live object against the reviewed
    manifest, and deletes only the reviewed keys whose (size, mtime) still match
    — new keys since the scan are left alone (drift), changed ones skipped
    (overwritten), vanished ones noted (gone). Bucket versioning is not required
    or queried: a real deletion may be permanent. A dry run touches nothing.

    Writes a decision-log parquet + `<deleted|would-delete>-summary.json` under
    `run_dir` and returns the summary. Does NOT write D1 — the Functions layer
    reflects this summary into `deletion_runs`/`deletion_bands`.
    """
    started = int(time.time())
    run = Path(run_dir)
    plan_summary = json.loads((run / "plan-summary.json").read_text())
    bucket = plan_summary["bucket"]
    sweep = plan_summary["sweep"]
    objects = set(plan_summary.get("approved_objects", []))
    roots = listing_roots(plan_summary)

    client = client or s3_client()

    manifest_path = run / "manifest" / f"{bucket}.parquet"
    from botocore.exceptions import ClientError
    from disk_tree.object_version import version_sql

    with duckdb.connect() as con:
        con.execute("SET VARIABLE manifest = ?", [str(manifest_path)])
        source = "read_parquet(getvariable('manifest'))"
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
        manifest = {name: (size, mtime, token) for name, size, mtime, token in con.execute(
            f"SELECT name, size_bytes, mtime, {version_sql(cols)} FROM {source} ORDER BY name",
        ).fetchall()}
        preserved_path = manifest_path.with_name(f"{bucket}-preserved.parquet")
        if preserved_path.exists():
            con.execute("SET VARIABLE preserved = ?", [str(preserved_path)])
            review_omissions = dict(con.execute("SELECT name, reason FROM read_parquet(getvariable('preserved'))").fetchall())
        else:
            review_omissions = None
    unknown = sum(token in (None, "", "*") for _, _, token in manifest.values())
    if unknown:
        raise SweepError(f"manifest has {unknown} object{'s' if unknown != 1 else ''} without a usable reviewed ETag; take a new scan and re-stage")

    counters = {k: 0 for k in ("deleted_objects", "deleted_bytes", "skipped_gone",
                               "skipped_overwritten", "drift_new", "delete_failed")}
    bands: dict[str, dict] = {}
    preservation = dict.fromkeys(("new_since_review", "changed_since_review", "new_since_scan", "unclassified"), 0)

    def band_rec(b: str) -> dict:
        return bands.setdefault(b, {k: 0 for k in ("bytes", "objects", "gone", "overwritten", "drift_new")})

    seen: set[str] = set()
    failed_keys: set[str] = set()
    raced_keys: set[str] = set()
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
                band = band_for(key, sweep, objects, root)
                if key in manifest:
                    seen.add(key)
                    size, mtime, token = manifest[key]
                    live_mtime = int(obj["LastModified"].timestamp())
                    if obj["Size"] == size and abs(live_mtime - mtime) <= mtime_tol and obj.get("ETag") == token:
                        to_delete.append((key, size, band))
                        log_rows.append((key, size, mtime, "delete", _dir_of(key), band))
                    else:
                        counters["skipped_overwritten"] += 1
                        band_rec(band)["overwritten"] += 1
                        log_rows.append((key, size, mtime, "skipped_overwritten", _dir_of(key), band))
                elif eligible(key, sweep):  # prefixes only: an exact item's sibling is not drift
                    # live, under a swept prefix, but not in the reviewed manifest → new since scan
                    counters["drift_new"] += 1
                    band_rec(band)["drift_new"] += 1
                    reason = review_omissions.get(key, "new_since_scan") if review_omissions is not None else "unclassified"
                    if reason not in preservation:
                        raise SweepError(f"unknown preservation reason {reason!r}")
                    preservation[reason] += 1
                # else: outside the plan — expected, ignore
            token = resp.get("NextContinuationToken")
            if not resp.get("IsTruncated"):
                break

    for key, (size, mtime, _token) in manifest.items():
        if key not in seen:
            counters["skipped_gone"] += 1
            band = band_for(key, sweep, objects)
            band_rec(band)["gone"] += 1
            log_rows.append((key, size, mtime, "skipped_gone", _dir_of(key), band))

    def _apply(item: tuple[str, int, str]) -> tuple[tuple[str, int, str], str]:
        key = item[0]
        try:
            client.delete_object(Bucket=bucket, Key=key, IfMatch=manifest[key][2])
        except ClientError as e:
            code = e.response["Error"]["Code"]
            return item, "skipped_overwritten" if code in ("PreconditionFailed", "412") else "delete_failed"
        return item, "delete"

    if for_real and to_delete:
        with ThreadPoolExecutor(max_workers=delete_workers) as ex:
            for (key, size, band), decision in ex.map(_apply, to_delete):
                if decision == "delete_failed":
                    failed_keys.add(key)
                    counters["delete_failed"] += 1
                elif decision == "skipped_overwritten":
                    raced_keys.add(key)
                    counters["skipped_overwritten"] += 1
                    band_rec(band)["overwritten"] += 1
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
    _write_log(log_dir / "part-00000.parquet", sorted(
        (name, size, mtime, "delete_failed" if name in failed_keys else "skipped_overwritten" if name in raced_keys else decision, directory, band)
        for name, size, mtime, decision, directory, band in log_rows
    ))

    out_summary = {
        "plan_id": plan_summary.get("plan_id"),
        "name": plan_summary.get("name"),
        "bucket": bucket,
        "mode": "real" if for_real else "dry",
        "versioning_guard": False,
        **counters,
        "preserved_breakdown": preservation,
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
    roots = listing_roots(plan_summary)
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
    roots = listing_roots(plan_summary)
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
