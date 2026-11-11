#!/usr/bin/env bash
# The 6-hourly scan's submitter. Cloud Scheduler `cw-usage-snapshot` (00/06/12/18 UTC) runs the Cloud Run job
# `cw-usage-snapshot-submit`, which runs this (infra/gcp `BatchSubmitCron`: its text is the container's `bash -c`).
# `$BATCH_SPEC` is the scan's Batch spec: `PIN=1 DRY=1 job/cw-batch-submit.sh`, rendered at `pulumi up`.
#
# The job is named for the UTC hour (`cw-usage-snapshot-YYYYMMDD-HH`), and a job of that name already existing counts
# as submitted, so a Scheduler retry, a second execution, or a re-run by hand within the hour starts no second scan.
# Transient failures (429, 5xx, connection) are retried for `$SUBMIT_RETRY_S` (default 30 min). If the submit still
# fails, this posts to #cw-s3-admin (`$SLACK_ALERT_CHANNEL`, with `$SLACK_BOT_TOKEN`) and exits nonzero.
set -uo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
JOB=cw-usage-snapshot-submit
template=cw-usage-snapshot-%Y%m%d-%H
slot_job=$(date -u "+$template")

: "${BATCH_SPEC:?the scan job spec (JSON), from the Cloud Run job env}"
spec=$(mktemp)
log=$(mktemp)
printf '%s' "$BATCH_SPEC" > "$spec"
dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" -t -e -r "${SUBMIT_RETRY_S:-1800}" "$template" "$spec" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
  why=$(grep -v '^note: ' "$log" | tail -n 1)
  logs="https://console.cloud.google.com/run/jobs/executions/details/$REGION/${CLOUD_RUN_EXECUTION:-unknown}/logs?project=$PROJECT"
  DISKY_NOTIFY=slack disk-tree notify -t "cw scan: not submitted" -u "$logs" -l "submitter logs" \
    "No scan this slot: Batch job $slot_job was not created (exit $rc). ${why:-no error output}
Re-run once Batch answers (a no-op if the job exists): \`gcloud run jobs execute $JOB --region $REGION --project $PROJECT\`" || true
fi
rm -f "$spec" "$log"
exit "$rc"
