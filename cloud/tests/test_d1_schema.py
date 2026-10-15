"""`dt-cloud d1-schema` and the branch's D1 schema file (specs/done/d1-migrations.md): `site/schema.sql` loads
cleanly with foreign keys ON and is its own canonical dump; pending recipes lead from its pre-image to it; the dump's
order and exclusions; the `check` command's verdicts."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.cli import main
from dt_cloud.d1_schema import HEADER, dump, from_rows, load, pending, pre_image, problems, replay, schema_file, table_ddl

SCHEMA = """
CREATE TABLE z_parent (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
CREATE TABLE a_child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES z_parent(id));
CREATE INDEX a_child_parent ON a_child (parent);
CREATE VIEW names AS SELECT name FROM z_parent;
CREATE TRIGGER z_parent_gone AFTER DELETE ON z_parent BEGIN DELETE FROM a_child WHERE parent = old.id; END;
CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE _cf_KV (key TEXT PRIMARY KEY, value BLOB);
CREATE TABLE m_solo (k TEXT PRIMARY KEY);
"""
DUMP = HEADER + """
CREATE TABLE m_solo (k TEXT PRIMARY KEY);

CREATE TABLE z_parent (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);

CREATE TABLE a_child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES z_parent(id));

CREATE VIEW names AS SELECT name FROM z_parent;

CREATE INDEX a_child_parent ON a_child (parent);

