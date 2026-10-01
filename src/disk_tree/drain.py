"""The server-side drainer (spec ``specs/staged-delete.md``, CP4; the laptop
case: ``specs/m3-site.md`` Phase 3).

The edge (CP2) *enqueues* a deletion run (``finished_ts IS NULL``) — it can't
reach arbitrary user buckets, or a laptop's disk. A process with the creds
(the laptop, or a worker) drains those runs: for each staged URI it sizes from
the local scan DB, deletes (or trashes) through the backend, records a per-URI
band, and writes the totals + ``finished_ts`` back to D1. Each poll also
writes its heartbeat (``agents``), which the ``laptop`` executor checks before
it accepts a dispatch.

Two D1 schemas hold these tables — ``ui/``'s (``plan_items.uri``,
``deletion_runs.batch_job``, ``deletion_bands.deleted``) and ``site/``'s
(``plan_items.prefix``, ``undo_deadline`` / ``purge_state``, no ``batch_job``)
— so the loop reads the schema first (:class:`Schema`) and speaks whichever
it finds.

The DB is duck-typed (``.query(sql, params) -> list[dict]``) so the loop runs
against the real :class:`~disk_tree.d1.D1Client` or an in-memory fake in tests;
``size_fn``/``delete_fn``/``trash_fn`` are injected exactly as the CP1 engine
does.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from disk_tree.staged import uncovered

SizeFn = Callable[[str], "tuple[int, int]"]
DeleteFn = Callable[[str], None]
TrashFn = Callable[[str, str], str]  # (uri, run_id) -> where it went
Announce = Callable[[dict], None]
AfterFn = Callable[[dict], None]  # a real run that deleted something just finished

AGENT = "drainer"


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@dataclass(frozen=True)
class Schema:
    """Which staged-delete schema the D1 carries (see the module doc)."""

    item_col: str          # plan_items: `uri` (ui/) or `prefix` (site/)
    batch_job: bool        # deletion_runs.batch_job (the Batch hand-off)
    bands_deleted: bool    # deletion_bands.deleted (ui/); site/ has `gone` only
    hold: bool             # deletion_runs.undo_deadline + purge_state (site/)
    agents: bool           # the `agents` heartbeat table
    freed: bool = False    # deletion_runs.freed_bytes: a dry run's measured reclaim (specs/apfs-sharing.md)

    @classmethod
    def detect(cls, db: Any) -> "Schema":
        def cols(table: str) -> set[str]:
            return {r["name"] for r in db.query(f"PRAGMA table_info({table})")}

        items, runs, bands = cols("plan_items"), cols("deletion_runs"), cols("deletion_bands")
        return cls(
            item_col="prefix" if "prefix" in items else "uri",
            batch_job="batch_job" in runs,
            bands_deleted="deleted" in bands,
            hold="undo_deadline" in runs and "purge_state" in runs,
            agents=bool(cols("agents")),
            freed="freed_bytes" in runs,
        )


def pending_runs(db: Any, schema: Optional[Schema] = None) -> list[dict]:
    """Runs awaiting execution, oldest first: not finished and not handed to Batch
    (a `batch_job` run is running remotely — the Batch job finishes it)."""
    schema = schema or Schema.detect(db)
    return db.query(
        "SELECT run_id, plan_id, mode, actor, started_ts FROM deletion_runs "
        "WHERE finished_ts IS NULL" + (" AND batch_job IS NULL" if schema.batch_job else "") + " ORDER BY started_ts"
    )


def run_items(db: Any, plan_id: int, schema: Optional[Schema] = None) -> list[str]:
    col = (schema or Schema.detect(db)).item_col
    return [r[col] for r in db.query(f"SELECT {col} FROM plan_items WHERE plan_id = ? ORDER BY {col}", [plan_id])]


def heartbeat(db: Any, schema: Schema, host: Optional[str] = None, now: Callable[[], int] = _now, name: str = AGENT) -> bool:
    """Record this agent's check-in; False when the schema has no `agents` table."""
    if not schema.agents:
        return False
    db.query("INSERT OR REPLACE INTO agents (name, seen_ts, host) VALUES (?, ?, ?)", [name, now(), host])
    return True


