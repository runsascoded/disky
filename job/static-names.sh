#!/usr/bin/env bash
# cw's static name index (specs/architecture/static-name-search.md, "Pipeline"; the base in specs/cw-static-names.md)
# on GCP Batch in us-east1, beside the data bucket. Every stage is `dt-cloud static-names …` from this checkout's
# committed `cloud/src/dt_cloud` (staged content-addressed by its git tree), in the pinned job image, with the
# bucket mounted at /gcs/$B and a local SSD at /stage. The generations live in CW's data bucket
# (`gs://$B/static-names/<gen>/`) and are served from CW's own R2 bucket (`R2_BUCKET`).
#
#   job/static-names.sh stage-src                 # upload HEAD's dt_cloud tree → gs://$B/static-names/src/<tree>/ (prints <tree>)
#   job/static-names.sh run KIND TASKS ARGS…      # one Batch job of TASKS tasks, each `python -m dt_cloud.$MODULE KIND -m /gcs/$B ARGS…`
#                                                 # (MODULE: static_names, static_catalog, static_roots or static_append); the
#                                                 # scratch bucket ($S: the suffix shuffle and other intermediates, 7-day expiry,
#                                                 # no soft delete) is mounted at /gcs/$S; waits for it and exits nonzero unless
#                                                 # every task succeeded
#   job/static-names.sh wait JOB                  # wait for a submitted job
#   job/static-names.sh r2-batch GEN [ARGS…]      # `static-names r2-copy -g GEN` as a 1-task Batch job as the cw job account
#                                                 # (`cw-s3-job`), with the R2 key the scan job already uses for `publish-r2`
#                                                 # (Secret Manager endpoint plus the isolated CW publisher pair):
#                                                 # GCS → R2 bucket $R2_BUCKET, idempotent (size + md5)
#
# Env: MODULE (static_names), MACHINE (n2-highmem-16), SSD (750; n2 16-vCPU needs ≥2 local SSDs of 375), PARALLELISM (TASKS),
# SPOT (1: spot VMs, 3 retries), MAX_RUN_SECONDS (14400), IMAGE (the pinned job image digest), SRC (a staged tree; default
# HEAD's; `image`: the image's own installed dt_cloud + pyrmts), NO_MOUNT=1 (no `-m`), NO_SCRATCH=1 (no scratch mount), ENV_JSON (the tasks' Batch
# `environment`), SA (the build stages' service account: `cw-s3-job`), R2_BUCKET
# (oa-cw-s3-usage-index), JOB_ID, DRY=1.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=oa-internal-450019
REGION=us-east1
B=${DATA_BUCKET:-oa-cw-s3-usage-data}
S=${STATIC_NAMES_SCRATCH:-oa-cw-s3-usage-scratch}
REPO=us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan
SA=${SA:-cw-s3-job@$PROJECT.iam.gserviceaccount.com}
R2_BUCKET=${R2_BUCKET:-oa-cw-s3-usage-index}
# The deployment's image. To pin a rerun's engines pass `IMAGE=…@sha256:D`, from within the registry's 14 days: cleanup
# (`IMAGE_DELETE_AFTER_S` in infra/gcp) deletes older images unless they're tagged `latest*` or among the newest 10.
IMAGE=${IMAGE:-$REPO:latest}
MACHINE=${MACHINE:-n2-highmem-16}
SSD=${SSD:-750}

tree() {
  if ! git diff --quiet HEAD -- cloud/src/dt_cloud; then
    echo "cloud/src/dt_cloud has uncommitted changes: commit them (the staged source is HEAD's tree)" >&2
    exit 1
  fi
  git rev-parse HEAD:cloud/src/dt_cloud
}

