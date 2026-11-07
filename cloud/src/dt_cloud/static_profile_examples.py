"""Example deployment profiles for the static name index (`static_profile.Profile`), selectable with
`STATIC_NAMES_PROFILE=<name>`: Open Athena's two deployments, as worked examples. Illustrative, not live: they carry no
generation (`gen`), which moves with every compaction. A deployment's live profile is a JSON file on its own branch
(`job/static-names.json`, `STATIC_NAMES_PROFILE=<path>`; `dt-cloud static-names profile` prints one to start from), so a
compaction is a branch commit, never a `cloud` one. Any field can be overridden by its env var (`static_profile.ENV`); the
R2 endpoint (it names the Cloudflare account) is always env (`R2_ENDPOINT`).
"""
from __future__ import annotations

from .static_profile import Profile

PROJECT = "oa-internal-450019"
#: No image is pinned here: the caller passes the job image it runs (`STATIC_NAMES_IMAGE`, e.g. the scan job's own `$JOB_IMAGE`)
#: — a digest pinned in code goes stale as the code moves on (2026-10-10: a pinned pre-`static_append` image failed every task).

#: GCS buckets: scans listed per scan id (`listing/<id>/`, before generations a single `path-index.parquet`); each run gets
#: the heavy-term drilldown.
GCS = Profile(
    name="gcs",
    layouts=("listing/{id}/path-index.parquet", "listing/{id}/index/{gen}/path-index.parquet"),
    bucket="oa-gcs-usage-dvx", scratch="oa-gcs-usage-scratch",
    r2_bucket="oa-gcs-usage-index", r2_secrets={"key_id": "gcs-static-index-r2-key-id", "secret": "gcs-static-index-r2-secret"},
    project=PROJECT, region="us-east1", sa=f"gcs-usage-job@{PROJECT}.iam.gserviceaccount.com", drill=True, anchors=True,
    # New generations are built with the rule; one built before it keeps the full index for its runs, as its
    # `scans.json` records no `hex_runs`.
    hex_runs="16,8",
    # Carries stop below level 5: a compaction is due after ~2^5 = 32 scans (~32 days of daily scans).
    compact_level=5,
)

#: CoreWeave S3 buckets: scans every 6 h (`cw-l2/<id>/index/<gen>/`), the same data and scratch buckets as gcs, its own
#: R2 bucket; every stage, the R2 copy included, runs as cw's own job account (objectAdmin on both buckets, its R2 key); each
#: run gets the heavy-term drilldown (its base has one).
CW = Profile(
    name="cw",
    layouts=("cw-l2/{id}/index/{gen}/path-index.parquet",),
    bucket="oa-gcs-usage-dvx", scratch="oa-gcs-usage-scratch",
    r2_bucket="oa-cw-s3-usage-index",
    r2_secrets={"endpoint": "cw-s3-r2-endpoint", "key_id": "cw-s3-r2-access-key-id", "secret": "cw-s3-r2-secret-access-key"},
    project=PROJECT, region="us-east1", sa=f"cw-s3-job@{PROJECT}.iam.gserviceaccount.com",
    r2_sa=f"cw-s3-job@{PROJECT}.iam.gserviceaccount.com", hex_runs="16,8", drill=True,
    # Carries stop below level 5: a compaction is due after ~2^5 = 32 scans (~8 days of 6-hourly scans).
    compact_level=5,
)

EXAMPLES = {p.name: p for p in (GCS, CW)}
