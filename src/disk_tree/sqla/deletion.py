"""Staged-delete data model (spec ``specs/staged-delete.md``, checkpoint 1).

A staged deletion is a **plan**: a set of URIs (``plan_item``) auto-populated by
trash/stage gestures, dispatched by a **run** (``deletion_run`` + per-URI
``deletion_band``). The schema mirrors gcs's D1 ``plans`` / ``plan_items`` /
``deletion_runs`` / ``deletion_bands`` (the shared cw+gcs shape) so the later
edge/D1 layer (checkpoint 2) is a direct mirror. Tables auto-create via
``Base.metadata.create_all`` — no migration.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Plan(Base):
    """A staged deletion set. ``state`` is ``open`` (accepting stages) until a
    real dispatch closes it."""

    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, init=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")  # open | closed
    closed_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class PlanItem(Base):
    """One staged URI in a plan (the delete set)."""

    __tablename__ = "plan_item"

    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("plan.id"), primary_key=True)
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    added_by: Mapped[str] = mapped_column(String, nullable=False)
    added_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class DeletionRun(Base):
    """One dispatch of a plan. ``mode`` is ``dry`` (report only) or ``real``
    (executed). ``undo_state`` / ``undo_deadline`` describe recoverability where
    the store offers it (S3/R2 versioning markers, GCS soft-delete window)."""

    __tablename__ = "deletion_run"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("plan.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)  # dry | real
    actor: Mapped[str] = mapped_column(String, nullable=False)
    started_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    deleted_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_objects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_gone: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    undo_state: Mapped[str] = mapped_column(String, nullable=False, default="none")  # none|partial|full|expired
    undo_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # CP8: the AWS/GCP Batch job a large run was handed to; NULL = inline/CFN. A
    # run with this set stays unfinished but the drainer skips it (Batch finishes it).
    batch_job: Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class DeletionBand(Base):
    """The per-URI result of a run: what it would delete (dry) or did (real)."""

    __tablename__ = "deletion_band"

    run_id: Mapped[str] = mapped_column(String, ForeignKey("deletion_run.run_id"), primary_key=True)
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    objects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0/1 — actually removed
    gone: Mapped[int] = mapped_column(Integer, nullable=False, default=0)     # 0/1 — already absent
