#!/usr/bin/env bash
# The age pyramids for the scans indexed before the scan pipeline built them (specs/m3-scan-nf.md §2a): for each
# scan of a store (default `laptop`, D1 `primary`), its newest `listing/<store>/<scan>/index/<gen>/` in R2, the
# `age-pyramid-<bin>` tiers from its `path` sort (`dt-cloud age-pyramid -c`, checked against it), uploaded beside it,
# then `index-sync -A` (only the age variants; the others keep their pointer). A scan that has its `1d` tier is
# skipped, so a rerun picks up where one stopped.
#
#   INGEST_PIPELINE=age-backfill (entry.sh), e.g. `aws/submit -c 4 -m 16384 -e INGEST_PIPELINE=age-backfill -`
set -euo pipefail

: "${R2_ENDPOINT_URL:?}" "${D1_DB_ID:?}" "${CLOUDFLARE_API_TOKEN:?}" "${CLOUDFLARE_ACCOUNT_ID:?}"
export AWS_DEFAULT_REGION=auto
export DISKY_R2_ENDPOINT_URL=$R2_ENDPOINT_URL
bucket=${BUCKET:-disk-tree}
store=${STORE:-laptop}
d1_store=${D1_STORE:-primary}
work=${WORK:-/work}/age-backfill
s3() { aws s3 "$@" --endpoint-url "$R2_ENDPOINT_URL" --only-show-errors; }
vcpus() {
  local cg=${DT_CGROUP:-/sys/fs/cgroup} q p
  if { read -r q p < "$cg/cpu.max"; } 2>/dev/null && [[ $q != max ]]; then :
  else nproc; return; fi
  echo $(( (q + p - 1) / p ))
}
mkdir -p "$work"
# Every `path` sort of the store: `<scan> <gen>`, the newest generation per scan.
aws s3 ls "s3://$bucket/listing/$store/" --recursive --endpoint-url "$R2_ENDPOINT_URL" \
  | awk '{print $4}' | grep -E "^listing/$store/[^/]+/index/[^/]+/path-index\.parquet$" \
  | awk -F/ '{print $3, $5}' | sort -k1,1 -k2,2 | awk '{g[$1] = $2} END {for (s in g) print s, g[s]}' | sort > "$work/scans.txt"
echo "[age-backfill] $(wc -l < "$work/scans.txt") $store scans"
# One scan's tiers, uploaded and synced; nonzero on the first failing command (`set -e` doesn't hold in an `if`).
backfill() {
  local scan=$1 gen=$2 dir=listing/$store/$1/index/$2 w=$work/$1
  mkdir -p "$w" &&
    s3 cp "s3://$bucket/$dir/path-index.parquet" "$w/path-index.parquet" &&
    ( cd /app/cloud && dt-cloud age-pyramid -c -t "$(vcpus)" -o "$w" "$w/path-index.parquet" ) &&
    s3 cp "$w/" "s3://$bucket/$dir/" --recursive --exclude '*' --include 'age-pyramid-*.parquet' &&
    ( cd /app/cloud && dt-cloud index-sync "$scan" -g "$gen" -b "r2://$bucket" -k "$dir" -s "$d1_store" -d "$w" -A )
}
done_n=0 skipped=0 failed=()
while read -r scan gen; do
  dir=listing/$store/$scan/index/$gen
  if aws s3api head-object --bucket "$bucket" --key "$dir/age-pyramid-1d.parquet" --endpoint-url "$R2_ENDPOINT_URL" > /dev/null 2>&1; then
    skipped=$((skipped + 1))
    continue
  fi
  t0=$(date +%s)
  # A scan that fails (e.g. a pre-2026-10-01 `path` sort, without `kind`/`mtime`) is reported and the rest go on.
  if backfill "$scan" "$gen" < /dev/null; then
    done_n=$((done_n + 1))
    echo "[age-backfill] $scan ($gen) in $(( $(date +%s) - t0 ))s"
  else
    failed+=("$scan")
    echo "[age-backfill] $scan ($gen) FAILED"
  fi
  rm -rf "${work:?}/$scan"
done < "$work/scans.txt"
echo "[age-backfill] built $done_n, already had $skipped, failed ${#failed[@]}${failed:+: ${failed[*]}}"
(( ${#failed[@]} == 0 ))
