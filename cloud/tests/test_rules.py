"""Tests for identities.yaml validation + site-JSON export (`dt-cloud rules`)."""

from pathlib import Path

import pytest

from dt_cloud.rules import check_rules, export_rules, parse_notes

YAML = """\
users:
  ryan-williams:
    aliases: [rw]
    github: ryan-williams
  russell-power:
    aliases: [rpower]  # observed as built_by
  larry-dial:
    aliases: []  # TODO confirm identity
    name: Larry D
  data-team: {}
prefix_owners:
  - prefix: gs://b1/datasets/
    user: data-team
  - prefix: gs://b-*/scratch/rw/
    user: ryan-williams  # personal scratch tree
"""


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    path = tmp_path / "identities.yaml"
    path.write_text(YAML)
    return path


def test_parse_notes_trailing_comments(yaml_path: Path):
    user_notes, prefix_notes = parse_notes(yaml_path.read_text())
    assert user_notes == {
        "russell-power": "observed as built_by",
        "larry-dial": "TODO confirm identity",
    }
    assert prefix_notes == {"gs://b-*/scratch/rw/": "personal scratch tree"}


def test_export_rules_clean(yaml_path: Path):
    payload, findings = export_rules(yaml_path)
    assert findings == []
    assert payload == {
        "users": [
            {"u": "ryan-williams", "aliases": ["rw"], "github": "ryan-williams"},
            {"u": "russell-power", "aliases": ["rpower"], "note": "observed as built_by"},
            {"u": "larry-dial", "aliases": [], "name": "Larry D", "note": "TODO confirm identity"},
            {"u": "data-team", "aliases": []},
        ],
        "prefix_owners": [
            {"prefix": "gs://b1/datasets/", "user": "data-team"},
            {
                "prefix": "gs://b-*/scratch/rw/",
                "user": "ryan-williams",
                "note": "personal scratch tree",
            },
        ],
    }


def test_check_rules_findings():
    doc = {
        "users": {
            "ryan-williams": {"aliases": ["rw", "russell-power", "ryan-williams"]},
            "russell-power": {"aliases": ["rw"]},
        },
        "prefix_owners": [
            {"prefix": "gs://b1/x/", "user": "ghost"},
            {"prefix": "gs://b1/x/"},
            {"prefix": "b1/no-scheme"},
        ],
    }
    assert sorted(check_rules(doc)) == sorted(
        [
            "alias 'russell-power' (of ryan-williams) shadows canonical user id 'russell-power'",
            "user ryan-williams: alias 'ryan-williams' is redundant (equals canonical id)",
            "alias 'rw' appears under 2 users",
            "prefix_owners gs://b1/x/: user 'ghost' not in users map",
            "prefix_owners: 'gs://b1/x/' listed 2 times",
            "prefix_owners: 'b1/no-scheme' must look like gs://bucket/path/",
        ]
    )
