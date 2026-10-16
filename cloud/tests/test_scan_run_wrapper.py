"""`dt-cloud scan-run start / phase-begin / phase-end / phase -- CMD / finish` (specs/scan-job-wrapper.md): the
recorded rows, byte-exact PHASE lines, alert payloads through a fake transport, one alert per failure across
processes, redaction, and `-T`'s process-group kill."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from disk_tree.notify import push
from dt_cloud import scan_runs
from dt_cloud.d1_schema import table_ddl
from dt_cloud.scan_runs import SqliteSink, cli, duration, parse_profile, phase_line, redact

DDL = table_ddl(scan_runs.TABLES)  # the tables as `site/schema.sql` has them
T0 = 1_760_000_000
SCAN = "2026-10-14T0600"
PROFILE = {
    "store": "primary",
    "project": "proj-1",
    "run_id_env": ["JOB_ID"],
    "alerts": {"label": "cw-s3", "logs_url": "https://logs.test/q;uid={job_uid}?project={project}&job={job}"},
    "secrets": ["SITE_TOKEN", "SLACK_BOT_TOKEN"],
}
SECRET = "s3cr3t-site-token"
NOTIFY_ENV = ("PUSHOVER_TOKEN", "PUSHOVER_USER", "DISCORD_NOTIFY_WEBHOOK", "SLACK_BOT_TOKEN", "SLACK_ALERT_CHANNEL",
              "DISK_TREE_NOTIFY", "DT_ALERTS", "DT_RECORD", "SCAN_ID", "BATCH_JOB_UID")


class Transport:
    """The `post` seam `disk-tree notify` sends through: records each Slack request, answers `ok`."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append((url, headers, json.loads(body)))
        return 200, '{"ok":true}'

    def texts(self) -> list[str]:
        return [b["text"] for _, _, b in self.calls]


