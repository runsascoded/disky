"""An idempotent, retried Batch submit (`batch.submit_job`'s `exists_ok` / `retry_for`, `batch.job_id_at`, and
`dt-cloud batch submit -e -r -t`): a job named for its period, a 409 for that name counted as submitted, transient
failures retried with exponential backoff for a bounded window, then a failure the caller can alert on."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from click.testing import CliRunner

from dt_cloud import batch, cli
from dt_cloud.batch import SubmitFailed, job_id_at, submit_job
from dt_cloud.cli import main

LABELS = {"app": "disky", "deployment": "test", "component": "scan"}
SPEC = {"labels": LABELS, "allocationPolicy": {"labels": LABELS}, "taskGroups": [{"taskSpec": {}}]}
URL = "https://batch.googleapis.com/v1/projects/p/locations/my-region/jobs"
AT = datetime(2026, 11, 8, 7, 0, 3, tzinfo=timezone.utc)


class Response:
    def __init__(self, status_code: int, job_id: str):
        self.status_code, self.job_id, self.text = status_code, job_id, f"status {status_code}"

    def json(self):
        return {"name": f"projects/p/locations/my-region/jobs/{self.job_id}"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class Batch:
    """`batch.session()` answering each POST from `answers` in turn: a status, or an exception to raise."""

    def __init__(self, *answers: int | Exception):
        self.answers, self.posts = list(answers), []

    def __call__(self):
        return self

    def post(self, url, json, params, **kw):
        self.posts.append((url, params, kw))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer, params["job_id"])


class Clock:
    """A monotonic clock that only `sleep` advances; `sleeps` records each wait."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def clock():
    return Clock()


def submit(net: Batch, clock: Clock, monkeypatch, **kw) -> str:
    monkeypatch.setattr(batch, "session", net)
    return submit_job(SPEC, "scan-20261108", None, "p", sleep=clock.sleep, clock=clock, **kw)


def post(**kw) -> tuple:
    return (URL, {"job_id": "scan-20261108"}, kw)


BOUNDED = {"timeout": (10, 60)}


# ── the id: a function of the period ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("template, when, expected", [
    ("gcs-usage-snapshot-%Y%m%d", AT, "gcs-usage-snapshot-20261108"),
    # the UTC day, whatever zone the caller's clock reads in: 23:30 at UTC-5 is the next UTC day
    ("gcs-usage-snapshot-%Y%m%d", datetime(2026, 11, 7, 23, 30, tzinfo=timezone(timedelta(hours=-5))), "gcs-usage-snapshot-20261108"),
    ("access-%Y%m%d-%H", AT, "access-20261108-07"),
    ("race-1", AT, "race-1"),
])
def test_job_id_at_names_the_period(template, when, expected):
    assert job_id_at(template, when) == expected


RULE = "at most 63 characters, a lowercase letter first, then lowercase letters, digits and '-', not ending in '-'"


@pytest.mark.parametrize("template, job_id", [
    ("scan-%Y-%m-%dT%H", "scan-2026-11-08T07"),
    ("%Y%m%d-scan", "20261108-scan"),
    ("scan_%Y%m%d", "scan_20261108"),
    ("scan-%Y%m%d-", "scan-20261108-"),
    ("s" * 55 + "-%Y%m%d", "s" * 55 + "-20261108"),
])
def test_job_id_at_refuses_an_id_batch_would(template, job_id):
    with pytest.raises(ValueError) as e:
        job_id_at(template, AT)
    assert str(e.value) == f"Batch job id {job_id!r}: {RULE}"


# ── 409: the job already exists ───────────────────────────────────────────────────────────────────────────────────────

def test_an_existing_job_counts_as_submitted(monkeypatch, clock, capsys):
    net = Batch(409)
    assert submit(net, clock, monkeypatch, exists_ok=True) == "scan-20261108"
    assert (net.posts, clock.sleeps, capsys.readouterr().err) == (
        [post()], [], "note: Batch job scan-20261108 already exists: counted as submitted\n")


def test_without_exists_ok_a_409_is_an_error(monkeypatch, clock):
    net = Batch(409)
    with pytest.raises(requests.HTTPError) as e:
        submit(net, clock, monkeypatch)
    assert (str(e.value), net.posts) == ("409 Error", [post()])


# ── retries: transient failures, then success ─────────────────────────────────────────────────────────────────────────

def test_a_5xx_and_a_connection_failure_are_retried_with_backoff(monkeypatch, clock, capsys):
    net = Batch(502, requests.ConnectionError("reset"), 429, 200)
    assert submit(net, clock, monkeypatch, exists_ok=True, retry_for=1800) == "scan-20261108"
    assert (net.posts, clock.sleeps) == ([post(**BOUNDED)] * 4, [5, 10, 20])
    assert capsys.readouterr().err.splitlines() == [
        "note: Batch job scan-20261108: attempt 1 failed (HTTP 502); retrying in 5s",
        "note: Batch job scan-20261108: attempt 2 failed (ConnectionError: reset); retrying in 10s",
        "note: Batch job scan-20261108: attempt 3 failed (HTTP 429); retrying in 20s",
    ]


def test_a_lost_answer_after_the_job_was_created_is_not_a_second_job(monkeypatch, clock, capsys):
    """The 2026-11-08 case: Batch created the job but the caller saw a 502. The retry names the same job, so Batch
    answers 409, and that is the success it was."""
    net = Batch(502, 409)
    assert submit(net, clock, monkeypatch, exists_ok=True, retry_for=1800) == "scan-20261108"
    assert (net.posts, clock.sleeps) == ([post(**BOUNDED)] * 2, [5])
    assert capsys.readouterr().err.splitlines() == [
        "note: Batch job scan-20261108: attempt 1 failed (HTTP 502); retrying in 5s",
        "note: Batch job scan-20261108 already exists: counted as submitted",
    ]


