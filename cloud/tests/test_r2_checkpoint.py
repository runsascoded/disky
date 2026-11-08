"""Closed-history folding preserves the read lens while steady-state work stays bounded."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud.cli import main
from dt_cloud.r2_checkpoint import META, parity, sync

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
