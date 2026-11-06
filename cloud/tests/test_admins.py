"""`dt-cloud admins check`: the admin list's GitHub handles against an org's owners, as a read-only drift report."""
from __future__ import annotations

import json
import subprocess
from inspect import cleandoc
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import admins
from dt_cloud.admins import EXIT_UNAVAILABLE, HANDLES_SQL, Drift, HandlesUnavailable, check_cmd, drift, list_handles, org_owners, render
from dt_cloud.cli import main


def test_drift_both_directions_case_insensitive() -> None:
    d = drift(["Zed", "ada", "Bo"], ["ADA", "carl", "bo"], no_handle=2)
    assert d == Drift(
        owners=["ada", "Bo", "Zed"],
        admins=["ADA", "bo", "carl"],
        owner_not_admin=["Zed"],
        admin_not_owner=["carl"],
        no_handle=2,
    )
    assert d.in_sync is False


def test_drift_expected_differences_are_reported_not_drift() -> None:
    d = drift(["Zed", "ada", "Yi"], ["ada", "carl", "dee"], expected=["yi", "CARL", "ada", "nobody"])
    assert d == Drift(
        owners=["ada", "Yi", "Zed"],
        admins=["ada", "carl", "dee"],
        owner_not_admin=["Zed"],
        admin_not_owner=["dee"],
        no_handle=0,
        expected_owner_not_admin=["Yi"],
        expected_admin_not_owner=["carl"],
        expected_unused=["ada", "nobody"],
    )
    assert drift(["ada", "Yi"], ["ada"], expected=["yi"]).in_sync is True
    assert render("my-org", d) == [
        "my-org: 3 owners; admin list: 3 handles, 0 rows without one",
        "owner, not an admin: Zed",
        "admin, not an owner: dee",
        "expected (-x) owner, not an admin: Yi",
        "expected (-x) admin, not an owner: carl",
        "expected (-x), but no different: ada, nobody",
        "drift",
    ]


def test_drift_in_sync() -> None:
    d = drift(["ada", "Bo"], ["bo", "Ada"])
    assert (d.owner_not_admin, d.admin_not_owner, d.no_handle, d.in_sync) == ([], [], 0, True)


def test_render() -> None:
    assert render("my-org", drift(["Zed", "ada"], ["ada", "carl"], no_handle=1)) == [
        "my-org: 2 owners; admin list: 2 handles, 1 rows without one",
        "owner, not an admin: Zed",
        "admin, not an owner: carl",
        "drift",
    ]
    assert render("my-org", drift(["ada"], ["ada"])) == [
        "my-org: 1 owners; admin list: 1 handles, 0 rows without one",
        "owner, not an admin: (none)",
        "admin, not an owner: (none)",
        "in sync",
    ]


def _fake_run(calls: list, stdout: str):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return run


def test_org_owners_reads_every_page_of_the_owner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(admins.subprocess, "run", _fake_run(calls, "ada\nBo\n\n"))
    assert org_owners("my-org") == ["ada", "Bo"]
    assert calls == [(
        ["gh", "api", "--paginate", "orgs/my-org/members?role=admin", "--jq", ".[].login"],
        {"check": True, "capture_output": True, "text": True},
    )]


def test_list_handles_reads_handles_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list = []
    out = json.dumps([{"results": [{"github": None, "n": 3}, {"github": "ada", "n": 2}, {"github": "carl", "n": 1}], "success": True}])
    monkeypatch.setattr(admins.subprocess, "run", _fake_run(calls, out))
    assert list_handles("my-db", config=Path("/x/wrangler.toml"), site=tmp_path) == (["ada", "carl"], 3)
    assert calls == [(
        ["pnpm", "exec", "wrangler", "d1", "execute", "my-db", "--remote", "--json", "--command", HANDLES_SQL, "-c", "/x/wrangler.toml"],
        {"cwd": tmp_path, "check": True, "capture_output": True, "text": True},
    )]
    assert HANDLES_SQL == "SELECT github, COUNT(*) AS n FROM admin_emails GROUP BY github ORDER BY github"


def _wrangler_fails(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", stderr: str = "") -> None:
    def run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, output=stdout, stderr=stderr)

    monkeypatch.setattr(admins.subprocess, "run", run)


