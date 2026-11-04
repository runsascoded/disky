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


def test_real_xml_replies_acknowledge_every_generation(source, tmp_path):
    """Measured on GCS: a multi-delete replied `Deleted` (HTTP 200) for the live current generation, one already deleted,
    an overwritten old one, a wrong one on a live key, a key that never existed and a repeat. So `delete` means attempted."""
    directory, plan = source
    rows = ROWS[:6]
    rewrite(directory, rows)
    posted = []

    class Http:
        def post(self, url, data, headers, timeout):
            posted.append(data)
            return xml_reply(*(("Deleted", row["name"], row["generation"], None) for row in rows))

    xml = XmlDeleter(http=Http())
    out = tmp_path / "out"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=lambda client, bucket, batch: xml(client, "b1", batch))
    assert len(posted) == 1
    assert re.findall(r"<Key>([^<]+)</Key><VersionId>(\d+)</VersionId>", posted[0].decode()) == [(row["name"], str(row["generation"])) for row in rows]
    bucket = result["buckets"]["b1"]
    assert {key: bucket[key] for key in ("decisions", "delete_bytes", "bands", "failed_dirs")} == {
        "decisions": {"delete": 6},
        "delete_bytes": 12,
        "bands": {"gs://b1/a/": {"bytes": 12, "objects": 6}},
        "failed_dirs": [],
    }
    assert _decisions(out, "deleted") == [(row["name"], "delete", row["generation"]) for row in rows]


def test_a_coded_per_object_error_is_still_attributed(source, tmp_path):
    """Not what GCS was measured to send: S3-style codes the parser keeps so a reply that does carry one isn't called deleted."""
    directory, plan = source
    rows = ROWS[:3]
    rewrite(directory, rows)

    class Http:
        def post(self, url, data, headers, timeout):
            return xml_reply(
                ("Deleted", rows[0]["name"], 1, None),
                ("Error", rows[1]["name"], 2, "NoSuchVersion"),
                ("Error", rows[2]["name"], 3, "PreconditionFailed"),
            )

    xml = XmlDeleter(http=Http())
    result = execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=lambda client, bucket, batch: xml(client, "b1", batch))
    assert result["buckets"]["b1"]["decisions"] == {"delete": 1, "skipped_gone": 1, "skipped_overwritten": 1}


def test_validates_only_without_for_real(source, tmp_path):
    directory, plan = source
    out = tmp_path / "dry"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), client=FakeClient(blobs={}), deleter=refuse)
    assert (result["for_real"], result["diagnostic"], result["buckets"]["b1"]["decisions"]) == (False, True, {"delete": 1005})
    assert json.loads((out / "plan-summary.json").read_text())["diagnostic"] is True
    assert _decisions(out, "would-delete") == sorted((row["name"], "delete", row["generation"]) for row in ROWS)


