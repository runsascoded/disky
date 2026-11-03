#!/usr/bin/env bash
# Daily snapshot job (GCP Batch). Chain:
#   1. DIY fan-out: list all 6 marin-* buckets ourselves (one Batch task per
#      bucket) → canonical listing parquet under listing/<date>/<bucket>/
#   2. path-index: aggregate the listings (+ attribution) into the path store
#      (every object + dir row, two sorts: `path` and `bysize`) and the
#      age/meta JSONs
#   3. publish snapshot JSONs to the data bucket (canonical store)
#
# The live site reads snapshots straight from the bucket (see
# site/functions/data/[[path]].ts), so the job no longer builds or deploys a
# site data dir — publishing to the bucket is the whole "publish" step.
#
# Buckets are mounted via GCS FUSE at /gcs/<bucket> (DuckDB needs local paths).
# Env: SNAP_ID (scan id; default SNAPSHOT_DATE, else today UTC), DATA_BUCKET, DUCKDB_MEM,
# SNAP_PATH (default snapshots/<date>).
# Parallel/experimental runs: override SNAP_PATH so they write to their own
# bucket path (the live site only reads snapshots/<date>/).
set -euxo pipefail

# Secrets arrive from Secret Manager verbatim, and a version created with
# `echo … | --data-file=-` carries a trailing newline (it broke the Discord
# digest for two days, 2026-09-16/17). Strip surrounding whitespace off every
# secret env var before anything (boto, curl, wrangler, the CLI) reads it.
# xtrace off for the block: `${!n}` would print the values.
{ set +x; } 2>/dev/null
for n in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY CLOUDFLARE_API_TOKEN DISCORD_BOT_TOKEN \
         DISCORD_GCS_USAGE_WEBHOOK GCS_USAGE_TOKEN SLACK_BOT_TOKEN SLACK_WEBHOOK \
         CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
set -x

# The image's source revision (`job/build.sh`'s stamp; `unstamped` for an older build), first in every run's log.
echo "run.sh: image $(dt-cloud image-sha)" >&2

