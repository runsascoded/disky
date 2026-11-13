# Job consolidation: one shared copy on `cloud`, per-deployment config only

**Status:** the shared versions are on `w/job-consolidate` (off `cloud` `d3c0c4afb`). Each deployment's owner switches over: add the config below, delete its copies, merge. Nothing here touches a deployment branch, an image, or a cloud resource.

The branch-harmony audit (`specs/branch-harmony-audit.md` on `w/branch-harmony`) found code each deployment branch carried its own copy of, absent from `cloud`. Each item below moves one copy to `cloud`, keeps every real per-deployment difference as config the deployment owns, and lists, per deployment, the config to add and the files to delete. Code is generic: no deployment's project, bucket, registry or host is a default; the values below are each deployment's own config, quoted here as the adoption recipe.

## 1. `job/gen-*.py`, `job/rerg`, the icons project's shared assets

On `cloud`:
- `job/gen-delta-arrows.py`, `job/gen-arrow-avatars.py`: gcs's and cw's (byte-identical), with the gcs-specific wording dropped from the docstrings.
- `job/icons/_headers` and `job/icons/arrows/*.png`: identical on all four branches (gcs, cw `job/icons/`; hf `job/icons-hf/`; r2 `job/icons-r2/`), and cw's `job/icons-cw/_headers` too.
- `job/rerg`: the launcher, with the deployment's values read from `job/rerg.env` (or `$RERG_ENV`, or the environment): `RERG_PROJECT`, `RERG_REGION`, `RERG_IMAGE`, `RERG_SA`, `RERG_STAGING`, optional `RERG_GCLOUD_ACCOUNT`. Any unset is an error naming it.

Each deployment's mark (`mark.png`) stays its own file: a deployment that serves its icons project from `job/icons/` adds its `job/icons/mark.png` there (gcs and cw already do).

| Deployment | Add | Delete |
| --- | --- | --- |
| gcs | `job/rerg.env` (below) | `job/gen-delta-arrows.py`, `job/gen-arrow-avatars.py`, `job/rerg`, `job/icons/_headers`, `job/icons/arrows/` (keeps `job/icons/mark.png`, `calendar.png`, `gcs-*.png`) |
| cw | `job/rerg.env` with its own values, if it uses `rerg` | the same five; `job/icons-cw/_headers` only if it moves its plot dir to `job/icons/` |
| hf | nothing | optional: move `job/icons-hf/mark.png` to `job/icons/mark.png`, set `digest.yml` `icons_dir: job/icons`, delete `job/icons-hf/` |
| r2 | nothing | optional: as hf, for `job/icons-r2/` |

gcs's `job/rerg.env` (today's hardcoded values):
```bash
RERG_PROJECT=oa-internal-450019
RERG_REGION=us-east1
RERG_IMAGE=us-central1-docker.pkg.dev/oa-internal-450019/cloud-run-source-deploy/pqtk:latest
RERG_SA=gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com
RERG_STAGING=gs://oa-gcs-usage-dvx/pqtk
```
cw's copy pinned `CLOUDSDK_CORE_ACCOUNT` to a person's address; with the shared launcher that is `RERG_GCLOUD_ACCOUNT` in the operator's environment, not a tracked file.

## 2. `job/static-names.sh`: one launcher over the deployment's static-names profile

