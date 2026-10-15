"""`dt-cloud d1-schema`: the branch's D1 schema as one current-state file (`site/schema.sql`), and the checks
that keep it honest (specs/done/d1-migrations.md).

A schema is normalized by loading it into an empty SQLite database (foreign keys ON, as D1 enforces them) and
dumping `sqlite_master`: every object's stored `CREATE` text, `;`-terminated, tables first (parents before the
tables whose foreign keys reference them, else by name), then views, indexes and triggers (by table, then name).
SQLite's own objects (`sqlite_*`, autoindexes), D1's (`_cf_*`) and wrangler's `d1_migrations` are left out. The
dump of a loaded dump is itself (`schema.sql` is in that canonical form), so two schemas are equal exactly when
their dumps are.

Sources: `.sql` files, replayed in order (a schema, or a schema then pending recipes); a SQLite database file;
or a live D1, read-only (`wrangler d1 execute <db> --remote --json`)."""
from __future__ import annotations

import difflib
import heapq
import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Sequence
from functools import partial
from pathlib import Path

from click import Path as ClickPath, argument, echo, group, option

err = partial(echo, err=True)  # click's, so a CliRunner captures it

HEADER = (
    "-- The D1 schema: this branch's current state (specs/done/d1-migrations.md). A new database is created from it\n"
    "-- (`wrangler d1 execute <db> --remote --file site/schema.sql`); a change lands as a recipe in `site/migrations/`\n"
    "-- plus this file, regenerated with `dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql`.\n"
    "-- Canonical form (`dt-cloud d1-schema dump`): edit through a recipe, not by hand.\n"
)
OBJECTS_SQL = "SELECT type, name, tbl_name, sql FROM sqlite_master"
_TYPE_ORDER = {"table": 0, "view": 1, "index": 2, "trigger": 3}


def _ours(name: str, sql: str | None) -> bool:
    """An object the schema declares: not SQLite's (`sqlite_*`, autoindexes have no `sql`), D1's (`_cf_*`) or
    wrangler's migration bookkeeping."""
    return sql is not None and not name.startswith(("sqlite_", "_cf_")) and name != "d1_migrations"


def connect() -> sqlite3.Connection:
    """An empty in-memory SQLite database with foreign keys ON (as D1 enforces them)."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def replay(sqls: Iterable[str]) -> sqlite3.Connection:
    """A fresh database with each script run in order."""
    con = connect()
    for sql in sqls:
        con.executescript(sql)
    return con


def from_rows(rows: Iterable[dict]) -> sqlite3.Connection:
    """A fresh database holding the schema objects in `sqlite_master` rows (`type`, `name`, `sql`) read from
    elsewhere (a live D1): tables first, so every index, view and trigger finds its table."""
    objs = sorted((r for r in rows if _ours(r["name"], r["sql"])), key=lambda r: (_TYPE_ORDER[r["type"]], r["name"]))
    return replay(f"{r['sql']};" for r in objs)


def _tables_in_fk_order(con: sqlite3.Connection, tables: Sequence[str]) -> list[str]:
    """`tables`, each after the tables its foreign keys reference (self-references aside), ties by name."""
    parents = {
        t: {r[2] for r in con.execute("SELECT * FROM pragma_foreign_key_list(?)", (t,))} & set(tables) - {t}
        for t in tables
    }
    children: dict[str, list[str]] = {t: [] for t in tables}
    for t, ps in parents.items():
        for p in ps:
            children[p].append(t)
    waiting = {t: len(ps) for t, ps in parents.items()}
    ready = [t for t, n in waiting.items() if n == 0]
    heapq.heapify(ready)
    out = []
    while ready:
        t = heapq.heappop(ready)
        out.append(t)
        for c in children[t]:
            waiting[c] -= 1
            if waiting[c] == 0:
                heapq.heappush(ready, c)
    if len(out) != len(tables):
        cycle = sorted(t for t, n in waiting.items() if n)
        raise ValueError(f"foreign-key cycle among tables {cycle}")
    return out


def dump(con: sqlite3.Connection) -> str:
    """The canonical DDL of `con`'s schema (see the module doc)."""
    objs = [r for r in con.execute(OBJECTS_SQL) if _ours(r[1], r[3])]
    sql = {(t, n): s for t, n, _, s in objs}
    tables = _tables_in_fk_order(con, sorted(n for t, n, _, _ in objs if t == "table"))
    rest = sorted(((t, n, tbl) for t, n, tbl, _ in objs if t != "table"), key=lambda o: (_TYPE_ORDER[o[0]], o[2], o[1]))
    stmts = [_bare(sql["table", t]) for t in tables] + [_bare(sql[t, n]) for t, n, _ in rest]
    return HEADER + "".join(f"\n{s};\n" for s in stmts)


