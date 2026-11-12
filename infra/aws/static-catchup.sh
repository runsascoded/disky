#!/usr/bin/env bash
# One-off: drain the primary store's static name index backlog in chunks, outside the scan pipeline. Each `THROUGH` id
# is one `static-names add -t <id>`, which uploads its runs before the next chunk starts, so a failure keeps the chunks
# before it (the pipeline's one `add` uploads only at the end: specs/static-add-incremental-upload.md upstream). An
# empty last entry (or `newest`) drains to the newest scan. `SN_THREADS` defaults to the task's vCPUs: Fargate's
# `cpu.max` is `max`, so the pipeline's `nproc` sees the host's CPUs and runs DuckDB with too many threads for its cap.
#
#   INGEST_PIPELINE=static-catchup (entry.sh), e.g.
#   aws/submit -c 4 -m 30720 -t 28800 -e INGEST_PIPELINE=static-catchup -e 'THROUGH=2026-11-02T1104 2026-11-02T2311 newest' -e SN_MEM=20GB -
set -euo pipefail

: "${R2_ENDPOINT_URL:?}" "${THROUGH:?space-separated scan ids; newest drains to the newest}"
export AWS_DEFAULT_REGION=auto
export DISKY_R2_ENDPOINT_URL=$R2_ENDPOINT_URL
export R2_ENDPOINT=$R2_ENDPOINT_URL R2_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
gen=${SN_GEN:-2026-10-15}
layout=${SN_LAYOUT:-listing/laptop/{id}/index/{gen}/path-index.parquet}
store=${SN_STORE:-r2://disk-tree}
work=${WORK:-/work}/static-catchup
vcpus() {
  local cg=${DT_CGROUP:-/sys/fs/cgroup} q p
  if { read -r q p < "$cg/cpu.max"; } 2>/dev/null && [[ $q != max ]]; then echo $(( (q + p - 1) / p )); return; fi
  # Fargate: no quota in the cgroup; the job's vCPUs come in from `aws/submit -c` as `SN_THREADS`, else 2 (the job def's)
  echo 2
}
threads=${SN_THREADS:-$(vcpus)}
mem=${SN_MEM:-9GB}
mkdir -p "$work"
for t in $THROUGH; do
  through=()
  [[ $t == newest ]] || through=(-t "$t")
  echo "[static-catchup] add -g $gen ${through[*]:-(to newest)} -p $threads -M $mem"
  # a fresh work dir per chunk (the mirror is ~80 s; no DuckDB spill carried over), the last one dropped first
  rm -rf "$work/chunk"
  (cd /app/cloud && /app/.venv/bin/dt-cloud static-names add -g "$gen" -L "$layout" -s "$store" -o "$store" -w "$work/chunk" \
    -p "$threads" -M "$mem" "${through[@]}")
done
echo "[static-catchup] done"
