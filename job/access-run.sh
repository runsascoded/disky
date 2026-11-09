#!/usr/bin/env bash
# Hourly ingest, supervised by access-submit.sh: its own /scans run, never a snapshot producer.
set -euo pipefail
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-/app/job/scan-runs.json}
export SCAN_ID=${ACCESS_RUN_ID:-$(date -u +%FT%H%M)}
export DT_JOB_START=${DT_JOB_START:-$(date +%s)}
export DT_TRIGGER=${DT_TRIGGER:-manual}
export D1_DB_ID=${D1_DB_ID:-e52398b7-5538-4bc4-83db-3355a1b5ef9a}
export CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-74981a43be0de7712369306c7b19133d}
export SITE_URL=${SITE_URL:-https://gcs.oa.dev}
# Keep the existing single failure notification; the recorder still writes its error and logs to stderr.
export DT_ALERTS=stderr
sr() { dt-cloud scan-run "$@"; }
trap 'rc=$?; sr finish -x "$rc"; exit "$rc"' EXIT
echo "access-run.sh: image $(dt-cloud image-sha)" >&2
sr start -B -k access-ingest "$SCAN_ID"
sr phase-begin access-ingest
summary=${ACCESS_SUMMARY_FILE:-access.json}
set +e
dt-cloud access ingest -j -x -P "${PARALLEL:-1}" -M 48GB | tee "$summary"
rc=$?
set -e
note=
if [ "$rc" -eq 0 ]; then
  note=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("skipped (busy): ingest lease held by another run" if d["lease"] == "held" else "ingest lease taken; all configured buckets checked")' "$summary")
  if [[ "$note" == skipped* ]]; then sr nop; fi
fi
sr phase-end -x "$rc" -q "$note" access-ingest || true
if [ "$rc" -ne 0 ]; then
  what="ingest failed (exit $rc)"
  if [ "$rc" -eq 3 ]; then
    what="stalled delivery: $(python3 -c 'import json,sys; print(", ".join(json.load(open(sys.argv[1]))["stalled"]))' "$summary")"
  fi
  DISKY_NOTIFY=slack disk-tree notify -t "gcs access logs" "$what · ${SITE_URL%/}/scans/${BATCH_JOB_UID:-unknown}" || true
fi
exit "$rc"
