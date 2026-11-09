#!/usr/bin/env bash
# Submit one run of the access-log poller (specs/access-log-freshness.md §A) to GCP Batch: `dt-cloud access ingest`
# over the usage CSVs delivered since the watermarks. The hourly Cloud Scheduler cron `gcs-access-ingest-hourly`
# (infra/gcp) posts this spec, generated from `PIN=1 DRY=1`. A run with nothing pending exits after a listing per
# bucket; a held lease (the previous run still going) exits 0. A stalled bucket (`-x`: exit 3) or a failed ingest
# posts to #gcs-admin.
set -euo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
SA=gcs-usage-job@$PROJECT.iam.gserviceaccount.com
if [ -n "${PIN:-}" ]; then unset IMAGE PARALLEL; fi
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
# One bucket at a time on a highmem-8 sustains ~100 MB/s (~360 GB/h), several times the heaviest delivery rate seen
# (eu-west4's ~110 GB/h on 2026-10-15); `-P` is for catching up a backlog by hand (memory is N × 48GB).
PARALLEL=${PARALLEL:-1}
JOB_ID=${JOB_ID:-gcs-access-ingest-$(date -u +%Y%m%d-%H%M%S)}

labels() {  # cost labels (specs/cost-labels.md): $DISKY_LABELS + component, as JSON; {} when unset
  python3 - <<'PY'
import json, os
d = dict(i.strip().split("=", 1) for i in os.environ.get("DISKY_LABELS", "").split(",") if i.strip())
print(json.dumps({**d, "component": "access-ingest"} if d else {}))
PY
}

# 48GB: 24GB OOM'd on a row-heavy 53GB chunk (2026-08-23); the scan step's peak RSS was 51GB (2026-10-18)
SCRIPT='exec dt-cloud scan-run supervise -H 60 -- bash job/access-run.sh'

spec=$(mktemp)
python3 - "$spec" "$SCRIPT" <<PY
import json, sys
spec = {
    "taskGroups": [{
        "taskCount": 1,
        "taskSpec": {
            "runnables": [{"container": {"imageUri": "$IMAGE", "entrypoint": "/bin/bash", "commands": ["-c", sys.argv[2]]}}],
            "computeResource": {"cpuMilli": 8000, "memoryMib": 60000},
            "maxRetryCount": 0,
            "maxRunDuration": "10800s",
            "environment": {
                "variables": {
                    "DATA_BUCKET": "oa-gcs-usage-dvx",
                    "PARALLEL": "$PARALLEL",
                    "DT_TRIGGER": "schedule" if "${PIN:-}" else "${DT_TRIGGER:-manual}",
                    "ACCESS_LOG_BUCKET": "marin-usage-logs",
                    "ACCESS_BUCKETS": "marin-us-central1 marin-us-central2 marin-us-east1 marin-us-east5 marin-eu-west4 marin-us-west1 marin-us-west4",
                    "SLACK_ALERT_CHANNEL": "C0C579HNRFB",  # #gcs-admin
                },
                "secretVariables": {
                    "SLACK_BOT_TOKEN": "projects/$PROJECT/secrets/gcs-alert-slack-bot-token/versions/latest",
                    "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cf-pages-token/versions/latest",
                },
            },
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": "n2-highmem-8", "bootDisk": {"type": "pd-balanced", "sizeGb": "100"}}}],
        "serviceAccount": {"email": "$SA"},
        "location": {"allowedLocations": ["regions/$REGION"]},
        "labels": json.loads('''$(labels)'''),
    },
    "labels": json.loads('''$(labels)'''),
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}
json.dump(spec, open(sys.argv[1], "w"), indent=2)
PY

# DRY=1: print the job spec (the hourly cron's body) instead of submitting
if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
dt-cloud batch submit -c access-ingest -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2  # refuses a spec without the cost labels
rm -f "$spec"
echo "$JOB_ID"
