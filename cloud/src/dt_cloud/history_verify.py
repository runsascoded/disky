"""Exact, independent retained-snapshot verification of canonical interval tables.

For every complete semantic row x, let M_x(j) be its multiplicity at retained
observation j. We compare its event vector E_x(0)=M_x(0),
E_x(j)=M_x(j)-M_x(j-1). Prefix sums recover every M_x(j), so equality of these
vectors is equivalent to equality of ALL retained snapshot multisets. Interval
boundaries map independently to observation indexes by counting retained times
strictly below each bound. A piece contributes +1 at its first observation and
-1 after its last. Pieces containing no retained observation contribute nothing.

DuckDB groups the complete semantic values, then EXCEPT ALL compares normalized
events in both directions. There are no probabilistic digests, shared projection
helpers, or expanded scan-by-live-row products. Work is bounded to one range at a
time and DuckDB may spill under the caller's memory/temp configuration. Its cost
still includes decoding every canonical value; this belongs in an offline job.

Only vf/vt and the explicitly named churn_cum/churn_end columns are excluded.
Churn has resampled semantics and requires its separate verification contract.
All other fields, including nullable identities and observed timestamps, compare
exactly under SQL value equality. This proves canonical retained snapshots, not
derived served-file correctness, churn correctness, or between-scan answers.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from . import heartbeat as hb
from .scan_id import check_order, check_scan_id, scan_epoch
from .static_names import err, q


TABLES = ("pv", "pvl", "rd", "sv", "svt", "attribution")
CORE = ("pv", "pvl", "rd")
IGNORED = ("churn_cum", "churn_end")
POSITION = "__history_observation"
DELTA = "__history_delta"


class VerificationError(ValueError):
    """An exact mismatch; audit contains the first failed range, never verified=True."""

    def __init__(self, message: str, audit: dict) -> None:
        super().__init__(message)
        self.audit = audit


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def _fresh_reads(con):
    """A writer connection may have cached the files before atomic replacement."""
    names = ("parquet_metadata_cache", "enable_external_file_cache")
    values = con.execute("SELECT " + ", ".join(f"current_setting('{name}')" for name in names)).fetchone()
    try:
        for name in names:
            con.execute(f"SET {name}=false")
        yield
    finally:
        for name, value in zip(names, values):
            con.execute(f"SET {name}={'true' if value else 'false'}")


def _columns(source: Path, candidate: Path) -> list[str]:
    """Require exact semantic field names/types, independent of Arrow metadata."""
    a, b = pq.read_schema(source), pq.read_schema(candidate)
    for schema, path in ((a, source), (b, candidate)):
        if len(set(schema.names)) != len(schema.names):
            raise ValueError(f"{path}: duplicate schema columns")
        for name in ("vf", "vt"):
            if name not in schema.names or not pa.types.is_integer(schema.field(name).type):
                raise ValueError(f"{path}: {name} must be an integer validity bound")
    fields = [{f.name: f.type for f in schema if f.name not in (*IGNORED, "vf", "vt")} for schema in (a, b)]
    if fields[0] != fields[1]:
        describe = lambda fs: {name: str(dtype) for name, dtype in sorted(fs.items())}
        raise ValueError(f"semantic schema mismatch: {source.name}: {describe(fields[0])!r} != {describe(fields[1])!r}")
    if not {"depth", "path"}.issubset(fields[0]):
        raise ValueError(f"{source}: semantic schema must contain depth and path")
    if set((POSITION, DELTA)).intersection(fields[0]):
        raise ValueError(f"{source}: semantic schema uses reserved verifier columns")
    return sorted(fields[0])


def _event_sql(
    source: Path,
    columns: Sequence[str],
    stamps: Sequence[int],
) -> str:
    """Exact multiplicity-change rows; no per-scan expansion or projection code."""
    cols = ", ".join(_ident(name) for name in columns)
    rawcols = ", ".join(f"r.{_ident(name)}" for name in columns)
    values = ", ".join(f"({stamp}::BIGINT)" for stamp in stamps)
    return f"""WITH kept(stamp) AS (VALUES {values}),
        raw AS MATERIALIZED (SELECT vf, vt, {cols} FROM read_parquet({q(str(source))})),
        boundaries AS (SELECT vf AS bound FROM raw UNION SELECT vt AS bound FROM raw),
        positions AS (SELECT b.bound, count(k.stamp)::BIGINT AS pos FROM boundaries b
            LEFT JOIN kept k ON k.stamp < b.bound GROUP BY b.bound),
        pieces AS (SELECT lo.pos AS a, hi.pos AS b, {rawcols} FROM raw r
            JOIN positions lo ON r.vf = lo.bound JOIN positions hi ON r.vt = hi.bound
            WHERE lo.pos < hi.pos AND lo.pos < {len(stamps)}),
        events AS (SELECT a AS {POSITION}, {cols}, 1::BIGINT AS {DELTA} FROM pieces
            UNION ALL SELECT b AS {POSITION}, {cols}, -1::BIGINT AS {DELTA} FROM pieces WHERE b < {len(stamps)})
        SELECT {POSITION}, {cols}, sum({DELTA})::BIGINT AS {DELTA} FROM events
        GROUP BY {POSITION}, {cols} HAVING sum({DELTA}) <> 0"""


def _verify_pair(
    con,
    source: Path,
    candidate: Path,
    ids: Sequence[str],
    stamps: Sequence[int],
) -> dict:
    columns = _columns(source, candidate)
    for path in (source, candidate):
        bad = con.execute(f"SELECT count(*) FROM read_parquet({q(str(path))}) WHERE vf IS NULL OR vt IS NULL OR vf >= vt").fetchone()[0]
        if bad:
            raise ValueError(f"{path}: {bad} invalid validity intervals")
    token = uuid4().hex
    names = (f"__history_source_{token}", f"__history_candidate_{token}")
    try:
        for name, path in zip(names, (source, candidate)):
            con.execute(f"CREATE TEMP TABLE {name} AS {_event_sql(path, columns, stamps)}")
        a, b = names
        missing = f"SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}"
        extra = f"SELECT * FROM {b} EXCEPT ALL SELECT * FROM {a}"
        counts = [con.execute(f"SELECT count(*), min({POSITION}) FROM ({query})").fetchone() for query in (missing, extra)]
        audit = {
            "range": source.name,
            "columns": columns,
            "source_intervals": pq.read_metadata(source).num_rows,
            "candidate_intervals": pq.read_metadata(candidate).num_rows,
            "source_events": con.execute(f"SELECT count(*) FROM {a}").fetchone()[0],
            "candidate_events": con.execute(f"SELECT count(*) FROM {b}").fetchone()[0],
            "missing_events": counts[0][0],
            "extra_events": counts[1][0],
        }
        if counts[0][0] or counts[1][0]:
            first = min(position for count, position in counts if count)
            audit["first_differing_scan"] = ids[first]
            raise VerificationError(
                f"retained snapshot mismatch: {source.parent.name}/{source.name}: "
                f"{counts[0][0]} missing and {counts[1][0]} extra event rows; first differing scan {ids[first]}",
                audit,
            )
        return audit
    finally:
        for name in names:
            con.execute(f"DROP TABLE IF EXISTS {name}")


def _verify(
    con,
    source: Path,
    candidate: Path,
    retained: list[str],
    tables: Sequence[str],
    required: Sequence[str],
    log: Callable[[str], None] = err,
) -> dict:
    ids = check_order([check_scan_id(scan) for scan in retained])
    if not ids:
        raise ValueError("retained scans must not be empty")
    stamps = [scan_epoch(scan) for scan in ids]
    source, candidate = Path(source), Path(candidate)
    if not source.is_dir() or not candidate.is_dir():
        raise ValueError("source and candidate must be existing canonical generation directories")
    paths = {
        table: [{p.name: p for p in sorted((root / table).glob("r*.parquet"))} for root in (source, candidate)]
        for table in tables
    }
    for table in required:
        if not paths[table][0] or not paths[table][1]:
            raise ValueError(f"required canonical table is absent: {table}")
    reference = set(paths[required[0]][0])
    for root in (source, candidate):
        ranges_file = root / "ranges.json"
        if ranges_file.is_file():
            declared = {f"r{r['i']:04d}.parquet" for r in json.loads(ranges_file.read_text())["ranges"]}
            if reference != declared:
                raise ValueError(f"canonical range inventory differs from {ranges_file}: found={sorted(reference)!r}, declared={sorted(declared)!r}")
    result, absent = {}, []
    for table in tables:
        left, right = paths[table]
        if not left and not right:
            absent.append(table)
            continue
        if set(left) != set(right) or set(left) != reference:
            raise ValueError(f"canonical ranges mismatch: {table}: source={sorted(left)!r}, candidate={sorted(right)!r}, expected={sorted(reference)!r}")
        checks = []
        for name in left:
            stage = f"history verify: {table}/{name} ({len(ids)} retained scans)"
            hb.set_stage(stage)
            log(stage)
            try:
                checks.append(_verify_pair(con, left[name], right[name], ids, stamps))
            except VerificationError as error:
                error.audit["table"] = table
                raise
        result[table] = checks
    return {
        "verified": True,
        "scope": "canonical-retained-snapshots",
        "method": "exact-observation-events-except-all",
        "retained": ids,
        "ignored_columns": list(IGNORED),
        "absent_tables": absent,
        "tables": result,
    }


def verify_ranges(
    con,
    source: Path,
    candidate: Path,
    retained: list[str],
) -> dict:
    """Compare six interval-store canonical tables; core pv/pvl/rd must exist.

    Source must already be independently folded over its selected manifest
    stack. Candidate is the thinned canonical generation. Neither is written.
    Optional owner/attribution tables absent from both are explicitly reported.
    A missing range/table, schema difference, invalid interval or exact event
    mismatch raises; no partial work is returned as a verified audit.
    """
    with _fresh_reads(con):
        return _verify(con, source, candidate, retained, TABLES, CORE)


def verify_static(
    con,
    source: Path,
    candidate: Path,
    retained: list[str],
) -> dict:
    """Compare static coalesced cintervals, including owner slices and all values."""
    with _fresh_reads(con):
        return _verify(con, source, candidate, retained, ("cintervals",), ("cintervals",))
