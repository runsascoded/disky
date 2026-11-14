"""`dt-cloud vm-pack` (specs/vm-mode.md §6): the mirror, the pack, the pointer, idempotence, pruning; and the
`vm-pack` scan step's command."""
import json
import os
import stat
from pathlib import Path

import pytest

from dt_cloud import vm_pack as V

GEN = "2026-10-09b.g3.c001"


def store(root: Path, manifests: dict[str, list[str]]) -> Path:
    """An interval store with `CURRENT.json` → GEN, its base sort, and `manifests` (`{name: [run keys]}`)."""
    g = root / "interval-store" / GEN
    (g / "served").mkdir(parents=True)
    (root / "interval-store/CURRENT.json").write_text(json.dumps({"gen": GEN}))
    (g / "scans.json").write_text("{}")
    (g / "served/path.parquet").write_text("base")
    (g / "manifests").mkdir()
    for name, runs in manifests.items():
        (g / "manifests" / name).write_text(json.dumps({"runs": [{"key": k} for k in runs]}))
        for k in runs:
            (g / k / "served").mkdir(parents=True, exist_ok=True)
            (g / k / "served/path.parquet").write_text(k)
    return root


@pytest.fixture
def fsb(tmp_path) -> str:
    """A stand-in `fsb pack-slim GEN -o FILE`: records the mirrored files, writes a pack naming them, prints its JSON."""
    p = tmp_path / "fsb"
    p.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
