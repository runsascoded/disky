"""DuckDB query helpers: a one-row result without the `fetchone() -> tuple | None`
dance at every call site (an aggregate or a `COPY` always yields its row)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb


def one(con: "duckdb.DuckDBPyConnection", sql: str, *params: Any) -> tuple[Any, ...]:
    """The single row `sql` returns; raises if it returns none."""
    row = con.execute(sql, *params).fetchone()
    if row is None:
        raise ValueError(f"query returned no row: {sql}")
    return row


def scalar(con: "duckdb.DuckDBPyConnection", sql: str, *params: Any) -> Any:
    """The first column of the single row `sql` returns."""
    return one(con, sql, *params)[0]
