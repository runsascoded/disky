"""The staged-delete engine (spec ``specs/staged-delete.md``, checkpoint 1).

Stage URIs into a shared open :class:`~disk_tree.sqla.Plan`, then dispatch the
plan — a :class:`~disk_tree.sqla.DeletionRun` that deletes each item (real) or
reports it (dry), recording a per-URI :class:`~disk_tree.sqla.DeletionBand`.

Deployment-agnostic: ``delete_fn(uri)`` (what actually removes the object — the
existing ``backend_for(uri).delete``) and ``size_fn(uri) -> (bytes, objects)``
are injected, so the engine has no backend or DB-init coupling and unit-tests
against a plain session with fakes.
"""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .sqla import DeletionBand, DeletionRun, Plan, PlanItem

STAGED = "Staged"

SizeFn = Callable[[str], "tuple[int, int]"]
DeleteFn = Callable[[str], None]
RestoreFn = Callable[[str], int]


def _now() -> datetime:
    return datetime.now().astimezone()


def _canonical(uri: str) -> str:
    from disk_tree.backends import canonical

    return canonical(uri)


def covers(a: str, b: str) -> bool:
    """Whether deleting ``a`` deletes ``b``: ``b`` is ``a`` or lies under it
    (a directory / key prefix). ``s3://b/a`` covers ``s3://b/a/x``, not
    ``s3://b/ab``."""
    return b == a or b.startswith(a.rstrip('/') + '/')


def uncovered(uris: Iterable[str]) -> list[str]:
    """``uris`` minus those under another of them (order kept)."""
    us = list(uris)
    return [u for u in us if not any(o != u and covers(o, u) for o in us)]


def open_plan(session: Session, who: str, name: str = STAGED) -> Plan:
    """The shared open plan named ``name``, created (empty) if none is open."""
    plan = session.scalars(
        select(Plan).where(Plan.name == name, Plan.state == "open").order_by(Plan.id.desc())
    ).first()
    if plan is None:
        plan = Plan(name=name, created_by=who, created_ts=_now())
        session.add(plan)
        session.flush()
    return plan


def stage(session: Session, uris: Iterable[str], who: str, note: str | None = None) -> tuple[Plan, list[str]]:
    """Add ``uris`` to the shared open plan. Returns the plan and the URIs newly
    staged. The plan's items never nest: a URI already covered by a staged one
    (itself, or a staged ancestor dir) is skipped, and staging a dir absorbs
    its staged descendants — so a plan's sizes never double-count."""
    plan = open_plan(session, who)
    have = {it.uri: it for it in items(session, plan)}
    added: list[str] = []
    for raw in uris:
        uri = _canonical(raw)
        if any(covers(h, uri) for h in have):
            continue
        for h in [h for h in have if covers(uri, h)]:
            session.delete(have.pop(h))
        have[uri] = PlanItem(plan_id=plan.id, uri=uri, added_by=who, added_ts=_now(), note=note)
        session.add(have[uri])
        added.append(uri)
    session.flush()
    return plan, added


def unstage(session: Session, uris: Iterable[str]) -> int:
    """Remove ``uris`` from every open plan. Returns the number removed."""
    open_ids = set(session.scalars(select(Plan.id).where(Plan.state == "open")))
    removed = 0
    for raw in uris:
        uri = _canonical(raw)
        for item in session.scalars(select(PlanItem).where(PlanItem.uri == uri, PlanItem.plan_id.in_(open_ids))):
            session.delete(item)
            removed += 1
    session.flush()
    return removed


def plan_by_ref(session: Session, ref: str | None) -> Plan | None:
    """Resolve a plan by id (numeric) or name; ``None`` -> the open ``Staged`` plan."""
    if ref is None:
        return session.scalars(
            select(Plan).where(Plan.name == STAGED, Plan.state == "open").order_by(Plan.id.desc())
        ).first()
    if ref.isdigit():
        return session.get(Plan, int(ref))
    return session.scalars(select(Plan).where(Plan.name == ref).order_by(Plan.id.desc())).first()


