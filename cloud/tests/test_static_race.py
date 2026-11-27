"""`static_merge race`: a race's Batch spec cloned from the running compaction job's, and `race-adopt`'s choice."""
from __future__ import annotations

import json

import pytest
from click import UsageError

from dt_cloud import static_merge as M

OLD = "us-central1-docker.pkg.dev/p/r/scan@sha256:" + "a" * 64
NEW = "us-central1-docker.pkg.dev/p/r/scan@sha256:" + "b" * 64
COMPACT = ("python3 -u -m dt_cloud.static_merge compact -g 2026-10-15cw.c003 -S scratch-b -C level:5 -r -j 8 -D 8 -A 8 -L 8 "
           "-M 270GB -p 48 -m /gcs/data-b")
PROFILE = {"name": "cw", "bucket": "data-b", "scratch": "scratch-b", "gen": "2026-10-15cw.c003", "image": OLD, "measure_tasks": 8}
LABELS = {"app": "disky", "deployment": "cw", "component": "static-names", "stage": "compact", "upkeep": "fold", "events": "alert",
          "batch-job-id": "sn-compact-x", "scan_id": "2026-11-26t1802"}


def job() -> dict:
    return {
        "name": "projects/p/locations/us-east1/jobs/sn-compact-x",
        "uid": "u",
        "status": {"state": "RUNNING"},
        "labels": dict(LABELS),
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
        "allocationPolicy": {"labels": dict(LABELS), "instances": [{"policy": {"machineType": "n2-highmem-48"}}]},
        "taskGroups": [{
            "name": "projects/p/locations/us-east1/jobs/sn-compact-x/taskGroups/group0",
            "taskCount": "1",
            "taskSpec": {
                "runnables": [{"container": {"imageUri": OLD, "entrypoint": "bash", "commands": [
                    "-c", f"set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && {COMPACT}"]}}],
                "environment": {"variables": {"STATIC_NAMES_PROFILE": json.dumps(PROFILE), "DT_JOB_IMAGE": OLD.rsplit("/", 1)[1],
                                              "DT_JOB_NAME": "sn-compact-x", "SCAN_ID": "2026-11-26T1802"}},
                "maxRunDuration": "57600s",
            },
        }],
    }


ROOT = M.race_root("scratch-b", "2026-10-15cw.c004", "r1717")
PROBE = ("python3 -u -m dt_cloud.static_merge compact-probe -g 2026-10-15cw.c003 -j 8 -F 8 -A 8 -M 270GB -p 48 -m /gcs/data-b -b data-b "
         "-o /stage/out/gen -T /stage/tmp/compact-probe -K gs://scratch-b/races/2026-10-15cw.c004-r1717/ck -R gs://scratch-b -t fold/spilled "
         "-U gs://scratch-b/races/2026-10-15cw.c004-r1717/upload -u gs://scratch-b/races/2026-10-15cw.c004-r1717/report.json")
RACE_LABELS = {"app": "disky", "deployment": "cw", "component": "static-names", "stage": "race", "upkeep": "race", "scan_id": "2026-11-26t1802"}


@pytest.mark.parametrize("adopt", [False, True])
def test_race_spec_clones_the_compaction_onto_the_new_image(adopt: bool):
    spec = M.race_spec(job(), NEW, name="sn-race-2026-10-15cw-c004-r1717", root=ROOT, through="fold/spilled", adopt=adopt,
                       scratch="scratch-b", bucket="data-b")
    tail = (" && rm -rf /stage/tmp/compact-probe /stage/out/gen && python3 -u -m dt_cloud.static_merge race-adopt "
            "gs://scratch-b/races/2026-10-15cw.c004-r1717/upload") if adopt else ""
    want = job()
    tg = want["taskGroups"][0]
    del tg["name"], tg["taskCount"]
    ts = tg["taskSpec"]
    ts["runnables"][0]["container"].update(imageUri=NEW, commands=["-c", f"set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && {PROBE}{tail}"])
    ts["environment"]["variables"].update(STATIC_NAMES_PROFILE=json.dumps({**PROFILE, "image": NEW}), DT_JOB_IMAGE=NEW.rsplit("/", 1)[1],
                                          DT_JOB_NAME="sn-race-2026-10-15cw-c004-r1717", DT_RACE_COMPACTION="sn-compact-x", DT_RACE_COMPACT_CMD=COMPACT,
                                          DT_RACE_GEN_URL="gs://data-b/static-names/2026-10-15cw.c004")
    want["labels"] = dict(RACE_LABELS)
    want["allocationPolicy"]["labels"] = dict(RACE_LABELS)
    for k in ("name", "uid", "status"):
        del want[k]
    assert spec == want


