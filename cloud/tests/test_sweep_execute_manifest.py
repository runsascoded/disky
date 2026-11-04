"""A real run with no reusable dry run deletes straight from its own manifest:
the reviewed executor's preflights, pacing, journal and undo log, over the
generations `sweep manifest` wrote, with no relist."""

import json
import re
import sqlite3
import threading
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from requests import Response

from dt_cloud import sweep_reviewed
from dt_cloud.sweep_exec import undo_run
from dt_cloud.sweep_manifest import MANIFEST_SCHEMA
from dt_cloud.sweep_reviewed import execute_manifest, prepare_manifest
from dt_cloud.sweep_xml import XmlDeleter
from test_sweep_exec import FakeBlob, FakeClient, T0, _decisions, _UndoClient, _UndoHandle
from test_sweep_manifest import AS_OF_PLAN, AS_OF_ROWS, B1, NOW, _as_of_root

DATE = "2026-10-03"
ROWS = [dict(name=f"a/d{i // 6}/o{i}", size_bytes=2, generation=i + 1, dir=f"a/d{i // 6}") for i in range(1005)]


def write_manifest(directory: Path, rows: list[dict], bucket: str = "b1", schema: pa.Schema = MANIFEST_SCHEMA) -> None:
    (directory / "manifest").mkdir(parents=True, exist_ok=True)
    columns = {field.name: [dict(storage_class_id=1, created=T0, **row).get(field.name) for row in rows] for field in schema}
    pq.write_table(pa.table(columns, schema=schema), directory / "manifest" / f"{bucket}.parquet")


def eligible(rows: list[dict]) -> dict:
    return {"bytes": sum(row["size_bytes"] for row in rows), "objects": len(rows)}


def summary_of(rows: list[dict], **extra) -> dict:
    return {
        "date": DATE, "plan_id": 1, "plan_name": "Staged", "approved": ["gs://b1/a/"], "as_of": {},
        "buckets": {"b1": {"objects": len(rows) + 3, "dirs": 168, "eligible": eligible(rows), "outside_bands": {"bytes": 30, "objects": 3}}},
        "total": {"eligible": eligible(rows), "outside_bands": {"bytes": 30, "objects": 3}},
        **extra,
    }


@pytest.fixture
def source(tmp_path):
    """What `sweep manifest -o <run>/source` leaves, beside the dispatch's plan.json."""
    directory = tmp_path / "run" / "source"
    write_manifest(directory, ROWS)
    (directory / "plan-summary.json").write_text(json.dumps(summary_of(ROWS)))
    plan = tmp_path / "run" / "plan.json"
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/"]}))
    return directory, plan


def rewrite(directory: Path, rows: list[dict], **extra) -> None:
    write_manifest(directory, rows)
    (directory / "plan-summary.json").write_text(json.dumps(summary_of(rows, **extra)))


def acknowledge(calls: list | None = None):
    def delete(client, bucket, batch):
        if calls is not None:
            calls.append([(blob.name, blob.generation) for blob in batch])
        return [(blob, "delete") for blob in batch]
    return delete


def refuse(*args):
    raise AssertionError("delete must not be called")


