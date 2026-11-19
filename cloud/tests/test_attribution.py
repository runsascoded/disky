"""Behavior tests for the attribution roster and its path signals (pure logic)."""

import datetime as dt
from pathlib import Path

import pytest

from dt_cloud.identity import load_identities, user_dir_lead
from dt_cloud.signals import AttributionRow, user_dir_rows
from dt_cloud.usernames import sanitize_username

ASOF = dt.date(2026, 7, 16)

IDENTITIES_YAML = """
users:
  ryan-williams:
    aliases: [rw]
  russell-power:
    aliases: [rpower]
prefix_owners:
  - prefix: gs://b1/datasets/
    user: data-team
user_dirs: [users/*/, home/staff/*/]
"""


@pytest.fixture
def identities(tmp_path: Path):
    path = tmp_path / "identities.yaml"
    path.write_text(IDENTITIES_YAML)
    return load_identities(path)


def test_sanitize_username():
    assert sanitize_username("Alice.Example@example.test") == "alice-example"
    assert sanitize_username("ryan.williams") == "ryan-williams"
    assert sanitize_username("rw") == "rw"
    with pytest.raises(RuntimeError):
        sanitize_username("@@..")


def test_resolve_via_alias_sanitization_and_fallthrough(identities):
    # Alias hit, direct sanitize-to-canonical hit, and the fallthrough contract:
    # an unmapped spelling resolves to its own sanitized segment (attributed,
    # surfaced for curation as not-known — never dropped).
    assert identities.resolve("rw") == "ryan-williams"
    assert identities.resolve("Ryan.Williams@laptop") == "ryan-williams"
    assert identities.resolve("rpower") == "russell-power"
    assert identities.resolve("someone.new") == "someone-new"
    assert identities.known("ryan-williams")
    assert not identities.known("someone-new")


def test_alias_collision_raises(tmp_path: Path):
    path = tmp_path / "identities.yaml"
    path.write_text(
        """
users:
  ryan-williams: {aliases: [rw]}
  russell-power: {aliases: [rw]}
"""
    )
    with pytest.raises(ValueError):
        load_identities(path)


def test_user_dirs(identities, tmp_path: Path):
    assert identities.user_dirs == ("users/*/", "home/staff/*/")
    assert [user_dir_lead(p) for p in identities.user_dirs] == ["users/", "home/staff/"]
    assert user_dir_lead("*/") == ""
    for bad in ("users/*", "/users/*/", "users/*/x/", "users/a*/", "users/*/*/"):
        with pytest.raises(ValueError):
            user_dir_lead(bad)
    path = tmp_path / "bad.yaml"
    path.write_text("users: {}\nuser_dirs: [users/*]\n")
    with pytest.raises(ValueError):
        load_identities(path)
    path.write_text("users: {}\n")
    assert load_identities(path).user_dirs == ()


def test_user_dir_rows(identities):
    dirs = [("b1", "users/", "rw"), ("b1", "users/", "someone.new"), ("b1", "users/", "rw"), ("b2", "home/staff/", "rpower")]
    assert user_dir_rows(dirs, identities, ASOF) == [
        AttributionRow(prefix="gs://b1/users/rw/", user="ryan-williams", source="user-prefix", evidence=None, asof=ASOF),
        AttributionRow(prefix="gs://b1/users/someone.new/", user="someone-new", source="user-prefix", evidence=None, asof=ASOF),
        AttributionRow(prefix="gs://b2/home/staff/rpower/", user="russell-power", source="user-prefix", evidence=None, asof=ASOF),
    ]
