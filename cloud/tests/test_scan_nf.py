"""`job/nf/scan.nf` end to end under the local executor (spec §4: the tests' executor): `dt-cloud scan head` over a
fixture profile, every step faked (`DT_SCAN_STEP_FAKE`: each appends `<name> <arg>` to a file). Checks the DAG's
order, a failure stopping the post task, and `-resume` re-running only what didn't finish.

Skipped without a working `nextflow` (it needs Java ≤ 25: `NXF_JAVA_HOME`)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import dt_cloud
import disk_tree

REPO = Path(__file__).resolve().parents[2]


def _nextflow_works() -> bool:
    if not shutil.which("nextflow"):
        return False
    r = subprocess.run(["nextflow", "-version"], capture_output=True, text=True, env={**os.environ, "NXF_OFFLINE": "true"})
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _nextflow_works(), reason="no working `nextflow` (set NXF_JAVA_HOME to a Java ≤ 25)")

ALL = ["bucket-check", "list s3://a", "list s3://b", "list s3://c", "index", "static-names", "publish-r2", "index-rows",
       "interval-store", "index-sync", "over-time", "index-gc", "warm-cache", "probe", "digest"]


SOURCES = "[s3://a, s3://b, {uri: s3://c, machine: m1, disk: {request: 375, type: local-ssd}}]"
OPTIONS = "{list: {import: true}, static-names: {profile: sn.json}, digest: {template: t, config: c.yml}}"


def head(tmp_path: Path, marks: Path, *args: str, extra: str = "", sources: str = SOURCES, options: str = OPTIONS,
         path: tuple[Path, ...] = (), layout: str = "{layer2: 'l2/{scan}/', snapshots: ''}", **env) -> subprocess.CompletedProcess:
    prof = tmp_path / "fixture.yml"
    prof.write_text(f"deployment: fixture\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                    f"layout: {layout}\n"
                    f"sources: {sources}\n"
                    "machines: {list: {type: m2, disk: 100}, big: {cpus: 1}}\n"
                    f"options: {options}\n" + extra)
    pypath = os.pathsep.join(str(Path(m.__file__).parents[1]) for m in (dt_cloud, disk_tree))
    e = {k: v for k, v in os.environ.items() if k not in ("SCAN_RUNS_PROFILE", "DT_SCAN_STEP_FAIL", "DISKY_LABELS")}
    e.update(PATH=os.pathsep.join([*map(str, path), str(Path(sys.executable).parent), e["PATH"]]), PYTHONPATH=pypath, NXF_OFFLINE="true",
             DT_SCAN_STEP_FAKE=str(marks), SNAP_ID="2026-10-16T1200", GEN="G1", DT_JOB_START="1", **env)
    return subprocess.run([sys.executable, "-m", "dt_cloud.cli", "scan", "head", *args, "-N", str(REPO / "job/nf"), str(prof)],
                          capture_output=True, text=True, env=e, timeout=600)


def _nf_node(script: Path) -> tuple[str, str]:
    """A task script's `(NF_PROCESS, NF_TAG)` exports."""
    ex = dict(line[len("export "):].split("=", 1) for line in script.read_text().splitlines() if line.startswith("export NF_"))
    return ex["NF_PROCESS"].strip("'"), ex["NF_TAG"].strip("'")


def test_dag_order_and_resume(tmp_path):
    marks = tmp_path / "marks"
    r1 = head(tmp_path, marks, DT_SCAN_STEP_FAIL="probe")
    assert r1.returncode != 0, r1.stderr
    ran = marks.read_text().splitlines()
    # everything up to the failing post step ran once; bucket-check folded into s3://a's list task
    assert sorted(ran) == sorted(ALL[:-2])
    at = {s: ran.index(s) for s in ran}
    lists = [at[f"list s3://{b}"] for b in "abc"]
    assert at["bucket-check"] < at["list s3://a"]
    assert max(lists) < at["index"]
    assert at["index"] < min(at["static-names"], at["publish-r2"], at["index-rows"])
    assert at["publish-r2"] < at["interval-store"]
    assert max(at["static-names"], at["interval-store"], at["index-rows"]) < at["index-sync"]
    # post runs its steps in order; over-time is its own task beside it, once the stores are written
    assert [x for x in ran[at["index-sync"]:] if x != "over-time"] == ["index-sync", "index-gc", "warm-cache"]
    assert max(at["static-names"], at["interval-store"], at["index-rows"]) < at["over-time"]

    marks.unlink()
    r2 = head(tmp_path, marks)
    assert r2.returncode == 0, r2.stderr
    # the resume ran only the post task (its steps skip their own done work); over-time's task was cached
    assert marks.read_text().splitlines() == ["index-sync", "index-gc", "warm-cache", "probe", "digest"]
    assert r2.stdout.splitlines()[-1] == f"SCAN-JOB-DONE 2026-10-16T1200 -> {tmp_path}/data/snapshots/2026-10-16T1200"
    # each task exports its DAG node (`NF_PROCESS`, `NF_TAG`), which the steps' scan-run phases record (index-sync and
    # bucket-check are folded into post and the first list task by default)
    nodes = sorted({_nf_node(p) for p in (tmp_path / "nf/work").rglob(".command.sh")})
    assert nodes == [("index", ""), ("index_rows", ""), ("interval_store", ""), ("list_source", "s3://a"),
                     ("list_source", "s3://b"), ("list_source", "s3://c"), ("over_time", ""), ("post", ""), ("publish_r2", ""), ("static_names", "")]
    # the run's reports beside it in the work store (`scan head` → `<work>/runs/<scan>/`), the DAG as Mermaid
    runs = tmp_path / "nf/runs/2026-10-16T1200"
    assert sorted(p.name for p in runs.iterdir()) == ["dag.mmd", "report.html", "timeline.html", "trace.tsv"]
    assert (runs / "dag.mmd").read_text().splitlines()[0] == "flowchart TB"