def items(session: Session, plan: Plan) -> list[PlanItem]:
    return list(session.scalars(select(PlanItem).where(PlanItem.plan_id == plan.id).order_by(PlanItem.uri)))


def restorable_bands(session: Session, run: DeletionRun) -> list[DeletionBand]:
    """The bands a run actually deleted (the undo candidates)."""
    return list(
        session.scalars(
            select(DeletionBand)
            .where(DeletionBand.run_id == run.run_id, DeletionBand.deleted == 1)
            .order_by(DeletionBand.uri)
        )
    )


def undo_run(session: Session, run: DeletionRun, restore_fn: RestoreFn) -> tuple[DeletionRun, list[tuple[str, int]]]:
    """Restore a run's deleted URIs via ``restore_fn(uri) -> objects restored``
    (the store's undo — e.g. removing a versioned bucket's delete-markers).
    Records ``undo_state`` (``full`` every band restored something, ``partial``
    some did, else unchanged) and returns the per-URI counts."""
    bands = restorable_bands(session, run)
    results = [(b.uri, restore_fn(b.uri)) for b in bands]
    restored_any = any(n for _, n in results)
    restored_all = bool(results) and all(n for _, n in results)
    if restored_all:
        run.undo_state = "full"
    elif restored_any:
        run.undo_state = "partial"
    session.flush()
    return run, results


def dispatch(
    session: Session,
    plan: Plan,
    actor: str,
    *,
    for_real: bool,
    delete_fn: DeleteFn,
    size_fn: SizeFn,
    undo_state: str = "none",
    uris: Iterable[str] | None = None,
) -> DeletionRun:
    """Execute ``plan``: size each item (``size_fn``) and, when ``for_real``,
    delete it (``delete_fn``). Records a :class:`DeletionRun` + one
    :class:`DeletionBand` per item; a real dispatch closes the plan. ``dry`` (the
    default) deletes nothing and leaves the plan open.

    ``uris`` restricts the run to those staged items (one-at-a-time deletes
    from the Staged page): a real subset run removes them from the plan and
    closes it only once nothing is left staged. A URI not staged in ``plan``
    raises ``KeyError`` — nothing runs."""
    started = _now()
    # a plan takes many dry runs + one real, so the second-resolution timestamp
    # alone collides; a random suffix keeps run_ids unique
    run_id = f"{plan.id}-{started.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
    run = DeletionRun(
        run_id=run_id, plan_id=plan.id, mode="real" if for_real else "dry",
        actor=actor, started_ts=started, undo_state=undo_state if for_real else "none",
    )
    staged_items = items(session, plan)
    subset = uris is not None
    if subset:
        by_uri = {it.uri: it for it in staged_items}
        want = sorted({_canonical(u) for u in uris})
        unknown = [u for u in want if u not in by_uri]
        if unknown:
            raise KeyError(f"not staged in plan {plan.id}: {', '.join(unknown)}")
        targets = [by_uri[u] for u in want]
    else:
        targets = staged_items
    # items staged before nesting was collapsed at stage time: an item under
    # another target goes with it, so it's neither sized nor deleted twice
    keep = set(uncovered(it.uri for it in targets))
    covered = [it for it in targets if it.uri not in keep]
    targets = [it for it in targets if it.uri in keep]
    session.add(run)
    for item in targets:
        nbytes, nobjs = size_fn(item.uri)
        deleted = 0
        if for_real:
            delete_fn(item.uri)
            deleted = 1
            run.deleted_bytes += nbytes
            run.deleted_objects += nobjs
            if subset:
                session.delete(item)
        session.add(DeletionBand(run_id=run_id, uri=item.uri, bytes=nbytes, objects=nobjs, deleted=deleted))
    if for_real and subset:
        for item in covered:
            session.delete(item)
    run.finished_ts = _now()
    if for_real and (not subset or len(targets) + len(covered) == len(staged_items)):
        plan.state = "closed"
        plan.closed_ts = run.finished_ts
    session.flush()
    return run
