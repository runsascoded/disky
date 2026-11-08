"""Incremental read-lens summaries over immutable frozen R2 collection windows.

One Parquet checkpoint replaces elapsed-window history with per-key counts and
stamps. Active windows are read afresh, never added to the checkpoint. No source
aggregate is deleted. GCS publication uses a generation precondition; local
publication holds an advisory lock, so concurrent writers cannot lose a carry.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from click import command, option

from .blobstore import Store
from .static_names import connect, q

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

META = b'r2_read_checkpoint'
EMPTY = "SELECT NULL::VARCHAR bucket, NULL::VARCHAR path, NULL::VARCHAR op, NULL::TIMESTAMP AS hour, NULL::BIGINT n_ops, NULL::TIMESTAMPTZ last_ts, NULL::TIMESTAMPTZ first_ts, NULL::BOOLEAN sampled WHERE false"
LEAF = "bucket, path, hour, op, n_ops, NULL::BIGINT bytes_out, NULL::BIGINT bytes_in, NULL::BIGINT n_requesters, last_ts, sampled"


@contextmanager
def checkpoint_file(uri: str, work: Path) -> Iterator[tuple[Path, Callable[[Path], None]]]:
    """Stage one checkpoint; its publisher atomically replaces precisely the version read."""
    local = work / 'previous.parquet'
    local.unlink(missing_ok=True)
    if uri.startswith(('gs://', 'gcs://')):
        from google.cloud import storage

        bucket, key = uri.split('://', 1)[1].split('/', 1)
        blob = storage.Client().bucket(bucket).get_blob(key)
        generation = int(blob.generation) if blob is not None else 0
        if blob is not None:
            blob.download_to_filename(str(local), if_generation_match=generation)
        else:
            blob = storage.Client().bucket(bucket).blob(key)

        def publish(src: Path) -> None:
            blob.upload_from_filename(str(src), if_generation_match=generation)

        yield local, publish
    elif '://' in uri:
        raise ValueError('read checkpoints require gs:// or a local file (atomic publication)')
    else:
        import fcntl
        import os
        import shutil

        target = Path(uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.with_suffix(target.suffix + '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if target.exists():
                shutil.copyfile(target, local)

            def publish(src: Path) -> None:
                landing = target.with_suffix(target.suffix + '.tmp')
                shutil.copyfile(src, landing)
                os.replace(landing, target)

            yield local, publish


def normalized(con: DuckDBPyConnection, paths: list[str], name: str, cut: datetime) -> None:
    """Canonical leaf rows, including action aliases and repeated-slash names, before any history fold."""
    from disk_tree.access.parsers.r2 import parse

    if not paths:
        con.execute(f'CREATE OR REPLACE TEMP TABLE {name} AS {EMPTY}')
        return
    con.register(f'{name}_raw', parse(paths, con=con, as_of=cut))
    con.execute(f"""CREATE OR REPLACE TEMP TABLE {name} AS
        SELECT bucket, rtrim(regexp_replace(path, '/+', '/', 'g'), '/') path, op, hour,
            sum(n_ops)::BIGINT n_ops, max(last_ts) last_ts, max(last_ts) first_ts, bool_or(sampled) sampled
        FROM {name}_raw GROUP BY 1, 2, 3, 4""")


def parity(con: DuckDBPyConnection, summary: Path, full: Path) -> None:
    """The read lens's exact fields agree, including cumulative counts and global observation bounds."""
    fields = 'bucket, path, parent, depth, kind, op, sum(n_ops)::BIGINT n_ops, max(last_ts) last_ts, bool_or(sampled) sampled'
    a = f'SELECT {fields} FROM read_parquet({q(str(summary))}) GROUP BY bucket, path, parent, depth, kind, op'
    b = f'SELECT {fields} FROM read_parquet({q(str(full))}) GROUP BY bucket, path, parent, depth, kind, op'
    differing = con.execute(f'SELECT count(*) FROM (({a} EXCEPT ALL {b}) UNION ALL ({b} EXCEPT ALL {a}))').fetchone()[0]
    if differing:
        raise ValueError(f'read checkpoint: {differing} differing read-lens rows; nothing published')
    bounds_a = con.execute(f'SELECT epoch_us(min(first_ts)), epoch_us(max(last_ts)) FROM read_parquet({q(str(summary))})').fetchone()
    bounds_b = con.execute(f'SELECT epoch_us(min(last_ts)), epoch_us(max(last_ts)) FROM read_parquet({q(str(full))})').fetchone()
    if bounds_a != bounds_b:
        raise ValueError(f'read checkpoint: observation bounds differ: {bounds_a} != {bounds_b}; nothing published')


