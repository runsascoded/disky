"""Automatic GC requires whole, agreed publications, grace, preserved pins and held builder leases."""
import os

import pytest
from click.testing import CliRunner

from dt_cloud import generation_gc as gc
from dt_cloud.blobstore import Store

P = 'static-names'
NOW = 1_000_000


def world(tmp_path):
    stores = [Store(str(tmp_path / name)) for name in ('gcs', 'r2')]
    for s in stores:
        s.write_text(f'{P}/CURRENT.json', '{"gen":"g2"}')
        s.write_text(f'{P}/g2/scans.json', '{"scans":[{"id":"s1"}]}')
        s.write_text(f'{P}/g2/compaction.json', '{"from":"g1","files":{"data":"hash","scans.json":"hash"}}')
        for gen in ('g0', 'g1', 'g2', 'meta', 'src', 'unlinked'):
            s.write_text(f'{P}/{gen}/data', 'abc')
        for gen, successor in (('g0', 'g1'), ('g1', 'g2')):
            s.write_text(f'{P}/{gen}/successor.json', '{"gen":"' + successor + '"}')
        s.write_text(f'{P}/g1/compaction.json', '{"from":"g0"}')
        for key in s.listing(P):
            os.utime(s.uri(key), (1, 1))
    return stores


def actions(stores, **kw):
    _, records = gc.plan(stores, P, ['meta'], now=NOW, grace_s=100, **kw)
    return [(r['gen'], r['action'], r['reason']) for r in records]


def test_collects_only_forwarded_predecessors_and_keeps_tombstones(tmp_path):
    stores = world(tmp_path)
    records = gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True)
    assert [(r['gen'], r['action']) for r in records] == [
        ('g0', 'delete'), ('g1', 'delete'), ('g2', 'keep'), ('meta', 'keep'), ('src', 'keep'), ('unlinked', 'keep'),
    ]
    assert [[c['deleted'] for c in r['copies']] for r in records if r['action'] == 'delete'] == [[1, 1], [2, 2]]
    expected = [f'{P}/{key}' for key in [
        'CURRENT.json', 'g0/successor.json', 'g1/successor.json', 'g2/compaction.json', 'g2/data', 'g2/scans.json',
        'meta/data', 'src/data', 'unlinked/data',
    ]]
    assert [sorted(s.listing(P)) for s in stores] == [expected, expected]
    assert [(r['gen'], r['action']) for r in gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True)] == [
        ('g2', 'keep'), ('meta', 'keep'), ('src', 'keep'), ('unlinked', 'keep'),
    ]


def test_pins_named_pointers_and_candidate_inputs_are_protected(tmp_path):
    stores = world(tmp_path)
    stores[0].write_text(f'{P}/dev-CURRENT.json', '{"gen":"g0"}')
    stores[1].write_text(f'{P}/candidate/candidate.json', '{"from":"g1"}')
    assert actions(stores) == [
        ('candidate', 'keep', 'pinned or candidate input'),
        ('g0', 'keep', 'pinned or candidate input'), ('g1', 'keep', 'pinned or candidate input'),
        ('g2', 'keep', 'current'), ('meta', 'keep', 'pinned or candidate input'),
        ('src', 'keep', 'no published forwarding chain to current in every copy'),
        ('unlinked', 'keep', 'no published forwarding chain to current in every copy'),
    ]


def test_grace_uses_cutover_in_each_copy_and_does_not_restart_for_older_ancestors(tmp_path):
    stores = world(tmp_path)
    os.utime(stores[1].uri(f'{P}/g2/compaction.json'), (NOW, NOW))
    assert actions(stores)[:3] == [
        ('g0', 'delete', None),
        ('g1', 'keep', 'retention grace after publication or last predecessor write'), ('g2', 'keep', 'current'),
    ]
    os.utime(stores[0].uri(f'{P}/g0/data'), (NOW, NOW))
    assert actions(stores)[0] == ('g0', 'keep', 'retention grace after publication or last predecessor write')


def test_promoted_candidates_do_not_pin_predecessors_forever(tmp_path):
    stores = world(tmp_path)
    for s in stores:
        s.write_text(f'{P}/g2/candidate.json', '{"from":"g1"}')
    assert actions(stores)[1] == ('g1', 'delete', None)


@pytest.mark.parametrize(('key', 'replacement', 'message'), [
    ('CURRENT.json', '{"gen":"other"}', 'current pointers disagree across copies; nothing collected'),
    ('g2/scans.json', '{"scans":[]}', 'g2: scan rosters disagree across copies; nothing collected'),
    ('g2/compaction.json', '{}', 'g2: publication records disagree across copies; nothing collected'),
    ('g2/data', 'wrong-size', 'g2/data: published inventory missing or sizes disagree; nothing collected'),
])
def test_disagreement_refuses_all_deletion(tmp_path, key, replacement, message):
    stores = world(tmp_path)
    stores[1].write_text(f'{P}/{key}', replacement)
    before = [s.listing(P) for s in stores]
    with pytest.raises(RuntimeError) as e:
        gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True)
    expected = f'{P}: {message}' if key == 'CURRENT.json' else f'{P}/{message}'
    assert str(e.value) == expected
    assert [s.listing(P) for s in stores] == before


