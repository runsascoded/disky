#!/usr/bin/env bash
# Submit the CoreWeave S3 scan to GCP Batch (see job/cw-run.sh for the chain).
#
# Deliberately a separate submitter from batch-submit.sh rather than a flag on
# it: the two jobs share almost no shape. The GCS job fans out over six buckets,
# mounts all of them, needs highmem-16 + local SSD for DuckDB spill, and posts a
# digest. This one lists a couple of buckets on a single third-party S3 endpoint
# (`SCAN_BUCKETS`, specs/done/cw-multi-bucket.md), mounts one bucket for output, and
# peaks under 2 GB.
#
#   ./job/cw-batch-submit.sh              # submit, print job id
#   DRY=1 ./job/cw-batch-submit.sh        # print the spec (for a scheduler body)
#   NF=1 ./job/cw-batch-submit.sh         # cw's own Nextflow head (job/nf/cw-head.sh) instead of the generic one
#   NF=0 ./job/cw-batch-submit.sh         # the one-VM driver (job/cw-run.sh)
#   IMAGE=…@sha256:D DT_SCAN_IMAGE=…@sha256:D ./job/cw-batch-submit.sh  # a candidate image, head AND tasks (a scan
#                                         # before it is promoted: `IMAGE` alone moves only the head)
#
# Prereq (one-time): the CAIOS S3 keypair in Secret Manager, readable by the SA.
#   printf %s "$KEY_ID" | gcloud secrets create cw-s3-access-key-id --data-file=-
#   printf %s "$SECRET" | gcloud secrets create cw-s3-secret-access-key --data-file=-
set -euo pipefail

# PIN=1: emit the canonical/cron spec — drop every ambient override so the body is
# byte-identical regardless of the caller's shell (the IaC in infra/gcp
# generates the Cloud Scheduler body from `PIN=1 DRY=1`). Must precede the
# ${VAR:-default} knobs below; the python vars() honors PIN too.
if [ -n "${PIN:-}" ]; then unset IMAGE MACHINE MEMORY_MIB DATA_BUCKET SWEEP_BUCKET; fi

