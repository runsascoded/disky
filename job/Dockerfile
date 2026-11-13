# The scan job image, every deployment's: the `disk-tree` + `dt-cloud` CLIs, Nextflow (`dt-cloud scan head` runs the
# scan as a DAG of Batch tasks in this same image), wrangler, and optionally the built static site. Built by
# `dt-cloud image build` (`job/build.sh`) from a staged context (the repo-root layout, the config's `context`), with
# the deployment's `build_args` (specs/job-consolidation.md §5):
#
#   SITE           `build`: build the site into `/app/dist` (needs `pnpm-workspace.yaml`, `package.json`,
#                  `pnpm-lock.yaml`, `packages/` and `site/` in the context); `none` (default): an empty `/app/dist`
#   APT_PACKAGES   extra Debian packages, space-separated (e.g. `time`, for a gate's peak RSS; `git`, for a `git+https` pin)
#   UV_SYNC_ARGS   extra `uv sync` args, e.g. `--extra s3 --extra overtime`
#   ENTRYPOINT     the command the container runs, its args appended (default `dt-cloud`), e.g.
#                  `dt-cloud scan-run supervise -- bash job/run.sh`
#
# BuildKit (`DOCKER_BUILDKIT=1`, which the build sets) builds only the `site-$SITE` stage the image uses.
ARG SITE=none

# Node from the node:22-slim stage (the same Debian base as python:3.12-slim): Debian's apt nodejs is v20, below
# wrangler's floor (≥22 as of wrangler 4.116).
FROM node:22-slim AS node

FROM node AS site-none
RUN mkdir -p /repo/site/dist

# The site is a pnpm-workspace member (with in-tree packages), so the workspace manifests and the members it needs.
FROM node AS site-build
WORKDIR /repo
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml ./
COPY packages/treemap ./packages/treemap
COPY packages/react ./packages/react
COPY packages/pages-kit ./packages/pages-kit
COPY site ./site
RUN corepack enable && pnpm install --frozen-lockfile
RUN cd site && pnpm build

FROM site-${SITE} AS site

FROM python:3.12-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g wrangler@4
# Nextflow (specs/pipelines-everywhere.md): a JRE (Nextflow takes 17–26), `ps` (procps: Nextflow's task-metrics wrapper
# refuses to run without it), the launcher pinned by NXF_VER, its runtime and the plugins it loads fetched at build
# time, so neither the head nor a task downloads anything. curl: thrds's Discord clients shell out to it too.
ARG APT_PACKAGES=""
ENV NXF_VER=26.10.2 NXF_HOME=/opt/nextflow
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends default-jre-headless procps curl ca-certificates $APT_PACKAGES \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/nextflow-io/nextflow/releases/download/v$NXF_VER/nextflow" -o /usr/local/bin/nextflow \
    && chmod +x /usr/local/bin/nextflow && nextflow -version \
    && nextflow plugin install nf-google@1.29.1,nf-cloudcache@0.6.2
# Python deps from the workspace lockfile (`uv.lock` covers the engine and `cloud/`), `--frozen`, into the image's own
# interpreter (`UV_PROJECT_ENVIRONMENT=/usr/local`): the image runs exactly the pins the tests ran. `--package dt-cloud`
# pulls the engine (`disk-tree[gcs,s3]`) as its dependency; `--no-editable`: the members are copied in, not linked.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
ARG UV_SYNC_ARGS=""
RUN uv sync --frozen --no-dev --no-editable --package dt-cloud $UV_SYNC_ARGS
COPY --from=site /repo/site/dist ./dist
COPY job ./job
# The deployment's Pages config: the job reads its D1, its index generations and the `cf-usage` step's resources from
# it, so the job and the site name the same ones.
COPY site/wrangler.toml ./site/wrangler.toml
# `dt-cloud scan head`'s pipeline (`scan.nf`, `base.config`)
ENV DT_NF_DIR=/app/job/nf
ARG ENTRYPOINT=dt-cloud
ENV DISKY_ENTRYPOINT=$ENTRYPOINT
# The source revision (the build's stamp), last so a new sha rebuilds no layer above: OCI labels for the registry, env
# for the code (`dt-cloud image-sha [--expect SHA]`, the stages' start-up log).
ARG DISKY_GIT_SHA=""
ARG DISKY_GIT_DIRTY=""
LABEL org.opencontainers.image.revision=$DISKY_GIT_SHA disky.dirty=$DISKY_GIT_DIRTY
ENV DISKY_GIT_SHA=$DISKY_GIT_SHA DISKY_GIT_DIRTY=$DISKY_GIT_DIRTY
# `$DISKY_ENTRYPOINT` word-split, the container's args after it; `exec`, so it is PID 1 and gets the signals (a
# `scan-run supervise` entrypoint records a preempted or OOM-killed run as failed). A Batch runnable or Nextflow task that
# sets its own entrypoint bypasses this.
ENTRYPOINT ["bash", "-c", "exec $DISKY_ENTRYPOINT \"$@\"", "--"]
