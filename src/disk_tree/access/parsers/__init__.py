"""Per-provider access-log parsers.

Each parser exposes ``parse(input_glob, store) -> DuckDB relation`` (SQL-first
so downstream aggregation stays out-of-core). Adding a provider = adding a
module here + registering it in :func:`parser_for`. (R2's collector publishes pre-aggregated per-hour
counts, not requests: :mod:`.r2` feeds :func:`~disk_tree.access.aggregate.aggregate_access_leaf` directly.)
"""

from __future__ import annotations


def parser_for(store: str):
    """Return the ``parse`` callable for ``store``. Raises for unknown stores."""
    if store == 'gcs':
        from . import gcs
        return gcs.parse
    raise ValueError(f"no access-log parser registered for store={store!r}")