class Job:
    """A job's environment, in process: the profile, a SQLite record, a pinned clock, captured stderr."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch):
        self.tmp, self.mp = tmp, monkeypatch
        self.profile = tmp / "scan-runs.json"
        self.profile.write_text(json.dumps(PROFILE))
        self.db = tmp / "runs.sqlite"
        SqliteSink(self.db, ddl=DDL)
        for k in NOTIFY_ENV:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("TMPDIR", str(tmp))
        monkeypatch.setenv("JOB_ID", "u1")
        monkeypatch.setenv("DT_JOB_START", str(T0))
        self.said: list[str] = []
        monkeypatch.setattr(scan_runs, "err", self.said.append)

    def __call__(self, cmd: str, *args: str, at: int) -> int:
        self.mp.setattr(scan_runs, "_now", lambda: at)
        r = CliRunner().invoke(cli, [cmd, "-c", str(self.profile), "-s", str(self.db), *args])
        assert (r.exception is None or isinstance(r.exception, SystemExit), r.stdout) == (True, "")
        return r.exit_code

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(r.values()) for r in SqliteSink(self.db).query(sql)]

    def take(self) -> list[str]:
        out, self.said[:] = list(self.said), []
        return out


@pytest.fixture
def job(tmp_path, monkeypatch) -> Job:
    return Job(tmp_path, monkeypatch)


@pytest.fixture
def slack(monkeypatch) -> Transport:
    t = Transport()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_ALERT_CHANNEL", "C0ALERTS")
    monkeypatch.setattr(push, "http_post", t)
    return t


def test_phase_lines_are_the_scripts_markers_and_parse_back():
    lines = [
        phase_line("listing-fanout", 0, start=True),
        phase_line("listing-fanout", 1514),
        phase_line("access-ingest", 1520, "overlapped the listing"),
        phase_line("publish-r2", 3601, "failed (exit 3)"),
    ]
    assert lines == [
        "PHASE-START listing-fanout: 0s (wall)",
        "PHASE listing-fanout: 1514s (wall)",
        "PHASE access-ingest: 1520s (wall, overlapped the listing)",
        "PHASE publish-r2: 3601s (wall, failed (exit 3))",
    ]
    p = parse_profile({})
    assert [m.group("name") for m in [re.match(p.phase_start_marker, lines[0])]] == ["listing-fanout"]
    assert [(m.group("name"), m.group("note")) for m in (re.match(p.phase_marker, ln) for ln in lines[1:])] == [
        ("listing-fanout", "wall"), ("access-ingest", "wall, overlapped the listing"), ("publish-r2", "wall, failed (exit 3"),
    ]


def test_duration_and_redact():
    assert [duration(s) for s in ("90", "90s", "2m", "1.5h", "1d")] == [90, 90, 120, 5400, 86400]
    with pytest.raises(ValueError, match="not a duration: '2x'"):
        duration("2x")
    env = {"A": "tok-long\n", "B": "tok", "C": ""}
    assert redact("tok-long tok tok-long\n x", ["B", "A", "C"], env) == "<A> <B> <A>\n x"


def test_start_phases_finish_record_exact_rows(job):
    assert job("start", "-k", "reproc", "-p", "parent-uid", "-m", "box-1", "--no-spot", "-T", "1", "-j", "job-9",
               "-R", "us-east1", SCAN, at=T0 + 5) == 0
    assert job("phase-begin", "listing", at=T0 + 10) == 0
    assert job("phase-end", "-M", "listing", at=T0 + 70) == 0
    assert job("phase-begin", "-g", "G1", "index", at=T0 + 70) == 0
    assert job("phase-end", "-M", "-q", "3 buckets", "index", at=T0 + 100) == 0
    assert job("phase-begin", "publish", at=T0 + 100) == 0
    assert job("phase-end", "-M", "-x", "3", "-m", "D1 pointers not advanced", "publish", at=T0 + 130) == 3
    job.mp.setenv("DT_ALERTS", "stderr")
    assert job("finish", "-x", "3", "-e", "exit 3: dt-cloud publish-r2 (run.sh:12)", at=T0 + 131) == 0
    assert job.rows("SELECT run_id, scan, kind, status, parent, failed_phase, error, started_ts, finished_ts, gen, job_name, "
                    "region, machine, spot, tasks, source FROM scan_runs") == [
        ("u1", SCAN, "reproc", "failed", "parent-uid", "publish", "exit 3: dt-cloud publish-r2 (run.sh:12)", T0 + 5, T0 + 131,
         "G1", "job-9", "us-east1", "box-1", 0, 1, "job"),
    ]
    assert job.rows("SELECT phase, seq, started_ts, finished_ts, status, note FROM scan_run_phases ORDER BY seq") == [
        ("listing", 0, T0 + 10, T0 + 70, "done", None),
        ("index", 1, T0 + 70, T0 + 100, "done", "3 buckets"),
        ("publish", 2, T0 + 100, T0 + 130, "failed", "failed (exit 3)"),
    ]
    assert job.take() == [
        f"scan-run: {SCAN} u1 running",
        "PHASE-START listing: 10s (wall)",
        f"scan-run: {SCAN} u1 running · phase listing",
        "PHASE listing: 70s (wall)",
        f"scan-run: {SCAN} u1 running · phase listing",
        "PHASE-START index: 70s (wall)",
        f"scan-run: {SCAN} u1 running · phase index",
        "PHASE index: 100s (wall, 3 buckets)",
        f"scan-run: {SCAN} u1 running · phase index",
        "PHASE-START publish: 100s (wall)",
        f"scan-run: {SCAN} u1 running · phase publish",
        "PHASE publish: 130s (wall, failed (exit 3))",
        f"scan-run: {SCAN} u1 running · phase publish",
        "alert (no channel configured): ❌ cw-s3 2026-10-14T0600: publish failed (exit 3): D1 pointers not advanced",
        f"scan-run: {SCAN} u1 failed",  # the phase alerted: `finish` posts no second alert
    ]
    assert sorted(p.name for p in (job.tmp / "dt-scan-run").iterdir()) == []  # `finish` clears the state + marker


@pytest.mark.parametrize("rc, status", [(0, "nop"), (1, "failed")])
def test_nop_kept_by_a_clean_finish(job, rc, status):
    job.mp.setenv("DT_ALERTS", "stderr")
    assert job("start", SCAN, at=T0) == 0
    assert job("nop", at=T0 + 5) == 0
    assert job("finish", "-x", str(rc), at=T0 + 6) == 0
    assert job.rows("SELECT status, started_ts, finished_ts FROM scan_runs") == [(status, T0, T0 + 6)]


def test_no_run_id_or_record_off_records_nothing_but_marks_and_alerts(job):
    job.mp.delenv("JOB_ID")
    job.mp.setenv("SCAN_ID", SCAN)
    job.mp.setenv("DT_ALERTS", "stderr")
    assert job("phase-end", "-x", "1", "p", at=T0 + 4) == 1
    job.mp.setenv("JOB_ID", "u1")
    job.mp.setenv("DT_RECORD", "off")
    assert job("phase-end", "-W", "-x", "2", "q", at=T0 + 5) == 0
    assert job.rows("SELECT run_id FROM scan_runs") == []
    assert job.take() == [
        "PHASE p: 4s (wall, failed (exit 1))",
        "alert (suppressed): ❌ cw-s3 2026-10-14T0600: p failed (exit 1)",
        "PHASE q: 5s (wall, failed (exit 2))",
        "alert (suppressed): ⚠️ cw-s3 2026-10-14T0600: q failed (exit 2)",
    ]


def test_missing_job_start_refuses_and_runs_nothing(job):
    job.mp.delenv("DT_JOB_START")
    ran = job.tmp / "ran"
    assert [
        job("phase-begin", "p", at=T0),
        job("phase-end", "p", at=T0),
        job("phase", "-M", "p", "--", "touch", str(ran), at=T0),
    ] == [2, 2, 2]
    assert (ran.exists(), job.take()) == (False, [
        "scan-run: $DT_JOB_START is not set: export DT_JOB_START=$(date +%s) at the job's top",
    ] * 3)


def test_alert_payloads_warn_fail_and_finish(job, slack):
    url = "https://logs.test/q;uid=u1?project=proj-1&job=job-9"

    def payload(text: str) -> tuple[str, dict, dict]:
        return (push.SLACK_URL, {"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer xoxb-test"},
                {"channel": "C0ALERTS", "text": text, "unfurl_links": False, "unfurl_media": False})

    assert job("start", "-j", "job-9", SCAN, at=T0) == 0
    resume = f"dt-cloud static-names runs add -c {SCAN}"
    assert job("phase-end", "-M", "-W", "-x", "1", "-m", "the scan publishes; search says not available", "-R", resume,
               "static-names", at=T0 + 50) == 0
    assert job("phase-end", "-M", "-x", "1", "publish", at=T0 + 60) == 1
    assert job("finish", "-x", "1", at=T0 + 61) == 0
    # a second run: no phase alerted, so `finish` does, with the ERR trap's text
    assert job("start", "-j", "job-9", SCAN, at=T0 + 100) == 0
    assert job("finish", "-x", "2", "-e", "exit 2: cp a b (run.sh:40)", at=T0 + 101) == 0
    assert slack.calls == [
        payload(f"*⚠️ cw-s3 {SCAN}*\nstatic-names failed (exit 1): the scan publishes; search says not available\n"
                f"Resume: `{resume}`\n<{url}|task logs>"),
        payload(f"*❌ cw-s3 {SCAN}*\npublish failed (exit 1)\n<{url}|task logs>"),
        payload(f"*❌ cw-s3 {SCAN}*\nrun failed (exit 2): exit 2: cp a b (run.sh:40)\n<{url}|task logs>"),
    ]
    assert [ln for ln in job.take() if ln.startswith("alert")] == ["alert: slack: sent"] * 3


CMD = ('( cd "$app/cloud" && "$time_bin" -v -o "$work/static-time.txt" "$app/.venv/bin/dt-cloud" static-names "$cmd" '
       '-g "$STATIC_GEN" -L "listing/$store/{id}/index/{gen}/path-index.parquet" -s "r2://$bucket" -o "r2://$bucket" '
       '-w "$work/static-names" -p "$(nproc)" -M "${STATIC_MEM:-5GB}" ${hex[@]+"${hex[@]}"} )')


V2_OOM = {"memory.events": "low 0\nhigh 0\nmax 12\noom 1\noom_kill 1\n", "memory.max": f"{16 * 2**30}\n",
          "memory.peak": f"{int(15.98 * 2**30)}\n"}
V1_NO_COUNT = {"memory.oom_control": "oom_kill_disable 0\nunder_oom 0\n", "memory.limit_in_bytes": "9223372036854771712\n"}


@pytest.mark.parametrize("files, error, recorded, alert", [
    pytest.param(
        V2_OOM, f"exit 137: {CMD} (ingest.sh:172)",
        f"out of memory (OOM-killed), peak 16.0 GiB of 16.0 GiB: exit 137: {CMD} (ingest.sh:172)",
        # 240 characters: the head, `…`, the location
        'out of memory (OOM-killed), peak 16.0 GiB of 16.0 GiB: exit 137: ( cd "$app/cloud" && "$time_bin" -v -o '
        '"$work/static-time.txt" "$app/.venv/bin/dt-cloud" static-names "$cmd" -g "$STATIC_GEN" -L '
        '"listing/$store/{id}/index/{g… (ingest.sh:172)',
        id="cgroup-v2-oom-kill-long-command",
    ),
    pytest.param(V1_NO_COUNT, "killed (SIGKILL)", "killed (SIGKILL), most likely out of memory", None,
                 id="supervise-kill-cgroup-v1-unlimited"),
    pytest.param({}, "exit 137: false (run.sh:3)", "killed (SIGKILL), most likely out of memory: exit 137: false (run.sh:3)",
                 None, id="no-cgroup"),
    pytest.param(V2_OOM, "exit 2: false (run.sh:3)", "exit 2: false (run.sh:3)", None, id="not-137-untouched"),
])
def test_exit_137_says_out_of_memory(job, slack, tmp_path, files, error, recorded, alert):
    """A SIGKILL nobody here sent (exit 137) is the OOM killer's: `finish` says so, with the cgroup's limit and peak,
    and the alert clips a long ERR-trap command (the record keeps it whole)."""
    cg = tmp_path / "cgroup"
    cg.mkdir()
    for name, text in files.items():
        (cg / name).write_text(text)
    job.mp.setenv("DT_CGROUP", str(cg))
    rc = int(error.split(":")[0].split()[1]) if error.startswith("exit") else 137
    assert job("start", SCAN, at=T0) == 0
    assert job("finish", "-x", str(rc), "-e", error, at=T0 + 1) == 0
    assert job.rows("SELECT error FROM scan_runs") == [(recorded,)]
    assert slack.texts() == [f"*❌ cw-s3 {SCAN}*\nrun failed (exit {rc}): {alert or recorded}"]
    if alert:
        assert len(alert) == 240


def test_logs_url_missing_a_fact_posts_no_link(job, slack):
    assert job("start", SCAN, at=T0) == 0  # no `-j`: `{job}` is unknown
    assert job("finish", "-x", "1", at=T0 + 1) == 0
    assert slack.texts() == [f"*❌ cw-s3 {SCAN}*\nrun failed (exit 1)"]


def test_redaction_of_a_seeded_secret(job, slack):
    job.mp.setenv("SITE_TOKEN", f"{SECRET}\n")  # as injected: with its newline
    assert job("start", SCAN, at=T0) == 0
    assert job("phase-end", "-M", "-W", "-x", "1", "-q", f"token {SECRET}", "-m", f"rejected {SECRET}",
               "-R", f"SITE_TOKEN={SECRET} dt-cloud index-sync -p {SCAN}", "index-sync", at=T0 + 10) == 0
    assert job("finish", "-x", "1", "-e", f"exit 1: curl -H 'Authorization: Bearer {SECRET}' (run.sh:9)", at=T0 + 11) == 0
    assert slack.texts() == [
        f"*⚠️ cw-s3 {SCAN}*\nindex-sync failed (exit 1): rejected <SITE_TOKEN>\nResume: `SITE_TOKEN=<SITE_TOKEN> dt-cloud index-sync -p {SCAN}`",
        f"*❌ cw-s3 {SCAN}*\nrun failed (exit 1): exit 1: curl -H 'Authorization: Bearer <SITE_TOKEN>' (run.sh:9)",
    ]
    assert job.rows("SELECT error FROM scan_runs") == [("exit 1: curl -H 'Authorization: Bearer <SITE_TOKEN>' (run.sh:9)",)]
    assert job.rows("SELECT phase, note FROM scan_run_phases") == [("index-sync", "failed (exit 1), token <SITE_TOKEN>")]
    said = job.take()
    assert said[0:2] == [f"scan-run: {SCAN} u1 running", "PHASE index-sync: 10s (wall, failed (exit 1), token <SITE_TOKEN>)"]
    assert SECRET not in "\n".join(said) + job.db.read_bytes().decode(errors="replace")  # secondary: nowhere at all


def test_timeout_kills_the_process_group_grandchild_included(job):
    """CMD's grandchild ignores TERM (as does CMD): past `-T`, the group gets TERM, then after `-G` KILL; exit 124."""
    job.mp.setenv("DT_ALERTS", "stderr")
    assert job("start", SCAN, at=T0) == 0
    pidfile = job.tmp / "grandchild.pid"
    script = f'trap "" TERM; sleep 60 & echo $! > {pidfile}; wait'
    t = time.monotonic()
    assert job("phase", "-M", "-T", "1s", "-G", "0.5", "slow", "--", "bash", "-c", script, at=T0 + 7) == 124
    elapsed = time.monotonic() - t
    gpid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"grandchild {gpid} survived the group kill")
    assert elapsed < 10
    assert [ln for ln in job.take() if not ln.startswith("scan-run: ")] == [
        "PHASE-START slow: 7s (wall)",
        "PHASE slow: 7s (wall, failed (exit 124))",
        f"alert (suppressed): ❌ cw-s3 {SCAN}: slow failed (exit 124, timed out after 1s)",
    ]


