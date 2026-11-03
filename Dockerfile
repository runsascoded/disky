# Cloud Run job image for daily storage snapshots (see job/run.sh).
# Stage 1: build the static site (data dirs are overlaid at runtime).
# The site is a pnpm-workspace member (with in-tree @disk-tree/react), so the
# build context is the repo root: copy the workspace manifests + the members
# the site needs.
FROM node:22-slim AS site
WORKDIR /repo
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml ./
COPY packages/treemap ./packages/treemap
COPY packages/react ./packages/react
COPY site ./site
RUN corepack enable && pnpm install --frozen-lockfile
RUN cd site && pnpm build

# Stage 2: pipeline + wrangler (node for wrangler; python for dt-cloud).
# Node comes from the node:22-slim stage (same Debian base) — Debian's apt
# nodejs is v20, below wrangler's floor (≥22 as of wrangler 4.116).
FROM python:3.12-slim
COPY --from=site /usr/local/bin/node /usr/local/bin/node
COPY --from=site /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g wrangler@4
# GNU time: the gate mode (`job/run.sh` GATE=1) reports each import's peak RSS.
# curl: thrds's Discord clients shell out to it (the 2026-09-15 job's Discord
# digest died with `FileNotFoundError: 'curl'`); python:slim ships none.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends time curl ca-certificates && rm -rf /var/lib/apt/lists/*
# Nextflow (specs/pipelines-everywhere.md): `dt-cloud scan head` runs the scan as a DAG of Batch tasks in this same
# image. A JRE (Nextflow takes 17–26), `ps` (procps: Nextflow's task-metrics wrapper refuses to run without it), the
# launcher pinned by NXF_VER, its runtime and the plugins it loads fetched at build time, so neither the head nor a
# task downloads anything.
ENV NXF_VER=26.10.2 NXF_HOME=/opt/nextflow
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends default-jre-headless procps && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/nextflow-io/nextflow/releases/download/v$NXF_VER/nextflow" -o /usr/local/bin/nextflow \
    && chmod +x /usr/local/bin/nextflow && nextflow -version \
    && nextflow plugin install nf-google@1.29.1,nf-cloudcache@0.6.2
WORKDIR /app
# Python deps come from the workspace lockfile (`uv.lock` at the root covers
# the engine and `cloud/`), `--frozen`, into the image's own interpreter
# (`UV_PROJECT_ENVIRONMENT=/usr/local`): the image runs exactly the pins the
# tests ran. The `pip install .` this replaced resolved fresh on every build —
# gcsfs 2026.8.1 came in that way and hung a scan job at exit (its adaptive
# prefetcher vs a pyarrow-held `gs://` handle; `blobfs` now opts out).
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1
# disk-tree engine (root project): fan-out listing tasks run `disk-tree
# bulk-list`.
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
# `--package dt-cloud` pulls the engine (`disk-tree[gcs,s3]`) as its dependency;
# `--no-editable`: the members are copied in, not linked.
RUN uv sync --frozen --no-dev --no-editable --package dt-cloud
COPY --from=site /repo/site/dist ./dist
COPY job ./job
# the deployment's Pages config: `dt-cloud cf-usage` reads its resources (project, buckets, D1s, KV, dataset) from it
COPY site/wrangler.toml ./site/wrangler.toml
# The source revision (`job/build.sh` → `cloudbuild.yaml` build args), last so a new sha rebuilds no layer above:
# OCI labels for the registry, env for the code (`dt-cloud image-sha [--expect SHA]`, the stages' start-up log).
ARG DISKY_GIT_SHA=""
ARG DISKY_GIT_DIRTY=""
LABEL org.opencontainers.image.revision=$DISKY_GIT_SHA disky.dirty=$DISKY_GIT_DIRTY
ENV DISKY_GIT_SHA=$DISKY_GIT_SHA DISKY_GIT_DIRTY=$DISKY_GIT_DIRTY
# `supervise` records a platform-terminated or OOM-killed run as failed (specs/scan-job-wrapper.md).
ENTRYPOINT ["dt-cloud", "scan-run", "supervise", "--", "bash", "job/run.sh"]
