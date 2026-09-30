"""Bucket lifecycle rules as a tracked file (`dt-cloud lifecycle pull|diff|push`).

The bucket's lifecycle configuration is the one piece of storage state that
decides what gets deleted without anyone running a job — Marin's `tmp/ttl=<N>d/`
TTLs on every bucket it uses, CoreWeave's abort-incomplete-MPU rule and (since
the 2026-09-16 quota incident) its bucket-wide noncurrent-version GC. Nobody
should hand-edit it in place, and its history should be reviewable, so:

- `pull` snapshots the live rules (normalized, stably sorted) to a JSON file —
  the tracked copy in the repo is the intended state, and the scan job writes
  one per snapshot so every scan carries the rules that were in force;
- `diff` shows file vs live (added / removed / changed rules);
- `push` applies the file read-modify-write style: the whole intended set is
  sent and then read back and compared — a mismatch raises rather than
  leaving a half-applied config.

Two clouds, one file shape per bucket. **S3 / CAIOS** rules carry an `ID`
(`{"ID", "Filter", "Status", "Expiration", …}`), so `diff` can say *changed*.
**GCS** rules are anonymous (`{"action": {"type"}, "condition": {…}}`), so a
rule's identity is its content: `diff_gcs` reports added / removed only, and
the site synthesizes a display name from the content. A `gs://` bucket URI
picks the GCS backend (`*_any`); a bare name is S3 (the CoreWeave deployment's
default). A deployment with several buckets snapshots a map `{bucket: rules}`
(`pull_many` + `dump_map`, specs/cw-multi-bucket.md §2) — the site reads this
and the bare list alike, normalizing each bucket's rules through the store's
adapter (`site/src/lifecycle.ts`).

`gc_rule` builds the S3 bucket-wide GC rule (`NoncurrentVersionExpiration` +
`ExpiredObjectDeleteMarker`): a no-op for null-version objects while versioning
is Suspended, it cleans stragglers and zero-byte delete markers, and it is the
safety net that was missing when versioning was Enabled without one.
"""
from __future__ import annotations

import json
import sys
from functools import partial
from typing import TYPE_CHECKING, Any

err = partial(print, file=sys.stderr)

if TYPE_CHECKING:
    from google.cloud.storage import Client as GcsClient
    from mypy_boto3_s3 import S3Client

GC_RULE_ID = "cw-noncurrent-gc"


# --- S3 / CAIOS (rules keyed by ID) ------------------------------------------

def normalize(rules: list[dict]) -> list[dict]:
    """Rules sorted by ID, so file and live compare and diff stably."""
    return sorted((json.loads(json.dumps(r, default=str)) for r in rules), key=lambda r: r["ID"])


def gc_rule(days: int = 1, prefix: str = "") -> dict:
    """Bucket-wide (or `prefix`-scoped) noncurrent-version GC after `days`
    (S3's minimum is 1; no sub-day granularity) + expired-delete-marker cleanup."""
    return {
        "ID": GC_RULE_ID,
        "Filter": {"Prefix": prefix},
        "Status": "Enabled",
        "NoncurrentVersionExpiration": {"NoncurrentDays": days},
        "Expiration": {"ExpiredObjectDeleteMarker": True},
    }


