#!/usr/bin/env bash
# Build this checkout's scan job image from the deployment's `job/build.json` (specs/job-consolidation.md §5):
# `dt-cloud image build` with that config. Prints `<repository>@<digest>` per repository, primary first.
#
#   job/build.sh                  # the config's mode: `candidate` (an immutable tag, nothing live moves) or `live`
#                                 #   (the candidate, then promoted: live tags moved, Cloud Run consumers re-pointed)
#   job/build.sh -m candidate     # a candidate whatever the config says (the release lane's build)
#   job/build.sh -t try-x -D      # a throwaway tag, from a dirty tree (stamped dirty)
#   job/build.sh promote TAG [-x sha256:…]   # promote an existing candidate
#
# The image is the checkout this script lives in, never the caller's: run from another checkout it refuses (`-O`
# builds this one anyway).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
if [ "${1:-}" = promote ]; then
  shift
  exec dt-cloud image promote -c "$here/build.json" "$@"
fi
exec dt-cloud image build -c "$here/build.json" "$@"
