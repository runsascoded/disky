#!/usr/bin/env bash
# The 6-hourly scan's submitter. Cloud Scheduler `cw-usage-snapshot` (00/06/12/18 UTC) runs the Cloud Run job
# `cw-usage-snapshot-submit`, which runs this (infra/gcp `BatchSubmitCron`: its text is the container's `bash -c`).
# `$BATCH_SPEC` is the scan's Batch spec: `PIN=1 DRY=1 job/cw-batch-submit.sh`, rendered at `pulumi up`.
#
# The job is named for the UTC hour (`cw-usage-snapshot-YYYYMMDD-HH`), and a job of that name already existing counts
# as submitted, so a Scheduler retry, a second execution, or a re-run by hand within the hour starts no second scan.
# Transient failures (429, 5xx, connection) are retried for `$SUBMIT_RETRY_S` (default 30 min). If the submit still
# fails, this posts to #cw-s3-admin (`$SLACK_ALERT_CHANNEL`, with `$SLACK_BOT_TOKEN`) and exits nonzero.
#
# A manual scan (`DT_TRIGGER=manual`: the site's "Run a scan", `/api/scan-runs/dispatch`, runs this job with that one
# override, the only variable the job lets a run change) is the same spec with `DT_TRIGGER=manual` in its env, named for
# the second (`cw-usage-snapshot-YYYYMMDD-HHMMSS`, as hand submits are), so it never meets the slot's job; its alert
# says it was manual. Unset (the cron), nothing here differs.
set -uo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
JOB=cw-usage-snapshot-submit
trigger=${DT_TRIGGER:-schedule}

: "${BATCH_SPEC:?the scan job spec (JSON), from the Cloud Run job env}"
spec=$(mktemp)
log=$(mktemp)
printf '%s' "$BATCH_SPEC" > "$spec"
case "$trigger" in
  schedule)
    template=cw-usage-snapshot-%Y%m%d-%H
    slot_job=$(date -u "+$template")
    dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" -t -e -r "${SUBMIT_RETRY_S:-1800}" "$template" "$spec" 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    ;;
  manual)
    slot_job=$(date -u +cw-usage-snapshot-%Y%m%d-%H%M%S)
    # the cron's spec, its run recorded as manual (`scan_runs.trigger`); refused unless it carries the cron's trigger
    python3 - "$spec" <<'PY' || { rm -f "$spec" "$log"; exit 2; }
import json, sys
path = sys.argv[1]
spec = json.load(open(path))
envs = [g["taskSpec"]["environment"]["variables"] for g in spec["taskGroups"]]
if not envs or any(v.get("DT_TRIGGER") != "schedule" for v in envs):
    sys.exit("BATCH_SPEC: want DT_TRIGGER=schedule in every task group's env, to make it manual")
for v in envs:
    v["DT_TRIGGER"] = "manual"
json.dump(spec, open(path, "w"))
PY
    dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" -e -r "${SUBMIT_RETRY_S:-1800}" "$slot_job" "$spec" 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    ;;
  *)
    echo "DT_TRIGGER=$trigger: want schedule (the cron) or manual" >&2
    rm -f "$spec" "$log"
    exit 2
    ;;
esac
if [ "$rc" -ne 0 ]; then
  why=$(grep -v '^note: ' "$log" | tail -n 1)
  logs="https://console.cloud.google.com/run/jobs/executions/details/$REGION/${CLOUD_RUN_EXECUTION:-unknown}/logs?project=$PROJECT"
  if [ "$trigger" = manual ]; then
    DISKY_NOTIFY=slack disk-tree notify -t "cw manual scan: not submitted" -u "$logs" -l "submitter logs" \
      "The manual scan started from /scans didn't start: Batch job $slot_job was not created (exit $rc). ${why:-no error output}
Start it again from https://cw.oa.dev/scans once Batch answers." || true
  else
    DISKY_NOTIFY=slack disk-tree notify -t "cw scan: not submitted" -u "$logs" -l "submitter logs" \
      "No scan this slot: Batch job $slot_job was not created (exit $rc). ${why:-no error output}
Re-run once Batch answers (a no-op if the job exists): \`gcloud run jobs execute $JOB --region $REGION --project $PROJECT\`" || true
  fi
fi
rm -f "$spec" "$log"
exit "$rc"