def _bare(stmt: str) -> str:
    """`stmt` without `--` comments or trailing whitespace: D1 stores a statement's text with its comments stripped (and
    the spaces before them kept), SQLite verbatim, so only this form compares equal across the two. (Our DDL has no `--`
    inside a string literal.)"""
    return "\n".join(re.sub(r"--[^\n]*", "", ln).rstrip() for ln in stmt.split("\n"))


def is_sqlite_file(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def load(paths: Sequence[Path]) -> sqlite3.Connection:
    """One SQLite database file (its schema, copied read-only into memory), or `.sql` scripts replayed in order."""
    if len(paths) == 1 and is_sqlite_file(paths[0]):
        src = sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True)
        try:
            rows = [dict(zip(("type", "name", "tbl_name", "sql"), r)) for r in src.execute(OBJECTS_SQL)]
        finally:
            src.close()
        return from_rows(rows)
    if any(is_sqlite_file(p) for p in paths):
        raise ValueError("a SQLite database file is a source on its own, not one of several")
    return replay(p.read_text() for p in paths)


def live_rows(db: str, *, config: Path | None = None, site: Path) -> list[dict]:
    """`sqlite_master` of a live D1, read-only (`wrangler d1 execute <db> --remote --json`, run from `site`)."""
    cmd = ["pnpm", "exec", "wrangler", "d1", "execute", db, "--remote", "--json", "--command", OBJECTS_SQL]
    if config is not None:
        cmd += ["-c", str(config)]
    out = subprocess.run(cmd, cwd=site, check=True, capture_output=True, text=True).stdout
    (res,) = json.loads(out)
    return res["results"]


def site_dir() -> Path:
    from .index_footer import _site_dir

    return _site_dir()


def schema_file() -> Path:
    """The branch's schema file, `site/schema.sql`: of the repo checkout this module runs from, else of the
    nearest `site/` up from the cwd (an installed package)."""
    site = Path(__file__).resolve().parents[3] / "site"
    return (site if (site / "functions").is_dir() else site_dir()) / "schema.sql"


def table_ddl(tables: Sequence[str], text: str | None = None) -> str:
    """The statements of the schema `text` (default: the schema file's) that create `tables`, with their indexes
    and triggers, in dump order: what a local SQLite sink runs to hold just those tables."""
    con = replay([text if text is not None else schema_file().read_text()])
    rows = [dict(zip(("type", "name", "tbl_name", "sql"), r)) for r in con.execute(OBJECTS_SQL) if r[2] in tables]
    missing = sorted(set(tables) - {r["name"] for r in rows if r["type"] == "table"})
    if missing:
        raise ValueError(f"no such tables in the schema: {missing}")
    return dump(from_rows(rows)).removeprefix(HEADER).lstrip("\n")


def pending(site: Path) -> list[Path]:
    """The recipes not yet applied to every database of this branch: the files in `site/migrations/`, in name
    order (a date-prefixed slug orders two pending at once)."""
    return sorted((site / "migrations").glob("*.sql"))


