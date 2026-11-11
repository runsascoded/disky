"""Additive Nextflow ownership evidence, never a retention/deletion decision.

The trace describes observed tasks, not a complete census of everything a session may resume. Keep its exact bytes
beside a versioned receipt, under store/run/head-attempt identity, without changing the legacy report links. A missing,
malformed or abbreviated trace remains explicit evidence of an incomplete observation. No directory is discovered by
expanding a shortened hash, and a CACHED row is another reference to its workdir, not proof of exclusive ownership.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from urllib.parse import urlsplit

from .blobstore import Store, scheme
from .image_sha import stamp
from .scan_profile import ScanProfile

VERSION = 1
PREFIX = 'ownership/v1'
REQUIRED_FIELDS = ('task_id', 'hash', 'native_id', 'name', 'status', 'exit', 'workdir')
TERMINAL = {'COMPLETED', 'CACHED', 'FAILED', 'ABORTED'}
KNOWN_STATUS = TERMINAL | {'NEW', 'SUBMITTED', 'RUNNING'}
SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
TASK_DIR = re.compile(r'[0-9a-f]{2}/[0-9a-f]{30}')
MAX_PARSE_BYTES = 256 * 1024
MAX_TASK_ROWS = 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class TraceSnapshot:
    path: Path | None
    size: int | None
    digest: str | None
    sample: bytes | None
    terminated: bool | None
    error: str | None = None
    truncated: bool = False
    source_size_at_open: int | None = None


def snapshot_trace(trace: Path, dest: Path) -> TraceSnapshot:
    """Capture at most MAX_SNAPSHOT_BYTES (+ one-byte probe), with a prefix-only digest when oversized."""
    try:
        src = trace.open('rb')
    except FileNotFoundError:
        return TraceSnapshot(None, None, None, None, None, 'trace-missing')
    except OSError:
        return TraceSnapshot(None, None, None, None, None, 'trace-unreadable')
    digest, size, sample, last = sha256(), 0, bytearray(), b''
    source_size = os.fstat(src.fileno()).st_size
    truncated = False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src, dest.open('xb') as out:
        while size < MAX_SNAPSHOT_BYTES:
            chunk = src.read(min(CHUNK_BYTES, MAX_SNAPSHOT_BYTES - size))
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            sample.extend(chunk[:max(0, MAX_PARSE_BYTES - len(sample))])
            last = chunk[-1:]
        if size == MAX_SNAPSHOT_BYTES:
            truncated = bool(src.read(1))
    return TraceSnapshot(dest, size, digest.hexdigest(), bytes(sample), last == b'\n', truncated=truncated,
                         source_size_at_open=source_size)


def prefix(
    deployment: str,
    store: str,
    run_id: str | None,
    attempt: str,
) -> str:
    parts = (deployment, store, run_id or '_unrecorded', attempt)
    if any(not SEGMENT.fullmatch(p) for p in (deployment, store, attempt)) or (run_id is not None and not SEGMENT.fullmatch(run_id)):
        raise ValueError('ownership receipt identity must use nonempty safe path segments')
    return '/'.join((PREFIX, *parts))


def workdir(raw: str, task_root: str) -> dict:
    """Accept only the full task path under the configured work store; preserve every unresolved spelling."""
    root_url = urlsplit(task_root)
    known = (not root_url.scheme and Path(task_root).is_absolute()) or (
        root_url.scheme == 'gs' and bool(root_url.netloc) and not root_url.query and not root_url.fragment)
    pre = task_root.rstrip('/') + '/'
    path = raw.rstrip('/')
    key = path[len(pre):] if known and path.startswith(pre) else None
    if key is not None and TASK_DIR.fullmatch(key):
        return {'state': 'full', 'raw': raw, 'uri': path, 'relative_to_task_root': key}
    return {'state': 'missing' if raw in ('', '-') else 'unresolved', 'raw': raw, 'uri': None, 'relative_to_task_root': None}


def observe_trace(snapshot: TraceSnapshot, task_root: str) -> dict:
    """Parse exactly observed TSV rows. Well-formed rows still do not establish a complete session inventory."""
    out: dict = {'state': 'missing', 'bytes': None, 'sha256': None, 'terminated': None, 'fields': [],
                 'issues': [], 'tasks': [], 'workdirs': [], 'inventory_proof': 'observed-trace-only',
                 'parse_limits': {'bytes': MAX_PARSE_BYTES, 'task_rows': MAX_TASK_ROWS},
                 'snapshot_limit_bytes': MAX_SNAPSHOT_BYTES, 'snapshot_truncated': snapshot.truncated,
                 'source_size_at_open': snapshot.source_size_at_open, 'sha256_scope': None}
    raw = snapshot.sample
    if raw is None:
        out['state'] = 'missing' if snapshot.error == 'trace-missing' else 'unavailable'
        out['issues'] = [{'line': None, 'code': snapshot.error}]
        return out
    assert snapshot.size is not None
    out.update(bytes=snapshot.size, sha256=snapshot.digest, terminated=snapshot.terminated,
               sha256_scope='snapshot-prefix' if snapshot.truncated else 'snapshot')
    if snapshot.truncated:
        out['issues'].append({'line': None, 'code': 'snapshot-byte-limit'})
    if not raw:
        out.update(state='incomplete', issues=[{'line': None, 'code': 'trace-empty'}])
        return out
    if snapshot.size > len(raw):
        out['issues'].append({'line': None, 'code': 'parse-byte-limit'})
        # Do not parse a partial sampled row or mistake a cut UTF-8 character for corrupt source bytes.
        if not raw.endswith(b'\n'):
            raw = raw[:raw.rfind(b'\n') + 1]
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('utf-8', errors='replace')
        out['issues'].append({'line': None, 'code': 'invalid-utf8'})
    lines = text.splitlines()
    if not lines:
        out.update(state='incomplete', issues=[*out['issues'], {'line': 1, 'code': 'header-unavailable'}])
        return out
    fields = lines[0].split('\t')
    out['fields'] = fields
    if len(set(fields)) != len(fields):
        out['issues'].append({'line': 1, 'code': 'duplicate-fields'})
    missing = [k for k in REQUIRED_FIELDS if k not in fields]
    if missing:
        out['issues'].append({'line': 1, 'code': 'missing-fields', 'fields': missing})
    if not out['terminated']:
        out['issues'].append({'line': len(lines), 'code': 'unterminated-line'})
    refs: dict[str, dict] = {}
    for line, content in enumerate(lines[1:], start=2):
        if len(out['tasks']) == MAX_TASK_ROWS:
            out['issues'].append({'line': line, 'code': 'parse-row-limit'})
            break
        values = content.split('\t')
        row = dict(zip(fields, values)) if len(values) == len(fields) and len(set(fields)) == len(fields) else None
        wd = workdir(row.get('workdir', '') if row else '', task_root)
        status = row.get('status', '') if row else ''
        task = {'line': line, 'fields': {k: row[k] for k in REQUIRED_FIELDS if k in row} if row else None,
                'cached': status == 'CACHED' if status in KNOWN_STATUS else None,
                'workdir': wd}
        out['tasks'].append(task)
        if row is None:
            out['issues'].append({'line': line, 'code': 'row-width-or-schema'})
        else:
            if any(row.get(k, '') in ('', '-') for k in ('task_id', 'name', 'status')):
                out['issues'].append({'line': line, 'code': 'missing-task-identity'})
            if status not in KNOWN_STATUS:
                out['issues'].append({'line': line, 'code': 'unknown-status'})
            elif status not in TERMINAL:
                out['issues'].append({'line': line, 'code': 'nonterminal-task'})
        if wd['state'] != 'full':
            out['issues'].append({'line': line, 'code': 'workdir-' + wd['state']})
        else:
            ref = refs.setdefault(wd['uri'], {'uri': wd['uri'], 'relative_to_task_root': wd['relative_to_task_root'],
                                            'task_lines': [], 'cached_lines': []})
            ref['task_lines'].append(line)
            if task['cached']:
                ref['cached_lines'].append(line)
    if not out['tasks']:
        out['issues'].append({'line': None, 'code': 'no-task-rows'})
    out['workdirs'] = [refs[k] for k in sorted(refs)]
    out['state'] = 'incomplete' if out['issues'] else 'observed'
    return out


def context(
    profile: ScanProfile,
    profile_json: str,
    scan: str,
    gen: str,
    session: str,
    run_id: str | None,
    run_id_source: str,
    record_run_id: str,
    attempt: str,
    started_at: str,
    env: Mapping[str, str],
    task_work_root: str,
) -> dict:
    """Capture source/config facts before launching. A configured image tag is explicitly not a digest pin."""
    image = env.get('DT_SCAN_IMAGE') or profile.executor.image
    source = stamp(env)
    return {
        'version': VERSION,
        'identity': {'deployment': profile.deployment, 'store': profile.store, 'scan': scan, 'gen': gen,
                     'run_id': run_id, 'run_id_source': run_id_source, 'record_run_id': record_run_id,
                     'head_attempt': attempt, 'session': session},
        'source': {'profile_sha256': sha256(profile_json.encode()).hexdigest(), 'git_sha': source.sha, 'git_dirty': source.dirty,
                   'task_image': image, 'task_image_pinned': bool(image and re.search(r'@sha256:[0-9a-f]{64}$', image)),
                   'executor': profile.executor.kind, 'work_root': profile.work, 'data_root': profile.data,
                   'task_work_root': task_work_root,
                   'source_uris': [s.uri for s in profile.sources]},
        'started_at': started_at,
    }


def receipt(
    ctx: dict,
    snapshot: TraceSnapshot,
    finished_at: str,
    exit_code: int,
    signal: int | None,
    reports: list[str],
) -> dict:
    """A finalized observation, including failed/stopped runs; no output-parity or deletion authority is inferred."""
    i = ctx['identity']
    root = prefix(i['deployment'], i['store'], i['run_id'], i['head_attempt'])
    return {**ctx, 'finished_at': finished_at,
            'outcome': {'status': 'terminated' if signal is not None else 'succeeded' if exit_code == 0 else 'failed',
                        'exit_code': exit_code, 'signal': signal},
            'trace': {**observe_trace(snapshot, ctx['source']['task_work_root']), 'key': f'{root}/trace.tsv' if snapshot.path is not None else None},
            'legacy_reports': {'keys': reports, 'mutable': True},
            'published_outputs_verified': False, 'deletion_authorized': False}


def _create(
    store: Store,
    key: str,
    src: BinaryIO,
    size: int,
) -> None:
    """Create only on atomic backends. Never use Store.create's check-then-write fallback on other schemes."""
    if not store.url:
        dest = Path(store.root) / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=dest.parent, prefix='.ownership-', delete=False) as f:
            tmp = Path(f.name)
            try:
                remaining = size
                while remaining:
                    chunk = src.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError('ownership snapshot became shorter before publication')
                    f.write(chunk)
                    remaining -= len(chunk)
                if src.read(1):
                    raise ValueError('ownership snapshot grew before publication')
                f.flush()
                os.fsync(f.fileno())
                os.link(tmp, dest)
            finally:
                tmp.unlink()
    elif scheme(store.root) == 'gs':
        from google.api_core.exceptions import PreconditionFailed
        from google.cloud.storage import Client

        bucket, _, path = store._p(key).partition('/')
        try:
            Client().bucket(bucket).blob(path).upload_from_file(src, size=size, if_generation_match=0)
        except PreconditionFailed as e:
            raise FileExistsError(key) from e
    else:
        raise ValueError('ownership receipts require atomic create: a local work root or gs://')


