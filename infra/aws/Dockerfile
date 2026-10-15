# A laptop deployment's ingest image: a capture (layer-1 listing shards in R2) →
# path index + snapshot, back to R2 (see ingest.sh).
#
# Built on CodeBuild by `BatchIngest` (`batch_ingest.py`) from the deploying
# checkout's sources, zipped by Pulumi; no registry ref to clone. Context is the
# repo root; `Dockerfile.dockerignore` allowlists the Python sources. `cloud/` is a member of the root uv
# workspace: one lockfile (`--frozen`), one venv holding `dt-cloud` and the
# `disk-tree` engine it depends on.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# GNU time: ingest.sh reports path-index's peak RSS (the spike's question).
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends time ca-certificates && rm -rf /var/lib/apt/lists/*
RUN uv tool install awscli==1.44.*

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
# The wheel force-includes the built UI; the ingest never serves it.
RUN mkdir -p ui/dist && uv sync --frozen --no-dev --package dt-cloud

# chmod: the source zip (Pulumi `AssetArchive`) doesn't carry file modes.
COPY infra/aws/ingest.sh /ingest.sh
RUN chmod 755 /ingest.sh
# The scan-run profile ingest.sh records with (`SCAN_RUNS_PROFILE`).
COPY infra/aws/scan-runs.json ./infra/aws/scan-runs.json
# `supervise` runs the script in its own process group: a TERM reaches every phase at once, and a
# terminated or killed job is recorded failed (specs/scan-job-wrapper.md). Its alert channels come
# from the container env, as the script's do.
ENTRYPOINT ["/app/.venv/bin/dt-cloud", "scan-run", "supervise", "--", "bash", "/ingest.sh"]
