#!/usr/bin/env bash
# One laptop capture → path index + snapshot, back to R2.
#
#   ingest.sh r2://<bucket>/captures/<host>/<root slug>/<stamp>
#
# Mirrors .github/workflows/daily-ingest.yml's union → path-index → upload →
# footers-to-D1 steps, with the capture's shards as the listing. R2 keys arrive
# as AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY and the D1 token as
# CLOUDFLARE_API_TOKEN (Batch injects them from Secrets Manager); the endpoint
# is R2_ENDPOINT_URL, the database D1_DB_ID / D1_DB_NAME. Logs are private (CloudWatch), but print
# totals only anyway: no laptop paths.
#
# With SCAN_RUNS_PROFILE set (the deployment's `dt-cloud scan-run` profile), the
# run is recorded in D1 as it goes (specs/scan-runs-ui.md): kind `ingest`, its
# id `$AWS_BATCH_JOB_ID` (the profile's `run_id_env`), started at the capture's
# own start, then phases capture (→ the capture's marker landing), queue (→ this
# job's start), fetch, path-index, capture-meta, upload, index-sync; the EXIT
# trap records the end, a failure with the phase it was in. Each phase also
# prints `PHASE <name>: <s>s`. Recording never fails the ingest.
#
# A failure the script sees is also pushed to a person (`notify_failure`). A
# kill it never sees (a Spot reclaim, OOM of the shell, an image pull) needs a
# watcher outside the job.
set -euo pipefail

