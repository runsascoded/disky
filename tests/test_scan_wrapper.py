"""`infra/aws/scan.sh`: a capture → its store's scan profile → `dt-cloud scan head` (then the meta store's after a
primary scan), with the run's facts and pre-job phases in the head's env. Fake `dt-cloud` / `aws` record what they
were asked and the env they got."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CAPTURE = 'r2://disk-tree/captures/{host}/root/2026-10-18T10-14-45Z'
STUB = r'''
import json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
KEYS = ('DT_CAPTURE', 'SNAP_ID', 'GEN', 'DT_RUN_ID', 'DT_RUN_KIND', 'DT_RUN_MACHINE', 'DT_RUN_SPOT', 'DT_RUN_REGION', 'DT_RUN_AT',
        'DT_RUN_TASKS', 'DT_PRE_PHASES', 'R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'DISKY_R2_ENDPOINT_URL', 'AWS_DEFAULT_REGION', 'DT_TRIGGER')
with open(os.environ['FAKE_TRACE'], 'a') as f:
    f.write(json.dumps([name, *args, {k: os.environ[k] for k in KEYS if k in os.environ}]) + '\n')
if name == 'aws' and args[:2] == ['s3', 'cp']:
    print(os.environ.get('FAKE_MARKER', '{}'))
elif name == 'aws':
    print('2026-10-18T10:20:45+00:00')
elif args[:2] == ['hosts', 'resolve']:
    stores = {'m3': 'primary', 'nas': 'nas'}
    if args[2] not in stores:
        print(f"hosts: no host '{args[2]}'", file=sys.stderr)
        sys.exit(3)
    print(stores[args[2]])
elif args[:2] == ['scan', 'head']:
    profile = Path(args[-1])
    with open(os.environ['FAKE_TRACE'], 'a') as f:
        f.write(json.dumps(['profile', profile.name, profile.read_text().count('@STORE@'), 'store: "nas"' in profile.read_text()]) + '\n')
'''


@pytest.fixture
def scan(tmp_path):
    bins = tmp_path / 'bin'
    bins.mkdir()
    for name in ('dt-cloud', 'aws'):
        (bins / name).write_text(f'#!{sys.executable}\n' + STUB)
        (bins / name).chmod(0o755)
    (tmp_path / 'app/cloud').mkdir(parents=True)
    trace = tmp_path / 'trace.jsonl'

    def run(host: str, **extra: str) -> tuple[subprocess.CompletedProcess, list]:
        trace.write_text('')
        env = {**{k: v for k, v in os.environ.items() if k != 'DT_TRIGGER'}, **extra, 'PATH': f'{bins}:{os.environ["PATH"]}', 'FAKE_TRACE': str(trace), 'INGEST_APP_ROOT': str(tmp_path / 'app'),
               'SCAN_PROFILES': str(REPO / 'job/profiles'), 'SCAN_NF_DIR': '/nf', 'WORK': str(tmp_path / 'work'),
               'R2_ENDPOINT_URL': 'https://r2.example', 'AWS_ACCESS_KEY_ID': 'kid', 'AWS_SECRET_ACCESS_KEY': 'sec',
               'AWS_BATCH_JOB_ID': 'job-1', 'AWS_REGION': 'us-east-1', 'INGEST_MACHINE': 'fargate-arm64-2vcpu-16gib', 'INGEST_SPOT': '1'}
        r = subprocess.run(['bash', str(REPO / 'infra/aws/scan.sh'), CAPTURE.format(host=host)], env=env, capture_output=True, text=True)
        return r, [json.loads(line) for line in trace.read_text().splitlines()]
    return run


def heads(calls: list) -> list:
    """Each head: the profile it got (name, unfilled `@STORE@`s, whether it names store nas) and its env."""
    out, it = [], iter(calls)
    for c in it:
        if c[:3] == ['dt-cloud', 'scan', 'head']:
            out.append((next(it)[1:], c[-1]))
    return out


def test_a_primary_capture_runs_the_laptop_profile_then_the_meta_store(scan):
    r, calls = scan('m3')
    assert r.returncode == 0, r.stderr
    t = 1792318485  # 2026-10-18T10:14:45Z
    base = {'R2_ENDPOINT': 'https://r2.example', 'R2_ACCESS_KEY_ID': 'kid', 'R2_SECRET_ACCESS_KEY': 'sec', 'DISKY_R2_ENDPOINT_URL': 'https://r2.example',
            'AWS_DEFAULT_REGION': 'auto',
            'SNAP_ID': '2026-10-18T1014', 'GEN': '202610181014', 'DT_RUN_MACHINE': 'fargate-arm64-2vcpu-16gib', 'DT_RUN_SPOT': '1',
            'DT_RUN_REGION': 'us-east-1', 'DT_RUN_TASKS': '1'}
    (laptop, env1), (meta, env2) = heads(calls)
    assert (laptop, meta) == (['m3.yml', 0, False], ['m3-meta.yml', 0, False])
    pre = json.loads(env1.pop('DT_PRE_PHASES'))
    assert pre == [{'name': 'capture', 'began': t, 'ended': t + 360, 'outputs': [f'capture={CAPTURE.format(host="m3")}/']},
                   {'name': 'queue', 'began': t + 360, 'ended': pre[1]['ended']}]
    assert env1 == {**base, 'DT_CAPTURE': CAPTURE.format(host='m3'), 'DT_RUN_ID': 'job-1', 'DT_RUN_KIND': 'ingest', 'DT_RUN_AT': str(t)}
    assert {**env2, 'DT_RUN_AT': '<now>'} == {**base, 'DT_RUN_ID': 'job-1-meta', 'DT_RUN_KIND': 'meta', 'DT_RUN_AT': '<now>'}
    assert [line for line in r.stdout.splitlines() if line.startswith('[scan]')] == [
        '[scan] capture=captures/m3/root/2026-10-18T10-14-45Z scan=2026-10-18T1014 gen=202610181014 host=m3 store=primary profile=m3.yml trigger=unset']
    assert 'sec' not in r.stdout + r.stderr


def test_another_store_fills_the_host_profile_and_skips_the_meta_store(scan):
    r, calls = scan('nas')
    assert r.returncode == 0, r.stderr
    assert [h[0] for h in heads(calls)] == [['m3-nas.yml', 0, True]]


def test_an_unknown_host_runs_nothing(scan):
    r, calls = scan('stranger')
    assert (r.returncode, heads(calls), r.stderr.strip()) == (3, [], "[scan] unknown capture host 'stranger' (no host 'stranger'); publishing nothing")


def test_the_profiles_name_the_sites_static_generations():
    """The laptop profile's static-names generation is the site's `STATIC_GEN`, the meta profile's its `STORES_JSON` meta
    `STATIC_GEN` (as the job env's, `test_capture_ingest.py`)."""
    import re

    import yaml
    toml = (REPO / 'site/wrangler.toml').read_text()
    site = re.search(r'^STATIC_GEN = "([^"]*)"', toml, re.M)[1]
    meta = json.loads(re.search(r"^STORES_JSON = '(.*)'$", toml, re.M)[1])['meta']['vars']['STATIC_GEN']
    gens = [yaml.safe_load((REPO / f'job/profiles/{p}.yml').read_text())['options']['static-names']['gen'] for p in ('m3', 'm3-meta')]
    assert gens == [site, meta]


@pytest.mark.parametrize('marker,extra,trigger', [
    ('{"files": 3, "trigger": "schedule"}', {}, 'schedule'),
    ('{"files": 3, "trigger": "manual"}', {}, 'manual'),
    ('{"files": 3, "trigger": "manual"}', {'DT_TRIGGER': 'manual'}, 'manual'),
    ('{"files": 3, "trigger": "schedule"}', {'DT_TRIGGER': 'manual'}, 'manual'),
    ('{"files": 3}', {}, None),
    ('{"files": 3, "trigger": "cron; rm -rf /"}', {}, None),
])
def test_the_trigger_comes_from_a_hand_submit_else_the_capture_marker(scan, marker, extra, trigger):
    """`DT_TRIGGER`: a hand submit's wins; else the marker's `trigger` (`schedule` / `manual` only); else none, in both heads."""
    r, calls = scan('m3', FAKE_MARKER=marker, **extra)
    assert r.returncode == 0, r.stderr
    assert [env.get('DT_TRIGGER') for _, env in heads(calls)] == [trigger, trigger]