def test_phase_sugar_passes_cmd_exit_through(job):
    job.mp.setenv("DT_ALERTS", "stderr")
    assert job("start", SCAN, at=T0) == 0
    assert job("phase", "-M", "-W", "ok", "--", "true", at=T0 + 1) == 0
    assert job("phase", "-M", "-W", "soft", "--", "sh", "-c", "exit 5", at=T0 + 2) == 0
    assert job("phase", "-M", "hard", "--", "sh", "-c", "exit 6", at=T0 + 3) == 6
    assert job.rows("SELECT phase, started_ts, finished_ts, status, note FROM scan_run_phases ORDER BY seq") == [
        ("ok", T0 + 1, T0 + 1, "done", None),
        ("soft", T0 + 2, T0 + 2, "failed", "failed (exit 5)"),
        ("hard", T0 + 3, T0 + 3, "failed", "failed (exit 6)"),
    ]


DT_CLOUD = Path(sys.executable).with_name("dt-cloud")
SCRIPT_HEAD = """set -euo pipefail
export DT_JOB_START=$(date +%s)
sr() { dt-cloud scan-run "$1" -s "$DB" "${@:2}"; }
trap 'SR_ERR="exit $?: $BASH_COMMAND"' ERR
trap 'sr finish -x $? ${SR_ERR:+-e "$SR_ERR"}' EXIT
sr start "$SCAN"
"""