def _git(site: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=site, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def pre_image(site: Path, recipes: Sequence[Path]) -> str:
    """`schema.sql` as it was before the earliest of `recipes` arrived on this branch: at the first parent of the
    commit that added it, following first parents (so a recipe merged in from `cloud` dates from the merge, and the
    pre-image is this branch's own schema); an uncommitted recipe's is `HEAD`'s. Needs the branch's history (CI:
    `fetch-depth: 0`)."""
    root = Path(_git(site, "rev-parse", "--show-toplevel").strip())
    if _git(site, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise RuntimeError("a shallow clone has no recipe's pre-image: fetch the history (`fetch-depth: 0`)")
    revs = []
    for r in recipes:
        added = _git(site, "log", "--first-parent", "--diff-merges=first-parent", "--diff-filter=A", "--no-patch", "--format=%H", "-1", "HEAD", "--", str(r.resolve())).strip()
        revs.append(f"{added}^1" if added else "HEAD")
    # The earliest: the one every other is a descendant of (each later recipe landed on top of it).
    base = revs[0]
    for rev in revs[1:]:
        if subprocess.run(["git", "merge-base", "--is-ancestor", rev, base], cwd=site).returncode == 0:
            base = rev
    schema = (site / "schema.sql").resolve().relative_to(root).as_posix()
    return _git(site, "show", f"{base}:{schema}")


def diff(expected: str, actual: str, expected_name: str, actual_name: str) -> str:
    return "".join(difflib.unified_diff(
        expected.splitlines(keepends=True), actual.splitlines(keepends=True), expected_name, actual_name,
    ))


@group("d1-schema")
def cli() -> None:
    """The D1 schema file (`site/schema.sql`): dump a normalized schema, check one against another."""


@cli.command("dump")
@option("-o", "--out", type=ClickPath(dir_okay=False, path_type=Path), default=None, help="Write here (default stdout)")
@argument("sources", nargs=-1, required=True, type=ClickPath(exists=True, dir_okay=False, path_type=Path))
def dump_cmd(out: Path | None, sources: tuple[Path, ...]) -> None:
    """The canonical DDL of SOURCES: `.sql` files replayed in order into an empty SQLite (foreign keys ON), or one
    SQLite database file. A schema change: `dump site/schema.sql site/migrations/<slug>.sql -o site/schema.sql`."""
    text = dump(load(sources))
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text)


@cli.command("check")
@option("-c", "--config", type=ClickPath(dir_okay=False, path_type=Path), default=None, help="With -d: the deployment's wrangler config (e.g. `wrangler.r2.toml`), relative to site/")
@option("-d", "--d1", "d1", default=None, help="Compare with this live D1's schema (read-only `wrangler d1 execute --remote`, run from site/)")
@option("-p", "--pending", "with_pending", is_flag=True, help="Compare with the pre-image of the pending recipes (`site/migrations/*.sql`, from git history) plus the recipes, in order")
@option("-s", "--schema", type=ClickPath(exists=True, dir_okay=False, path_type=Path), default=None, help="The schema file (default site/schema.sql)")
@argument("sources", nargs=-1, type=ClickPath(exists=True, dir_okay=False, path_type=Path))
def check_cmd(config: Path | None, d1: str | None, with_pending: bool, schema: Path | None, sources: tuple[Path, ...]) -> None:
    """Check the schema file: it is canonical (loads cleanly with foreign keys ON and dumps to itself), and equals
    the schema of SOURCES (`.sql` files replayed, or a SQLite file), of a live D1 (-d), or of the pending recipes
    applied to its pre-image (-p). Prints a unified diff and exits 1 on any difference."""
    schema = schema or schema_file()
    site = schema.resolve().parent
    text = schema.read_text()
    if sum([bool(sources), d1 is not None, with_pending]) > 1:
        raise SystemExit("d1-schema check: one of SOURCES, -d, -p")
    failed = False
    for problem in problems(text):
        err(f"{schema}: {problem}")
        failed = True
    if sources:
        other, name = dump(load(sources)), " + ".join(map(str, sources))
    elif d1 is not None:
        other, name = dump(from_rows(live_rows(d1, config=config, site=schema_file().parent))), f"D1 {d1}"
    elif with_pending:
        recipes = pending(site)
        if not recipes:
            err("d1-schema check: no pending recipes")
            other, name = None, ""
        else:
            other = dump(replay([pre_image(site, recipes), *(r.read_text() for r in recipes)]))
            name = "pre-image + " + " + ".join(r.name for r in recipes)
    else:
        other, name = None, ""
    if other is not None:
        if other == text:
            err(f"{schema} == {name}")
        else:
            sys.stdout.write(diff(text, other, str(schema), name))
            failed = True
    if failed:
        raise SystemExit(1)


def problems(text: str) -> list[str]:
    """What keeps `text` from being a canonical schema file: it fails to load (foreign keys ON), a foreign key
    names a missing table, or it is not its own dump."""
    try:
        con = replay([text])
    except sqlite3.Error as e:
        return [f"does not load: {e}"]
    out = [
        f"{t}: foreign key references missing table {p}"
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        for p in {r[2] for r in con.execute("SELECT * FROM pragma_foreign_key_list(?)", (t,))}
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (p,)).fetchone()
    ]
    if dump(con) != text:
        out.append("not canonical (regenerate with `dt-cloud d1-schema dump`)")
    return out
