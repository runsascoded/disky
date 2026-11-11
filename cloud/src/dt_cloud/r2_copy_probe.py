"""Paired measurements of GCS-streamed and existing-local-file R2 uploads, under owned temporary keys."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from uuid import uuid4

from click import IntRange, command, option

from . import publish as pub
from .blobstore import Store


def sample(objs: list[pub.Obj], budget: int) -> list[pub.Obj]:
    """A bounded sample of multipart-sized objects; neither a source object nor the budget is exceeded."""
    selected, used = [], 0
    for obj in sorted(objs, key=lambda o: (-o.size, o.key)):
        if obj.md5 and 8 * 2**20 <= obj.size <= 256 * 2**20 and used + obj.size <= budget:
            selected.append(obj)
            used += obj.size
    if not selected:
        raise ValueError('no objects with md5 and sizes 8–256 MiB fit the probe budget')
    return selected


def probe(
    bucket: str,
    prefix: str,
    work: Path,
    *,
    budget: int,
    workers: int,
    rounds: int,
) -> dict:
    """Download once; compare AB/BA upload rounds including local checksum validation; verify HEADs and clean up.

    Only UUID-owned `tmp/r2-copy-probe/` keys are written or deleted. Source generations and pointers are read only.
    The download is reported separately: compaction already has these files on its SSD.
    """
    objs = sample(pub.list_source(bucket, [prefix.rstrip('/') + '/']), budget)
    source, local = Store(f'gs://{bucket}'), work / 'source'
    t0 = monotonic()

    def download(obj: pub.Obj) -> None:
        path = local / obj.key
        source.get(obj.key, path)
        if path.stat().st_size != obj.size or pub.file_md5(path) != obj.md5:
            raise ValueError(f'{obj.key}: downloaded source differs from the GCS inventory')

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(download, objs))
    spool_s = monotonic() - t0
    s3, r2 = pub.r2_client(), pub.r2_bucket()
    owned = f'tmp/r2-copy-probe/{uuid4().hex}'
    written, measurements = [], []
    try:
        for round_ in range(rounds):
            for mode in (('stream', 'local') if round_ % 2 == 0 else ('local', 'stream')):
                keys = {o.key: f'{owned}/{round_}/{mode}/{o.key}' for o in objs}
                written.extend(keys.values())

                def send(obj: pub.Obj) -> None:
                    src = bucket
                    if mode == 'local':
                        if pub.file_md5(local / obj.key) != obj.md5:
                            raise ValueError(f'{obj.key}: local file changed before upload')
                        src = str(local.resolve())
                    pub.copy_one(src, s3, r2, obj, dest_key=keys[obj.key])

                t0 = monotonic()
                with ThreadPoolExecutor(workers) as ex:
                    list(ex.map(send, objs))
                elapsed = monotonic() - t0
                for obj in objs:
                    if pub.head_dest(s3, r2, keys[obj.key]) != pub.Dest(size=obj.size, md5=obj.md5):
                        raise RuntimeError(f'{keys[obj.key]}: uploaded size/md5 metadata verification failed')
                measurements.append({'round': round_, 'mode': mode, 's': round(elapsed, 3),
                                     'MB_s': round(sum(o.size for o in objs) / elapsed / 1e6, 2)})
                pub.err(json.dumps(measurements[-1]))
    finally:
        for start in range(0, len(written), 1000):
            result = s3.delete_objects(Bucket=r2, Delete={'Objects': [{'Key': k} for k in written[start:start + 1000]]})
            if result.get('Errors'):
                raise RuntimeError(f'{owned}: temporary object cleanup failed: {result["Errors"]}')
    return {'source': f'gs://{bucket}/{prefix}', 'temporary_prefix': owned, 'objects': len(objs),
            'bytes_per_round': sum(o.size for o in objs), 'workers': workers, 'spool_s': round(spool_s, 3),
            'measurements': measurements, 'temporary_objects_deleted': len(written)}


@command('r2-copy-probe')
@option('-b', '--bucket', required=True, help='Read-only GCS source bucket')
@option('-j', '--workers', default=32, type=IntRange(min=1), help='Concurrent uploads, as for compaction')
@option('-m', '--max-mib', default=4096, type=IntRange(min=8), help='Maximum source sample MiB (uploaded per mode per round)')
@option('-p', '--prefix', required=True, help='Read-only source generation prefix')
@option('-r', '--rounds', default=2, type=IntRange(min=1), help='Paired rounds, alternating stream/local order')
@option('-w', '--work', type=Path, required=True, help='Scratch directory on the worker SSD')
def command_cmd(
    bucket: str,
    workers: int,
    max_mib: int,
    prefix: str,
    rounds: int,
    work: Path,
) -> None:
    """Compare the old GCS-streamed R2 copy with verified uploads of existing local outputs."""
    print(json.dumps(probe(bucket, prefix, work, budget=max_mib * 2**20, workers=workers, rounds=rounds), indent=1))
