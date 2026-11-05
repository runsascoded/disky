"""The sweep's minimum soft-delete retention: `-s` on `sweep execute*` (what the site reads from `sweep_settings`, 7 when
it has no row), the refusal under it, and the run's own record of what it ran under and what undo it has."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from google.cloud import storage

from dt_cloud import cli, index_footer
from dt_cloud.sweep_exec import execute_plan, narrowest_retention, record_run
from dt_cloud.sweep_job import reviewed_job_spec
from dt_cloud.sweep_reviewed import execute_manifest
from test_sweep_exec import FakeClient, _client, _plan_dir
from test_sweep_execute_manifest import (  # noqa: F401 (fixtures)
    acknowledge, d1, generation_run, generation_source, refuse, reviewed_source, source, two_buckets,
)
from test_sweep_job import IMAGE, template  # noqa: F401 (fixture)

DAY = 86400
REFUSED = "b1: permissions/soft-delete guard refused reviewed deletion"


def client_at(days: int) -> FakeClient:
    client = _client()
    client.soft_days = days
    return client


class TestExecutePlan:
    def test_refuses_at_the_default(self, tmp_path):
        with pytest.raises(SystemExit) as refused:
            execute_plan(str(_plan_dir(tmp_path)), for_real=True, client=client_at(0))
        assert str(refused.value) == "b1: soft delete retention 0d < required 7d — refusing --for-real"

    def test_runs_at_zero_with_a_minimum_of_zero(self, tmp_path):
        summary = execute_plan(str(_plan_dir(tmp_path)), for_real=True, min_soft_delete_days=0, client=client_at(0))
        assert (summary["min_soft_delete_days"], summary["buckets"]["b1"]["soft_delete_days"]) == (0, 0)
        assert summary["buckets"]["b1"]["decisions"]["delete"] > 0

    def test_still_refuses_a_retention_under_the_setting(self, tmp_path):
        with pytest.raises(SystemExit) as refused:
            execute_plan(str(_plan_dir(tmp_path)), for_real=True, min_soft_delete_days=14, client=client_at(7))
        assert str(refused.value) == "b1: soft delete retention 7d < required 14d — refusing --for-real"

    def test_a_dry_run_records_the_minimum_it_would_be_refused_under(self, tmp_path):
        summary = execute_plan(str(_plan_dir(tmp_path)), client=client_at(0))
        assert (summary["min_soft_delete_days"], summary["buckets"]["b1"]["soft_delete_days"]) == (7, 0)


class TestExecuteManifest:
    def run(self, source, tmp_path, days, **kwargs):  # noqa: F811
        directory, plan = source
        client = FakeClient(blobs={}, soft_days=days)
        return execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=client, **kwargs)

    def test_refuses_at_the_default(self, source, tmp_path):  # noqa: F811
        with pytest.raises(ValueError) as refused:
            self.run(source, tmp_path, 0, deleter=refuse)
        assert str(refused.value) == REFUSED
        assert (tmp_path / "out").exists() is False

    def test_runs_at_zero_with_a_minimum_of_zero(self, source, tmp_path):  # noqa: F811
        calls = []
        summary = self.run(source, tmp_path, 0, min_soft_delete_days=0, deleter=acknowledge(calls))
        assert (summary["min_soft_delete_days"], summary["buckets"]["b1"]["soft_delete_days"]) == (0, 0)
        assert sum(map(len, calls)) == 1005

    def test_still_refuses_a_retention_under_the_setting(self, source, tmp_path):  # noqa: F811
        with pytest.raises(ValueError) as refused:
            self.run(source, tmp_path, 7, min_soft_delete_days=14, deleter=refuse)
        assert str(refused.value) == REFUSED

    def test_a_missing_permission_still_refuses_at_a_minimum_of_zero(self, source, tmp_path, monkeypatch):  # noqa: F811
        monkeypatch.setattr("dt_cloud.sweep_reviewed._missing_perms", lambda _: ["storage.objects.delete"])
        with pytest.raises(ValueError) as refused:
            self.run(source, tmp_path, 7, min_soft_delete_days=0, deleter=refuse)
        assert str(refused.value) == REFUSED


def run_row(db: sqlite3.Connection) -> dict:
    return dict(db.execute("SELECT mode, finished_ts, min_soft_delete_days, soft_delete_days, undo_deadline FROM deletion_runs").fetchone())


@pytest.fixture
def run_cli(generation_source, tmp_path, monkeypatch, d1):  # noqa: F811
    """The dispatched real run's command line at bucket retention `days`, with `flags` before its source."""
    command, directory, plan = generation_source
    calls, run, args = generation_run(command, directory, plan, tmp_path, monkeypatch)

    def invoke(days: int, *flags: str):
        storage.Client().soft_days = days  # `generation_run`'s one client
        started = []
        monkeypatch.setattr("dt_cloud.sweep_progress.write_samples", lambda *_: started.append(run_row(d1)))
        result = CliRunner().invoke(cli.main, [*args[:-1], *flags, args[-1]])
        return result, calls, started
    return invoke


