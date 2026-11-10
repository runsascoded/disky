"""An empty remote suite must not be hidden by another suite's success."""
from pathlib import Path
from subprocess import run

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'remote-test'


@pytest.mark.parametrize(('codes', 'expected'), [
    ([0, 0], ['0', '1']),
    ([0, 5], ['5', '1']),
    ([5, 0], ['5', '1']),
    ([2, 0], ['2', '1']),
])
def test_remote_suite_preserves_empty_and_failed_results(tmp_path, codes, expected):
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