stage_src() {
  local t d
  t=$(tree)
  if gcloud storage ls "gs://$B/static-names/src/$t/dt_cloud/__init__.py" > /dev/null 2>&1; then echo "$t"; return; fi
  d=tmp/static-names-src/$t
  rm -rf "$d" && mkdir -p "$d/dt_cloud"
  git archive HEAD:cloud/src/dt_cloud | tar -x -C "$d/dt_cloud"
  gcloud storage rsync -r --verbosity=error "$d/dt_cloud" "gs://$B/static-names/src/$t/dt_cloud" > /dev/null
  echo "$t"
}

# `pyrmts` (pure Python; `pyrmts.intervals`) is not in the job image: stage the locked venv's copy, content-addressed by
# the archive commit `cloud/pyproject.toml` pins, and put it on the tasks' PYTHONPATH beside `dt_cloud`.
pyrmts_rev() { grep -o 'pyrmts/archive/[0-9a-f]*' cloud/pyproject.toml | head -1 | cut -d/ -f3; }
stage_pyrmts() {
  local r d src
  r=$(pyrmts_rev)
  if gcloud storage ls "gs://$B/static-names/src/pyrmts-$r/pyrmts/intervals.py" > /dev/null 2>&1; then echo "$r"; return; fi
  src=$(${PYTHON:-.venv/bin/python} -c 'import os, pyrmts; print(os.path.dirname(pyrmts.__file__))')
  d=tmp/static-names-src/pyrmts-$r
  rm -rf "$d" && mkdir -p "$d"
  cp -r "$src" "$d/pyrmts" && find "$d" -name __pycache__ -prune -exec rm -rf {} +
  gcloud storage rsync -r --verbosity=error "$d/pyrmts" "gs://$B/static-names/src/pyrmts-$r/pyrmts" > /dev/null
  echo "$r"
}

wait_job() {
  local job=$1 state="" delay=20 counts
  while :; do
    state=$(gcloud batch jobs describe "$job" --project "$PROJECT" --location "$REGION" --format='value(status.state)')
    counts=$(gcloud batch jobs describe "$job" --project "$PROJECT" --location "$REGION" --format=json \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(" ".join(f"{k}={v}" for g in (d.get("status",{}).get("taskGroups") or {}).values() for k,v in sorted(g.get("counts",{}).items())))')
    echo "$(date -u +%H:%M:%S) $job $state $counts" >&2
    case $state in
      SUCCEEDED) return 0 ;;
      FAILED|DELETION_IN_PROGRESS|CANCELLED) return 1 ;;
    esac
    sleep "$delay"
    [ "$delay" -lt 120 ] && delay=$((delay * 2))
  done
}