job_t0=$(date +%s)
capture=${1:?usage: ingest.sh r2://<bucket>/<capture dir>}
: "${R2_ENDPOINT_URL:?}"
region=${AWS_REGION:-${AWS_DEFAULT_REGION:-}}  # the job's own, before R2's `auto` replaces it
export AWS_DEFAULT_REGION=auto
export DISK_TREE_R2_ENDPOINT_URL="$R2_ENDPOINT_URL"
bucket=${capture#r2://}; bucket=${bucket%%/*}
key=${capture#r2://$bucket/}
# The scan's date and generation come from the capture's own stamp
# (`disk-tree capture`: `<host>/<root>/%Y-%m-%dT%H-%M-%SZ`), so a re-ingest
# lands where the first did. Keep the time in the scan id: morning and evening
# captures must not replace each other's snapshots. DATE / GEN override both.
stamp=${key%/}; stamp=${stamp##*/}
if [[ $stamp =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2})-([0-9]{2})-([0-9]{2})Z$ ]]; then
  m=("${BASH_REMATCH[@]}")
  date=${DATE:-${m[1]}-${m[2]}-${m[3]}T${m[4]}${m[5]}}
  gen=${GEN:-${m[1]}${m[2]}${m[3]}${m[4]}${m[5]}}
  captured="${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}"
else
  echo "[ingest] invalid capture stamp: expected %Y-%m-%dT%H-%M-%SZ" >&2
  exit 2
fi
# The store comes from the capture's host: `HOST_STORES` (`host=store,…`, the job
# env) names the secondary stores (`STORES_JSON` keys on the site), which publish
# under `snapshots/<store>/` and sync D1 as that store. Any other host is the
# deployment's primary store: `snapshots/$STORE/` (default `laptop`), D1 `primary`.
host=${key#captures/}; host=${host%%/*}
store=${STORE:-laptop}
d1_store=primary
for pair in ${HOST_STORES//,/ }; do
  if [[ ${pair%%=*} == "$host" ]]; then store=${pair#*=}; d1_store=$store; fi
done
work=${WORK:-/work}
app=${INGEST_APP_ROOT:-/app}
time_bin=${INGEST_TIME:-/usr/bin/time}
s3() { aws s3 "$@" --endpoint-url "$R2_ENDPOINT_URL" --only-show-errors; }

# ── Scan-run recording (a no-op without SCAN_RUNS_PROFILE) ──────────────────
# `epoch YYYY-MM-DDTHH:MM:SS` (UTC; GNU date in the image, BSD date elsewhere).
epoch() { date -u -d "$1Z" +%s 2>/dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%S' "$1" +%s; }
scan_run() {
  [[ -n ${SCAN_RUNS_PROFILE:-} ]] || return 0
  ( cd "$app/cloud" && "$app/.venv/bin/dt-cloud" scan-run record -c "$SCAN_RUNS_PROFILE" -K "$d1_store" -k ingest -g "$gen" "$@" "$date" ) || true
}
phase=queue
phase_t0=$job_t0
# A phase just ended: its marker, then its record (extra args: `-o KEY=URI` outputs).
ended() {
  local now; now=$(date +%s)
  echo "PHASE $1: $(( now - phase_t0 ))s"
  phase_t0=$now
  scan_run -P "$1" -t "$now" "${@:2}"
}
# A failure pushes a notification (`disk-tree notify`: Pushover / a Discord webhook, from the job's
# env; a no-op without one), linking the run on the site when INGEST_SITE_URL is set.
notify_failure() {
  local link=() q=
  [[ $d1_store != primary ]] && q="?store=$d1_store"
  [[ -n ${INGEST_SITE_URL:-} && -n ${AWS_BATCH_JOB_ID:-} ]] && link=(-u "$INGEST_SITE_URL/scans/$AWS_BATCH_JOB_ID$q" -l run)
  "$app/.venv/bin/disk-tree" notify -p 1 -t "disky: $store ingest failed" ${link[@]+"${link[@]}"} \
    "scan $date failed in $phase (exit $1)" || true
}
on_exit() {
  local rc=$?
  if (( rc )); then
    scan_run -x "$rc" -F "$phase"
    notify_failure "$rc"
  else
    scan_run -x 0
  fi
}
trap on_exit EXIT
if [[ -n ${SCAN_RUNS_PROFILE:-} ]]; then
  facts=()
  [[ -n ${INGEST_MACHINE:-} ]] && facts+=(-m "$INGEST_MACHINE")
  [[ ${INGEST_SPOT:-} == 1 ]] && facts+=(--spot)
  [[ -n $region ]] && facts+=(-R "$region")
  scan_run -S -t "$(epoch "$captured")" -T 1 ${facts[@]+"${facts[@]}"}
  # The capture ran on the laptop until its marker landed; the job queued from then to its start.
  marked=$(aws s3api head-object --bucket "$bucket" --key "$key/_SUCCESS.json" --endpoint-url "$R2_ENDPOINT_URL" \
    --query LastModified --output text 2>/dev/null) || marked=
  if [[ $marked =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[T\ ]([0-9]{2}:[0-9]{2}:[0-9]{2}) ]]; then
    scan_run -P capture -t "$(epoch "${BASH_REMATCH[1]}T${BASH_REMATCH[2]}")" -o "capture=r2://$bucket/$key/"
  fi
  scan_run -P queue -t "$job_t0"
fi

phase=fetch phase_t0=$(date +%s)
echo "[ingest] capture=$key date=$date gen=$gen host=$host store=$store d1=$d1_store vcpu=$(nproc) mem=$(awk '/MemTotal/{printf "%.1f GiB", $2/1048576}' /proc/meminfo)"
mkdir -p "$work/listing" "$work/index" "$work/snap"

t0=$(date +%s)
s3 cp "s3://$bucket/$key/" "$work/listing/" --recursive --exclude '*' --include '*.parquet'
s3 cp "s3://$bucket/$key/_SUCCESS.json" "$work/capture.json"
echo "[ingest] fetched $(find "$work/listing" -name '*.parquet' | wc -l) shards, $(du -sh "$work/listing" | cut -f1) in $(( $(date +%s) - t0 ))s"
ended fetch

phase='path-index'
t0=$(date +%s)
( cd "$app/cloud" && "$time_bin" -v -o "$work/time.txt" "$app/.venv/bin/dt-cloud" path-index -g -d "$date" \
    -l "$work/listing/*.parquet" -P "$work/index/path-index.parquet" -o "$work/snap" )
ended path-index
phase='capture-meta'
"$app/.venv/bin/disk-tree" capture-meta -c "r2://$bucket/snapshots/$store/$date/meta.json" "$work/capture.json" "$work/snap/meta.json"
ended capture-meta
echo "[ingest] path-index: $(( $(date +%s) - t0 ))s, peak RSS $(awk -F': ' '/Maximum resident/{printf "%.2f GiB", $2/1048576}' "$work/time.txt")"
echo "[ingest] outputs: index $(du -sh "$work/index" | cut -f1), snap $(du -sh "$work/snap" | cut -f1)"

phase=upload
t0=$(date +%s)
s3 cp "$work/index/" "s3://$bucket/listing/$store/$date/index/$gen/" --recursive
s3 cp "$work/snap/"  "s3://$bucket/snapshots/$store/$date/" --recursive
echo "[ingest] uploaded to s3://$bucket/{listing/$store/$date/index/$gen,snapshots/$store/$date}/ in $(( $(date +%s) - t0 ))s"
ended upload -o "index=r2://$bucket/listing/$store/$date/index/$gen/" -o "snapshot=r2://$bucket/snapshots/$store/$date/"

phase='index-sync'
t0=$(date +%s)
: "${D1_DB_ID:?}" "${CLOUDFLARE_API_TOKEN:?}" "${CLOUDFLARE_ACCOUNT_ID:?}"
# `-m`: the snapshot's meta (capture-meta stamped `started` above) → the pointers' `index_schema.started`;
# `-M`: any earlier date-only scan of the store still without one takes its own meta's.
( cd "$app/cloud" && "$app/.venv/bin/dt-cloud" index-sync "$date" -g "$gen" -b "$bucket" -k "listing/$store/$date/index/$gen" \
    -s "$d1_store" -d "$work/index" -v path -v bysize -m "$work/snap/meta.json" -M "r2://$bucket/snapshots/$store" )
echo "[ingest] footers → D1 $D1_DB_NAME in $(( $(date +%s) - t0 ))s"
ended index-sync
