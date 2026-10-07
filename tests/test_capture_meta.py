"""A capture's physical capacity survives snapshot publication without using listing totals."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from disk_tree.cli.base import cli
from disk_tree.cli.capture import capture_space


MANIFEST = {
    'format': 'disk-tree-capture',
    'version': 1,
    'time': '2026-10-05T15:41:33Z',
    'container': {'device': 'disk3', 'capacity': 1000, 'used': 875, 'free': 125},
}
SPACE = {'capacity': 1000, 'used': 875, 'free': 125, 'device': 'disk3', 'captured_at': '2026-10-05T15:41:33Z'}


def test_physical_space() -> None:
    assert capture_space(MANIFEST) == SPACE


@pytest.mark.parametrize('container', [
    {'device': 'disk3', 'capacity': 0, 'used': 0, 'free': 0},
    {'device': 'disk3', 'capacity': 1000, 'used': 875, 'free': 200},
    {'device': 'disk3', 'capacity': 1000, 'used': -1, 'free': 1001},
    {'device': 'disk3', 'capacity': 1000, 'used': 875.0, 'free': 125},
])
def test_invalid_measurement(container: dict) -> None:
    with pytest.raises(ValueError, match=r'^container capacity must be positive integer bytes equal to used \+ free$'):
        capture_space({**MANIFEST, 'container': container})


def test_publish_and_clear_annotation(tmp_path: Path) -> None:
    manifest = tmp_path / '_SUCCESS.json'
    snapshot = tmp_path / 'meta.json'
    manifest.write_text(json.dumps(MANIFEST))
    original = {'asof': '2026-10-05', 'total_bytes': 1500, 'total_objects': 3}
    snapshot.write_text(json.dumps(original))
    runner = CliRunner()
    result = runner.invoke(cli, ['capture-meta', str(manifest), str(snapshot)])
    assert result.exit_code == 0, result.exception
    assert result.output == f'{snapshot}\n'
    assert json.loads(snapshot.read_text()) == {**original, 'disk_space': SPACE}
    manifest.write_text(json.dumps({k: v for k, v in MANIFEST.items() if k != 'container'}))
    result = runner.invoke(cli, ['capture-meta', str(manifest), str(snapshot)])
    assert result.exit_code == 0, result.exception
    assert result.output == f'{snapshot}\n'
    assert json.loads(snapshot.read_text()) == original


def test_invalid_manifest_does_not_modify_snapshot(tmp_path: Path) -> None:
    manifest = tmp_path / '_SUCCESS.json'
    snapshot = tmp_path / 'meta.json'
    manifest.write_text('{}')
    snapshot.write_text('{"total_bytes": 1500}\n')
    result = CliRunner().invoke(cli, ['capture-meta', str(manifest), str(snapshot)])
    assert result.exit_code == 1
    assert str(result.exception) == 'expected a version 1 disk-tree-capture manifest'
    assert snapshot.read_text() == '{"total_bytes": 1500}\n'