case ${1:?stage-src|run|r2-batch|wait} in
stage-src) stage_src ;;
r2-batch)
  # As the cw job account, with the R2 key its scan job uses for `publish-r2` (the token for cw's serving bucket):
  # no new key, no new grant.
  GEN=${2:?GEN}
  shift 2
  ENV_JSON=$(python3 -c 'import json, sys
s = "projects/%s/secrets/%s/versions/latest"
p = sys.argv[2]
print(json.dumps({"variables": {"R2_BUCKET": sys.argv[1], "STATIC_NAMES_PROFILE": "/app/job/static-names.json"},
                  "secretVariables": {"R2_ENDPOINT": s % (p, "cw-s3-r2-endpoint"), "R2_ACCESS_KEY_ID": s % (p, "cw-s3-isolated-r2-access-key-id"),
                                      "R2_SECRET_ACCESS_KEY": s % (p, "cw-s3-isolated-r2-secret-access-key")}}))' "$R2_BUCKET" "$PROJECT")
  ENV_JSON=$ENV_JSON SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com NO_SCRATCH=1 MODULE=static_names NO_MOUNT=1 MACHINE=${R2_MACHINE:-n2-highmem-4} \
    SSD=375 PARALLELISM=1 JOB_ID=${JOB_ID:-sn-r2-$(date -u +%Y%m%d-%H%M%S)} exec "$0" run r2-copy 1 -g "$GEN" "$@"
  ;;
wait) wait_job "${2:?JOB}" ;;
run)
  KIND=${2:?KIND}
  TASKS=${3:?TASKS}
  shift 3
  JOB_ID=${JOB_ID:-sn-$KIND-$(date -u +%Y%m%d-%H%M%S)}
  CPU=$(( ${MACHINE##*-} * 1000 ))
  MEM_MIB=$(( ${MACHINE##*-} * 7700 ))
  MOUNT_ARG=""
  if [ -z "${NO_MOUNT:-}" ]; then MOUNT_ARG="-m /gcs/$B"; fi
  if [ "${SRC:-}" = image ]; then
    cmd="set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && \
python3 -u -m dt_cloud.${MODULE:-static_names} $KIND $MOUNT_ARG $(printf '%q ' "$@")"
  else
    SRC=${SRC:-$(stage_src)}
    PYRMTS=$(stage_pyrmts)
    cmd="set -euo pipefail; mkdir -p /stage/src /stage/tmp /stage/out && cp -r /gcs/$B/static-names/src/$SRC/dt_cloud /gcs/$B/static-names/src/pyrmts-$PYRMTS/pyrmts /stage/src/ && cd /stage && \
PYTHONPATH=/stage/src python3 -u -m dt_cloud.${MODULE:-static_names} $KIND $MOUNT_ARG $(printf '%q ' "$@")"
  fi
  # Every task reads cw's deployment profile (`dt_cloud.static_profile_examples.CW`: buckets, layouts, gen, R2).
  ENVJ=${ENV_JSON:-'{"variables": {"STATIC_NAMES_PROFILE": "/app/job/static-names.json"}}'}
  # NO_SCRATCH=1: no scratch mount when the task only publishes completed artifacts.
  SCRATCH_RO="" SCRATCH_VOL=""
  if [ -z "${NO_SCRATCH:-}" ]; then
    SCRATCH_RO="\"/mnt/disks/gcs/$S:/gcs/$S:ro\", "
    SCRATCH_VOL="{\"gcs\": {\"remotePath\": \"$S\"}, \"mountPath\": \"/mnt/disks/gcs/$S\", \"mountOptions\": [\"--implicit-dirs\"]},"
  fi
  if [ -n "${SPOT:-}" ]; then MODEL=SPOT; RETRIES=3; else MODEL=STANDARD; RETRIES=0; fi
  spec=$(mktemp)
  cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": $TASKS,
    "parallelism": ${PARALLELISM:-$TASKS},
    "taskSpec": {
      "runnables": [{"container": {
        "imageUri": "$IMAGE",
        "entrypoint": "bash",
        "commands": $(python3 -c 'import json, sys; print(json.dumps(["-c", sys.argv[1]]))' "$cmd"),
        "volumes": ["/mnt/disks/gcs/$B:/gcs/$B:ro", $SCRATCH_RO"/mnt/disks/stage:/stage:rw"]
      }}],
      "environment": $ENVJ,
      "computeResource": {"cpuMilli": $CPU, "memoryMib": $MEM_MIB},
      "maxRetryCount": $RETRIES,
      "maxRunDuration": "${MAX_RUN_SECONDS:-14400}s",
      "volumes": [
        {"gcs": {"remotePath": "$B"}, "mountPath": "/mnt/disks/gcs/$B", "mountOptions": ["--implicit-dirs"]},
        $SCRATCH_VOL
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
      ]
    }
  }],
  "allocationPolicy": {
    "instances": [{"policy": {
      "machineType": "$MACHINE",
      "provisioningModel": "$MODEL",
      "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
      "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": "$SSD"}, "deviceName": "stage"}]
    }}],
    "serviceAccount": {"email": "$SA"},
    "location": {"allowedLocations": ["regions/$REGION"]}
  },
  "labels": {"purpose": "static-names", "deployment": "cw", "stage": "$KIND"},
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF
  if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
  DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c static-names -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
  rm -f "$spec"
  echo "submitted $JOB_ID (src $SRC, $TASKS tasks on $MACHINE $MODEL)" >&2
  wait_job "$JOB_ID"
  echo "$JOB_ID"
  ;;
esac
