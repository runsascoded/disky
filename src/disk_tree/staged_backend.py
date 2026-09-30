"""Wiring between the pure staged-delete engine (`disk_tree.staged`) and the
real backends/DB (spec ``specs/staged-delete.md``).

The engine takes ``size_fn``/``delete_fn`` injected so it stays backend- and
DB-free; this is the one place that binds them to the actual scan DB and
``backend_for``. Shared by the CLI (``cli/staged.py``) and the Flask server.

The session comes from a standalone SQLAlchemy engine over
``config.SQLITE_PATH`` — *not* flask-sqlalchemy's ``init()``, which pushes its
own app context and so corrupts the server's request context when called inside
a request handler.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

_ENGINE: "Engine | None" = None


def _engine() -> "Engine":
    """A process-wide engine over the current ``config.SQLITE_PATH`` (rebuilt if
    the root changes, e.g. a library switch). The deletion tables auto-create,
    and columns the models gained since the DB was made are added."""
    global _ENGINE
    from sqlalchemy import create_engine

    from . import config
    from .sqla import Plan  # noqa: F401  (registers every model on `Base.metadata`)
    from .sqla.migrate import add_missing_columns

    url = f"sqlite:///{config.SQLITE_PATH}"
    if _ENGINE is None or str(_ENGINE.url) != url:
        _ENGINE = create_engine(url)
        add_missing_columns(_ENGINE)  # create_all + the schema catch-up pass
    return _ENGINE


def session() -> "Session":
    """A fresh session on the scan DB (caller commits/closes)."""
    from sqlalchemy.orm import Session

    return Session(_engine())


def describe(uri: str) -> dict:
    """``{bytes, objects, kind}`` for ``uri`` from the freshest scan covering
    it: a scan *of* ``uri`` answers from its denormalized root stats (a scan
    root is a directory), otherwise the nearest ancestor's scan is opened at
    the one row (``objects`` counts the item itself plus its descendants).
    ``{0, 0, None}`` if no scan covers it — the executor still deletes; this is
    for the report and the Staged page."""
    import sqlite3

    from disk_tree import config
    from disk_tree.backends import canonical
    from disk_tree.registry import freshest_scan_covering

    none = {"bytes": 0, "objects": 0, "kind": None}
    uri = canonical(uri)
    con = sqlite3.connect(config.SQLITE_PATH)
    con.row_factory = sqlite3.Row
    try:
        scan = freshest_scan_covering(con, uri)
    finally:
        con.close()
    if scan is None:
        return none
    if scan["path"] == uri:
        return {"bytes": scan["size"] or 0, "objects": (scan["n_desc"] or 0) + 1, "kind": "dir"}
    from disk_tree.diff import resolve_chunk_for_path
    from disk_tree.storage import get_backend

    rel = uri[len(scan["path"].rstrip("/") + "/"):]
    blob, rebased = resolve_chunk_for_path(scan["blob"], rel)
    depth = rebased.count("/") + 1
    df = get_backend().load(blob, min_depth=depth, max_depth=depth, path_prefix=rebased)
    row = df[df["path"] == rebased]
    if row.empty:
        return none
    r = row.iloc[0]
    kind = str(r["kind"])
    # a file is one object whatever its `n_desc` says (0 or 1, by scan source —
    # see memory `n_desc-semantics-vary-by-scan-source`)
    objects = 1 if kind == "file" else int(r["n_desc"] or 0) + 1
    return {"bytes": int(r["size"]), "objects": objects, "kind": kind}


def size_fn(uri: str) -> tuple[int, int]:
    """``(bytes, objects)`` for ``uri`` (see :func:`describe`) — the engine's
    ``SizeFn``."""
    d = describe(uri)
    return d["bytes"], d["objects"]


def delete_fn(uri: str) -> None:
    from disk_tree.backends import backend_for

    backend_for(uri).delete(uri)


def restore_fn(uri: str) -> int:
    """Undo a delete of ``uri`` through its backend (spec CP5); objects restored."""
    from disk_tree.backends import backend_for

    return backend_for(uri).restore(uri)