@pytest.mark.parametrize("mutation,message", [
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


NULLS = [dict(name=f"a/null/o{i}", size_bytes=7, generation=None, dir="a/null") for i in range(3)]
LIVE = ROWS[:4]


def log_rows(out: Path, bucket: str = "b1") -> list[tuple]:
    """The decision log, exact: pyarrow, so a null generation stays `None`."""
    table = pq.read_table(out / "deleted" / bucket)
    return sorted(zip(*(table[column].to_pylist() for column in ("name", "size_bytes", "generation", "decision", "dir"))), key=lambda row: row[0])


def bucket_of(result: dict, bucket: str = "b1") -> dict:
    return {key: value for key, value in result["buckets"][bucket].items() if key != "performance"}


def test_a_null_generation_is_skipped_not_deleted(source, tmp_path):
    directory, plan = source
    rewrite(directory, [*LIVE, *NULLS])
    calls = []
    out = tmp_path / "out"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge(calls))
    assert calls == [[(row["name"], row["generation"]) for row in LIVE]]
    assert bucket_of(result) == {
        "missing_perms": [], "soft_delete_days": 7,
        "decisions": {"skipped_no_generation": 3, "delete": 4},
        "delete_bytes": 8,
        "drift_dirs": [], "failed_dirs": [],
        "bands": {"gs://b1/a/": {"bytes": 8, "objects": 4, "no_generation": 3, "no_generation_bytes": 21}},
        "skipped_no_generation": {"objects": 3, "bytes": 21, "whole_bucket": False},
    }
    assert result["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_buckets": []}
    assert log_rows(out) == sorted([
        *((row["name"], 2, row["generation"], "delete", row["dir"]) for row in LIVE),
        *((row["name"], 7, None, "skipped_no_generation", "a/null") for row in NULLS),
    ], key=lambda row: row[0])
    final = json.loads((out / "deleted-summary.json").read_text())
    assert {key: value for key, value in final["buckets"]["b1"].items() if key != "performance"} == bucket_of(result)
    assert final["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_buckets": []}
    written = json.loads((out / "plan-summary.json").read_text())
    # What the run planned stays the summary's `eligible`; the skips are named beside it, before any delete lands.
    assert written["total"] == {"eligible": eligible([*LIVE, *NULLS])}
    assert written["buckets"]["b1"]["eligible"] == eligible([*LIVE, *NULLS])
    assert written["buckets"]["b1"]["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_bucket": False}
    assert written["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_buckets": []}
    progress = json.loads((out / "progress/b1.json").read_text())
    assert {key: progress[key] for key in ("roots", "roots_done", "decisions", "done")} == {"roots": 1, "roots_done": 1, "decisions": {"skipped_no_generation": 3, "delete": 4}, "done": True}


def test_totals_count_deletes_and_skips_exactly(source, tmp_path):
    directory, plan = source
    # A summary describing only the rows with generations: not this manifest.
    write_manifest(directory, [*LIVE, *NULLS])
    (directory / "plan-summary.json").write_text(json.dumps(summary_of(LIVE)))
    with pytest.raises(ValueError, match="^b1: manifest totals do not match its plan summary$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))
    # Objects right, bytes wrong.
    summary = summary_of([*LIVE, *NULLS])
    summary["buckets"]["b1"]["eligible"]["bytes"] += 1
    (directory / "plan-summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="^b1: manifest totals do not match its plan summary$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work2"))
    (directory / "plan-summary.json").write_text(json.dumps(summary_of([*LIVE, *NULLS])))
    assert prepare_manifest(str(directory), str(plan), str(tmp_path / "work3"))[1] == {"buckets": {"b1": {
        "decisions": {"delete": 4}, "delete_bytes": 8, "skipped_no_generation": {"objects": 3, "bytes": 21, "whole_bucket": False},
    }}}


def test_a_manifest_with_no_generation_at_all_refuses_before_any_delete(source, tmp_path):
    directory, plan = source
    rewrite(directory, NULLS)
    with pytest.raises(ValueError) as error:
        execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=refuse)
    assert str(error.value) == "manifest has no generations: none of its 3 rows can be deleted; its scan's listing lacks them"
    assert (tmp_path / "out").exists() is False


def two_buckets(directory: Path, plan: Path, nulls: list[dict] = NULLS) -> None:
    """b1 with generations, b2 with none."""
    others = [dict(row, name=row["name"].replace("a/", "x/", 1), dir=row["dir"].replace("a/", "x/", 1)) for row in nulls]
    write_manifest(directory, LIVE)
    write_manifest(directory, others, bucket="b2")
    summary = summary_of(LIVE, approved=["gs://b1/a/", "gs://b2/x/"])
    summary["buckets"]["b2"] = {"objects": len(others), "dirs": 1, "eligible": eligible(others)}
    (directory / "plan-summary.json").write_text(json.dumps(summary))
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/", "gs://b2/x/"]}))


def test_a_whole_bucket_of_nulls_is_flagged_and_the_others_proceed(source, tmp_path):
    directory, plan = source
    two_buckets(directory, plan)
    calls = []
    out = tmp_path / "out"
    result = execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge(calls))
    assert calls == [[(row["name"], row["generation"]) for row in LIVE]]
    assert bucket_of(result, "b2") == {
        "missing_perms": [], "soft_delete_days": 7,
        "decisions": {"skipped_no_generation": 3},
        "delete_bytes": 0,
        "drift_dirs": [], "failed_dirs": [],
        "bands": {"gs://b2/x/": {"no_generation": 3, "no_generation_bytes": 21}},
        "skipped_no_generation": {"objects": 3, "bytes": 21, "whole_bucket": True},
    }
    assert (bucket_of(result, "b1")["decisions"], bucket_of(result, "b1")["delete_bytes"]) == ({"delete": 4}, 8)
    assert result["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_buckets": ["b2"]}
    assert json.loads((out / "deleted-summary.json").read_text())["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_buckets": ["b2"]}
    written = json.loads((out / "plan-summary.json").read_text())
    assert written["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_buckets": ["b2"]}
    assert written["buckets"]["b2"]["eligible"] == {"objects": 3, "bytes": 21}
    assert log_rows(out, "b2") == [(f"x/null/o{i}", 7, None, "skipped_no_generation", "x/null") for i in range(3)]


def test_a_bucket_of_nulls_is_not_held_to_the_delete_guards(source, tmp_path):
    # Nothing is deleted there, so a short retention on it doesn't stop the buckets that do have generations.
    directory, plan = source
    two_buckets(directory, plan)
    client = FakeClient(blobs={})
    real = client.get_bucket

    def get_bucket(name):
        handle = real(name)
        if name == "b2":
            class Short:
                soft_delete_policy = None
            return Short()
        return handle

    client.get_bucket = get_bucket
    result = execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=client, deleter=acknowledge())
    assert (result["buckets"]["b1"]["decisions"], result["buckets"]["b2"]["decisions"]) == ({"delete": 4}, {"skipped_no_generation": 3})


