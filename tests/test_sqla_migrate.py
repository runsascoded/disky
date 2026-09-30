"""Specs for the schema catch-up pass (`disk_tree.sqla.migrate`): a model gains a
column, an existing DB is missing it, and the pass adds it — the gap that broke
`/api/staged` (`no such column: deletion_run.batch_job`) when `batch_job` landed
without a migration."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from disk_tree.sqla import DeletionRun
from disk_tree.sqla.base import Base
from disk_tree.sqla.migrate import add_missing_columns, missing_columns


def _cols(eng, table: str) -> list[str]:
    return [c["name"] for c in inspect(eng).get_columns(table)]


@pytest.fixture
def eng(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(e)
    return e


def test_fresh_db_has_nothing_missing(eng):
    assert missing_columns(eng) == []
    assert add_missing_columns(eng) == []


def test_adds_a_nullable_column_an_older_db_lacks(eng):
    with eng.begin() as c:
        c.exec_driver_sql("ALTER TABLE deletion_run DROP COLUMN batch_job")
    # the repro: the ORM selects every mapped column, so the server's query fails
    with Session(eng) as s, pytest.raises(OperationalError, match="no such column: deletion_run.batch_job"):
        s.scalars(select(DeletionRun)).all()
    assert missing_columns(eng) == [("deletion_run", "batch_job")]

    assert add_missing_columns(eng) == [("deletion_run", "batch_job")]
    assert _cols(eng, "deletion_run") == [c.name for c in DeletionRun.__table__.columns]
    with Session(eng) as s:
        assert s.scalars(select(DeletionRun)).all() == []
    assert add_missing_columns(eng) == []   # idempotent


def test_adds_a_not_null_column_with_its_scalar_default(eng):
    with eng.begin() as c:
        c.exec_driver_sql("INSERT INTO plan (name, created_by, created_ts, state) VALUES ('Staged', 'ryan', '2026-09-23 00:00:00', 'open')")
        c.exec_driver_sql("INSERT INTO deletion_run (run_id, plan_id, mode, actor, started_ts, deleted_bytes, deleted_objects, skipped_gone, undo_state) VALUES ('r1', 1, 'dry', 'ryan', '2026-09-23 00:00:00', 0, 0, 0, 'none')")
        c.exec_driver_sql("ALTER TABLE deletion_run DROP COLUMN skipped_gone")
        c.exec_driver_sql("ALTER TABLE deletion_run DROP COLUMN batch_job")
    assert add_missing_columns(eng) == [("deletion_run", "skipped_gone"), ("deletion_run", "batch_job")]
    with Session(eng) as s:
        run = s.get(DeletionRun, "r1")
        assert (run.skipped_gone, run.batch_job) == (0, None)   # existing row got the model default


def test_a_table_the_db_lacks_is_created_not_altered(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'u.db'}")
    Base.metadata.tables["scan"].create(e)   # an old DB: only the scan table
    assert missing_columns(e) == []          # no columns missing from tables that exist
    assert add_missing_columns(e) == []
    assert sorted(inspect(e).get_table_names()) == sorted(Base.metadata.tables)   # create_all ran