class TestRecord:
    def test_a_run_at_zero_records_its_minimum_and_no_undo_deadline(self, run_cli, d1):  # noqa: F811
        result, calls, _ = run_cli(0, "-s", "0")
        assert (result.exit_code, result.exception) == (0, None)
        assert len(calls) > 0
        row = run_row(d1)
        assert row["finished_ts"] is not None
        assert row == {"mode": "real", "finished_ts": row["finished_ts"], "min_soft_delete_days": 0, "soft_delete_days": 0, "undo_deadline": None}

    def test_the_undo_deadline_follows_the_buckets_actual_retention(self, run_cli, d1):  # noqa: F811
        result, _, _ = run_cli(14, "-s", "7")
        assert (result.exit_code, result.exception) == (0, None)
        row = run_row(d1)
        assert row == {"mode": "real", "finished_ts": row["finished_ts"], "min_soft_delete_days": 7, "soft_delete_days": 14, "undo_deadline": row["finished_ts"] + 14 * DAY}

    def test_the_default_is_seven_and_its_row_says_so(self, run_cli, d1):  # noqa: F811
        result, _, _ = run_cli(7)
        assert (result.exit_code, result.exception) == (0, None)
        row = run_row(d1)
        assert row == {"mode": "real", "finished_ts": row["finished_ts"], "min_soft_delete_days": 7, "soft_delete_days": 7, "undo_deadline": row["finished_ts"] + 7 * DAY}

    def test_the_row_states_both_while_the_run_is_in_flight(self, run_cli):
        # A job killed mid-run is closed by `sweepReflect` from this row, so it must already hold what the run measured.
        _, _, started = run_cli(0, "-s", "0")
        assert started[0] == {"mode": "real", "finished_ts": None, "min_soft_delete_days": 0, "soft_delete_days": 0, "undo_deadline": None}

    def test_a_retention_under_the_setting_refuses_before_any_delete(self, run_cli, d1):  # noqa: F811
        result, calls, _ = run_cli(7, "-s", "14")
        assert result.exit_code == 1
        assert str(result.exception) == REFUSED
        assert calls == []
        assert d1.execute("SELECT count(*) FROM deletion_runs").fetchone()[0] == 0

    @pytest.mark.parametrize("value", ["-1", "91", "x"])
    def test_a_value_out_of_range_is_a_usage_error(self, run_cli, value):
        result, calls, _ = run_cli(7, "-s", value)
        assert (result.exit_code, calls) == (2, [])

    def test_a_dry_run_records_its_minimum_too(self, tmp_path, monkeypatch, d1):  # noqa: F811
        plan_dir = _plan_dir(tmp_path)
        summary = json.loads((plan_dir / "plan-summary.json").read_text())
        (plan_dir / "plan-summary.json").write_text(json.dumps({**summary, "plan_id": 1, "plan_name": "p", "date": "2026-01-01"}))
        monkeypatch.setattr(cli, "_hard_exit", lambda: None)
        monkeypatch.setattr("google.cloud.storage.Client", lambda: client_at(0))
        monkeypatch.setattr("dt_cloud.sweep_progress.write_samples", lambda *_: None)
        result = CliRunner().invoke(cli.main, ["sweep", "execute", "-s", "0", str(plan_dir)])
        assert (result.exit_code, result.exception) == (0, None)
        row = run_row(d1)
        assert row == {"mode": "dry", "finished_ts": row["finished_ts"], "min_soft_delete_days": 0, "soft_delete_days": 0, "undo_deadline": None}