def test_a_cut_to_only_a_bucket_of_nulls_refuses(source, tmp_path):
    directory, plan = source
    two_buckets(directory, plan)
    with pytest.raises(ValueError) as error:
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"), ("b2",))
    assert str(error.value) == "manifest has no generations: none of its 3 rows can be deleted; its scan's listing lacks them"


def test_a_skipped_row_is_still_checked(source, tmp_path):
    directory, plan = source
    rows = [*LIVE, dict(name="other/x", size_bytes=1, generation=None, dir="other")]
    rewrite(directory, rows)
    with pytest.raises(ValueError, match="^b1: manifest row is outside staged prefixes$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))
    rewrite(directory, [*LIVE, dict(name="a/x", size_bytes=1, generation=None, dir=None)])
    with pytest.raises(ValueError, match="^b1: manifest row lacks directory metadata$"):
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work2"))


def test_a_repeated_name_with_no_generation_refuses(source, tmp_path):
    directory, plan = source
    rewrite(directory, [*LIVE, NULLS[0], NULLS[0]])
    with pytest.raises(ValueError) as error:
        prepare_manifest(str(directory), str(plan), str(tmp_path / "work"))
    assert str(error.value) == f"b1: manifest repeats {NULLS[0]['name']}@None"


def test_undo_ignores_skipped_rows_and_keeps_exact_generations(source, tmp_path):
    # A null in the log must not turn the column into floats on the way to pandas.
    directory, plan = source
    big = 2 ** 53 + 1
    rows = [dict(LIVE[0], generation=big), *NULLS]
    rewrite(directory, rows)
    out = tmp_path / "out"
    execute_manifest(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge())
    handle = _UndoHandle()
    result = undo_run(str(out), client=_UndoClient(handle), workers=1, now=1_800_000_000)
    assert handle.calls == [(LIVE[0]["name"], big, 0)]
    assert result["buckets"]["b1"]["decisions"] == {"restored": 1}


def test_a_resume_repeats_the_skips_and_deletes_the_remainder(source, tmp_path):
    directory, plan = source
    rewrite(directory, [*ROWS, *NULLS])
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
    assert [sorted(batch) for batch in calls] == [[(row["name"], row["generation"]) for row in ROWS[1000:]]]
    assert bucket_of(result)["decisions"] == {"skipped_no_generation": 3, "delete": 5}
    assert bucket_of(result)["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_bucket": False}
    assert bucket_of(result).get("interrupted") is None


def test_a_resume_keeps_a_whole_bucket_of_nulls_flagged(source, tmp_path):
    directory, plan = source
    two_buckets(directory, plan)
    first = tmp_path / "first"
    execute_manifest(str(directory), str(plan), str(first), str(tmp_path / "work1"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge())
    result = execute_manifest(str(directory), str(plan), str(tmp_path / "second"), str(tmp_path / "work2"), for_real=True, client=FakeClient(blobs={}), deleter=refuse, exclude_runs=(str(first),))
    # (b1 had nothing left, so it leaves the resume; b2 has nothing it could ever delete, and stays flagged)
    assert sorted(result["buckets"]) == ["b2"]
    assert result["skipped_no_generation"] == {"objects": 3, "bytes": 21, "whole_buckets": ["b2"]}


def test_a_refused_run_leaves_its_reason_in_the_run_dir(source, tmp_path, monkeypatch):
    """No `deletion_runs` row yet: `/api/sweep/jobs` reads this for the row and the Slack thread."""
    from dt_cloud import cli

    directory, plan = source
    rewrite(directory, NULLS)
    client = FakeClient(blobs={})
    monkeypatch.setattr("google.cloud.storage.Client", lambda: client)
    run = tmp_path / "run"
    executed = CliRunner().invoke(cli.main, [
        "sweep", "execute-manifest", "-e", "xml", "-p", str(plan), "-o", str(run), "-w", str(tmp_path / "work"), "--for-real", str(directory),
    ])
    assert executed.exit_code == 1
    failure = json.loads((run / "failure.json").read_text())
    assert {key: failure[key] for key in ("step", "reason")} == {
        "step": "execute-manifest", "reason": "manifest has no generations: none of its 3 rows can be deleted; its scan's listing lacks them",
    }
    assert sorted(failure) == ["reason", "step", "ts"]


def test_a_refused_manifest_step_leaves_its_reason_in_its_output_dir(tmp_path):
    from dt_cloud import cli

    out = tmp_path / "run" / "source"
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/"]}))
    built = CliRunner().invoke(cli.main, ["sweep", "manifest", "-d", DATE, "--plan", str(plan), "-b", "elsewhere", "-r", str(tmp_path), "-o", str(out)])
    assert built.exit_code == 1
    failure = json.loads((out / "failure.json").read_text())
    assert {key: failure[key] for key in ("step", "reason")} == {"step": "manifest", "reason": "no plan bucket among -b elsewhere (plan 1 names b1)"}


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


def test_a_prefix_with_only_nulls_records_no_band_so_it_stays_staged(source, tmp_path, d1):
    # `unstageDeleted` unstages any item with a band on a succeeded run: a band here would read as swept.
    from dt_cloud.sweep_exec import record_bands

    directory, plan = source
    two_buckets(directory, plan)
    result = execute_manifest(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=acknowledge())
    assert record_bands("run", result) == 1
    assert [tuple(band) for band in d1.execute("SELECT prefix, bytes, objects FROM deletion_bands")] == [("gs://b1/a/", 8, 4)]


def test_a_partly_null_prefix_records_no_band_beside_a_clean_one(monkeypatch):
    # With no band it stays staged; the run's totals and `deleted/` log still hold its deletes.
    from dt_cloud import index_footer, sweep_exec

    sent: list[str] = []
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(index_footer, "_d1_query", lambda sql, acct, tok: sent.append(sql))
    partly = {"bytes": 8, "objects": 4, "no_generation": 2, "no_generation_bytes": 3}
    only = {"bytes": 0, "objects": 0, "no_generation": 5, "no_generation_bytes": 9}
    clean = {"bytes": 1, "objects": 1}
    assert sweep_exec.record_bands("r", {"buckets": {"b": {"bands": {"gs://b/p/": partly, "gs://b/q/": only, "gs://b/c/": clean}}}}) == 1
    assert sent == [
        "INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects) VALUES "
        "('r', 'gs://b/c/', 1, 1, 0, 0, 0, 0) ON CONFLICT (run_id, prefix) DO NOTHING",
    ]


def test_a_run_with_no_cut_records_every_bucket_undo_must_read(tmp_path, d1):
    # `runBuckets` falls back to the bands' buckets only for a NULL `buckets`, and a bucket can have no band.
    from dt_cloud.sweep_exec import record_run_start

    plan = {"date": "2026-01-01", "plan_id": 1, "head": 0, "buckets": {"b2": {}, "b1": {}}}
    record_run_start(plan, "gs://x/nocut", "ann", 1000, True)
    record_run_start(plan, "gs://x/cut", "ann", 2000, True, ("b2",))
    assert [tuple(row) for row in d1.execute("SELECT log_dir, buckets FROM deletion_runs ORDER BY started_ts")] == [
        ("gs://x/nocut", "b1,b2"),
        ("gs://x/cut", "b2"),
    ]


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


def fail_to_record(*args, **kwargs):
    raise RuntimeError("D1 query failed (503): overloaded")


REFUSED = "real run refused before any delete: couldn't record its start in D1: D1 query failed (503): overloaded"


def failure_of(run: Path) -> dict:
    failure = json.loads((run / "failure.json").read_text())
    return {key: failure[key] for key in ("step", "reason")}


def generation_run(command: str, directory: Path, plan: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list, Path, list[str]]:
    """The dispatched real run's command line, its real delete seam counted (no `deleter=`: this is what the CLI wires)."""
    from dt_cloud import cli

    calls = []
    client = FakeClient(blobs={})
    client._credentials = None
    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    monkeypatch.setattr(sweep_reviewed, "XmlDeleter", lambda **_: acknowledge(calls))
    monkeypatch.setattr("google.cloud.storage.Client", lambda: client)
    run = tmp_path / "gcs-sweep-real-20261104-120000z"
    return calls, run, ["sweep", command, "-e", "xml", "-p", str(plan), "-o", str(run), "-w", str(tmp_path / "work"), "--for-real", str(directory)]


@pytest.fixture
def reviewed_source(tmp_path):
    """A dry run's dir, as `execute-reviewed` reads it."""
    directory = tmp_path / "dry"
    (directory / "would-delete").mkdir(parents=True)
    rows = [dict(name=f"a/o{i}", size_bytes=1, generation=i + 1, decision="delete", dir="a") for i in range(3)]
    pq.write_table(pa.Table.from_pylist(rows), directory / "would-delete/b1.parquet")
    (directory / "plan-summary.json").write_text(json.dumps({"plan_id": 1, "date": DATE, "approved": ["gs://b1/a/"], "buckets": {"b1": {"eligible": {"objects": 3, "bytes": 3}}}, "total": {"eligible": {"objects": 3, "bytes": 3}}}))
    (directory / "would-delete-summary.json").write_text(json.dumps({"for_real": False, "buckets": {"b1": {"decisions": {"delete": 3}, "delete_bytes": 3}}}))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/"]}))
    return directory, plan


@pytest.fixture(params=["execute-manifest", "execute-reviewed"])
def generation_source(request, source, reviewed_source):
    directory, plan = source if request.param == "execute-manifest" else reviewed_source
    return request.param, directory, plan


def test_a_real_run_with_no_start_record_refuses_before_any_delete(generation_source, tmp_path, monkeypatch):
    """With no row, a job that then failed would be closed by `sweepReflect` as a real run that deleted nothing."""
    from dt_cloud import cli

    command, directory, plan = generation_source
    calls, run, args = generation_run(command, directory, plan, tmp_path, monkeypatch)
    monkeypatch.setattr("dt_cloud.sweep_exec.record_run_start", fail_to_record)
    monkeypatch.setattr("dt_cloud.sweep_exec.record_run", fail_to_record)
    executed = CliRunner().invoke(cli.main, args)
    assert executed.exit_code == 1
    assert calls == []
    assert failure_of(run) == {"step": command, "reason": REFUSED}
    assert sorted(path.name for path in run.glob("deleted/**/*")) == []


def test_a_real_run_with_its_start_record_goes_on_to_delete(generation_source, tmp_path, monkeypatch):
    """The other side of the refusal: the same command line, the same seam, recorded first and then deleting."""
    from dt_cloud import cli

    command, directory, plan = generation_source
    calls, run, args = generation_run(command, directory, plan, tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr("dt_cloud.sweep_exec.record_run_start", lambda *_: order.append(len(calls)) or "run")
    monkeypatch.setattr("dt_cloud.sweep_exec.record_run", lambda *_: "run")
    monkeypatch.setattr("dt_cloud.sweep_progress.write_samples", lambda *_: None)
    executed = CliRunner().invoke(cli.main, args)
    assert (executed.exit_code, executed.exception) == (0, None)
    assert order == [0]
    assert len(calls) > 0
    assert not (run / "failure.json").exists()


def sweep_plan_dir(tmp_path: Path) -> Path:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "plan-summary.json").write_text(json.dumps({"plan_id": 1, "plan_name": "p", "date": DATE}))
    return plan_dir


def test_the_older_execute_refuses_a_real_run_with_no_start_record(tmp_path, monkeypatch):
    from dt_cloud import cli

    executed_plans = []
    monkeypatch.setattr("dt_cloud.sweep_exec.record_run_start", fail_to_record)
    monkeypatch.setattr("dt_cloud.sweep_exec.execute_plan", lambda *args, **kwargs: executed_plans.append(args))
    plan_dir = sweep_plan_dir(tmp_path)
    executed = CliRunner().invoke(cli.main, ["sweep", "execute", "--for-real", str(plan_dir)])
    assert executed.exit_code == 1
    assert executed_plans == []
    assert failure_of(plan_dir) == {"step": "execute", "reason": REFUSED}


def test_a_dry_run_goes_on_past_a_failed_start_record(tmp_path, monkeypatch):
    from dt_cloud import cli

    executed_plans = []

    def execute_plan(*args, **kwargs):
        executed_plans.append(kwargs["for_real"])
        return {"buckets": {}, "_plan": {}}

    logged = []
    monkeypatch.setattr(cli, "err", logged.append)
    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    monkeypatch.setattr("dt_cloud.sweep_exec.record_run_start", fail_to_record)
    monkeypatch.setattr("dt_cloud.sweep_exec.record_run", lambda *_, **__: "run")
    monkeypatch.setattr("dt_cloud.sweep_exec.execute_plan", execute_plan)
    plan_dir = sweep_plan_dir(tmp_path)
    executed = CliRunner().invoke(cli.main, ["sweep", "execute", str(plan_dir)])
    assert (executed.exit_code, executed.exception) == (0, None)
    assert executed_plans == [False]
    assert not (plan_dir / "failure.json").exists()
    assert logged[:2] == ["execute (dry-run) plan 1 ('p')", "WARN: deletion-run start record failed: D1 query failed (503): overloaded"]
