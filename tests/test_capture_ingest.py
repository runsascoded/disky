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
    if key.endswith('/_SUCCESS.json') and 'FAKE_HEAD_ERROR' in os.environ:
        print(os.environ['FAKE_HEAD_ERROR'], file=sys.stderr)
        sys.exit(254)
    elif key.endswith('/_SUCCESS.json'):
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
elif name == 'dt-cloud' and args[:2] == ['hosts', 'resolve']:
    # D1 `hosts` as `FAKE_HOSTS` (JSON host → store); unset = a D1 without the table
    if 'FAKE_HOSTS' not in os.environ:
        print('hosts: this D1 has no `hosts` table', file=sys.stderr)
        sys.exit(4)
    hosts = json.loads(os.environ['FAKE_HOSTS'])
    if hosts.get(args[2]) == 'revoked':
        print(f"hosts: host '{args[2]}' was disconnected at 2026-10-16T01:00:00Z", file=sys.stderr)
        sys.exit(3)
    if args[2] not in hosts:
        print(f"hosts: no host '{args[2]}'", file=sys.stderr)
        sys.exit(3 if hosts else 1)
    print(hosts[args[2]])
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
    elif args[0] == 'interval-store':
        # the store's generation is its `CURRENT.json`; `build` writes one, `add` needs one
        store = root / args[args.index('-o') + 1].split('://', 1)[1] / 'interval-store'
        current = store / 'CURRENT.json'
        if args[1] == 'current':
            print(json.loads(current.read_text())['gen'] if current.exists() else '')
        elif args[1] == 'build':
            store.mkdir(parents=True, exist_ok=True)
            current.write_text(json.dumps({'gen': args[args.index('-g') + 1]}))
        else:
            assert args[1] == 'add' and current.exists(), args
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
        'PRIMARY_HOST': 'm3',
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


def lookups_only(trace: Path) -> list[list[str]]:
    """The run's calls, asserting each was the `hosts resolve` lookup (nothing touched the cloud)."""
    calls = [c for c in map(json.loads, trace.read_text().splitlines())]
    assert all(c[:3] == ['dt-cloud', 'hosts', 'resolve'] for c in calls), calls
    return [c[1:] for c in calls]


def test_an_unknown_host_is_refused_before_any_cloud_operation(pipeline) -> None:
    """specs/device-enrollment.md phase 1: a new Mac's capture must never publish into the primary store."""
    run, bucket, trace = pipeline
    result = run('2026-10-11T10-00-01Z', 'm1')
    unnamed = run('2026-10-11T10-00-01Z', 'm3', PRIMARY_HOST='')
    assert [(r.returncode, r.stdout, r.stderr) for r in (result, unnamed)] == [
        (3, '', "[ingest] unknown capture host 'm1': no D1 hosts table, neither PRIMARY_HOST (m3) nor in HOST_STORES (nas=nas); publishing nothing\n"),
        (3, '', "[ingest] unknown capture host 'm3': no D1 hosts table, neither PRIMARY_HOST (unset) nor in HOST_STORES (nas=nas); publishing nothing\n"),
    ]
    assert lookups_only(trace) == [['hosts', 'resolve', 'm1'], ['hosts', 'resolve', 'm3']]
    assert sorted(str(p.relative_to(bucket)) for p in bucket.rglob('*') if p.is_file() and not str(p.relative_to(bucket)).startswith('captures/')) == []


HOSTS = json.dumps({'m3': 'primary', 'nas': 'nas', 'm1': 'm1', 'old': 'revoked'})


def test_d1_hosts_route_each_capture_and_override_the_env(pipeline) -> None:
    """specs/hosts-registry.md: D1 `hosts` names each host's store; the env's `PRIMARY_HOST` / `HOST_STORES` are
    only the fallback for a D1 without the table, so an enrolled Mac publishes with no job change."""
    run, bucket, trace = pipeline
    results = [run('2026-10-11T10-00-01Z', h, FAKE_HOSTS=HOSTS, PRIMARY_HOST='', HOST_STORES='') for h in ('m3', 'nas', 'm1')]
    assert [r.returncode for r in results] == [0, 0, 0]
    assert [r.stdout.splitlines()[0].split(' date=')[1].split(' vcpu=')[0] for r in results] == [
        '2026-10-11T1000 gen=202610111000 host=m3 store=laptop d1=primary',
        '2026-10-11T1000 gen=202610111000 host=nas store=nas d1=nas',
        '2026-10-11T1000 gen=202610111000 host=m1 store=m1 d1=m1',
    ]
    assert sorted({str(p.relative_to(bucket)).split('/2026')[0] for p in bucket.rglob('meta.json')}) == [
        'snapshots/laptop', 'snapshots/m1', 'snapshots/nas',
    ]


