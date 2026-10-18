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
# run is recorded in D1 as it goes (specs/scan-runs-ui.md, specs/scan-job-wrapper.md):
# kind `ingest`, its id `$AWS_BATCH_JOB_ID` (the profile's `run_id_env`), started at
# the capture's own start, then phases capture (→ the capture's marker landing), queue
# (→ this job's start), fetch, path-index, capture-meta, upload, index-sync and
# static-names, each printing `PHASE <name>: <s>s (wall…)` (seconds from the job's
# start). The EXIT trap's `finish` records the end; a failure alerts once, through
# `disk-tree notify`'s channels. The entrypoint `dt-cloud scan-run supervise` records
# what no trap sees (a platform termination, an OOM kill). A host lost outright still
# needs a watcher outside the job.
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
# The store comes from the capture's host, as D1 `hosts` registers it (specs/hosts-registry.md;
# `dt-cloud hosts resolve`): `primary` is the deployment's own store, `snapshots/$STORE/`
# (default `laptop`) and D1 `primary`; any other store publishes under `snapshots/<store>/`
# and syncs D1 as that store. An unknown or disconnected host is refused before anything is
# recorded or published (specs/device-enrollment.md): a new Mac's capture must never land in
# the primary's scans. A D1 without the table falls back to `PRIMARY_HOST` (the primary's
# host) and `HOST_STORES` (`host=store,…`).
app=${INGEST_APP_ROOT:-/app}
host=${key#captures/}; host=${host%%/*}
store=''
d1_store=''
rc=0
resolved=$(cd "$app/cloud" && "$app/.venv/bin/dt-cloud" hosts resolve "$host" 2>&1) || rc=$?
case $rc in
  0)
    if [[ $resolved == primary ]]; then store=${STORE:-laptop}; d1_store=primary
    else store=$resolved; d1_store=$resolved; fi ;;
  3)
    echo "[ingest] unknown capture host '$host' (${resolved#hosts: }); publishing nothing" >&2
    exit 3 ;;
  4)
    if [[ -n ${PRIMARY_HOST:-} && $host == "$PRIMARY_HOST" ]]; then
      store=${STORE:-laptop}
      d1_store=primary
    fi
    for pair in ${HOST_STORES//,/ }; do
      if [[ ${pair%%=*} == "$host" ]]; then store=${pair#*=}; d1_store=$store; fi
    done
    if [[ -z $store ]]; then
      echo "[ingest] unknown capture host '$host': no D1 hosts table, neither PRIMARY_HOST (${PRIMARY_HOST:-unset}) nor in HOST_STORES (${HOST_STORES:-}); publishing nothing" >&2
      exit 3
    fi ;;
  *)
    echo "[ingest] hosts lookup failed (exit $rc): $resolved" >&2
    exit 1 ;;
esac
work=${WORK:-/work}
time_bin=${INGEST_TIME:-/usr/bin/time}
s3() { aws s3 "$@" --endpoint-url "$R2_ENDPOINT_URL" --only-show-errors; }
# The CPUs this task may use: the cgroup's quota (v2 `cpu.max`, v1 `cpu.cfs_quota_us`), rounded up. Fargate's
# `nproc` reports the host's (4 on a 1-vCPU task), and threads past the quota only add buffers and contention.
vcpus() {
  local cg=${DT_CGROUP:-/sys/fs/cgroup} q p
  if { read -r q p < "$cg/cpu.max"; } 2>/dev/null && [[ $q != max ]]; then :
  elif q=$(cat "$cg/cpu/cpu.cfs_quota_us" 2>/dev/null) && p=$(cat "$cg/cpu/cpu.cfs_period_us" 2>/dev/null) && (( q > 0 )); then :
  else nproc; return; fi
  echo $(( (q + p - 1) / p ))
}

# ── Scan-run recording (specs/scan-job-wrapper.md; a no-op without SCAN_RUNS_PROFILE) ──
# `dt-cloud scan-run supervise` (the image's entrypoint) runs this script: a platform
# termination or a kill is recorded failed and alerted even when no trap here runs.
# `epoch YYYY-MM-DDTHH:MM:SS` (UTC; GNU date in the image, BSD date elsewhere).
epoch() { date -u -d "$1Z" +%s 2>/dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%S' "$1" +%s; }
export DT_JOB_START=$job_t0
# One `dt-cloud scan-run` step; recording never fails the ingest (a fatal `phase-end` aside).
sr() {
  [[ -n ${SCAN_RUNS_PROFILE:-} ]] || return 0
  ( cd "$app/cloud" && "$app/.venv/bin/dt-cloud" scan-run "$@" ) || true
}
begin() { sr phase-begin "$1"; }
ended() { sr phase-end "$@"; }
scan_err() { SR_ERR="exit $1: $3 (${BASH_SOURCE[1]##*/}:$2)"; }
trap 'scan_err $? $LINENO "$BASH_COMMAND"' ERR
trap 'exit 143' TERM
trap 'exit 130' INT
# `finish` records the end; a failure alerts once (`disk-tree notify`'s channels, from the job's env;
# the profile's `alerts` label and link).
trap 'sr finish -x $? ${SR_ERR:+-e "$SR_ERR"}' EXIT
if [[ -n ${SCAN_RUNS_PROFILE:-} ]]; then
  facts=()
  [[ -n ${INGEST_MACHINE:-} ]] && facts+=(-m "$INGEST_MACHINE")
  [[ ${INGEST_SPOT:-} == 1 ]] && facts+=(--spot)
  [[ -n $region ]] && facts+=(-R "$region")
  # The run starts at its capture's start: the capture ran on the laptop until its marker landed,
  # then the job queued until it started.
  t_capture=$(epoch "$captured")
  sr start -k ingest -K "$d1_store" -g "$gen" -t "$t_capture" -T 1 ${facts[@]+"${facts[@]}"} "$date"
  marked=$(aws s3api head-object --bucket "$bucket" --key "$key/_SUCCESS.json" --endpoint-url "$R2_ENDPOINT_URL" \
    --query LastModified --output text 2>&1) || true
  # The image's AWS CLI v1 prints the header's RFC 1123 date (`Wed, 14 Oct 2026 22:11:39 GMT`), v2 ISO 8601:
  # GNU date reads either; BSD date (a laptop's tests) only the ISO form, through `epoch`.
  t_marked=
  if [[ -n $marked ]]; then
    t_marked=$(date -u -d "$marked" +%s 2>/dev/null) || t_marked=
    if [[ -z $t_marked && $marked =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[T\ ]([0-9]{2}:[0-9]{2}:[0-9]{2}) ]]; then
      t_marked=$(epoch "${BASH_REMATCH[1]}T${BASH_REMATCH[2]}")
    fi
  fi
  if [[ -n $t_marked ]]; then
    sr phase-begin capture -b "$t_capture"
    sr phase-end capture -t "$t_marked" -o "capture=r2://$bucket/$key/"
  else
    # Without the marker's time, capture and queue are one span: the queue phase from the capture's start.
    echo "[ingest] capture marker time unreadable: ${marked:0:300}"
    t_marked=$t_capture
  fi
  sr phase-begin queue -b "$t_marked"
  sr phase-end queue -t "$job_t0"
fi

begin fetch
echo "[ingest] capture=$key date=$date gen=$gen host=$host store=$store d1=$d1_store vcpu=$(vcpus) (nproc $(nproc)) mem=$(awk '/MemTotal/{printf "%.1f GiB", $2/1048576}' /proc/meminfo)"
mkdir -p "$work/listing" "$work/index" "$work/snap"

t0=$(date +%s)
s3 cp "s3://$bucket/$key/" "$work/listing/" --recursive --exclude '*' --include '*.parquet'
s3 cp "s3://$bucket/$key/_SUCCESS.json" "$work/capture.json"
echo "[ingest] fetched $(find "$work/listing" -name '*.parquet' | wc -l) shards, $(du -sh "$work/listing" | cut -f1) in $(( $(date +%s) - t0 ))s"
ended fetch

begin path-index
t0=$(date +%s)
( cd "$app/cloud" && "$time_bin" -v -o "$work/time.txt" "$app/.venv/bin/dt-cloud" path-index -g -d "$date" \
    -l "$work/listing/*.parquet" -P "$work/index/path-index.parquet" -o "$work/snap" )
ended path-index
begin capture-meta
"$app/.venv/bin/disk-tree" capture-meta -c "r2://$bucket/snapshots/$store/$date/meta.json" "$work/capture.json" "$work/snap/meta.json"
ended capture-meta
echo "[ingest] path-index: $(( $(date +%s) - t0 ))s, peak RSS $(awk -F': ' '/Maximum resident/{printf "%.2f GiB", $2/1048576}' "$work/time.txt")"
echo "[ingest] outputs: index $(du -sh "$work/index" | cut -f1), snap $(du -sh "$work/snap" | cut -f1)"

begin upload
t0=$(date +%s)
s3 cp "$work/index/" "s3://$bucket/listing/$store/$date/index/$gen/" --recursive
s3 cp "$work/snap/"  "s3://$bucket/snapshots/$store/$date/" --recursive
echo "[ingest] uploaded to s3://$bucket/{listing/$store/$date/index/$gen,snapshots/$store/$date}/ in $(( $(date +%s) - t0 ))s"
ended upload -o "index=r2://$bucket/listing/$store/$date/index/$gen/" -o "snapshot=r2://$bucket/snapshots/$store/$date/"

# ── The interval store (`dt-cloud interval-store`; specs/m3-interval-store.md): the primary store's scans ──
# Each scan appends as a run to the store's current generation (`interval-store/CURRENT.json`, which the
# site follows under PATH_STORE), compacting every 2^5 scans. The base generation is a build over every
# scan so far: too big for the scheduled job, so only a job submitted for it (`aws/submit -c 4 -m 16384
# -e IS_BUILD=1 <capture>`) runs one, and until then each ingest skips. Before index-sync, as the r2 demo's
# daily ingest: a listed scan is already on the store. A failed append doesn't block the publish; the run
# fails at the end.
is_rc=
if [[ $d1_store == primary ]]; then
  is_common=(-L "listing/$store/{id}/index/{gen}/path-index.parquet" -s "r2://$bucket" -o "r2://$bucket"
    -w "$work/interval-store" -p "$(vcpus)" -M "${IS_MEM:-10GB}")
  is_cmd=
  is_gen=$(cd "$app/cloud" && "$app/.venv/bin/dt-cloud" interval-store current -o "r2://$bucket") || is_rc=$?
  if [[ -n $is_rc ]]; then
    echo "[ingest] interval-store current failed (exit $is_rc); publishing the scan anyway" >&2
  elif [[ -n $is_gen ]]; then is_cmd=add
  elif [[ ${IS_BUILD:-} == 1 ]]; then is_cmd=build; is_gen=$date
  else echo "[ingest] interval-store: no generation yet; skipping (IS_BUILD=1 builds it)"; fi
  if [[ -n $is_cmd ]]; then
    begin interval-store
    t0=$(date +%s)
    is_args=("${is_common[@]}")
    [[ $is_cmd == build ]] && is_args=(-g "$is_gen" "${is_args[@]}")
    if ( cd "$app/cloud" && "$time_bin" -v -o "$work/is-time.txt" "$app/.venv/bin/dt-cloud" interval-store "$is_cmd" "${is_args[@]}" ); then
      echo "[ingest] interval-store $is_cmd (gen $is_gen) in $(( $(date +%s) - t0 ))s, peak RSS $(awk -F': ' '/Maximum resident/{printf "%.2f GiB", $2/1048576}' "$work/is-time.txt")"
      ended interval-store -o "interval-store=r2://$bucket/interval-store/"
    else
      is_rc=$?
      echo "[ingest] interval-store $is_cmd failed (exit $is_rc); publishing the scan anyway" >&2
    fi
  fi
fi

begin index-sync
t0=$(date +%s)
: "${D1_DB_ID:?}" "${CLOUDFLARE_API_TOKEN:?}" "${CLOUDFLARE_ACCOUNT_ID:?}"
# `-m`: the snapshot's meta (capture-meta stamped `started` above) → the pointers' `index_schema.started`;
# `-M`: any earlier date-only scan of the store still without one takes its own meta's.
( cd "$app/cloud" && "$app/.venv/bin/dt-cloud" index-sync "$date" -g "$gen" -b "r2://$bucket" -k "listing/$store/$date/index/$gen" \
    -s "$d1_store" -d "$work/index" -v path -v bysize -m "$work/snap/meta.json" -M "r2://$bucket/snapshots/$store" )
echo "[ingest] footers → D1 $D1_DB_NAME in $(( $(date +%s) - t0 ))s"
ended index-sync

# ── The static name index (exact `?f=`; `dt-cloud static-names`): the primary store's scans ──
# STATIC_GEN is the site's (`site/wrangler.toml`; the job env carries the same value, which
# `tests/test_capture_ingest.py` pins). `add` appends this scan (and any other the generation
# lacks) as a run; the first run of a generation `build`s its base over every scan so far.
if [[ -n ${STATIC_GEN:-} && $d1_store == primary ]]; then
  t0=$(date +%s)
  cmd=add
  if ! aws s3api head-object --bucket "$bucket" --key "static-names/$STATIC_GEN/scans.json" --endpoint-url "$R2_ENDPOINT_URL" > /dev/null 2>&1; then
    cmd=build
  fi
  hex=()
  [[ $cmd == build ]] && hex=(-x off)
  if [[ $cmd == build && ${STATIC_BUILD:-} != 1 ]]; then
    # A base build over every scan needs a bigger box than the scheduled job's: only a job submitted
    # for it (`aws/submit -c 4 -m 16384 -e STATIC_BUILD=1 …`) runs one; until then each ingest skips.
    echo "[ingest] static-names: generation $STATIC_GEN not built yet; skipping (STATIC_BUILD=1 builds it)"
  else
    begin static-names
    ( cd "$app/cloud" && "$time_bin" -v -o "$work/static-time.txt" "$app/.venv/bin/dt-cloud" static-names "$cmd" -g "$STATIC_GEN" -L "listing/$store/{id}/index/{gen}/path-index.parquet" \
        -s "r2://$bucket" -o "r2://$bucket" -w "$work/static-names" -p "$(vcpus)" -M "${STATIC_MEM:-5GB}" ${hex[@]+"${hex[@]}"} )
    echo "[ingest] static-names $cmd -g $STATIC_GEN in $(( $(date +%s) - t0 ))s, peak RSS $(awk -F': ' '/Maximum resident/{printf "%.2f GiB", $2/1048576}' "$work/static-time.txt")"
    ended static-names -o "static-names=r2://$bucket/static-names/$STATIC_GEN/"
  fi
fi

if [[ -n $is_rc ]]; then
  SR_ERR="exit $is_rc: interval-store ${is_cmd:-current} (the scan is published; its store append is not)"
  exit "$is_rc"
fi
