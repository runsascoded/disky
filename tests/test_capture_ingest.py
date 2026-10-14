"""Run the deployment's ingest shell against local stand-ins for AWS and dt-cloud.

The real capture-meta command guards publication. Assert uploaded snapshots and
D1 sync identities, so dropping the time at any pipeline stage loses a scan here.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INGEST = REPO / 'infra/aws/ingest.sh'
STUB = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['FAKE_CLOUD'])
with open(os.environ['FAKE_TRACE'], 'a') as f:
    f.write(json.dumps([name, *args]) + '\n')
if name == 'nproc':
    print(1)
elif name == 'time':
    Path(args[2]).write_text('Maximum resident set size (kbytes): 1024\n')
    sys.exit(subprocess.call(args[3:]))
elif name == 'aws' and args[:2] == ['s3api', 'head-object']:
    key = args[args.index('--key') + 1]
    if key.endswith('/_SUCCESS.json'):
        print(os.environ['FAKE_MARKED'])
    elif not (root / args[args.index('--bucket') + 1] / key).exists():
        sys.exit(254)
elif name == 'aws':
    assert args[:2] == ['s3', 'cp'], args[:2]
    source, dest = args[2:4]
    def local(p):
        return root / p.split('://', 1)[1] if p.startswith('s3://') else Path(p)
    src, dst = local(source), local(dest)
    if '--recursive' in args:
        dst.mkdir(parents=True, exist_ok=True)
        for p in src.iterdir():
            if p.is_file() and ('--include' not in args or p.suffix == '.parquet'):
                shutil.copyfile(p, dst / p.name)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
elif name == 'dt-cloud':
    if args[0] == os.environ.get('FAKE_FAIL'):
        sys.exit(3)
    if args[0] == 'scan-run':
        assert os.environ['CLOUDFLARE_API_TOKEN'] == 'fixture-token'
        sys.exit(int(os.environ.get('FAKE_RECORD_RC', '0')))
    if args[0] == 'path-index':
        out = Path(args[args.index('-o') + 1])
        index = Path(args[args.index('-P') + 1])
        out.mkdir(parents=True, exist_ok=True)
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_bytes(b'index')
        (out / 'meta.json').write_text(json.dumps({'asof': args[args.index('-d') + 1], 'total_bytes': 4096}))
    elif args[0] == 'static-names':
        # a generation exists once its manifest does
        gen = Path(args[args.index('-o') + 1].split('://', 1)[1], 'static-names', args[args.index('-g') + 1])
        (root / gen).mkdir(parents=True, exist_ok=True)
        (root / gen / 'scans.json').write_text('{}')
    else:
        assert args[0] == 'index-sync', args
        # what `-m` hands index-sync at sync time: the meta's `started`
        meta = json.loads(Path(args[args.index('-m') + 1]).read_text())
        with open(os.environ['FAKE_TRACE'], 'a') as f:
            f.write(json.dumps(['index-sync-meta', meta.get('started')]) + '\n')
elif name == 'disk-tree' and args[0] == 'notify':
    sys.exit(int(os.environ.get('FAKE_NOTIFY_RC', '0')))
elif name == 'disk-tree':
    from click.testing import CliRunner
    from disk_tree.cli.base import cli
    i = args.index('-c') + 1
    args[i] = str(root / args[i].split('://', 1)[1])
    result = CliRunner().invoke(cli, args)
    print(result.output, end='')
    if result.exception:
        print(str(result.exception), file=sys.stderr)
    sys.exit(result.exit_code)
else:
    raise AssertionError(name)
'''


@pytest.fixture
def pipeline(tmp_path: Path):
    app = tmp_path / 'app'
    (app / 'cloud').mkdir(parents=True)
    binaries = app / '.venv/bin'
    binaries.mkdir(parents=True)
    for name in ('aws', 'dt-cloud', 'disk-tree', 'time', 'nproc'):
        p = binaries / name
        p.write_text(f'#!{sys.executable}\n' + STUB)
        p.chmod(0o755)
    cloud = tmp_path / 'objects'
    trace = tmp_path / 'trace.jsonl'
    env = {
        **os.environ,
        'PATH': f'{binaries}:{os.environ["PATH"]}',
        'PYTHONPATH': str(REPO / 'src'),
        'INGEST_APP_ROOT': str(app),
        'INGEST_TIME': str(binaries / 'time'),
        'FAKE_CLOUD': str(cloud),
        'FAKE_TRACE': str(trace),
        'WORK': str(tmp_path / 'work'),
        'R2_ENDPOINT_URL': 'https://r2.invalid',
        'CLOUDFLARE_ACCOUNT_ID': 'fixture-account',
        'CLOUDFLARE_API_TOKEN': 'fixture-token',
        'D1_DB_ID': 'fixture-db',
        'D1_DB_NAME': 'fixture',
        'HOST_STORES': 'nas=nas',
    }
    for key in ('DATE', 'GEN', 'STORE', 'STATIC_GEN', 'STATIC_MEM', 'STATIC_BUILD', 'SCAN_RUNS_PROFILE', 'INGEST_MACHINE', 'INGEST_SPOT', 'INGEST_SITE_URL', 'AWS_BATCH_JOB_ID', 'AWS_REGION', 'AWS_DEFAULT_REGION'):
        env.pop(key, None)

    def run(stamp: str, host: str = 'm3', **extra: str) -> subprocess.CompletedProcess:
        cap = cloud / 'bucket' / 'captures' / host / 'root' / stamp
        cap.mkdir(parents=True, exist_ok=True)
        (cap / 'shard-00000.parquet').write_bytes(b'listing')
        (cap / '_SUCCESS.json').write_text(json.dumps({
            'format': 'disk-tree-capture', 'version': 1,
            'time': stamp.replace('-00-', ':00:').replace('T10-', 'T10:').replace('T22-', 'T22:'),
        }))
        return subprocess.run(['bash', str(INGEST), f'r2://bucket/captures/{host}/root/{stamp}'], env={**env, **extra}, capture_output=True, text=True)

    return run, cloud / 'bucket', trace


def test_morning_evening_and_reingest_keep_two_independent_snapshots(pipeline) -> None:
    run, bucket, trace = pipeline
    for stamp in ('2026-10-11T10-00-01Z', '2026-10-11T22-00-02Z', '2026-10-11T10-00-01Z'):
        result = run(stamp)
        assert result.returncode == 0, result.stderr
    snapshots = bucket / 'snapshots/laptop'
    assert sorted(p.name for p in snapshots.iterdir()) == ['2026-10-11T1000', '2026-10-11T2200']
    assert [json.loads((snapshots / s / 'meta.json').read_text()) for s in ('2026-10-11T1000', '2026-10-11T2200')] == [
        {'asof': '2026-10-11T1000', 'total_bytes': 4096, 'captured_at': '2026-10-11T10:00:01Z', 'started': '2026-10-11T10:00:01.000Z'},
        {'asof': '2026-10-11T2200', 'total_bytes': 4096, 'captured_at': '2026-10-11T22:00:02Z', 'started': '2026-10-11T22:00:02.000Z'},
    ]
    syncs = [a for a in map(json.loads, trace.read_text().splitlines()) if a[:2] == ['dt-cloud', 'index-sync']]
    assert [(a[2], a[a.index('-g') + 1], a[a.index('-k') + 1], a[a.index('-s') + 1]) for a in syncs] == [
        ('2026-10-11T1000', '202610111000', 'listing/laptop/2026-10-11T1000/index/202610111000', 'primary'),
        ('2026-10-11T2200', '202610112200', 'listing/laptop/2026-10-11T2200/index/202610112200', 'primary'),
        ('2026-10-11T1000', '202610111000', 'listing/laptop/2026-10-11T1000/index/202610111000', 'primary'),
    ]
    # Each sync reads the start capture-meta stamped (`-m`), for `index_schema.started`.
    assert [a[1] for a in map(json.loads, trace.read_text().splitlines()) if a[0] == 'index-sync-meta'] == [
        '2026-10-11T10:00:01.000Z', '2026-10-11T22:00:02.000Z', '2026-10-11T10:00:01.000Z',
    ]


def test_nas_and_laptop_publish_into_separate_stores(pipeline) -> None:
    run, bucket, trace = pipeline
    for host in ('nas', 'm3'):
        result = run('2026-10-11T10-00-01Z', host)
        assert result.returncode == 0, result.stderr
    assert sorted(str(p.relative_to(bucket)) for p in bucket.glob('snapshots/*/*/meta.json')) == [
        'snapshots/laptop/2026-10-11T1000/meta.json', 'snapshots/nas/2026-10-11T1000/meta.json',
    ]
    syncs = [a for a in map(json.loads, trace.read_text().splitlines()) if a[:2] == ['dt-cloud', 'index-sync']]
    assert [a[a.index('-s') + 1] for a in syncs] == ['nas', 'primary']


def test_a_second_capture_in_the_same_minute_cannot_replace_the_first(pipeline) -> None:
    run, bucket, trace = pipeline
    first = run('2026-10-11T10-00-01Z')
    assert first.returncode == 0, first.stderr
    original = (bucket / 'snapshots/laptop/2026-10-11T1000/meta.json').read_text()
    second = run('2026-10-11T10-00-59Z')
    assert second.returncode == 1
    assert second.stderr.strip().splitlines()[-1] == 'scan slot already belongs to a different capture; refusing to replace it'
    assert (bucket / 'snapshots/laptop/2026-10-11T1000/meta.json').read_text() == original
    syncs = [a[2] for a in map(json.loads, trace.read_text().splitlines()) if a[:2] == ['dt-cloud', 'index-sync']]
    assert syncs == ['2026-10-11T1000']


def test_invalid_capture_stamps_fail_before_any_cloud_operation(pipeline) -> None:
    run, _, trace = pipeline
    result = run('not-a-stamp')
    assert (result.returncode, result.stdout, result.stderr) == (2, '', '[ingest] invalid capture stamp: expected %Y-%m-%dT%H-%M-%SZ\n')
    assert trace.exists() is False


# ── scan-run recording (SCAN_RUNS_PROFILE) ──────────────────────────────────

RECORD = ['dt-cloud', 'scan-run', 'record', '-c', '/app/infra/aws/scan-runs.json', '-K', 'nas', '-k', 'ingest', '-g', '202610111000']
CAPTURED = int(datetime(2026, 10, 11, 10, 0, 1, tzinfo=timezone.utc).timestamp())
MARKED = int(datetime(2026, 10, 11, 10, 12, 30, tzinfo=timezone.utc).timestamp())
RECORDING = {
    'SCAN_RUNS_PROFILE': '/app/infra/aws/scan-runs.json',
    'INGEST_MACHINE': 'fargate-1vcpu-8gib',
    'INGEST_SPOT': '1',
    'AWS_REGION': 'us-east-1',
    'FAKE_MARKED': '2026-10-11T10:12:30+00:00',
}


def records(trace: Path, t0: int, t1: int) -> list[list[str]]:
    """The `scan-run record` calls, after the shared prefix; a `-t` other than the capture's two fixed times is this
    run's clock (checked to lie within it) → `<now>`."""
    out = []
    for call in map(json.loads, trace.read_text().splitlines()):
        if call[:3] != RECORD[:3]:
            continue
        assert call[:len(RECORD)] == RECORD, call
        rest = call[len(RECORD):]
        assert rest[-1] == '2026-10-11T1000', rest
        rest = rest[:-1]
        if '-t' in rest:
            i = rest.index('-t') + 1
            t = int(rest[i])
            if t not in (CAPTURED, MARKED):
                assert t0 <= t <= t1, (t0, t, t1)
                rest[i] = '<now>'
            else:
                rest[i] = t
        out.append(rest)
    return out