def test_an_unregistered_or_disconnected_host_is_refused_and_a_failed_lookup_fails(pipeline) -> None:
    run, bucket, trace = pipeline
    results = [
        run('2026-10-11T10-00-01Z', 'm2', FAKE_HOSTS=HOSTS),
        run('2026-10-11T10-00-01Z', 'old', FAKE_HOSTS=HOSTS),
        run('2026-10-11T10-00-01Z', 'm3', FAKE_HOSTS='{}'),
    ]
    assert [(r.returncode, r.stdout, r.stderr) for r in results] == [
        (3, '', "[ingest] unknown capture host 'm2' (no host 'm2'); publishing nothing\n"),
        (3, '', "[ingest] unknown capture host 'old' (host 'old' was disconnected at 2026-10-16T01:00:00Z); publishing nothing\n"),
        (1, '', "[ingest] hosts lookup failed (exit 1): hosts: no host 'm3'\n"),
    ]
    assert lookups_only(trace) == [['hosts', 'resolve', 'm2'], ['hosts', 'resolve', 'old'], ['hosts', 'resolve', 'm3']]


# ── scan-run recording (SCAN_RUNS_PROFILE; specs/scan-job-wrapper.md) ─────────

CAPTURED = int(datetime(2026, 10, 11, 10, 0, 1, tzinfo=timezone.utc).timestamp())
MARKED = int(datetime(2026, 10, 11, 10, 12, 30, tzinfo=timezone.utc).timestamp())
RECORDING = {
    'SCAN_RUNS_PROFILE': '/app/infra/aws/scan-runs.json',
    'INGEST_MACHINE': 'fargate-1vcpu-8gib',
    'INGEST_SPOT': '1',
    'AWS_REGION': 'us-east-1',
    'FAKE_MARKED': '2026-10-11T10:12:30+00:00',
}


def steps(trace: Path, t0: int, t1: int) -> list[list[str | int]]:
    """The `dt-cloud scan-run` calls, after `scan-run`; a time other than the capture's two fixed ones is this run's
    clock (checked to lie within it) → `<now>`, and the ERR trap's `-e` loses its line number."""
    out: list[list[str | int]] = []
    for call in map(json.loads, trace.read_text().splitlines()):
        if call[:2] != ['dt-cloud', 'scan-run']:
            continue
        rest: list[str | int] = call[2:]
        for flag in ('-t', '-b'):
            if flag in rest:
                i = rest.index(flag) + 1
                t = int(rest[i])
                if t in (CAPTURED, MARKED):
                    rest[i] = t
                else:
                    assert t0 <= t <= t1, (t0, t, t1)
                    rest[i] = '<now>'
        if '-e' in rest:
            i = rest.index('-e') + 1
            rest[i] = re.sub(r'\(ingest\.sh:\d+\)$', '(ingest.sh:<line>)', str(rest[i]))
        out.append(rest)
    return out


def phases(*names: str) -> list[list[str | int]]:
    return [step for n in names for step in (['phase-begin', n], ['phase-end', n])]


START = ['start', '-k', 'ingest', '-K', 'nas', '-g', '202610111000', '-t', CAPTURED, '-T', '1',
         '-m', 'fargate-1vcpu-8gib', '--spot', '-R', 'us-east-1', '2026-10-11T1000']
BEFORE_JOB = [
    ['phase-begin', 'capture', '-b', CAPTURED],
    ['phase-end', 'capture', '-t', MARKED, '-o', 'capture=r2://bucket/captures/nas/root/2026-10-11T10-00-01Z/'],
    ['phase-begin', 'queue', '-b', MARKED],
    ['phase-end', 'queue', '-t', '<now>'],
]


