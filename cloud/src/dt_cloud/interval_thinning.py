"""Project canonical interval tables onto retained observation timestamps.

Only structural validity bounds move. Every other column, including nullable object identity, observed timestamps,
and any churn columns, is compared and carried exactly. Thus snapshot answers at retained timestamps are unchanged.
This is not a churn migration: an existing ``churn_end`` describes its ORIGINAL closing boundary, and a transient
object absent at every retained timestamp disappears. Before cutting a projected generation's served sorts, callers
must implement their chosen churn contract (recompute from retained observations, or preserve an independent ledger).

``project_ranges`` operates on a private, already folded candidate, before ``interval_store.cut_gen``. The caller
owns its retained ``scans.json``, churn handling, verification, publication and source-generation protection. Never
call this over a published generation. ``project_sql`` also works on static-name coalesced intervals, keyed by usr.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pyarrow.parquet as pq

from . import static_names as sn


TABLES = ("pv", "pvl", "rd", "sv", "svt", "attribution")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def retained_stamps(kept: Sequence[int]) -> tuple[int, ...]:
    """Validate an explicit, increasing, nonempty retained timestamp sequence; OPEN is never an observation."""
    stamps = tuple(kept)
    if not stamps or any(type(t) is not int or t < 0 or t >= sn.OPEN for t in stamps):
        raise ValueError("kept timestamps must be nonempty integers in [0, OPEN)")
    if any(a >= b for a, b in zip(stamps, stamps[1:])):
        raise ValueError("kept timestamps must be strictly increasing")
    return stamps


def project_sql(
    source: str,
    columns: Sequence[str],
    kept: Sequence[int],
    *,
    key: Sequence[str] = ("depth", "path"),
) -> str:
    """An ordered SELECT projecting nonoverlapping ``source`` intervals onto ``kept``.

    ``source`` is a SELECT, not a relation name. ``columns`` gives the complete output schema order; every column
    besides the key and vf/vt is semantic state, compared with NULL-safe equality. ``key`` must include usr for owner
    slices and static coalesced intervals. Two forward ASOF joins map each boundary to the least retained timestamp
    at or after it (OPEN when none). Empty intervals vanish; adjacent identical states then merge. Equal states
    separated only by discarded observations can merge. No original-boundary lookup or per-scan expansion is needed.
    """
    stamps = retained_stamps(kept)
    names, keys = tuple(columns), tuple(key)
    if len(set(names)) != len(names) or len(set(keys)) != len(keys):
        raise ValueError("columns and key must not contain duplicates")
    if not keys or not set((*keys, "vf", "vt")).issubset(names) or set(keys).intersection(("vf", "vt")):
        raise ValueError("columns must contain a nonempty key and distinct vf/vt bounds")
    values = [c for c in names if c not in (*keys, "vf", "vt")]
    kcols = ", ".join(_ident(c) for c in keys)
    order = ", ".join(f"{_ident(c)} NULLS FIRST" for c in keys)
    cols = ", ".join(_ident(c) for c in names)
    selected = ", ".join(
        f"coalesce(f.stamp, {sn.OPEN})::BIGINT AS vf" if c == "vf" else
        f"coalesce(t.stamp, {sn.OPEN})::BIGINT AS vt" if c == "vt" else
        f"p.{_ident(c)}" for c in names
    )
    same = " AND ".join(f"lag({_ident(c)}) OVER w IS NOT DISTINCT FROM {_ident(c)}" for c in values) or "true"
    carried = {c: f"first({_ident(c)} ORDER BY vf) AS {_ident(c)}" for c in values}
    aggregate = ", ".join(
        "min(vf)::BIGINT AS vf" if c == "vf" else "max(vt)::BIGINT AS vt" if c == "vt" else
        _ident(c) if c in keys else carried[c] for c in names
    )
    times = ", ".join(f"({t}::BIGINT)" for t in stamps)
    return f"""WITH kept(stamp) AS (VALUES {times}),
        projected AS (SELECT {selected} FROM ({source}) p
            ASOF LEFT JOIN kept f ON p.vf <= f.stamp
            ASOF LEFT JOIN kept t ON p.vt <= t.stamp),
        marked AS (SELECT *, CASE WHEN lag(vt) OVER w = vf AND {same} THEN 0 ELSE 1 END AS __thin_new
            FROM projected WHERE vf < vt WINDOW w AS (PARTITION BY {kcols} ORDER BY vf)),
        grouped AS (SELECT *, sum(__thin_new) OVER (PARTITION BY {kcols} ORDER BY vf ROWS UNBOUNDED PRECEDING) AS __thin_group
            FROM marked),
        islands AS (SELECT {aggregate} FROM grouped GROUP BY {kcols}, __thin_group)
        SELECT {cols} FROM islands ORDER BY {order}, vf"""


def project_parquet(
    con,
    source: Path,
    out: Path,
    kept: Sequence[int],
    *,
    key: Sequence[str] | None = None,
    rg_rows: int = sn.INTERVAL_RG,
) -> dict[str, int]:
    """Stream one projected canonical range, preserving its complete Arrow schema and metadata.

    A sibling temporary is replaced only after a successful write; source == out is supported for a private candidate.
    With key omitted, usr is part of the key whenever its column exists. The connection owns spill/memory limits.
    """
    source, out = Path(source), Path(out)
    schema = pq.read_schema(source)
    keys = tuple(key) if key is not None else ("depth", "path", *(["usr"] if "usr" in schema.names else []))
    sql = project_sql(f"SELECT * FROM read_parquet({sn.q(str(source))})", schema.names, kept, key=keys)
    before = pq.read_metadata(source).num_rows
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.thinning.parquet")
    dictionary = [c for c in ("kind", "us", "usr", "user", "source") if c in schema.names]
    rows = sn.write_sorted(sn._batches(con, sql), tmp, schema, rg_rows, dictionary=dictionary)
    tmp.replace(out)
    return {"before": before, "after": rows}


def project_ranges(
    con,
    gen_dir: Path,
    kept: Sequence[int],
    *,
    tables: Sequence[str] = TABLES,
    rg_rows: int = sn.INTERVAL_RG,
) -> dict[str, dict[str, int]]:
    """Project each selected canonical table in a private folded generation; absent optional tables are skipped.

    Returns exact before/after row totals by table. This helper neither changes generation metadata nor cuts served
    sorts. In particular, do not pass the source fold's old path counts into cut_gen: transient-only paths vanished;
    let it recount (paths=None). Existing churn columns remain opaque until the caller handles them explicitly.
    """
    stamps = retained_stamps(kept)
    gen_dir = Path(gen_dir)
    unknown = sorted(set(tables) - set(TABLES))
    if unknown:
        raise ValueError(f"unknown canonical tables: {', '.join(unknown)}")
    totals = {}
    for table in tables:
        files = sorted((gen_dir / table).glob("r*.parquet"))
        if not files:
            continue
        before, after = 0, 0
        for path in files:
            got = project_parquet(con, path, path, stamps, rg_rows=rg_rows)
            before += got["before"]
            after += got["after"]
        totals[table] = {"before": before, "after": after}
    return totals
