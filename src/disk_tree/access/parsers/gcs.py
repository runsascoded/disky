"""GCS usage-log parser (layer-1a raw → canonical).

Google delivers hourly CSVs to a bucket you designate. Filename convention:
``<prefix>_usage_YYYY_MM_DD_HH_MM_SS_<id>_v0`` (usage) or ``_storage_`` (daily
byte-hours). This module handles the former; the latter is a bonus stats
table (bucket-level, no path attribution) parsed by
:func:`parse_storage_daily`.

Header row is present. Fields we care about (per
`Google's docs
<https://cloud.google.com/storage/docs/access-logs#format>`_):

- ``time_micros`` — request time (usec since epoch)
- ``c_ip`` — client IP (best requester ID we get without extra work)
- ``cs_method`` — HTTP method (GET/PUT/DELETE/HEAD)
- ``cs_uri`` — request URI (includes query string)
- ``sc_status`` — response status
- ``cs_bytes`` / ``sc_bytes`` — request/response body sizes
- ``cs_user_agent``
- ``cs_operation`` — Google's own verb (``GET_Object``, ``LIST_Bucket``, …)
- ``cs_bucket`` / ``cs_object`` — bucket + key targeted
- ``s_request_id`` — Google's request id (used for dedupe: Google documents
  rare duplicate log lines; deduping on this field is idempotent).

The parser returns a DuckDB relation exposing the canonical access columns
(`ts, store, bucket, path, op, op_raw, status, bytes_out, bytes_in,
requester, user_agent, request_id`). Callers can materialize
to parquet with ``rel.write_parquet(path)`` or feed it directly to
:mod:`disk_tree.access.aggregate`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


#: View `parse` leaves on the connection, holding the CSV rows *before* dedupe.
#: Kept addressable so callers can reconcile rows-in against rows-out — see
#: :func:`dropped_fraction`.
RAW_VIEW = '_gcs_usage_raw'

#: Google documents duplicate log lines as rare. Losing more than this fraction
#: of input rows to the dedupe means the key is wrong, not that the day was
#: unusual: a batched write logs one line per object under a single request id,
#: and keying on the id alone silently discarded all but one of them (measured
#: at 41% of a single log-hour before the key gained `path`/`op_raw`).
DEDUPE_WARN_FRACTION = 0.01


def dropped_fraction(con, n_out: int) -> tuple[int, float]:
    """``(rows_in, dropped_fraction)`` for the parse still open on ``con``.

    The invariant this exists to make visible: 1a should be *marginally* smaller
    than its source CSVs, never substantially. Costs one extra scan of the
    already-cached input, and is the check whose absence let a 41%-loss dedupe
    bug ship. Call it right after materializing :func:`parse`'s relation.
    """
    n_in = con.execute(f'SELECT COUNT(*) FROM {RAW_VIEW}').fetchone()[0]
    return n_in, (0.0 if not n_in else (n_in - n_out) / n_in)


def parse(input_glob: str, store: str = 'gcs', con: "duckdb.DuckDBPyConnection | None" = None) -> "duckdb.DuckDBPyRelation":
    """Parse GCS usage-log CSVs matching ``input_glob`` → canonical relation.

    Collapses duplicate log lines (Google documents these as rare but real).
    The output relation is lazy — no I/O until you materialize or aggregate.
    """
    import duckdb as _duckdb
    if con is None:
        con = _duckdb.connect()

    # `read_csv` with a header-inferring load + explicit type hints so we can
    # ingest arbitrary GCS bucket paths (gs://…), local globs, or fsspec URLs.
    # `union_by_name=true` handles the (unlikely but possible) case where
    # Google's field set drifts across days.
    con.execute(f"""
        CREATE OR REPLACE VIEW {RAW_VIEW} AS
        SELECT *
        FROM read_csv(
            {_sql_literal(input_glob)},
            header=true,
            filename=true,
            union_by_name=true
        )
    """)

    # Canonical projection + normalization. Path derived from cs_object
    # (already the object key); bucket from cs_bucket. `cs_operation` beats
    # `cs_method` for the normalized op (`_normalize_op_sql`).
    #
    # Dedupe on the WHOLE canonical record, not on s_request_id. Google's
    # wording is "Occasionally, a single *record* may appear twice... you can
    # use the s_request_id field to *detect* duplicates" — detect, i.e. narrow
    # the candidates, not key on. The docs never call s_request_id unique, and
    # it is not: one id can cover many distinct objects. In a sampled file the
    # largest such group is 9 records, all zero-byte PUT_Object, user-agent
    # "GCS Lifecycle Management" — i.e. an internal lifecycle *batch operation*
    # (storage-class transitions surface as PUTs), not an HTTP request, logged
    # under one id with one microsecond timestamp across scattered sibling
    # prefixes. Keying on the id discarded all but one per group: measured at
    # 2,408 lost PUTs over 7h on marin-us-east1, the fleet's *smallest* bucket,
    # understating its PUTs by 53%. (Those PUTs are largely lifecycle churn,
    # not application writes, so recovering them matters for correctness, not
    # as a write-recency signal — treat with care.)
    #
    # Measured structure: within a multi-record id, cs_object is the ONLY field
    # that varies (90/90 groups sampled) — time_micros, c_ip, cs_method,
    # cs_operation, sc_status, cs_user_agent constant; cs_uri / cs_bytes /
    # sc_bytes / time_taken_micros all NULL. So the records are distinguishable
    # purely by object name, and two actions on the *same* object under one id
    # would be byte-identical and collapse. The schema carries no sequence
    # field, so that case is unresolvable in principle, not just here; zero such
    # (id, object) collisions in the sampled file.
    #
    # Full-record equality is exactly the documented notion of a duplicate, so
    # it needs no heuristic about which fields distinguish a batch member.
    # Cost is a wash: DISTINCT ON already had to carry all twelve output
    # columns through its hash aggregate, so keying on all of them instead of
    # three changes the group *count*, not the row width — and with near-zero
    # real duplicates the group count is ~n either way. What must be avoided is
    # row_number() OVER (PARTITION BY ...), which materializes every raw column
    # (incl. fat unused strings like cs_uri / cs_referer) and is DuckDB's
    # worst-spilling operator; it OOM'd on a real 40M-request hour.
    #
    # Two records identical across all twelve canonical fields are collapsed
    # even if some *raw* field we discard (e.g. time_taken_micros) differed.
    # That is deliberate: such records are indistinguishable in our schema, so
    # keeping both would add a row no consumer could ever tell apart.
    return con.sql(f"""
        SELECT DISTINCT
            to_timestamp(time_micros / 1e6) AS ts,
            '{store}'::VARCHAR AS store,
            cs_bucket::VARCHAR AS bucket,
            COALESCE(cs_object, '')::VARCHAR AS path,
            {_normalize_op_sql} AS op,
            (COALESCE(cs_operation, cs_method, ''))::VARCHAR AS op_raw,
            sc_status::INTEGER AS status,
            COALESCE(sc_bytes, 0)::BIGINT AS bytes_out,
            COALESCE(cs_bytes, 0)::BIGINT AS bytes_in,
            COALESCE(c_ip, '')::VARCHAR AS requester,
            COALESCE(cs_user_agent, '')::VARCHAR AS user_agent,
            s_request_id::VARCHAR AS request_id
        FROM {RAW_VIEW}
        WHERE cs_bucket IS NOT NULL
    """)


def parse_storage_daily(input_glob: str, con: "duckdb.DuckDBPyConnection | None" = None) -> "duckdb.DuckDBPyRelation":
    """Parse the daily ``_storage_`` files (bucket byte-hours) → simple relation.

    Not a request log — one row per bucket per day with storage-class byte
    totals. Useful for cost reconciliation but distinct from the access-log
    plane; treat as a bonus.
    """
    import duckdb as _duckdb
    if con is None:
        con = _duckdb.connect()
    return con.sql(f"""
        SELECT * FROM read_csv({_sql_literal(input_glob)}, header=true, union_by_name=true)
    """)


# SQL fragment: fold GCS's cs_operation/cs_method into the canonical op
# vocabulary (GET | PUT | DELETE | HEAD | LIST | OTHER).
#
# LIST detection is the subtle one — GCS spells "list the objects in a bucket"
# three ways depending on API surface, none of them literally "LIST":
#   XML API:  cs_operation = GET_Bucket      (an HTTP GET on the bucket)
#   JSON API: cs_operation = storage.objects.list / storage.buckets.list
#   (docs' LIST_Bucket spelling appears in older examples)
# The original case-sensitive `LIKE '%_BUCKET'` matched none of the real
# spellings, so every listing was miscounted as GET — the "no LIST rows"
# mystery from the 2026-08-14 smoke. Uppercase once, then match.
_normalize_op_sql = """
CASE
    WHEN upper(COALESCE(cs_operation, '')) LIKE '%LIST%'
         OR upper(COALESCE(cs_operation, '')) = 'GET_BUCKET' THEN 'LIST'
    WHEN upper(COALESCE(cs_operation, '')) LIKE 'GET%'
         OR upper(COALESCE(cs_operation, '')) LIKE 'STORAGE.%.GET' OR cs_method = 'GET' THEN 'GET'
    WHEN upper(COALESCE(cs_operation, '')) LIKE 'PUT%' OR cs_method = 'PUT'
         OR upper(COALESCE(cs_operation, '')) IN ('POST_OBJECT', 'POST_UPLOADS')
         OR upper(COALESCE(cs_operation, '')) SIMILAR TO 'STORAGE\\..*\\.(INSERT|PATCH|UPDATE)' THEN 'PUT'
    WHEN upper(COALESCE(cs_operation, '')) LIKE 'HEAD%' OR cs_method = 'HEAD' THEN 'HEAD'
    WHEN upper(COALESCE(cs_operation, '')) LIKE 'DELETE%'
         OR upper(COALESCE(cs_operation, '')) LIKE 'STORAGE.%.DELETE' OR cs_method = 'DELETE' THEN 'DELETE'
    ELSE 'OTHER'
END::VARCHAR
"""


def _sql_literal(s: str) -> str:
    """Safely embed a string literal in SQL (single quotes; escape internal)."""
    return "'" + s.replace("'", "''") + "'"