def pull(client: "S3Client", bucket: str) -> list[dict]:
    """The live rules, normalized. A bucket with no configuration → `[]`."""
    try:
        return normalize(client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"])
    except client.exceptions.ClientError as e:  # no config at all is a legitimate state
        if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
            return []
        raise


def diff(intended: list[dict], live: list[dict]) -> dict[str, list]:
    """`{"added": [ids only in intended], "removed": [ids only in live],
    "changed": [ids in both whose bodies differ]}` — all sorted."""
    a = {r["ID"]: r for r in normalize(intended)}
    b = {r["ID"]: r for r in normalize(live)}
    return {
        "added": sorted(a.keys() - b.keys()),
        "removed": sorted(b.keys() - a.keys()),
        "changed": sorted(k for k in a.keys() & b.keys() if a[k] != b[k]),
    }


class LifecycleRaced(RuntimeError):
    """The live configuration changed between reading it and writing ours."""


def push(client: "S3Client", bucket: str, intended: list[dict], *, base: list[dict] | None = None) -> list[dict]:
    """Replace the bucket's configuration with `intended` and verify the
    read-back equals it (normalized). Returns the live rules after the PUT.

    S3 has no conditional PUT for bucket configuration (no ETag/If-Match), so
    `base` is the poor man's compare-and-swap: the live rules the caller
    diffed against. They are re-read immediately before the PUT and, if they
    moved, nothing is written (`LifecycleRaced`) — a millisecond window instead
    of the seconds a human spends reviewing a diff."""
    want = normalize(intended)
    if base is not None:
        live = pull(client, bucket)
        if live != normalize(base):
            raise LifecycleRaced(f"{bucket}: live rules changed since they were read: {diff(live, base)}")
    client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": want})
    got = pull(client, bucket)
    if got != want:
        raise RuntimeError(
            f"lifecycle push did not round-trip on {bucket}: live differs from intended: {diff(want, got)}"
        )
    return got


# --- GCS (anonymous rules; identity = content) ----------------------------------

def gcs_key(rule: dict) -> str:
    """A GCS rule's identity: its canonical JSON (sorted keys, no whitespace)."""
    return json.dumps(rule, sort_keys=True, separators=(",", ":"), default=str)


def normalize_gcs(rules: list[Any]) -> list[dict]:
    """Plain dicts (the client yields dict subclasses), sorted by content."""
    return sorted((json.loads(json.dumps(r, default=str)) for r in rules), key=gcs_key)


def pull_gcs(client: "GcsClient", bucket: str) -> list[dict]:
    """The live rules of a GCS bucket, normalized (`[]` when it has none).
    Needs `storage.buckets.get` (the job SA's `legacyBucketReader` has it)."""
    b = client.bucket(bucket)
    b.reload()
    return normalize_gcs(list(b.lifecycle_rules))


def diff_gcs(intended: list[dict], live: list[dict]) -> dict[str, list]:
    """`{"added": [keys only in intended], "removed": [keys only in live],
    "changed": []}` — a GCS rule has no ID, so an edit is a removal plus an
    addition; the shape matches `diff` so callers treat both alike."""
    a = {gcs_key(r) for r in normalize_gcs(intended)}
    b = {gcs_key(r) for r in normalize_gcs(live)}
    return {"added": sorted(a - b), "removed": sorted(b - a), "changed": []}


def push_gcs(client: "GcsClient", bucket: str, intended: list[dict], *, base: list[dict] | None = None) -> list[dict]:
    """Replace a GCS bucket's lifecycle rules with `intended` (a PATCH of the
    whole `lifecycle` field) and verify the read-back. Same `base` guard as
    `push`: the live rules are re-read right before the write and, if they
    moved since the caller's diff, nothing is written."""
    want = normalize_gcs(intended)
    b = client.bucket(bucket)
    if base is not None:
        live = pull_gcs(client, bucket)
        if live != normalize_gcs(base):
            raise LifecycleRaced(f"{bucket}: live rules changed since they were read: {diff_gcs(live, base)}")
    b.reload()
    b.lifecycle_rules = want
    b.patch()
    got = pull_gcs(client, bucket)
    if got != want:
        raise RuntimeError(
            f"lifecycle push did not round-trip on {bucket}: live differs from intended: {diff_gcs(want, got)}"
        )
    return got


# --- either cloud, by the bucket URI's scheme -------------------------------------

GCS_SCHEME = "gs://"


def is_gcs(bucket: str) -> bool:
    return bucket.startswith(GCS_SCHEME)


def bucket_name(bucket: str) -> str:
    """`gs://name` → `name`; a bare name unchanged."""
    return bucket[len(GCS_SCHEME):] if is_gcs(bucket) else bucket


def normalize_any(bucket: str, rules: list[dict]) -> list[dict]:
    return normalize_gcs(rules) if is_gcs(bucket) else normalize(rules)


def diff_any(bucket: str, intended: list[dict], live: list[dict]) -> dict[str, list]:
    return diff_gcs(intended, live) if is_gcs(bucket) else diff(intended, live)


def pull_any(bucket: str, *, s3: "S3Client | None" = None, gcs: "GcsClient | None" = None) -> list[dict]:
    """`pull` or `pull_gcs` by the bucket's scheme; the matching client must be given."""
    if is_gcs(bucket):
        if gcs is None:
            raise ValueError(f"{bucket}: a GCS client is required")
        return pull_gcs(gcs, bucket_name(bucket))
    if s3 is None:
        raise ValueError(f"{bucket}: an S3 client is required")
    return pull(s3, bucket)


def push_any(
    bucket: str,
    intended: list[dict],
    *,
    base: list[dict] | None = None,
    s3: "S3Client | None" = None,
    gcs: "GcsClient | None" = None,
) -> list[dict]:
    """`push` or `push_gcs` by the bucket's scheme; the matching client must be given."""
    if is_gcs(bucket):
        if gcs is None:
            raise ValueError(f"{bucket}: a GCS client is required")
        return push_gcs(gcs, bucket_name(bucket), intended, base=base)
    if s3 is None:
        raise ValueError(f"{bucket}: an S3 client is required")
    return push(s3, bucket, intended, base=base)


def pull_many(
    buckets: list[str],
    *,
    s3: "S3Client | None" = None,
    gcs: "GcsClient | None" = None,
    keep_going: bool = False,
) -> dict[str, list[dict]]:
    """`{bucket as given: rules}` for several buckets, in the given order — the
    input to `dump_map` (which knows each bucket's cloud from its scheme).
    With ``keep_going``, a bucket whose pull raises (typically a 403: the
    caller's principal lacks `storage.buckets.get` there) is reported on
    stderr and left out, so the fleet snapshot still covers the readable
    buckets; the error propagates only when none could be read."""
    out: dict[str, list[dict]] = {}
    failed: list[tuple[str, Exception]] = []
    for b in buckets:
        try:
            out[b] = pull_any(b, s3=s3, gcs=gcs)
        except Exception as e:
            if not keep_going:
                raise
            failed.append((b, e))
            err(f"lifecycle: {b}: {type(e).__name__}: {e} — skipped")
    if failed and not out:
        raise failed[0][1]
    return out


def load(path: str) -> list[dict]:
    """A tracked file: a rule list (either cloud's shape), as written by `dump`."""
    with open(path) as f:
        rules = json.load(f)
    if not isinstance(rules, list):
        raise ValueError(f"{path}: expected a JSON list of rules (a per-bucket map is a snapshot, not a tracked file)")
    return rules


def dump(rules: list[dict], bucket: str = "") -> str:
    """JSON text for a tracked file: a rule list, normalized for `bucket`'s cloud
    (a bare name / empty = S3)."""
    return json.dumps(normalize_any(bucket, rules), indent=2) + "\n"


def dump_map(by_bucket: dict[str, list[dict]]) -> str:
    """The multi-bucket snapshot the scan job writes (specs/cw-multi-bucket.md
    §2): `{<bucket>: rules}` in the given (deployment) order — the primary
    first — each bucket's rules normalized for its cloud (a `gs://` key) and
    keyed by the bare name, which is how the site's `Store.buckets` name them."""
    return json.dumps({bucket_name(b): normalize_any(b, r) for b, r in by_bucket.items()}, indent=2) + "\n"