def test_deletes_the_manifests_generations_without_a_listing(source, tmp_path):
    directory, plan = source
    calls = []
    # A key written after the scan: live beside the manifest's, named by nothing.
    client = FakeClient(blobs={"b1": [FakeBlob("a/d0/new", 9, 999, T0)]})
    out = tmp_path / "out"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, workers=2, client=client, deleter=acknowledge(calls))
    assert sorted(calls, key=len, reverse=True) == [[(row["name"], row["generation"]) for row in ROWS[:1000]], [(row["name"], row["generation"]) for row in ROWS[1000:]]]
    assert client.listed == []
    bucket = result["buckets"]["b1"]
    assert {key: bucket[key] for key in ("decisions", "delete_bytes", "drift_dirs", "failed_dirs", "bands", "soft_delete_days", "missing_perms")} == {
        "decisions": {"delete": 1005}, "delete_bytes": 2010, "drift_dirs": [], "failed_dirs": [],
        "bands": {"gs://b1/a/": {"bytes": 2010, "objects": 1005}}, "soft_delete_days": 7, "missing_perms": [],
    }
    assert {key: result[key] for key in ("plan", "for_real", "diagnostic", "drift", "manifest_source", "backend", "delete_count_semantics")} == {
        "plan": str(out), "for_real": True, "diagnostic": False, "drift": "manifest-generations-no-relist", "manifest_source": str(directory),
        "backend": "xml", "delete_count_semantics": "acknowledged-generation-deletes",
    }
    written = json.loads((out / "plan-summary.json").read_text())
    assert {key: written[key] for key in ("date", "plan_id", "approved", "total", "contract", "manifest_source", "diagnostic")} == {
        "date": DATE, "plan_id": 1, "approved": ["gs://b1/a/"], "total": {"eligible": eligible(ROWS)},
        "contract": "manifest-delete-generations", "manifest_source": str(directory), "diagnostic": False,
    }
    assert _decisions(out, "deleted") == sorted((row["name"], "delete", row["generation"]) for row in ROWS)
    assert json.loads((out / "manifest-source.json").read_text()) == {"b1": [{"path": f"{directory}/manifest/b1.parquet", "rows": 1005}]}
    progress = json.loads((out / "progress/b1.json").read_text())
    assert {key: progress[key] for key in ("unit", "roots", "roots_done", "decisions", "done")} == {"unit": "batches", "roots": 2, "roots_done": 2, "decisions": {"delete": 1005}, "done": True}
    # the manifest stays as the listing wrote it, its summary too
    assert pq.read_table(directory / "manifest/b1.parquet").schema == MANIFEST_SCHEMA
    assert json.loads((directory / "plan-summary.json").read_text()) == summary_of(ROWS)


def xml_reply(*entries: tuple[str, str, int, str | None]) -> Response:
    body = "".join(
        f"<Deleted><Key>{key}</Key><VersionId>{generation}</VersionId></Deleted>" if tag == "Deleted"
        else f"<Error><Key>{key}</Key><VersionId>{generation}</VersionId><Code>{code}</Code></Error>"
        for tag, key, generation, code in entries
    )
    response = Response()
    response.status_code = 200
    response._content = f"<DeleteResult>{body}</DeleteResult>".encode()
    return response


def test_gone_and_overwritten_are_recorded_per_object(source, tmp_path):
    directory, plan = source
    rows = ROWS[:3]
    rewrite(directory, rows)
    posted = []

    class Http:
        def post(self, url, data, headers, timeout):
            posted.append(data)
            return xml_reply(
                ("Deleted", rows[0]["name"], 1, None),
                ("Error", rows[1]["name"], 2, "NoSuchVersion"),
                ("Error", rows[2]["name"], 3, "PreconditionFailed"),
            )

    xml = XmlDeleter(http=Http())
    out = tmp_path / "out"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=lambda client, bucket, batch: xml(client, "b1", batch))
    assert len(posted) == 1
    assert re.findall(r"<Key>([^<]+)</Key><VersionId>(\d+)</VersionId>", posted[0].decode()) == [(row["name"], str(row["generation"])) for row in rows]
    bucket = result["buckets"]["b1"]
    assert {key: bucket[key] for key in ("decisions", "delete_bytes", "bands", "failed_dirs")} == {
        "decisions": {"delete": 1, "skipped_gone": 1, "skipped_overwritten": 1},
        "delete_bytes": 2,
        "bands": {"gs://b1/a/": {"bytes": 2, "objects": 1, "gone": 1, "overwritten": 1}},
        "failed_dirs": [],
    }
    assert _decisions(out, "deleted") == [
        (rows[0]["name"], "delete", 1),
        (rows[1]["name"], "skipped_gone", 2),
        (rows[2]["name"], "skipped_overwritten", 3),
    ]


