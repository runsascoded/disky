"""`dt-cloud admins`: a deployment's admin list (`admin_emails` in its D1; `/admin`, `/api/admins`) against a GitHub
org's owners.

`check` is a read-only drift report, never a sync: the handles recorded on the list (`admin_emails.github`) beside the
org's owners (`gh api orgs/<org>/members?role=admin`), as who is an owner but not an admin, and who is an admin but not
an owner. Handles compare case-insensitively (GitHub logins are). Rows with no handle can't be compared and are
counted. Only handles are read from the database: no address is selected or printed. A difference that is meant
(`-x HANDLE`: an owner deliberately left off the list, an admin who isn't an owner) is reported on its own line and
isn't drift; one named there that no longer differs is said so, for the caller to drop.

A database without the handles (no `github` column or no `admin_emails` table: the `admin-list` recipe unapplied) has
nothing to compare, which is its own answer (`EXIT_UNAVAILABLE`), neither drift nor in sync.

GitHub lists an org's owners to its members only: run it as a member of the org (`gh auth status`), or the owners
come back incomplete."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

from click import Path as ClickPath, echo, group, option

err = partial(print, file=sys.stderr)

HANDLES_SQL = "SELECT github, COUNT(*) AS n FROM admin_emails GROUP BY github ORDER BY github"

EXIT_DRIFT = 1
# Not click's 2 (a usage error).
EXIT_UNAVAILABLE = 3


class HandlesUnavailable(Exception):
    """The database has no handles to compare; the message says why."""


# What the database answers when the recipe is unapplied, and what that means.
UNAVAILABLE = (
    (re.compile(r"no such column: github"), "`admin_emails` has no `github` column in this database (its recipe, `site/migrations/admin-list.sql`, is unapplied)"),
    (re.compile(r"no such table: admin_emails"), "this database has no `admin_emails` table (its recipe, `site/migrations/admin-list.sql`, is unapplied)"),
)


@dataclass(frozen=True)
class Drift:
    """The list against the org: both directions, in the org's / the list's spelling, sorted case-insensitively."""
    owners: list[str]
    admins: list[str]
    owner_not_admin: list[str]
    admin_not_owner: list[str]
    no_handle: int
    # The differences named as intended (`-x`), by direction, and the names given there that don't differ.
    expected_owner_not_admin: list[str] = field(default_factory=list)
    expected_admin_not_owner: list[str] = field(default_factory=list)
    expected_unused: list[str] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not self.owner_not_admin and not self.admin_not_owner


def _sorted(handles: Sequence[str]) -> list[str]:
    return sorted(handles, key=lambda h: (h.lower(), h))


def drift(
    owners: Sequence[str],
    admins: Sequence[str],
    no_handle: int = 0,
    expected: Sequence[str] = (),
) -> Drift:
    """`owners` (the org's) against `admins` (the list's handles, one per person however many addresses they have);
    `expected` names the handles whose difference, either way, is meant."""
    own = {h.lower(): h for h in owners}
    adm = {h.lower(): h for h in admins}
    exp = {h.lower(): h for h in expected}
    only_own = {k: h for k, h in own.items() if k not in adm}
    only_adm = {k: h for k, h in adm.items() if k not in own}
    return Drift(
        owners=_sorted(list(own.values())),
        admins=_sorted(list(adm.values())),
        owner_not_admin=_sorted([h for k, h in only_own.items() if k not in exp]),
        admin_not_owner=_sorted([h for k, h in only_adm.items() if k not in exp]),
        no_handle=no_handle,
        expected_owner_not_admin=_sorted([h for k, h in only_own.items() if k in exp]),
        expected_admin_not_owner=_sorted([h for k, h in only_adm.items() if k in exp]),
        expected_unused=_sorted([h for k, h in exp.items() if k not in only_own and k not in only_adm]),
    )


def org_owners(org: str) -> list[str]:
    """The org's owners' logins (`gh api orgs/<org>/members?role=admin`, every page)."""
    out = subprocess.run(
        ["gh", "api", "--paginate", f"orgs/{org}/members?role=admin", "--jq", ".[].login"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def list_handles(
    db: str,
    *,
    config: Path | None = None,
    site: Path,
) -> tuple[list[str], int]:
    """The list's handles and its count of rows without one, read-only (`wrangler d1 execute <db> --remote --json`,
    run from `site`). `HandlesUnavailable` where the database hasn't them; any other failure raises as it is."""
    cmd = ["pnpm", "exec", "wrangler", "d1", "execute", db, "--remote", "--json", "--command", HANDLES_SQL]
    if config is not None:
        cmd += ["-c", str(config)]
    try:
        out = subprocess.run(cmd, cwd=site, check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as e:
        said = f"{e.stdout or ''}\n{e.stderr or ''}"
        for pattern, reason in UNAVAILABLE:
            if pattern.search(said):
                raise HandlesUnavailable(reason) from None
        raise
    (res,) = json.loads(out)
    rows = res["results"]
    return [r["github"] for r in rows if r["github"]], sum(int(r["n"]) for r in rows if not r["github"])


def render(org: str, d: Drift) -> list[str]:
    names = lambda hs: ", ".join(hs) if hs else "(none)"
    return [
        f"{org}: {len(d.owners)} owners; admin list: {len(d.admins)} handles, {d.no_handle} rows without one",
        f"owner, not an admin: {names(d.owner_not_admin)}",
        f"admin, not an owner: {names(d.admin_not_owner)}",
        *([f"expected (-x) owner, not an admin: {names(d.expected_owner_not_admin)}"] if d.expected_owner_not_admin else []),
        *([f"expected (-x) admin, not an owner: {names(d.expected_admin_not_owner)}"] if d.expected_admin_not_owner else []),
        *([f"expected (-x), but no different: {names(d.expected_unused)}"] if d.expected_unused else []),
        "in sync" if d.in_sync else "drift",
    ]


@group("admins")
def cli() -> None:
    """A deployment's admin list."""


@cli.command("check")
@option("-c", "--config", type=ClickPath(dir_okay=False, path_type=Path), default=None, help="The deployment's wrangler config (e.g. `site/wrangler.r2.toml`): a path from here, else one relative to site/")
@option("-d", "--d1", "d1", required=True, help="The deployment's D1 (read-only `wrangler d1 execute --remote`, run from site/)")
@option("-j", "--json", "as_json", is_flag=True, help="Print the report as JSON")
@option("-o", "--org", required=True, help="The GitHub org whose owners the list should mirror")
@option("-x", "--expected", multiple=True, help="A handle whose difference is meant (an owner left off the list, an admin who isn't an owner): reported, not drift; repeatable")
def check_cmd(
    config: Path | None,
    d1: str,
    as_json: bool,
    org: str,
    expected: tuple[str, ...],
) -> None:
    """Compare the admin list's GitHub handles with ORG's owners. Reads only; changes nothing.

    Exit status: 0 in sync; 1 drift; 3 the comparison isn't available (the database has no `github` column or no `admin_emails` table: its recipe is unapplied), which says nothing about drift."""
    from .d1_schema import site_dir

    site = site_dir()
    # wrangler runs in site/: a config given from here is passed absolute (else it is taken as site-relative)
    cfg = config.resolve() if config is not None and config.exists() else config
    try:
        handles, no_handle = list_handles(d1, config=cfg, site=site)
    except HandlesUnavailable as e:
        echo(json.dumps({"org": org, "available": False, "reason": str(e), "in_sync": None}) if as_json else f"{org}: the handle comparison isn't available: {e}")
        sys.exit(EXIT_UNAVAILABLE)
    d = drift(org_owners(org), handles, no_handle, [h.lstrip("@") for h in expected])
    if as_json:
        echo(json.dumps({"org": org, **asdict(d), "in_sync": d.in_sync}))
    else:
        for line in render(org, d):
            echo(line)
    if not d.in_sync:
        sys.exit(EXIT_DRIFT)
