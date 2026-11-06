#!/usr/bin/env bash
# Submit a one-task GCP Batch job that measures scan-to-scan churn for
# specs/storage-consolidation.md phase 3 — read-only: it stages the named scans'
# `path` sorts (D1's pointer dir for each) and the named over-time groups onto
# local disk, then runs `dt_cloud.churn` over them (`scan_churn` per adjacent
# pair + any extra pairs, writing each delta locally to measure its bytes;
# `group_churn` per group). Results are single-line JSON in the job's log:
# `CHURN-RESULT {…}` per pair, `GROUP-RESULT {…}` per group, `PEAK-RSS-MB n`.
# Nothing is written to any bucket.
#
# The churn module rides IN the job body (copied from this tree's
# `cloud/src/dt_cloud/churn.py`), so this runs on the current `:latest` image
# before an image with `dt-cloud churn` ships.
#
#   job/cw-churn-submit.sh -p 2026-10-01T0003:2026-10-02T1201 2026-10-01T0003 2026-10-01T1201 2026-10-02T0001 2026-10-02T1201
#   G=<over-time.parquet key>… job/cw-churn-submit.sh …      # also these groups (bucket-relative keys)
#   DRY=1 job/cw-churn-submit.sh …                            # print the body only
#
# Scans are given oldest first; adjacent ones are paired. `-p A:B` adds a pair.
set -euo pipefail
PROJECT=oa-internal-450019
REGION=us-east1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
DATA_BUCKET=${DATA_BUCKET:-oa-cw-s3-usage-data}
MACHINE=${MACHINE:-n2-standard-8}
JOB_ID=${JOB_ID:-cw-churn-$(date -u +%Y%m%d-%H%M%S)}
HERE=$(cd "$(dirname "$0")" && pwd)

EXTRA=()
while [ "${1:-}" = "-p" ]; do EXTRA+=("$2"); shift 2; done
if [ $# -lt 2 ] && [ ${#EXTRA[@]} -eq 0 ]; then echo "usage: $0 [-p A:B]… <scan>…" >&2; exit 2; fi
SCANS=("$@")
# Each scan's `path` pointer dir, from D1 (the generation the site serves;
# needs CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID).
export D1_DB_ID=${D1_DB_ID:-7f1e1326-b879-4ecd-8621-846621c24f36}   # oa-cw-s3-usage-db
export D1_DB_NAME=${D1_DB_NAME:-oa-cw-s3-usage-db}
DIRS=()
for s in "${SCANS[@]}" $(printf '%s\n' "${EXTRA[@]}" | tr ':' '\n'); do
  case " ${DIRS[*]:-} " in *" $s="*) continue ;; esac
  d=$(dt-cloud index-dir "$s") || { echo "no path pointer for $s" >&2; exit 1; }
  DIRS+=("$s=$d")
done
export PROJECT SA IMAGE DATA_BUCKET MACHINE REGION
export CHURN_PY="$HERE/../cloud/src/dt_cloud/churn.py"
export GROUPS_KEYS="${G:-}"
body=$(python3 - "${#SCANS[@]}" "${SCANS[@]}" "${EXTRA[@]}" "${DIRS[@]}" <<'EOF'
import json, os, sys
n = int(sys.argv[1]); scans = sys.argv[2:2 + n]
rest = sys.argv[2 + n:]
extra = [a for a in rest if ':' in a and '=' not in a]
dirs = dict(a.split('=', 1) for a in rest if '=' in a)
pairs = [(scans[i], scans[i + 1]) for i in range(len(scans) - 1)] + [tuple(p.split(':')) for p in extra]
groups = os.environ["GROUPS_KEYS"].split()
src = open(os.environ["CHURN_PY"]).read().replace("from .overtime import", "from dt_cloud.overtime import")
keys = {s: f"{d}/path-index.parquet" for s, d in dirs.items()}
driver = f'''
import json, resource, sys, time
import duckdb
sys.path.insert(0, "/work/sidecar")
import churn_side as C
con = duckdb.connect()
con.execute("SET memory_limit='22GB'; SET threads=8; SET temp_directory='/work/duck'; SET preserve_insertion_order=false")
for a, b in {pairs!r}:
    t = time.time()
    r = C.scan_churn(f"/work/scans/{{a}}.parquet", f"/work/scans/{{b}}.parquet", out_dir=f"/work/delta/{{a}}_{{b}}", con=con)
    print("CHURN-RESULT", json.dumps({{"a": a, "b": b, "secs": round(time.time() - t, 1), **r}}), flush=True)
for i, g in enumerate({groups!r}):
    t = time.time()
    r = C.group_churn(f"/work/groups/{{i}}.parquet", con=con)
    print("GROUP-RESULT", json.dumps({{"group": g, "secs": round(time.time() - t, 1), **r}}), flush=True)
print("PEAK-RSS-MB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024, flush=True)
'''
fetch = "\n".join(
    [f"{k} /work/scans/{s}.parquet" for s, k in keys.items()]
    + [f"{g} /work/groups/{i}.parquet" for i, g in enumerate(groups)]
)
cmd = (
    "set -euo pipefail\n"
    "mkdir -p /work/sidecar /work/scans /work/groups /work/delta /work/duck\n"
    "cat > /work/sidecar/churn_side.py <<'CHURN_PY_EOF'\n" + src + "CHURN_PY_EOF\n"
    "cat > /work/driver.py <<'DRIVER_EOF'\n" + driver + "DRIVER_EOF\n"
    "cat > /work/fetch.txt <<'FETCH_EOF'\n" + fetch + "\nFETCH_EOF\n"
    # Stage with the job SA via ADC (google-cloud-storage is in the image; no
    # gcsfs reads — its prefetcher hangs on long pyarrow-held handles).
    "python3 - \"$DATA_BUCKET\" <<'PY'\n"
    "import sys; from google.cloud import storage\n"
    "bk = storage.Client().bucket(sys.argv[1])\n"
    "for line in open('/work/fetch.txt'):\n"
    "    key, dst = line.split()\n"
    "    bk.blob(key).download_to_filename(dst); print('  ↓', key, file=sys.stderr)\n"
    "PY\n"
    "ls -l /work/scans /work/groups\n"
    "python3 /work/driver.py\n"
    "du -b /work/delta/*/*.parquet\n"
)
spec = {
    "taskGroups": [{
        "taskCount": 1,
        "taskSpec": {
            "runnables": [{"container": {"imageUri": os.environ["IMAGE"], "entrypoint": "bash", "commands": ["-c", cmd]}}],
            "computeResource": {"cpuMilli": 8000, "memoryMib": 30000},
            "maxRetryCount": 0,
            "maxRunDuration": "3600s",
            "environment": {"variables": {"DATA_BUCKET": os.environ["DATA_BUCKET"]}},
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": os.environ["MACHINE"], "bootDisk": {"type": "pd-balanced", "sizeGb": "100"}}}],
        "serviceAccount": {"email": os.environ["SA"]},
        "location": {"allowedLocations": [f"regions/{os.environ['REGION']}"]},
    },
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}
print(json.dumps(spec, indent=2))
EOF
)
if [ -n "${DRY:-}" ]; then echo "$body"; exit 0; fi
spec=$(mktemp); echo "$body" > "$spec"
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c analysis -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
