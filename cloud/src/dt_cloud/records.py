"""Mine ``provenance.built_by`` from ``.artifact.json`` sidecars.

The listing says exactly which objects are record files
(:func:`dt_cloud.signals.record_file_paths`), so this is a bounded set of
targeted reads, not a bucket walk.

The sidecar shape is marin's on-disk artifact-record contract
(``marin.execution.artifact.ArtifactRecord``, written next to every executor
output). Only the one field this repo needs is read: ``provenance.built_by``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import posixpath
from concurrent.futures import ThreadPoolExecutor

import fsspec

from .identity import IdentityMap
from .signals import AttributionRow

logger = logging.getLogger(__name__)


def built_by_of_record(text: str) -> str | None:
    """The ``provenance.built_by`` of an artifact-record JSON body, if any.

    Pre-migration sidecars are frequently literal ``null`` (~95% of the fleet
    as of 2026-07); any non-dict body is simply "no signal", not a failure.
    """
    record = json.loads(text)
    if not isinstance(record, dict):
        return None
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return None
    built_by = provenance.get("built_by")
    return built_by if isinstance(built_by, str) and built_by else None


def mine_record_rows(
    record_paths: list[str],
    identities: IdentityMap,
    asof: dt.date,
    max_workers: int = 16,
) -> tuple[list[AttributionRow], list[str]]:
    """Attribution rows for every record file whose provenance names a user.

    Returns ``(rows, failed_paths)``. Per-file read/parse failures are
    collected rather than raised: a weekly batch over thousands of sidecars
    must survive the handful of corrupt or half-written ones that
    legitimately exist, and the failure list is part of the report output.
    """

    def one(path: str) -> AttributionRow | None:
        with fsspec.open(path, "r") as f:
            built_by = built_by_of_record(f.read())
        if built_by is None:
            return None
        user = identities.resolve(built_by)
        return AttributionRow(
            prefix=f"{posixpath.dirname(path)}/",
            user=user,
            source="artifact-record",
            evidence=f"built_by={built_by}",
            asof=asof,
        )

    rows: list[AttributionRow] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for path, future in [(p, pool.submit(one, p)) for p in record_paths]:
            try:
                row = future.result()
            except Exception:
                logger.warning("failed to read/parse record %s", path, exc_info=True)
                failed.append(path)
                continue
            if row is not None:
                rows.append(row)
    return rows, failed
