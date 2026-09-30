#!/usr/bin/env bash
# Build + push a deployment's sheet-mirror image via Cloud Build (no local
# Docker). The Cloud Run job runs the config's image (default `:latest`), so a
# rebuild is how `sync.sh` / CLI changes reach it.
#
#   deploy/sheet-mirror/build.sh <sheet-mirror.yml>
#
# Reads PROJECT / IMAGE from the config (`dt-cloud sheet-mirror env`), and
# uploads only what the Dockerfile copies (staged into a temp dir), not the
# whole repo.
set -euo pipefail
cfg=${1:?usage: build.sh <sheet-mirror.yml>}
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)

eval "$(dt-cloud sheet-mirror env "$cfg")"

ctx=$(mktemp -d)
trap 'rm -rf "$ctx"' EXIT
mkdir -p "$ctx/cloud" "$ctx/deploy/sheet-mirror"
cp -R "$root/pyproject.toml" "$root/README.md" "$root/uv.lock" "$root/src" "$ctx/"
cp -R "$root/cloud/pyproject.toml" "$root/cloud/src" "$ctx/cloud/"
cp "$here/Dockerfile" "$here/cloudbuild.yaml" "$here/sync.sh" "$ctx/deploy/sheet-mirror/"
find "$ctx" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "building $IMAGE (Cloud Build, project $PROJECT)" >&2
gcloud builds submit --project "$PROJECT" \
  --config "$ctx/deploy/sheet-mirror/cloudbuild.yaml" --substitutions "_IMAGE=$IMAGE" "$ctx"