def test_validates_only_without_for_real(source, tmp_path):
    directory, plan = source
    out = tmp_path / "dry"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), client=FakeClient(blobs={}), deleter=refuse)
    assert (result["for_real"], result["diagnostic"], result["buckets"]["b1"]["decisions"]) == (False, True, {"delete": 1005})
    assert json.loads((out / "plan-summary.json").read_text())["diagnostic"] is True
    assert _decisions(out, "would-delete") == sorted((row["name"], "delete", row["generation"]) for row in ROWS)


@pytest.mark.parametrize("mutation,message", [
    ({"generation": None}, "b1: manifest row lacks a positive generation"),
    ({"generation": 0}, "b1: manifest row lacks a positive generation"),
    ({"generation": -1}, "b1: manifest row lacks a positive generation"),
    ({"name": "other/x"}, "b1: manifest row is outside staged prefixes"),
    ({"name": "ab/x"}, "b1: manifest row is outside staged prefixes"),
    ({"size_bytes": -1}, "b1: manifest row has invalid size"),
    ({"dir": None}, "b1: manifest row lacks directory metadata"),
])
def test_one_bad_row_refuses_the_run_before_any_delete(source, tmp_path, mutation, message):
    directory, plan = source
    # The last row: every valid row before it is held back too.
    write_manifest(directory, [*ROWS[:-1], {**ROWS[-1], **mutation}])
    with pytest.raises(ValueError) as error:
        execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=refuse)
    assert str(error.value) == message
    assert (tmp_path / "out").exists() is False


def test_refuses_a_repeated_identity(source, tmp_path):
    # `XmlDeleter` refuses a batch repeating one, mid-run: preflight catches it, however far apart the rows are.
    directory, plan = source
    rewrite(directory, [*ROWS, ROWS[0]])
    with pytest.raises(ValueError) as error:
        execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=refuse)
    assert str(error.value) == f"b1: manifest repeats {ROWS[0]['name']}@{ROWS[0]['generation']}"


def test_refuses_a_manifest_without_a_generation_column(source, tmp_path):
    directory, plan = source
    write_manifest(directory, ROWS, schema=pa.schema([field for field in MANIFEST_SCHEMA if field.name != "generation"]))
    with pytest.raises(ValueError, match="^b1: manifest has no generation column$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))


def test_refuses_a_manifest_its_summary_does_not_describe(source, tmp_path):
    directory, plan = source
    (directory / "plan-summary.json").write_text(json.dumps(summary_of(ROWS[:-1])))
    with pytest.raises(ValueError, match="^b1: manifest totals do not match its plan summary$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))
    (directory / "manifest/b1.parquet").unlink()
    with pytest.raises(ValueError, match="^b1: plan summary names eligible objects but the manifest is missing$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work2"))


def test_refuses_a_diagnostic_manifest(source, tmp_path):
    directory, plan = source
    rewrite(directory, ROWS, diagnostic=True)
    with pytest.raises(ValueError, match="^diagnostic manifests cannot be used for deletion$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))


def test_refuses_another_plans_manifest(source, tmp_path):
    directory, plan = source
    plan.write_text(json.dumps({"plan_id": 2, "sweep": ["gs://b1/a/"]}))
    with pytest.raises(ValueError, match="^manifest does not match the current staged plan$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/", "gs://b1/wider/"]}))
    with pytest.raises(ValueError, match="^manifest does not match the current staged plan$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))


def test_refuses_a_manifest_that_did_not_honor_the_items_as_of(source, tmp_path):
    directory, plan = source
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/"], "as_of": {"gs://b1/a/": "2026-10-01"}}))
    with pytest.raises(ValueError, match="^manifest predates the staged items' as_of scans; rebuild it$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))
    rewrite(directory, ROWS, as_of={"gs://b1/a/": "2026-10-01"})
    assert sorted(prepare_manifest(str(directory), str(plan), str(tmp_path / "held"))[2]) == ["b1"]


def test_refuses_a_bucket_outside_the_plan(source, tmp_path):
    directory, plan = source
    with pytest.raises(ValueError, match="^requested bucket is outside the staged plan$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"), ("b2",))
    # A summary naming a bucket no item covers: nothing could vouch for its keys.
    summary = summary_of(ROWS)
    summary["buckets"]["b2"] = {"objects": 1, "dirs": 1, "eligible": {"bytes": 1, "objects": 1}}
    (directory / "plan-summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="^b2: manifest bucket is outside the staged plan$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work2"))


def test_short_soft_delete_retention_refuses(source, tmp_path):
    directory, plan = source
    with pytest.raises(ValueError, match="^b1: permissions/soft-delete guard refused reviewed deletion$"):
        execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}, soft_days=3), deleter=refuse)
    assert (tmp_path / "out").exists() is False


