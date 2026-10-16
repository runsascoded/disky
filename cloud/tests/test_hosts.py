"""`dt-cloud hosts resolve`: the ingest's capture host → store lookup (specs/hosts-registry.md)."""
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from dt_cloud.hosts import cli

DDL = """
CREATE TABLE hosts (id TEXT PRIMARY KEY, store TEXT NOT NULL UNIQUE, label TEXT NOT NULL, home TEXT, root TEXT, mark TEXT,
  created_at TEXT NOT NULL DEFAULT '2026-10-16T00:00:00Z', created_by TEXT, revoked_at TEXT);
INSERT INTO hosts (id, store, label) VALUES ('m3', 'primary', 'm3'), ('nas', 'nas', 'NAS');
INSERT INTO hosts (id, store, label, revoked_at) VALUES ('old', 'old', 'Old', '2026-10-16T01:00:00Z');
"""


def run(db: Path, host: str) -> tuple[int, str, str]:
    r = CliRunner().invoke(cli, ["resolve", "-s", str(db), host])
    return r.exit_code, r.stdout, r.stderr


def test_resolve(tmp_path: Path):
    db = tmp_path / "d1.sqlite"
    sqlite3.connect(db).executescript(DDL)
    assert [run(db, h) for h in ["m3", "nas", "m1", "old"]] == [
        (0, "primary\n", ""),
        (0, "nas\n", ""),
        (3, "", "hosts: no host 'm1'\n"),
        (3, "", "hosts: host 'old' was disconnected at 2026-10-16T01:00:00Z\n"),
    ]


def test_resolve_without_the_table(tmp_path: Path):
    db = tmp_path / "d1.sqlite"
    sqlite3.connect(db).execute("CREATE TABLE other (x)")
    assert run(db, "m3") == (4, "", "hosts: this D1 has no `hosts` table\n")