gen, out = Path(sys.argv[2]), sys.argv[4]
files = sorted(str(f.relative_to(gen)) for f in gen.rglob("*") if f.is_file())
Path(out).write_text(json.dumps(files))
print(json.dumps({"bytes": 1, "scans": 3, "load": {"peak_rss_gb": 1.5}}))
""")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_build_mirrors_the_newest_manifest_and_publishes(tmp_path, fsb):
    root = store(tmp_path / "s", {"2026-11-12T0702.json": ["deltas/a"], "2026-11-13T1734.json": ["deltas/a"],
                                  "2026-11-13T1734.m001.json": ["deltas/a_b", "deltas/c"]})
    doc = V.build(str(root), tmp_path / "w", fsb)
    key = f"vm-pack/{GEN}/2026-11-13T1734.m001.fsb"
    assert {k: doc[k] for k in ("gen", "manifest", "key", "scans", "peak_rss_gb")} == {
        "gen": GEN, "manifest": "2026-11-13T1734.m001.json", "key": key, "scans": 3, "peak_rss_gb": 1.5}
    # Exactly what fsb reads: the newest manifest's runs, not the older manifests'.
    assert json.loads((root / key).read_text()) == [
        "deltas/a_b/served/path.parquet", "deltas/c/served/path.parquet", "manifests/2026-11-13T1734.m001.json", "scans.json", "served/path.parquet"]
    assert json.loads((root / V.POINTER).read_text()) == doc
    assert sorted(p.name for p in (tmp_path / "w").iterdir()) == ["gen"]
    # Again: the pointer already names this generation and manifest.
    assert V.build(str(root), tmp_path / "w", fsb) == {**doc, "skipped": True}


def test_build_prunes_beyond_keep(tmp_path, fsb):
    root = store(tmp_path / "s", {"2026-11-12T0702.json": []})
    m = root / "interval-store" / GEN / "manifests"
    keys = []
    for i, name in enumerate(["2026-11-13T0702.json", "2026-11-14T0702.json", "2026-11-15T0702.json"]):
        (m / name).write_text(json.dumps({"runs": []}))
        keys.append(V.build(str(root), tmp_path / "w", fsb, keep=2)["key"])
        os.utime(root / keys[-1], (1000 + i, 1000 + i))
    assert sorted(str(p.relative_to(root)) for p in (root / "vm-pack").rglob("*.fsb")) == keys[1:]


def test_build_superseded_leaves_the_pointer(tmp_path, fsb, monkeypatch):
    """A manifest newer than the one packed lands while it packs (an overlapping job): the pack is written, but the
    pointer stays as it was and nothing is pruned (the newer job publishes)."""
    root = store(tmp_path / "s", {"2026-11-13T0702.json": []})
    first = V.build(str(root), tmp_path / "w", fsb, keep=1)
    (root / "interval-store" / GEN / "manifests/2026-11-14T0702.json").write_text(json.dumps({"runs": []}))
    seen = iter([(GEN, "2026-11-14T0702.json"), (GEN, "2026-11-15T0702.json")])
    monkeypatch.setattr(V, "newest", lambda s: next(seen))
    doc = V.build(str(root), tmp_path / "w", fsb, keep=1)
    assert (doc["key"], doc["superseded"]) == (f"vm-pack/{GEN}/2026-11-14T0702.fsb", True)
    assert json.loads((root / V.POINTER).read_text()) == first
    assert sorted(str(p.relative_to(root)) for p in (root / "vm-pack").rglob("*.fsb")) == [first["key"], doc["key"]]


def test_build_without_a_generation(tmp_path, fsb):
    (tmp_path / "s").mkdir()
    with pytest.raises(FileNotFoundError) as e:
        V.build(str(tmp_path / "s"), tmp_path / "w", fsb)
    assert str(e.value) == f"{tmp_path}/s/interval-store: no CURRENT.json"


LABELS = {"DISKY_LABELS": "app=disky,deployment=d1", "SCAN_ID": "2026-11-13T1734"}
COST = {"app": "disky", "deployment": "d1", "component": "vm-pack", "scan_id": "2026-11-13t1734"}
JOB = dict(image="r/img@sha256:ab", service_account="sa@p.iam", region="us-east1", project="p")


def test_job_id_names_the_pack():
    assert [V.job_id("gcs", GEN, "2026-11-13T1734.m001.json"), V.job_id("r2-demo", "g1", None),
            V.job_id("deployment-with-a-long-name", GEN, "2026-11-13T1734.m001.json")] == [
        "vm-pack-gcs-2026-10-09b-g3-c001-2026-11-13t1734-m001",
        "vm-pack-r2-demo-g1-base",
        "vm-pack-deployment-with-a-long-name-2026-10-09b-g3-c00-b627e80d",
    ]


def test_job_spec_defaults_spot_highmem_on_local_ssd():
    assert V.job_spec("gs://packs/x", gen=GEN, environ=LABELS, **JOB) == {
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {
                "imageUri": "r/img@sha256:ab", "entrypoint": "bash",
                "commands": ["-c", "dt-cloud image-sha >&2; exec dt-cloud vm-pack build -o gs://packs/x -w /stage/vm-pack -k 2"],
                "volumes": ["/mnt/disks/stage:/stage:rw"]}}],
            "environment": {"variables": LABELS},
            "computeResource": {"cpuMilli": 16000, "memoryMib": 123200},
            "maxRetryCount": 2,
            "maxRunDuration": "14400s",
            "volumes": [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
        }}],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": "n2-highmem-16", "provisioningModel": "SPOT",
                                      "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
                                      "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": "375"}, "deviceName": "stage"}]}}],
            "serviceAccount": {"email": "sa@p.iam"},
            "location": {"allowedLocations": ["regions/us-east1"]},
            "labels": COST,
        },
        "labels": {"purpose": "vm-pack", "gen": "2026-10-09b-g3-c001", **COST},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def test_job_spec_standard_boot_disk_env_and_secrets():
    s = V.job_spec("s3://cw-store", gen=GEN, machine="e2-standard-8", spot=False, ssd_gb=0, keep=3,
                   env={"AWS_ENDPOINT_URL": "https://e.example"},
                   secrets={"AWS_SECRET_ACCESS_KEY": "cw-key", "X": "projects/q/secrets/x/versions/2"}, environ=LABELS, **JOB)
    task, policy = s["taskGroups"][0]["taskSpec"], s["allocationPolicy"]["instances"][0]["policy"]
    assert (task["runnables"], task["environment"], task["computeResource"], task["maxRetryCount"], sorted(task), policy) == (
        [{"container": {"imageUri": "r/img@sha256:ab", "entrypoint": "bash",
                        "commands": ["-c", "dt-cloud image-sha >&2; exec dt-cloud vm-pack build -o s3://cw-store -w /tmp/vm-pack -k 3"]}}],
        {"variables": {"AWS_ENDPOINT_URL": "https://e.example", **LABELS},
         "secretVariables": {"AWS_SECRET_ACCESS_KEY": "projects/p/secrets/cw-key/versions/latest", "X": "projects/q/secrets/x/versions/2"}},
        {"cpuMilli": 8000, "memoryMib": 29600},
        0,
        ["computeResource", "environment", "maxRetryCount", "maxRunDuration", "runnables"],
        {"machineType": "e2-standard-8", "provisioningModel": "STANDARD", "bootDisk": {"type": "pd-balanced", "sizeGb": "100"}},
    )
    with pytest.raises(ValueError) as e:
        V.job_spec("gs://x", gen=GEN, machine="n2-custom-8-65536", environ=LABELS, **JOB)
    assert str(e.value) == "machine n2-custom-8-65536: name its memory (memory_mib); known families: highmem, standard, highcpu"
    assert V.job_spec("gs://x", gen=GEN, machine="n2-custom-8-65536", memory_mib=60000, environ=LABELS, **JOB)[
        "taskGroups"][0]["taskSpec"]["computeResource"] == {"cpuMilli": 8000, "memoryMib": 60000}


def test_submit_once_per_pack(tmp_path, monkeypatch):
    """A job per generation and manifest through `batch.submit_job` (its id idempotent, retried, not waited on); none
    once the pointer names them; `dry_run` submits nothing."""
    from dt_cloud import batch

    calls = []
    monkeypatch.setattr(batch, "submit_job", lambda spec, jid, region, project, **kw: calls.append((spec, jid, region, project, kw)) or jid)
    root = store(tmp_path / "s", {"2026-11-13T1734.m001.json": []})
    jid = "vm-pack-d1-2026-10-09b-g3-c001-2026-11-13t1734-m001"
    spec = V.job_spec(str(root), gen=GEN, environ=LABELS, **JOB)
    head = {"job": jid, "region": "us-east1", "gen": GEN, "manifest": "2026-11-13T1734.m001.json"}
    assert V.submit(str(root), dry_run=True, environ=LABELS, **JOB) == {**head, "spec": spec}
    assert calls == []
    assert V.submit(str(root), cross_region=True, environ=LABELS, **JOB) == {**head, "submitted": True}
    assert calls == [(spec, jid, "us-east1", "p", {"buckets": [], "cross_region": True, "exists_ok": True, "retry_for": 300})]
    pointer = {"gen": GEN, "manifest": "2026-11-13T1734.m001.json", "key": "k"}
    (root / V.POINTER).parent.mkdir()
    (root / V.POINTER).write_text(json.dumps(pointer))
    assert V.submit(str(root), environ=LABELS, **JOB) == {**pointer, "skipped": True}
    assert len(calls) == 1
    assert [V.gcs_bucket(r) for r in ("gs://b/p", "packs", "s3://b", "r2://b", "/a/dir")] == ["b", "packs", None, None, None]


def test_submit_args():
    assert V.submit_args({}) == []
    assert V.submit_args({"image": "i", "service_account": "sa", "region": "r", "project": "p", "machine": "m", "ssd_gb": 0,
                          "memory_mib": 9, "spot": False, "cross_region": True, "env": {"B": "2", "A": "1"},
                          "secrets": {"S": "s"}}) == [
        "-i", "i", "-a", "sa", "-L", "r", "-P", "p", "-m", "m", "-d", "0", "-M", "9", "-S", "-X", "-e", "A=1", "-e", "B=2", "-s", "S=s"]
    assert V.submit_args({"spot": True, "cross_region": False}) == []
    with pytest.raises(ValueError) as e:
        V.submit_args({"mem": 1})
    assert str(e.value) == ("options.vm-pack.batch: unknown mem (known: image, service_account, region, project, machine, spot, "
                            "ssd_gb, memory_mib, env, secrets, cross_region)")
