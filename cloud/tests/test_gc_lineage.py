"""GC follows every completed interval migration, preserves aliases, and starts grace at publication."""
import json
import os

import pytest

from dt_cloud import scan_retire as sr
from dt_cloud import store_gen_gc as gc
from dt_cloud.blobstore import Store

P = 'interval-store'
NOW = 1_000_000


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path))
    s.write_text(f'{P}/CURRENT.json', '{"gen":"new"}')
    for gen in ('old', 'new', 'unlinked'):
        s.write_text(f'{P}/{gen}/data', 'abc')
        s.write_text(f'{P}/{gen}/scans.json', '{"scans":[{"id":"2026-01-01"}]}')
        s.write_text(f'{P}/{gen}/ranges.json', '{"k":1}')
    for key in s.listing(P):
        os.utime(s.uri(key), (1, 1))
    return s


@pytest.mark.parametrize('record', ['compaction.json', 'backfill.json', 'attribution-backfill.json',
                                  'backfill-churn.json', 'object-version-migration.json'])
def test_collects_completed_migration_parent_preserving_alias_and_parity(store, record, monkeypatch):
    key = f'{P}/new/{record}'
    store.write_text(key, '{"from":"old"}')
    os.utime(store.uri(key), (1, 1))
    assert sr.lineage(store, 'new') == ['new', 'old']
    calls = []
    monkeypatch.setattr(gc.ic, 'carry_markers', lambda s, old, new: calls.append((old, new)) or 0)
    records = gc.run(store, P, [], lineage_only=True, now=NOW, grace_s=100, execute=True)
    assert [(r['gen'], r['action']) for r in records] == [('new', 'keep'), ('old', 'delete'), ('unlinked', 'keep')]
    assert calls == [('old', 'new')]
    assert store.listing(f'{P}/old') == {f'{P}/old/successor.json': len(json.dumps({'gen': 'new'}) + '\n')}
    assert store.read_json(f'{P}/old/successor.json') == {'gen': 'new'}
    assert [(r['gen'], r['action']) for r in gc.run(store, P, [], lineage_only=True, now=NOW, grace_s=100)] == [
        ('new', 'keep'), ('unlinked', 'keep'),
    ]


def test_recent_backfill_keeps_an_ancient_source_for_the_grace(store):
    key = f'{P}/new/backfill-churn.json'
    store.write_text(key, '{"from":"old"}')
    os.utime(store.uri(key), (NOW, NOW))
    records = gc.run(store, P, [], lineage_only=True, now=NOW, grace_s=100, execute=True)
    assert [(r['gen'], r['action']) for r in records] == [('new', 'keep'), ('old', 'keep'), ('unlinked', 'keep')]
    assert store.read_bytes(f'{P}/old/data') == b'abc'


def test_explicit_pin_preserves_a_backfill_parent(store):
    key = f'{P}/new/backfill-churn.json'
    store.write_text(key, '{"from":"old"}')
    os.utime(store.uri(key), (1, 1))
    records = gc.run(store, P, ['old'], lineage_only=True, now=NOW, grace_s=100, execute=True)
    assert [(r['gen'], r['action']) for r in records] == [('new', 'keep'), ('old', 'keep'), ('unlinked', 'keep')]
    assert store.read_bytes(f'{P}/old/data') == b'abc'


def test_current_change_during_planning_aborts_before_deletion(store, monkeypatch):
    key = f'{P}/new/backfill-churn.json'
    store.write_text(key, '{"from":"old"}')
    os.utime(store.uri(key), (1, 1))
    original = gc.plan

    def racing(*args, **kw):
        records = original(*args, **kw)
        store.write_text(f'{P}/CURRENT.json', '{"gen":"unlinked"}')
        return records

    monkeypatch.setattr(gc, 'plan', racing)
    with pytest.raises(RuntimeError, match='^interval-store: current changed during GC; stopped before deleting old$'):
        gc.run(store, P, [], lineage_only=True, now=NOW, grace_s=100, execute=True)
    assert store.read_bytes(f'{P}/old/data') == b'abc'