@pytest.mark.parametrize("body, rc, alerts, run", [
    pytest.param(
        '( sr phase -M bg -- false ) & BG=$!\nsr phase -M fg -- true\nwait "$BG"\necho unreachable\n',
        1, [f"alert (suppressed): ❌ cw-s3 {SCAN}: bg failed (exit 1)"],
        ("failed", "bg", 'exit 1: wait "$BG"'),
        id="background-phase-then-finish",
    ),
    pytest.param(
        "sr phase -M fg -- true\nfalse\n",
        1, [f"alert (suppressed): ❌ cw-s3 {SCAN}: run failed (exit 1): exit 1: false"],
        ("failed", None, "exit 1: false"),
        id="unphased-failure",
    ),
    pytest.param("sr phase -M fg -- true\n", 0, [], ("succeeded", None, None), id="clean"),
])
def test_one_alert_across_processes(tmp_path, body, rc, alerts, run):
    """A job script's real shape: traps, `start`, phases (one in a background subshell), `finish` from the EXIT
    trap. Each failure alerts exactly once, whichever process saw it first."""
    (tmp_path / "scan-runs.json").write_text(json.dumps(PROFILE))
    db = tmp_path / "runs.sqlite"
    SqliteSink(db, ddl=DDL)
    script = tmp_path / "job.sh"
    script.write_text(SCRIPT_HEAD + body)
    env = {k: v for k, v in os.environ.items() if k not in NOTIFY_ENV}
    env.update(PATH=f"{DT_CLOUD.parent}:{env['PATH']}", TMPDIR=str(tmp_path), JOB_ID="u-bg", SCAN=SCAN, DB=str(db),
               SCAN_RUNS_PROFILE=str(tmp_path / "scan-runs.json"), DT_ALERTS="stderr")
    p = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=120)
    lines = p.stderr.splitlines()
    assert (p.returncode, p.stdout, [ln for ln in lines if ln.startswith("alert")]) == (rc, "", alerts)
    phases = sorted(re.sub(r": \d+s ", ": <N>s ", ln) for ln in lines if ln.startswith("PHASE"))
    expected_phases = ["PHASE fg: <N>s (wall)", "PHASE-START fg: <N>s (wall)"]
    if "bg" in body:
        expected_phases = sorted(expected_phases + ["PHASE bg: <N>s (wall, failed (exit 1))", "PHASE-START bg: <N>s (wall)"])
    assert phases == expected_phases
    s = SqliteSink(db)
    assert [tuple(r.values()) for r in s.query("SELECT status, failed_phase, error FROM scan_runs")] == [run]
    assert sorted(p.name for p in (tmp_path / "dt-scan-run").iterdir()) == []