def test_broken_forwarding_or_incorrect_publication_keeps_source(tmp_path):
    stores = world(tmp_path)
    stores[1].write_text(f'{P}/g1/compaction.json', '{"from":"somewhere-else"}')
    assert actions(stores)[0] == ('g0', 'keep', 'successor publication not verified')
    stores[1].delete([f'{P}/g1/successor.json'])
    assert actions(stores)[1] == ('g1', 'keep', 'no published forwarding chain to current in every copy')


@pytest.mark.parametrize('publication', [None, '{"from":"g1"}', '{"from":"g1","files":{}}'])
def test_missing_output_inventory_refuses_all_deletion(tmp_path, publication):
    stores = world(tmp_path)
    for s in stores:
        if publication is None:
            s.delete([f'{P}/g2/compaction.json'])
        else:
            s.write_text(f'{P}/g2/compaction.json', publication)
    before = [s.listing(P) for s in stores]
    with pytest.raises(RuntimeError) as e:
        gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True)
    assert str(e.value) == f'{P}/g2: no completed output inventory; nothing collected'
    assert [s.listing(P) for s in stores] == before


@pytest.mark.parametrize('change', ['lease', 'pointer', 'inventory'])
def test_rechecks_lease_pointer_and_inventory_before_deleting(tmp_path, change):
    stores = world(tmp_path)
    before = [s.read_bytes(f'{P}/g0/data') for s in stores]

    def check():
        if change == 'lease':
            raise RuntimeError('lease lost')
        if change == 'pointer':
            for s in stores:
                s.write_text(f'{P}/g1/scans.json', '{"scans":[{"id":"s1"}]}')
                s.write_text(f'{P}/CURRENT.json', '{"gen":"g1"}')
        else:
            stores[0].write_text(f'{P}/g0/new', 'new')

    with pytest.raises(RuntimeError):
        gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True, check=check)
    assert [s.read_bytes(f'{P}/g0/data') for s in stores] == before


def test_retry_after_one_copy_was_collected(tmp_path):
    stores = world(tmp_path)
    calls = 0

    def fail_second_copy():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError('lost lease before second copy')

    with pytest.raises(RuntimeError, match='^lost lease before second copy$'):
        gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True, check=fail_second_copy)
    assert [s.exists(f'{P}/g0/data') for s in stores] == [False, True]
    gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True)
    assert [s.listing(f'{P}/g0') for s in stores] == [{f'{P}/g0/successor.json': 12}] * 2


def test_audit_is_durable_before_deletion_and_its_failure_aborts(tmp_path):
    import json

    stores = world(tmp_path)
    receipts = []

    def audit(doc):
        assert [s.read_bytes(f'{P}/g0/data') for s in stores] == [b'abc', b'abc']
        receipts.append(json.loads(json.dumps(doc)))
        raise RuntimeError('audit unavailable')

    with pytest.raises(RuntimeError, match='^audit unavailable$'):
        gc.collect(stores, P, ['meta'], now=NOW, grace_s=100, execute=True, audit=audit)
    assert receipts[0]['generations'][0]['_files'] == [{f'{P}/g0/data': 3}] * 2
    assert [s.read_bytes(f'{P}/g0/data') for s in stores] == [b'abc', b'abc']


def test_execute_requires_builder_coordinator(tmp_path):
    result = CliRunner().invoke(gc.command_cmd, ['-o', str(tmp_path), '-t', P, '-k', 'meta', '-x'])
    assert result.exit_code == 1
    assert str(result.exception) == 'executing generation GC requires --scratch to coordinate with builders'


@pytest.mark.parametrize('busy', [False, True])
def test_execute_holds_both_builder_leases_and_releases_them(tmp_path, monkeypatch, busy):
    from contextlib import contextmanager
    from types import SimpleNamespace

    events = []

    @contextmanager
    def coordinator(scratch, prefix):
        events.append(('coordinator', scratch, prefix))
        try:
            yield lambda: events.append(('coordinator-check',))
        finally:
            events.append(('coordinator-release',))

    @contextmanager
    def compaction(file, gen):
        events.append(('compaction', file, gen))
        if busy:
            raise gc.ar.Busy('compaction busy')
        try:
            yield SimpleNamespace(check=lambda: events.append(('compaction-check',)))
        finally:
            events.append(('compaction-release',))

    def collect(*a, **kw):
        kw['check']()
        events.append(('collect',))
        return []

    monkeypatch.setattr(gc.ar, 'gcs_coordinator_lease', coordinator)
    monkeypatch.setattr(gc.ar, 'GcsLeaseFile', lambda bucket, key: (bucket, key))
    monkeypatch.setattr(gc.ar, 'compaction_lease', compaction)
    monkeypatch.setattr(gc, 'collect', collect)
    result = CliRunner().invoke(gc.command_cmd, ['-o', str(tmp_path), '-t', P, '-k', 'meta', '-s', 'scratch', '-x'])
    assert result.exit_code == (1 if busy else 0)
    assert events == [('coordinator', 'scratch', P), ('compaction', ('scratch', f'{P}/compacting.lease.json'), 'generation-gc'),
                      *([] if busy else [('coordinator-check',), ('compaction-check',), ('collect',), ('compaction-release',)]),
                      ('coordinator-release',)]
