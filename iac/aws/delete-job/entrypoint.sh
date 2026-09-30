#!/usr/bin/env bash
# AWS Batch delete-job (spec `specs/staged-delete.md` CP8). Env in:
#   RUN_ID  — the deletion_run to finish
#   URIS    — newline-separated s3:// (or r2://) URIs to delete
#   DISK_TREE_D1_DATABASE_ID, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN — D1 write-back
# Deletes each URI, then finishes the run in D1 (finished_ts + totals). The
# drainer left `batch_job` set, so it never re-submitted this run.
set -euo pipefail

deleted_objects=0
while IFS= read -r uri; do
  [ -z "$uri" ] && continue
  # r2:// rides the same S3 API via its endpoint; the aws CLI speaks s3://.
  s3_uri="s3://${uri#*://}"
  n=$(aws s3 rm --recursive "$s3_uri" | wc -l)
  deleted_objects=$((deleted_objects + n))
done <<< "$URIS"

now=$(date +%s)
d1_query() {
  curl -sf -X POST \
    "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/d1/database/${DISK_TREE_D1_DATABASE_ID}/query" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H 'Content-Type: application/json' \
    -d "$1" > /dev/null
}
d1_query "$(jq -nc --arg r "$RUN_ID" --argjson n "$deleted_objects" --argjson t "$now" \
  '{sql: "UPDATE deletion_runs SET finished_ts = ?, deleted_objects = ? WHERE run_id = ?", params: [$t, $n, $r]}')"

echo "run $RUN_ID: deleted $deleted_objects object(s), finished in D1"