SUPERVISED_HEAD = SCRIPT_HEAD.replace("sr start", "trap 'exit 143' TERM\ntrap 'exit 130' INT\nsr start")


class Supervised:
    """`dt-cloud scan-run supervise [-G grace] -- bash job.sh`, a real process tree over a SQLite record."""

    def __init__(self, tmp: Path, script: str, grace: str = "5"):
        self.tmp = tmp
        (tmp / "scan-runs.json").write_text(json.dumps(PROFILE))
        self.db = tmp / "runs.sqlite"
        SqliteSink(self.db, ddl=DDL)
        (tmp / "job.sh").write_text(script)
        env = {k: v for k, v in os.environ.items() if k not in NOTIFY_ENV}
        env.update(PATH=f"{DT_CLOUD.parent}:{env['PATH']}", TMPDIR=str(tmp), JOB_ID="u-sv", SCAN=SCAN, DB=str(self.db),
                   SCAN_RUNS_PROFILE=str(tmp / "scan-runs.json"), DT_ALERTS="stderr", T=str(tmp))
        self.p = subprocess.Popen([str(DT_CLOUD), "scan-run", "supervise", "-G", grace, "--", "bash", str(tmp / "job.sh")],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def pid(self, name: str, timeout: float = 30) -> int:
        """The pid a job process wrote to `$T/<name>` (waiting for it)."""
        f = self.tmp / name
        deadline = time.monotonic() + timeout
        while not (f.exists() and f.read_text().strip()):
            assert (self.p.poll(), time.monotonic() < deadline) == (None, True), f"no {name} pid"
            time.sleep(0.05)
        return int(f.read_text())

    def end(self, sig: int | None = None) -> tuple[int, str, list[str]]:
        if sig:
            self.p.send_signal(sig)
        out, stderr = self.p.communicate(timeout=60)
        return self.p.returncode, out, stderr.splitlines()

    def run_row(self) -> list[tuple]:
        return [tuple(r.values()) for r in SqliteSink(self.db).query("SELECT status, failed_phase, error FROM scan_runs")]

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in (self.tmp / "dt-scan-run").iterdir())