# The scan id keys everything this run writes (listing/, snapshots/, the index
# generation, D1): `YYYY-MM-DDTHHMM` by default, so a later cron stays after an
# earlier manual scan in the append stores' ID order. Explicit SNAP_ID and
# legacy SNAPSHOT_DATE overrides remain supported. DATE is its UTC day, for
# what is genuinely per-day (path-index `--asof`: the access-log cutoff).
SNAP_ID=${SNAP_ID:-${SNAPSHOT_DATE:-$(date -u +%FT%H%M)}}
export SCAN_ID=$SNAP_ID
# Manual submits pin this before allocation; static cron bodies learn the
# root identity here. Never replace it with a downstream job's own UID.
export SCAN_RUN_ID=${SCAN_RUN_ID:-${BATCH_JOB_UID:-}}
DATE=${SNAP_ID%%T*}
DATA=${DATA_BUCKET:-oa-gcs-usage-dvx}
# Deployment config (specs/oa-decoupling.md): `dt-cloud` carries no defaults for these.
export DATA_BUCKET=$DATA
export SITE_URL=${SITE_URL:-https://gcs.oa.dev}
export GCP_PROJECT=${GCP_PROJECT:-oa-internal-450019}
export JOB_SA=${JOB_SA:-gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com}
export JOB_IMAGE=${JOB_IMAGE:-us-central1-docker.pkg.dev/oa-internal-450019/cloud-run-source-deploy/gcs-usage-snapshot:latest}
# Buckets listed from a VM in their own region (cross-ocean list pages pay full RTT).
export LISTING_REGIONS=${LISTING_REGIONS:-'{"marin-eu-west4":"europe-west4"}'}
export ACCESS_LOG_BUCKET=${ACCESS_LOG_BUCKET:-marin-usage-logs}
export ACCESS_BUCKETS=${ACCESS_BUCKETS:-"marin-us-central1 marin-us-central2 marin-us-east1 marin-us-east5 marin-eu-west4 marin-us-west1 marin-us-west4"}
export WARM_PATHS=${WARM_PATHS:-",marin-us-central2,marin-us-east5,marin-us-central1,marin-eu-west4,marin-us-west4,marin-us-east1"}
# The scanned fleet (`submit-listing -b`, one per bucket).
FLEET=(marin-us-central2 marin-eu-west4 marin-us-central1 marin-us-east5 marin-us-east1 marin-us-west4)
# Production buckets the scratch delete benchmarks refuse to target (`sweep_delete_benchmark`).
export PROTECTED_BUCKETS=${PROTECTED_BUCKETS-marin-*}
# This deployment's D1 (site/wrangler.toml `oa-gcs-usage-auth`): index-sync,
# index-gc and index-dir name it explicitly — the base CLI has no default D1
# (a57f615), so a misconfigured job can't write footers into another deploy's.
export D1_DB_ID=${D1_DB_ID:-e52398b7-5538-4bc4-83db-3355a1b5ef9a}
export D1_DB_NAME=${D1_DB_NAME:-oa-gcs-usage-auth}
# The attribution roster lives with the data, not in the repo (`-i` /
# `$DT_CLOUD_IDENTITIES`; no bundled default). Read through the job's GCS mount.
export DT_CLOUD_IDENTITIES=${DT_CLOUD_IDENTITIES:-/gcs/$DATA/config/identities.yaml}
SNAP_PATH=${SNAP_PATH:-snapshots/$SNAP_ID}
# INDEX_PATH: where the floor-free path index (+ its by-user/by-team variants)
# lands; default is colocated with the listing. SCRATCH=1 marks a verification
# run: publish to SNAP_PATH/INDEX_PATH only, and skip every step that touches
# live state (D1 index-sync, healthcheck, series.json, diff, digest) — so a
# pipeline suffix can be re-run for an old date into a scratch location and
# compared against what was published, without disturbing it.
# GEN: this run's index generation. Its tiers live under
# listing/$SNAP_ID/index/$GEN/ and D1 points at that dir once the footers are
# synced — a run never overwrites a parquet the site is reading
# (specs/view-serving.md, "Index rewrite vs D1 footer").
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}
INDEX_DIR=listing/$SNAP_ID/index/$GEN
INDEX_PATH=${INDEX_PATH:-$INDEX_DIR/path-index.parquet}
if [ "${SPLIT_COMPUTE:-0}" = "1" ]; then
  [[ "$JOB_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "ERROR: split scan requires a digest-pinned JOB_IMAGE" >&2; exit 1; }
  [ -n "${BATCH_JOB_UID:-}" ] || { echo "ERROR: split scan needs a Batch controller UID" >&2; exit 1; }
  for mode in GATE SWEEP SWEEP_BENCHMARK_PLAN ACCESS_ONLY SCAN_COMPUTE_ONLY NOP_IF_PUBLISHED; do
    [ -z "${!mode:-}" ] || [ "${!mode}" = "0" ] || { echo "ERROR: split scan does not support $mode" >&2; exit 1; }
  done
  if [ "${SCRATCH:-0}" = "1" ]; then
    [[ "$SNAP_PATH" == scratch/* && "$INDEX_PATH" == scratch/* ]] || { echo "ERROR: split SCRATCH requires SNAP_PATH and INDEX_PATH under scratch/" >&2; exit 1; }
  fi
fi

# Scan-run record and alerts (specs/scan-runs-ui.md, specs/scan-job-wrapper.md):
# `dt-cloud scan-run start / phase-begin / phase-end / phase -- CMD / finish`
# print the PHASE markers (the backfill parses them from the logs; N is seconds
# since `$DT_JOB_START`), record the run's phases, outputs and exit in D1
# (`scan_runs`, shown at /scans), and post failure alerts through `disk-tree
# notify`'s slack channel (SLACK_BOT_TOKEN + SLACK_ALERT_CHANNEL, the
# #gcs-admin channel, falling back to the digest's SLACK_CHANNEL). They
# never fail the job for their own reasons, take no secret on argv, and redact
# the profile's `secrets` from what they post or record. The profile
# (`job/scan-runs.json`) says what each phase wrote, the alert label and the
# logs link. A SCRATCH / GATE run is not a scan run: nothing is recorded and
# alerts print instead of posting. A fatal `phase-end -x RC` alerts and exits
# RC; `-W` warns and continues.
export DT_JOB_START=${DT_JOB_START:-$(date +%s)}
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-job/scan-runs.json}
export SLACK_ALERT_CHANNEL=${SLACK_ALERT_CHANNEL:-${SLACK_CHANNEL:-}}
if [ "${SCRATCH:-0}" = "1" ] || [ "${GATE:-0}" = "1" ]; then export DT_RECORD=off DT_ALERTS=stderr; fi
SR_KIND=scan; [ "${REPROC:-0}" = "1" ] && SR_KIND=reproc
[ "${SCAN_COMPUTE_ONLY:-0}" = "1" ] && SR_KIND=compute
sr() { dt-cloud scan-run "$@"; }
wall() { echo "$(( $(date +%s) - DT_JOB_START ))s"; }

# Any command dying under `set -e` ends the run through the EXIT trap, whose
# `finish` posts the failure alert (unless a fatal phase already did) with this
# reason, also recorded as `scan_runs.error` — otherwise silence is the only
# failure signal (both 2026-08-28 incidents went unnoticed). The backfill finds
# the reason in the logs by its trace line (`error_marker`).
SR_ERR=""
scan_err() { SR_ERR="exit $1: $3 (run.sh:$2)"; }
trap 'scan_err $? $LINENO "$BASH_COMMAND"' ERR
# A platform stop (Batch preemption / cancel) exits now, not after the foreground
# child: `scan-run supervise` (the image's entrypoint) forwards it to the group and
# records the run failed (specs/scan-job-wrapper.md, the entrypoint).
trap 'exit 143' TERM
trap 'exit 130' INT

cd /app

# Access-log ingest (incremental, watermarked — specs/access-logs-and-cost.md
# § Productionize). Runs on every scheduled attempt, including ones where the
# snapshot below NOPs, so read-recency ("atime") stays ~6h fresh. A failure
# never blocks the snapshot (it self-heals next attempt via the watermark).
# ACCESS_ONLY=1 = ingest-only job (e.g. the backlog backfill); SKIP_ACCESS=1
# opts out entirely (e.g. REPROC runs that only re-aggregate).
# Default the ingest's DuckDB cap here (not just batch-submit.sh) so runs
# launched from a stale scheduler template don't fall back to the laptop-sized
# 8GB CLI default — that OOM'd the 2026-08-23 daily run's parquet write.
# 48GB is safe: ingest still completes before the webdata step (the barrier
# below), and it overlaps only the listing fan-out — which runs on a *separate*
# node, leaving the orchestrator's 128G free for ingest. A row-heavy chunk blew
# the earlier 24GB cap.
export DUCKDB_MEM_ACCESS=${DUCKDB_MEM_ACCESS:-48GB}
# A short, read-only sweep re-list benchmark. This runs in the same image,
# identity, region, and machine family as the executor, but neither builds a
# new manifest nor writes to a target bucket. The result JSON defaults beside
# the source plan so it survives the Batch VM.
if [ -n "${SWEEP_BENCHMARK_PLAN:-}" ]; then
  [ -n "${SWEEP_BENCHMARK_BUCKET:-}" ] || { echo "ERROR: SWEEP_BENCHMARK_PLAN needs SWEEP_BENCHMARK_BUCKET" >&2; exit 1; }
  benchmark_args=(-b "$SWEEP_BENCHMARK_BUCKET")
  for w in ${SWEEP_BENCHMARK_WORKERS:-8 16 32 64}; do benchmark_args+=(-j "$w"); done
  benchmark_args+=(-n "${SWEEP_BENCHMARK_ROOTS:-128}")
  benchmark_args+=(-r "${SWEEP_BENCHMARK_RESULTS_PER_ROOT:-50000}")
  benchmark_out=${SWEEP_BENCHMARK_OUT:-${SWEEP_BENCHMARK_PLAN%/}/benchmark-$SWEEP_BENCHMARK_BUCKET-$(date -u +%Y%m%dT%H%M%SZ).json}
  dt-cloud sweep benchmark "${benchmark_args[@]}" -o "$benchmark_out" "$SWEEP_BENCHMARK_PLAN"
  echo "SWEEP-BENCHMARK-DONE $benchmark_out"
  exit 0
fi
# ACCESS_ONLY (backlog backfill): serial ingest, then exit — no snapshot.
ACCESS_WARN="the snapshot continues; read recency lags until the next run's ingest (watermarked, it self-heals)"
if [ "${ACCESS_ONLY:-0}" = "1" ]; then
  if [ -n "${SCAN_ROOT_UID:-}" ]; then
    trap 'sr finish -x $? ${SR_ERR:+-e "$SR_ERR"}' EXIT
    sr start -k access -B -g "$GEN" -p "$SCAN_ROOT_UID" "$SNAP_ID"
    # Never fatal (the snapshot continues without it): a failure warns, and this
    # downstream run ends clean.
    sr phase access-ingest -W -m "$ACCESS_WARN" -- dt-cloud access ingest ${ACCESS_ARGS:-}
  else
    dt-cloud access ingest ${ACCESS_ARGS:-}
  fi
  echo "ACCESS-JOB-DONE"
  exit 0
fi

# SWEEP=dry|real + SWEEP_PLAN=<plan.json path or gs:// URL> (+ SWEEP_BUCKETS=
# "marin-us-east1 …", SWEEP_DATE=<scan>): the sweep executor as a Batch job
# launched from the CLI — the same two steps /staged's dispatch bridge runs
# (functions/api/sweep/dispatch.ts, which overrides the entrypoint instead of
# using this branch): build the object-level manifest from the plan's items
# (`--plan`; the keep/sweep sign-off mode was removed with marks, 2026-09-28),
# then execute it — dry writes would-delete/ logs; real deletes
# (generation-matched, ≥7d soft delete required). Both record the run in D1
# (`deletion_runs`). Size it as n2-standard-8 (MACHINE/MEMORY_MIB on
# batch-submit); tokens ride in env and stay out of xtrace.
if [ -n "${SWEEP:-}" ]; then
  [ "$SWEEP" = dry ] || [ "$SWEEP" = real ] || { echo "ERROR: SWEEP must be dry|real" >&2; exit 1; }
  [ -n "${SWEEP_PLAN:-}" ] || { echo "ERROR: SWEEP needs SWEEP_PLAN (a plan.json; /staged's dispatch writes one per run)" >&2; exit 1; }
  sd=${SWEEP_DATE:-$SNAP_ID}
  plan="gs://$DATA/sweep/runs/gcs-sweep-$SWEEP-$(date -u +%Y%m%d-%H%M%S)z"
  bflags=()
  for b in ${SWEEP_BUCKETS:-}; do bflags+=(-b "$b"); done
  { set +x; } 2>/dev/null
  dt-cloud sweep manifest -d "$sd" -p "$SWEEP_PLAN" "${bflags[@]}" -o "$plan"
  if [ "$SWEEP" = real ]; then
    dt-cloud sweep execute "${bflags[@]}" --for-real "$plan"
  else
    dt-cloud sweep execute "${bflags[@]}" "$plan"
  fi
  echo "SWEEP-JOB-DONE $SWEEP $plan"
  exit 0
fi

# From here on this job is a scan run (the branches above exit as other jobs):
# record its start (with its Batch facts: machine, SPOT, cost) and its exit.
trap 'sr finish -x $? ${SR_ERR:+-e "$SR_ERR"}' EXIT
sr start -k "$SR_KIND" -B -g "$GEN" ${SCAN_ROOT_UID:+-p "$SCAN_ROOT_UID"} "$SNAP_ID"

# Scheduled retry attempts set NOP_IF_PUBLISHED=1: exit quietly when the
# snapshot already exists (an earlier attempt won). Manual runs leave it
# unset so intentional re-runs always proceed. A NOP retry still ingests access
# logs (keeps read-recency ~6h fresh) — serially, since no listing follows it.
if [ "${NOP_IF_PUBLISHED:-0}" = "1" ] && [ -f "/gcs/$DATA/$SNAP_PATH/meta.json" ]; then
  [ "${SKIP_ACCESS:-0}" = "1" ] || dt-cloud access ingest ${ACCESS_ARGS:-} \
    || echo "WARN: access ingest failed (watermark self-heals next run)" >&2
  echo "SNAPSHOT-JOB-NOP $SNAP_ID (already published)"
  sr nop  # the EXIT trap's `finish -x 0` keeps it a NOP
  exit 0
fi

# Real snapshot run: kick access ingest off in the BACKGROUND so it overlaps the
# listing fan-out below — both are ~40 min and independent (only `stage` reads
# the ingest's access/agg shards, gated on the `wait` barrier before staging).
# Was serial (ingest THEN listing), putting ~37 min squarely on the critical
# path; now it hides under the listing's wall time. Non-fatal: a failure is
# caught at the barrier and the watermark self-heals next run.
ACCESS_PID=""
sr phase-begin listing-fanout
if [ "${SKIP_ACCESS:-0}" != "1" ] && [ "${REPROC:-0}" != "1" ]; then
  sr phase-begin access-ingest
  if [ "${SPLIT_COMPUTE:-0}" = "1" ]; then
    dt-cloud scan-run task -s access "$SNAP_ID" & ACCESS_PID=$!
  else
    dt-cloud access ingest ${ACCESS_ARGS:-} & ACCESS_PID=$!
  fi
fi

# 1. List every bucket ourselves via a fan-out Batch job (one task per bucket).
# We own the listing end-to-end — no dependency on SII inventory reports, which
# generated at scattered times (02:26-13:00 UTC), lagged ~30h on day-1, and
# didn't exist at all for us-central2. Tasks reuse completed listings, so
# re-runs only fill gaps. If the fan-out fails or any bucket lacks a completed
# listing, abort — better no snapshot than a silently incomplete one (a dropped
# bucket like central2 is a huge hole).
FLEET=(marin-us-central2 marin-eu-west4 marin-us-central1 marin-us-east5 marin-us-east1 marin-us-west4)
# Listing node size/parallelism are runtime config: set LISTING_MACHINE /
# LISTING_PROCS / LISTING_WORKERS on the snapshot job's env (batch-submit.sh or
# the scheduler body) to resize without rebuilding — unset falls back to the CLI
# defaults. computeResource is derived from the machine, so any size fits.
# REPROC=1 re-aggregates an existing date from its already-archived listing/
# (e.g. to re-apply an identities.yaml change): skip the fan-out entirely and
# just verify the listings are present. The digest is also suppressed below.
if [ "${REPROC:-0}" != "1" ]; then
  LZ=()
  [ -n "${LISTING_MACHINE:-}" ] && LZ+=(-m "$LISTING_MACHINE")
  [ -n "${LISTING_PROCS:-}" ] && LZ+=(-P "$LISTING_PROCS")
  [ -n "${LISTING_WORKERS:-}" ] && LZ+=(-w "$LISTING_WORKERS")
  for b in "${FLEET[@]}"; do LZ+=(-b "$b"); done
  # central2's chunk weights may also come from the pre-DIY listing layout (opt-in since `cloud` dropped the default).
  LZ+=(-L marin-us-central2=central2-listing)
  dt-cloud job submit-listing -d "$SNAP_ID" -W "${LZ[@]}"
fi
sr phase-end listing-fanout

# Barrier: the concurrent access ingest must finish before we stage (stage reads
# its access/agg shards) and before HAVE_ACCESS is computed just below. Non-fatal.
ACCESS_RC=0
if [ -n "$ACCESS_PID" ]; then
  wait "$ACCESS_PID" || ACCESS_RC=$?
fi
sr phase-end access-ingest -x "$ACCESS_RC" -W -m "$ACCESS_WARN" -q "overlapped the listing"
if [ "${SPLIT_COMPUTE:-0}" = "1" ]; then
  sr phase-begin scan-compute
  GEN="$GEN" SNAP_PATH="$SNAP_PATH" INDEX_PATH="$INDEX_PATH" dt-cloud scan-run task -s compute "$SNAP_ID"
  sr phase-end scan-compute
  if [ "${SCRATCH:-0}" = "1" ]; then
    echo "SCRATCH — split computation complete; no serving pointers changed" >&2
    exit 0
  fi
else
sr phase-begin path-index
G=()
for b in "${FLEET[@]}"; do
  if [ -f "/gcs/$DATA/listing/$SNAP_ID/$b/_SUCCESS.json" ]; then G+=("/gcs/$DATA/listing/$SNAP_ID/$b/*.parquet")
  else echo "ERROR: no completed $SNAP_ID listing for $b — aborting snapshot" >&2; exit 1; fi
done
AG=("/gcs/$DATA/attr/attribution-2026-07-20.parquet" "/gcs/$DATA/attr/attribution-wandb.parquet")
# Access-log layer-2a shards (read-recency join) — optional until the first
# ingest lands them; webdata runs without -x when none exist yet.
XG="/gcs/$DATA/access/agg/*/*.parquet"
HAVE_ACCESS=0
compgen -G "$XG" > /dev/null && HAVE_ACCESS=1

# With STAGE_DIR set (a local-SSD mount on Batch), copy all inputs there once
# and point webdata at the local copies — gcsfuse reads are ~20-50 MB/s and
# webdata makes several passes, so local NVMe scans win by an order of magnitude.
# DuckDB spill goes to the same disk.
# Each listing's `_SUCCESS.json` goes too: path-index reads the scan's start
# (`meta.started`) from the markers beside its `-l` globs.
if [ -n "${STAGE_DIR:-}" ]; then
  SG=("${G[@]}" "${G[@]/%\*.parquet/_SUCCESS.json}" "${AG[@]}")
  [ "$HAVE_ACCESS" = "1" ] && SG+=("$XG")
  dt-cloud stage -o "$STAGE_DIR" "${SG[@]}"
  export DUCKDB_TMP=${DUCKDB_TMP:-$STAGE_DIR/.duckdb-tmp}
  mkdir -p "$DUCKDB_TMP"
fi
loc() { if [ -n "${STAGE_DIR:-}" ]; then echo "$STAGE_DIR/${1#/gcs/}"; else echo "$1"; fi; }
L=(); for g in "${G[@]}"; do L+=(-l "$(loc "$g")"); done
A=(); for a in "${AG[@]}"; do A+=(-a "$(loc "$a")"); done
X=(); [ "$HAVE_ACCESS" = "1" ] && X+=(-x "$(loc "$XG")")

# GATE=1 (with REPROC=1: the date's listing is archived): the A.3 gate of
# specs/done/cascade-gate.md instead of the snapshot — DT's `import -e duckdb
# --label usr` per bucket on the same staged inputs, `cascade-a2a` against the
# date's published path index, peak RSS + wall per bucket. Reports go to
# gs://$DATA/gate/$SNAP_ID/; nothing is published. GATE_K = --partition-depth
# (default 2), GATE_P = --partition-files (default 4M; keys over it split
# recursively), GATE_THREADS = DuckDB threads (default 8), GATE_HIST=1 adds
# --size-hist.
if [ "${GATE:-0}" = "1" ]; then
  GD="${STAGE_DIR:-/tmp}/gate"
  mkdir -p "$GD/labels" "$GD/tiers" "$GD/l2" "$GD/db" "$GD/root"
  export DISK_TREE_ROOT="$GD/root"
  dt-cloud labels "${L[@]}" "${A[@]}" -o "$GD/labels"
  srckey=$(dt-cloud index-dir "$SNAP_ID") || { echo "ERROR: no synced path index for $SNAP_ID to compare against" >&2; exit 1; }
  for b in "${FLEET[@]}"; do
    echo "GATE $b: import (k=${GATE_K:-2}, P=${GATE_P:-4000000}, n=${GATE_THREADS:-8}, mem=${DUCKDB_MEM:-100GB})" >&2
    /usr/bin/time -v disk-tree import -e duckdb -l "$(loc "/gcs/$DATA/listing/$SNAP_ID/$b/*.parquet")" -b "$b" -s gcs \
      -d "$GD/db" -k "${GATE_K:-2}" -P "${GATE_P:-4000000}" -n "${GATE_THREADS:-8}" -L "$GD/labels/labels-$b.parquet" -c usr -p storage_class_id -m ${GATE_HIST:+-H} \
      -i dirs -O "$GD/tiers" -r 8192 -S usr -M "${DUCKDB_MEM:-100GB}" -T "${DUCKDB_TMP:-/tmp}" \
      -t "${DATE}T00:00:00Z" -o "$GD/l2" > "$GD/import-$b.log" 2>&1 || echo "GATE $b: import FAILED (see import-$b.log)" >&2
    grep -E "partition depth|Elapsed|Maximum resident" "$GD/import-$b.log" >&2 || true
    dt-cloud cascade-a2a -b "$b" -i "/gcs/$DATA/$srckey/path-index.parquet" "$GD/tiers/gcs-$b.dirs.parquet" > "$GD/a2a-$b.txt" 2>&1 \
      && echo "GATE $b: a2a exact" >&2 || echo "GATE $b: a2a DIFFERENT (see a2a-$b.txt)" >&2
    rm -rf "$GD/db"/* "$GD/l2"/*
  done
  mkdir -p "/gcs/$DATA/gate/$SNAP_ID"
  cp "$GD"/import-*.log "$GD"/a2a-*.txt "/gcs/$DATA/gate/$SNAP_ID/"
  echo "PHASE gate: $(wall) (wall); reports at gs://$DATA/gate/$SNAP_ID/" >&2
  exit 0
fi

# Layer-2 dir-cache: attribution-independent per-dir rollups, written on the
# first aggregation of a date and reused by any re-attribution run (REPROC,
# ledger refreshes) — those then skip the 595M-row object scans entirely.
# Colocated with the listing (immutable per date, same lifecycle).
#
# The path store (specs/path-store.md): its union and both sorts are written
# beside -P and re-read once per sort, so they go to the local SSD, not the
# GCS FUSE mount (~20-50 MB/s; the phase-0 sizing runs, 2026-09-30), and the
# finished files are uploaded in one parallel pass. Row groups stay at the
# 8k default: a small drill decodes ~one group per subtree depth, so 32k
# groups made every drill 4× dearer and hit the reader's decode cap (413s on
# 2026-10-01); `PATH_INDEX_RG_ROWS` overrides. `-u bysize`: one user-first
# copy, `bysize-user`, which a lens view (a user's estate) reads so a user's
# root decodes only their own groups; `path` stays mixed-user (a lens below
# the root filters it per row). `PATH_INDEX_USER_SORT_TIERS` overrides.
PI_DIR="${STAGE_DIR:-/tmp}/path-index"
mkdir -p "$PI_DIR"
dt-cloud path-index -d "$DATE" "${L[@]}" "${A[@]}" "${X[@]}" -o "/tmp/snap/$SNAP_ID" \
  -c "/gcs/$DATA/listing/$SNAP_ID/dir-cache" \
  -P "$PI_DIR/path-index.parquet" ${PATH_INDEX_RG_ROWS:+-r "$PATH_INDEX_RG_ROWS"} -u "${PATH_INDEX_USER_SORT_TIERS:-bysize}" \
  ${PATH_INDEX_SEARCH:+-S}  # PATH_INDEX_SEARCH=1: the filter's search sidecars too (specs/path-store-search.md; off until measured)
sr phase-end path-index
sr phase-begin "path-index upload"
ls -l "$PI_DIR" >&2
PI_DIR="$PI_DIR" DATA="$DATA" DEST="$(dirname "$INDEX_PATH")" python3 - <<'PY'
import os, time
from pathlib import Path
from google.cloud import storage
from google.cloud.storage import transfer_manager as tm
src, dest = Path(os.environ["PI_DIR"]), os.environ["DEST"]
b = storage.Client().bucket(os.environ["DATA"])
t0, tot = time.time(), 0
for f in sorted(p for p in src.iterdir() if p.is_file() and not p.name.startswith(".")):
    blob = b.blob(f"{dest}/{f.name}")
    if f.stat().st_size > 256 << 20:
        tm.upload_chunks_concurrently(str(f), blob, chunk_size=64 << 20, max_workers=16)
    else:
        blob.upload_from_filename(str(f))
    tot += f.stat().st_size
print(f"path-index upload: {tot / 1e9:.1f} GB in {time.time() - t0:.0f}s → gs://{os.environ['DATA']}/{dest}/", flush=True)
PY
sr phase-end "path-index upload"
sr phase-begin webdata+stage
dt-cloud rules -o /tmp/rules.json || true  # findings shouldn't block the snapshot
sr phase-end webdata+stage
sr phase-begin publish

# 3. publish to the canonical store — the live site reads these directly
# (site/functions/data/[[path]].ts), so no site rebuild/deploy is needed.
mkdir -p "/gcs/$DATA/$SNAP_PATH"
cp "/tmp/snap/$SNAP_ID"/*.json "/gcs/$DATA/$SNAP_PATH/"
# The fleet's lifecycle rules in force at this scan (Marin's tmp/ttl=<N>d TTLs
# on every bucket): snapshotted next to the scan, keyed by bucket, so the site
# can show them and diff them scan to scan. `job/lifecycle/<bucket>.json` is
# the tracked copy (`dt-cloud lifecycle diff`); a pull never fails the scan.
LC=(); for b in "${FLEET[@]}"; do LC+=(-b "gs://$b"); done
dt-cloud lifecycle pull -k "${LC[@]}" -o "/gcs/$DATA/$SNAP_PATH/lifecycle.json" || echo "WARN: lifecycle pull failed" >&2
if [ "${SCRATCH:-0}" = "1" ]; then
  echo "SCRATCH — published to $SNAP_PATH + $INDEX_PATH; skipping index-sync/healthcheck/series/diff/digest" >&2
  echo "PHASE total: $(wall) (wall)" >&2
  exit 0
fi
# attribution rules: the single latest copy the /data/rules.json function serves
cp /tmp/rules.json "/gcs/$DATA/snapshots/rules.json" 2>/dev/null || true
sr phase-end publish
sr phase-begin publish-r2

# Serving off GCS (the site's `SERVED_R2`): the small files its pages read beside
# the path store — this scan's snapshot JSONs and its generation's extras
# (`attr.tsv` + index) — and the rules registry, copied to the R2 index bucket
# before index-sync lists the scan. The key is the static index's
# (`gcs-static-index-r2-*`, Batch secretVariables). This is a visibility
# barrier: missing credentials, source files or verified copies fail the job
# before any D1 pointer can move. The previous scan remains visible.
{ set +x; } 2>/dev/null
if [ -n "${R2_ACCESS_KEY_ID:+set}" ] && [ -n "${R2_SECRET_ACCESS_KEY:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  r2pub() {
    R2_ENDPOINT=${R2_ENDPOINT:-https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com} R2_BUCKET=oa-gcs-usage-index \
      LAYER2_PREFIX='listing/{scan}/index/' dt-cloud publish-r2 "$@"
  }
  if r2pub -S -a \
      -R "$SNAP_PATH/meta.json" -R "$SNAP_PATH/age.json" \
      -R "$INDEX_DIR/attr.tsv" -R "$INDEX_DIR/attr.tsv.idx.json" "$SNAP_ID" \
      && r2pub -a -p snapshots/rules.json -R snapshots/rules.json "$SNAP_ID"; then
    set -x; sr phase-end publish-r2
  else
    rc=$?; set -x
    sr phase-end publish-r2 -x "$rc" -m "D1 pointers were not advanced, so the previous scan remains visible. Repair the R2 copy before resuming index-sync." || exit $?
  fi
else
  set -x
  sr phase-end publish-r2 -x 1 -q "missing credentials" -m "no R2 credentials: publication blocked; D1 pointers were not advanced, so the previous scan remains visible." || exit $?
fi

if [ "${SCAN_COMPUTE_ONLY:-0}" = "1" ]; then
  echo "SCAN-COMPUTE-DONE $SNAP_ID" >&2
  exit 0
fi
fi  # inline computation or dedicated compute worker

# Static name index (specs/static-append.md): append this scan to the search
# index on R2 — every per-scan stage, the heavy-literal drill included — before
# index-sync lists the scan, so it appears already searchable. `-c` first
# appends any earlier published scan still pending, in scan-id order (a
# missed day catches up). Bounded and never fatal: past STATIC_NAMES_TIMEOUT or
# on a failure the scan still publishes (browse and diff work; search says "not
# available for this scan yet") and the alert says how to resume — every stage
# skips what's done, so a rerun picks up where this one stopped.
static_names_step() {
  if [ "${SKIP_STATIC_NAMES:-0}" != "1" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
    # The append's Batch tasks run this job's own image (`JOB_IMAGE`), not the profile's pinned default — on
    # 2026-10-10 they ran that stale digest and every task failed (`No module named dt_cloud.static_append`).
    sr phase static-names -W -T "${STATIC_NAMES_TIMEOUT:-120m}" \
      -m 'the scan publishes; search says "not available" for it' \
      -R "STATIC_NAMES_PROFILE=job/static-names.json STATIC_NAMES_IMAGE=$JOB_IMAGE dt-cloud static-names runs add -c $SNAP_ID" \
      -- env R2_ENDPOINT="${R2_ENDPOINT:-https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com}" STATIC_NAMES_PROFILE=/app/job/static-names.json \
      STATIC_NAMES_IMAGE="$JOB_IMAGE" dt-cloud static-names runs add -c "$SNAP_ID"
  fi
}

# Interval store (specs/interval-store.md §2.7): append this scan to the change-
# interval path store as a run beside its base generation — per key range on
# Batch, the run's served sorts, the binary counter's merges, R2, then the
# immutable `manifests/<scan>.json`. `-c` first appends any earlier published
# scan still pending, in scan-id order. On by default; INTERVAL_STORE_APPEND=0 opts out (the
# site reads the store only under `PATH_STORE=opt-in` + `ps=iv` until the read
# switch). Bounded and never fatal: past INTERVAL_STORE_TIMEOUT or on a failure
# the scan still publishes and reads per-scan; every stage skips what's done,
# so a rerun resumes. Its Batch tasks run this job's own image (the profile
# pins an older one, without `interval_append`).
interval_store_step() {
  if [ "${INTERVAL_STORE_APPEND:-1}" = "1" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
    sr phase interval-store -W -T "${INTERVAL_STORE_TIMEOUT:-60m}" \
      -m "the scan publishes and reads per-scan" \
      -R "INTERVAL_STORE_PROFILE=job/interval-store.json INTERVAL_STORE_IMAGE=$JOB_IMAGE dt-cloud interval-store append -c $SNAP_ID" \
      -- env R2_ENDPOINT="${R2_ENDPOINT:-https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com}" INTERVAL_STORE_PROFILE=/app/job/interval-store.json \
      INTERVAL_STORE_IMAGE="${INTERVAL_STORE_IMAGE:-$JOB_IMAGE}" DISKY_LABELS="${DISKY_LABELS:-app=disky,deployment=gcs}" \
      dt-cloud interval-store append -c -w "${INTERVAL_STORE_MERGE_WAIT:-1800}" "$SNAP_ID"
  fi
}

# The two appends are independent (each reads the published path index and
# writes its own R2 store; their work runs as separate Batch jobs), so they run
# side by side: the interval store's ~17 min hides under the name index's
# ~50. Both finish before index-sync lists the scan. Each is never fatal on
# its own (`phase -W` records a failure and warns); `job/scan-runs.json` names
# them `concurrent`, so each phase starts where `publish` ended.
# index-sync's row-group upload (`-r`, most of its time) runs beside them too:
# staged rows stay invisible until the pointer flips below (every reader filters
# on the pointer's gen). xtrace stays off around the credentials check.
index_sync_creds() { [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; }
ROWS_PID=""
{ set +x; } 2>/dev/null
if index_sync_creds; then
  sr phase-begin "index-sync rows"
  (
    if dt-cloud index-sync -R -r -d "/gcs/$DATA/$INDEX_DIR" -g "$GEN" -k "$INDEX_DIR" "$SNAP_ID"; then
      sr phase-end "index-sync rows"
    else
      echo "WARN: index-sync -r failed (the pointer step below re-syncs in full)" >&2
      sr phase-end "index-sync rows" -q "failed"  # not alerted: the pointer step re-syncs in full
    fi
  ) & ROWS_PID=$!
fi
set -x
static_names_step & SN_PID=$!
interval_store_step & IV_PID=$!
wait "$SN_PID"
wait "$IV_PID"
[ -z "$ROWS_PID" ] || wait "$ROWS_PID"

# Footer-in-D1: sync the path-index parquet footer into the site's D1 so the
# reader skips the cold-isolate footer parse (specs/done/path-agnostic-serving.md
# §2.1). Needs CLOUDFLARE_API_TOKEN (D1 write) + CLOUDFLARE_ACCOUNT_ID — set as
# Batch secretVariables; without them the site falls back to parsing the footer,
# so this never blocks the snapshot. Disable xtrace for the WHOLE block first:
# even the `[ -n "$CLOUDFLARE_API_TOKEN" ]` test echoes the token under `set -x`.
# The rows were staged above (`-r`); `-p` checks them against each footer and
# flips the pointers, which lists the scan. If that refuses, a full sync redoes
# rows + pointers. `-M` records the scan's start (`index_schema.started`, from
# its meta.json) and fills any older date-only pointer still without one.
sr phase-begin index-sync
{ set +x; } 2>/dev/null
if index_sync_creds; then
  SNAPS=$(dirname "/gcs/$DATA/$SNAP_PATH")
  dt-cloud index-sync -R -p -M "$SNAPS" -d "/gcs/$DATA/$INDEX_DIR" -g "$GEN" -k "$INDEX_DIR" "$SNAP_ID" \
    || dt-cloud index-sync -R -M "$SNAPS" -d "/gcs/$DATA/$INDEX_DIR" -g "$GEN" -k "$INDEX_DIR" "$SNAP_ID" \
    || echo "WARN: index-sync failed (the site keeps serving the previous generation)" >&2
else
  echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — skipping index-sync (scan stays unlisted)" >&2
fi
set -x
sr phase-end index-sync
sr phase-begin healthcheck

# Serving invariant: the snapshot published + the footer synced — but is the
# SITE actually serving this scan? `dt-cloud healthcheck` asserts the scan is
# fresh and that subtree + the data JSONs serve (the 2026-08-31 outage was a
# footer that never synced to D1 → the owner rollup 1102'd → /users blank).
# Non-fatal — the snapshot data is fine, only serving would be degraded — but
# warn to #gcs-usage so it's caught at 07:00, not via a screenshot. Needs the
# agent token (also used by `series` below); skip quietly without it, and on
# REPROC (no fresh publish to verify). The token rides in env, never the
# cmdline, so xtrace is safe here.
HC_RC=0
if [ -n "${GCS_USAGE_TOKEN:+set}" ] && [ "${REPROC:-0}" != "1" ]; then
  dt-cloud healthcheck -d "$SNAP_ID" || HC_RC=$?
fi
sr phase-end healthcheck -x "$HC_RC" -W \
  -m "published, but the live site health check failed: data is fine, serving may be degraded (e.g. missing D1 index footer → /users blank). Debug: \`dt-cloud healthcheck -d $SNAP_ID\`."
sr phase-begin warm-cache

# Warm the site's subtree + diff caches for this scan (colo cache + global
# KV) so the first viewer of the day gets hits instead of a multi-second
# compute — the home page's default requests at the common canvas widths
# (`dt-cloud warm-cache`). Same token, same gating; never fatal.
if [ -n "${GCS_USAGE_TOKEN:+set}" ] && [ "${REPROC:-0}" != "1" ]; then
  dt-cloud warm-cache -d "$SNAP_ID" -r "gs://$DATA/snapshots" \
    || echo "WARN: cache warm-up failed for $SNAP_ID" >&2
fi

sr phase-end warm-cache
sr phase-begin probe

# Replay the site's page loads uncached (`dt-cloud probe -c`): every response
# a 2xx/4xx, and one cold-latency record per scan under probes/ (the time
# series of what a first viewer waits). Same token, same gating; a failure
# (a 5xx or a dropped request) warns but never fails the snapshot.
PROBE_RC=0
if [ -n "${GCS_USAGE_TOKEN:+set}" ] && [ "${REPROC:-0}" != "1" ]; then
  dt-cloud probe -c -o "gs://$DATA/probes/" || PROBE_RC=$?
fi
sr phase-end probe -x "$PROBE_RC" -W \
  -m "a page-load probe got a 5xx or a dropped request: data is fine, some views may fail. Debug: \`dt-cloud probe -c\`."
sr phase-begin slack-digest

# Converge the monthly Shape-C digest thread in Slack (specs/done/slack-digest-
# shape-c.md): the OP + one reply per scan. Only when SLACK_BOT_TOKEN +
# SLACK_CHANNEL are set — Shape C needs the Web API's per-message sender/avatar
# overrides, so the webhook fallback can't drive it. `dt-cloud digest` also
# renders the mosaic plot into job/icons/ and `wrangler pages deploy`s it to the
# gcs-usage-icons Pages project, so it needs CLOUDFLARE_* + node/wrangler (both
# present in this image). A failed digest never fails the snapshot.
if [ "${REPROC:-0}" = "1" ]; then
  echo "REPROC — skipping usage digest" >&2
elif [ -n "${SLACK_BOT_TOKEN:+set}" ] && [ -n "${SLACK_CHANNEL:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud digest -C job/digest.yml -r "gs://$DATA/snapshots" \
    || echo "WARN: usage-digest step failed" >&2
else
  echo "no Slack bot transport (SLACK_BOT_TOKEN+SLACK_CHANNEL) — skipping usage digest" >&2
fi

sr phase-end slack-digest
sr phase-begin discord-digest

# The same month thread in Marin's Discord #gcs-usage (specs/done/discord-
# digest-twin.md): OP + plot through the channel's app-owned webhook, the bot
# opens the thread and lends its arrow emoji. Both secrets are always mounted
# (batch-submit.sh); a failed post never fails the snapshot.
if [ "${REPROC:-0}" = "1" ]; then
  echo "REPROC — skipping Discord digest" >&2
elif [ -n "${DISCORD_GCS_USAGE_WEBHOOK:+set}" ] && [ -n "${DISCORD_BOT_TOKEN:+set}" ]; then  # `:+set`: xtrace must not print secrets
  dt-cloud digest -C job/digest.yml -P discord -r "gs://$DATA/snapshots" \
    || echo "WARN: Discord digest step failed" >&2
else
  echo "no Discord transport (DISCORD_GCS_USAGE_WEBHOOK+DISCORD_BOT_TOKEN) — skipping Discord digest" >&2
fi

sr phase-end discord-digest
sr phase-begin cf-usage

# The deployment's Cloudflare usage per day (specs/usage-metrics.md): one
# `snapshots/usage/<date>.json` per day of the last 7 that a copy lacks, in the
# data bucket and on R2 (what /admin/usage reads). Never fails the snapshot.
{ set +x; } 2>/dev/null
if [ "${REPROC:-0}" != "1" ] && [ -n "${CF_ANALYTICS_TOKEN:+set}" ]; then
  R2_OUT=()
  if [ -n "${R2_ACCESS_KEY_ID:+set}" ]; then
    R2_OUT=(-o "r2://oa-gcs-usage-index/snapshots/usage")
  fi
  DISKY_R2_ENDPOINT_URL="${R2_ENDPOINT:-https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com}" \
  AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}" \
    dt-cloud cf-usage -c site/wrangler.toml -d 7 -o "gs://$DATA/snapshots/usage" "${R2_OUT[@]}" \
    || echo "WARN: cf-usage snapshots failed" >&2
else
  echo "no CF_ANALYTICS_TOKEN, or REPROC — skipping the Cloudflare usage snapshots" >&2
fi
set -x
sr phase-end cf-usage
sr phase-begin index-gc

# Sweep row groups of generations the pointer no longer names (a REPROC's
# previous generation; every reader handle has expired by now), and retire
# the floor-free row groups of scans older than the newest INDEX_RETAIN —
# D1's 10 GB cap. A path-store scan costs ~218 MB of D1 (3 sorts × ~95k
# groups at ~766 B/row), so D1 is the hot window: 30 scans ≈ 6.5 GB. A scan
# past it keeps its pointers and is read from each tier's cold footer tier,
# `<tier>.groups.parquet` (range-read by the site); `index-gc -r` retires a
# variant's rows only once that file exists (specs/path-store.md §1.6).
{ set +x; } 2>/dev/null
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  dt-cloud index-gc -r "${INDEX_RETAIN:-30}" "$SNAP_ID" || echo "WARN: index-gc failed" >&2
fi
set -x
sr phase-end index-gc

echo "PHASE total: $(wall) (wall)" >&2
echo "SNAPSHOT-JOB-DONE $SNAP_ID"