On `cloud`: `job/static-names.sh`, gcs's launcher (`stage-src`, `run`, `wait`, `resume-after`, `r2-batch`, the drill / anchors exit diagnostics, cost labels) with cw's additions (`NO_SCRATCH`, the R2 copy as the profile's R2 account), and every deployment value read from the static-names profile the runner already reads (`dt_cloud.static_profile`, through `dt-cloud static-names profile`):

| Launcher value | Profile field | Was (gcs / cw) |
| --- | --- | --- |
| `PROJECT`, `REGION` | `project`, `region` | hardcoded / hardcoded |
| `B` (data bucket) | `bucket` | hardcoded / `DATA_BUCKET` |
| `S` (scratch; unset: no mount) | `scratch` | hardcoded / `STATIC_NAMES_SCRATCH` |
| `IMAGE` (env wins) | `image` | a pinned digest / `scan:latest` |
| `SA` (env wins) | `sa` | hardcoded / `SA` |
| the R2 copy's account | `r2_sa`, else `sa` | `sa` / hardcoded |
| the R2 copy's bucket | `r2_bucket` | hardcoded / `R2_BUCKET` |
| the R2 copy's key (+ endpoint) secrets | `r2_secrets` (`key_id`, `secret`, optional `endpoint`) | `gcs-static-index-r2-*` as `AWS_*` / cw's three as `R2_*` |

The profile is `$STATIC_NAMES_PROFILE`, default `job/static-names.json` beside the script (each deployment's existing file). Every task gets the resolved profile inline in `STATIC_NAMES_PROFILE` (added to `ENV_JSON`'s variables unless set), so a task reads the checkout's profile, not whatever copy its image holds. Launcher-only settings go in an optional `job/static-names.env` (sourced; `$STATIC_NAMES_ENV` names another). The R2 endpoint, when `r2_secrets` names no secret for it, is `$R2_ENDPOINT` or `$CLOUDFLARE_ACCOUNT_ID`'s, as before.

Behaviour changes, each deliberate:
- gcs's `r2-batch` passes its key as `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` (was `AWS_*`; `r2-copy` reads either), and mounts no scratch bucket.
- `resume-after` reads the profile from its task env (was the image's `job/static-names.json`), runs in the profile's region (was `us-east1`), and passes `GCP_PROJECT` plus `R2_ENDPOINT` / `CLOUDFLARE_ACCOUNT_ID` from the launcher's environment (were gcs's literals).
- cw's tasks gain the drill / anchors exit diagnostics, `resume-after`, and cost labels from `dt-cloud cost-labels` (was a literal `deployment: cw` label).

| Deployment | Add | Delete |
| --- | --- | --- |
| gcs | `job/static-names.env`: `IMAGE=us-central1-docker.pkg.dev/oa-internal-450019/cloud-run-source-deploy/gcs-usage-snapshot@sha256:6974a8b71e56469bb8180495c13c170decdc10aefe695cd0f8c5200fb3ac520a` (the 2026-10-08 pin; drop it to follow the profile's `image`) | `job/static-names.sh` |
| cw | `job/static-names.env`: `export DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw}` | `job/static-names.sh` |
| hf, r2 | nothing (they build search in-job: `static-names add`) | nothing |

Both profiles (`job/static-names.json`) already carry every field above; no edit. `DATA_BUCKET` / `STATIC_NAMES_SCRATCH` / `R2_BUCKET` / `SA` env overrides on cw become the profile's own env vars (`STATIC_NAMES_BUCKET`, `STATIC_NAMES_SCRATCH`, `R2_BUCKET`, `STATIC_NAMES_SA`) or `SA`.

## 3. `site/src/oaPeers.ts` → the `PEERS` var

On `cloud`: `site/src/peers.ts` (`parsePeers`, tested) reads the deployment's `PEERS` var, a JSON array of `Peer`s in a TOML literal string under `[vars]`, at build time (`vite.config.ts`, which now reads `'…'` literal strings as well as `"…"` ones). A malformed list fails the build, naming the entry. The site's peers are the store's `peers` when set, else `PEERS`; unset, none (the generic default). `wrangler.example.toml` documents it.

Every OA deployment (gcs, cw, hf, r2) makes the same switch:
1. Add to `site/wrangler.toml` `[vars]` (the list may name the deployment itself; its own prod host is skipped):
   ```toml
   PEERS = '[{"label": "GCS usage", "prod": "gcs.oa.dev", "dev": "dev.gcs.oa.dev", "owners": true, "staging": true, "meta": true}, {"label": "CoreWeave usage", "prod": "cw.oa.dev", "dev": "dev.cw.oa.dev", "staging": true, "meta": true}, {"label": "Hugging Face usage", "prod": "hf.oa.dev", "dev": "dev.hf.oa.dev", "meta": true}, {"label": "R2 usage", "prod": "r2.oa.dev", "dev": "dev.r2.oa.dev", "meta": true}]'
   ```
2. Drop `peers: OA_PEERS` and the `import { OA_PEERS } from '../oaPeers'` from its store (`site/src/stores/gcs.tsx`, `cw.tsx`, `hf.tsx`, `oa-r2.ts`).
3. Delete `site/src/oaPeers.ts`.

The list is still written four times, once per deployment's config: a peer added or renamed is a one-line config change on each branch, with no code change and no merge conflict.

## 4. `infra/cf/run`

On `cloud`: `infra/cf/run`, cw's and hf's byte-identical wrapper (`pulumi -C infra/cf` with `$CF_IAC_TOKEN`, trimmed, as `CLOUDFLARE_API_TOKEN`). Generic as it was: no config.

| Deployment | Add | Delete |
| --- | --- | --- |
| cw, hf | nothing | `infra/cf/run` (identical: the merge takes `cloud`'s) |
| gcs, r2 | nothing (available if they want it; needs `CF_IAC_TOKEN` in their environment) | nothing |

## 5. The scan job image: `job/build.sh`, `job/Dockerfile`, `dt-cloud image`

On `cloud`:
- `job/Dockerfile`: one image for every deployment. The differences are build args: `SITE` (`build`: the static site into `/app/dist`, gcs and cw; `none`), `APT_PACKAGES`, `UV_SYNC_ARGS` (the `dt-cloud` extras), `ENTRYPOINT` (the container's command, its args appended: run via `bash -c 'exec $DISKY_ENTRYPOINT "$@"'`, so it is PID 1). A Batch spec that sets its own `entrypoint` (r2's scheduled heads set `dt-cloud`) bypasses the wrapper; it applies only to submits that don't. The stamp (`DISKY_GIT_SHA` / `DISKY_GIT_DIRTY` → OCI labels `org.opencontainers.image.revision`, `disky.dirty` and env) is the build's own; a config can't set it. `DT_NF_DIR=/app/job/nf` everywhere (r2 set it; it is where every image has the pipeline).
- `dt-cloud image build` / `dt-cloud image promote` (`cloud/src/dt_cloud/image_build.py`, tests `cloud/tests/test_image_build.py`), configured by the deployment's `job/build.json`. `job/build.sh` is `dt-cloud image build -c job/build.json "$@"` (`job/build.sh promote TAG` for the other). No `cloudbuild.yaml`: the Cloud Build config is generated per build into the staged context.

What a build does:
1. Refuse a cwd in another checkout (`-O` overrides): the image is the config's checkout. Refuse a tree with uncommitted changes to tracked files unless `-D` (and never where `allow_dirty: false`).
2. The candidate tag: the commit's first 12 hex chars; a dirty build's adds `-dirty-<UTC time>`; `-t TAG` names another (never a live tag). `-a KEY=VALUE` overrides a build arg and requires `-t` (it changes the image without changing the commit). If the primary repository already has the tag, nothing is rebuilt.
3. Stage the context: `git archive <sha> -- <context pathspecs>` (dirty: the worktree's tracked files) under `tmp/image-build-<time>/`, with an empty `.gcloudignore` and the generated `cloudbuild.json`; `gcloud builds submit` it. One docker step builds `dockerfile` stamped, tagged `<repository>:<tag>` and `<mirror>:<tag>` for each mirror; Cloud Build pushes them all. With `cache`, `--cache-from` the first live tag's image (inline cache).
4. Print `<repository>@<digest>` per repository, primary first (stdout; logs go to stderr).
5. `mode: live` (or `-m live`) only: promote.

`promote TAG [-x sha256:…]`: in every repository, move each of `live_tags` to the digest `TAG` names there (`gcloud artifacts docker tags add`), then re-point `repoint`'s Cloud Run jobs and services at `<repository>:<first live tag>` (each one missing is skipped). `-x` refuses unless the primary digest is the reviewed one.

So the release spec's scanner lane (`specs/oa-release-workflow.md`, "Four release lanes") is `job/build.sh -m candidate` (an immutable tag, its digest printed, nothing live moved, nothing re-pointed), a smoke against `<repository>@<digest>`, then `job/build.sh promote <tag> -x <digest>`. `mode: live` keeps today's one-step behaviour where a deployment wants it.

`job/build.json` fields: `project`, `repository` (no tag) required; `mirrors`, `mode` (`candidate` default | `live`), `live_tags`, `build_args`, `context` (git pathspecs, default `.`), `dockerfile` (default `job/Dockerfile`), `cache`, `allow_dirty` (default true), `cloudbuild` (`region`, `service_account` email, `staging_dir`, `machine_type`, `timeout`, `logging`, `tags`), `repoint` (`region`, `jobs`, `services`). Unknown keys are errors.

### gcs
`job/build.json`:
```json
{
 "project": "oa-internal-450019",
 "repository": "us-central1-docker.pkg.dev/oa-internal-450019/cloud-run-source-deploy/gcs-usage-snapshot",
 "mirrors": [
  "us-east1-docker.pkg.dev/oa-internal-450019/gcs-usage/scan"
 ],
 "mode": "live",
 "live_tags": [
  "latest"
 ],
 "cache": true,
 "build_args": {
  "SITE": "build",
  "APT_PACKAGES": "time",
  "ENTRYPOINT": "dt-cloud scan-run supervise -- bash job/run.sh"
 },
 "context": [
  "pyproject.toml",
  "README.md",
  "uv.lock",
  "src",
  "cloud/pyproject.toml",
  "cloud/src",
  "job",
  "site",
  "packages",
  "pnpm-workspace.yaml",
  "package.json",
  "pnpm-lock.yaml"
 ],
 "cloudbuild": {
  "machine_type": "E2_HIGHCPU_8",
  "timeout": "1800s"
 },
 "repoint": {
  "region": "us-central1",
  "jobs": [
   "gcs-usage-snapshot-submit"
  ],
  "services": [
   "gcs-alert-relay"
  ]
 }
}
```
Keeps: `:latest` pushed to both repositories (the us-east1 mirror that `access-submit.sh` pulls), the Cloud Run job `gcs-usage-snapshot-submit` and service `gcs-alert-relay` re-pointed, both only in `live` mode or on `promote`; a `-m candidate` build does neither. New: each build also leaves its immutable `<sha12>` tag in both repositories (the mirror's `:latest` moved only on promote, as the primary's). Delete: root `Dockerfile`, `cloudbuild.yaml`, `job/build.sh` (take `cloud`'s). `.gcloudignore` stays only if another `--source` upload uses it; the image build no longer reads it.

### cw
`job/build.json`:
```json
{
 "project": "oa-internal-450019",
 "repository": "us-central1-docker.pkg.dev/oa-internal-450019/cw-s3-usage/scan",
 "mode": "live",
 "live_tags": [
  "latest"
 ],
 "cache": true,
 "allow_dirty": false,
 "build_args": {
  "SITE": "build",
  "APT_PACKAGES": "time git",
  "UV_SYNC_ARGS": "--extra overtime",
  "ENTRYPOINT": "dt-cloud scan-run supervise -- bash job/cw-run.sh"
 },
 "context": [
  "pyproject.toml",
  "README.md",
  "uv.lock",
  "src",
  "cloud/pyproject.toml",
  "cloud/src",
  "job",
  "site",
  "packages",
  "pnpm-workspace.yaml",
  "package.json",
  "pnpm-lock.yaml"
 ],
 "cloudbuild": {
  "machine_type": "E2_HIGHCPU_8",
  "timeout": "1800s"
 }
}
```
Keeps: committed source only (`allow_dirty: false`; was `scripts/require-clean`, which also refused untracked files, which a `git archive` context can't contain anyway), the frozen `git archive` upload. `JOB=<script> job/build.sh` becomes `job/build.sh -t <tag> -a 'ENTRYPOINT=dt-cloud scan-run supervise -- bash job/<script>'`; `TAG=x` becomes `-t x`. The cleanup policy (`IMAGE_DELETE_AFTER_S`: keep `latest*` and the newest 10) bounds the candidate tags. Delete: root `Dockerfile`, `cloudbuild.yaml`, `job/build.sh`.

### hf
`job/build.json`:
```json
{
 "project": "oa-internal-450019",
 "repository": "us-central1-docker.pkg.dev/oa-internal-450019/hf-usage/scan",
 "mode": "live",
 "live_tags": [
  "hf"
 ],
 "build_args": {
  "UV_SYNC_ARGS": "--extra s3"
 },
 "context": [
  "pyproject.toml",
  "README.md",
  "uv.lock",
  "src",
  "cloud/pyproject.toml",
  "cloud/src",
  "job",
  "site/wrangler.toml"
 ]
}
```
Keeps: the minimal context (no site build), `:hf` as the live tag. New: candidates; the image now has the full `job/` and `/app/dist` (empty). Delete: `job/Dockerfile`, `job/cloudbuild.yaml`, `job/build.sh` (take `cloud`'s).

### r2
`job/build.json`:
```json
{
 "project": "oa-internal-450019",
 "repository": "us-west1-docker.pkg.dev/oa-internal-450019/oa-r2-usage/scan",
 "build_args": {
  "UV_SYNC_ARGS": "--extra s3 --extra overtime",
  "ENTRYPOINT": "dt-cloud scan-run supervise -- python job/r2-scan run"
 },
 "context": [
  "pyproject.toml",
  "README.md",
  "uv.lock",
  "src",
  "cloud/pyproject.toml",
  "cloud/src",
  "job",
  "site/wrangler.toml"
 ],
 "cloudbuild": {
  "region": "us-west1",
  "service_account": "oa-r2-usage-builder@oa-internal-450019.iam.gserviceaccount.com",
  "staging_dir": "gs://oa-r2-usage-scratch/builds/source",
  "logging": "CLOUD_LOGGING_ONLY",
  "timeout": "1200s",
  "tags": [
   "disky",
   "oa-r2",
   "scan-image"
  ]
 }
}
```
r2 already builds candidates and pins digests (`job/r2-scan build TAG`, then `plan`/`run` with `<repository>@sha256:…`), so it has no live tags and no promote: `job/build.sh` (or `-t TAG`) replaces `r2-scan build`; its printed `<repository>@<digest>` is what `job_spec` accepts. Delete: `job/Dockerfile`, `job/cloudbuild.yaml`, `r2-scan`'s `build` subcommand, `build_command` and `source_revision` (and `test_r2_scan.py`'s cases for them). Its `image_repository` / `builder_account` / `scratch_bucket` stay in `job/r2.json` for `plan`.

### Not made generic
- Artifact Registry only: digests come from `gcloud artifacts docker images describe`, and promote moves tags with `gcloud artifacts docker tags add` (no image copy, so each mirror needs its own candidate tag, which the build pushes). Every deployment's registry is Artifact Registry today.
- Cloud Run re-pointing is the only consumer update; other consumers (Batch cron bodies, Nextflow profiles) follow a live tag or pin a digest by their own config.
- The image's content differs from today's per deployment in small ways: one apt layer for all packages (cache cold once), the `bash -c` entrypoint wrapper, wrangler's `npx` link on hf and r2, an empty `/app/dist` on hf and r2.

## Adoption status (2026-11-13)

| Deployment | Merged `cloud` | Candidate built | Smoke | Live |
| --- | --- | --- | --- | --- |
| hf | `0fc42d96` | `0fc42d96db75` | scratch scan, all 7 Batch tasks on the candidate | promoted to `:hf` |
| r2 | `c5ed8d428` | `c5ed8d42863d` | scratch head, 21 min | daily heads pinned (`1896bfe3e`, `pulumi up -s r2`) |
| cw | `506747fd9` | rebuilding | manual scan on the candidate digest, then promote | pending |
| gcs | `76c64dc66` | pending | container-level; a full scan writes a real scan (no scratch profile) | pending |

**A candidate smoke covers the step tasks only when the submitter forwards `DT_SCAN_IMAGE`** on manual submits (hf `a2457745`, gcs `b1e430fcc`; cw's already did). Without it the head runs the candidate and the steps run the live tag. The cron bodies stay byte-identical.
