#!/usr/bin/env bash
# Submit a one-shot GCP Batch job that runs `dt-cloud interval-store <build|add>` for cw on one machine, the
# store and the scans' `path` sorts both on R2 (specs/interval-store.md §2.8): the base generation over every
# scan so far (`build`, once), or a catch-up `add`. The scan job (`cw-run.sh` 4c') appends each later scan
# itself; this is for the base, and for sizing (the summary prints per-stage seconds, work-disk and RSS peaks).
#
#   job/cw-interval-submit.sh build             # the base generation $INTERVAL_STORE_GEN over every scan
#   job/cw-interval-submit.sh build -t <scan>   # … through one scan (extra args go to the command)
#   job/cw-interval-submit.sh add               # append every scan after the generation's newest
#   job/cw-interval-submit.sh verify -s A,B,…   # parity against the per-scan path store at those scans (the base's)
#   TASKS=8 job/cw-interval-submit.sh verify -s A,B,…  # … split over 8 Batch tasks (each its share of the scans)
#   DRY=1 job/cw-interval-submit.sh build       # print the body only
#
# Same image, SA and R2 secrets as the scan job (cw-batch-submit.sh); R2 through `job/r2-profile.sh`. Never
# over a generation: `build` refuses an existing one. Standard (not spot) VM: a build has no resume.
set -euo pipefail

if [ $# -eq 0 ]; then echo "usage: $0 build|add|verify [args…]" >&2; exit 2; fi
CMD=$1; shift
case $CMD in build|add|verify) ;; *) echo "$0: build, add or verify, not $CMD" >&2; exit 2 ;; esac

PROJECT=oa-internal-450019
REGION=us-central1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
R2_BUCKET=${R2_BUCKET:-oa-cw-s3-usage-index}
GEN=${INTERVAL_STORE_GEN:-2026-10-14cw}
MACHINE=${MACHINE:-n2-highmem-16}
MEM=${MEM:-100GB}
DISK_GB=${DISK_GB:-1000}
JOB_ID=${JOB_ID:-cw-iv-$CMD-$(date -u +%Y%m%d-%H%M%S)}
STORE=${STORE:-}
SEED_GEN=${SEED_GEN:-}
TASKS=${TASKS:-1}

export PROJECT SA IMAGE R2_BUCKET GEN MACHINE MEM DISK_GB CMD STORE SEED_GEN TASKS
body=$(python3 - "$@" <<'EOF'
import json, os, shlex, sys
extra = " ".join(shlex.quote(a) for a in sys.argv[1:])
CMD, GEN = os.environ["CMD"], os.environ["GEN"]
STORE, SEED = os.environ["STORE"], os.environ["SEED_GEN"]
out = shlex.quote(STORE) if STORE else '"$R2_ARCHIVE"'
if CMD == "verify":  # every case's lines + summary land in /stage/verify (and the generation's verify/ on R2); the summary is printed
    # each task its share of the sampled scans (`-i` defaults to $BATCH_TASK_INDEX); each scan's marker lands in the generation's verify/
    run = f'dt-cloud interval-store verify -b "$R2_ARCHIVE" -g {GEN} -n {os.environ["TASKS"]} -T /stage/tmp -o /stage {extra}\n'
else:
    run = (f"dt-cloud interval-store {CMD} -g {GEN} -L 'cw-l2/{{id}}/index/{{gen}}/path-index.parquet'"
           f' -s "$R2_ARCHIVE" -o {out} -w /stage/iv -M {os.environ["MEM"]} -p "$(nproc)" {extra}\n')
if SEED:  # a store at the base's scans, which `add` re-appends from: the generation's base files, server-side
    run = f'python job/iv-seed.py "$R2_ARCHIVE" {out} {shlex.quote(SEED)}\n' + run
cmd = (
    "set -euo pipefail\n"
    ". job/r2-profile.sh\n"
    'test -n "${R2_ARCHIVE:-}" || { echo "no R2 env" >&2; exit 2; }\n'
    "set -x\n"
    + run
    + "echo INTERVAL-STORE-DONE"
)
P = os.environ["PROJECT"]
print(json.dumps({
    "taskGroups": [{
        "taskCount": int(os.environ["TASKS"]) if CMD == "verify" else 1,
        "parallelism": int(os.environ["TASKS"]) if CMD == "verify" else 1,
        "taskSpec": {
            "runnables": [{"container": {"imageUri": os.environ["IMAGE"], "entrypoint": "bash", "commands": ["-c", cmd],
                                         "volumes": ["/mnt/disks/stage:/stage:rw"]}}],
            "volumes": [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
            "maxRetryCount": 0,
            "maxRunDuration": "21600s",
            "environment": {
                "variables": {"R2_BUCKET": os.environ["R2_BUCKET"], "AWS_DEFAULT_REGION": "auto", "AWS_EC2_METADATA_DISABLED": "true",
                              "DISKY_LABELS": "app=disky,deployment=cw"},
                "secretVariables": {
                    "R2_ENDPOINT": f"projects/{P}/secrets/cw-s3-r2-endpoint/versions/latest",
                    "R2_ACCESS_KEY_ID": f"projects/{P}/secrets/cw-s3-isolated-r2-access-key-id/versions/latest",
                    "R2_SECRET_ACCESS_KEY": f"projects/{P}/secrets/cw-s3-isolated-r2-secret-access-key/versions/latest",
                },
            },
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {
            "machineType": os.environ["MACHINE"], "provisioningModel": "STANDARD",
            "bootDisk": {"type": "pd-balanced", "sizeGb": "50"},
            "disks": [{"newDisk": {"type": "pd-ssd", "sizeGb": os.environ["DISK_GB"]}, "deviceName": "stage"}],
        }}],
        "serviceAccount": {"email": os.environ["SA"]},
    },
    "labels": {"app": "disky", "deployment": "cw-s3", "component": "interval-store"},
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}, indent=1))
EOF
)

if [ -n "${DRY:-}" ]; then echo "$body"; exit 0; fi
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c interval-store -p "$PROJECT" -L "$REGION" "$JOB_ID" - <<<"$body" >/dev/null
echo "submitted $JOB_ID ($CMD $GEN on $MACHINE)"
echo "  gcloud batch jobs describe $JOB_ID --project $PROJECT --location $REGION --format='value(status.state)'"
