#!/usr/bin/env bash
# One laptop capture on the scan pipeline (`job/nf/scan.nf`, Nextflow's local executor in this task;
# specs/m3-scan-nf.md): the capture's host → its store's profile (`job/profiles/m3.yml` for the primary,
# `m3-host.yml` filled for another store), `dt-cloud scan head` over it, then the meta store's (`m3-meta.yml`)
# after a primary scan.
#
#   scan.sh r2://<bucket>/captures/<host>/<root slug>/<stamp>
#
# The scan's id and generation come from the capture's stamp, as `ingest.sh`'s (a re-ingest lands where the first
# did). The head records the run (`$AWS_BATCH_JOB_ID`, started at the capture's start, its capture and queue phases
# before the job: `DT_RUN_*`, `DT_PRE_PHASES`); each step records its own phases. R2 keys arrive as AWS_* (Batch
# injects them from Secrets Manager) and are handed to the steps under the scan pipeline's R2_* names.
set -euo pipefail

job_t0=$(date +%s)
capture=${1:?usage: scan.sh r2://<bucket>/<capture dir>}
: "${R2_ENDPOINT_URL:?}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}"
region=${AWS_REGION:-${AWS_DEFAULT_REGION:-}}  # the job's own, before R2's `auto` replaces it
export AWS_DEFAULT_REGION=auto
app=${INGEST_APP_ROOT:-/app}
nf=${SCAN_NF_DIR:-$app/job/nf}
profiles=${SCAN_PROFILES:-$app/job/profiles}
bucket=${capture#r2://}; bucket=${bucket%%/*}
key=${capture#r2://$bucket/}
stamp=${key%/}; stamp=${stamp##*/}
if [[ ! $stamp =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2})-([0-9]{2})-([0-9]{2})Z$ ]]; then
  echo "[scan] invalid capture stamp: expected %Y-%m-%dT%H-%M-%SZ" >&2
  exit 2
fi
m=("${BASH_REMATCH[@]}")
date=${DATE:-${m[1]}-${m[2]}-${m[3]}T${m[4]}${m[5]}}
gen=${GEN:-${m[1]}${m[2]}${m[3]}${m[4]}${m[5]}}
captured=$(date -u -d "${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z" +%s)

# The store, as `ingest.sh` resolves it (D1 `hosts`, `dt-cloud hosts resolve`): an unknown or disconnected host is
# refused before anything runs.
host=${key#captures/}; host=${host%%/*}
rc=0
resolved=$(cd "$app/cloud" && dt-cloud hosts resolve "$host" 2>&1) || rc=$?
case $rc in
  0) store=$resolved ;;
  3) echo "[scan] unknown capture host '$host' (${resolved#hosts: }); publishing nothing" >&2; exit 3 ;;
  *) echo "[scan] hosts lookup failed (exit $rc): $resolved" >&2; exit 1 ;;
esac
if [[ $store == primary ]]; then
  profile=$profiles/m3.yml
else
  [[ $store =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "[scan] bad store name '$store'" >&2; exit 2; }
  profile=${WORK:-/work}/m3-$store.yml
  mkdir -p "$(dirname "$profile")"
  sed "s/@STORE@/$store/g" "$profiles/m3-host.yml" > "$profile"
fi

# The steps' R2 names (the scan pipeline's: `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, …); `R2_BUCKET` is the profile's. The head's
# own `r2://` reads (sizing a pre-job phase's outputs) take the engine's endpoint variable.
export R2_ENDPOINT=$R2_ENDPOINT_URL R2_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
export DISKY_R2_ENDPOINT_URL=$R2_ENDPOINT_URL
export DT_CAPTURE=$capture SNAP_ID=$date GEN=$gen DT_JOB_START=$job_t0 DT_RUN_ID=${AWS_BATCH_JOB_ID:-}
export DT_RUN_KIND=ingest DT_RUN_MACHINE=${INGEST_MACHINE:-} DT_RUN_SPOT=${INGEST_SPOT:-} DT_RUN_REGION=$region
export DT_RUN_AT=$captured DT_RUN_TASKS=1
# The capture ran on the laptop until its marker landed, then the job queued until it started.
marked=$(aws s3api head-object --bucket "$bucket" --key "$key/_SUCCESS.json" --endpoint-url "$R2_ENDPOINT_URL" \
  --query LastModified --output text 2>/dev/null) || marked=
t_marked=$( [[ -n $marked ]] && date -u -d "$marked" +%s 2>/dev/null ) || t_marked=
if [[ -n $t_marked ]]; then
  export DT_PRE_PHASES="[{\"name\": \"capture\", \"began\": $captured, \"ended\": $t_marked, \"outputs\": [\"capture=$capture/\"]}, {\"name\": \"queue\", \"began\": $t_marked, \"ended\": $job_t0}]"
else
  echo "[scan] capture marker time unreadable: one capture+queue span"
  export DT_PRE_PHASES="[{\"name\": \"queue\", \"began\": $captured, \"ended\": $job_t0}]"
fi
echo "[scan] capture=$key scan=$date gen=$gen host=$host store=$store profile=${profile##*/}"
cd "$app/cloud"
dt-cloud scan head -N "$nf" "$profile"

# The meta store: the bucket itself, at the same scan id, after a primary scan (its own run).
if [[ $store == primary && -f $profiles/m3-meta.yml ]]; then
  unset DT_CAPTURE DT_PRE_PHASES
  export DT_RUN_ID=${AWS_BATCH_JOB_ID:+$AWS_BATCH_JOB_ID-meta} DT_RUN_KIND=meta DT_RUN_AT=$(date +%s)
  dt-cloud scan head -N "$nf" "$profiles/m3-meta.yml"
fi