def phase_markers(stdout: str) -> list[str]:
    return [re.sub(r': \d+s$', ': <s>s', line) for line in stdout.splitlines() if line.startswith('PHASE ')]


PHASES = ['fetch', 'path-index', 'capture-meta', 'upload', 'index-sync']


def test_recording_sequence_with_phase_times_facts_and_outputs(pipeline) -> None:
    run, bucket, trace = pipeline
    t0 = int(time.time())
    result = run('2026-10-11T10-00-01Z', 'nas', **RECORDING)
    t1 = int(time.time()) + 1
    assert result.returncode == 0, result.stderr
    assert records(trace, t0, t1) == [
        ['-S', '-t', CAPTURED, '-T', '1', '-m', 'fargate-1vcpu-8gib', '--spot', '-R', 'us-east-1'],
        ['-P', 'capture', '-t', MARKED, '-o', 'capture=r2://bucket/captures/nas/root/2026-10-11T10-00-01Z/'],
        ['-P', 'queue', '-t', '<now>'],
        ['-P', 'fetch', '-t', '<now>'],
        ['-P', 'path-index', '-t', '<now>'],
        ['-P', 'capture-meta', '-t', '<now>'],
        ['-P', 'upload', '-t', '<now>', '-o', 'index=r2://bucket/listing/nas/2026-10-11T1000/index/202610111000/',
         '-o', 'snapshot=r2://bucket/snapshots/nas/2026-10-11T1000/'],
        ['-P', 'index-sync', '-t', '<now>'],
        ['-x', '0'],
    ]
    assert phase_markers(result.stdout) == [f'PHASE {p}: <s>s' for p in PHASES]


