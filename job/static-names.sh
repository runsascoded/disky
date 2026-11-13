#!/usr/bin/env bash
# The static name-search build (specs/architecture/static-name-search.md, "Pipeline") on GCP Batch, beside the data
# bucket. Every stage is `dt-cloud static-names …` from this checkout's committed `cloud/src/dt_cloud` (staged
# content-addressed by its git tree), in the deployment's job image, with the data bucket mounted at /gcs/$B, the
# scratch bucket at /gcs/$S, and a local SSD at /stage.
#
#   job/static-names.sh stage-src                 # upload HEAD's dt_cloud tree → gs://$B/static-names/src/<tree>/ (prints <tree>)
#   job/static-names.sh run KIND TASKS ARGS…      # one Batch job of TASKS tasks, each `python -m dt_cloud.$MODULE KIND -m /gcs/$B ARGS…`
#                                                 # (MODULE: static_names, static_catalog, static_roots, static_append, …);
#                                                 # waits for it and exits nonzero unless every task succeeded
#   job/static-names.sh wait JOB                  # wait for a submitted job
#   job/static-names.sh resume-after JOB SCAN     # durable Batch controller: wait for JOB to succeed, then catch up search
#                                                 # through SCAN (`static-names runs add -c`, no carries), in the image
#   job/static-names.sh r2-batch GEN [ARGS…]      # `static-names r2-copy -g GEN` (GCS → the profile's R2 bucket, idempotent)
#                                                 # as a 1-task Batch job as the profile's `r2_sa` (else `sa`), its R2 key
#                                                 # from the Secret Manager secrets the profile's `r2_secrets` names
#
# Config: the deployment's static-names profile (`dt_cloud.static_profile`): `$STATIC_NAMES_PROFILE`, default
# `job/static-names.json` beside this script, each field's env var on top. The launcher reads its `project`, `region`,
# `bucket` ($B), `scratch` ($S: the suffix shuffle and other intermediates; unset: no scratch mount), `image`, `sa`,
# `r2_sa`, `r2_bucket` and `r2_secrets`, and hands the resolved profile to every task (`STATIC_NAMES_PROFILE`, inline).
# Launcher-only settings (an image pin, `DISKY_LABELS`) may go in `job/static-names.env` beside this script (sourced
# when present; `$STATIC_NAMES_ENV` names another).
#
# Env: MODULE (static_names), MACHINE (n2-highmem-16), SSD (750; n2 16-vCPU needs ≥2 local SSDs of 375), PARALLELISM (TASKS),
# SPOT (1: spot VMs, 3 retries), MAX_RUN_SECONDS (14400), IMAGE (default the profile's `image`), SRC (a staged tree;
# default HEAD's; `image`: the image's own installed dt_cloud + pyrmts), NO_MOUNT=1 (no `-m`), NO_SCRATCH=1 (no scratch
# mount), ENV_JSON (the tasks' Batch `environment`; the profile is added to its variables), SA (default the profile's
# `sa`), R2_ENDPOINT or CLOUDFLARE_ACCOUNT_ID (r2-batch, when the profile names no endpoint secret), JOB_ID, DRY=1.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/.."

env_file=${STATIC_NAMES_ENV:-$here/static-names.env}
if [ -f "$env_file" ]; then
  # shellcheck disable=SC1090
  . "$env_file"
elif [ -n "${STATIC_NAMES_ENV:-}" ]; then
  echo "STATIC_NAMES_ENV=$STATIC_NAMES_ENV: no such file" >&2
  exit 2
fi

export STATIC_NAMES_PROFILE=${STATIC_NAMES_PROFILE:-$here/static-names.json}
PROFILE_JSON=$(dt-cloud static-names profile)
# One profile field (`json.dumps` for a non-string, empty for null); `!` suffix: required.
pf() {
  python3 -c 'import json, os, sys
name = sys.argv[2].rstrip("!")
v = json.loads(sys.argv[1]).get(name)
if v in (None, "", [], {}):
    if sys.argv[2].endswith("!"):
        src = os.environ["STATIC_NAMES_PROFILE"]
        sys.exit(f"static-names.sh: the profile ({src}) has no {name}")
    v = ""
print(v if isinstance(v, str) else json.dumps(v))' "$PROFILE_JSON" "$1"
}
PROJECT=$(pf project!)
REGION=$(pf region!)
B=$(pf bucket!)
S=$(pf scratch)
IMAGE=${IMAGE:-$(pf image)}
SA=${SA:-$(pf sa!)}
MACHINE=${MACHINE:-n2-highmem-16}
SSD=${SSD:-750}