@pytest.mark.parametrize("days, deadline", [(0, None), (3, 100 + 3 * DAY), (7, 100 + 7 * DAY), (30, 100 + 30 * DAY)])
def test_record_run_deadline_is_the_narrowest_measured_retention(monkeypatch, days, deadline):
    sent = []
    monkeypatch.setattr(index_footer, "_creds", lambda: ("token", "account"))
    monkeypatch.setattr(index_footer, "_d1_query", lambda sql, *_: sent.append(sql) or [])
    monkeypatch.setattr("dt_cloud.sweep_exec.record_bands", lambda *_: 0)
    record_run({"for_real": True, "plan": "gs://x", "buckets": {}}, {"date": "2026-01-01", "plan_id": 1}, "ann", 50, 100, days, 0)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE deletion_runs (run_id TEXT PRIMARY KEY, plan, scan, head, exec_head, actor, mode, started_ts, finished_ts, deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, drift_dirs, ledger_drift_dirs, undo_deadline, log_dir, plan_id, min_soft_delete_days, soft_delete_days)")
    db.execute(sent[0])
    assert db.execute("SELECT undo_deadline, min_soft_delete_days, soft_delete_days FROM deletion_runs").fetchall() == [(deadline, 0, days)]


class TestNarrowestRetention:
    """What a run's undo rests on: only the buckets it deleted from, so a bucket of skips can't make it read "no undo"."""

    DELETES = {"soft_delete_days": 7, "decisions": {"delete": 4}}
    SKIPS = {"soft_delete_days": 0, "decisions": {"skipped_no_generation": 3}}

    @pytest.mark.parametrize("entries, narrowest", [
        ([DELETES, SKIPS], 7),
        ([DELETES, {**DELETES, "soft_delete_days": 3}], 3),
        ([DELETES, {**SKIPS, "decisions": {"delete": 1}}], 0),
        # Each may have deleted, whatever its tally says: a delete with no answer, or a bucket cut short.
        ([DELETES, {**SKIPS, "failed_dirs": [{"dir": "a", "objects": 1}]}], 0),
        ([DELETES, {**SKIPS, "interrupted": {"roots_skipped": 1, "roots": 2}}], 0),
        ([DELETES, {**SKIPS, "error": "boom"}], 0),
        # Deleted nothing anywhere: what they measured still stands.
        ([SKIPS, {**SKIPS, "soft_delete_days": 7}], 0),
        ([{"decisions": {"delete": 1}}], None),
        ([], None),
    ])
    def test_narrowest(self, entries, narrowest):
        assert narrowest_retention(entries) == narrowest

    def test_a_run_with_a_bucket_of_skips_at_zero_keeps_the_undo_of_the_bucket_it_deleted_from(self, source, tmp_path):  # noqa: F811
        directory, plan = source
        two_buckets(directory, plan)
        client = FakeClient(blobs={})
        real = client.get_bucket
        client.get_bucket = lambda name: SimpleNamespace(soft_delete_policy=None) if name == "b2" else real(name)
        started = []
        result = execute_manifest(
            str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=client, deleter=acknowledge(),
            on_prepared=lambda _, days: started.append(days),
        )
        assert (result["buckets"]["b1"]["soft_delete_days"], result["buckets"]["b2"]["soft_delete_days"]) == (7, 0)
        assert (started, narrowest_retention(result["buckets"].values())) == ([7], 7)