def test_backoff_doubles_to_its_cap(monkeypatch, clock):
    net = Batch(*[503] * 9, 200)
    assert submit(net, clock, monkeypatch, exists_ok=True, retry_for=3600) == "scan-20261108"
    assert clock.sleeps == [5, 10, 20, 40, 80, 160, 300, 300, 300]


# ── giving up ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_gives_up_when_the_window_runs_out(monkeypatch, clock, capsys):
    net = Batch(*[503] * 10)
    with pytest.raises(SubmitFailed) as e:
        submit(net, clock, monkeypatch, exists_ok=True, retry_for=30)
    assert str(e.value) == "Batch job scan-20261108: submit failed 4 times over 30s; the last: HTTP 503"
    # the last wait is cut to the window: no attempt starts after it
    assert (len(net.posts), clock.sleeps, clock.t) == (4, [5, 10, 15], 30)


def test_a_refusal_is_not_retried(monkeypatch, clock):
    net = Batch(400)
    with pytest.raises(requests.HTTPError) as e:
        submit(net, clock, monkeypatch, exists_ok=True, retry_for=1800)
    assert (str(e.value), net.posts, clock.sleeps) == ("400 Error", [post(**BOUNDED)], [])


def test_an_unexpected_exception_is_not_retried(monkeypatch, clock):
    net = Batch(KeyError("boom"))
    with pytest.raises(KeyError):
        submit(net, clock, monkeypatch, exists_ok=True, retry_for=1800)
    assert (len(net.posts), clock.sleeps) == (1, [])


def test_without_retry_nothing_is_retried(monkeypatch, clock):
    net = Batch(502)
    with pytest.raises(requests.HTTPError) as e:
        submit(net, clock, monkeypatch, exists_ok=True)
    assert (str(e.value), net.posts, clock.sleeps) == ("502 Error", [post()], [])


@pytest.mark.parametrize("job_id, exists_ok", [(None, True), ("scan-20261108", False)])
def test_a_retry_needs_an_id_and_exists_ok(monkeypatch, clock, job_id, exists_ok):
    net = Batch(200)
    monkeypatch.setattr(batch, "session", net)
    with pytest.raises(ValueError) as e:
        submit_job(SPEC, job_id, None, "p", exists_ok=exists_ok, retry_for=60, sleep=clock.sleep, clock=clock)
    assert (str(e.value), net.posts) == (
        f"Batch job {job_id or '(unnamed)'}: a retried submit needs a job id and exists_ok: without them, a retry after a "
        "lost answer starts a second job", [])


# ── the CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────────

class Fixed(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT


def _cli(monkeypatch, tmp_path, *args: str, job_id: str = "scan-%Y%m%d"):
    calls = []

    def fake_submit(spec, job_id, region, project, **kw):
        calls.append((job_id, region, project, kw))
        if kw["retry_for"] and job_id == "fail-20261108":
            raise SubmitFailed(f"Batch job {job_id}: submit failed 7 times over 1800s; the last: HTTP 502")
        return job_id

    monkeypatch.setattr(batch, "submit_job", fake_submit)
    monkeypatch.setattr(cli, "dt", SimpleNamespace(datetime=Fixed, timezone=timezone))
    spec = tmp_path / "spec.json"
    spec.write_text('{"taskGroups": [{"taskSpec": {}}]}')
    r = CliRunner().invoke(main, ["batch", "submit", "-c", "scan", "-p", "p", *args, job_id, str(spec)])
    return r, calls


def test_cli_names_the_job_for_the_day_and_retries(monkeypatch, tmp_path):
    r, calls = _cli(monkeypatch, tmp_path, "-t", "-e", "-r", "1800")
    assert (r.exit_code, r.stdout) == (0, "scan-20261108\n")
    assert calls == [("scan-20261108", "my-region", "p", {"buckets": (), "cross_region": None, "exists_ok": True, "retry_for": 1800})]


def test_cli_without_t_takes_the_id_literally(monkeypatch, tmp_path):
    r, calls = _cli(monkeypatch, tmp_path, "-e", job_id="race-1")
    assert (r.exit_code, r.stdout, [c[0] for c in calls]) == (0, "race-1\n", ["race-1"])


def test_cli_exits_1_naming_the_failure_when_retries_run_out(monkeypatch, tmp_path):
    r, calls = _cli(monkeypatch, tmp_path, "-t", "-e", "-r", "1800", job_id="fail-%Y%m%d")
    assert (r.exit_code, r.stdout, r.stderr) == (1, "", "Batch job fail-20261108: submit failed 7 times over 1800s; the last: HTTP 502\n")


@pytest.mark.parametrize("args, job_id, message", [
    (["-r", "60"], "scan-1", "-r/--retry-for needs -e/--exists-ok: without it, a retry after a lost answer fails on the job it created"),
    (["-t", "-e"], "Scan-%Y%m%d", f"Batch job id 'Scan-20261108': {RULE}"),
])
def test_cli_usage_errors_submit_nothing(monkeypatch, tmp_path, args, job_id, message):
    r, calls = _cli(monkeypatch, tmp_path, *args, job_id=job_id)
    assert (r.exit_code, r.stderr.splitlines()[-1], calls) == (2, f"Error: {message}", [])
