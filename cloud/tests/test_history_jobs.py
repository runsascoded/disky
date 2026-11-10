"""Thinning jobs compare actual object states and publish only new candidate keys."""
from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import append_runner as ar
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import static_local as sl
from dt_cloud import static_names as sn
from dt_cloud.blobstore import Store
from dt_cloud.history_jobs import (
    ObjectChanges, assert_source, build_candidate, check_job_id, digest, estimate_retention, fingerprint,
    make_plan, run_job, source_state, storage_report, transition_pins, validate_plan,
)
from dt_cloud.scan_id import scan_epoch
from dt_cloud.scan_thinning import ChangeMetric
from test_interval_compact import make_world
from test_interval_store import V2_FROM


DATES = [f'2026-08-0{i}' for i in range(1, 6)]
T = [scan_epoch(s) for s in DATES]


def test_exact_object_changes_against_endpoint_reference(tmp_path):
    rows = []
    def add(path, start, end, size, version, **extra):
        rows.append({'depth': 1, 'path': path, 'vf': start, 'vt': end, 'kind': 'file', 'size': size,
                     'mtime': 1, **{c: 0 for c in ist.CLS}, 'version': version, 'last_read': 99, 'us': '', **extra})
    add('stable', T[0], T[1], 10, None)
    add('stable', T[1], sn.OPEN, 10, None, last_read=100, us='someone')
    add('edited', T[0], T[1], 10, 'a')
    add('edited', T[1], T[3], 20, 'b')
    add('edited', T[3], sn.OPEN, 10, 'a')
    add('transient', T[1], T[2], 5, 'x')
    add('token-edit', T[0], T[2], 3, 'old')
    add('token-edit', T[2], sn.OPEN, 3, 'new')
    add('directory', T[0], sn.OPEN, 999, None, kind='dir')
    path = tmp_path / 'pv.parquet'
    pq.write_table(pa.Table.from_pylist(rows), path)
    columns = ['size', 'mtime', *ist.CLS, 'version']
    snapshots = [{r['path']: tuple(r[c] for c in columns) for r in rows if r['kind'] == 'file' and r['vf'] <= t < r['vt']} for t in T]
    expected = {}
    for i, a in enumerate(DATES):
        for j, b in enumerate(DATES[i + 1:], i + 1):
            left, right = snapshots[i], snapshots[j]
            keys = [k for k in set(left) | set(right) if left.get(k) != right.get(k)]
            expected[a, b] = ChangeMetric(len(keys), sum(abs(right.get(k, (0,))[0] - left.get(k, (0,))[0]) for k in keys))
    with duckdb.connect() as con:
        oracle = ObjectChanges(con, [str(path)], DATES)
        actual = {(a, b): oracle(a, b) for a, b in expected}
    assert actual == expected
    assert actual[DATES[0], DATES[-1]] == ChangeMetric(1, 0)


@pytest.mark.parametrize('value', ['', '../bad', 'A', 'space bad', 'x' * 64])
def test_bad_job_ids(value):
    with pytest.raises(ValueError, match='^invalid history job id$'):
        check_job_id(value)


def test_transition_pins_preserve_format_and_attribution_boundaries():
    entries = [
        {'id': DATES[0], 'version': 1},
        {'id': DATES[1], 'version': 1},
        {'id': DATES[2], 'version': 2},
        {'id': DATES[3], 'version': 2, ist.ATTRIBUTION_SRC: 'attr.parquet'},
        {'id': DATES[4], 'version': 2, ist.ATTRIBUTION_SRC: 'next.parquet'},
    ]
    assert transition_pins(entries) == DATES[1:4]


def test_fingerprint_not_just_current_name(tmp_path):
    store = Store(str(tmp_path))
    ar.set_current(store, ist.PREFIX, 'g')
    base = {'scans': [{'id': DATES[0]}]}
    store.write_text('interval-store/g/scans.json', json.dumps(base))
    first = fingerprint(store, ist.PREFIX)
    assert first == {'gen': 'g', 'base': digest(base), 'manifest': None, 'manifest_digest': digest(None), 'scans': [DATES[0]]}
    manifest = {'runs': [{'key': 'deltas/x', 'scans': [DATES[1]]}]}
    store.write_text(f'interval-store/g/manifests/{DATES[1]}.json', json.dumps(manifest))
    assert fingerprint(store, ist.PREFIX) == {**first, 'manifest': f'manifests/{DATES[1]}.json', 'manifest_digest': digest(manifest), 'scans': DATES[:2]}


