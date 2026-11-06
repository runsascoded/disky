#!/usr/bin/env bash
# Submit job/cw-reindex.sh to GCP Batch: reindex published CW scans from their
# persisted layer-2 parquets (no CAIOS listing), to backfill the age pyramid
# onto scans that predate it. A separate, minimal submitter from
# cw-batch-submit.sh (no CAIOS/Slack secrets, longer run window) so the
# IaC-managed scan submitter stays byte-stable.
#
#   SCANS="2026-09-20T1201 2026-09-20T0001" ./job/cw-reindex-submit.sh   # just these
#   ./job/cw-reindex-submit.sh                                            # all not-yet-pyramided scans
#   DRY=1 ./job/cw-reindex-submit.sh                                      # print the spec
set -euo pipefail

PROJECT=oa-internal-450019
REGION=us-east1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
MACHINE=${MACHINE:-n2-standard-8}
MEMORY_MIB=${MEMORY_MIB:-30000}
MAX_RUN_SECONDS=${MAX_RUN_SECONDS:-28800}  # up to ~78 scans × a few min each
DATA_BUCKET=${DATA_BUCKET:-oa-cw-s3-usage-data}
JOB_ID=${JOB_ID:-cw-reindex-$(date -u +%Y%m%d-%H%M%S)}

vars() {
  python3 - <<EOF
import json, os
v = {
    "DATA_BUCKET": "$DATA_BUCKET",
    "WORK_DIR": "/stage",
    "CLOUDFLARE_ACCOUNT_ID": os.environ.get("CLOUDFLARE_ACCOUNT_ID", "74981a43be0de7712369306c7b19133d"),
}
for k in ("SCANS", "FORCE", "AGE_ONLY", "DUCKDB_MEM", "IMPORT_JOBS"):
    if os.environ.get(k):
        v[k] = os.environ[k]
print(json.dumps(v))
EOF
}

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
          "commands": ["job/cw-reindex.sh"],
          "volumes": ["/mnt/disks/gcs/$DATA_BUCKET:/gcs/$DATA_BUCKET:rw", "/mnt/disks/stage:/stage:rw"]
        }
      }],
      "computeResource": {"cpuMilli": 8000, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "${MAX_RUN_SECONDS}s",
      "volumes": [
        {"gcs": {"remotePath": "$DATA_BUCKET"}, "mountPath": "/mnt/disks/gcs/$DATA_BUCKET", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ],
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cw-s3-cf-pages-token/versions/latest"
        }
      }
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
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c index -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
