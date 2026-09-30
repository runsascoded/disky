# Lightweight image for the scheduled sheet mirror (`sync.sh`): just the
# `dt-cloud` CLI (`export`, `sheet-push`, `sheet-mirror plan`) — no site build,
# no wrangler. Built from a minimal context `build.sh` stages (the repo-root
# layout, trimmed to what's copied here); see cloudbuild.yaml.
#
# Installed by uv from the workspace lockfile (`uv.lock` at the root covers the
# engine and `cloud/`), `--frozen`, into the image's own interpreter
# (`UV_PROJECT_ENVIRONMENT=/usr/local`): the image runs exactly the pins the
# tests ran, not whatever `pip install .` would resolve on build day (a fresh
# resolve pulled gcsfs 2026.8.1 into the cw job image and hung every `gs://`
# read at exit — specs/listing-slim.md phase 2).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
# The engine's wheel force-includes the built UI (ui/dist) — none is needed
# here, so an empty dir stands in. `--no-editable`: the members are copied in,
# not linked. `--package dt-cloud --extra sheets`: the overlay + what it needs.
RUN mkdir -p ui/dist && uv sync --no-dev --no-editable --package dt-cloud --extra sheets
COPY deploy/sheet-mirror/sync.sh ./sync.sh
ENTRYPOINT ["bash", "sync.sh"]