NO_COLUMN = "`admin_emails` has no `github` column in this database (its recipe, `site/migrations/admin-list.sql`, is unapplied)"
NO_TABLE = "this database has no `admin_emails` table (its recipe, `site/migrations/admin-list.sql`, is unapplied)"


@pytest.mark.parametrize("failed, reason", [
    ({"stderr": "✘ [ERROR] no such column: github: SQLITE_ERROR"}, NO_COLUMN),
    ({"stdout": json.dumps({"error": {"text": "no such column: github: SQLITE_ERROR"}})}, NO_COLUMN),
    ({"stderr": "✘ [ERROR] no such table: admin_emails: SQLITE_ERROR"}, NO_TABLE),
])
def test_list_handles_says_why_the_handles_are_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed: dict, reason: str) -> None:
    _wrangler_fails(monkeypatch, **failed)
    with pytest.raises(HandlesUnavailable) as e:
        list_handles("my-db", site=tmp_path)
    assert str(e.value) == reason


def test_list_handles_any_other_failure_still_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _wrangler_fails(monkeypatch, stderr="✘ [ERROR] Authentication error")
    with pytest.raises(subprocess.CalledProcessError):
        list_handles("my-db", site=tmp_path)


def _cli_no_column(monkeypatch: pytest.MonkeyPatch, *args: str):
    _wrangler_fails(monkeypatch, stderr="✘ [ERROR] no such column: github: SQLITE_ERROR")

    def no_owners(org: str) -> list[str]:
        raise AssertionError("the org isn't asked when there is nothing to compare")

    monkeypatch.setattr(admins, "org_owners", no_owners)
    return CliRunner().invoke(main, ["admins", "check", "-d", "my-db", "-o", "my-org", *args])


def test_check_without_the_column_reports_unavailable_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _cli_no_column(monkeypatch)
    assert (EXIT_UNAVAILABLE, r.exit_code, r.output.split("\n")) == (3, 3, [f"my-org: the handle comparison isn't available: {NO_COLUMN}", ""])


def test_check_without_the_column_json(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _cli_no_column(monkeypatch, "-j")
    assert (r.exit_code, json.loads(r.output)) == (3, {"org": "my-org", "available": False, "reason": NO_COLUMN, "in_sync": None})


def test_check_help_documents_its_exit_statuses() -> None:
    assert cleandoc(check_cmd.help) == (
        "Compare the admin list's GitHub handles with ORG's owners. Reads only; changes nothing.\n\n"
        "Exit status: 0 in sync; 1 drift; 3 the comparison isn't available (the database has no `github` column or no "
        "`admin_emails` table: its recipe is unapplied), which says nothing about drift."
    )


def _cli(monkeypatch: pytest.MonkeyPatch, owners: list[str], handles: list[str], no_handle: int, *args: str):
    monkeypatch.setattr(admins, "org_owners", lambda org: owners)
    monkeypatch.setattr(admins, "list_handles", lambda db, *, config, site: (handles, no_handle))
    return CliRunner().invoke(main, ["admins", "check", "-d", "my-db", "-o", "my-org", *args])


def test_check_drift_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _cli(monkeypatch, ["Zed", "ada"], ["ada", "carl"], 1)
    assert (r.exit_code, r.output.split("\n")) == (1, [
        "my-org: 2 owners; admin list: 2 handles, 1 rows without one",
        "owner, not an admin: Zed",
        "admin, not an owner: carl",
        "drift",
        "",
    ])


def test_check_in_sync_exits_0_json(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _cli(monkeypatch, ["ada", "Bo"], ["bo", "ada"], 0, "-j")
    assert (r.exit_code, json.loads(r.output)) == (0, {
        "org": "my-org",
        "owners": ["ada", "Bo"],
        "admins": ["ada", "bo"],
        "owner_not_admin": [],
        "admin_not_owner": [],
        "no_handle": 0,
        "expected_owner_not_admin": [],
        "expected_admin_not_owner": [],
        "expected_unused": [],
        "in_sync": True,
    })


def test_check_expected_difference_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _cli(monkeypatch, ["ada", "Yi"], ["ada"], 0, "-x", "@yi")
    assert (r.exit_code, r.output.split("\n")) == (0, [
        "my-org: 2 owners; admin list: 1 handles, 0 rows without one",
        "owner, not an admin: (none)",
        "admin, not an owner: (none)",
        "expected (-x) owner, not an admin: Yi",
        "in sync",
        "",
    ])
