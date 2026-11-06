#!/usr/bin/env bash
# Submit a GCP Batch array job that rewrites cw's old-format (v1) layer-2
# listings in place as v2 — `disk-tree recompress` (specs/listing-slim.md
# phase 2): drops the derivable `uri` column and the class pivot that equals
# `size`, re-encodes in 64K-row groups, verifies row count + a content digest
# against the original, then swaps. Lossless; a file that fails verification is
# left untouched and reported. A v2 file already under the image's codec is
# skipped, one under another codec is re-encoded (same columns and keys), so
# the job is idempotent and re-runnable, and a codec flip is one more run over
# every scan. Measured on cw's 2026-09-29T1201 listing: the v2 file is ≈ 61 %
# of the original under Snappy (the default until 2026-09-30) and ≈ 23 % under
# zstd (the default since; `DISK_TREE_PARQUET_CODEC` in the task env overrides
# the image's default). Nothing served reads these files (the site reads the
# index tiers), so a rewrite is invisible to viewers.
#
#   job/cw-recompress-submit.sh 2026-08-19T0303 2026-08-20T0001   # these scans
#   job/cw-recompress-submit.sh $(job/cw-recompress-submit.sh -l)  # every v1 scan
#   job/cw-recompress-submit.sh -l                                 # list scan dirs (local, no submit)
#   DRY=1 job/cw-recompress-submit.sh …                            # print the body only
#   RECOMPRESS_DRY=1 job/cw-recompress-submit.sh …                 # tasks run `recompress -n` (report, write nothing)
#   PARALLELISM=8 job/cw-recompress-submit.sh …                    # concurrent tasks (default 4)
#
# One task per scan dir (`gs://$DATA_BUCKET/cw-l2/<scan>/`, its bucket parquets;
# the `index/` tiers underneath are not listings and are not touched). Each task
# downloads the listings to local disk, rewrites them there, and uploads the
# verified v2 files over the originals (no bucket mount; the job SA via ADC) —
# `recompress` straight over gs:// URLs hangs in gcsfs's prefetcher.
# **The R2 mirror is not touched**: if raw listings keep being mirrored,
# `job/cw-publish-submit.sh <scans>` re-syncs them afterwards (idempotent by
# size + md5, so it re-uploads exactly the rewritten files); if the mirror is to
# drop raw listings instead, that is a `publish-r2` prefix change, not this job.
#
# Needs the `:latest` image built from a tree that has `disk-tree recompress`
# (cloud `e957f8d` or later). Run `-l`'s first scan alone before the rest, and
# read its report (`recompress` prints old → new per file and a totals line).
set -euo pipefail
PROJECT=oa-internal-450019
REGION=us-east1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
DATA_BUCKET=${DATA_BUCKET:-oa-cw-s3-usage-data}
PARALLELISM=${PARALLELISM:-4}
JOB_ID=${JOB_ID:-cw-recompress-$(date -u +%Y%m%d-%H%M%S)}

if [ "${1:-}" = "-l" ]; then
  # Every scan dir under cw-l2/, oldest first (the caller picks; `recompress`
  # itself skips v2 files, so passing all of them is safe).
  # (scan ids only — `cw-l2/` also holds non-scan dirs such as `over-time-dev/`)
  gcloud storage ls "gs://$DATA_BUCKET/cw-l2/" | sed -n 's#^gs://[^/]*/cw-l2/\([0-9]\{4\}-[0-9][0-9]-[0-9][0-9]T[0-9]\{4\}\)/$#\1#p' | sort
  exit 0