def gone(*pids: int, timeout: float = 5) -> list[int]:
    """Which of `pids` still exist after `timeout` (orphans are reaped by init/launchd shortly after they die)."""
    deadline = time.monotonic() + timeout
    while True:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive or time.monotonic() > deadline:
            return alive
        time.sleep(0.05)


def statuses(lines: list[str]) -> list[str]:
    """The record's own progress lines (`scan-run: <scan> <run> <status>[ · phase P]`)."""
    return [ln for ln in lines if ln.startswith("scan-run")]


def alerts(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("alert")]


def test_supervise_term_fails_the_run_once_and_empties_the_tree(tmp_path):
    """(a) The platform TERMs the entrypoint mid-phase: the whole group gets it at once, the phase ends failed and
    alerts, the script's traps finish 143, and `supervise`'s record (`terminated`) is the run's; nothing survives."""
    sv = Supervised(tmp_path, SUPERVISED_HEAD + 'echo $$ > "$T/job"\nsr phase -M fg -- sh -c \'echo $$ > "$T/child"; exec sleep 60\'\n')
    job, child = sv.pid("job"), sv.pid("child")
    rc, out, lines = sv.end(signal.SIGTERM)
    assert (rc, out, alerts(lines)) == (143, "", [f"alert (suppressed): ❌ cw-s3 {SCAN}: fg failed (exit 143, terminated by SIGTERM)"])
    assert sv.run_row() == [("failed", "fg", "terminated (SIGTERM)")]
    assert statuses(lines) == [
        f"scan-run: {SCAN} u-sv running",
        f"scan-run: {SCAN} u-sv running · phase fg",
        f"scan-run: {SCAN} u-sv running · phase fg",
        f"scan-run: {SCAN} u-sv failed",  # the script's EXIT trap (`exit 143` from its TERM trap)
        f"scan-run: {SCAN} u-sv failed",  # `supervise`'s
    ]
    assert (gone(job, child), sv.leftovers()) == ([], [])


