"""`dt-cloud scan-run start / phase-begin / phase-end / phase -- CMD / finish` (specs/scan-job-wrapper.md): the
recorded rows, byte-exact PHASE lines, alert payloads through a fake transport, one alert per failure across
processes, redaction, and `-T`'s process-group kill."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from disk_tree.notify import push
from dt_cloud import scan_runs
from dt_cloud.scan_runs import SqliteSink, cli, duration, parse_profile, phase_line, redact

SITE = Path(__file__).parents[2] / "site"
DDL = (SITE / "migrations/cw/0016_scan_runs.sql").read_text()
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