def test_a_failed_phase_is_recorded_with_its_name_and_the_ingest_still_fails(pipeline) -> None:
    run, bucket, trace = pipeline
    t0 = int(time.time())
    result = run('2026-10-11T10-00-01Z', 'nas', **RECORDING, FAKE_FAIL='path-index')
    t1 = int(time.time()) + 1
    assert result.returncode == 3
    assert [r[:2] for r in records(trace, t0, t1)] == [['-S', '-t'], ['-P', 'capture'], ['-P', 'queue'], ['-P', 'fetch'], ['-x', '3']]
    assert records(trace, t0, t1)[-1] == ['-x', '3', '-F', 'path-index']
    assert phase_markers(result.stdout) == ['PHASE fetch: <s>s']
    assert list(bucket.glob('snapshots/*/*/meta.json')) == []


def test_a_failing_recorder_never_fails_the_ingest(pipeline) -> None:
    run, bucket, trace = pipeline
    result = run('2026-10-11T10-00-01Z', 'nas', **RECORDING, FAKE_RECORD_RC='1')
    assert result.returncode == 0, result.stderr
    assert sorted(str(p.relative_to(bucket)) for p in bucket.glob('snapshots/*/*/meta.json')) == ['snapshots/nas/2026-10-11T1000/meta.json']
    assert len([c for c in map(json.loads, trace.read_text().splitlines()) if c[:2] == ['dt-cloud', 'scan-run']]) == 9


