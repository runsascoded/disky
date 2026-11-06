#!/usr/bin/env bash
# The cw scan's Nextflow head (specs/workflow-engine.md): stamps the scan's identity once (SNAP_ID, GEN, DT_JOB_START),
# starts its scan-run record, runs `job/nf/cw.nf` (every step a Batch task of its own) and finishes the record with
# Nextflow's exit status. Runs on a small Batch VM in the scan image (its ENTRYPOINT overridden), or anywhere with
# ADC (a manual or scratch run: `DT_RECORD=off`).
#
# Resume: the session id is derived from the scan id, and Nextflow's cache lives in the data bucket
# (`NXF_CLOUDCACHE_PATH`), so re-running the head for a failed scan (`SNAP_ID=<scan> GEN=<its gen>`) — on any machine,
# with no local state — re-runs only the steps that didn't finish. Nextflow finds a session only through its local
# history file, so the head writes that one line first.
#
# Env: SNAP_ID / GEN / DT_JOB_START (default: now), DATA_BUCKET, NF_IMAGE (the steps' image; default the scan
# image), NF_SECRETS (`ENV=SECRET` bindings the steps fetch themselves, as this job's account; default cw's set),
# NF_PASS (the env vars passed through to every step), SCAN_RUNS_PROFILE, DT_RECORD.
set -Eeuo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../cw-env.sh"
export DT_RUN_ID=${DT_RUN_ID:-${BATCH_JOB_UID:-}}
SR_ERR=""
trap 'scan_err $? $LINENO "$BASH_COMMAND"' ERR
trap 'exit 143' TERM
trap 'exit 130' INT
sr start -B -g "$GEN" "$SNAP_ID"

NF_SECRETS=${NF_SECRETS:-"AWS_ACCESS_KEY_ID=cw-s3-access-key-id AWS_SECRET_ACCESS_KEY=cw-s3-secret-access-key SLACK_BOT_TOKEN=cw-s3-slack-bot-token CLOUDFLARE_API_TOKEN=cw-s3-cf-pages-token SITE_TOKEN=cw-s3-job-grant R2_ENDPOINT=cw-s3-r2-endpoint R2_ACCESS_KEY_ID=cw-s3-isolated-r2-access-key-id R2_SECRET_ACCESS_KEY=cw-s3-isolated-r2-secret-access-key"}
NF_PASS=${NF_PASS:-"DATA_BUCKET SCAN_BUCKETS LISTING_PROCS LISTING_WORKERS IMPORT_JOBS R2_BUCKET STATIC_NAMES STATIC_NAMES_IMAGE SLACK_CHANNEL SLACK_ALERT_CHANNEL CLOUDFLARE_ACCOUNT_ID AWS_DEFAULT_REGION AWS_EC2_METADATA_DISABLED DT_S3_ADDRESSING_STYLE DT_RECORD DT_ALERTS REPROC INTERVAL_STORE_APPEND DISKY_LABELS"}
export NF_SECRETS NF_PASS
work=${NF_HOME:-${TMPDIR:-/tmp}/nf-$SNAP_ID}
mkdir -p "$work/.nextflow" && cd "$work"
session=$(python3 -c "import sys, uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, 'disky/cw-s3/scan/' + sys.argv[1]))" "$SNAP_ID")
export NF_SESSION=$session
params=$work/params.json
python3 - "$params" <<'PY'
import json, os, sys
env = {k: os.environ[k] for k in os.environ.get("NF_PASS", "").split() if k in os.environ}
p = {
    "scan": os.environ["SNAP_ID"], "gen": os.environ["GEN"], "job_start": os.environ["DT_JOB_START"],
    "run_id": os.environ.get("DT_RUN_ID", ""), "buckets": os.environ["BUCKETS"], "data_bucket": os.environ["DATA"],
    "secrets": os.environ.get("NF_SECRETS", ""), "env": env, "session": os.environ["NF_SESSION"],
}
# the tasks' cost labels: the stack's (`$DISKY_LABELS`, as the cron passes them) + `component: scan`
labels = dict(i.strip().split("=", 1) for i in os.environ.get("DISKY_LABELS", "").split(",") if i.strip())
if labels:
    p["labels"] = {**labels, "component": "scan"}
if os.environ.get("NF_IMAGE"):
    p["image"] = os.environ["NF_IMAGE"]
json.dump(p, open(sys.argv[1], "w"), indent=1)
PY
printf '%s\t-\tscan_%s\tOK\t-\t%s\tnextflow run cw.nf\n' "$(date -u '+%F %T')" "${SNAP_ID//[^0-9A-Za-z]/_}" "$session" > .nextflow/history
export NXF_CLOUDCACHE_PATH=gs://$DATA/nf/cache NXF_ANSI_LOG=false
# Nextflow in the background, so a TERM / INT to this head (a cancelled or preempted Batch job) reaches it at once and
# it cancels the step tasks it submitted; in the foreground, bash would hold the trap until Nextflow exited, and the
# container's KILL would orphan them (seen on a deleted scratch head, 2026-10-16).
# Its own process group (`set -m`), signalled whole: the `nextflow` launcher is a script whose JVM, the process that
# cancels the tasks on TERM, is its child.
rc=0
set -m
nextflow run "$here/cw.nf" -c "$here/nextflow.config" -params-file "$params" -resume "$session" -with-trace trace.tsv &
nf=$!
set +m
# Nextflow cancels the tasks it knows of; one whose submit was in flight lands after it exits (seen: 23 s later), so
# the head then cancels whatever still carries the session's label (`nf-session`, nextflow.config).
stop() { kill -TERM -- "-$nf" 2>/dev/null; wait "$nf" || true; sleep "${NF_SWEEP_DELAY:-30}"; dt-cloud batch-cancel -r us-east1 -l "nf-session=$session" || true; sr finish -x "$1" -e "head terminated (signal; session $session)" -l "nf-session=$session"; exit "$1"; }
trap 'stop 143' TERM
trap 'stop 130' INT
wait "$nf" || rc=$?
# The first failed task, for the record and the alert.
if [ "$rc" != 0 ] && [ -f trace.tsv ]; then
  f=$(awk -F'\t' 'NR>1 && $5=="FAILED" {print $4 " (exit " $6 ")"; exit}' trace.tsv)
  SR_ERR="nextflow exit $rc${f:+: $f failed} (session $session)"
fi
sr finish -x "$rc" ${SR_ERR:+-e "$SR_ERR"} -l "nf-session=$session"
if [ "$rc" = 0 ]; then echo "CW-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/cw/$SNAP_ID"; fi
exit "$rc"
