#!/usr/bin/env bash
# Submit the daily snapshot job to GCP Batch, with local SSD for DuckDB spill.
# Default: n2-highmem-32; SPLIT_COMPUTE=1 opts into a small manual controller
# whose aggregation/access workers retain the known-good memory limits.
#
# Env overrides: SNAP_ID (a scan id, e.g. a sub-daily `YYYY-MM-DDTHHMM` for an extra scan) or
# SNAPSHOT_DATE (legacy explicit day override; default is a UTC timestamp inside run.sh), IMAGE,
# DUCKDB_MEM (default 100GB, split controller 8GB), DUCKDB_THREADS (16, split 4), SNAP_PATH,
# LISTING_MACHINE/PROCS/WORKERS (DIY fan-out sizing) — forwarded when set.
set -euo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
SA=gcs-usage-job@$PROJECT.iam.gserviceaccount.com
# PIN=1: emit the canonical/cron spec — drop every ambient override so the body is
# byte-identical regardless of the caller's shell (the IaC in infra/gcp
# generates the Cloud Scheduler body from `PIN=1 DRY=1`). Must precede the
# ${VAR:-default} knobs below; the python vars() honors PIN too.
if [ -n "${PIN:-}" ]; then unset IMAGE MACHINE MEMORY_MIB LOCAL_SSD_GB MAX_RUN_SECONDS DATA_BUCKET ZONE; fi
if [ -n "${ZONE:-}" ]; then
  [[ "$ZONE" =~ ^us-central1-[a-z]$ ]] || { echo "ERROR: ZONE must be in $REGION" >&2; exit 1; }
fi

IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
# webdata's peak RSS is DuckDB at its cap plus ~40GB it doesn't account for
# (the access dict + hash-agg overshoot on 179M dir groups): 124GB on a
# highmem-16 was OOM-killed on 2026-09-05/06, so the daily runs on a highmem-32
# (256G) until specs/view-serving.md §4–5 land and the peak comes back down.
# The Cloud Scheduler body is generated from `PIN=1 DRY=1` here (infra/gcp),
# so this stays the single source of truth for the daily spec.
if [ -z "${PIN:-}" ] && [ "${SPLIT_COMPUTE:-0}" = "1" ]; then
  [[ "$IMAGE" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || { echo "ERROR: split scan requires a digest-pinned IMAGE" >&2; exit 1; }
  MACHINE=${MACHINE:-n2-standard-4}
  MEMORY_MIB=${MEMORY_MIB:-14000}
  LOCAL_SSD_GB=${LOCAL_SSD_GB:-0}
else
  MACHINE=${MACHINE:-n2-highmem-32}
  MEMORY_MIB=${MEMORY_MIB:-250000}
  LOCAL_SSD_GB=${LOCAL_SSD_GB:-1500}  # n2 32-vCPU machines require >=4 local SSDs
fi
CPU_MILLI=8000
case "$MACHINE" in
  *-2) CPU_MILLI=2000 ;;
  *-4) CPU_MILLI=4000 ;;
esac
JOB_ID=${JOB_ID:-gcs-usage-snapshot-$(date -u +%Y%m%d-%H%M%S)}
if [ -z "${PIN:-}" ]; then
  export SCAN_RUN_ID=${SCAN_RUN_ID:-$JOB_ID}
  export SNAP_ID=${SNAP_ID:-${SNAPSHOT_DATE:-$(date -u +%FT%H%M)}}
fi

labels() {  # cost labels (specs/cost-labels.md): $DISKY_LABELS + component $1, as JSON; {} when unset
  python3 - "$1" <<'PY'
import json, os, re, sys
d = dict(i.strip().split("=", 1) for i in os.environ.get("DISKY_LABELS", "").split(",") if i.strip())
if d and not os.environ.get("PIN"):
    for var, key in (("SCAN_RUN_ID", "scan_run"), ("SNAP_ID", "scan_id")):
        value = os.environ[var].lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,63}", value):
            raise ValueError(f"invalid {key} label: {value!r}")
        d[key] = value
print(json.dumps({**d, "component": sys.argv[1]} if d else {}))
PY
}