def test_supervise_kills_what_ignores_term_after_the_grace(tmp_path):
    """(b) The phase's command ignores TERM: after `-G` the group is KILLed (no trap runs), and `supervise` records the
    run failed and posts the one alert."""
    cmd = 'trap "" TERM; sleep 60 & echo $! > "$T/child"; wait'
    sv = Supervised(tmp_path, SUPERVISED_HEAD + f"sr phase -M fg -- bash -c '{cmd}'\n", grace="0.5")
    child = sv.pid("child")
    t = time.monotonic()
    rc, out, lines = sv.end(signal.SIGTERM)
    assert (rc, out, alerts(lines), time.monotonic() - t < 10) == (137, "", [
        f"alert (suppressed): ❌ cw-s3 {SCAN}: run failed (exit 143) in fg: terminated (SIGTERM); killed after the 0.5s grace",
    ], True)
    assert sv.run_row() == [("failed", "fg", "terminated (SIGTERM); killed after the 0.5s grace")]
    assert [tuple(r.values()) for r in SqliteSink(sv.db).query("SELECT phase, status, note FROM scan_run_phases")] == [
        ("fg", "failed", "failed (exit 143)"),
    ]
    assert (gone(child), sv.leftovers()) == ([], [])


def test_supervise_termination_overrides_a_recorded_success(tmp_path):
    """(c) The observed m3 bug: the script's EXIT trap records success (`$?` was 0) as the platform terminates it.
    `supervise` re-records the run failed, and alerts once (the trap's `finish -x 0` posted nothing)."""
    head = SCRIPT_HEAD.replace("sr finish -x $? ${SR_ERR:+-e \"$SR_ERR\"}", "sr finish -x 0")
    sv = Supervised(tmp_path, head + 'sh -c \'echo $$ > "$T/child"; exec sleep 60\'\n')
    child = sv.pid("child")
    rc, out, lines = sv.end(signal.SIGTERM)
    assert (rc, out, alerts(lines)) == (143, "", [f"alert (suppressed): ❌ cw-s3 {SCAN}: run failed (exit 143): terminated (SIGTERM)"])
    assert statuses(lines) == [
        f"scan-run: {SCAN} u-sv running",
        f"scan-run: {SCAN} u-sv succeeded",
        f"scan-run: {SCAN} u-sv failed",
    ]
    assert sv.run_row() == [("failed", None, "terminated (SIGTERM)")]
    assert (gone(child), sv.leftovers()) == ([], [])


@pytest.mark.parametrize("body, status", [
    pytest.param("sr phase -M fg -- true\n", "succeeded", id="clean"),
    pytest.param("sr nop\n", "nop", id="nop"),
])
def test_supervise_clean_run_passes_through_untouched(tmp_path, body, status):
    """(d) No signal, exit 0: the script's own record stands, `supervise` writes nothing more and clears the state."""
    sv = Supervised(tmp_path, SUPERVISED_HEAD + body)
    rc, out, lines = sv.end()
    assert (rc, out, alerts(lines)) == (0, "", [])
    expected = [f"scan-run: {SCAN} u-sv running"]
    expected += [f"scan-run: {SCAN} u-sv running · phase fg"] * 2 if "phase" in body else [f"scan-run: {SCAN} u-sv nop"]
    assert statuses(lines) == expected + [f"scan-run: {SCAN} u-sv {status}"]
    assert (sv.run_row(), sv.leftovers()) == ([(status, None, None)], [])


