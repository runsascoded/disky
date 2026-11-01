# Batch job image for the CoreWeave scans (see job/cw-run.sh).
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
# git: the [overtime] extra pins pyrmts from GitHub (`git+https`), which pip
# can only fetch with a git binary.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends time curl ca-certificates git && rm -rf /var/lib/apt/lists/*
# Nextflow (specs/workflow-engine.md): `job/nf/cw-head.sh` runs the scan as a DAG of Batch tasks in this same image.
# A JRE (Nextflow takes 17–26), `ps` (procps: Nextflow's task-metrics wrapper refuses to run without it), the launcher pinned by NXF_VER, its runtime and the plugins it loads fetched at build
# time, so neither the head nor a task downloads anything.
ENV NXF_VER=26.10.2 NXF_HOME=/opt/nextflow
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends default-jre-headless procps && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/nextflow-io/nextflow/releases/download/v$NXF_VER/nextflow" -o /usr/local/bin/nextflow \
    && chmod +x /usr/local/bin/nextflow && nextflow -version \
    && nextflow plugin install nf-google@1.29.1,nf-cloudcache@0.6.2
WORKDIR /app
# Python deps from the workspace lockfile (`uv.lock` at the root covers the
# disk-tree engine and `cloud/`, a workspace member), installed into the system
# interpreter (`UV_PROJECT_ENVIRONMENT=/usr/local`) so `disk-tree` / `dt-cloud`
# are on PATH as before. `--frozen`: the image runs exactly the pins CI tested
# (the previous unpinned `pip install` resolved fresh each build — gcsfs 2026.8.1's
# default prefetcher hung `recompress gs://` on Batch, 2026-09-30). `--package
# dt-cloud` pulls `disk-tree[gcs,s3]` through dt-cloud's dependency; `[overtime]` =
# pyrmts' multiscan kernel
# (a `git+https` pin — hence git above). Same recipe as deploy/sheet-mirror/Dockerfile.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
RUN uv sync --no-dev --no-editable --package dt-cloud --extra overtime
COPY --from=site /repo/site/dist ./dist
COPY job ./job
# The Pages config the `cf-usage` step filters Cloudflare's analytics by (`options.cf-usage.config`)
COPY site/wrangler.toml ./site/wrangler.toml
# The entrypoint runs `job/$JOB` (`job/build.sh` passes `--build-arg JOB=…`):
# the CoreWeave scan job, `cw-run.sh`, by default, under `dt-cloud scan-run
# supervise` (specs/scan-job-wrapper.md, "The entrypoint"): a preemption, cancel
# or OOM kill is recorded as a failed run, never left `running` or `succeeded`.
ARG JOB=cw-run.sh
ENV JOB_SCRIPT=$JOB
ENTRYPOINT ["bash", "-c", "exec dt-cloud scan-run supervise -- bash job/$JOB_SCRIPT \"$@\"", "--"]