vars() {  # container env: defaults + optional passthroughs
  JOB_IMAGE="$IMAGE" SUBMIT_REGION="$REGION" python3 - <<'EOF'
import json, os
# PIN=1 emits the canonical/cron spec: ignore every ambient override so the body
# is byte-stable regardless of the caller's shell (the IaC in infra/gcp
# generates the Cloud Scheduler body from `PIN=1 DRY=1`). Without PIN, the env
# knobs below and the passthrough list stay live for manual one-off submits.
pin = bool(os.environ.get("PIN"))
g = (lambda k, d="": d) if pin else os.environ.get
v = {
    # 100 on the 256G node (was 86 on 128G: DuckDB at its cap plus ~40GB of
    # unaccounted process memory must fit the container — 100 and 90 inside
    # 124GB both got kernel-OOM-killed, exit 137, as the dir-group count grew).
    # Measured 2026-09-06 REPROC on highmem-32: peak RSS 141.7GB at 100GB.
    "DUCKDB_MEM": g("DUCKDB_MEM", "8GB" if g("SPLIT_COMPUTE") == "1" else "100GB"),
    # access ingest runs before (not concurrent with) the webdata step, so
    # it can take a big slice of the 128G node; 24GB OOM'd on a row-heavy
    # 53GB chunk (2026-08-23)
    "DUCKDB_MEM_ACCESS": g("DUCKDB_MEM_ACCESS", "48GB"),
    "DUCKDB_THREADS": g("DUCKDB_THREADS", "4" if g("SPLIT_COMPUTE") == "1" else "16"),
    "DATA_BUCKET": g("DATA_BUCKET", "oa-gcs-usage-dvx"),
    # stage inputs to the local-SSD mount (see disks below); STAGE_DIR="" disables
    "STAGE_DIR": g("STAGE_DIR", "/tmp" if g("SPLIT_COMPUTE") == "1" else "/stage"),
    # Slack digest target: chat.postMessage (bot token) so per-message avatars apply
    "SLACK_CHANNEL": g("SLACK_CHANNEL", "C0BNWAASXFW"),  # #gcs-usage
    # failure/healthcheck alerts go to #gcs-admin (with the staging threads), not the digest channel #gcs-usage
    "SLACK_ALERT_CHANNEL": g("SLACK_ALERT_CHANNEL", "C0C579HNRFB"),
    # CF account for index-sync's `wrangler d1 execute` (token is a secretVariable)
    "CLOUDFLARE_ACCOUNT_ID": g("CLOUDFLARE_ACCOUNT_ID", "74981a43be0de7712369306c7b19133d"),
    # the stack's cost labels (PIN keeps them: `submitter_spec` passes them through), so the
    # jobs this one submits (the listing fan-out, the static-names chain) carry them too
    "DISKY_LABELS": os.environ.get("DISKY_LABELS", ""),
    # what started the scan (`scan_runs.trigger`, the scan's `trigger.json`): the cron body is the PIN spec;
    # everything else is a hand submit
    "DT_TRIGGER": "schedule" if pin else os.environ.get("DT_TRIGGER", "manual"),
    "JOB_IMAGE": os.environ["JOB_IMAGE"],
}
v = {k: s for k, s in v.items() if s}
if not pin:  # one-off overrides forwarded only for manual submits, never the cron spec
    for k in ["SPLIT_COMPUTE", "SCAN_RUN_ID", "SNAP_ID", "GEN", "DT_JOB_START", "SNAPSHOT_DATE", "SNAP_PATH", "INDEX_PATH", "SCRATCH", "REPROC", "SWEEP", "SWEEP_PLAN", "SWEEP_DATE", "SWEEP_BUCKETS", "USER",
              "SWEEP_BENCHMARK_PLAN", "SWEEP_BENCHMARK_BUCKET", "SWEEP_BENCHMARK_WORKERS", "SWEEP_BENCHMARK_ROOTS", "SWEEP_BENCHMARK_RESULTS_PER_ROOT", "SWEEP_BENCHMARK_OUT",
              "ACCESS_ONLY", "SKIP_ACCESS", "ACCESS_ARGS", "GATE", "GATE_K", "GATE_P", "GATE_THREADS", "GATE_HIST",
              "LISTING_MACHINE", "LISTING_PROCS", "LISTING_WORKERS",
              "GCS_ALERT_CEILING_TB", "GCS_ALERT_SPIKE_PCT", "INDEX_RETAIN", "PATH_INDEX_RG_ROWS", "PATH_INDEX_USER_SORT_TIERS", "PATH_INDEX_SEARCH"]:  # SLACK_WEBHOOK is a secretVariable (see below)
        if k in os.environ:
            v[k] = os.environ[k]
# The one-VM fallback (`NF=0`) names its region for `scan-run task` and `job submit-listing`, and mounts the
# fleet's and the data bucket from this region on purpose; never in the pinned cron body.
if not pin and os.environ.get("NF") == "0":
    v["BATCH_REGION"] = os.environ["SUBMIT_REGION"]
    v["BATCH_CROSS_REGION"] = "1"
print(json.dumps(v))
EOF
}

# Batch only auto-creates host mount dirs under /mnt/disks — mount there, then map
# into the container at the /gcs/<bucket> paths run.sh expects.
BUCKETS_PY='["marin-us-east1", "marin-us-east5", "marin-us-central1",
             "marin-us-central2", "marin-eu-west4", "marin-us-west4",
             "oa-gcs-usage-dvx"]'

volumes() {
  python3 - <<EOF
import json
print(json.dumps([
    {"gcs": {"remotePath": b}, "mountPath": f"/mnt/disks/gcs/{b}", "mountOptions": ["--implicit-dirs"]}
    for b in $BUCKETS_PY
]))
EOF
}

container_volumes() {
  python3 - <<EOF
import json
print(json.dumps([f"/mnt/disks/gcs/{b}:/gcs/{b}:rw" for b in $BUCKETS_PY]
                 + (["/mnt/disks/stage:/stage:rw"] if $LOCAL_SSD_GB else [])))
EOF
}