def test_phase_forwards_a_received_term_to_its_session(tmp_path):
    """Without `supervise`, a TERM sent to `phase -T` itself reaches CMD's own process group at once; the phase ends
    failed with 143."""
    (tmp_path / "scan-runs.json").write_text(json.dumps(PROFILE))
    env = {k: v for k, v in os.environ.items() if k not in NOTIFY_ENV}
    env.update(TMPDIR=str(tmp_path), DT_JOB_START=str(int(time.time())), DT_ALERTS="stderr", SCAN_ID=SCAN,
               SCAN_RUNS_PROFILE=str(tmp_path / "scan-runs.json"))
    pidfile = tmp_path / "child"
    p = subprocess.Popen([str(DT_CLOUD), "scan-run", "phase", "-M", "-T", "1h", "fg", "--", "sh", "-c",
                          f"echo $$ > {pidfile}; exec sleep 60"], env=env, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    t = time.monotonic()
    p.send_signal(signal.SIGTERM)
    _, stderr = p.communicate(timeout=30)
    lines = [re.sub(r": \d+s ", ": <N>s ", ln) for ln in stderr.splitlines()]
    assert (p.returncode, lines, time.monotonic() - t < 5) == (143, [
        "PHASE-START fg: <N>s (wall)",
        "PHASE fg: <N>s (wall, failed (exit 143))",
        f"alert (suppressed): ❌ cw-s3 {SCAN}: fg failed (exit 143, terminated by SIGTERM)",
    ], True)
    assert gone(int(pidfile.read_text())) == []


def test_explicit_times_for_phases_before_the_job(job):
    """A run that starts before its job (`start -t`: its capture's start) and phases that ended before the job existed
    (`phase-end -t`): recorded at the stated instants; their PHASE lines count from `$DT_JOB_START`, so negative."""
    assert job("start", "-t", str(T0 - 300), SCAN, at=T0 + 1) == 0
    assert job("phase-begin", "-b", str(T0 - 300), "capture", at=T0 + 1) == 0
    assert job("phase-end", "-M", "-t", str(T0 - 120), "capture", at=T0 + 1) == 0
    assert job("phase-begin", "-b", str(T0 - 120), "queue", at=T0 + 1) == 0
    assert job("phase-end", "-M", "-t", str(T0), "queue", at=T0 + 1) == 0
    assert job("phase-begin", "fetch", at=T0 + 1) == 0
    assert job("phase-end", "-M", "fetch", at=T0 + 5) == 0
    assert job("finish", "-x", "0", at=T0 + 6) == 0
    assert job.rows("SELECT status, started_ts, finished_ts FROM scan_runs") == [("succeeded", T0 - 300, T0 + 6)]
    assert job.rows("SELECT phase, started_ts, finished_ts, status FROM scan_run_phases ORDER BY seq") == [
        ("capture", T0 - 300, T0 - 120, "done"),
        ("queue", T0 - 120, T0, "done"),
        ("fetch", T0 + 1, T0 + 5, "done"),
    ]
    assert [ln for ln in job.take() if ln.startswith("PHASE ")] == [
        "PHASE capture: -120s (wall)", "PHASE queue: 0s (wall)", "PHASE fetch: 5s (wall)",
    ]


def test_an_unphased_failure_inside_a_begun_phase_names_it(job):
    """`set -e` kills the script inside a begun phase (no failing `phase-end`): `finish` names the one running phase."""
    assert job("start", SCAN, at=T0 + 1) == 0
    assert job("phase-begin", "listing", at=T0 + 2) == 0
    assert job("phase-end", "-M", "listing", at=T0 + 3) == 0
    assert job("phase-begin", "index", at=T0 + 4) == 0
    job.mp.setenv("DT_ALERTS", "stderr")
    assert job("finish", "-x", "3", "-e", "exit 3: cp a b (run.sh:9)", at=T0 + 5) == 0
    assert job.rows("SELECT status, failed_phase, error FROM scan_runs") == [("failed", "index", "exit 3: cp a b (run.sh:9)")]
    assert [ln for ln in job.take() if ln.startswith("alert")] == [
        f"alert (suppressed): ❌ cw-s3 {SCAN}: run failed (exit 3) in index: exit 3: cp a b (run.sh:9)",
    ]
