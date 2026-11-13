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