def test_missing_permission_refuses(source, tmp_path):
    directory, plan = source
    client = FakeClient(blobs={})
    client.handle.perms = {"storage.buckets.get", "storage.objects.delete"}
    with pytest.raises(ValueError, match="^b1: permissions/soft-delete guard refused reviewed deletion$"):
        execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=client, deleter=refuse)
    assert (tmp_path / "out").exists() is False


def test_output_must_be_new_and_work_dir_empty(source, tmp_path):
    directory, plan = source
    out = tmp_path / "used"
    out.mkdir()
    (out / "plan-summary.json").write_text("{}")
    with pytest.raises(ValueError, match="^reviewed execution output directory must be new$"):
        execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=refuse)
    # The source's own dir holds a plan-summary.json: never a run's output.
    with pytest.raises(ValueError, match="^reviewed execution output directory must be new$"):
        execute_manifest(str(directory), str(plan), str(directory), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=refuse)
    work = tmp_path / "busy"
    work.mkdir()
    (work / "x").write_text("x")
    with pytest.raises(ValueError, match="^manifest execution work directory must be empty$"):
        prepare_manifest(str(directory), str(plan), str(work))


def test_source_artifacts_are_pinned_before_any_read(source, tmp_path, monkeypatch):
    directory, plan = source
    pinned = []
    real = sweep_reviewed.pin_path

    def pin(fs, path):
        pinned.append(path)
        return real(fs, path)

    monkeypatch.setattr(sweep_reviewed, "pin_path", pin)
    prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))
    assert pinned == [f"{directory}/plan-summary.json", f"{directory}/manifest/b1.parquet"]


def test_a_bucket_with_nothing_eligible_needs_no_manifest(source, tmp_path):
    directory, plan = source
    summary = summary_of([])
    del summary["buckets"]["b1"]["eligible"]
    (directory / "plan-summary.json").write_text(json.dumps(summary))
    (directory / "manifest/b1.parquet").unlink()
    result = execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=refuse)
    assert result["buckets"] == {}
    assert json.loads((tmp_path / "out/deleted-summary.json").read_text())["buckets"] == {}


def test_a_bucket_cut_takes_only_its_rows(source, tmp_path):
    directory, plan = source
    others = [dict(name="x/o", size_bytes=4, generation=7, dir="x")]
    write_manifest(directory, others, bucket="b2")
    summary = summary_of(ROWS, approved=["gs://b1/a/", "gs://b2/x/"])
    summary["buckets"]["b2"] = {"objects": 1, "dirs": 1, "eligible": eligible(others)}
    (directory / "plan-summary.json").write_text(json.dumps(summary))
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/", "gs://b2/x/"]}))
    calls = []
    out = tmp_path / "out"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, only_buckets=("b2",), client=FakeClient(blobs={}), deleter=acknowledge(calls))
    assert calls == [[("x/o", 7)]]
    assert list(result["buckets"]) == ["b2"]
    assert json.loads((out / "plan-summary.json").read_text())["approved"] == ["gs://b2/x/"]


