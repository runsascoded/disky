"""Listing-source normalization: raw object-listing parquet + SII / S3-Inventory reports.

:func:`prepare_listing` returns a parenthesized SQL fragment exposing the
canonical layer-1 columns (``bucket, name, size_bytes, created,
storage_class_id, updated``) over one or more parquet globs, usable anywhere
the pipeline previously interpolated ``read_parquet('<glob>')``. ``updated``
is the platform's second stamp where it has one (GCS ``updated`` beside
``timeCreated``; S3/R2 have only ``LastModified``, which lands in ``created``)
and NULL otherwise — the path store keeps both verbatim (spec
``path-store.md`` §1.1). Sources may mix schemas:

- raw object-listing parquet (e.g. `bucket_list.py` output, or marin's
  ``scan_gcs`` listings) passes through unchanged;
- GCS Storage Insights inventory reports are renamed/cast (``size`` →
  ``size_bytes``, ``timeCreated`` → ``created``, ``storageClass`` string →
  class id) with soft-deleted rows (``timeDeleted``) dropped.

When several sources cover the same bucket, the earliest listed source wins —
so ``-l <fresh SII glob> -l <older full scan>`` reads fresh buckets from the
former and only the missing buckets (e.g. ``marin-us-central2``, where SII is
unavailable) from the latter.

Ported from `oa/marin-gcs-usage/src/gcs_usage/listing.py` (2026-08-04).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

SII_CLASS_IDS = {"STANDARD": 1, "NEARLINE": 2, "COLDLINE": 3, "ARCHIVE": 4}

# S3 storage classes → same 4-bucket "hot/warm/cool/cold" tiering as GCS SII.
# STANDARD-family and IntelligentTiering top tier → 1 (hot).
# STANDARD_IA / ONEZONE_IA / IntelligentTiering middle tiers → 2 (warm).
# GLACIER_IR / GLACIER_INSTANT_RETRIEVAL / IntelligentTiering archive tier → 3 (cool).
# GLACIER / DEEP_ARCHIVE → 4 (cold).
S3_CLASS_IDS = {
    "STANDARD": 1,
    "REDUCED_REDUNDANCY": 1,
    "INTELLIGENT_TIERING": 1,
    "OUTPOSTS": 1,
    "STANDARD_IA": 2,
    "ONEZONE_IA": 2,
    "GLACIER_IR": 3,
    "GLACIER_INSTANT_RETRIEVAL": 3,
    "GLACIER": 4,
    "DEEP_ARCHIVE": 4,
}


def _normalized_select(con: "duckdb.DuckDBPyConnection", glob: str) -> str:
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{glob}') LIMIT 0").fetchall()}
    updated = "updated" if "updated" in cols else "NULL::TIMESTAMPTZ AS updated"
    if "size_bytes" in cols:
        created = "created" if "created" in cols else "NULL::TIMESTAMPTZ AS created"
        cls = "storage_class_id" if "storage_class_id" in cols else "0 AS storage_class_id"
        return f"SELECT bucket, name, size_bytes, {created}, {cls}, {updated} FROM read_parquet('{glob}')"
    if {"size", "timeCreated", "storageClass"} <= cols:
        case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in SII_CLASS_IDS.items())
        drop = " WHERE timeDeleted IS NULL" if "timeDeleted" in cols else ""
        return (
            "SELECT bucket, name, size::BIGINT AS size_bytes, timeCreated AS created,"
            f" CASE storageClass {case} ELSE 0 END AS storage_class_id, {updated}"
            f" FROM read_parquet('{glob}'){drop}"
        )
    if {"Bucket", "Key", "Size"} <= cols:
        # S3 Inventory report (parquet). Column availability depends on the inventory
        # config; only Bucket/Key/Size are guaranteed. Filter delete markers and
        # non-latest versions if those columns are present.
        last_mod = "LastModifiedDate AS created" if "LastModifiedDate" in cols else "NULL::TIMESTAMPTZ AS created"
        if "StorageClass" in cols:
            case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in S3_CLASS_IDS.items())
            cls = f"CASE StorageClass {case} ELSE 0 END AS storage_class_id"
        else:
            cls = "0 AS storage_class_id"
        filters = []
        if "IsDeleteMarker" in cols:
            filters.append("(IsDeleteMarker IS NULL OR IsDeleteMarker = FALSE)")
        if "IsLatest" in cols:
            filters.append("(IsLatest IS NULL OR IsLatest = TRUE)")
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        return (
            f"SELECT Bucket AS bucket, Key AS name, Size::BIGINT AS size_bytes, {last_mod}, {cls}, NULL::TIMESTAMPTZ AS updated"
            f" FROM read_parquet('{glob}'){where}"
        )
    raise ValueError(f"unrecognized listing schema for {glob}: {sorted(cols)}")


def prepare_listing(con: "duckdb.DuckDBPyConnection", listings: tuple[str, ...]) -> str:
    """SQL fragment over ``listings`` globs; earlier sources win per bucket."""
    if not listings:
        raise ValueError("at least one listing glob is required")
    selects = [_normalized_select(con, g) for g in listings]
    if len(selects) == 1:
        return f"({selects[0]})"
    ctes = ", ".join(f"__l{i} AS ({s})" for i, s in enumerate(selects))
    parts = ["SELECT * FROM __l0"]
    for i in range(1, len(selects)):
        prior = " UNION ".join(f"SELECT DISTINCT bucket FROM __l{j}" for j in range(i))
        parts.append(f"SELECT * FROM __l{i} WHERE bucket NOT IN ({prior})")
    return f"(WITH {ctes} {' UNION ALL '.join(parts)})"
