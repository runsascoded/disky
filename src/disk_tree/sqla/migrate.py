"""Schema catch-up: add the columns a model has that an existing DB lacks.

``Base.metadata.create_all`` creates missing *tables* but never alters an
existing one, so a column added to a model (``DeletionRun.batch_job``, CP8)
silently broke every ORM select against an older ``disk-tree.db`` (the ORM
names every mapped column: ``no such column: deletion_run.batch_job``). This
pass diffs each model against ``PRAGMA table_info`` and ``ALTER TABLE … ADD
COLUMN`` the gaps, so the next added column needs no hand-written migration.

Runs after every ``create_all`` (``sqla.db.init``, ``staged_backend``) and from
``disk-tree migrate``. Only additive: a column the DB has that the model
doesn't is left alone; a NOT NULL column without a scalar default can't be
added to a populated SQLite table and raises rather than guessing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import inspect

from .base import Base

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def missing_columns(engine: "Engine") -> list[tuple[str, str]]:
    """``(table, column)`` for every model column absent from an existing table,
    in model order. Tables the DB lacks entirely are ``create_all``'s job and
    are not listed."""
    insp = inspect(engine)
    have_tables = set(insp.get_table_names())
    out: list[tuple[str, str]] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in have_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        out.extend((table.name, col.name) for col in table.columns if col.name not in have)
    return out


def add_missing_columns(engine: "Engine") -> list[tuple[str, str]]:
    """Create any tables the DB lacks, then add each missing column (see
    :func:`missing_columns`). Returns the columns added."""
    Base.metadata.create_all(engine)
    added = missing_columns(engine)
    if not added:
        return []
    dialect = engine.dialect
    with engine.begin() as conn:
        for table_name, col_name in added:
            col = Base.metadata.tables[table_name].columns[col_name]
            ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col.type.compile(dialect)}'
            if not col.nullable:
                default = col.default
                if default is None or default.is_callable or not default.is_scalar:
                    raise RuntimeError(
                        f"{table_name}.{col_name} is NOT NULL with no scalar default: "
                        f"it can't be added to an existing table without a hand-written migration"
                    )
                ddl += f" NOT NULL DEFAULT {_literal(default.arg)}"
            conn.exec_driver_sql(ddl)
    return added


def _literal(v: object) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise RuntimeError(f"unsupported scalar default {v!r}")
