"""Listing-source normalization: scan_gcs parquets and SII inventory reports.

:func:`prepare_listing` returns a parenthesized SQL fragment exposing the
canonical listing columns (``bucket, name, size_bytes, created,
storage_class_id``) over one or more parquet globs, usable anywhere the
pipeline previously interpolated ``read_parquet('<glob>')``. Sources may mix
schemas:

- marin ``scan_gcs`` listings pass through unchanged;
- GCS Storage Insights inventory reports are renamed/cast (``size`` →
  ``size_bytes``, ``timeCreated`` → ``created``, ``storageClass`` string →
  class id) with soft-deleted rows (``timeDeleted``) dropped.

When several sources cover the same bucket, the earliest listed source wins —
so ``-l <fresh SII glob> -l <older full scan>`` reads fresh buckets from the
former and only the missing buckets (e.g. ``marin-us-central2``, where SII is
unavailable) from the latter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

SII_CLASS_IDS = {"STANDARD": 1, "NEARLINE": 2, "COLDLINE": 3, "ARCHIVE": 4}


def _normalized_select(con: "duckdb.DuckDBPyConnection", glob: str) -> str:
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{glob}') LIMIT 0").fetchall()}
    if "size_bytes" in cols:
        created = "created" if "created" in cols else "NULL::TIMESTAMPTZ AS created"
        cls = "storage_class_id" if "storage_class_id" in cols else "0 AS storage_class_id"
        return f"SELECT bucket, name, size_bytes, {created}, {cls} FROM read_parquet('{glob}')"
    if {"size", "timeCreated", "storageClass"} <= cols:
        case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in SII_CLASS_IDS.items())
        drop = " WHERE timeDeleted IS NULL" if "timeDeleted" in cols else ""
        return (
            "SELECT bucket, name, size::BIGINT AS size_bytes, timeCreated AS created,"
            f" CASE storageClass {case} ELSE 0 END AS storage_class_id"
            f" FROM read_parquet('{glob}'){drop}"
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
