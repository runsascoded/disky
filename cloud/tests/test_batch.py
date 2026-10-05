import pytest

from dt_cloud.batch import fleet_buckets, listing_dir, listing_job_spec, listing_regions


@pytest.fixture(autouse=True)
def _job_env(monkeypatch):
    monkeypatch.setenv("JOB_SA", "job@my-project.iam.gserviceaccount.com")
    monkeypatch.setenv("JOB_IMAGE", "registry.example/job:latest")


def script(date: str, buckets: str, weights_dirs: str = "", weights_cases: str = "") -> str:
    """The task script `listing_job_spec` writes, with the data bucket `my-data`."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euxo pipefail\n"
        f"BUCKETS=({buckets})\n"
        "b=${BUCKETS[$BATCH_TASK_INDEX]}\n"
        "W=()\n"
        f"for d in $(ls -d /gcs/my-data/listing/*/$b{weights_dirs} 2>/dev/null | sort -r); do\n"
        f'  case "$d" in */listing/{date}/$b) continue;;{weights_cases} esac\n'
        '  if [ -f "$d/_SUCCESS.json" ]; then W=(-W "$d/*.parquet"); break; fi\n'
        "done\n"
        "if [ ${#W[@]} -eq 0 ]; then\n"
        "  sii=$(ls \"/gcs/$b/inventory-reports/\" 2>/dev/null | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | sort -ru | head -1)\n"
        '  [ -n "$sii" ] && W=(-W "/gcs/$b/inventory-reports/*_${sii}T*_*.parquet")\n'
        "fi\n"
        f'disk-tree bulk-list "gcs://$b" -o "gs://my-data/listing/{date}/$b" -P 4 -w 3 -x reuse "${{W[@]}}"\n'
    )


def test_listing_dir():
    assert listing_dir("my-data", "2026-07-30", "b1") == "my-data/listing/2026-07-30/b1"


def test_listing_job_spec_task_per_bucket():
    spec = listing_job_spec("2026-07-30", ["b1", "b2"], procs=4, threads=3)
    tg = spec["taskGroups"][0]
    assert (tg["taskCount"], tg["parallelism"]) == (2, 2)
    container = tg["taskSpec"]["runnables"][0]["container"]
    assert (container["entrypoint"], container["imageUri"]) == ("/bin/bash", "registry.example/job:latest")
    # The data bucket read-write; the job's own buckets (each task reads only
    # its `/gcs/$b`) read-only.
    assert container["volumes"] == [
        "/mnt/disks/gcs/my-data:/gcs/my-data:rw",
        "/mnt/disks/gcs/b1:/gcs/b1:ro",
        "/mnt/disks/gcs/b2:/gcs/b2:ro",
    ]
    assert container["commands"] == ["-c", script("2026-07-30", "b1 b2")]
    assert tg["taskSpec"]["volumes"] == [
        {"gcs": {"remotePath": b}, "mountPath": f"/mnt/disks/gcs/{b}", "mountOptions": ["--implicit-dirs"]}
        for b in ["my-data", "b1", "b2"]
    ]
    # cpuMilli/memoryMib derive from the machine (32 vCPU → 30 requested, 2 for
    # the agent), never a constant that could exceed the node.
    assert tg["taskSpec"]["computeResource"] == {"cpuMilli": 30000, "memoryMib": 48000}
    alloc = spec["allocationPolicy"]
    assert alloc["instances"][0]["policy"] == {"machineType": "n2-standard-32", "bootDisk": {"type": "pd-balanced", "sizeGb": "50"}}
    assert (alloc["serviceAccount"], alloc["location"]) == ({"email": "job@my-project.iam.gserviceaccount.com"}, {"allowedLocations": ["regions/us-central1"]})


def test_legacy_weights_dirs_are_scanned_for_their_bucket_only():
    spec = listing_job_spec("2026-07-30", ["b1", "b2"], procs=4, threads=3, legacy={"b2": "old-listing"})
    assert spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1] == script(
        "2026-07-30", "b1 b2",
        weights_dirs=" /gcs/my-data/old-listing/*",
        weights_cases=' */old-listing/*) [ "$b" = b2 ] || continue;;',
    )


def test_listing_job_spec_compute_resource_fits_machine():
    # Regression: a hardcoded cpuMilli that exceeds the machine's vCPUs is a hard
    # 400 from Batch. The request must scale with `machine` and stay under its cap.
    for machine, vcpus in [("n2-standard-16", 16), ("n2-standard-32", 32), ("n2-standard-8", 8)]:
        cr = listing_job_spec("2026-07-30", ["b1"], machine=machine)["taskGroups"][0]["taskSpec"]["computeResource"]
        assert cr == {"cpuMilli": (vcpus - 2) * 1000, "memoryMib": vcpus * 1500}
        assert cr["cpuMilli"] <= vcpus * 1000


def test_fleet_and_regions_from_the_env(monkeypatch):
    monkeypatch.setenv("FLEET_BUCKETS", "b1 b2  b3")
    monkeypatch.setenv("LISTING_REGIONS", '{"b3": "europe-west4"}')
    assert (fleet_buckets(), listing_regions()) == (["b1", "b2", "b3"], {"b3": "europe-west4"})
    monkeypatch.delenv("LISTING_REGIONS")
    assert listing_regions() == {}


def test_unset_job_config_is_an_error_naming_it(monkeypatch):
    monkeypatch.delenv("FLEET_BUCKETS", raising=False)
    with pytest.raises(SystemExit) as e:
        fleet_buckets()
    assert str(e.value) == "FLEET_BUCKETS is unset: export the buckets to list (or pass -b per bucket)"
    for var, what in [("JOB_SA", "the service account the listing job runs as"), ("JOB_IMAGE", "the listing job's container image")]:
        monkeypatch.delenv(var)
        with pytest.raises(SystemExit) as e:
            listing_job_spec("2026-07-30", ["b1"])
        assert str(e.value) == f"{var} is unset: export {what}"
        monkeypatch.setenv(var, "x")