@pytest.fixture(scope='module')
def history_world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('history-world')
    world = make_world(tmp / 'source', owned=False)
    entries = world['scans'][V2_FROM:]
    doc = {'scans': entries, 'bucket': str(world['root']), 'layouts': ['listing/{id}/path-index.parquet']}
    data, serving = Store(str(tmp / 'data')), Store(str(tmp / 'serving'))
    il.build_gen(doc, str(world['root']), Path(serving.root) / 'interval-store/iv', tmp / 'iv-work', gen='iv', k=2,
                 rg_rows=8, threads=2, mem='4GB', level=True)
    sl.build_gen(doc, str(world['root']), Path(data.root) / 'static-names/st', tmp / 'static-work', gen='st', rule=None,
                 k=2, shard_rows=100, max_rows=30, R=12, K=5, floor=2, chunk_rows=7, threads=2, mem='4GB')
    ar.set_current(data, sn.PREFIX, 'st')
    ar.set_current(serving, ist.PREFIX, 'iv')
    return tmp, data, serving, entries


def _hashes(store, prefix):
    return {k: sha256((Path(store.root) / k).read_bytes()).hexdigest() for k in store.listing(prefix)}


def test_build_is_new_immutable_verified_snapshot(history_world):
    tmp, data, serving, entries = history_world
    before = {'iv': _hashes(serving, 'interval-store/iv'), 'st': _hashes(data, 'static-names/st')}
    state = source_state(data, serving)
    plan = make_plan({'policy': {'kind': 'low-change', 'drop_count': 2}, 'pinned': [], 'required_pins': []}, data, serving,
                     tmp / 'preview', threads=2, mem='4GB')
    assert plan['source'] == state
    assert len(plan['removed']) == 2
    assert plan['retained'][0] == entries[0]['id']
    assert plan['retained'][-1] == entries[-1]['id']
    validate_plan(plan, [])
    candidate = build_candidate(plan, data, serving, tmp / 'build', job_id='history-candidate', threads=2, mem='4GB')
    descriptor = serving.read_json('history/versions/history-candidate.json')
    assert descriptor == candidate['descriptor']
    assert descriptor['verified'] == {'interval': True, 'static': True, 'inventory': True}
    assert descriptor['retained'] == plan['retained']
    assert descriptor['removed'] == sorted(plan['removed'])
    assert candidate['storage'] == descriptor['storage']
    assert descriptor['storage']['additional_generation_bytes_before_gc'] == sum(
        copy['bytes'] for copy in descriptor['verification']['copies'].values()
    )
    assert descriptor['storage']['net_reduction_after_source_gc_bytes'] == descriptor['storage']['source_bytes'] - descriptor['storage']['candidate_bytes']
    assert source_state(data, serving) == state
    assert {'iv': _hashes(serving, 'interval-store/iv'), 'st': _hashes(data, 'static-names/st')} == before
    assert ar.current(serving, ist.PREFIX) == 'iv'
    assert ar.current(data, sn.PREFIX) == 'st'
    with pytest.raises(FileExistsError):
        build_candidate(plan, data, serving, tmp / 'repeat-build', job_id='history-candidate', threads=2, mem='4GB')


def test_reviewed_plan_digest_and_fresh_pins_are_enforced():
    plan = {'scans': DATES, 'retained': [DATES[0], DATES[-1]], 'removed': DATES[1:-1], 'required_pins': [], 'pinned': []}
    plan['digest'] = digest(plan)
    validate_plan(plan, [])
    with pytest.raises(ValueError, match='^plan removes a protected scan$'):
        validate_plan(plan, [DATES[1]])
    with pytest.raises(ValueError, match='^plan digest mismatch$'):
        validate_plan({**plan, 'removed': []}, [])


def test_completed_job_is_idempotent(tmp_path):
    store = Store(str(tmp_path))
    key = 'history/jobs/done'
    store.write_text(f'{key}/request.json', json.dumps({'action': 'plan'}))
    expected = {'id': 'done', 'state': 'planned', 'plan': {'digest': 'already-recorded'}}
    store.write_text(f'{key}/status.json', json.dumps(expected))
    assert run_job(store, 'done', tmp_path / 'unused') == expected


