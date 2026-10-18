# A laptop deployment's ingest image: a capture (layer-1 listing shards in R2) →
# path index + snapshot, back to R2 (see ingest.sh).
#
# Built on CodeBuild by `BatchIngest` (`batch_ingest.py`) from the deploying
# checkout's sources, zipped by Pulumi; no registry ref to clone. Context is the
# repo root; `Dockerfile.dockerignore` allowlists the Python sources. `cloud/` is a member of the root uv
# workspace: one lockfile (`--frozen`), one venv holding `dt-cloud` and the
# `disk-tree` engine it depends on.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# GNU time: ingest.sh reports path-index's peak RSS (the spike's question). Nextflow (the scan pipeline, `scan.sh`,
# specs/m3-scan-nf.md), as the OA images': a JRE (Nextflow takes 17–26), `ps` (procps: its task wrapper refuses to
# run without it), the launcher pinned by NXF_VER and its runtime fetched at build time. The local executor needs no
# plugins.
ENV NXF_VER=26.10.2 NXF_HOME=/opt/nextflow
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends time ca-certificates default-jre-headless procps curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/nextflow-io/nextflow/releases/download/v$NXF_VER/nextflow" -o /usr/local/bin/nextflow \
    && chmod +x /usr/local/bin/nextflow && nextflow -version
RUN uv tool install awscli==1.44.*

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
# The wheel force-includes the built UI; the ingest never serves it.
RUN mkdir -p ui/dist && uv sync --frozen --no-dev --package dt-cloud

# The pipeline's steps call `dt-cloud` / `disk-tree` bare.
ENV PATH=/app/.venv/bin:$PATH
# chmod: the source zip (Pulumi `AssetArchive`) doesn't carry file modes.
COPY infra/aws/ingest.sh infra/aws/scan.sh infra/aws/entry.sh infra/aws/age-backfill.sh /
RUN chmod 755 /ingest.sh /scan.sh /entry.sh /age-backfill.sh
COPY job/nf ./job/nf
COPY job/profiles ./job/profiles
# The scan-run profile ingest.sh records with (`SCAN_RUNS_PROFILE`).
COPY infra/aws/scan-runs.json ./infra/aws/scan-runs.json
# `entry.sh`: the scan pipeline under `INGEST_PIPELINE=nf`, else the bash ingest under `supervise`, which runs it in its
# own process group (a TERM reaches every phase at once) and records a terminated or killed job failed
# (specs/scan-job-wrapper.md). Alert channels come from the container env.
ENTRYPOINT ["bash", "/entry.sh"]
