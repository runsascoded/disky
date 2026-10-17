"""R2 read-recency parser (the collector's public per-day files → canonical).

The R2 access collector (`infra/cf/r2-access`) publishes, per source bucket
and UTC day, ``<prefix>/<bucket>/<day>.jsonl``: one JSON line per provider
event, ``{id, bucket, ts, action, key, status}`` (``ts`` epoch ms, ``key``
null for bucket-level actions). The files carry no requester identity: IPs,
user agents and actors never leave the collector's private archive, so
``requester`` / ``user_agent`` are NULL here, as are the byte columns (R2's
``response.bytes`` is not transferred bytes: a HEAD reports the object size).

A key-less action (LIST, HeadBucket) lands on the bucket root (``path ''``),
as GCS's ``cs_object``-less LISTs do. Whole records are deduplicated (each
carries its provider event id, and the collector refuses an id whose fields
change), so overlapping copies of a day's file never double-count.
"""

from __future__ import annotations

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


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def parse(input_glob: str, store: str = 'r2', con: "duckdb.DuckDBPyConnection | None" = None) -> "duckdb.DuckDBPyRelation":
    """Parse the collector's public ``.jsonl`` files matching ``input_glob`` → canonical relation."""
    import duckdb as _duckdb
    if con is None:
        con = _duckdb.connect()
    cases = ' '.join(f"WHEN {_sql_literal(a)} THEN {_sql_literal(op)}" for a, op in OPS.items())
    return con.sql(f"""
        SELECT DISTINCT
            make_timestamptz(ts * 1000) AS ts,
            {_sql_literal(store)}::VARCHAR AS store,
            bucket::VARCHAR AS bucket,
            COALESCE(key, '')::VARCHAR AS path,
            (CASE action {cases} ELSE 'OTHER' END)::VARCHAR AS op,
            action::VARCHAR AS op_raw,
            status::INTEGER AS status,
            NULL::BIGINT AS bytes_out,
            NULL::BIGINT AS bytes_in,
            NULL::VARCHAR AS requester,
            NULL::VARCHAR AS user_agent,
            id::VARCHAR AS request_id
        FROM read_json(
            {_sql_literal(input_glob)},
            format = 'newline_delimited',
            columns = {{id: 'VARCHAR', bucket: 'VARCHAR', ts: 'BIGINT', action: 'VARCHAR', key: 'VARCHAR', status: 'INTEGER'}}
        )
    """)
