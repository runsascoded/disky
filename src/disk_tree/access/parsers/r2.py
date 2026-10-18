"""R2 read-recency parser (the collector's hour files → layer-2a leaf rows).

The R2 access collector (`infra/cf/r2-access`) writes, per source bucket and UTC hour, one JSON object. Two forms:

- the private aggregate, ``agg/<bucket>/<YYYY-MM-DD>/<HH>/<run>.json`` (``schema_version`` 3, or 4 since per-key
  rows are reads only), whose current key per hour the archive's ``state.json`` names (:func:`state_aggs`; a
  re-drained hour gets a new key and the old one is deleted after the state publish, so listing ``agg/`` can see two
  versions of an hour)::

    {"schema_version", "bucket", "hour" (ISO), "events", "own", "actions", "classes",
     "sampling": {"observed", "estimated", "max_sample_interval", "capture"},
     "rows": [[key, action, n, first_ts, last_ts], …]}

- the public per-hour file a deployment opts into (``PUBLIC_BUCKETS``), ``<prefix>/<bucket>/<YYYY-MM-DD>/<HH>.json``
  (``schema_version`` 1), the same rows less ``first_ts``::

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
Each (bucket, hour) must come in one version: the same file matched twice (an overlapping glob) collapses, and two
different versions of one hour refuse.

As-of cut: a row is kept only when every request it counts precedes ``as_of``, i.e. ``last_ts + 1 minute ≤
as_of``. That is exact for every row kept; the only loss is a row in the minute (or hour) straddling the cut whose
latest request is at or after it — its earlier requests drop with it.
"""

from __future__ import annotations

import os
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

#: The hour-file versions this parser reads: the public file (1) and the private aggregate (3, 4).
VERSIONS = (1, 3, 4)

#: Every version's fields as JSON-typed columns; a field a version lacks reads NULL.
_COLUMNS = (
    "{schema_version: 'INTEGER', bucket: 'VARCHAR', hour: 'VARCHAR', capture: 'VARCHAR', sampling: 'JSON', "
    "rows: 'JSON'}"
)


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _max_object_size(globs: list[str]) -> int:
    """`read_json`'s per-file buffer bound: the largest local input (≥ 1 MiB), else 1 GiB for a URL glob. DuckDB
    allocates buffers from the bound, not the file, so 1 GiB over a dozen small files ran a laptop out of memory
    (18 GB for ~30 MB of aggregates)."""
    from glob import glob

    if any('://' in g for g in globs):
        return 1 << 30
    sizes = [os.path.getsize(f) for g in globs for f in glob(g)]
    return max([1 << 20, *(size + 1024 for size in sizes)])


def hours(inputs: "str | list[str]", con: "duckdb.DuckDBPyConnection") -> "duckdb.DuckDBPyRelation":
    """One row per (bucket, hour) file: ``bucket, hour (TIMESTAMP, UTC), capture, rows``. Refuses an unknown
    ``schema_version`` and two different versions of one hour."""
    globs = [inputs] if isinstance(inputs, str) else inputs
    files = '[' + ', '.join(_sql_literal(g) for g in globs) + ']'
    rel = con.sql(f"""
        SELECT DISTINCT schema_version, bucket, (hour::TIMESTAMPTZ AT TIME ZONE 'UTC') AS hour,
            COALESCE(capture, sampling->>'capture') AS capture, rows
        FROM read_json({files}, format = 'unstructured', maximum_object_size = {_max_object_size(globs)}, columns = {_COLUMNS})
    """)
    versions = {r[0] for r in rel.project('schema_version').distinct().fetchall()}
    if versions - set(VERSIONS):
        raise ValueError(f"r2 hour files: unknown schema_version {sorted(versions - set(VERSIONS), key=str)}")
    counts = rel.aggregate('count(*), count(DISTINCT (bucket, hour))').fetchone()
    assert counts is not None  # an aggregate without GROUP BY is exactly one row
    n, distinct = counts
    if n != distinct:
        raise ValueError(f"r2 hour files: {n - distinct} hour(s) appear in two different versions")
    return rel.project('bucket, hour, capture, rows')


def parse(
    inputs: "str | list[str]",
    con: "duckdb.DuckDBPyConnection | None" = None,
    as_of: "str | datetime | None" = None,
) -> "duckdb.DuckDBPyRelation":
    """The hour files matching ``inputs`` (glob(s)), either form → leaf rows (``bucket, path, hour, op, n_ops,
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
            FROM (
                -- A row is an object (version 1) or a `[key, action, n, first_ts, last_ts]` array (3, 4).
                SELECT bucket, hour, capture, {{
                    key: CASE WHEN json_type(j) = 'ARRAY' THEN j->>0 ELSE j->>'key' END,
                    action: CASE WHEN json_type(j) = 'ARRAY' THEN j->>1 ELSE j->>'action' END,
                    n: (CASE WHEN json_type(j) = 'ARRAY' THEN j->>2 ELSE j->>'n' END)::BIGINT,
                    last_ts: (CASE WHEN json_type(j) = 'ARRAY' THEN j->>4 ELSE j->>'last_ts' END)::BIGINT,
                }} AS r
                FROM (SELECT bucket, hour, capture, UNNEST(rows::JSON[]) AS j FROM r2_hours)
            )
        ) {cut}
    """)


def state_aggs(state: dict, before: "datetime | None" = None) -> list[str]:
    """The current aggregate key of every hour the archive's ``state.json`` names (``hours[*].agg`` within
    retention, ``frozen`` past it), across its buckets, sorted; with ``before``, only hours starting before it. The
    state is the only source: a listing of ``agg/`` also holds superseded versions until the collector deletes
    them. Refuses a state version it doesn't know and version-1 per-day aggregates (``legacy_days``, a different
    shape)."""
    if state.get('schema_version') != 2:
        raise ValueError(f"r2 state: unknown schema_version {state.get('schema_version')!r}")
    out = []
    for bucket, b in sorted(state['buckets'].items()):
        if b.get('legacy_days'):
            raise ValueError(f"r2 state: {bucket} has version-1 per-day aggregates (legacy_days), which this parser doesn't read")
        named = {**b.get('frozen', {}), **{h: e['agg'] for h, e in b['hours'].items()}}
        for hour, key in sorted(named.items()):
            if before is None or _hour_start(hour) < before:
                out.append(key)
    return out


def _hour_start(hour: str) -> "datetime":
    """A state hour key (``YYYY-MM-DDTHH``, UTC) → its start."""
    from datetime import datetime, timezone

    return datetime.strptime(hour, '%Y-%m-%dT%H').replace(tzinfo=timezone.utc)