PROJECT=oa-internal-450019
# A head only orchestrates, so it stays beside the image; the one-VM driver (`NF=0`) mounts the data bucket, so it runs beside that.
REGION=us-central1
if [ "${NF:-scan}" = 0 ]; then REGION=us-east1; fi
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
export IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}  # exported: stage 4f runs on it
# Measured on a 92M-object scan: 1.1 GB RSS listing, 1.8 GB import. The listing
# is CPU-bound (92% of 8 vCPUs on 02a, 10/13), but 16 vCPUs (10/14) only moved
# the run's critical path onto the static-names stage it waits on, at twice the
# price, so it stays on 8.
MACHINE=${MACHINE:-n2-standard-8}
CPU_MILLI=$(( ${MACHINE##*-} * 1000 ))
MEMORY_MIB=${MEMORY_MIB:-30000}
DATA_BUCKET=${DATA_BUCKET:-oa-cw-s3-usage-data}
JOB_ID=${JOB_ID:-cw-scan-$(date -u +%Y%m%d-%H%M%S)}

labels() {  # cost labels (specs/cost-labels.md): $DISKY_LABELS + component $1, as JSON; {} when unset
  python3 - "$1" <<'PY'
import json, os, sys
d = dict(i.strip().split("=", 1) for i in os.environ.get("DISKY_LABELS", "").split(",") if i.strip())
print(json.dumps({**d, "component": sys.argv[1]} if d else {}))
PY
}

vars() {
  python3 - <<'EOF'
import json, os
# PIN=1 emits the canonical/cron spec: ignore ambient overrides so the body is
# byte-stable regardless of the caller's shell (see the PIN note at the top and
# infra/gcp). Without PIN the knobs and passthrough list stay live.
pin = bool(os.environ.get("PIN"))
g = (lambda k, d="": d) if pin else os.environ.get
v = {
    # the stack's cost labels (PIN keeps them: `submitter_spec` passes them through)
    **({"DISKY_LABELS": os.environ["DISKY_LABELS"]} if os.environ.get("DISKY_LABELS") else {}),
    # what started the scan (`scan_runs.trigger`, the scan's `trigger.json`): the cron body is the PIN spec;
    # everything else is a hand submit
    "DT_TRIGGER": "schedule" if pin else os.environ.get("DT_TRIGGER", "manual"),
    "SWEEP_BUCKET": g("SWEEP_BUCKET", "marin-us-east-02a"),
    "SWEEP_S3_ENDPOINT": g("SWEEP_S3_ENDPOINT", "https://cwobject.com"),
    "DATA_BUCKET": g("DATA_BUCKET", "oa-cw-s3-usage-data"),
    "WORK_DIR": g("WORK_DIR", "/stage/cw"),
    # boto/aws-sdk needs a region even though CAIOS ignores it
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    # CAIOS rejects path-style requests, and boto's auto degrades to path
    # for custom endpoints when no ~/.aws/config says otherwise.
    "DT_S3_ADDRESSING_STYLE": "virtual",
    # Slack digest target (specs/cw-slack-digest.md): chat.postMessage (bot
    # token) so per-message avatars apply
    "SLACK_CHANNEL": g("SLACK_CHANNEL", "C0C1YR7D0KU"),  # #cw-s3-usage
    # failure alerts go to #cw-s3-admin (with the staging threads); #cw-s3-usage carries only the digest
    "SLACK_ALERT_CHANNEL": g("SLACK_ALERT_CHANNEL", "C0C53CPM886"),
    # CF account for the digest plot's `wrangler pages deploy` (token is a secretVariable)
    "CLOUDFLARE_ACCOUNT_ID": g("CLOUDFLARE_ACCOUNT_ID", "74981a43be0de7712369306c7b19133d"),
    # R2 serving bucket the 4c publish stage copies each scan's served subset
    # to (the `r2_bucket` output of the cw-s3 CF Pulumi stack; creds are the
    # `cw-s3-r2-*` secretVariables) — specs/done/r2-serving-migration.md
    "R2_BUCKET": g("R2_BUCKET", "oa-cw-s3-usage-index"),
    # Stage 4f, the static name index (specs/cw-static-names.md): every scan appends itself as a run, its Batch
    # stages on this job's own image (the profile pins none).
    "STATIC_NAMES": g("STATIC_NAMES", "1"),
    "STATIC_NAMES_IMAGE": g("STATIC_NAMES_IMAGE", os.environ["IMAGE"]),
}
if not pin:  # one-off overrides forwarded only for manual submits, never the cron spec
    for k in ["SCAN_BUCKETS", "SNAP_ID", "LISTING_PROCS", "LISTING_WORKERS", "IMPORT_JOBS",
              "GEN", "NF_IMAGE", "NF_SECRETS", "DT_RECORD", "DT_ALERTS", "DT_SCAN_IMAGE"]:
        if k in os.environ:
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
          "entrypoint": "dt-cloud",
          "commands": ["scan-run", "supervise", "--", "bash", "job/cw-run.sh"],
          "volumes": ["/mnt/disks/gcs/$DATA_BUCKET:/gcs/$DATA_BUCKET:rw", "/mnt/disks/stage:/stage:rw"]
        }
      }],
      "computeResource": {"cpuMilli": $CPU_MILLI, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "14400s",
      "volumes": [
        {"gcs": {"remotePath": "$DATA_BUCKET"}, "mountPath": "/mnt/disks/gcs/$DATA_BUCKET", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ],
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "AWS_ACCESS_KEY_ID": "projects/$PROJECT/secrets/cw-s3-access-key-id/versions/latest",
          "AWS_SECRET_ACCESS_KEY": "projects/$PROJECT/secrets/cw-s3-secret-access-key/versions/latest",
          "SLACK_BOT_TOKEN": "projects/$PROJECT/secrets/cw-s3-slack-bot-token/versions/latest",
          "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cw-s3-cf-pages-token/versions/latest",
          "SITE_TOKEN": "projects/$PROJECT/secrets/cw-s3-job-grant/versions/latest",
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
    "location": {"allowedLocations": ["regions/$REGION"]},
    "labels": $(labels scan)
  },
  "labels": $(labels scan),
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF

# The generic Nextflow head (`dt-cloud scan head $PROFILE`, specs/pipelines-everywhere.md) is the default, and so the
# cron's body (PIN=1 runs under a scrubbed env); NF=1 submits cw's own head (job/nf/cw-head.sh), NF=0 the one-VM driver
# (job/cw-run.sh). The head only
# drives the DAG (each step is a Batch task of its own, which fetches its own secrets), so: a small VM, no stage disk,
# and of the secrets only what the head itself uses (the scan-run record's D1 token, the alert's Slack token).
# The generic head reads the deployment's profile (`PROFILE`, a path in the image).
if [ "${NF:-scan}" != 0 ]; then
  NF=${NF:-scan} PROFILE=${PROFILE:-/app/job/profiles/cw.yml} python3 - "$spec" <<'PY'
import json, os, sys
p = sys.argv[1]; j = json.load(open(p))
t = j["taskGroups"][0]["taskSpec"]
c = t["runnables"][0]["container"]
if os.environ["NF"] == "scan":
    c["entrypoint"] = "dt-cloud"
    c["commands"] = ["scan", "head", os.environ["PROFILE"]]
else:
    c["entrypoint"] = "bash"
    c["commands"] = ["job/nf/cw-head.sh"]
c["volumes"] = []
t["volumes"] = []
t["computeResource"] = {"cpuMilli": 2000, "memoryMib": 7000}
t["maxRunDuration"] = "21600s"
keep = {"CLOUDFLARE_API_TOKEN", "SLACK_BOT_TOKEN"}
t["environment"]["secretVariables"] = {k: v for k, v in t["environment"]["secretVariables"].items() if k in keep}
pol = j["allocationPolicy"]["instances"][0]["policy"]
pol["machineType"] = "e2-standard-2"
pol.pop("disks", None)
pol["bootDisk"] = {"type": "pd-balanced", "sizeGb": "30"}
json.dump(j, open(p, "w"), indent=2)
PY
fi

if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