def test_manual_preview_exposes_exact_steps_candidates_and_set_estimate(history_world):
    tmp, data, serving, entries = history_world
    ids = [entry['id'] for entry in entries]
    plan = make_plan(
        {'policy': {'kind': 'manual', 'remove': [ids[3], ids[1]]}, 'pinned': [ids[2]], 'required_pins': []},
        data, serving, tmp / 'manual-preview', threads=2, mem='4GB', progress=lambda note: None,
    )
    assert (plan['policy'], plan['policy_options'], plan['removed']) == (
        'manual', {'kind': 'manual', 'remove': [ids[3], ids[1]]}, [ids[1], ids[3]],
    )
    assert plan['retained'] == [scan for scan in ids if scan not in (ids[1], ids[3])]
    assert [step['scan'] for step in plan['steps']] == [ids[1], ids[3]]
    assert [(candidate['scan'], candidate['before'], candidate['after'], candidate['protected']) for candidate in plan['candidates']] == [
        (scan, ids[i - 1] if i else None, ids[i + 1] if i + 1 < len(ids) else None, i in (0, 2, len(ids) - 1))
        for i, scan in enumerate(ids)
    ]
    assert plan['candidates'][0]['reasons'] == ['first', 'pinned', 'required-pin']
    assert plan['candidates'][2]['reasons'] == ['pinned']
    assert {field: plan['estimate'][field] for field in ('method', 'scope', 'exact_rows', 'estimated_bytes', 'set_level')} == {
        'method': 'projected-rows-proportional-compressed-data',
        'scope': 'folded-interval-canonical-tables',
        'exact_rows': True,
        'estimated_bytes': True,
        'set_level': True,
    }
    assert plan['estimate']['source_rows'] == sum(table['source_rows'] for table in plan['estimate']['tables'].values())
    assert plan['estimate']['retained_rows'] == sum(table['retained_rows'] for table in plan['estimate']['tables'].values())
    assert plan['estimate']['estimated_saved_bytes'] == plan['estimate']['source_bytes'] - plan['estimate']['estimated_retained_bytes']
    assert plan['digest'] == digest({key: value for key, value in plan.items() if key != 'digest'})
    validate_plan(plan, [])


@pytest.mark.parametrize('empty', [False, True])
def test_canonical_estimate_counts_projected_rows_without_counting_payload_bytes(tmp_path, empty):
    schema = pa.schema([
        pa.field('depth', pa.uint8()), pa.field('path', pa.string()),
        pa.field('vf', pa.int64()), pa.field('vt', pa.int64()), pa.field('size', pa.int64()),
    ])
    path = tmp_path / 'pv' / 'r0000.parquet'
    path.parent.mkdir()
    rows = [] if empty else [
        {'depth': 1, 'path': 'x', 'vf': T[0], 'vt': T[1], 'size': 10**15},
        {'depth': 1, 'path': 'x', 'vf': T[1], 'vt': T[2], 'size': 2 * 10**15},
        {'depth': 1, 'path': 'x', 'vf': T[2], 'vt': sn.OPEN, 'size': 10**15},
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, row_group_size=2)
    original = path.read_bytes()
    footer = pq.read_metadata(path)
    compressed = sum(footer.row_group(i).column(j).total_compressed_size for i in range(footer.num_row_groups) for j in range(footer.num_columns))
    size = len(original)
    after_bytes = size if empty else size - compressed + (compressed + 1) // 3
    with duckdb.connect() as con:
        estimate = estimate_retention(con, tmp_path, [DATES[0], DATES[2]], progress=lambda note: None)
    assert estimate['tables'] == {'pv': {
        'ranges': 1, 'source_rows': 0 if empty else 3, 'retained_rows': 0 if empty else 1,
        'source_bytes': size, 'estimated_retained_bytes': after_bytes, 'estimated_saved_bytes': size - after_bytes,
    }}
    assert estimate['source_bytes'] == size
    assert estimate['note'] == 'Estimate applies to this whole retained set. Per-scan savings are not additive; payload-change bytes are not stored-index bytes.'
    assert estimate['exclusions'] == [
        'Static name/search generations and their derived artifacts.',
        'Interval served sorts, sidecars and generation metadata.',
        'Churn recomputation and its additional interval splits.',
        'Mirrored copies, retained predecessor generations and job working space.',
        'Changes to compression, footer size and physical layout after rebuilding.',
    ]
    assert path.read_bytes() == original


def test_measured_storage_is_signed_per_copy_and_reclamation_is_conditional():
    sources = {'r2': {'files': 2, 'bytes': 100}, 'gs': {'files': 3, 'bytes': 300}}
    candidates = {'r2': {'files': 3, 'bytes': 200}, 'gs': {'files': 2, 'bytes': 150}}
    assert storage_report(sources, candidates) == {
        'method': 'measured-generation-inventories',
        'scope': 'source-and-candidate-generation-copies',
        'source_bytes': 400,
        'candidate_bytes': 350,
        'additional_generation_bytes_before_gc': 350,
        'net_reduction_after_source_gc_bytes': 50,
        'potential_source_reclaim_bytes': 400,
        'copies': {
            'r2': {'source_files': 2, 'candidate_files': 3, 'source_bytes': 100, 'candidate_bytes': 200, 'additional_generation_bytes_before_gc': 200, 'net_reduction_after_source_gc_bytes': -100},
            'gs': {'source_files': 3, 'candidate_files': 2, 'source_bytes': 300, 'candidate_bytes': 150, 'additional_generation_bytes_before_gc': 150, 'net_reduction_after_source_gc_bytes': 150},
        },
        'reclaim_condition': 'No space is freed by building or reader adoption. Sources remain until writer references, reader/version pins and retention rules release them and GC actually deletes them. Reader adoption does not switch the writer or retire its source.',
        'exclusions': ['Job working space and temporary files.', 'History job/version metadata outside generation prefixes.', 'Other generations and storage-system retention beyond these measured copies.'],
    }