CREATE TRIGGER z_parent_gone AFTER DELETE ON z_parent BEGIN DELETE FROM a_child WHERE parent = old.id; END;
"""


# ── The branch's schema file ─────────────────────────────────────────────────


def test_schema_file_loads_with_foreign_keys_on_and_is_canonical():
    text = schema_file().read_text()
    assert problems(text) == []
    con = replay([text])
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_pending_recipes_lead_from_the_pre_image_to_the_schema():
    """Each recipe in `site/migrations/` (applied to its live databases, then removed) takes the branch's previous
    schema to `schema.sql`: old `schema.sql` + the pending recipes, replayed with foreign keys ON, dump to it."""
    site = schema_file().parent
    recipes = pending(site)
    if not recipes:
        pytest.skip("no pending recipe in site/migrations/")
    assert dump(replay([pre_image(site, recipes), *(r.read_text() for r in recipes)])) == schema_file().read_text()


# ── The dump ─────────────────────────────────────────────────────────────────


def test_dump_orders_tables_by_foreign_key_then_name_and_leaves_out_bookkeeping():
    assert dump(replay([SCHEMA])) == DUMP


def test_a_dump_reloads_to_itself():
    assert dump(replay([DUMP])) == DUMP
    assert problems(DUMP) == []


def test_rows_read_from_a_live_d1_dump_as_the_replay():
    rows = [dict(zip(("type", "name", "tbl_name", "sql"), r)) for r in replay([SCHEMA]).execute("SELECT type, name, tbl_name, sql FROM sqlite_master")]
    assert dump(from_rows(reversed(rows))) == DUMP


def test_a_sqlite_file_is_a_source(tmp_path):
    db = tmp_path / "local.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO z_parent (name) VALUES ('a')")
    con.commit()
    con.close()
    assert dump(load([db])) == DUMP


def test_a_foreign_key_cycle_is_refused():
    con = replay(["CREATE TABLE a (b_id REFERENCES b(id), id INTEGER PRIMARY KEY); CREATE TABLE b (a_id REFERENCES a(id), id INTEGER PRIMARY KEY);"])
    with pytest.raises(ValueError, match=r"^foreign-key cycle among tables \['a', 'b'\]$"):
        dump(con)


def test_problems_of_a_schema_file():
    dangling = dump(replay(["CREATE TABLE c (p_id INTEGER REFERENCES p(id))"]))
    assert [
        problems(dangling),
        problems(DUMP.replace("\n\nCREATE VIEW", "\nCREATE VIEW")),
        problems("CREATE TABLE t (x TEXT); CREATE TABLE t (y TEXT);"),
    ] == [
        ["c: foreign key references missing table p"],
        ["not canonical (regenerate with `dt-cloud d1-schema dump`)"],
        ["does not load: table t already exists"],
    ]


def test_table_ddl_is_just_those_tables_and_their_indexes():
    assert table_ddl(["a_child", "m_solo"], SCHEMA) == (
        "CREATE TABLE a_child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES z_parent(id));\n"
        "\n"
        "CREATE TABLE m_solo (k TEXT PRIMARY KEY);\n"
        "\n"
        "CREATE INDEX a_child_parent ON a_child (parent);\n"
    )
    with pytest.raises(ValueError, match=r"^no such tables in the schema: \['nope'\]$"):
        table_ddl(["m_solo", "nope"], SCHEMA)


# ── `dt-cloud d1-schema` ─────────────────────────────────────────────────────


def test_dump_and_check_commands(tmp_path):
    src = tmp_path / "src.sql"
    src.write_text(SCHEMA)
    schema = tmp_path / "schema.sql"
    r = CliRunner().invoke(main, ["d1-schema", "dump", "-o", str(schema), str(src)])
    assert (r.exit_code, schema.read_text()) == (0, DUMP)
    same = CliRunner().invoke(main, ["d1-schema", "check", "-s", str(schema), str(src)])
    assert (same.exit_code, same.stdout, same.stderr) == (0, "", f"{schema} == {src}\n")
    recipe = tmp_path / "recipe.sql"
    recipe.write_text("ALTER TABLE m_solo ADD COLUMN v TEXT;")
    other = CliRunner().invoke(main, ["d1-schema", "check", "-s", str(schema), str(src), str(recipe)])
    assert (other.exit_code, other.stdout.splitlines(), other.stderr) == (1, [
        f"--- {schema}",
        f"+++ {src} + {recipe}",
        "@@ -3,7 +3,7 @@",
        " -- plus this file, regenerated with `dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql`.",
        " -- Canonical form (`dt-cloud d1-schema dump`): edit through a recipe, not by hand.",
        " ",
        "-CREATE TABLE m_solo (k TEXT PRIMARY KEY);",
        "+CREATE TABLE m_solo (k TEXT PRIMARY KEY, v TEXT);",
        " ",
        " CREATE TABLE z_parent (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);",
        " ",
    ], "")
    schema.write_text(SCHEMA)
    bad = CliRunner().invoke(main, ["d1-schema", "check", "-s", str(schema)])
    assert (bad.exit_code, bad.stderr) == (1, f"{schema}: not canonical (regenerate with `dt-cloud d1-schema dump`)\n")


# ── A recipe's pre-image, from git history ───────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "init.defaultBranch=dep", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


def _commit(repo: Path, msg: str, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        if text is None:
            _git(repo, "rm", "-q", name)
        else:
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
            _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)


def test_pre_image_follows_first_parents(tmp_path):
    """The schema before a recipe arrived on this branch: the parent of the commit that added it, or (merged in
    from `cloud`) the merge's first parent, i.e. this branch's own schema; an uncommitted recipe's is `HEAD`'s."""
    repo, site = tmp_path, tmp_path / "site"
    _git(repo, "init", "-q")
    _commit(repo, "base", {"site/schema.sql": "base\n"})
    _git(repo, "checkout", "-q", "-b", "cloud")
    _commit(repo, "cloud: a recipe", {"site/schema.sql": "cloud 2\n", "site/migrations/2026-10-01_a.sql": "a\n"})
    _git(repo, "checkout", "-q", "dep")
    _commit(repo, "dep: its own schema", {"site/schema.sql": "dep 1\n"})
    with pytest.raises(subprocess.CalledProcessError):
        _git(repo, "merge", "-q", "--no-edit", "cloud")
    (site / "schema.sql").write_text("dep 2\n")
    _git(repo, "add", "site/schema.sql")
    _git(repo, "commit", "-q", "--no-edit")
    a = site / "migrations/2026-10-01_a.sql"
    assert pending(site) == [a]
    assert pre_image(site, [a]) == "dep 1\n"
    _git(repo, "checkout", "-q", "cloud")
    assert pre_image(site, [a]) == "base\n"
    _git(repo, "checkout", "-q", "dep")
    _commit(repo, "dep: another recipe", {"site/schema.sql": "dep 3\n", "site/migrations/2026-10-02_b.sql": "b\n"})
    b = site / "migrations/2026-10-02_b.sql"
    c = site / "migrations/2026-10-03_c.sql"
    c.write_text("c\n")
    assert [pre_image(site, [b]), pre_image(site, [c]), pre_image(site, [a, b, c])] == ["dep 2\n", "dep 3\n", "dep 1\n"]
