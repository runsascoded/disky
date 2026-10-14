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


def test_vm_volume_allocation_survives_publication(tmp_path: Path) -> None:
    manifest = tmp_path / '_SUCCESS.json'
    snapshot = tmp_path / 'meta.json'
    container = {**MANIFEST['container'], 'volumes': [
        {'roles': ['Data'], 'used': 800},
        {'roles': ['VM'], 'used': 70},
        {'roles': ['VM'], 'used': 5},
    ]}
    manifest.write_text(json.dumps({**MANIFEST, 'container': container}))
    snapshot.write_text('{"total_bytes": 1500}\n')
    result = CliRunner().invoke(cli, ['capture-meta', str(manifest), str(snapshot)])
    assert (result.exit_code, result.output) == (0, f'{snapshot}\n')
    assert json.loads(snapshot.read_text()) == {'total_bytes': 1500, 'captured_at': MANIFEST['time'], 'disk_space': {**SPACE, 'vm_used': 75}}
    container['volumes'] = [{'roles': ['VM'], 'used': 0}]
    assert capture_space({**MANIFEST, 'container': container}) == {**SPACE, 'vm_used': 0}


@pytest.mark.parametrize('used', [-1, 1.5, '70', True])
def test_invalid_vm_allocation(used: object) -> None:
    container = {**MANIFEST['container'], 'volumes': [{'roles': ['VM'], 'used': used}]}
    with pytest.raises(ValueError, match=r'^VM volume usage must be nonnegative integer bytes$'):
        capture_space({**MANIFEST, 'container': container})


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
    assert json.loads(snapshot.read_text()) == {**original, 'captured_at': MANIFEST['time'], 'disk_space': SPACE}
    manifest.write_text(json.dumps({k: v for k, v in MANIFEST.items() if k != 'container'}))
    result = runner.invoke(cli, ['capture-meta', str(manifest), str(snapshot)])
    assert result.exit_code == 0, result.exception
    assert result.output == f'{snapshot}\n'
    assert json.loads(snapshot.read_text()) == {**original, 'captured_at': MANIFEST['time']}


def test_same_capture_can_be_reingested_but_a_different_capture_cannot_replace_it(tmp_path: Path) -> None:
    manifest = tmp_path / '_SUCCESS.json'
    snapshot = tmp_path / 'meta.json'
    existing = tmp_path / 'published.json'
    manifest.write_text(json.dumps(MANIFEST))
    snapshot.write_text('{"total_bytes": 1500}\n')
    runner = CliRunner()
    args = ['capture-meta', '-c', str(existing), str(manifest), str(snapshot)]
    assert runner.invoke(cli, args).exit_code == 0
    existing.write_text(snapshot.read_text())
    assert runner.invoke(cli, args).exit_code == 0
    before = snapshot.read_text()
    manifest.write_text(json.dumps({**MANIFEST, 'time': '2026-10-05T15:41:59Z'}))
    result = runner.invoke(cli, args)
    assert (result.exit_code, str(result.exception)) == (1, 'scan slot already belongs to a different capture; refusing to replace it')
    assert snapshot.read_text() == before


def test_invalid_manifest_does_not_modify_snapshot(tmp_path: Path) -> None:
    manifest = tmp_path / '_SUCCESS.json'
    snapshot = tmp_path / 'meta.json'
    manifest.write_text('{}')
    snapshot.write_text('{"total_bytes": 1500}\n')
    result = CliRunner().invoke(cli, ['capture-meta', str(manifest), str(snapshot)])
    assert result.exit_code == 1
    assert str(result.exception) == 'expected a version 1 disk-tree-capture manifest'
    assert snapshot.read_text() == '{"total_bytes": 1500}\n'
