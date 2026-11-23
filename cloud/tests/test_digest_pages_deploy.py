import subprocess
from pathlib import Path

import pytest

from dt_cloud import digest as dg

CMD = ["/bin/wrangler", "pages", "deploy", "icons", "--project-name", "demo-icons", "--branch", "plot", "--commit-dirty=true"]
OK = subprocess.CompletedProcess(CMD, 0, "Deployed to https://ab12cd34.demo-icons.pages.dev\n", "")
FAIL = subprocess.CompletedProcess(CMD, 1, "uploading\n", "Authentication error [code: 10000]\n")


@pytest.fixture
def wrangler(monkeypatch):
    """`pages_deploy` over scripted `wrangler` results; returns (calls, sleeps, results)."""
    calls: list[list[str]] = []
    sleeps: list[float] = []
    results: list[subprocess.CompletedProcess] = []

    def run(cmd, **kw):
        calls.append(cmd)
        return results.pop(0)

    monkeypatch.setattr(dg.time, "sleep", sleeps.append)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    return calls, sleeps, results


def test_retry_then_success_logs_the_failure(wrangler, capsys):
    calls, sleeps, results = wrangler
    results += [FAIL, OK]
    assert dg.pages_deploy(Path("icons"), "demo-icons", "plot") == "https://ab12cd34.demo-icons.pages.dev"
    assert calls == [CMD, CMD]
    assert sleeps == [10]
    assert capsys.readouterr().err.split("\n") == [
        "digest: wrangler pages deploy exited 1 (attempt 1)",
        "uploading",
        "Authentication error [code: 10000]",
        "",
        "Deployed to https://ab12cd34.demo-icons.pages.dev",
        "",
        "",
    ]


def test_three_failures_raise_with_wrangler_output(wrangler, capsys):
    calls, sleeps, results = wrangler
    results += [FAIL, FAIL, FAIL]
    with pytest.raises(subprocess.CalledProcessError) as e:
        dg.pages_deploy(Path("icons"), "demo-icons", "plot")
    assert (e.value.returncode, e.value.cmd, e.value.stderr) == (1, CMD, "Authentication error [code: 10000]\n")
    assert calls == [CMD, CMD, CMD]
    assert sleeps == [10, 30]
    assert [line for line in capsys.readouterr().err.split("\n") if line.startswith("digest:")] == [
        "digest: wrangler pages deploy exited 1 (attempt 1)",
        "digest: wrangler pages deploy exited 1 (attempt 2)",
        "digest: wrangler pages deploy exited 1 (attempt 3)",
    ]