def test_without_a_profile_nothing_is_recorded(pipeline) -> None:
    run, _, trace = pipeline
    result = run('2026-10-11T10-00-01Z', 'nas')
    assert result.returncode == 0, result.stderr
    calls = [c[:2] for c in map(json.loads, trace.read_text().splitlines())]
    assert sorted({tuple(c) for c in calls}) == [
        ('aws', 's3'), ('disk-tree', 'capture-meta'), ('dt-cloud', 'index-sync'), ('dt-cloud', 'path-index'),
        ('index-sync-meta', '2026-10-11T10:00:01.000Z'), ('nproc',), ('time', '-v'),
    ]
    assert phase_markers(result.stdout) == [f'PHASE {p}: <s>s' for p in PHASES]


# ── failure notifications (`disk-tree notify`) ──────────────────────────────

def notifies(trace: Path) -> list[list[str]]:
    return [c[1:] for c in map(json.loads, trace.read_text().splitlines()) if c[:2] == ['disk-tree', 'notify']]


def test_a_failure_notifies_with_the_phase_and_a_link_to_the_run(pipeline) -> None:
    run, _, trace = pipeline
    site = {'INGEST_SITE_URL': 'https://disk.example', 'AWS_BATCH_JOB_ID': 'job-1'}
    nas = run('2026-10-11T10-00-01Z', 'nas', **site, FAKE_FAIL='path-index')
    laptop = run('2026-10-11T22-00-02Z', 'm3', **site, FAKE_FAIL='index-sync')
    assert (nas.returncode, laptop.returncode) == (3, 3)
    assert notifies(trace) == [
        ['notify', '-p', '1', '-t', 'disky: nas ingest failed', '-u', 'https://disk.example/scans/job-1?store=nas', '-l', 'run',
         'scan 2026-10-11T1000 failed in path-index (exit 3)'],
        ['notify', '-p', '1', '-t', 'disky: laptop ingest failed', '-u', 'https://disk.example/scans/job-1', '-l', 'run',
         'scan 2026-10-11T2200 failed in index-sync (exit 3)'],
    ]