def test_recording_sequence_with_phase_times_facts_and_outputs(pipeline) -> None:
    run, bucket, trace = pipeline
    t0 = int(time.time())
    result = run('2026-10-11T10-00-01Z', 'nas', **RECORDING)
    t1 = int(time.time()) + 1
    assert result.returncode == 0, result.stderr
    assert steps(trace, t0, t1) == [
        START, *BEFORE_JOB,
        *phases('fetch', 'path-index', 'capture-meta'),
        ['phase-begin', 'upload'],
        ['phase-end', 'upload', '-o', 'index=r2://bucket/listing/nas/2026-10-11T1000/index/202610111000/',
         '-o', 'snapshot=r2://bucket/snapshots/nas/2026-10-11T1000/'],
        *phases('index-sync'),
        ['finish', '-x', '0'],
    ]


def test_the_capture_marker_time_reads_as_aws_cli_v1_prints_it(pipeline) -> None:
    """The image's AWS CLI v1 prints `LastModified` as the header's RFC 1123 date, not ISO 8601."""
    run, _, trace = pipeline
    t0 = int(time.time())
    result = run('2026-10-11T10-00-01Z', 'nas', **{**RECORDING, 'FAKE_MARKED': 'Sun, 11 Oct 2026 10:12:30 GMT'})
    t1 = int(time.time()) + 1
    assert result.returncode == 0, result.stderr
    assert steps(trace, t0, t1)[:5] == [START, *BEFORE_JOB]


def test_an_unreadable_capture_marker_still_records_the_queue_from_the_capture_start(pipeline) -> None:
    run, _, trace = pipeline
    t0 = int(time.time())
    recording = {k: v for k, v in RECORDING.items() if k != 'FAKE_MARKED'}
    result = run('2026-10-11T10-00-01Z', 'nas', **recording, FAKE_HEAD_ERROR='An error occurred (403) when calling the HeadObject operation: Forbidden')
    t1 = int(time.time()) + 1
    assert result.returncode == 0, result.stderr
    assert steps(trace, t0, t1)[:3] == [START, ['phase-begin', 'queue', '-b', CAPTURED], ['phase-end', 'queue', '-t', '<now>']]
    assert [line for line in result.stdout.splitlines() if 'marker' in line] == [
        '[ingest] capture marker time unreadable: An error occurred (403) when calling the HeadObject operation: Forbidden',
    ]


def test_a_failed_phase_is_recorded_by_finish_with_the_failing_command(pipeline) -> None:
    run, bucket, trace = pipeline
    t0 = int(time.time())
    result = run('2026-10-11T10-00-01Z', 'nas', **RECORDING, FAKE_FAIL='path-index')
    t1 = int(time.time()) + 1
    assert result.returncode == 3
    assert steps(trace, t0, t1) == [
        START, *BEFORE_JOB, *phases('fetch'), ['phase-begin', 'path-index'],
        ['finish', '-x', '3', '-e', 'exit 3: ( cd "$app/cloud" && "$time_bin" -v -o "$work/time.txt" "$app/.venv/bin/dt-cloud" path-index -g -d "$date" -l "$work/listing/*.parquet" -P "$work/index/path-index.parquet" -o "$work/snap" ) (ingest.sh:<line>)'],
    ]
    assert list(bucket.glob('snapshots/*/*/meta.json')) == []


def test_a_failing_recorder_never_fails_the_ingest(pipeline) -> None:
    run, bucket, trace = pipeline
    result = run('2026-10-11T10-00-01Z', 'nas', **RECORDING, FAKE_RECORD_RC='1')
    assert result.returncode == 0, result.stderr
    assert sorted(str(p.relative_to(bucket)) for p in bucket.glob('snapshots/*/*/meta.json')) == ['snapshots/nas/2026-10-11T1000/meta.json']
    assert len([c for c in map(json.loads, trace.read_text().splitlines()) if c[:2] == ['dt-cloud', 'scan-run']]) == 16