spec=$(mktemp)
# Every secretVariable must be readable by the job SA or Batch refuses to start
# the job; the two Discord ones (the #gcs-usage app-owned webhook + Marin Bot's
# token, both in this project) were granted 2026-09-14. The daily cron body is
# generated from this spec via `PIN=1 DRY=1` (infra/gcp) and carries the same set.
cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": 1,
    "taskSpec": {
      "runnables": [{"container": {"imageUri": "$IMAGE", "volumes": $(container_volumes)}}],
      "computeResource": {"cpuMilli": $CPU_MILLI, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "${MAX_RUN_SECONDS:-21600}s",
      "volumes": $(volumes | python3 -c "import json,sys; v=json.load(sys.stdin); print(json.dumps(v + ([{'deviceName': 'stage', 'mountPath': '/mnt/disks/stage'}] if $LOCAL_SSD_GB else [])))"),
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "SLACK_BOT_TOKEN": "projects/$PROJECT/secrets/gcs-alert-slack-bot-token/versions/latest",
          "SLACK_WEBHOOK": "projects/$PROJECT/secrets/gcs-alert-slack-webhook/versions/latest",
          "CLOUDFLARE_API_TOKEN": "projects/$PROJECT/secrets/cf-pages-token/versions/latest",
          "R2_ACCESS_KEY_ID": "projects/$PROJECT/secrets/gcs-static-index-r2-key-id/versions/latest",
          "R2_SECRET_ACCESS_KEY": "projects/$PROJECT/secrets/gcs-static-index-r2-secret/versions/latest",
          "GCS_USAGE_TOKEN": "projects/$PROJECT/secrets/gcs-snapshot-site-token/versions/latest",
          "DISCORD_GCS_USAGE_WEBHOOK": "projects/$PROJECT/secrets/gcs-usage-discord-webhook/versions/latest",
          "DISCORD_BOT_TOKEN": "projects/$PROJECT/secrets/marin-discord-bot-token/versions/latest",
          "CF_ANALYTICS_TOKEN": "projects/$PROJECT/secrets/gcs-cf-analytics-token/versions/latest"
        }
      }
    }
  }],
  "allocationPolicy": {
    "instances": [{
      "policy": {
        "machineType": "$MACHINE",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
        "disks": $(python3 -c "import json; print(json.dumps([{'newDisk': {'type': 'local-ssd', 'sizeGb': '$LOCAL_SSD_GB'}, 'deviceName': 'stage'}] if $LOCAL_SSD_GB else []))")
      }
    }],
    "serviceAccount": {"email": "$SA"},
    "location": {"allowedLocations": ["${ZONE:+zones/}${ZONE:-regions/$REGION}"]},
    "labels": $(labels scan)
  },
  "labels": $(labels scan),
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF

# NF=scan (the default, and the daily's since 2026-10-17's prod run): the generic Nextflow head (`dt-cloud scan head
# $PROFILE`, specs/pipelines-everywhere.md); NF=0 submits the one-VM `job/run.sh` instead. The head only drives the DAG (each step is a Batch task of its own, sized by the profile, which
# fetches its own secrets), so: a small VM, no mounts or stage disk, and of the secrets only what the head itself uses
# (the scan-run record's D1 token, the alert's Slack token). Its Batch job is the run every task records into.
if [ "${NF:-scan}" = scan ]; then
  PROFILE=${PROFILE:-/app/job/profiles/gcs.yml} python3 - "$spec" <<'PY'
import json, os, sys
p = sys.argv[1]; j = json.load(open(p))
t = j["taskGroups"][0]["taskSpec"]
c = t["runnables"][0]["container"]
c["entrypoint"] = "dt-cloud"
c["commands"] = ["scan", "head", os.environ["PROFILE"]]
c["volumes"] = []
t["volumes"] = []
t["computeResource"] = {"cpuMilli": 2000, "memoryMib": 7000}
# 12 h: the 2026-10-18 scan hit 6 h (a 158-min access ingest gating the index, then a 2 h+ static-names drill) and the
# kill left the scan unsynced; the head only orchestrates, so a long run costs an e2-standard-2
t["maxRunDuration"] = "43200s"
keep = {"CLOUDFLARE_API_TOKEN", "SLACK_BOT_TOKEN"}
t["environment"]["secretVariables"] = {k: v for k, v in t["environment"]["secretVariables"].items() if k in keep}
pol = j["allocationPolicy"]["instances"][0]["policy"]
pol["machineType"] = "e2-standard-2"
pol.pop("disks", None)
pol["bootDisk"] = {"type": "pd-balanced", "sizeGb": "30"}
json.dump(j, open(p, "w"), indent=2)
PY
fi

# DRY=1: print the job spec (e.g. for the Cloud Scheduler daily body) instead of submitting
if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2  # refuses a spec without the cost labels
rm -f "$spec"
echo "$JOB_ID"
