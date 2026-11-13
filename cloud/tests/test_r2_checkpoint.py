"""Closed-history folding preserves the read lens while steady-state work stays bounded."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud.cli import main
from dt_cloud.r2_checkpoint import META, parity, publish_receipt, receipt, sync

UTC = timezone.utc
CUT = datetime(2026, 11, 8, tzinfo=UTC)


def hour(root: Path, bucket: str, when: datetime, rows: list[tuple], *, run: str = 'a', sampled: bool = False) -> tuple[str, str]:
    name = when.strftime('%Y-%m-%dT%H:%M')
    key = f'agg/{bucket}/{name}/{run}.json'
    p = root / key
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = int(when.timestamp() * 1000)
    p.write_text(json.dumps({'schema_version': 4, 'bucket': bucket, 'hour': when.isoformat(),
                            'sampling': {'capture': 'sampled' if sampled else 'unsampled'},
                            'rows': [[path, action, n, ts, ts] for path, action, n in rows]}))
    return name, key


def state(root: Path, frozen: dict[str, list[tuple[str, str]]], active: dict[str, list[tuple[str, str]]]) -> str:
    p = root / 'state.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'schema_version': 2, 'account': 'account', 'buckets': {
        b: {'frozen': dict(frozen.get(b, [])), 'hours': {h: {'agg': k} for h, k in active.get(b, [])}}
        for b in sorted(frozen.keys() | active.keys())
    }}))
    return str(p)


def totals(path: Path) -> list[tuple]:
    with duckdb.connect() as con:
        rows = con.execute("SELECT bucket,path,op,sum(n_ops)::BIGINT,max(last_ts) AT TIME ZONE 'UTC',bool_or(sampled) FROM read_parquet(?) GROUP BY 1,2,3 ORDER BY 1,2,3", [str(path)]).fetchall()
        return [(b, p, op, n, ts.replace(tzinfo=UTC), sampled) for b, p, op, n, ts, sampled in rows]


def test_bootstrap_and_unchanged_update_match_full_history(tmp_path: Path) -> None:
    root = tmp_path / 'archive'
    old = hour(root, 'b1', CUT - timedelta(days=10), [('a//x/', 'GetObject', 2), ('a/x', 'GetObject', 3)])
    older = hour(root, 'b1', CUT - timedelta(days=9), [('a/x', 'GetObject', 7)], sampled=True)
    recent = hour(root, 'b1', CUT - timedelta(hours=3), [('a/x', 'GetObject', 11), ('z', 'HeadObject', 5)])
    other = hour(root, 'b2', CUT - timedelta(days=10), [('a/x', 'GetObject', 13)])
    src = state(root, {'b1': [old, older], 'b2': [other]}, {'b1': [recent]})
    checkpoint = str(tmp_path / 'closed.parquet')
    a, b = tmp_path / 'a.parquet', tmp_path / 'b.parquet'
    first = sync(src, checkpoint, CUT, a, tmp_path / 'first', verify=True)
    second = sync(src, checkpoint, CUT, b, tmp_path / 'second', verify=True)
    assert [(r['downloaded'], r['new_frozen'], r['active'], r['closed_rows'], r['closed_windows'], r['verified']) for r in (first, second)] == [
        (4, 3, 1, 2, 3, True), (1, 0, 1, 2, 3, True),
    ]
    t = CUT - timedelta(hours=3)
    assert totals(a) == totals(b) == [
        ('b1', '.', 'GET', 23, t, True), ('b1', '.', 'HEAD', 5, t, False),
        ('b1', 'a', 'GET', 23, t, True), ('b1', 'a/x', 'GET', 23, t, True), ('b1', 'z', 'HEAD', 5, t, False),
        ('b2', '.', 'GET', 13, CUT - timedelta(days=10), False),
        ('b2', 'a', 'GET', 13, CUT - timedelta(days=10), False),
        ('b2', 'a/x', 'GET', 13, CUT - timedelta(days=10), False),
    ]
    assert pq.read_metadata(a).metadata[b'grain'] == b'read-summary'
    assert json.loads(pq.read_metadata(checkpoint).metadata[META])['cursors'] == {
        'b1': (CUT - timedelta(days=9)).timestamp(), 'b2': (CUT - timedelta(days=10)).timestamp(),
    }


def test_active_replacement_then_freezing_is_counted_once(tmp_path: Path) -> None:
    root = tmp_path / 'archive'
    when = CUT - timedelta(days=8)
    old = hour(root, 'b1', when, [('hot', 'GetObject', 2)])
    src = state(root, {}, {'b1': [old]})
    checkpoint = str(tmp_path / 'closed.parquet')
    out = tmp_path / 'summary.parquet'
    sync(src, checkpoint, CUT, out, tmp_path / 'one', verify=True)
    replacement = hour(root, 'b1', when, [('hot', 'GetObject', 5)], run='replacement')
    (root / old[1]).unlink()
    state(root, {}, {'b1': [replacement]})
    sync(src, checkpoint, CUT, out, tmp_path / 'two', verify=True)
    state(root, {'b1': [replacement]}, {})
    third = sync(src, checkpoint, CUT, out, tmp_path / 'three', verify=True)
    fourth = sync(src, checkpoint, CUT, out, tmp_path / 'four', verify=True)
    assert (third['new_frozen'], third['downloaded'], fourth['new_frozen'], fourth['downloaded']) == (1, 1, 0, 0)
    assert totals(out) == [('b1', '.', 'GET', 5, when, False), ('b1', 'hot', 'GET', 5, when, False)]


def test_fixed_keys_keep_one_closed_row_as_windows_accumulate(tmp_path: Path) -> None:
    root = tmp_path / 'archive'
    frozen = []
    checkpoint = str(tmp_path / 'closed.parquet')
    out = tmp_path / 'summary.parquet'
    results = []
    for i in range(6):
        frozen.append(hour(root, 'b1', CUT - timedelta(days=20 - i), [('hot', 'GetObject', 3)]))
        src = state(root, {'b1': frozen}, {})
        r = sync(src, checkpoint, CUT, out, tmp_path / str(i), verify=True)
        results.append((r['downloaded'], r['closed_rows'], r['closed_windows']))
    assert results == [(1, 1, n) for n in range(1, 7)]
    assert totals(out) == [
        ('b1', '.', 'GET', 18, CUT - timedelta(days=15), False),
        ('b1', 'hot', 'GET', 18, CUT - timedelta(days=15), False),
    ]


def test_empty_windows_advance_without_fabricating_reads(tmp_path: Path) -> None:
    root = tmp_path / 'archive'
    empty = hour(root, 'b1', CUT - timedelta(days=10), [])
    src = state(root, {'b1': [empty]}, {})
    checkpoint = str(tmp_path / 'closed.parquet')
    out = tmp_path / 'summary.parquet'
    r = sync(src, checkpoint, CUT, out, tmp_path / 'one', verify=True)
    again = sync(src, checkpoint, CUT, out, tmp_path / 'two', verify=True)
    assert (r['closed_rows'], r['closed_windows'], r['rows_in'], out.exists(), again['downloaded']) == (0, 1, 0, False, 0)


def test_missing_window_preserves_checkpoint_and_output(tmp_path: Path) -> None:
    root = tmp_path / 'archive'
    old = hour(root, 'b1', CUT - timedelta(days=10), [('hot', 'GetObject', 3)])
    src = state(root, {'b1': [old]}, {})
    checkpoint, out = tmp_path / 'closed.parquet', tmp_path / 'summary.parquet'
    sync(src, str(checkpoint), CUT, out, tmp_path / 'one', verify=True)
    before = (checkpoint.read_bytes(), out.read_bytes())
    missing = ('2026-11-07T12:00', 'agg/missing.json')
    state(root, {'b1': [old]}, {'b1': [missing]})
    with pytest.raises(FileNotFoundError):
        sync(src, str(checkpoint), CUT, out, tmp_path / 'two')
    assert (checkpoint.read_bytes(), out.read_bytes()) == before


def test_earlier_cut_and_wrong_source_refuse(tmp_path: Path) -> None:
    root = tmp_path / 'archive'
    old = hour(root, 'b1', CUT - timedelta(days=10), [('hot', 'GetObject', 3)])
    src = state(root, {'b1': [old]}, {})
    checkpoint, out = str(tmp_path / 'closed.parquet'), tmp_path / 'summary.parquet'
    sync(src, checkpoint, CUT, out, tmp_path / 'one')
    with pytest.raises(ValueError, match='^read checkpoint cannot answer a cut before its closed history; use original inputs$'):
        sync(src, checkpoint, CUT - timedelta(days=11), out, tmp_path / 'two')
    other = state(tmp_path / 'other', {'b1': []}, {})
    with pytest.raises(ValueError, match='^read checkpoint schema/source mismatch$'):
        sync(other, checkpoint, CUT, out, tmp_path / 'three')


def test_cli_and_minute_cut(tmp_path: Path) -> None:
    root = tmp_path / 'archive'
    early = hour(root, 'b1', CUT - timedelta(minutes=5), [('hot', 'GetObject', 3)])
    late = hour(root, 'b1', CUT, [('future', 'GetObject', 9)])
    src = state(root, {}, {'b1': [early, late]})
    out = tmp_path / 'summary.parquet'
    result = CliRunner().invoke(main, ['access', 'r2-sync', '-c', str(tmp_path / 'closed.parquet'), '-d', '2026-11-08', '-o', str(out), '-s', src, '-v', '-w', str(tmp_path / 'work')])
    assert result.exit_code == 0, result.exception
    doc = json.loads(result.stdout)
    assert {k: doc[k] for k in ('active', 'downloaded', 'new_frozen', 'rows_in', 'verified', 'closed_rows')} == {
        'active': 1, 'downloaded': 1, 'new_frozen': 0, 'rows_in': 3, 'verified': True, 'closed_rows': 0,
    }
    assert totals(out) == [
        ('b1', '.', 'GET', 3, CUT - timedelta(minutes=5), False),
        ('b1', 'hot', 'GET', 3, CUT - timedelta(minutes=5), False),
    ]


def test_failed_comparison_preserves_published_files(tmp_path: Path, monkeypatch) -> None:
    from dt_cloud import r2_checkpoint

    root = tmp_path / 'archive'
    old = hour(root, 'b1', CUT - timedelta(days=10), [('hot', 'GetObject', 3)])
    src = state(root, {'b1': [old]}, {})
    checkpoint, out = tmp_path / 'closed.parquet', tmp_path / 'summary.parquet'
    sync(src, str(checkpoint), CUT, out, tmp_path / 'one')
    before = checkpoint.read_bytes(), out.read_bytes()
    later = hour(root, 'b1', CUT - timedelta(days=9), [('hot', 'GetObject', 5)])
    state(root, {'b1': [old, later]}, {})

    def mismatch(*args) -> None:
        raise ValueError('injected differing read-lens rows')

    monkeypatch.setattr(r2_checkpoint, 'parity', mismatch)
    with pytest.raises(ValueError, match='^injected differing read-lens rows$'):
        sync(src, str(checkpoint), CUT, out, tmp_path / 'two', verify=True)
    assert (checkpoint.read_bytes(), out.read_bytes()) == before


def test_gcs_checkpoint_publication_is_conditional(tmp_path: Path, monkeypatch) -> None:
    from google.cloud import storage
    from dt_cloud.r2_checkpoint import checkpoint_file

    calls = []

    class Blob:
        generation = 17

        def download_to_filename(self, name, *, if_generation_match):
            calls.append(('read', if_generation_match))
            Path(name).write_bytes(b'old')

        def upload_from_filename(self, name, *, if_generation_match):
            calls.append(('write', if_generation_match, Path(name).read_bytes()))
            if if_generation_match != self.generation:
                raise ValueError('checkpoint changed concurrently')

    blob = Blob()

    class Bucket:
        def get_blob(self, key):
            assert key == 'access/r2/closed.parquet'
            return blob

    class Client:
        def bucket(self, name):
            assert name == 'data'
            return Bucket()

    monkeypatch.setattr(storage, 'Client', Client)
    work = tmp_path / 'work'
    work.mkdir()
    candidate = tmp_path / 'candidate'
    candidate.write_bytes(b'new')
    with checkpoint_file('gs://data/access/r2/closed.parquet', work) as (previous, publish):
        assert previous.read_bytes() == b'old'
        blob.generation = 18
        with pytest.raises(ValueError, match='^checkpoint changed concurrently$'):
            publish(candidate)
    assert calls == [('read', 17), ('write', 17, b'new')]


def test_compacted_history_uses_checkpoint_and_keeps_new_windows_exact(tmp_path: Path) -> None:
    from disk_tree.access.parsers.r2 import state_aggs

    root = tmp_path / 'archive'
    old = hour(root, 'b1', CUT - timedelta(days=10), [('hot', 'GetObject', 3)])
    recent = hour(root, 'b1', CUT - timedelta(hours=3), [('hot', 'GetObject', 5)])
    src = state(root, {'b1': [old]}, {'b1': [recent]})
    checkpoint, out = str(tmp_path / 'closed.parquet'), tmp_path / 'out.parquet'
    sync(src, checkpoint, CUT, out, tmp_path / 'one', verify=True)
    expected = totals(out)
    doc = json.loads(Path(src).read_text())
    doc['schema_version'] = 3
    doc['buckets']['b1']['closed'] = {'through': (CUT - timedelta(days=10)).timestamp(), 'checkpoint': checkpoint}
    doc['buckets']['b1']['frozen'] = {}
    Path(src).write_text(json.dumps(doc))
    (root / old[1]).unlink()
    result = sync(src, checkpoint, CUT, out, tmp_path / 'two')
    assert (result['downloaded'], result['new_frozen'], result['closed_rows']) == (1, 0, 1)
    assert totals(out) == expected
    with pytest.raises(ValueError, match='^r2 state: compacted history requires its durable read checkpoint$'):
        state_aggs(doc, CUT)
    with pytest.raises(ValueError, match='^read checkpoint: full-history verification requires original, uncompacted state$'):
        sync(src, checkpoint, CUT, out, tmp_path / 'verify', verify=True)


@pytest.mark.parametrize('change', ['missing', 'ahead', 'other'])
def test_compacted_state_refuses_missing_lagging_or_other_checkpoint(tmp_path: Path, change: str) -> None:
    root = tmp_path / 'archive'
    old = hour(root, 'b1', CUT - timedelta(days=10), [('hot', 'GetObject', 3)])
    src = state(root, {'b1': [old]}, {})
    checkpoint, out = tmp_path / 'closed.parquet', tmp_path / 'out.parquet'
    sync(src, str(checkpoint), CUT, out, tmp_path / 'one', verify=True)
    expected = out.read_bytes()
    doc = json.loads(Path(src).read_text())
    doc['schema_version'] = 3
    doc['buckets']['b1'].update(frozen={}, closed={
        'through': (CUT - timedelta(days=9 if change == 'ahead' else 10)).timestamp(),
        'checkpoint': str(tmp_path / 'other.parquet') if change == 'other' else str(checkpoint),
    })
    Path(src).write_text(json.dumps(doc))
    if change == 'missing':
        checkpoint.unlink()
    with pytest.raises(ValueError, match='^r2 state: b1 compacted history is ahead of or differs from the read checkpoint$'):
        sync(src, str(checkpoint), CUT, out, tmp_path / 'two')
    assert out.read_bytes() == expected


def test_receipt_requires_verified_bootstrap_and_survives_steady_update(tmp_path: Path) -> None:
    from hashlib import sha256

    root = tmp_path / 'archive'
    old = hour(root, 'b1', CUT - timedelta(days=10), [('hot', 'GetObject', 3)])
    src = state(root, {'b1': [old]}, {})
    checkpoint, out = tmp_path / 'closed.parquet', tmp_path / 'out.parquet'
    sync(src, str(checkpoint), CUT, out, tmp_path / 'one')
    with pytest.raises(ValueError, match='^read checkpoint receipt requires a passing full-history bootstrap comparison$'):
        receipt(str(checkpoint), tmp_path / 'unverified')
    sync(src, str(checkpoint), CUT, out, tmp_path / 'two', verify=True)
    sync(src, str(checkpoint), CUT, out, tmp_path / 'three')
    expected = {'schema_version': 1, 'kind': 'r2-read-checkpoint', 'source': src, 'account': 'account',
                'checkpoint': {'uri': str(checkpoint), 'generation': None, 'sha256': sha256(checkpoint.read_bytes()).hexdigest()},
                'cursors': {'b1': (CUT - timedelta(days=10)).timestamp()}}
    assert receipt(str(checkpoint), tmp_path / 'receipt') == expected
    destination = tmp_path / 'receipt.json'
    result = CliRunner().invoke(main, ['access', 'r2-receipt', '-c', str(checkpoint), '-o', str(destination), '-w', str(tmp_path / 'cli')])
    assert result.exit_code == 0, result.exception
    assert json.loads(destination.read_text()) == expected
    assert json.loads(result.stdout) == {'out': str(destination), 'cursors': expected['cursors'], 'generation': None}


@pytest.mark.parametrize('mode', ['create', 'advance', 'behind', 'conflict'])
def test_receipt_publication_is_monotonic_conditional_and_read_back(monkeypatch, mode: str) -> None:
    from io import BytesIO
    from types import SimpleNamespace
    import boto3
    from botocore.exceptions import ClientError
    from botocore.response import StreamingBody
    from botocore.stub import Stubber
    from disk_tree import blobfs

    client = boto3.client('s3', endpoint_url='https://receipt.example.org', region_name='auto',
                          aws_access_key_id='fixture', aws_secret_access_key='fixture')
    monkeypatch.setattr(boto3, 'Session', lambda **_: SimpleNamespace(client=lambda *args, **kw: client))
    monkeypatch.setattr(blobfs, 'r2_endpoint', lambda _: 'https://receipt.example.org')
    monkeypatch.setattr(blobfs, 'bucket_profile', lambda _: None)
    doc = {'schema_version': 1, 'kind': 'r2-read-checkpoint', 'account': 'own', 'source': 'r2://archive/state.json',
           'checkpoint': {'uri': 'gs://own/closed.parquet', 'generation': '18', 'sha256': 'a' * 64}, 'cursors': {'b': 200}}
    old = {**doc, 'checkpoint': {**doc['checkpoint'], 'generation': '17'}, 'cursors': {'b': 300 if mode == 'behind' else 100}}
    data = (json.dumps(doc, sort_keys=True) + '\n').encode()
    target = {'Bucket': 'own-receipts', 'Key': 'r2-read.json'}
    def response(value: bytes, etag: str) -> dict:
        return {'Body': StreamingBody(BytesIO(value), len(value)), 'ETag': etag}
    with Stubber(client) as stub:
        condition = {'IfNoneMatch': '*'} if mode == 'create' else {'IfMatch': '"old"'}
        if mode == 'create':
            stub.add_client_error('get_object', service_error_code='NoSuchKey', http_status_code=404, expected_params=target)
        else:
            stub.add_response('get_object', response(json.dumps(old).encode(), '"old"'), target)
        if mode != 'behind':
            put = {**target, 'Body': data, 'ContentType': 'application/json', **condition}
            if mode == 'conflict':
                stub.add_client_error('put_object', service_error_code='PreconditionFailed', http_status_code=412, expected_params=put)
            else:
                stub.add_response('put_object', {'ETag': '"new"'}, put)
                stub.add_response('get_object', response(data, '"new"'), {**target, 'IfMatch': '"new"'})
        if mode == 'behind':
            with pytest.raises(ValueError, match='^receipt would move backwards or change its source/checkpoint$'):
                publish_receipt(doc, 'r2://own-receipts/r2-read.json')
        elif mode == 'conflict':
            with pytest.raises(ClientError) as error:
                publish_receipt(doc, 'r2://own-receipts/r2-read.json')
            assert error.value.response['Error']['Code'] == 'PreconditionFailed'
        else:
            assert publish_receipt(doc, 'r2://own-receipts/r2-read.json') is None
        stub.assert_no_pending_responses()