def test_exact_items_are_their_own_bands(source, tmp_path):
    directory, plan = source
    rows = [dict(name="c/exact.bin", size_bytes=5, generation=7, dir="c"), *ROWS[:2]]
    rewrite(directory, rows, approved_objects=["gs://b1/c/exact.bin"])
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/"], "objects": ["gs://b1/c/exact.bin"]}))
    result = execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge())
    assert result["buckets"]["b1"]["bands"] == {
        "gs://b1/a/": {"bytes": 4, "objects": 2},
        "gs://b1/c/exact.bin": {"bytes": 5, "objects": 1},
    }
    # an exact item vouches for its key only: a sibling in the manifest is refused
    rows = [dict(name="c/exact.bin.bak", size_bytes=5, generation=8, dir="c"), *ROWS[:2]]
    rewrite(directory, rows, approved_objects=["gs://b1/c/exact.bin"])
    with pytest.raises(ValueError, match="^b1: manifest row is outside staged prefixes$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work2"))


def test_undo_restores_the_logged_generations(source, tmp_path):
    directory, plan = source
    out = tmp_path / "out"
    execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge())
    handle = _UndoHandle()
    result = undo_run(str(out), client=_UndoClient(handle), workers=2, now=1_800_000_000)
    assert sorted(handle.calls) == sorted((row["name"], row["generation"], 0) for row in ROWS)
    assert (result["buckets"]["b1"]["decisions"], result["buckets"]["b1"]["restored_bytes"]) == ({"restored": 1005}, 2010)


