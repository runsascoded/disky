"""Fallback machine types for a stuck task (spec §7 "Capacity for big tasks"), end to end through `job/nf/scan.nf`.

A google-batch profile's generated executor config, run under a fake Slurm (`fixtures/fake_slurm`: Nextflow's grid
executor keeps a task SUBMITTED while the queue reports it pending, as nf-google does while Batch reports QUEUED /
SCHEDULED; the local executor starts every task at once, so it can't be stuck). A task whose machine type matches
`$FAKE_SLURM_STUCK` stays pending forever, like a job in a stocked-out pool: Nextflow's `maxSubmitAwait` cancels it
(`scancel`, as nf-google deletes the Batch job) and the label's errorStrategy resubmits it on the next type. Every step
is faked (`DT_SCAN_STEP_FAKE`); `DT_SCAN_STEP_FAIL=interval-store` makes a genuine failure on the fallback, which is not
retried.

Skipped without a working `nextflow` (it needs Java ≤ 25: `NXF_JAVA_HOME`) or its nf-google plugin installed (the
generated config pins it; the run is offline)."""
from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import disk_tree
import dt_cloud
from dt_cloud import scan_head as H
from dt_cloud.scan_head import NF_GOOGLE

from test_scan_nf import _nextflow_works

REPO = Path(__file__).resolve().parents[2]
FAKE_SLURM = Path(__file__).parent / "fixtures" / "fake_slurm"


def _plugin_installed() -> bool:
    home = Path(os.environ.get("NXF_HOME") or Path.home() / ".nextflow")
    return (home / "plugins" / NF_GOOGLE.replace("@", "-")).is_dir()


pytestmark = pytest.mark.skipif(not (_nextflow_works() and _plugin_installed()),
                                reason=f"no working `nextflow` (Java ≤ 25: NXF_JAVA_HOME) with {NF_GOOGLE} installed")

PROFILE = """\
deployment: fixture
executor: {kind: google-batch, project: p, region: r1, image: img}
work: gs://wb/nf
data: gs://wb
layout: {layer2: 'l2/{scan}/', snapshots: ''}
sources: [s3://a]
machines:
  list: m-list
  small: m-small
  post: m-post
  big: {type: m-primary, fallback: [m-fb1, m-fb2], queue_timeout: 1}
skip: [bucket-check, static-names, publish-r2, over-time, index-gc, warm-cache, probe, digest]
options: {list: {import: true}}
"""


def test_queued_task_falls_back_and_a_real_failure_does_not(tmp_path):
    prof = tmp_path / "fixture.yml"
    prof.write_text(PROFILE)
    launch = tmp_path / "launch"
    env = {"SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "1"}
    assert H.head(prof, REPO / "job/nf", launch, dry_run=True, env=env) == 0
    # the generated timeout is the profile's (minutes); 3 s here, so the test doesn't wait two minutes
    cfg = (launch / "executor.config").read_text()
    assert cfg.count("'1m'") == 1
    (launch / "executor.config").write_text(cfg.replace("'1m'", "'3s'"))
    (launch / "test.config").write_text(
        f"workDir = '{tmp_path}/work'\n"
        "process.executor = 'slurm'\n"
        "executor { pollInterval = '1 sec'; queueStatInterval = '1 sec'; exitReadTimeout = '30 sec' }\n")
    q = tmp_path / "slurm"
    q.mkdir()
    marks = tmp_path / "marks"
    pypath = os.pathsep.join(str(Path(m.__file__).parents[1]) for m in (dt_cloud, disk_tree))
    e = {k: v for k, v in os.environ.items() if k not in ("SCAN_RUNS_PROFILE", "DT_SCAN_STEP_FAIL", "DISKY_LABELS")}
    e.update(PATH=os.pathsep.join([str(FAKE_SLURM), str(Path(sys.executable).parent), e["PATH"]]), PYTHONPATH=pypath,
             NXF_OFFLINE="true", NXF_ANSI_LOG="false", DT_SCAN_STEP_FAKE=str(marks), DT_SCAN_STEP_FAIL="interval-store",
             FAKE_SLURM=str(q), FAKE_SLURM_STUCK="m-primary|m-fb1")
    argv = H.nextflow_argv(REPO / "job/nf", launch, H.session_id("fixture", env["SNAP_ID"])) + ["-c", str(launch / "test.config")]
    r = subprocess.run(argv, cwd=launch, env=e, capture_output=True, text=True, timeout=600)
    assert r.returncode == 1, r.stdout + r.stderr

    # index and interval_store (both `big`) waited on the primary and the first fallback, each cancelled, then ran on the
    # last fallback; the other labels' tasks ran on their own machines, once each
    assert sorted((q / "submitted").read_text().splitlines()) == [
        "index m-fb1 PD",
        "index m-fb2 R",
        "index m-primary PD",
        "index_rows m-small R",
        "interval_store m-fb1 PD",
        "interval_store m-fb2 R",
        "interval_store m-primary PD",
        "list_source m-list R",
    ]
    assert sorted((q / "cancelled").read_text().splitlines()) == [
        "index m-fb1", "index m-primary", "interval_store m-fb1", "interval_store m-primary",
    ]
    # interval-store's genuine failure on m-fb2 was not retried (submitted once there), and stopped the run before post
    assert sorted(marks.read_text().splitlines()) == ["index", "index-rows", "list s3://a"]
    assert sorted(re.sub(r"^.*?(scan head: )", r"\1", line) for line in r.stdout.splitlines() if "scan head: " in line) == [
        "scan head: index queued 1 min on m-fb1; retrying on m-fb2 (the last: no queue timeout)",
        "scan head: index queued 1 min on m-primary; retrying on m-fb1",
        "scan head: interval_store queued 1 min on m-fb1; retrying on m-fb2 (the last: no queue timeout)",
        "scan head: interval_store queued 1 min on m-primary; retrying on m-fb1",
    ]
    # each queue timeout is a FAILED row (no exit) followed by its retry's row; the run's reported failure is the real one
    with (launch / "trace.tsv").open() as f:
        rows = [(row["name"], row["status"], row["exit"]) for row in csv.DictReader(f, delimiter="\t")]
    assert [x for x in rows if x[0] == "index"] == [("index", "FAILED", "-"), ("index", "FAILED", "-"), ("index", "COMPLETED", "0")]
    assert [x for x in rows if x[0] == "interval_store"] == [
        ("interval_store", "FAILED", "-"), ("interval_store", "FAILED", "-"), ("interval_store", "FAILED", "1"),
    ]
    assert H.first_failed(launch / "trace.tsv") == "interval_store (exit 1)"
