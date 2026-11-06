#!/usr/bin/env bash
# Submit job/cw-overtime.sh to GCP Batch: seal the over-time index groups
# (specs/obs-axis-indexing.md Phase 1) from the published scans' path-indices
# — the same stage cw-run.sh runs after each scan (4d), as a one-off for the
# first backfill or after a reader change. Derived from cw-reindex-submit.sh
# (no CAIOS/Slack secrets) plus the R2 seam, since the site reads groups from R2.
#
#   ./job/cw-overtime-submit.sh          # every missing group
#   DRY=1 ./job/cw-overtime-submit.sh    # print the spec
set -euo pipefail

PROJECT=oa-internal-450019
REGION=us-east1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
MACHINE=${MACHINE:-n2-standard-8}
MEMORY_MIB=${MEMORY_MIB:-30000}
MAX_RUN_SECONDS=${MAX_RUN_SECONDS:-14400}  # 5 groups × 16 scans on the first backfill
DATA_BUCKET=${DATA_BUCKET:-oa-cw-s3-usage-data}
JOB_ID=${JOB_ID:-cw-overtime-$(date -u +%Y%m%d-%H%M%S)}

vars() {
  python3 - <<EOF
import json, os
v = {
    "DATA_BUCKET": "$DATA_BUCKET",
    "WORK_DIR": "/stage",
    "CLOUDFLARE_ACCOUNT_ID": os.environ.get("CLOUDFLARE_ACCOUNT_ID", "74981a43be0de7712369306c7b19133d"),
}
v["R2_BUCKET"] = os.environ.get("R2_BUCKET", "oa-cw-s3-usage-index")
for k in ("DUCKDB_MEM", "IMPORT_JOBS", "GEN"):
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
          "commands": ["job/cw-overtime.sh"],
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
          "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cw-s3-cf-pages-token/versions/latest",
          "R2_ENDPOINT": "projects/$PROJECT/secrets/cw-s3-r2-endpoint/versions/latest",
          "R2_ACCESS_KEY_ID": "projects/$PROJECT/secrets/cw-s3-isolated-r2-access-key-id/versions/latest",
          "R2_SECRET_ACCESS_KEY": "projects/$PROJECT/secrets/cw-s3-isolated-r2-secret-access-key/versions/latest"
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
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c over-time -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
