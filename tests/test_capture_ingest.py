"""Run the deployment's ingest shell against local stand-ins for AWS and dt-cloud.

The real capture-meta command guards publication. Assert uploaded snapshots and
D1 sync identities, so dropping the time at any pipeline stage loses a scan here.
"""
import json
import os
import subprocess
import sys
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
    if args[0] == 'path-index':
        out = Path(args[args.index('-o') + 1])
        index = Path(args[args.index('-P') + 1])
        out.mkdir(parents=True, exist_ok=True)
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_bytes(b'index')
        (out / 'meta.json').write_text(json.dumps({'asof': args[args.index('-d') + 1], 'total_bytes': 4096}))
    else:
        assert args[0] == 'index-sync', args
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
    for key in ('DATE', 'GEN', 'STORE'):
        env.pop(key, None)

    def run(stamp: str, host: str = 'm3') -> subprocess.CompletedProcess:
        cap = cloud / 'bucket' / 'captures' / host / 'root' / stamp
        cap.mkdir(parents=True, exist_ok=True)
        (cap / 'shard-00000.parquet').write_bytes(b'listing')
        (cap / '_SUCCESS.json').write_text(json.dumps({
            'format': 'disk-tree-capture', 'version': 1,
            'time': stamp.replace('-00-', ':00:').replace('T10-', 'T10:').replace('T22-', 'T22:'),
        }))
        return subprocess.run(['bash', str(INGEST), f'r2://bucket/captures/{host}/root/{stamp}'], env=env, capture_output=True, text=True)

    return run, cloud / 'bucket', trace


def test_morning_evening_and_reingest_keep_two_independent_snapshots(pipeline) -> None:
    run, bucket, trace = pipeline
    for stamp in ('2026-10-11T10-00-01Z', '2026-10-11T22-00-02Z', '2026-10-11T10-00-01Z'):
        result = run(stamp)
        assert result.returncode == 0, result.stderr
    snapshots = bucket / 'snapshots/laptop'
    assert sorted(p.name for p in snapshots.iterdir()) == ['2026-10-11T1000', '2026-10-11T2200']
    assert [json.loads((snapshots / s / 'meta.json').read_text()) for s in ('2026-10-11T1000', '2026-10-11T2200')] == [
        {'asof': '2026-10-11T1000', 'total_bytes': 4096, 'captured_at': '2026-10-11T10:00:01Z'},
        {'asof': '2026-10-11T2200', 'total_bytes': 4096, 'captured_at': '2026-10-11T22:00:02Z'},
    ]
    syncs = [a for a in map(json.loads, trace.read_text().splitlines()) if a[:2] == ['dt-cloud', 'index-sync']]
    assert [(a[2], a[a.index('-g') + 1], a[a.index('-k') + 1], a[a.index('-s') + 1]) for a in syncs] == [
        ('2026-10-11T1000', '202610111000', 'listing/laptop/2026-10-11T1000/index/202610111000', 'primary'),
        ('2026-10-11T2200', '202610112200', 'listing/laptop/2026-10-11T2200/index/202610112200', 'primary'),
        ('2026-10-11T1000', '202610111000', 'listing/laptop/2026-10-11T1000/index/202610111000', 'primary'),
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
