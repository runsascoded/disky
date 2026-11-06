#!/usr/bin/env bash
# Submit path-store phase 0 (specs/path-store.md §5, phase 0) to GCP Batch:
# one task on the daily cw job's machine class (n2-standard-8, 30 GB, pd-ssd
# stage — cw-batch-submit.sh's) that cuts the store's two sorts from one cw
# layer-2 and plans the reader's spans over them; the chain is
# job/cw-path-store-phase0.sh. Output goes to a scratch prefix (OUT_URI), never
# under `cw-l2/` or `snapshots/`, so nothing served changes.
#
#   ./job/cw-path-store-phase0-submit.sh                  # submit, print job id
#   DRY=1 ./job/cw-path-store-phase0-submit.sh            # print the spec
#   L2_URI=gs://… OUT_URI=gs://… ./job/cw-path-store-phase0-submit.sh
#
# Needs the `:latest` image built from a tree that has `disk-tree tiers` (cloud
# `0ebb173` or later) and this script's task file. Same job SA as the scans
# (reads + writes the data bucket via ADC); no secrets.
set -euo pipefail
PROJECT=oa-internal-450019
REGION=us-east1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
MACHINE=${MACHINE:-n2-standard-8}
MEMORY_MIB=${MEMORY_MIB:-30000}
L2_URI=${L2_URI:-gs://oa-cw-s3-usage-data/cw-l2/2026-09-30T1203/marin-us-east-02a.parquet}
OUT_URI=${OUT_URI:-gs://oa-cw-s3-usage-scratch/path-store-phase0/2026-09-30T1203}
JOB_ID=${JOB_ID:-cw-path-store-phase0-$(date -u +%Y%m%d-%H%M%S)}

spec=$(mktemp)
cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": 1,
    "taskSpec": {
      "runnables": [{
        "container": {
          "imageUri": "$IMAGE",
          "entrypoint": "bash",
          "commands": ["job/cw-path-store-phase0.sh"],
          "volumes": ["/mnt/disks/stage:/stage:rw"]
        }
      }],
      "computeResource": {"cpuMilli": 8000, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "7200s",
      "volumes": [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
      "environment": {"variables": {"L2_URI": "$L2_URI", "OUT_URI": "$OUT_URI", "WORK_DIR": "/stage/phase0"}}
    }
  }],
  "allocationPolicy": {
    "instances": [{
      "policy": {
        "machineType": "$MACHINE",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
        "disks": [{"newDisk": {"type": "pd-ssd", "sizeGb": "200"}, "deviceName": "stage"}]
      }
    }],
    "serviceAccount": {"email": "$SA"},
    "location": {"allowedLocations": ["regions/$REGION"]}
  },
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF

if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c analysis -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