def test_success_never_notifies_and_a_failing_notifier_keeps_the_exit_code(pipeline) -> None:
    run, _, trace = pipeline
    ok = run('2026-10-11T10-00-01Z')
    failed = run('2026-10-11T22-00-02Z', FAKE_FAIL='path-index', FAKE_NOTIFY_RC='2')
    assert (ok.returncode, failed.returncode) == (0, 3)
    assert notifies(trace) == [['notify', '-p', '1', '-t', 'disky: laptop ingest failed', 'scan 2026-10-11T2200 failed in path-index (exit 3)']]


def static_calls(trace: Path) -> list[list[str]]:
    return [a[1:] for a in map(json.loads, trace.read_text().splitlines()) if a[:2] == ['dt-cloud', 'static-names']]


def test_static_names_builds_the_generation_once_then_appends_primary_scans_only(pipeline) -> None:
    run, bucket, trace = pipeline
    for stamp, host in [('2026-10-14T06-00-01Z', 'm3'), ('2026-10-14T10-00-01Z', 'nas'), ('2026-10-14T18-00-02Z', 'm3')]:
        result = run(stamp, host, STATIC_GEN='2026-10-14', STATIC_BUILD='1')
        assert result.returncode == 0, result.stderr
    common = ['-g', '2026-10-14', '-L', 'listing/laptop/{id}/index/{gen}/path-index.parquet', '-s', 'r2://bucket', '-o', 'r2://bucket']
    calls = [[re.sub(r'^/.*/work/', '<work>/', a) for a in c] for c in static_calls(trace)]
    assert calls == [
        ['static-names', 'build', *common, '-w', '<work>/static-names', '-p', '1', '-M', '5GB', '-x', 'off'],
        ['static-names', 'add', *common, '-w', '<work>/static-names', '-p', '1', '-M', '5GB'],
    ]
    assert sorted(p.relative_to(bucket).as_posix() for p in (bucket / 'static-names').rglob('*') if p.is_file()) == [
        'static-names/2026-10-14/scans.json',
    ]


def test_static_names_failure_fails_the_ingest_in_its_phase(pipeline) -> None:
    run, _, trace = pipeline
    result = run('2026-10-14T06-00-01Z', STATIC_GEN='2026-10-14', STATIC_BUILD='1', FAKE_FAIL='static-names')
    assert result.returncode == 3
    assert phase_markers(result.stdout) == [f'PHASE {p}: <s>s' for p in PHASES]
    assert notifies(trace) == [[
        'notify', '-p', '1', '-t', 'disky: laptop ingest failed', 'scan 2026-10-14T0600 failed in static-names (exit 3)',
    ]]



def test_a_scheduled_ingest_never_builds_the_generation(pipeline) -> None:
    run, bucket, trace = pipeline
    result = run('2026-10-14T06-00-01Z', STATIC_GEN='2026-10-14')
    assert result.returncode == 0, result.stderr
    assert static_calls(trace) == []
    assert [line for line in result.stdout.splitlines() if line.startswith('[ingest] static-names')] == [
        '[ingest] static-names: generation 2026-10-14 not built yet; skipping (STATIC_BUILD=1 builds it)',
    ]
    assert not (bucket / 'static-names').exists()

