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