class TestUndoCli:
    ROW = {"run_id": "2026-01-01-p1/20260101T000000Z", "mode": "real", "started_ts": 1, "finished_ts": 2, "undo_deadline": None, "undo_state": "none", "log_dir": "gs://x/run", "deleted_objects": 5}

    def invoke(self, monkeypatch, row: dict, *flags: str):
        restored = []
        monkeypatch.setattr(cli, "_hard_exit", lambda: None)
        monkeypatch.setattr(index_footer, "_creds", lambda: ("token", "account"))
        monkeypatch.setattr(index_footer, "_d1_query", lambda *_: [row])
        monkeypatch.setattr("dt_cloud.sweep_exec.undo_run", lambda log_dir, **kwargs: restored.append(kwargs["only_buckets"]) or {})
        monkeypatch.setattr("dt_cloud.sweep_exec.record_undo", lambda *_: "partial")
        return CliRunner().invoke(cli.main, ["sweep", "undo", *flags, row["run_id"]]), restored

    def test_refuses_a_run_with_soft_delete_off(self, monkeypatch):
        result, restored = self.invoke(monkeypatch, {**self.ROW, "soft_delete_days": 0})
        assert (result.exit_code, restored) == (1, [])
        assert str(result.exception) == (
            "2026-01-01-p1/20260101T000000Z: no undo: a bucket in this run had soft delete off, so its deletes are permanent. "
            "Name the buckets that retained theirs with -b (their retention: the run's `deleted-summary.json`)"
        )

    def test_a_bucket_cut_can_still_restore_the_buckets_that_retained(self, monkeypatch):
        result, restored = self.invoke(monkeypatch, {**self.ROW, "soft_delete_days": 0}, "-b", "kept")
        assert (result.exit_code, result.exception, restored) == (0, None, [("kept",)])

    def test_refuses_a_run_that_required_nothing_and_measured_nothing(self, monkeypatch):
        # A killed `sweep execute --for-real -s 0`: its start row has no measured retention, and nothing promised one.
        result, restored = self.invoke(monkeypatch, {**self.ROW, "min_soft_delete_days": 0, "soft_delete_days": None})
        assert (result.exit_code, restored) == (1, [])
        assert str(result.exception) == (
            "2026-01-01-p1/20260101T000000Z: no undo promised: it required no soft delete and recorded no retention, so treat its deletes as permanent. "
            "Name the buckets to try with -b"
        )

    def test_a_bucket_cut_can_still_try_such_a_run(self, monkeypatch):
        result, restored = self.invoke(monkeypatch, {**self.ROW, "min_soft_delete_days": 0, "soft_delete_days": None}, "-b", "kept")
        assert (result.exit_code, result.exception, restored) == (0, None, [("kept",)])

    def test_a_run_that_required_days_and_measured_nothing_is_undone(self, monkeypatch):
        result, restored = self.invoke(monkeypatch, {**self.ROW, "min_soft_delete_days": 3, "soft_delete_days": None})
        assert (result.exit_code, result.exception, restored) == (0, None, [()])

    def test_a_run_from_before_the_column_is_undone_as_before(self, monkeypatch):
        result, restored = self.invoke(monkeypatch, self.ROW)
        assert (result.exit_code, result.exception, restored) == (0, None, [()])


class TestReviewedJobSpec:
    def last_line(self, spec: dict) -> str:
        return spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1].splitlines()[-1]

    def test_no_flag_by_default(self, template):  # noqa: F811
        # The image is the caller's pin, and an image from before the flag would refuse it.
        spec = reviewed_job_spec(template, "src", "plan", "out", IMAGE)
        assert self.last_line(spec) == "dt-cloud sweep execute-reviewed -e xml -j 32 -r 8000 -p plan -o out -w /work/reviewed -B 6 -c guided src"

    def test_a_named_minimum_rides_on_the_command(self, template):  # noqa: F811
        spec = reviewed_job_spec(template, "src", "plan", "out", IMAGE, min_soft_delete_days=0)
        assert self.last_line(spec) == "dt-cloud sweep execute-reviewed -e xml -j 32 -r 8000 -p plan -o out -w /work/reviewed -B 6 -c guided -s 0 src"