fi
if [ $# -eq 0 ]; then echo "usage: $0 <scan-id>… | -l" >&2; exit 2; fi
SCANS=("$@")
export PROJECT SA IMAGE DATA_BUCKET PARALLELISM
body=$(python3 - "${SCANS[@]}" <<'EOF'
import json, os, sys
scans = sys.argv[1:]
flag = "-n " if os.environ.get("RECOMPRESS_DRY") else ""
cmd = (
    "set -euo pipefail\n"
    f"SCANS=({' '.join(scans)})\n"
    'SNAP_ID=${SCANS[$BATCH_TASK_INDEX]}\n'
    'DIR="cw-l2/$SNAP_ID"\n'
    'W=/work/$SNAP_ID; mkdir -p "$W"\n'
    'echo "task $BATCH_TASK_INDEX → recompress gs://$DATA_BUCKET/$DIR (staged in $W)"\n'
    # Stage the scan's top-level parquets (the per-bucket listings; the index/<gen>/
    # tiers underneath are not listings) on local disk: `recompress` over a gs://
    # URL hangs in gcsfs's prefetcher (2026-09-30: 2 h with no output on the first
    # try, 34 s for the same pass from local disk). google-cloud-storage is in the
    # image (publish-r2 uses it); the download and the upload both checksum.
    "python3 - \"$DATA_BUCKET\" \"$DIR\" \"$W\" <<'PY'\n"
    "import sys; from google.cloud import storage\n"
    "bucket, d, w = sys.argv[1:]\n"
    "for b in storage.Client().list_blobs(bucket, prefix=d + '/', delimiter='/'):\n"
    "    n = b.name.rsplit('/', 1)[1]\n"
    "    if n.endswith('.parquet'): print('  ↓', b.name, b.size, file=sys.stderr); b.download_to_filename(f'{w}/{n}')\n"
    "PY\n"
    'ls -l "$W"\n'
    'disk-tree listing-format "$W"/*.parquet\n'
    f'disk-tree recompress -j {flag}"$W"/*.parquet > "$W/report.json"\n'
    'python3 -c "import json,sys; t=json.load(open(sys.argv[1]))[\'totals\']; print(\'recompress totals:\', json.dumps(t))" "$W/report.json"\n'
    # Upload each rewritten file over its original (same key). Dry-run rewrites
    # nothing, so nothing is uploaded; a failed verification leaves the original
    # untouched locally and is a `failure` in the report (uploaded: nothing).
    "python3 - \"$DATA_BUCKET\" \"$DIR\" \"$W\" <<'PY'\n"
    "import json, os, sys; from google.cloud import storage\n"
    "bucket, d, w = sys.argv[1:]\n"
    "rep = json.load(open(f'{w}/report.json'))\n"
    "bk = storage.Client().bucket(bucket)\n"
    "for r in rep['results']:\n"
    "    if r['status'] != 'rewritten': continue\n"
    "    n = os.path.basename(r['path']); key = f'{d}/{n}'\n"
    "    bk.blob(key).upload_from_filename(r['path'], content_type='application/octet-stream', checksum='md5')\n"
    "    print('  ↑', key, os.path.getsize(r['path']), file=sys.stderr)\n"
    "if rep['failures']: sys.exit('recompress failures: ' + json.dumps(rep['failures']))\n"
    "PY\n"
    'rm -rf "$W"\n'
)
spec = {
    "taskGroups": [{
        "taskCount": len(scans),
        "parallelism": int(os.environ["PARALLELISM"]),
        "taskSpec": {
            "runnables": [{"container": {"imageUri": os.environ["IMAGE"], "entrypoint": "bash", "commands": ["-c", cmd]}}],
            # one decoded source row group (≤ 1 M rows of the oldest blobs) + one 64K output batch
            "computeResource": {"cpuMilli": 2000, "memoryMib": 7000},
            "maxRetryCount": 0,
            "maxRunDuration": "7200s",
            "environment": {"variables": {"DATA_BUCKET": os.environ["DATA_BUCKET"]}},
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": "n2-standard-2", "bootDisk": {"type": "pd-balanced", "sizeGb": "60"}}}],
        "serviceAccount": {"email": os.environ["SA"]},
        "location": {"allowedLocations": [f"regions/{os.environ.get('REGION', 'us-east1')}"]},
    },
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}
print(json.dumps(spec, indent=2))
EOF
)
if [ -n "${DRY:-}" ]; then echo "$body"; exit 0; fi
spec=$(mktemp); echo "$body" > "$spec"
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c recompress -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
