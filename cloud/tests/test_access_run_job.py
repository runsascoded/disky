"""GCS poller lifecycle: one run, no snapshot, no duplicate Slack alert."""
import json
import os
from pathlib import Path
from subprocess import run

import pytest


@pytest.mark.parametrize(('rc', 'lease', 'expected_status'), [(0, 'taken', 'done'), (0, 'held', 'nop'), (1, None, 'failed'), (3, 'taken', 'failed')])
def test_access_job_lifecycle(tmp_path, rc, lease, expected_status):
    script = Path(__file__).resolve().parents[2] / 'job' / 'access-run.sh'
    if not script.exists():
        pytest.skip('GCS deployment entrypoint only')
    executable = tmp_path / 'dt-cloud'
    executable.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
p=Path(os.environ['CALLS'])
with p.open('a') as f: f.write(json.dumps(args)+'\\n')
if args==['image-sha']: print('test-sha')
if args[:2]==['access','ingest']:
 lease=os.environ.get('LEASE')
 if lease: print(json.dumps({'lease':lease,'buckets':[],'stalled':['b'] if os.environ['RC']=='3' else []}))
 sys.exit(int(os.environ['RC']))
''')
    executable.chmod(0o755)
    (tmp_path / 'disk-tree').symlink_to(executable)
    calls = tmp_path / 'calls.jsonl'
    result = run(['bash', str(script)], cwd=tmp_path, env={
        **os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}', 'CALLS': str(calls), 'RC': str(rc),
        'LEASE': lease or '', 'ACCESS_RUN_ID': '2026-11-09T0820', 'BATCH_JOB_UID': 'uid',
        'SITE_URL': 'https://gcs.oa.dev', 'PARALLEL': '1',
    }, capture_output=True, text=True)
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    note = 'skipped (busy): ingest lease held by another run' if lease == 'held' else 'ingest lease taken; all configured buckets checked' if rc == 0 else ''
    expected = [
        ['image-sha'], ['scan-run', 'start', '-B', '-k', 'access-ingest', '2026-11-09T0820'],
        ['scan-run', 'phase-begin', 'access-ingest'], ['access', 'ingest', '-j', '-x', '-P', '1', '-M', '48GB'],
    ]
    if expected_status == 'nop': expected.append(['scan-run', 'nop'])
    expected.append(['scan-run', 'phase-end', '-x', str(rc), '-q', note, 'access-ingest'])
    if rc:
        what = 'stalled delivery: b' if rc == 3 else f'ingest failed (exit {rc})'
        expected.append(['notify', '-t', 'gcs access logs', f'{what} · https://gcs.oa.dev/scans/uid'])
    expected.append(['scan-run', 'finish', '-x', str(rc)])
    assert result.returncode == rc, result.stderr
    assert actual == expected


@pytest.mark.parametrize(('pin', 'trigger'), [('', 'manual'), ('1', 'schedule')])
def test_access_submitter_records_its_actual_trigger(pin, trigger):
    script = Path(__file__).resolve().parents[2] / 'job' / 'access-submit.sh'
    if not script.exists():
        pytest.skip('GCS deployment submitter only')
    result = run(['bash', str(script)], env={**os.environ, 'DRY': '1', 'PIN': pin, 'DT_TRIGGER': 'manual'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    task = json.loads(result.stdout)['taskGroups'][0]['taskSpec']
    assert (task['environment']['variables']['DT_TRIGGER'], task['runnables'][0]['container']['commands']) == (
        trigger, ['-c', 'exec dt-cloud scan-run supervise -H 60 -- bash job/access-run.sh'],
    )
