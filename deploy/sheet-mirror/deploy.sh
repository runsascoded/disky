#!/usr/bin/env bash
# Create/update a deployment's sheet mirror: a Cloud Run *job* (runs sync.sh
# once over every mirror in the config) + a Cloud Scheduler trigger, both as
# the config's service account.
#
#   deploy/sheet-mirror/deploy.sh [-P] <sheet-mirror.yml>
#
# `-P`: the deployment's Pulumi stack (`infra/gcp`, `RunJobCron`) owns the
# job's IAM and its Scheduler trigger; only upsert the job's image and
# rendered config (Pulumi ignores those two).
#
# Idempotent: `run jobs deploy` upserts, the trigger is create-or-update, and
# the two IAM bindings it needs (secret access on the token, run.invoker on
# the job so the Scheduler can trigger it) are re-applied as no-ops. The
# config itself rides into the job as `$SHEET_MIRROR_CONFIG_B64`, so a mirror
# change is a re-run of this script; a code change is `build.sh` (the job runs
# the config's image, default `:latest`).
set -euo pipefail
iac=false
if [ "${1:-}" = -P ]; then iac=true; shift; fi
cfg=${1:?usage: deploy.sh [-P] <sheet-mirror.yml>}

eval "$(dt-cloud sheet-mirror env "$cfg")"
# Validate the mirrors too (fails loudly on a bad source / key / executor).
n=$(dt-cloud sheet-mirror plan "$cfg" | wc -l | tr -d ' ')
# The RENDERED config (every `${NAME}` substituted from this shell's env,
# validated): ids kept out of the repo reach the job, never git.
config_b64=$(dt-cloud sheet-mirror render "$cfg" | base64 | tr -d '\n')

echo "== $n mirror(s) → job $JOB ($PROJECT/$REGION), schedule '$SCHEDULE' ==" >&2

if ! $iac; then
  echo "== IAM: SA can read the token secret ==" >&2
  gcloud secrets add-iam-policy-binding "$TOKEN_SECRET" --project "$PROJECT" \
    --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null
fi

echo "== Cloud Run job (upsert) ==" >&2
# `^@^`: gcloud's alternate list delimiter — base64 has no `@`, but may end in `=`.
gcloud run jobs deploy "$JOB" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --service-account "$SA" \
  --set-env-vars "^@^SHEET_MIRROR_CONFIG_B64=$config_b64" \
  --set-secrets "GCS_USAGE_TOKEN=$TOKEN_SECRET:latest" \
  --max-retries 1 --task-timeout 600

if ! $iac; then
  echo "== IAM: SA can run the job (Scheduler invokes as SA) ==" >&2
  gcloud run jobs add-iam-policy-binding "$JOB" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:$SA" --role roles/run.invoker >/dev/null

  URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run"
  echo "== Cloud Scheduler $TRIGGER (create or update) ==" >&2
  if gcloud scheduler jobs describe "$TRIGGER" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
    verb=update
  else
    verb=create
  fi
  gcloud scheduler jobs "$verb" http "$TRIGGER" --project "$PROJECT" --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone UTC \
    --uri "$URI" --http-method POST \
    --oauth-service-account-email "$SA"
fi

echo "done. Trigger a one-off run with:" >&2
echo "  gcloud run jobs execute $JOB --project $PROJECT --region $REGION --wait" >&2
