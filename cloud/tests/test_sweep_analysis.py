from pathlib import Path
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_analysis import analyze_preserved, classify_review


def snapshots(tmp_path: Path) -> tuple[Path, Path, Path]:
    current, reviewed, manifest = (tmp_path / f'{name}.parquet' for name in ('current', 'reviewed', 'manifest'))
    for path, names, times in (
        (reviewed, ['p/same', 'p/changed'], [100, 100]),
        (current, ['p/same', 'p/changed', 'p/new'], [100, 200, 200]),
    ):
        pq.write_table(pa.table({'path': names, 'size': [7] * len(names), 'mtime': times, 'kind': ['file'] * len(names)}), path)
    pq.write_table(pa.table({'name': ['p/same'], 'size_bytes': [7], 'mtime': [100]}), manifest)
    return current, reviewed, manifest


def test_classification_verifies_original_manifest(tmp_path: Path) -> None:
    assert classify_review(*snapshots(tmp_path), 'p/') == {
        'objects': 2, 'bytes': 14, 'new_objects': 1, 'changed_objects': 1,
        'keys': [{'name': 'p/changed', 'reason': 'changed_since_review'}, {'name': 'p/new', 'reason': 'new_since_review'}],
    }


def test_mismatched_manifest_refuses_backfill(tmp_path: Path) -> None:
    current, reviewed, manifest = snapshots(tmp_path)
    pq.write_table(pa.table({'name': ['p/new'], 'size_bytes': [7], 'mtime': [200]}), manifest)
    with pytest.raises(ValueError, match=r'^p/: reconstructed eligibility differs from the original manifest \(2 rows\)$'):
        classify_review(current, reviewed, manifest, 'p/')


def test_backfill_is_supplemental_and_keeps_execution_unknown(monkeypatch, tmp_path: Path) -> None:
    from dt_cloud import sweep_analysis as module

    current, reviewed, manifest = snapshots(tmp_path)
    run, out = tmp_path / 'run', tmp_path / 'analysis'
    (run / 'manifest').mkdir(parents=True)
    (run / 'manifest/b.parquet').write_bytes(manifest.read_bytes())
    docs = {
        'plan.json': {'bucket': 'b', 'name': 'p', 'sweep': ['p/'], 'as_of': {'p/': 'then'}},
        'plan-summary.json': {'skipped_after_as_of': {'objects': 2, 'bytes': 14}},
        'deleted-summary.json': {'mode': 'real', 'delete_failed': 0, 'drift_new': 2, 'bands': [{'prefix': 'p/', 'drift_new': 2}]},
    }
    for name, doc in docs.items():
        (run / name).write_text(json.dumps(doc) + '\n')
    before = {str(p.relative_to(run)): p.read_bytes() for p in run.rglob('*') if p.is_file()}
    monkeypatch.setattr(module, 'store_gen', lambda store, gen: 'pinned')

    def export(store, scan, bucket, prefixes, exact, path, *, gen):
        assert (store, bucket, prefixes, exact, gen) == ('historical-store', 'b', ['p/'], [], 'pinned')
        path.write_bytes((current if scan == 'now' else reviewed).read_bytes())

    monkeypatch.setattr(module, 'review_source', export)
    doc = analyze_preserved(str(run), 'now', 'historical-store', out)
    assert doc['method'] == 'review-snapshots'
    assert doc['review_omissions'] == {'objects': 2, 'bytes': 14, 'new_objects': 1, 'changed_objects': 1}
    assert doc['execution'] == {'outside_review': 2, 'keys_recorded': False}
    assert doc['prefixes'] == [{
        'prefix': 'p/', 'as_of': 'then', 'objects': 2, 'bytes': 14, 'new_objects': 1, 'changed_objects': 1,
        'keys': [{'name': 'p/changed', 'reason': 'changed_since_review'}, {'name': 'p/new', 'reason': 'new_since_review'}],
        'execution_outside_review': 2,
    }]
    assert sorted(doc['input_sha256']) == ['deleted-summary.json', 'manifest.parquet', 'now.parquet', 'plan-summary.json', 'plan.json', 'then.parquet']
    assert {str(p.relative_to(run)): p.read_bytes() for p in run.rglob('*') if p.is_file()} == before
    assert json.loads((out / 'preservation-analysis.json').read_text()) == doc
    with pytest.raises(FileExistsError):
        analyze_preserved(str(run), 'now', 'historical-store', out)
