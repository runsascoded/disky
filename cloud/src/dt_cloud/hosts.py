"""The capture hosts' registry (D1 `hosts`, `specs/hosts-registry.md`): which store a capture host publishes into.

`dt-cloud hosts resolve HOST` is the ingest's lookup (`infra/aws/ingest.sh`): it prints the host's store
(`primary` = the deployment's own), or exits `UNKNOWN` for a host with no live row (none, or revoked) and
`NO_TABLE` when the D1 has no `hosts` table yet (the ingest then falls back to `PRIMARY_HOST` / `HOST_STORES`).
"""
from __future__ import annotations

import sys

from click import argument, echo, group, option

from .scan_runs import D1Sink, Sink, SqliteSink


UNKNOWN = 3
NO_TABLE = 4


class NoHostsTable(Exception):
    pass


def resolve(sink: Sink, host: str) -> tuple[str | None, str | None]:
    """`(store, revoked_at)` for `host`'s row, `(None, None)` with none; `NoHostsTable` on an unmigrated D1."""
    lit = "'" + host.replace("'", "''") + "'"
    try:
        rows = sink.query(f"SELECT store, revoked_at FROM hosts WHERE id = {lit}")
    except Exception as e:  # D1 says so in its error text; SQLite raises `OperationalError`
        if "no such table: hosts" in str(e):
            raise NoHostsTable() from None
        raise
    if not rows:
        return None, None
    return rows[0]["store"], rows[0]["revoked_at"]


@group("hosts")
def cli():
    """The capture hosts' registry (D1 `hosts`)."""


@cli.command("resolve")
@option("-s", "--sqlite", help="A local SQLite file instead of the deployment's D1 (tests, dry runs)")
@argument("host")
def resolve_cmd(sqlite: str | None, host: str):
    """Print HOST's store (`primary` or a store key); exit 3 for an unknown or revoked host, 4 without the table."""
    sink: Sink = SqliteSink(sqlite, ddl="") if sqlite else D1Sink()
    try:
        store, revoked = resolve(sink, host)
    except NoHostsTable:
        echo("hosts: this D1 has no `hosts` table", err=True)
        sys.exit(NO_TABLE)
    if store is None:
        echo(f"hosts: no host '{host}'", err=True)
        sys.exit(UNKNOWN)
    if revoked:
        echo(f"hosts: host '{host}' was disconnected at {revoked}", err=True)
        sys.exit(UNKNOWN)
    print(store)