def execute_run(
    db: Any,
    run: dict,
    *,
    size_fn: SizeFn,
    delete_fn: DeleteFn,
    schema: Optional[Schema] = None,
    trash_fn: Optional[TrashFn] = None,
    undo_state: str = "none",
    hold_s: Optional[int] = None,
    now: Callable[[], int] = _now,
    reclaim_fn: Optional[Callable[[list[str]], int]] = None,
) -> dict:
    """Execute one enqueued run. A dry run sizes every staged URI and records
    the would-delete totals. A real run deletes each URI inline — through
    `trash_fn` (a rename into the run's trash dir; the site's `undo_deadline`
    = finish + `hold_s` and `purge_state = 'pending'` say so) or `delete_fn` —
    records a band per URI, and finishes the run; a single URI's failure is
    recorded, not fatal. A dry run on a schema with `freed_bytes` also measures what the set as a
    whole would free (`reclaim_fn`: extents shared with a clone or hardlink
    outside it don't count); a failed measurement leaves it unset.
    Returns a summary."""
    schema = schema or Schema.detect(db)
    dry = run.get("mode") == "dry"
    # a staged dir covers staged items under it: size + delete each byte once
    uris = uncovered(run_items(db, run["plan_id"], schema))
    sized = [(uri, *size_fn(uri)) for uri in uris]  # (uri, bytes, objects)

    deleted_bytes = deleted_objects = deleted_paths = 0
    errors: list[tuple[str, str]] = []
    trashed = not dry and trash_fn is not None
    for uri, nbytes, nobjs in sized:
        deleted = 0
        try:
            if not dry:
                if trash_fn is not None:
                    trash_fn(uri, run["run_id"])
                else:
                    delete_fn(uri)
            deleted = 0 if dry else 1
            deleted_paths += deleted
            deleted_bytes += nbytes
            deleted_objects += nobjs
        except Exception as e:  # one URI failing must not strand the rest of the run
            errors.append((uri, str(e)))
        # idempotent on a retry of a half-finished run
        if schema.bands_deleted:
            db.query(
                f"INSERT OR REPLACE INTO deletion_bands (run_id, {schema.item_col}, bytes, objects, deleted, gone) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                [run["run_id"], uri, nbytes, nobjs, deleted],
            )
        else:
            db.query(
                f"INSERT OR REPLACE INTO deletion_bands (run_id, {schema.item_col}, bytes, objects, gone) "
                "VALUES (?, ?, ?, ?, 0)",
                [run["run_id"], uri, nbytes, nobjs],
            )
    freed: Optional[int] = None
    if dry and schema.freed and reclaim_fn is not None and uris:
        try:
            freed = reclaim_fn(uris)
        except Exception as e:  # sizing still stands without it
            errors.append(("(reclaim)", str(e)))
    finished = now()
    # `deleted_paths` (what actually went), not the scan's object count, keys
    # the undo state and the hold: a path no scan covers sizes as 0.
    sets = "finished_ts = ?, deleted_bytes = ?, deleted_objects = ?, undo_state = ?"
    params: list[Any] = [finished, deleted_bytes, deleted_objects, undo_state if deleted_paths else "none"]
    if freed is not None:
        sets += ", freed_bytes = ?"
        params.append(freed)
    if schema.hold and trashed and deleted_paths:
        sets += ", undo_deadline = ?, purge_state = 'pending'"
        params.append(finished + hold_s if hold_s is not None else None)
    db.query(f"UPDATE deletion_runs SET {sets} WHERE run_id = ?", [*params, run["run_id"]])
    return {
        "run_id": run["run_id"], "plan_id": run["plan_id"], "actor": run.get("actor"), "mode": run.get("mode"),
        "items": len(uris), "deleted_paths": deleted_paths, "deleted_bytes": deleted_bytes, "deleted_objects": deleted_objects,
        "errors": errors, "finished_ts": finished, "trashed": trashed,
        **({"freed_bytes": freed} if freed is not None else {}),
    }


def drain_once(
    db: Any,
    *,
    size_fn: SizeFn,
    delete_fn: DeleteFn,
    schema: Optional[Schema] = None,
    trash_fn: Optional[TrashFn] = None,
    undo_state: str = "none",
    hold_s: Optional[int] = None,
    announce: Optional[Announce] = None,
    after: Optional[AfterFn] = None,
    now: Callable[[], int] = _now,
    host: Optional[str] = None,
    reclaim_fn: Optional[Callable[[list[str]], int]] = None,
) -> list[dict]:
    """Check in, then execute every pending run once. `after` runs once per real run that deleted
    something (the laptop re-captures so the map reflects it). Returns a
    summary per run."""
    schema = schema or Schema.detect(db)
    heartbeat(db, schema, host=host, now=now)
    out = []
    for run in pending_runs(db, schema):
        summary = execute_run(
            db, run, size_fn=size_fn, delete_fn=delete_fn, schema=schema, trash_fn=trash_fn,
            undo_state=undo_state, hold_s=hold_s, now=now, reclaim_fn=reclaim_fn,
        )
        out.append(summary)
        if announce:
            announce(summary)
        if after and summary["deleted_paths"]:
            after(summary)
    return out
