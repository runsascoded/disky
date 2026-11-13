"""Broad generation GC preserves cache/build directories without a generation marker."""
import json

import pytest

from dt_cloud.blobstore import Store
from dt_cloud import store_gen_gc as G


@pytest.mark.parametrize('kind', ['static-names', 'interval-store'])
def test_gc_holds_directories_without_a_root_scan_marker(tmp_path, kind):
    store = Store(str(tmp_path))
    contents = {
        f'{kind}/CURRENT.json': b'{"gen":"live"}\n',
        f'{kind}/live/scans.json': b'{"scans":[]}\n',
        f'{kind}/live/served.parquet': b'current',
        f'{kind}/old/scans.json': b'{"scans":[]}\n',
        f'{kind}/old/served.parquet': b'old',
        f'{kind}/src/scan-a/scans.json': b'{"scans":[]}\n',
        f'{kind}/src/scan-a/input.parquet': b'cached input',
        f'{kind}/unfinished/served.parquet': b'incomplete build',
    }
    for key, data in contents.items():
        store.write_bytes(key, data)
    keep = ['live'] if kind == 'static-names' else []
    plan = G.plan(store, kind, keep, lineage_only=False, grace_s=0, now=4e9)
    assert [(r['gen'], r['action'], r['reason']) for r in plan] == [
        ('live', 'keep', 'current'),
        ('old', 'delete', None),
        ('src', 'keep', 'not a published generation (missing root scans.json)'),
        ('unfinished', 'keep', 'not a published generation (missing root scans.json)'),
    ]
    result = G.run(store, kind, keep, grace_s=0, now=4e9, execute=True)
    assert [(r['gen'], r.get('deleted', 0)) for r in result] == [('live', 0), ('old', 2), ('src', 0), ('unfinished', 0)]
    remaining = {k: data for k, data in contents.items() if not k.startswith(f'{kind}/old/')}
    assert store.listing(kind) == {k: len(data) for k, data in remaining.items()}
    assert {k: store.read_bytes(k) for k in remaining} == remaining


def test_a_forwarding_record_does_not_publish_a_cache_directory(tmp_path):
    store = Store(str(tmp_path))
    contents = {
        'static-names/CURRENT.json': b'{"gen":"live"}\n',
        'static-names/live/scans.json': b'{"scans":[]}\n',
        'static-names/old/scans.json': b'{"scans":[]}\n',
        'static-names/old/successor.json': b'{"gen":"live"}\n',
        'static-names/src/successor.json': b'{"gen":"live"}\n',
        'static-names/src/input.parquet': b'cache',
    }
    for key, data in contents.items():
        store.write_bytes(key, data)
    result = G.run(store, 'static-names', ['live'], grace_s=0, now=4e9, execute=True)
    assert [(r['gen'], r['action'], r.get('deleted', 0)) for r in result] == [
        ('live', 'keep', 0), ('old', 'delete', 1), ('src', 'keep', 0),
    ]
    remaining = {k: data for k, data in contents.items() if k != 'static-names/old/scans.json'}
    assert {k: store.read_bytes(k) for k in store.listing('static-names')} == remaining
    assert json.loads(store.read_bytes('static-names/old/successor.json')) == {'gen': 'live'}
