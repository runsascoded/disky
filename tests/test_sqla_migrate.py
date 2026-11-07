"""Specs for the schema catch-up pass (`disk_tree.sqla.migrate`): a model gains a
column, an existing DB is missing it, and the pass adds it — otherwise every ORM
select against the older DB fails with `no such column`."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from disk_tree.sqla.base import Base
from disk_tree.sqla.migrate import add_missing_columns, missing_columns
from disk_tree.sqla.model import Scan


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
        c.exec_driver_sql("ALTER TABLE scan DROP COLUMN mtime")
    # the repro: the ORM selects every mapped column, so the query fails
    with Session(eng) as s, pytest.raises(OperationalError, match="no such column: scan.mtime"):
        s.scalars(select(Scan)).all()
    assert missing_columns(eng) == [("scan", "mtime")]

    assert add_missing_columns(eng) == [("scan", "mtime")]
    assert _cols(eng, "scan") == [c.name for c in Scan.__table__.columns]
    with Session(eng) as s:
        assert s.scalars(select(Scan)).all() == []
    assert add_missing_columns(eng) == []   # idempotent


def test_a_table_the_db_lacks_is_created_not_altered(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'u.db'}")
    Base.metadata.tables["scan"].create(e)   # an old DB: only the scan table
    assert missing_columns(e) == []          # no columns missing from tables that exist
    assert add_missing_columns(e) == []
    assert sorted(inspect(e).get_table_names()) == sorted(Base.metadata.tables)   # create_all ran
