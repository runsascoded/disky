# Sourced by every cw scan step (`job/cw-step.sh`) and its drivers (`job/cw-run.sh`, `job/nf/cw.nf`): the secrets
# stripped, the deployment's config, the scan's identity (SNAP_ID, GEN, DT_JOB_START: stamped once by the driver,
# exported, so every step of one run agrees) and the scan-run helpers. Paths under `$GCS/$DATA` are the data bucket's
# FUSE mount: `/gcs` in the bash driver's job, `/mnt/disks` in a Nextflow task (its work dir's bucket, mounted rw).
# Secrets arrive from Secret Manager verbatim, and a version created with
# `echo … | --data-file=-` carries a trailing newline (it broke the Discord
# digest for two days, 2026-09-16/17). Strip surrounding whitespace off every
# secret env var before anything (boto, curl, wrangler, the CLI) reads it.
# xtrace off for the block: `${!n}` would print the values.
{ set +x; } 2>/dev/null
for n in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY CLOUDFLARE_API_TOKEN DISCORD_BOT_TOKEN \
         DISCORD_GCS_USAGE_WEBHOOK SITE_TOKEN GCS_USAGE_TOKEN SLACK_BOT_TOKEN SLACK_WEBHOOK \
         CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
# The site token's old name (`GCS_USAGE_TOKEN`) is what the scheduler body still
# passes until its next `pulumi up`; carry it under the new one.
if [ -z "${SITE_TOKEN+set}" ] && [ -n "${GCS_USAGE_TOKEN+set}" ]; then export SITE_TOKEN=$GCS_USAGE_TOKEN; fi
unset GCS_USAGE_TOKEN
# The R2 archive through blobfs (`R2_ARCHIVE`): R2's own credentials profile + `buckets.yml` entry.
. "$(dirname "${BASH_SOURCE[0]}")/r2-profile.sh"
echo "cw-env: image $(dt-cloud image-sha)" >&2
set -x

# Deployment config for the shared CLI (cloud/src/dt_cloud/{index_footer,warm}.py).
export D1_DB_ID=${D1_DB_ID:-7f1e1326-b879-4ecd-8621-846621c24f36}   # oa-cw-s3-usage-db (site/wrangler.toml)
export D1_DB_NAME=${D1_DB_NAME:-oa-cw-s3-usage-db}
export INDEX_VARIANTS=${INDEX_VARIANTS:-path}                           # no user-sorted tiers here
export SITE_URL=${SITE_URL:-https://cw.oa.dev}
export SNAPSHOTS_SUBDIR=${SNAPSHOTS_SUBDIR:-cw}                          # snapshots/cw/ (site/wrangler.toml says the same)
export LAYER2_PREFIX=${LAYER2_PREFIX:-'cw-l2/{scan}/'}   # this store's layer-2 dir (`dt-cloud publish-r2 -l`; the base's is listing/{scan}/index/)
export WARM_PATHS=${WARM_PATHS:-",marin-us-east-02a,marin-us-east-02a/marin,marin-us-east-02a/tmp,marin-us-east-02a/iris,hero-checkpoints,hero-checkpoints/tmp,hero-checkpoints/marin,marin-us-east-06a,marin-us-east-06a/marin,rhoarnet-us-east-08a,marin-us-east-08a,marin-us-west-04a"}

# Every bucket in the scan; the primary first. The scheduler body sets only
# the primary, so the list's default here IS the deployment's.
BUCKETS=${SCAN_BUCKETS:-${CW_BUCKETS:-"marin-us-east-02a hero-checkpoints marin-us-east-06a rhoarnet-us-east-08a marin-us-east-08a marin-us-west-04a"}}
BUCKET=${SWEEP_BUCKET:-${CW_BUCKET:-${BUCKETS%% *}}}
ENDPOINT=${SWEEP_S3_ENDPOINT:-${CW_ENDPOINT:-https://cwobject.com}}
export SCAN_BUCKETS=$BUCKETS SWEEP_BUCKET=$BUCKET SWEEP_S3_ENDPOINT=$ENDPOINT
unset CW_BUCKETS CW_BUCKET CW_ENDPOINT
DATA=${DATA_BUCKET:-oa-cw-s3-usage-data}
export DATA_BUCKET=$DATA
PROCS=${LISTING_PROCS:-$(nproc)}
WORKERS=${LISTING_WORKERS:-8}
# Snapshot ids are sub-daily: the bucket can move >40 TiB between morning and
# evening during a cleanup push, so a date-only id would silently overwrite.
SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}
DATE=${SNAP_ID%%T*}
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}  # this run's index generation (step 4b), stamped once at start
export SNAP_ID DATE GEN DATA PROCS WORKERS BUCKETS BUCKET ENDPOINT
# The scan-run wrapper's scan (`$SCAN_ID`) when a step runs without the driver's state file (a Nextflow task, on its own
# VM), and the `scan` cost label on the Batch jobs the steps submit (`cost_labels`)
export SCAN_ID=${SCAN_ID:-$SNAP_ID}
export GCS=${GCS:-/gcs}
WORK=${WORK_DIR:-/stage/cw-$SNAP_ID}
INDEX_KEY="cw-l2/$SNAP_ID/index/$GEN"
# Scan-run record + failure alerts (specs/scan-job-wrapper.md): `dt-cloud scan-run` prints each PHASE line (timed
# from DT_JOB_START), records the run in D1 (`scan_runs`, shown at /scans; the index-sync credentials) and, when the
# job fails, posts one alert to SLACK_ALERT_CHANNEL through `disk-tree notify`'s slack channel, its text redacted of
# the profile's `secrets` (job/scan-runs.json, which also says what each phase wrote). It records only under Batch
# (BATCH_JOB_UID is the run id), not for a REPROC run, and never fails the job; xtrace is off around it so the
# tokens in its env never reach the log. Alerting was added after the 8/26–8/28 crash-loop on a missing script
# went unnoticed for two days.
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-/app/job/scan-runs.json}
export DT_JOB_START=${DT_JOB_START:-$(date +%s)}
if [ "${REPROC:-0}" = "1" ]; then export DT_RECORD=off; fi
sr() { { set +x; } 2>/dev/null; local rc=0; dt-cloud scan-run "$@" || rc=$?; set -x; return "$rc"; }
begin() { sr phase-begin "$1"; }
phase() { sr phase-end "$1" ${2:+-q "$2"}; }
# ERR: name the failed command for the record and the alert (`finish` posts it). The backfill matches this
# function's xtrace line (`error_marker`), so its name and argument order are a contract.
scan_err() { { set +x; } 2>/dev/null; SR_ERR="exit $1: $3 (${BASH_SOURCE[1]##*/}:$2)"; }