def test_without_a_profile_nothing_is_recorded(pipeline) -> None:
    run, _, trace = pipeline
    result = run('2026-10-11T10-00-01Z', 'nas')
    assert result.returncode == 0, result.stderr
    calls = [c[:2] for c in map(json.loads, trace.read_text().splitlines())]
    assert sorted({tuple(c) for c in calls}) == [
        ('aws', 's3'), ('disk-tree', 'capture-meta'), ('dt-cloud', 'hosts'), ('dt-cloud', 'index-sync'), ('dt-cloud', 'path-index'),
        ('index-sync-meta', '2026-10-11T10:00:01.000Z'), ('nproc',), ('time', '-v'),
    ]


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
    result = run('2026-10-14T06-00-01Z', STATIC_GEN='2026-10-14', STATIC_BUILD='1', FAKE_FAIL='static-names', SCAN_RUNS_PROFILE='/app/p.json')
    assert result.returncode == 3
    begun = [s[1] for s in steps(trace, 0, 2 ** 40) if s[0] == 'phase-begin']
    finished = [s[:3] for s in steps(trace, 0, 2 ** 40) if s[0] == 'finish']
    assert (begun[-1], finished) == ('static-names', [['finish', '-x', '3']])


def test_a_scheduled_ingest_never_builds_the_generation(pipeline) -> None:
    run, bucket, trace = pipeline
    result = run('2026-10-14T06-00-01Z', STATIC_GEN='2026-10-14')
    assert result.returncode == 0, result.stderr
    assert static_calls(trace) == []
    assert [line for line in result.stdout.splitlines() if line.startswith('[ingest] static-names')] == [
        '[ingest] static-names: generation 2026-10-14 not built yet; skipping (STATIC_BUILD=1 builds it)',
    ]
    assert not (bucket / 'static-names').exists()


def interval_calls(trace: Path) -> list[list[str]]:
    return [[re.sub(r'^/.*/work/', '<work>/', a) for a in c[1:]] for c in map(json.loads, trace.read_text().splitlines())
            if c[:2] == ['dt-cloud', 'interval-store']]


def test_interval_store_builds_the_generation_once_then_appends_primary_scans_only(pipeline) -> None:
    run, bucket, trace = pipeline
    for stamp, host, env in [
        ('2026-10-14T06-00-01Z', 'm3', {}),                  # no generation, not a build job: skipped
        ('2026-10-14T10-00-01Z', 'm3', {'IS_BUILD': '1'}),   # the build job
        ('2026-10-14T12-00-01Z', 'nas', {}),                 # a secondary store: never on it
        ('2026-10-14T18-00-02Z', 'm3', {}),                  # appends
    ]:
        result = run(stamp, host, **env)
        assert result.returncode == 0, result.stderr
    common = ['-L', 'listing/laptop/{id}/index/{gen}/path-index.parquet', '-s', 'r2://bucket', '-o', 'r2://bucket',
              '-w', '<work>/interval-store', '-p', '1', '-M', '10GB']
    assert interval_calls(trace) == [
        ['interval-store', 'current', '-o', 'r2://bucket'],
        ['interval-store', 'current', '-o', 'r2://bucket'],
        ['interval-store', 'build', '-g', '2026-10-14T1000', *common],
        ['interval-store', 'current', '-o', 'r2://bucket'],
        ['interval-store', 'add', *common],
    ]
    assert json.loads((bucket / 'interval-store/CURRENT.json').read_text()) == {'gen': '2026-10-14T1000'}


def test_a_failed_interval_store_call_still_publishes_then_fails_the_run(pipeline) -> None:
    run, bucket, trace = pipeline
    (bucket / 'interval-store').mkdir(parents=True)
    (bucket / 'interval-store/CURRENT.json').write_text(json.dumps({'gen': 'g1'}))
    result = run('2026-10-14T06-00-01Z', FAKE_FAIL='interval-store', SCAN_RUNS_PROFILE='/app/p.json')
    assert result.returncode == 3
    calls = [c[:2] for c in map(json.loads, trace.read_text().splitlines()) if c[0] == 'dt-cloud' and c[1] != 'scan-run']
    assert calls == [['dt-cloud', 'hosts'], ['dt-cloud', 'path-index'], ['dt-cloud', 'interval-store'], ['dt-cloud', 'index-sync']]
    finished = [s[:3] for s in steps(trace, 0, 2 ** 40) if s[0] == 'finish']
    assert finished == [['finish', '-x', '3']]
