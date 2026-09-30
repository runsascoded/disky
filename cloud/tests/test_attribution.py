"""Behavior tests for the attribution pipeline.

Identity resolution and path signals are pure logic; record mining runs
against real ``.artifact.json`` files in local temp dirs (fsspec treats a
local absolute path like any other URL), asserting on the on-disk JSON
contract marin's executor writes.
"""

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from dt_cloud.identity import load_identities
from dt_cloud.records import mine_record_rows
from dt_cloud.signals import AttributionRow, record_file_paths, user_prefix_rows
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
"""


@pytest.fixture
def identities(tmp_path: Path):
    path = tmp_path / "identities.yaml"
    path.write_text(IDENTITIES_YAML)
    return load_identities(path)


def test_sanitize_username_matches_rigging_rules():
    # Must stay in lockstep with rigging.provenance.username_segment in
    # marin-community/marin — that function generates the `users/<seg>/`
    # prefixes this repo attributes.
    assert sanitize_username("Russell.Power@gmail.com") == "russell-power"
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


def test_user_prefix_rows(identities):
    listing = pd.DataFrame(
        {
            "bucket": ["b1", "b1", "b1", "b2"],
            "name": [
                "users/rw/ckpt/step-10/x.bin",
                "users/rw/other/y.bin",
                "users/someone.new/z.bin",
                "checkpoints/shared/w.bin",
            ],
        }
    )
    assert user_prefix_rows(listing, identities, ASOF) == [
        AttributionRow(
            prefix="gs://b1/users/rw/",
            user="ryan-williams",
            source="user-prefix",
            evidence=None,
            asof=ASOF,
        ),
        AttributionRow(
            prefix="gs://b1/users/someone.new/",
            user="someone-new",
            source="user-prefix",
            evidence=None,
            asof=ASOF,
        ),
    ]


def test_record_file_paths():
    listing = pd.DataFrame(
        {
            "bucket": ["b1", "b1", "b1", "b2"],
            "name": [
                "a/b/.artifact.json",
                "a/b/data.bin",
                "c/.executor_info",  # legacy sidecar: no built_by, not a candidate
                ".artifact.json",
            ],
        }
    )
    assert record_file_paths(listing) == ["gs://b1/a/b/.artifact.json", "gs://b2/.artifact.json"]


def _write_record(dir_path: Path, provenance: dict | None) -> str:
    body = {"name": "step", "output_path": str(dir_path)}
    if provenance is not None:
        body["provenance"] = provenance
    path = dir_path / ".artifact.json"
    path.write_text(json.dumps(body))
    return str(path)


def test_mine_record_rows_local_files(tmp_path: Path, identities):
    owned = tmp_path / "owned"
    anonymous = tmp_path / "anonymous"
    corrupt = tmp_path / "corrupt"
    null_body = tmp_path / "null_body"
    for d in (owned, anonymous, corrupt, null_body):
        d.mkdir()
    owned_path = _write_record(owned, {"built_by": "rw", "tree_hash": "t", "base_commit": "b"})
    anonymous_path = _write_record(anonymous, None)
    corrupt_path = corrupt / ".artifact.json"
    corrupt_path.write_text("not json")
    # Pre-migration sidecars are frequently a literal JSON null (~95% of the
    # 2026-07 fleet): no signal, but must not count as a read failure either.
    null_path = null_body / ".artifact.json"
    null_path.write_text("null")

    paths = [owned_path, anonymous_path, str(corrupt_path), str(null_path)]
    rows, failed = mine_record_rows(paths, identities, ASOF, max_workers=2)

    assert rows == [
        AttributionRow(
            prefix=f"{owned}/",
            user="ryan-williams",
            source="artifact-record",
            evidence="built_by=rw",
            asof=ASOF,
        )
    ]
    assert failed == [str(corrupt_path)]
