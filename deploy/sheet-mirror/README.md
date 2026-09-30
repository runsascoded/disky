# `sheet-mirror` — tabular site data → Google Sheet tabs, on a schedule

An opt-in, per-deployment job that keeps named tabs of a Google Sheet mirroring tables the site already serves (`/users` ownership, the `/staged` deletion plan, recent deletion runs), refreshed on a Cloud Scheduler cron, with no human re-export. With no config, nothing is built, deployed or scheduled. Spec: [`specs/done/sheet-mirror.md`][spec].

## Config

One `sheet-mirror.yml` per deployment (start from [`example.yml`]): the site, the Secret Manager secret holding a read-only grant token for it, the cron, the GCP project / service account / job name, and a list of **mirrors** — each a `(sheet, tab)` fed by one named `source` with a stable row `key`:

| source | endpoint | columns |
|---|---|---|
| `owners` | `GET /api/owners?date=<latest>` | `user, bytes, standard, nearline, coldline, archive` |
| `staged` | `GET /api/plans/staged` (+ `/api/subtree` per prefix) | `prefix, staged_by, staged_at, note, bytes` |
| `runs` | `GET /api/<executor>/jobs` ⋈ `/api/plans/staged` runs | `run, mode, by, started, state, deleted_bytes` |

`dt-cloud export --list` prints the same. A `runs` mirror names the site's `executor` (`sweep` on gcs, `plan-sweep` on cw-s3 — the site's `Store.executor`, which the API doesn't expose). `dt-cloud sheet-mirror plan <config>` validates a config and prints what the job will run.

## Chain (`sync.sh`, per mirror)

1. `dt-cloud export <source> -u <site>` → the source's CSV, read with the grant token (`$GCS_USAGE_TOKEN`, from Secret Manager).
2. `dt-cloud sheet-push -k <key> -w <tab> -D <footer>` → a cell-level diff into the one **named** tab, keyed by `key`: existing rows keep their order, a new key is one appended row, a removed key one cleared row (compacted on a later run whose data is otherwise unchanged). The footer's "last change" stamp only advances when data changes, so a no-op run writes nothing and Version History shows only real deltas. Other tabs (derived views people add) are never touched.

One mirror failing doesn't stop the others; the job exits non-zero if any did. The job runs **as** the config's service account: ambient ADC covers the Sheets write (share each sheet with it as Editor), no key material.

## Deploy

```bash
deploy/sheet-mirror/build.sh  <deployment>/sheet-mirror.yml   # Cloud Build → the config's image (default :latest)
deploy/sheet-mirror/deploy.sh <deployment>/sheet-mirror.yml   # Cloud Run job + Scheduler trigger + IAM
gcloud run jobs execute <job> --project <project> --region <region> --wait   # one-off test
```

Both read the config through `dt-cloud sheet-mirror env` (so `dt-cloud` must be on `PATH` locally). `deploy.sh` is idempotent: it upserts the job (the config rides in as `$SHEET_MIRROR_CONFIG_B64`), creates or updates the trigger (`<job>-trigger` unless `gcp.trigger` names one), and grants the service account the two roles the wiring needs (`secretmanager.secretAccessor` on the token, `run.invoker` on the job so the Scheduler can trigger it). A mirror change reaches the job by re-running `deploy.sh`; a code change, by re-running `build.sh`.

Run locally against a config (with ADC that can edit the sheets): `GCS_USAGE_TOKEN=… deploy/sheet-mirror/sync.sh <config.yml>`.

## The grant token

A read-only grant for the deployment's site (can read `/api/*`, can't stage or dispatch), stashed in Secret Manager under the config's `token_secret`. Revoke it by setting `revoked_at` on its `grants` row — independent of anyone's personal token.

[spec]: ../../specs/done/sheet-mirror.md
[`example.yml`]: example.yml
