#!/usr/bin/env bash
# Build + push the daily-snapshot Batch image via Cloud Build (offloaded — no
# local Docker). Bakes `job/run.sh`, the `dt-cloud` package (cloud/src), and the
# disk-tree engine per the root Dockerfile; `.gcloudignore` trims the context.
#
# The scheduled job (batch-submit.sh) runs `IMAGE:latest`, so a rebuild is how
# run.sh / pipeline changes reach prod. Immutable per-day snapshots mean this is
# safe to run any time; the next daily run (or a manual batch-submit) picks it up.
#
# The image is the checkout this script lives in (not the caller's cwd), stamped
# with its exact `git rev-parse HEAD` and dirty state: OCI labels
# (`org.opencontainers.image.revision`, `disky.dirty`) and env (`DISKY_GIT_SHA`,
# `DISKY_GIT_DIRTY`), which `dt-cloud image-sha [--expect SHA]` reads back. Run
# a worktree's own `job/build.sh`: calling another checkout's from inside a
# worktree is refused (it would image that other checkout).
#
# `:latest` is also pushed to `MIRROR` (default the us-east1 copy, `gcs-usage/scan:latest`, which the jobs running
# beside the data bucket pull: `access-submit.sh`); a build with `IMAGE` overridden pushes no copy unless `MIRROR` is set.
#
# Env: PROJECT, IMAGE (override the tag, e.g. a throwaway tag to test a build), MIRROR,
# ALLOW_DIRTY=1 (build a tree with uncommitted changes to tracked files; stamped
# dirty), ALLOW_OTHER_CHECKOUT=1 (build this script's checkout from another's cwd).
set -euo pipefail
caller=$PWD
cd "$(dirname "$0")/.."
root=$(git rev-parse --show-toplevel)
cd "$root"

if caller_root=$(git -C "$caller" rev-parse --show-toplevel 2>/dev/null) && [ "$caller_root" != "$root" ] && [ "${ALLOW_OTHER_CHECKOUT:-}" != 1 ]; then
  echo "refusing: cwd is in checkout $caller_root, but this build.sh images $root" >&2
  echo "  run $caller_root/job/build.sh to image your checkout (ALLOW_OTHER_CHECKOUT=1 to build $root anyway)" >&2
  exit 1
fi

PROJECT=${PROJECT:-oa-internal-450019}
DEFAULT_IMAGE=0
if [ -z "${IMAGE:-}" ]; then
  DEFAULT_IMAGE=1
  IMAGE=us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest
  MIRROR=${MIRROR-us-east1-docker.pkg.dev/$PROJECT/gcs-usage/scan:latest}
fi
MIRROR=${MIRROR:-}

sha=$(git rev-parse HEAD)
branch=$(git rev-parse --abbrev-ref HEAD)
dirty_paths=$(git status --porcelain --untracked-files=no)
dirty=0
if [ -n "$dirty_paths" ]; then dirty=1; fi

{
  echo "================================================================"
  echo "building checkout: $root"
  echo "  branch: $branch"
  echo "  sha:    $sha"
  echo "  dirty:  $dirty"
  if [ "$dirty" = 1 ]; then
    echo "  uncommitted changes to tracked files:"
    echo "$dirty_paths" | sed 's/^/    /'
  fi
  echo "  image:  $IMAGE"
  if [ -n "$MIRROR" ]; then echo "  mirror: $MIRROR"; fi
  echo "================================================================"
} >&2

if [ "$dirty" = 1 ] && [ "${ALLOW_DIRTY:-}" != 1 ]; then
  echo "refusing to build a dirty tree: commit (or ALLOW_DIRTY=1 to build it stamped dirty)" >&2
  exit 1
fi

echo "building $IMAGE (Cloud Build; context = $root, minus .gcloudignore)" >&2
gcloud builds submit --project "$PROJECT" --config cloudbuild.yaml \
  --substitutions "_IMAGE=$IMAGE,_MIRROR=$MIRROR,_GIT_SHA=$sha,_GIT_DIRTY=$dirty" .

# Cloud Run resolves an image tag to its digest when a job or service is created or updated, not when it runs: the
# deployment's Cloud Run consumers of `:latest` (the daily scan's submitter, the alert relay) keep the build they were
# made from until updated. A default build re-points them at it (same tag, so the stack sees no change); one that
# doesn't exist yet is skipped.
if [ "$DEFAULT_IMAGE" = 1 ]; then
  for job in gcs-usage-snapshot-submit; do
    if gcloud run jobs describe "$job" --project "$PROJECT" --region us-central1 --format='value(name)' >/dev/null 2>&1; then
      gcloud run jobs update "$job" --project "$PROJECT" --region us-central1 --image "$IMAGE" --quiet >&2 && echo "re-pointed Cloud Run job $job at $IMAGE" >&2
    fi
  done
  for svc in gcs-alert-relay; do
    if gcloud run services describe "$svc" --project "$PROJECT" --region us-central1 --format='value(metadata.name)' >/dev/null 2>&1; then
      gcloud run services update "$svc" --project "$PROJECT" --region us-central1 --image "$IMAGE" --quiet >&2 && echo "re-pointed Cloud Run service $svc at $IMAGE" >&2
    fi
  done
fi
