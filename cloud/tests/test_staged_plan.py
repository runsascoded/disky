"""`sweep manifest --plan`: the staged set as the delete set (specs/staged-delete.md)
— plan.json parsing, the per-dir rule, the run record's `plan_id`, and the
manifest command end to end over local shards."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud.staged_plan import PlanError, StagedPlan, parse_plan, split_prefix
from dt_cloud.sweep_exec import record_run_start, run_id_for

E1 = "marin-us-east1"
W4 = "marin-eu-west4"
PLAN = {
    "plan_id": 12,
    "name": "Staged",
    "sweep": [f"gs://{E1}/ckpt/old/", f"gs://{W4}/tmp/x", f"gs://{E1}/ckpt/old/"],
}


def test_split_prefix_normalizes_the_trailing_slash() -> None:
    assert split_prefix(f"gs://{E1}/a/b") == (E1, "a/b/")
    assert split_prefix(f"gs://{E1}/a/b/") == (E1, "a/b/")


def test_parse_groups_items_by_bucket_relative_and_sorted() -> None:
    plan = parse_plan(PLAN)
    assert plan == StagedPlan(
        plan_id=12,
        name="Staged",
        sweep={W4: ("tmp/x/",), E1: ("ckpt/old/",)},
    )
    assert plan.buckets == (W4, E1)
    assert plan.bands(E1) == (f"gs://{E1}/ckpt/old/",)
    assert plan.bands("marin-us-west4") == ()


def test_name_defaults_from_the_id() -> None:
    plan = parse_plan({"plan_id": 3, "sweep": [f"gs://{E1}/x/"]})
    assert plan == StagedPlan(plan_id=3, name="plan 3", sweep={E1: ("x/",)})


def test_classify_a_staged_prefix_covers_its_subtree() -> None:
    plan = parse_plan(PLAN)
    cases = {
        (E1, "ckpt/old"): "eligible",
        (E1, "ckpt/old/run7"): "eligible",
        (E1, "ckpt/old/best"): "eligible",
        (E1, "ckpt/older"): "outside_bands",
        (E1, "ckpt"): "outside_bands",
        (E1, ""): "outside_bands",
        (W4, "tmp/x"): "eligible",
        (W4, "tmp"): "outside_bands",
        ("marin-us-west4", "ckpt/old"): "outside_bands",
    }
    assert {k: plan.classify(*k) for k in cases} == cases


@pytest.mark.parametrize("bad", [
    "not a dict",
    {"sweep": [f"gs://{E1}/x/"]},
    {"plan_id": "12", "sweep": [f"gs://{E1}/x/"]},
    {"plan_id": True, "sweep": [f"gs://{E1}/x/"]},
    {"plan_id": 12, "sweep": []},
    {"plan_id": 12, "sweep": f"gs://{E1}/x/"},
    {"plan_id": 12, "sweep": [f"s3://{E1}/x/"]},
    {"plan_id": 12, "sweep": [f"gs://{E1}/"]},
    {"plan_id": 12, "sweep": [f"gs://{E1}/a/../b/"]},
    {"plan_id": 12, "sweep": [f"gs://{E1}/a//b/"]},
    {"plan_id": 12, "sweep": [f"gs://{E1}/a\\b/"]},
    {"plan_id": 12, "sweep": [f"gs://{E1}/x/"], "name": 5},
])
def test_malformed_plan_raises(bad: object) -> None:
    with pytest.raises(PlanError):
        parse_plan(bad)


T0 = int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp())


def test_run_id_names_the_plan() -> None:
    assert run_id_for({"date": "2026-09-01", "plan_id": 12}, T0) == "2026-09-01-p12/20260901T000000Z"


def test_record_run_start_links_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import index_footer

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(index_footer, "_d1_query", lambda sql, acct, tok: sent.append((sql, acct, tok)) or [])
    summary = {"date": "2026-09-01", "plan_id": 12}
    run_id = record_run_start(summary, "gs://b/runs/j1", actor="me@x", started_ts=T0, for_real=False, buckets=(E1,))
    assert run_id == "2026-09-01-p12/20260901T000000Z"
    assert sent == [(
        "INSERT INTO deletion_runs (run_id, plan, scan, head, exec_head, actor, mode, started_ts, finished_ts, "
        "deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, drift_dirs, ledger_drift_dirs, "
        "undo_deadline, log_dir, buckets, plan_id) VALUES ("
        "'2026-09-01-p12/20260901T000000Z', 'gs://b/runs/j1', '2026-09-01', 0, 0, 'me@x', "
        f"'dry', {T0}, NULL, 0, 0, 0, 0, 0, 0, NULL, 'gs://b/runs/j1', "
        f"'{E1}', 12)",
        "acct", "tok",
    )]


def _shards(root: Path, date: str, bucket: str, rows: list[tuple[str, int]]) -> None:
    d = root / "listing" / date / bucket
    d.mkdir(parents=True)
    t = pa.table({
        "name": pa.array([n for n, _ in rows], pa.string()),
        "size_bytes": pa.array([s for _, s in rows], pa.int64()),
        "storage_class_id": pa.array([1] * len(rows), pa.int8()),
        "created": pa.array([dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)] * len(rows), pa.timestamp("us", tz="UTC")),
    })
    pq.write_table(t, d / "0.parquet")


@pytest.fixture
def listing(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    _shards(root, "2026-09-01", E1, [("ckpt/old/a", 10), ("ckpt/old/best/m", 20), ("ckpt/new/x", 30), ("root-file", 5)])
    _shards(root, "2026-09-01", W4, [("tmp/x/1", 7), ("tmp/x/deep/2", 8), ("other/3", 9)])
    _shards(root, "2026-09-01", "marin-us-west4", [("y/z", 1)])
    return root


def _manifest(runner_args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from dt_cloud import cli

    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(PLAN))
    out = tmp_path / "out"
    r = CliRunner().invoke(cli.main, ["sweep", "manifest", "-d", "2026-09-01", "--plan", str(plan_path), "-o", str(out), *runner_args])
    return r, out


def test_manifest_from_plan_spans_buckets(listing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r, out = _manifest(["-r", str(listing)], tmp_path, monkeypatch)
    assert (r.exit_code, r.exception) == (0, None)
    summary = json.loads((out / "plan-summary.json").read_text())
    assert summary == {
        "date": "2026-09-01",
        "plan_id": 12,
        "plan_name": "Staged",
        "approved": [f"gs://{W4}/tmp/x/", f"gs://{E1}/ckpt/old/"],
        "buckets": {
            W4: {
                "objects": 3, "dirs": 2,
                "eligible": {"bytes": 15, "objects": 2},
                "outside_bands": {"bytes": 9, "objects": 1},
            },
            E1: {
                "objects": 4, "dirs": 2,
                "eligible": {"bytes": 30, "objects": 2},
                "outside_bands": {"bytes": 35, "objects": 2},
            },
        },
        "total": {
            "eligible": {"bytes": 45, "objects": 4},
            "outside_bands": {"bytes": 44, "objects": 3},
        },
    }
    e1 = pq.read_table(out / "manifest" / f"{E1}.parquet").to_pylist()
    w4 = pq.read_table(out / "manifest" / f"{W4}.parquet").to_pylist()
    assert [(r["name"], r["size_bytes"], r["dir"]) for r in e1] == [("ckpt/old/a", 10, "ckpt/old"), ("ckpt/old/best/m", 20, "ckpt/old/best")]
    assert [(r["name"], r["size_bytes"], r["dir"]) for r in w4] == [("tmp/x/1", 7, "tmp/x"), ("tmp/x/deep/2", 8, "tmp/x/deep")]
    assert sorted(p.name for p in (out / "manifest").iterdir()) == [f"{W4}.parquet", f"{E1}.parquet"]


def test_bucket_flags_intersect_the_plans_buckets(listing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r, out = _manifest(["-r", str(listing), "-b", E1, "-b", "marin-us-west4"], tmp_path, monkeypatch)
    assert (r.exit_code, r.exception) == (0, None)
    summary = json.loads((out / "plan-summary.json").read_text())
    assert sorted(summary["buckets"]) == [E1]
    assert summary["approved"] == [f"gs://{E1}/ckpt/old/"]
    assert sorted(p.name for p in (out / "manifest").iterdir()) == [f"{E1}.parquet"]


def test_bucket_flags_disjoint_from_the_plan_refuse(listing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r, _ = _manifest(["-r", str(listing), "-b", "marin-us-west4"], tmp_path, monkeypatch)
    assert (r.exit_code, str(r.exception)) == (1, f"no plan bucket among -b marin-us-west4 (plan 12 names {W4}, {E1})")


def test_execute_takes_a_staged_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import cli, sweep_exec

    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    plan_dir = tmp_path / "run"
    plan_dir.mkdir()
    (plan_dir / "plan-summary.json").write_text(json.dumps({"date": "2026-09-01", "plan_id": 12, "plan_name": "Staged", "buckets": {}}))
    calls: list[dict] = []

    def fake_execute(plan_dir: str, **kw) -> dict:
        calls.append({"plan_dir": plan_dir, "for_real": kw["for_real"], "drift": kw["drift"]})
        return {"plan": plan_dir, "for_real": kw["for_real"], "drift": kw["drift"], "buckets": {}, "_plan": {}}

    monkeypatch.setattr(sweep_exec, "execute_plan", fake_execute)
    monkeypatch.setattr(sweep_exec, "stop_file_watch", lambda plan_dir, stop: None)
    r = CliRunner().invoke(cli.main, ["sweep", "execute", "--no-record", str(plan_dir)])
    assert (r.exit_code, r.exception) == (0, None)
    assert calls == [{"plan_dir": str(plan_dir), "for_real": False, "drift": "skip"}]