def _put(
    store: Store,
    key: str,
    src: BinaryIO,
    size: int,
    digest: str,
) -> None:
    try:
        _create(store, key, src, size)
    except FileExistsError:
        pass  # An identical retry is legitimate; the full digest/size check below refuses a conflicting object.
    actual, count = sha256(), 0
    with store.open(key) as held:
        while count <= size:
            chunk = held.read(min(CHUNK_BYTES, size + 1 - count))
            if not chunk:
                break
            actual.update(chunk)
            count += len(chunk)
    if count != size or actual.hexdigest() != digest:
        raise ValueError(f'ownership receipt conflicts with existing object or readback: {key}')


def publish(store: Store, doc: dict, snapshot: TraceSnapshot) -> str:
    """Immutable trace first, receipt last. An identical retry succeeds; any conflicting existing bytes are refused."""
    i = doc['identity']
    key = prefix(i['deployment'], i['store'], i['run_id'], i['head_attempt']) + '/receipt.json'
    if (doc['trace']['sha256'], doc['trace']['bytes']) != (snapshot.digest, snapshot.size):
        raise ValueError('ownership trace bytes do not match the receipt')
    if snapshot.path is not None:
        assert snapshot.size is not None and snapshot.digest is not None
        if doc['trace']['key'] != key.removesuffix('receipt.json') + 'trace.tsv':
            raise ValueError('ownership trace key does not match the receipt identity')
        with snapshot.path.open('rb') as src:
            _put(store, doc['trace']['key'], src, snapshot.size, snapshot.digest)
    raw = (json.dumps(doc, sort_keys=True, separators=(',', ':')) + '\n').encode()
    _put(store, key, BytesIO(raw), len(raw), sha256(raw).hexdigest())
    return key