def test_a_drained_run_resumes_from_the_same_manifest(source, tmp_path):
    directory, plan = source
    stop = threading.Event()

    def stopping(client, bucket, batch):
        stop.set()
        return [(blob, "delete") for blob in batch]

    first = tmp_path / "first"
    result = execute_manifest(str(directory), str(plan), str(first), str(tmp_path / "work1"), for_real=True, client=FakeClient(blobs={}), deleter=stopping, stop=stop)
    assert result["buckets"]["b1"]["interrupted"] == {"roots_skipped": 1, "roots": 2}
    calls = []
    second = tmp_path / "second"
    result = execute_manifest(str(directory), str(plan), str(second), str(tmp_path / "work2"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge(calls), exclude_runs=(str(first),))
    # (the remainder's order is the resume filter's)
    assert [sorted(batch) for batch in calls] == [[(row["name"], row["generation"]) for row in ROWS[1000:]]]
    assert (result["resume_runs"], result["buckets"]["b1"]["decisions"]) == ([str(first)], {"delete": 5})


D1_RUNS = """CREATE TABLE deletion_runs (
  run_id TEXT PRIMARY KEY, plan TEXT, scan TEXT, head INT, exec_head INT, actor TEXT, mode TEXT, started_ts INT, finished_ts INT,
  deleted_bytes INT, deleted_objects INT, skipped_gone INT, skipped_overwritten INT, drift_dirs INT, ledger_drift_dirs INT,
  undo_deadline INT, log_dir TEXT, buckets TEXT, plan_id INT, plan_digest TEXT
)"""
D1_BANDS = """CREATE TABLE deletion_bands (
  run_id TEXT, prefix TEXT, bytes INT, objects INT, gone INT, overwritten INT, drift_new_objects INT, undone_objects INT,
  PRIMARY KEY (run_id, prefix)
)"""
D1_PROGRESS = """CREATE TABLE deletion_progress (
  run_id TEXT, bucket TEXT, ts REAL, deletes INT, bytes INT, gone INT, overwritten INT, failed INT, done INT, bytes_exact INT,
  PRIMARY KEY (run_id, bucket, ts)
)"""


@pytest.fixture
def d1(monkeypatch):
    """gcs's run tables (the `[cloud]` schema is cw's lineage), behind the executor's `_d1_query`."""
    from dt_cloud import index_footer

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    for table in (D1_RUNS, D1_BANDS, D1_PROGRESS):
        db.execute(table)
    lock = threading.Lock()

    def query(sql, *_, **__):
        with lock:
            db.execute(sql)
        return []

    monkeypatch.setattr(index_footer, "_d1_query", query)
    monkeypatch.setattr(index_footer, "_creds", lambda: ("token", "account"))
    return db


def test_manifest_then_execute_closes_the_run_with_its_totals(tmp_path, monkeypatch, d1):
    """The dispatched script's two commands, end to end: `sweep manifest` into the run's `source/`, `execute-manifest` into
    the run, as_of holds honored, and the D1 row `sweepReflect` closes keyed by the run dir."""
    from dt_cloud import cli

    calls = []
    client = FakeClient(blobs={})
    client._credentials = None
    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    monkeypatch.setattr(sweep_reviewed, "XmlDeleter", lambda **_: acknowledge(calls))
    monkeypatch.setattr("google.cloud.storage.Client", lambda: client)
    monkeypatch.setenv("USER", "ann")
    root = _as_of_root(tmp_path, AS_OF_ROWS)
    run = tmp_path / "gcs-sweep-real-20261103-162200z"
    run.mkdir()
    plan = run / "plan.json"
    plan.write_text(json.dumps(AS_OF_PLAN))
    built = CliRunner().invoke(cli.main, ["sweep", "manifest", "-d", NOW, "--plan", str(plan), "-b", B1, "-r", str(root), "-j", "2", "-o", f"{run}/source"])
    assert (built.exit_code, built.exception) == (0, None)
    executed = CliRunner().invoke(cli.main, [
        "sweep", "execute-manifest", "-e", "xml", "-j", "64", "-B", "1", "-c", "guided", "-r", "8000", "-b", B1,
        "-p", str(plan), "-o", str(run), "-w", str(tmp_path / "work"), "--for-real", f"{run}/source",
    ])
    assert (executed.exit_code, executed.exception) == (0, None)
    assert sorted(identity for batch in calls for identity in batch) == [("ckpt/a/kept", 11), ("ckpt/a/sub/kept", 14), ("ckpt/b/new", 41), ("tmp/new", 51)]
    assert client.listed == []

    rows = [dict(row) for row in d1.execute("SELECT * FROM deletion_runs")]
    assert len(rows) == 1
    row = rows[0]
    assert re.fullmatch(rf"{NOW}-p9/\d{{8}}T\d{{6}}Z", row["run_id"])
    assert row["undo_deadline"] == row["finished_ts"] + 7 * 86400
    assert {key: row[key] for key in ("scan", "mode", "actor", "plan_id", "log_dir", "plan", "buckets", "deleted_objects", "deleted_bytes", "skipped_gone", "skipped_overwritten", "drift_dirs", "plan_digest")} == {
        "scan": NOW, "mode": "real", "actor": "ann", "plan_id": 9, "log_dir": str(run), "plan": str(run), "buckets": B1,
        "deleted_objects": 4, "deleted_bytes": 1 + 8 + 16 + 64, "skipped_gone": 0, "skipped_overwritten": 0, "drift_dirs": 0, "plan_digest": None,
    }
    assert [tuple(band) for band in d1.execute("SELECT prefix, bytes, objects, gone, overwritten, drift_new_objects FROM deletion_bands ORDER BY prefix")] == [
        (f"gs://{B1}/ckpt/a/", 9, 2, 0, 0, 0),
        (f"gs://{B1}/ckpt/b/", 16, 1, 0, 0, 0),
        (f"gs://{B1}/tmp/", 64, 1, 0, 0, 0),
    ]
    # What `sweepReflect` runs once Batch says the job finished: the finished row, found by its run dir.
    reflected = d1.execute("UPDATE deletion_runs SET plan_digest = ? WHERE log_dir = ? AND finished_ts IS NOT NULL AND plan_digest IS NULL RETURNING run_id", ("digest", str(run))).fetchall()
    assert [r["run_id"] for r in reflected] == [row["run_id"]]