def test_preview_dag_is_the_runs(tmp_path):
    """The head's up-front DAG (`-preview`: no task runs) reduces to exactly the graph the real run's `-with-dag` does."""
    from dt_cloud import run_dag
    from dt_cloud.scan_head import preview_argv

    marks = tmp_path / "marks"
    r = head(tmp_path, marks)
    assert r.returncode == 0, r.stderr
    launch = tmp_path / "nf"
    marks.unlink()
    env = {**os.environ, "NXF_OFFLINE": "true", "NXF_ANSI_LOG": "false", "DT_SCAN_STEP_FAKE": str(marks)}
    p = subprocess.run(preview_argv(REPO / "job/nf", launch), cwd=launch, env=env, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr
    assert marks.exists() is False  # nothing ran
    preview = run_dag.read("nextflow", (launch / "dag-preview.mmd").read_text())
    real = run_dag.read("nextflow", (launch / "runs/2026-10-16T1200/dag.mmd").read_text())
    assert preview.to_json() == real.to_json()


def test_reproc_unfolded(tmp_path):
    """`-R` runs from `index` (no list tasks, no bucket-check); `fold: []` gives index-sync its own task."""
    marks = tmp_path / "marks"
    r = head(tmp_path, marks, "-R", extra="fold: []\n")
    assert r.returncode == 0, r.stderr
    ran = marks.read_text().splitlines()
    assert sorted(ran) == sorted(ALL[4:])
    at = {s: ran.index(s) for s in ran}
    assert ran[0] == "index"
    assert max(at["static-names"], at["interval-store"], at["index-rows"]) < at["index-sync"]
    assert [x for x in ran[at["index-sync"]:] if x != "over-time"] == ["index-sync", "index-gc", "warm-cache", "probe", "digest"]
    assert at["index-sync"] < at["over-time"]


def test_sharded_index_fans_out_parts(tmp_path):
    """`options.index.parts`: one `index_part` task per part (a bucket, or a key range of one: `<bucket>~<i>`), after
    every list, the merging `index` after them all."""
    marks = tmp_path / "marks"
    r = head(tmp_path, marks, options="{index: {mode: path-index, parts: {c: [m, t]}}, static-names: {profile: sn.json}, "
                                      "digest: {template: t, config: c.yml}}",
             layout="{layer2: 'l2/{scan}/', snapshots: '', listing: 'listing/{scan}/'}")
    assert r.returncode == 0, r.stderr
    ran = marks.read_text().splitlines()
    parts = [f"index {p}" for p in ("a", "b", "c~0", "c~1", "c~2")]
    assert sorted(x for x in ran if x.startswith("index ")) == parts
    at = {s: ran.index(s) for s in ran}
    assert max(at[f"list s3://{b}"] for b in "abc") < min(at[p] for p in parts)
    assert max(at[p] for p in parts) < at["index"]
    nodes = {_nf_node(p) for p in (tmp_path / "nf/work").rglob(".command.sh")}
    assert sorted(n for n in nodes if n[0] == "index_part") == [("index_part", p.removeprefix("index ")) for p in parts]


def test_per_call_secrets(tmp_path):
    """Each step's call fetches its own bindings: the profile's `secrets`, overlaid by `options.<step>.secrets`, and a
    list call's by its source's `secrets`, also where a step is folded into another's task (bucket-check in the first
    list task, index-sync in post). A `dt-cloud` shim first on PATH logs each `secrets exec` (no Secret Manager here)."""
    shim, log = tmp_path / "shim", tmp_path / "secrets.log"
    shim.mkdir()
    (shim / "dt-cloud").write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "secrets exec" ]; then\n'
        "  shift 2; b=''\n"
        '  while [ "$1" != "--" ]; do b="$b $1"; shift; done\n'
        "  shift\n"
        f'  echo "$4 $5|$b" >> {log}\n'
        '  exec "$@"\n'
        "fi\n"
        f'exec {sys.executable} -m dt_cloud.cli "$@"\n')
    (shim / "dt-cloud").chmod(0o755)
    r = head(tmp_path, tmp_path / "marks", path=(shim,),
             sources="[{uri: s3://a, secrets: {AWS_ACCESS_KEY_ID: a-key}}, s3://b]",
             options="{list: {import: true, secrets: {AWS_ACCESS_KEY_ID: list-key}}, static-names: {profile: sn.json}, "
                     "digest: {template: t, config: c.yml}, index-gc: {secrets: {CLOUDFLARE_API_TOKEN: gc-cf}}}",
             extra="secrets: {AWS_ACCESS_KEY_ID: base-key, CLOUDFLARE_API_TOKEN: cf}\n")
    assert r.returncode == 0, r.stderr
    base = " -s AWS_ACCESS_KEY_ID=base-key -s CLOUDFLARE_API_TOKEN=cf"
    assert sorted(log.read_text().splitlines()) == sorted([
        f"bucket-check |{base}",
        "list s3://a| -s AWS_ACCESS_KEY_ID=a-key -s CLOUDFLARE_API_TOKEN=cf",
        "list s3://b| -s AWS_ACCESS_KEY_ID=list-key -s CLOUDFLARE_API_TOKEN=cf",
        *(f"{s} |{base}" for s in ("index", "static-names", "publish-r2", "index-rows", "interval-store", "index-sync",
                                   "over-time", "warm-cache", "probe", "digest")),
        "index-gc | -s AWS_ACCESS_KEY_ID=base-key -s CLOUDFLARE_API_TOKEN=gc-cf",
    ])
