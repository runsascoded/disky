"""What changed between two scans, per object (specs/done/change-metrics.md §1.2): the classification rules, as SQL
over path versions. Pure logic: nothing here reads or writes a store, and nothing calls it yet. It's what remains of
the reverted per-scan counters (phase 1, 2026-11-27), kept for VM mode (specs/vm-pilot.md), which answers added /
deleted / modified over any slice at query time from the interval store's version rows.

An object (a `kind = 'file'` path version) is **added** at stamp t where a version of its path opens at t and none closed
there, **deleted** where one closed at t and none opened, and **modified** where one closed and one opened whose identity
differs: both carry a version token (`version`, or the legacy `generation`) and the tokens differ; else (a token NULL)
the sizes differ, or the mtimes do where they are write times (`mtime: "write"`; `"coarse"`, as hf's repo-wide
`lastModified`, never decides). Anything else that opens a version (a read-day split, an owner or class change, a churn
split, a NULL → token transition with the same size and mtime) is a touch, not a change. A file ↔ directory flip is the
file row's add or delete.

The same events come from any path versions where each version is opened at its `vf` and closed at its `vt` (≠ `OPEN`):
a run's deltas (opened: `op = 1`; closed: every row with a `vt`) and a generation's folded pieces (every piece opened;
consecutive pieces of one version meet, a touch).
"""
from __future__ import annotations

from .static_names import OPEN

#: What a scan's mtimes say about a rewrite: `write` (GCS `timeCreated`, S3 / R2 `LastModified`: every write moves it),
#: `coarse` (hf's repo-wide dates: never decides a mod).
MTIMES = ("write", "coarse")
EVENTS = ("added", "deleted", "modified")
#: What decided whether a version pair is a mod: both tokens, else the size, else the mtime.
BASES = ("token", "size", "mtime")


def token_sql(column: str | None) -> str:
    """The version token as VARCHAR SQL: `column` (the identity column: `version`, or the legacy `generation`), NULL
    where the versions carry none."""
    return f'CAST("{column}" AS VARCHAR)' if column else "NULL::VARCHAR"


def events_sql(src: str, opened: str, *, tok: str, mtime: str) -> str:
    """The file events in `src` (path versions: `depth, path, vf, vt, kind, size, mtime`, and `tok` the version token as
    SQL), the versions passing `opened` (SQL) opened at their `vf`, every one with a `vt` closed there: `(t, path, event,
    basis, size, psize)` per path and stamp `t` where something opened or closed. `event` is NULL for a touch; `basis`
    (`BASES`) is what decided between a mod and a touch, NULL for an add or a delete; `size` the opened version's, `psize`
    the closed one's."""
    if mtime not in MTIMES:
        raise ValueError(f"mtime: one of {MTIMES}, not {mtime!r}")
    write = "true" if mtime == "write" else "false"
    return f"""WITH f AS (SELECT depth, path, vf, vt, size, mtime, {tok} AS tok, ({opened}) AS opened FROM {src} WHERE kind = 'file'),
        o AS (SELECT depth, path, vf AS t, size, mtime, tok FROM f WHERE opened),
        c AS (SELECT depth, path, vt AS t, size, mtime, tok FROM f WHERE vt <> {OPEN})
        SELECT coalesce(o.t, c.t) AS t, coalesce(o.path, c.path) AS path,
            CASE WHEN c.path IS NULL THEN 'added' WHEN o.path IS NULL THEN 'deleted'
                 WHEN o.tok IS NOT NULL AND c.tok IS NOT NULL THEN CASE WHEN o.tok <> c.tok THEN 'modified' END
                 WHEN o.size <> c.size OR ({write} AND o.mtime <> c.mtime) THEN 'modified' END AS event,
            CASE WHEN c.path IS NULL OR o.path IS NULL THEN NULL
                 WHEN o.tok IS NOT NULL AND c.tok IS NOT NULL THEN 'token' WHEN o.size <> c.size THEN 'size' ELSE 'mtime' END AS basis,
            o.size AS size, c.size AS psize
        FROM o FULL JOIN c ON o.depth = c.depth AND o.path = c.path AND o.t = c.t"""


def comparable(prev: dict, scan: dict) -> bool:
    """Whether `scan`'s changes since `prev` (`scans.json` entries) mean anything: both list objects (`version` ≥ 2). Not
    where objects are first listed (`interval_churn.counts_at`: every object would appear added), nor across a
    directory-only scan."""
    return prev.get("version", 1) >= 2 and scan.get("version", 1) >= 2
