"""R2 read-recency parser (the collector's public per-hour files → layer-2a leaf rows).

The R2 access collector (`infra/cf/r2-access`) publishes, per source bucket and UTC hour,
``<prefix>/<bucket>/<YYYY-MM-DD>/<HH>.json``: one JSON object

::

    {"schema_version": 1, "bucket", "hour" (ISO), "capture" (unsampled|sampled|inconsistent),
     "observed", "estimated", "max_sample_interval", "excluded",
     "rows": [{"key", "action", "n", "last_ts"}, …]}

``rows`` are per (object key, action) counts with the minute of the latest request (``last_ts``, epoch ms
truncated to the minute): object reads only (GET, HEAD) since the collector's aggregate ``schema_version`` 4;
earlier files also carry writes, listings and key-less bucket-level actions (``key`` null). The files carry no requester identity and no
per-request events, so they can't become canonical rows (one per request) at volume; :func:`parse` emits
:func:`~disk_tree.access.aggregate.aggregate_access_leaf`'s pre-aggregated leaf rows instead: ``n_requesters`` and
the byte columns NULL (R2's ``response.bytes`` is not transferred bytes: a HEAD reports the object size), and
``sampled`` true when the hour's capture was ``sampled`` or ``inconsistent`` (its counts undercount requests).

A key-less action (LIST, HeadBucket) lands on the bucket root (``path ''``), as GCS's ``cs_object``-less LISTs do.
A re-drained hour replaces its file, so each (bucket, hour) has one version; the same file matched twice (an
overlapping glob) collapses, and two different versions of one hour refuse.

As-of cut: a row is kept only when every request it counts precedes ``as_of``, i.e. ``last_ts + 1 minute ≤
as_of``. That is exact for every row kept; the only loss is a row in the minute (or hour) straddling the cut whose
latest request is at or after it — its earlier requests drop with it. Scan instants are UTC midnights, so in
practice nothing straddles.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


#: R2 action → the canonical op (`disk_tree.access.parsers.gcs`'s vocabulary).
OPS: dict[str, str] = {
    'GetObject': 'GET',
    'HeadObject': 'HEAD', 'HeadBucket': 'HEAD',
    'ListObjects': 'LIST', 'ListObjectsV2': 'LIST', 'ListMultipartUploads': 'LIST', 'ListParts': 'LIST',
    'PutObject': 'PUT', 'CopyObject': 'PUT', 'CreateMultipartUpload': 'PUT', 'UploadPart': 'PUT',
    'UploadPartCopy': 'PUT', 'CompleteMultipartUpload': 'PUT',
    'DeleteObject': 'DELETE', 'DeleteObjects': 'DELETE',
}

#: Captures whose counts may undercount requests (provider ingestion sampling, or a count/drain mismatch).
SAMPLED = ('sampled', 'inconsistent')

_COLUMNS = (
    "{schema_version: 'INTEGER', bucket: 'VARCHAR', hour: 'VARCHAR', capture: 'VARCHAR', "
    "rows: 'STRUCT(key VARCHAR, action VARCHAR, n BIGINT, last_ts BIGINT)[]'}"
)


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def hours(inputs: "str | list[str]", con: "duckdb.DuckDBPyConnection") -> "duckdb.DuckDBPyRelation":
    """One row per (bucket, hour) file: ``bucket, hour (TIMESTAMP, UTC), capture, rows``. Refuses an unknown
    ``schema_version`` and two different versions of one hour."""
    globs = [inputs] if isinstance(inputs, str) else inputs
    files = '[' + ', '.join(_sql_literal(g) for g in globs) + ']'
    rel = con.sql(f"""
        SELECT DISTINCT schema_version, bucket, (hour::TIMESTAMPTZ AT TIME ZONE 'UTC') AS hour, capture, rows
        FROM read_json({files}, format = 'unstructured', maximum_object_size = 1073741824, columns = {_COLUMNS})
    """)
    versions = {r[0] for r in rel.project('schema_version').distinct().fetchall()}
    if versions - {1}:
        raise ValueError(f"r2 hour files: unknown schema_version {sorted(versions - {1}, key=str)}")
    n, distinct = rel.aggregate('count(*), count(DISTINCT (bucket, hour))').fetchone()
    if n != distinct:
        raise ValueError(f"r2 hour files: {n - distinct} hour(s) appear in two different versions")
    return rel.project('bucket, hour, capture, rows')


def parse(
    inputs: "str | list[str]",
    con: "duckdb.DuckDBPyConnection | None" = None,
    as_of: "str | datetime | None" = None,
) -> "duckdb.DuckDBPyRelation":
    """The public hour files matching ``inputs`` (glob(s)) → leaf rows (``bucket, path, hour, op, n_ops,
    bytes_out, bytes_in, n_requesters, last_ts, sampled``), cut at ``as_of`` (module docstring)."""
    import duckdb as _duckdb

    from ..aggregate import as_of_literal
    if con is None:
        con = _duckdb.connect()
    con.register('r2_hours', hours(inputs, con))
    cases = ' '.join(f"WHEN {_sql_literal(a)} THEN {_sql_literal(op)}" for a, op in OPS.items())
    sampled = ', '.join(_sql_literal(c) for c in SAMPLED)
    cut = f"WHERE last_ts + INTERVAL 1 MINUTE <= {as_of_literal(as_of)}" if as_of is not None else ''
    return con.sql(f"""
        SELECT * FROM (
            SELECT
                bucket::VARCHAR AS bucket,
                COALESCE(r.key, '')::VARCHAR AS path,
                hour::TIMESTAMP AS hour,
                (CASE r.action {cases} ELSE 'OTHER' END)::VARCHAR AS op,
                r.n::BIGINT AS n_ops,
                NULL::BIGINT AS bytes_out,
                NULL::BIGINT AS bytes_in,
                NULL::BIGINT AS n_requesters,
                make_timestamptz(r.last_ts * 1000) AS last_ts,
                capture IN ({sampled}) AS sampled
            FROM (SELECT bucket, hour, capture, UNNEST(rows) AS r FROM r2_hours)
        ) {cut}
    """)
