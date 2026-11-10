"""Exact ownership observations and immutable publication, without Nextflow, cloud calls or cleanup."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from dt_cloud import scan_ownership as O
from dt_cloud.blobstore import Store
from dt_cloud.scan_profile import parse

HEADER = 'task_id\thash\tnative_id\tname\tstatus\texit\tworkdir\n'
TAIL = 'ab/' + 'c' * 30
TASK_ROOT = 'gs://bucket/nf/work'
DIR = TASK_ROOT + '/' + TAIL


def snapshot(tmp_path: Path, raw: bytes | None) -> O.TraceSnapshot:
    source, dest = tmp_path / 'trace.tsv', tmp_path / 'snapshot.tsv'
    if raw is not None:
        source.write_bytes(raw)
    return O.snapshot_trace(source, dest)


def context(store: str = 'primary', run: str | None = 'run-1', attempt: str = 'head-1') -> dict:
    profile = parse({'deployment': 'example', 'store': store,
                     'executor': {'kind': 'google-batch', 'project': 'test-project', 'region': 'us-central1', 'image': 'registry/scan:test'},
                     'work': 'gs://bucket/nf',
                     'data': 'gs://bucket/data', 'layout': {'layer2': 'l2/{scan}/', 'snapshots': ''}, 'sources': ['s3://source'],
                     'skip': ['static-names', 'digest', 'interval-store', 'warm-cache', 'probe'], 'options': {'list': {'import': True}}})
    return O.context(profile, '{}', '2026-11-09T1200', 'g1', 'session-1', run, 'DT_RUN_ID' if run else 'unrecorded',
                     run or '', attempt, '2026-11-09T12:00:00+00:00', {}, TASK_ROOT)


def test_exact_trace_with_shared_cached_workdir(tmp_path):
    raw = (HEADER + f'1\tab/cccccc\t123\tindex\tCOMPLETED\t0\t{DIR}\n'
           + f'2\tab/cccccc\t-\tindex-copy\tCACHED\t0\t{DIR}\n').encode()
    captured = snapshot(tmp_path, raw)
    wd = {'state': 'full', 'raw': DIR, 'uri': DIR, 'relative_to_task_root': TAIL}
    assert O.observe_trace(captured, TASK_ROOT) == {
        'state': 'observed', 'bytes': len(raw), 'sha256': sha256(raw).hexdigest(), 'terminated': True,
        'fields': ['task_id', 'hash', 'native_id', 'name', 'status', 'exit', 'workdir'], 'issues': [],
        'tasks': [
            {'line': 2, 'fields': {'task_id': '1', 'hash': 'ab/cccccc', 'native_id': '123', 'name': 'index',
                                  'status': 'COMPLETED', 'exit': '0', 'workdir': DIR}, 'cached': False, 'workdir': wd},
            {'line': 3, 'fields': {'task_id': '2', 'hash': 'ab/cccccc', 'native_id': '-', 'name': 'index-copy',
                                  'status': 'CACHED', 'exit': '0', 'workdir': DIR}, 'cached': True, 'workdir': wd},
        ],
        'workdirs': [{'uri': DIR, 'relative_to_task_root': TAIL, 'task_lines': [2, 3], 'cached_lines': [3]}],
        'inventory_proof': 'observed-trace-only', 'parse_limits': {'bytes': 262144, 'task_rows': 1024},
        'snapshot_limit_bytes': 16777216, 'snapshot_truncated': False, 'source_size_at_open': len(raw), 'sha256_scope': 'snapshot',
    }
    assert captured.path.read_bytes() == raw


@pytest.mark.parametrize('root,raw,expected', [
    ('/data/nextflow-tasks', '/data/nextflow-tasks/' + TAIL, 'full'),
    ('gs://b/custom-task-root', 'gs://b/custom-task-root/' + TAIL, 'full'),
    ('gs://b/custom-task-root', 'gs://b/custom-task-root/ab/cccccc', 'unresolved'),
    ('gs://b/custom-task-root', 'gs://other/custom-task-root/' + TAIL, 'unresolved'),
    ('gs://b/custom-task-root', '/mnt/disks/b/custom-task-root/' + TAIL, 'unresolved'),
    ('relative-task-root', 'relative-task-root/' + TAIL, 'unresolved'),
    ('gs://b/custom-task-root', 'gs://b/custom-task-root/../custom-task-root/' + TAIL, 'unresolved'),
    ('gs://b/custom-task-root', '-', 'missing'),
])
def test_full_paths_only_under_the_configured_root(root, raw, expected):
    full = expected == 'full'
    assert O.workdir(raw, root) == {'state': expected, 'raw': raw, 'uri': raw if full else None,
                                    'relative_to_task_root': TAIL if full else None}


def test_missing_trace_has_no_invented_tasks(tmp_path):
    captured = snapshot(tmp_path, None)
    assert O.observe_trace(captured, TASK_ROOT) == {
        'state': 'missing', 'bytes': None, 'sha256': None, 'terminated': None, 'fields': [],
        'issues': [{'line': None, 'code': 'trace-missing'}], 'tasks': [], 'workdirs': [],
        'inventory_proof': 'observed-trace-only', 'parse_limits': {'bytes': 262144, 'task_rows': 1024},
        'snapshot_limit_bytes': 16777216, 'snapshot_truncated': False, 'source_size_at_open': None, 'sha256_scope': None,
    }


@pytest.mark.parametrize('raw,fields,issues', [
    (b'', [], [{'line': None, 'code': 'trace-empty'}]),
    (b'name\tstatus\nindex\tCACHED\n', ['name', 'status'], [
        {'line': 1, 'code': 'missing-fields', 'fields': ['task_id', 'hash', 'native_id', 'exit', 'workdir']},
        {'line': 2, 'code': 'missing-task-identity'}, {'line': 2, 'code': 'workdir-missing'},
    ]),
    ((HEADER + '1\tab/c\t-\tindex\tRUNNING\t-\tgs://bucket/nf/work/ab/cccccc').encode(),
     ['task_id', 'hash', 'native_id', 'name', 'status', 'exit', 'workdir'], [
         {'line': 2, 'code': 'unterminated-line'}, {'line': 2, 'code': 'nonterminal-task'},
         {'line': 2, 'code': 'workdir-unresolved'},
     ]),
    ((HEADER + '1\tab/c\t-\tindex\tFAILED\t1\n').encode(),
     ['task_id', 'hash', 'native_id', 'name', 'status', 'exit', 'workdir'], [
         {'line': 2, 'code': 'row-width-or-schema'}, {'line': 2, 'code': 'workdir-missing'},
     ]),
])
def test_incomplete_trace_evidence_is_explicit(tmp_path, raw, fields, issues):
    captured = snapshot(tmp_path, raw)
    observed = O.observe_trace(captured, TASK_ROOT)
    assert (observed['state'], observed['fields'], observed['issues'], observed['workdirs']) == ('incomplete', fields, issues, [])
    assert captured.path.read_bytes() == raw


def test_parsing_is_bounded_but_raw_snapshot_is_complete(tmp_path, monkeypatch):
    line = f'1\tab/cccccc\t-\tindex\tCACHED\t0\t{DIR}\n'
    raw = (HEADER + line * 20).encode()
    monkeypatch.setattr(O, 'MAX_PARSE_BYTES', len((HEADER + line).encode()) + 10)
    captured = snapshot(tmp_path, raw)
    observed = O.observe_trace(captured, TASK_ROOT)
    assert (len(captured.sample), captured.size, captured.digest, captured.path.read_bytes()) == (
        len((HEADER + line).encode()) + 10, len(raw), sha256(raw).hexdigest(), raw)
    assert (observed['state'], len(observed['tasks']), observed['issues']) == (
        'incomplete', 1, [{'line': None, 'code': 'parse-byte-limit'}])


def test_row_limit_keeps_an_explicit_incomplete_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(O, 'MAX_TASK_ROWS', 1)
    raw = (HEADER + f'1\tab/cccccc\t-\tindex\tCACHED\t0\t{DIR}\n' * 2).encode()
    observed = O.observe_trace(snapshot(tmp_path, raw), TASK_ROOT)
    assert (observed['state'], len(observed['tasks']), observed['issues']) == (
        'incomplete', 1, [{'line': 3, 'code': 'parse-row-limit'}])


def test_snapshot_limit_never_claims_a_full_source_digest(tmp_path, monkeypatch):
    line = f'1\tab/cccccc\t-\tindex\tCACHED\t0\t{DIR}\n'
    kept = (HEADER + line).encode()
    raw = kept + line.encode() * 20
    monkeypatch.setattr(O, 'MAX_SNAPSHOT_BYTES', len(kept))
    captured = snapshot(tmp_path, raw)
    assert (captured.size, captured.digest, captured.path.read_bytes(), captured.source_size_at_open, captured.truncated) == (
        len(kept), sha256(kept).hexdigest(), kept, len(raw), True)
    observed = O.observe_trace(captured, TASK_ROOT)
    assert {k: observed[k] for k in ('state', 'bytes', 'sha256', 'sha256_scope', 'snapshot_truncated', 'issues')} == {
        'state': 'incomplete', 'bytes': len(kept), 'sha256': sha256(kept).hexdigest(), 'sha256_scope': 'snapshot-prefix',
        'snapshot_truncated': True, 'issues': [{'line': None, 'code': 'snapshot-byte-limit'}],
    }


def test_context_records_unknown_pins_and_actual_run_separately():
    ctx = context(run=None)
    assert ctx == {
        'version': 1,
        'identity': {'deployment': 'example', 'store': 'primary', 'scan': '2026-11-09T1200', 'gen': 'g1',
                     'run_id': None, 'run_id_source': 'unrecorded', 'record_run_id': '', 'head_attempt': 'head-1', 'session': 'session-1'},
        'source': {'profile_sha256': sha256(b'{}').hexdigest(), 'git_sha': None, 'git_dirty': None,
                   'task_image': 'registry/scan:test', 'task_image_pinned': False, 'executor': 'google-batch', 'work_root': 'gs://bucket/nf',
                   'data_root': 'gs://bucket/data', 'task_work_root': TASK_ROOT, 'source_uris': ['s3://source']},
        'started_at': '2026-11-09T12:00:00+00:00',
    }


@pytest.mark.parametrize('rc,sig,status', [(0, None, 'succeeded'), (1, None, 'failed'), (143, 15, 'terminated')])
def test_final_outcome_does_not_claim_output_verification_or_deletion(tmp_path, rc, sig, status):
    ctx = context()
    doc = O.receipt(ctx, snapshot(tmp_path, None), '2026-11-09T12:01:00+00:00', rc, sig, ['runs/2026-11-09T1200/trace.tsv'])
    assert {k: doc[k] for k in ('identity', 'finished_at', 'outcome', 'legacy_reports', 'published_outputs_verified', 'deletion_authorized')} == {
        'identity': ctx['identity'], 'finished_at': '2026-11-09T12:01:00+00:00',
        'outcome': {'status': status, 'exit_code': rc, 'signal': sig},
        'legacy_reports': {'keys': ['runs/2026-11-09T1200/trace.tsv'], 'mutable': True},
        'published_outputs_verified': False, 'deletion_authorized': False,
    }


def test_store_run_attempt_namespaces_and_immutable_publication(tmp_path):
    captured = snapshot(tmp_path, HEADER.encode())
    store = Store(str(tmp_path / 'work'))
    identities = [('primary', 'run-1', 'head-1'), ('meta', 'run-1', 'head-1'),
                  ('primary', 'run-2', 'head-2'), ('primary', 'run-1', 'head-3')]
    keys = []
    for site_store, run, attempt in identities:
        doc = O.receipt(context(site_store, run, attempt), captured, 'finished', 0, None, [])
        key = O.publish(store, doc, captured)
        assert O.publish(store, doc, captured) == key
        assert store.read_json(key) == doc
        assert store.read_bytes(doc['trace']['key']) == HEADER.encode()
        keys.append(key)
    assert keys == [
        'ownership/v1/example/primary/run-1/head-1/receipt.json',
        'ownership/v1/example/meta/run-1/head-1/receipt.json',
        'ownership/v1/example/primary/run-2/head-2/receipt.json',
        'ownership/v1/example/primary/run-1/head-3/receipt.json',
    ]
    assert O.prefix('example', 'primary', None, 'head-4') == 'ownership/v1/example/primary/_unrecorded/head-4'
    held = store.read_bytes(keys[-1])
    changed = O.receipt(context(*identities[-1]), captured, 'changed', 1, None, [])
    with pytest.raises(ValueError, match='^ownership receipt conflicts with existing object or readback: '):
        O.publish(store, changed, captured)
    assert store.read_bytes(keys[-1]) == held


def test_rejects_changed_trace_and_unsupported_nonatomic_store(tmp_path):
    captured = snapshot(tmp_path, HEADER.encode())
    doc = O.receipt(context(), captured, 'finished', 0, None, [])
    with pytest.raises(ValueError, match='^ownership trace bytes do not match the receipt$'):
        O.publish(Store(str(tmp_path / 'work')), doc, replace(captured, digest='changed'))
    memory = Store('memory://ownership-test')
    with pytest.raises(ValueError, match='^ownership receipts require atomic create: a local work root or gs://$'):
        O.publish(memory, doc, captured)
    assert memory.listing('ownership') == {}


def test_gcs_uses_atomic_create_precondition_and_compares_existing_bytes(tmp_path, monkeypatch):
    from google.api_core.exceptions import PreconditionFailed
    from google.cloud import storage

    objects, calls = {}, []

    class Blob:
        def __init__(self, key):
            self.key = key

        def upload_from_file(self, src, *, size, if_generation_match):
            calls.append((self.key, size, if_generation_match))
            if self.key in objects:
                raise PreconditionFailed('exists')
            objects[self.key] = src.read()

    monkeypatch.setattr(
        storage,
        'Client',
        lambda: SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda key: Blob(f'{name}/{key}'))),
    )
    fs = SimpleNamespace(open=lambda key, mode: BytesIO(objects[key]))
    store = Store('gs://bucket/nf', fs=fs, base='bucket/nf')
    captured = snapshot(tmp_path, HEADER.encode())
    doc = O.receipt(context(), captured, 'finished', 1, None, [])
    key = O.publish(store, doc, captured)
    assert O.publish(store, doc, captured) == key
    prefix = 'bucket/nf/ownership/v1/example/primary/run-1/head-1/'
    receipt_size = len(objects[prefix + 'receipt.json'])
    assert calls == [(prefix + 'trace.tsv', len(HEADER.encode()), 0), (prefix + 'receipt.json', receipt_size, 0)] * 2
    old = objects[prefix + 'trace.tsv']
    objects[prefix + 'trace.tsv'] = b'conflicting trace'
    with pytest.raises(ValueError, match='^ownership receipt conflicts with existing object or readback: '):
        O.publish(store, doc, captured)
    assert objects[prefix + 'trace.tsv'] == b'conflicting trace'
    assert old == HEADER.encode()