# The tasks' Batch `environment`: $1 (JSON; `{}` when empty) with the resolved profile and, when set, `$DISKY_LABELS`,
# `$SCAN_RUN_ID`, `$SCAN_ID` added to its variables.
task_env() {
  PROFILE_JSON=$PROFILE_JSON BASE=${1:-'{}'} python3 - <<'PY'
import json, os
d = json.loads(os.environ["BASE"])
v = d.setdefault("variables", {})
v.setdefault("STATIC_NAMES_PROFILE", json.dumps(json.loads(os.environ["PROFILE_JSON"]), separators=(",", ":")))
for key in ("DISKY_LABELS", "SCAN_RUN_ID", "SCAN_ID"):
    if os.environ.get(key):
        v[key] = os.environ[key]
print(json.dumps(d))
PY
}

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

# `pyrmts` (pure Python; `pyrmts.intervals`) may not be in the job image: stage the locked venv's copy, content-addressed
# by the archive commit `cloud/pyproject.toml` pins, and put it on the tasks' PYTHONPATH beside `dt_cloud`.
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

# The plain R2 endpoint, when the profile names no secret for it: `$R2_ENDPOINT`, else from `$CLOUDFLARE_ACCOUNT_ID`.
r2_endpoint() {
  if [ -n "${R2_ENDPOINT:-}" ]; then echo "$R2_ENDPOINT"
  elif [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then echo "https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com"
  fi
}

case ${1:?stage-src|run|r2-batch|wait|resume-after} in
stage-src) stage_src ;;
resume-after)
  WAIT_JOB=${2:?JOB}
  export SCAN_ID=${3:?SCAN}
  : "${IMAGE:?no image: set image in the profile, or IMAGE}"
  wait_arg=$(printf '%q' "$WAIT_JOB")
  scan_arg=$(printf '%q' "$SCAN_ID")
  image_arg=$(printf '%q' "$IMAGE")
  region_arg=$(printf '%q' "$REGION")
  COMMAND="set -euo pipefail; BATCH_REGION=$region_arg dt-cloud job watch -L -i 30 $wait_arg && cd /app && STATIC_NAMES_IMAGE=$image_arg STATIC_NAMES_SPOT=0 dt-cloud static-names runs add -c -M $scan_arg"
  if [ -z "${ENV_JSON:-}" ]; then
    ENDPOINT=$(r2_endpoint)
    ENV_JSON=$(PROJECT=$PROJECT ENDPOINT=$ENDPOINT python3 -c 'import json, os
v = {"GCP_PROJECT": os.environ["PROJECT"]}
for k, e in (("R2_ENDPOINT", "ENDPOINT"), ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID")):
    if os.environ.get(e):
        v[k] = os.environ[e]
print(json.dumps({"variables": v}))')
  fi
  COMMAND="$COMMAND" ENV_JSON="$ENV_JSON" SRC=image MACHINE=n2-highmem-4 SSD=375 NO_MOUNT=1 \
    JOB_ID=${JOB_ID:-sn-resume-$(date -u +%Y%m%d-%H%M%S)} exec "$0" run resume 1
  ;;
r2-batch)
  # The R2 copy as a 1-task Batch job: the profile's R2 bucket, its R2 key (and endpoint, if it names one) from Secret
  # Manager, as the profile's R2 account; no scratch mount (it only publishes finished files).
  GEN=${2:?GEN}
  shift 2
  R2_BUCKET=$(pf r2_bucket!)
  R2_SECRETS=$(pf r2_secrets!)
  ENDPOINT=$(r2_endpoint)
  ENV_JSON=$(PROJECT=$PROJECT R2_BUCKET=$R2_BUCKET SECRETS=$R2_SECRETS ENDPOINT=$ENDPOINT python3 -c 'import json, os, sys
names = {"endpoint": "R2_ENDPOINT", "key_id": "R2_ACCESS_KEY_ID", "secret": "R2_SECRET_ACCESS_KEY"}
project = os.environ["PROJECT"]
secrets = {names[k]: f"projects/{project}/secrets/{v}/versions/latest" for k, v in json.loads(os.environ["SECRETS"]).items()}
for var in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
    if var not in secrets:
        sys.exit(f"static-names.sh: the profile r2_secrets names no secret for {var}")
variables = {"R2_BUCKET": os.environ["R2_BUCKET"]}
if "R2_ENDPOINT" not in secrets:
    if not os.environ.get("ENDPOINT"):
        sys.exit("static-names.sh: the R2 endpoint: set R2_ENDPOINT or CLOUDFLARE_ACCOUNT_ID, or name its secret in the profile r2_secrets (endpoint)")
    variables["R2_ENDPOINT"] = os.environ["ENDPOINT"]
print(json.dumps({"variables": variables, "secretVariables": secrets}))')
  R2_SA=$(pf r2_sa)
  ENV_JSON=$ENV_JSON SA=${R2_SA:-$SA} NO_SCRATCH=1 MODULE=static_names NO_MOUNT=1 MACHINE=${R2_MACHINE:-n2-highmem-4} SSD=375 \
    PARALLELISM=1 JOB_ID=${JOB_ID:-sn-r2-$(date -u +%Y%m%d-%H%M%S)} exec "$0" run r2-copy 1 -g "$GEN" "$@"
  ;;
wait) wait_job "${2:?JOB}" ;;
run)
  KIND=${2:?KIND}
  TASKS=${3:?TASKS}
  shift 3
  : "${IMAGE:?no image: set image in the profile, or IMAGE}"
  JOB_ID=${JOB_ID:-sn-$KIND-$(date -u +%Y%m%d-%H%M%S)}
  CPU=$(( ${MACHINE##*-} * 1000 ))
  MEM_MIB=$(( ${MACHINE##*-} * 7700 ))
  MOUNT_ARG=""
  if [ -z "${NO_MOUNT:-}" ]; then MOUNT_ARG="-m /gcs/$B"; fi
  if [ -n "${COMMAND:-}" ]; then
    cmd=$COMMAND
  elif [ "${SRC:-}" = image ]; then
    cmd="set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && \
python3 -u -m dt_cloud.${MODULE:-static_names} $KIND $MOUNT_ARG $(printf '%q ' "$@")"
  else
    SRC=${SRC:-$(stage_src)}
    PYRMTS=$(stage_pyrmts)
    cmd="set -euo pipefail; mkdir -p /stage/src /stage/tmp /stage/out && cp -r /gcs/$B/static-names/src/$SRC/dt_cloud /gcs/$B/static-names/src/pyrmts-$PYRMTS/pyrmts /stage/src/ && cd /stage && \
PYTHONPATH=/stage/src python3 -u -m dt_cloud.${MODULE:-static_names} $KIND $MOUNT_ARG $(printf '%q ' "$@")"
  fi
  if [ "${MODULE:-static_names}" = static_drill ] && [ "$KIND" = build ]; then
    diagnostics=$(python3 -c 'from dt_cloud.append_runner import drill_exit_diagnostics; print(drill_exit_diagnostics(), end="")')
    cmd="$diagnostics$cmd"
  elif [ "${MODULE:-static_names}" = static_anchors ] && [ "$KIND" = run ]; then
    diagnostics=$(python3 -c 'from dt_cloud.append_runner import drill_exit_diagnostics; print(drill_exit_diagnostics("ANCHOR"), end="")')
    cmd="$diagnostics$cmd"
  fi
  ENVJ=$(task_env "${ENV_JSON:-}")
  if [ -n "${SPOT:-}" ]; then MODEL=SPOT; RETRIES=3; else MODEL=STANDARD; RETRIES=0; fi
  # The scratch bucket's mount, unless the profile has none or NO_SCRATCH=1 (a task that only publishes finished files).
  SCRATCH_RO="" SCRATCH_VOL=""
  if [ -n "$S" ] && [ -z "${NO_SCRATCH:-}" ]; then
    SCRATCH_RO="\"/mnt/disks/gcs/$S:/gcs/$S:ro\", "
    SCRATCH_VOL="{\"gcs\": {\"remotePath\": \"$S\"}, \"mountPath\": \"/mnt/disks/gcs/$S\", \"mountOptions\": [\"--implicit-dirs\"]},"
  fi
  # cost labels (specs/cost-labels.md): $DISKY_LABELS + component, {} when unset
  COST_LABELS=$(dt-cloud cost-labels -c static-names -j)
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
    "location": {"allowedLocations": ["regions/$REGION"]},
    "labels": $COST_LABELS
  },
  "labels": $(python3 -c 'import json, sys; print(json.dumps({"purpose": "static-names", "stage": sys.argv[1], **json.loads(sys.argv[2])}))' "$KIND" "$COST_LABELS"),
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF
  if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
  dt-cloud batch submit -c static-names -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2  # refuses a spec without the cost labels
  rm -f "$spec"
  echo "submitted $JOB_ID (src ${SRC:-}, $TASKS tasks on $MACHINE $MODEL)" >&2
  wait_job "$JOB_ID"
  echo "$JOB_ID"
  ;;
esac
