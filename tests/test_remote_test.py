"""Missing test paths fail before SSH; legitimate filtered-empty suites keep their existing behavior."""
import os
from pathlib import Path
from subprocess import run

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'remote-test'


@pytest.mark.parametrize(('codes', 'expected'), [
    ([0, 0], ['0', '1']),
    ([0, 5], ['0', '1']),
    ([5, 0], ['0', '1']),
    ([2, 0], ['2', '1']),
])
def test_remote_suite_preserves_filtered_empty_and_failed_results(tmp_path, codes, expected):
    # Execute the actual function embedded in the remote heredoc, with pytest's exit statuses injected.
    # This needs neither SSH nor a disposable checkout and exercises aggregation across successive suites.
    source = SCRIPT.read_text()
    suite = 'suite() {' + source.split('suite() {', 1)[1].split('start=', 1)[0]
    suite = suite.replace('\\$', '$')
    shell = '\n'.join([
        'rc=0; ran=0',
        'pytest() { return "$1"; }',
        suite,
        *(f'suite . {code}' for code in codes),
        'printf "%s\\n" "$rc" "$ran"',
    ])
    result = run(['bash', '-c', shell], cwd=tmp_path, capture_output=True, text=True)
    assert (result.returncode, result.stdout.splitlines(), result.stderr) == (0, expected, '')


@pytest.mark.parametrize(('paths', 'missing'), [
    (['tests/no-such-remote-regression.py'], 'tests/no-such-remote-regression.py'),
    (['cloud/tests/no-such-remote-regression.py::test_case'], 'cloud/tests/no-such-remote-regression.py'),
    (['tests/test_cf_status.py', 'cloud/tests/no-such-remote-regression.py'], 'cloud/tests/no-such-remote-regression.py'),
])
def test_missing_paths_refuse_before_any_remote_action(tmp_path, paths, missing):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    called = tmp_path / 'ssh-called'
    ssh = bindir / 'ssh'
    ssh.write_text('#!/bin/sh\ntouch "$SSH_CALLED"\nexit 97\n')
    ssh.chmod(0o755)
    result = run(
        ['bash', str(SCRIPT), *paths],
        cwd=SCRIPT.parent.parent,
        env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}', 'SSH_CALLED': str(called),
             'REMOTE_TEST_CONFIG': '/dev/null', 'REMOTE_TEST_HOST': 'unused'},
        capture_output=True,
        text=True,
    )
    assert (result.returncode, result.stdout, result.stderr, called.exists()) == (
        2, '', f'remote-test: test path does not exist: {missing}\n', False,
    )