def test_race_spec_refuses_a_compact_option_it_cannot_carry():
    j = job()
    c = j["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]
    c["commands"][-1] = c["commands"][-1].replace(" -r ", " -r -P gs://x ")
    with pytest.raises(UsageError) as e:
        M.race_spec(j, NEW, name="n", root=ROOT, through="fold/spilled", adopt=False, scratch="scratch-b", bucket="data-b")
    assert e.value.message == f"race: `compact -P` (in {COMPACT.replace(' -r ', ' -r -P gs://x ')!r}): not an option a race knows how to carry"


def test_race_gen_is_the_compactions_g():
    assert M.race_gen(M.race_compact_cmd(job())[1]) == "2026-10-15cw.c003"


GEN_URL = "gs://data-b/static-names/2026-10-15cw.c004"
UPLOAD = "gs://scratch-b/races/u/upload"
CANCEL_AND_PUBLISH = [("cancel", "sn-compact-x"), ("call", [*COMPACT.split(), "--prebuilt", UPLOAD])]


@pytest.mark.parametrize(("state", "before", "after", "code", "calls"), [
    # live and still folding: cancelled, then the race's fold published in its place
    ("RUNNING", 0, 0, 0, CANCEL_AND_PUBLISH),
    # ended: it published first (or failed, leaving the next compaction to fold): nothing adopted
    ("SUCCEEDED", 0, 0, 0, []),
    ("FAILED", 0, 0, 0, []),
    # already uploading the new generation: about to win, left to finish
    ("RUNNING", 3, 3, 0, []),
    # began uploading between the check and its end: cancelled, but nothing published over the partial generation
    ("RUNNING", 0, 2, 3, [("cancel", "sn-compact-x")]),
])
def test_race_adopt_takes_over_only_a_rival_still_folding(monkeypatch, state: str, before: int, after: int, code: int, calls: list):
    from click.testing import CliRunner

    import subprocess

    from dt_cloud import batch, blobstore, gcp

    states = iter([state, "DELETION_IN_PROGRESS", "DELETED"])
    seen: list = []
    listings = iter([before, after])
    monkeypatch.setattr(gcp, "batch_job", lambda name: {"status": {"state": next(states)}})
    monkeypatch.setattr(batch, "cancel_job", lambda name: seen.append(("cancel", name)))
    monkeypatch.setattr(M, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "call", lambda argv: seen.append(("call", argv)) or 0)

    class FakeStore:
        def __init__(self, url: str):
            assert url == "gs://data-b"

        def listing(self, prefix: str) -> dict[str, int]:
            assert prefix == "static-names/2026-10-15cw.c004"
            return {f"{prefix}/f{i}": 1 for i in range(next(listings))}

    monkeypatch.setattr(blobstore, "Store", FakeStore)
    monkeypatch.setenv(M.RACE_JOB_ENV, "sn-compact-x")
    monkeypatch.setenv(M.RACE_COMPACT_ENV, COMPACT)
    monkeypatch.setenv(M.RACE_GEN_ENV, GEN_URL)
    r = CliRunner().invoke(M.cli, ["race-adopt", UPLOAD])
    assert (r.exit_code, seen) == (code, calls), r.output
