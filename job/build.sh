#!/usr/bin/env bash
# Build + push the CoreWeave scan job's Batch image via Cloud Build (offloaded —
# no local Docker). Bakes `job/cw-run.sh`, the `dt-cloud` package (cloud/src),
# and the disk-tree engine per the root Dockerfile; `.gcloudignore` trims the
# context.
#
# The scheduled scan (cw-batch-submit.sh) runs `IMAGE:latest`, so a rebuild is how
# cw-run.sh / pipeline changes reach prod. Immutable per-scan snapshots mean
# this is safe to run any time; the next scan picks it up.
#
# Every image is stamped with its exact `git rev-parse HEAD` and dirty state: OCI labels
# (`org.opencontainers.image.revision`, `disky.dirty`) and env (`DISKY_GIT_SHA`,
# `DISKY_GIT_DIRTY`), which `dt-cloud image-sha [--expect SHA]` reads back. Run
# a worktree's own `job/build.sh`: calling another checkout's from inside a
# worktree is refused (it would image that other checkout).
#
# Env: PROJECT, IMAGE (override the tag, e.g. a throwaway tag to test a build),
# ALLOW_OTHER_CHECKOUT=1 (build this script's checkout from another's cwd).
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
# Which job script the image runs (`job/<JOB>`); the tag defaults to `latest`, the
# deployment's (repo `cw-s3-usage` is cw's alone: `DeploymentImages` in infra/gcp).
JOB=${JOB:-cw-run.sh}
TAG=${TAG:-latest}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:$TAG}

sha=$(scripts/require-clean)
branch=$(git rev-parse --abbrev-ref HEAD)

{
  echo "================================================================"
  echo "building checkout: $root"
  echo "  branch: $branch"
  echo "  sha:    $sha"
  echo "  dirty:  0"
  echo "  image:  $IMAGE"
  echo "================================================================"
} >&2

# Freeze the upload from Git rather than racing concurrent edits in the worktree.
mkdir -p tmp
source_dir=$(mktemp -d "$root/tmp/cw-build-source.XXXXXX")
trap 'rm -rf "$source_dir"' EXIT
git archive "$sha" | tar -x -C "$source_dir"
echo "building $IMAGE (Cloud Build; committed source $sha, minus .gcloudignore)" >&2
gcloud builds submit --project "$PROJECT" --config "$source_dir/cloudbuild.yaml" \
  --substitutions "_IMAGE=$IMAGE,_JOB=$JOB,_GIT_SHA=$sha,_GIT_DIRTY=0" "$source_dir"