def sync(
    state: str,
    checkpoint: str,
    cut: datetime,
    out: Path,
    work: Path,
    *,
    verify: bool = False,
    mem: str = '2GB',
    threads: int = 2,
) -> dict:
    """Fold newly frozen windows once, replace active contributions, and publish only a complete result."""
    import pyarrow.parquet as pq

    from disk_tree.access.aggregate import aggregate_access_leaf
    from disk_tree.access.parsers.r2 import _hour_start, state_aggs

    from .cli import r2_state_inputs

    if cut.tzinfo is None:
        raise ValueError('read checkpoint cut must include a timezone')
    cut = cut.astimezone(timezone.utc)
    work.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint_file(checkpoint, work) as (previous, publish):
        metadata = {'version': 1, 'source': state, 'account': None, 'cursors': {}, 'windows': 0}
        if previous.exists():
            metadata = json.loads(pq.read_metadata(previous).metadata[META])
            if metadata['version'] != 1 or metadata['source'] != state:
                raise ValueError('read checkpoint schema/source mismatch')
        cursors = metadata['cursors']
        if cursors and cut < datetime.fromtimestamp(max(cursors.values()), timezone.utc) + timedelta(hours=1):
            raise ValueError('read checkpoint cannot answer a cut before its closed history; use original inputs')
        selected: dict = {}

        def select(snapshot: dict) -> list[str]:
            state_aggs(snapshot, cut)  # validate schema and reject legacy per-day history
            if metadata['account'] is not None and metadata['account'] != snapshot['account']:
                raise ValueError('read checkpoint collector account mismatch')
            frozen, active, next_cursors = [], [], dict(cursors)
            for bucket, value in sorted(snapshot['buckets'].items()):
                for hour, key in sorted(value.get('frozen', {}).items()):
                    start = _hour_start(hour)
                    # A whole-hour safety margin also covers old hourly keys and adaptively shortened windows.
                    if start.timestamp() > cursors.get(bucket, float('-inf')) and start + timedelta(hours=1) <= cut:
                        frozen.append(key)
                        next_cursors[bucket] = max(next_cursors.get(bucket, float('-inf')), start.timestamp())
                for hour, entry in sorted(value['hours'].items()):
                    if _hour_start(hour) < cut:
                        active.append(entry['agg'])
                # Frozen windows too close to this cut are still read as active inputs, not discarded.
                active.extend(key for hour, key in value.get('frozen', {}).items()
                              if _hour_start(hour).timestamp() > cursors.get(bucket, float('-inf'))
                              and _hour_start(hour) < cut < _hour_start(hour) + timedelta(hours=1))
            selected.update(account=snapshot['account'], frozen=frozen, active=active, cursors=next_cursors, snapshot=snapshot)
            return sorted(set(frozen + active))

        files = r2_state_inputs(state, cut, work / 'inputs', select=select)
        local = {str(p.relative_to(work / 'inputs')): str(p) for p in map(Path, files)}

        def verification_inputs() -> list[str]:
            # Compare the exact snapshot used above. Active keys may already have been superseded remotely;
            # use their staged bytes, and fetch only frozen keys already represented by the old checkpoint.
            archive_uri, state_key = state.rsplit('/', 1) if '/' in state else ('.', state)
            archive = Store(archive_uri)

            def read(uri: str) -> bytes:
                if uri == state:
                    return json.dumps(selected['snapshot']).encode()
                key = uri.removeprefix(archive_uri.rstrip('/') + '/') if '://' in state else Path(uri).relative_to(Path(state).parent).as_posix()
                return Path(local[key]).read_bytes() if key in local else archive.read_bytes(key)

            return r2_state_inputs(state, cut, work / 'verify-inputs', read=read)
        con = connect(threads, mem, work / 'duckdb')
        try:
            normalized(con, [local[k] for k in selected['frozen']], 'new_closed', cut)
            normalized(con, [local[k] for k in selected['active']], 'active', cut)
            old = f'SELECT * FROM read_parquet({q(str(previous))})' if previous.exists() else EMPTY
            con.execute(f"""CREATE TEMP TABLE closed AS SELECT bucket, path, op, max(hour) AS hour,
                sum(n_ops)::BIGINT n_ops, max(last_ts) last_ts, min(first_ts) first_ts, bool_or(sampled) sampled
                FROM ({old} UNION ALL BY NAME SELECT * FROM new_closed) GROUP BY bucket, path, op""")
            metadata.update(account=selected['account'], cursors=selected['cursors'], windows=metadata['windows'] + len(selected['frozen']))
            closed = work / 'closed.parquet'
            con.execute(f"""COPY (SELECT * FROM closed ORDER BY bucket, path, op) TO {q(str(closed))}
                (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{r2_read_checkpoint: {q(json.dumps(metadata, sort_keys=True))}}})""")
            floor = datetime.fromtimestamp(max(selected['cursors'].values()), timezone.utc) + timedelta(hours=1) if selected['cursors'] else None
            floor_sql = f'{q(floor.isoformat())}::TIMESTAMPTZ' if floor else 'NULL::TIMESTAMPTZ'
            con.execute(f'CREATE TEMP TABLE combined AS SELECT *, {floor_sql} history_before FROM (SELECT * FROM closed UNION ALL BY NAME SELECT * FROM active)')
            n_ops = con.execute('SELECT coalesce(sum(n_ops), 0) FROM combined').fetchone()[0]
            result = work / 'summary.parquet'
            stats = {'rows_in': 0, 'paths_out': 0, 'hours': 0, 'total_bytes_out': 0}
            if n_ops:
                stats = aggregate_access_leaf(con, f'(SELECT {LEAF}, first_ts, history_before FROM combined)', str(result), cut, read_summary=True)
                if verify:
                    all_files = verification_inputs()
                    normalized(con, all_files, 'full_leaf', cut)
                    full = work / 'full.parquet'
                    aggregate_access_leaf(con, f'(SELECT {LEAF} FROM full_leaf)', str(full), cut)
                    parity(con, result, full)
            elif verify:
                all_files = verification_inputs()
                normalized(con, all_files, 'full_leaf', cut)
                if con.execute('SELECT coalesce(sum(n_ops), 0) FROM full_leaf').fetchone()[0]:
                    raise ValueError('read checkpoint is empty but full history has reads; nothing published')
            publish(closed)
            if n_ops:
                result.replace(out)
            return {**stats, 'downloaded': len(files), 'new_frozen': len(selected['frozen']), 'active': len(selected['active']),
                    'closed_windows': metadata['windows'], 'closed_rows': con.execute('SELECT count(*) FROM closed').fetchone()[0],
                    'history_before': floor.isoformat() if floor else None, 'verified': verify}
        finally:
            con.close()


@command('r2-sync')
@option('-c', '--checkpoint', required=True, help='One atomically replaced closed-history Parquet, gs:// URL or local file')
@option('-d', '--asof', required=True, help='Scan id whose instant cuts the input')
@option('-M', '--mem', default='2GB', help='DuckDB memory limit')
@option('-o', '--out', required=True, type=Path, help='Read-summary Parquet for path-index -x')
@option('-p', '--threads', default=2, type=int, help='DuckDB threads')
@option('-s', '--state', required=True, help='Collector state.json, local file or URL')
@option('-v', '--verify', is_flag=True, help='Compare every read-lens field against full historical aggregation before publishing')
@option('-w', '--work', required=True, type=Path, help='Scratch directory')
def sync_cmd(
    checkpoint: str,
    asof: str,
    mem: str,
    out: Path,
    threads: int,
    state: str,
    verify: bool,
    work: Path,
) -> None:
    """Update a bounded R2 read-lens checkpoint; never deletes collection history."""
    from .scan_id import scan_time

    print(json.dumps(sync(state, checkpoint, scan_time(asof), out, work, verify=verify, mem=mem, threads=threads), sort_keys=True))
