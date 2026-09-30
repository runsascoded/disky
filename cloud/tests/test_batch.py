from dt_cloud.batch import FLEET_BUCKETS, listing_dir, listing_job_spec


def test_listing_dir():
    assert listing_dir("oa-gcs-usage-dvx", "2026-07-30", "marin-us-east1") == (
        "oa-gcs-usage-dvx/listing/2026-07-30/marin-us-east1"
    )


def test_listing_job_spec_task_per_bucket():
    spec = listing_job_spec("2026-07-30", ["marin-us-east1", "marin-us-west4"], procs=4, threads=3)
    tg = spec["taskGroups"][0]
    assert (tg["taskCount"], tg["parallelism"]) == (2, 2)
    container = tg["taskSpec"]["runnables"][0]["container"]
    assert container["entrypoint"] == "/bin/bash"
    assert container["volumes"] == [
        "/mnt/disks/gcs/oa-gcs-usage-dvx:/gcs/oa-gcs-usage-dvx:rw",
        "/mnt/disks/gcs/marin-us-central2:/gcs/marin-us-central2:ro",
        "/mnt/disks/gcs/marin-eu-west4:/gcs/marin-eu-west4:ro",
        "/mnt/disks/gcs/marin-us-central1:/gcs/marin-us-central1:ro",
        "/mnt/disks/gcs/marin-us-east5:/gcs/marin-us-east5:ro",
        "/mnt/disks/gcs/marin-us-east1:/gcs/marin-us-east1:ro",
        "/mnt/disks/gcs/marin-us-west4:/gcs/marin-us-west4:ro",
    ]
    assert container["commands"][0] == "-c"
    assert container["commands"][1] == (
        "#!/usr/bin/env bash\n"
        "set -euxo pipefail\n"
        "BUCKETS=(marin-us-east1 marin-us-west4)\n"
        "b=${BUCKETS[$BATCH_TASK_INDEX]}\n"
        "W=()\n"
        "for d in $(ls -d /gcs/oa-gcs-usage-dvx/listing/*/$b /gcs/oa-gcs-usage-dvx/central2-listing/* 2>/dev/null | sort -r); do\n"
        '  case "$d" in */listing/2026-07-30/$b) continue;; */central2-listing/*) [ "$b" = marin-us-central2 ] || continue;; esac\n'
        '  if [ -f "$d/_SUCCESS.json" ]; then W=(-W "$d/*.parquet"); break; fi\n'
        "done\n"
        "if [ ${#W[@]} -eq 0 ]; then\n"
        "  sii=$(ls \"/gcs/$b/inventory-reports/\" 2>/dev/null | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | sort -ru | head -1)\n"
        '  [ -n "$sii" ] && W=(-W "/gcs/$b/inventory-reports/*_${sii}T*_*.parquet")\n'
        "fi\n"
        'disk-tree bulk-list "gcs://$b" -o "gs://oa-gcs-usage-dvx/listing/2026-07-30/$b" -P 4 -w 3 -x reuse "${W[@]}"\n'
    )
    assert tg["taskSpec"]["volumes"] == [
        {
            "gcs": {"remotePath": b},
            "mountPath": f"/mnt/disks/gcs/{b}",
            "mountOptions": ["--implicit-dirs"],
        }
        for b in ["oa-gcs-usage-dvx", "marin-us-central2", "marin-eu-west4", "marin-us-central1",
                  "marin-us-east5", "marin-us-east1", "marin-us-west4"]
    ]
    # cpuMilli/memoryMib derive from the machine (32 vCPU → 30 requested, 2 for
    # the agent), never a constant that could exceed the node.
    assert tg["taskSpec"]["computeResource"] == {"cpuMilli": 30000, "memoryMib": 48000}
    policy = spec["allocationPolicy"]["instances"][0]["policy"]
    assert policy == {"machineType": "n2-standard-32", "bootDisk": {"type": "pd-balanced", "sizeGb": "50"}}


def test_listing_job_spec_compute_resource_fits_machine():
    # Regression: a hardcoded cpuMilli that exceeds the machine's vCPUs is a hard
    # 400 from Batch. The request must scale with `machine` and stay under its cap.
    for machine, vcpus in [("n2-standard-16", 16), ("n2-standard-32", 32), ("n2-standard-8", 8)]:
        cr = listing_job_spec("2026-07-30", ["marin-us-east1"], machine=machine)["taskGroups"][0]["taskSpec"]["computeResource"]
        assert cr == {"cpuMilli": (vcpus - 2) * 1000, "memoryMib": vcpus * 1500}
        assert cr["cpuMilli"] <= vcpus * 1000


def test_listing_job_spec_default_fleet():
    spec = listing_job_spec("2026-07-30")
    assert spec["taskGroups"][0]["taskCount"] == len(FLEET_BUCKETS) == 6
