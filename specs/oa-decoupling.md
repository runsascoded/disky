# OA decoupling: OA-specific behavior leaves `cloud`, as deployment config

**Status (2026-10-03):** steps 1–17 done on `cloud`, plus 18's names, comments and package name. dt-cloud carries no OA default (only a credit line in `usernames.py` mentions Marin).
- **Next:** gcs and cw-s3 rebuild their images (one build each; their step-0 scripts cover every key), then switch to the renamed env vars at leisure. Old names (`GCS_USAGE_TOKEN`/`_URL`, `CW_BUCKET`/`_ENDPOINT`) are accepted for one release. After that, the site's job specs (`planDispatch`, `sweepDispatch`, `cwBatch`) switch to the new names too.
- **Left:** the test fixtures' Marin-shaped bucket names (examples); the secret names (Ryan: OK as they are).

## Goal

`cloud` keeps the shared abstractions and their end-to-end examples. Every value that makes shared code behave like one particular deployment becomes a config key that the deployment sets: a default bucket, a GCP project, a staff domain, a bucket→region map, a repo link, a registry entry. After this, `cloud..gcs` and `cloud..cw-s3` are each a small diff, mostly config.

An audit of `cloud`'s source (2026-10-03) found 237 OA/Marin mentions:

| Class | Count | Treatment |
|---|---|---|
| Behavior (defaults, constants, registry entries) | 91 | Replaced by config keys (below) |
| Move | 18 | The OA store-registry entries, the About text, a screenshot config |
| Cosmetic (comments, help text, examples) | 114 | Reworded where they name people or read as OA config; the rest are fine as examples |
| OK | 14 | Package names, credits, compatibility notes |

## Rules

1. **Branch config lands first, as a no-op.** Each deployment sets every key to today's value before `cloud` removes the default.
2. **A removed default fails loudly.** An unset key gives a 503 "not configured" or a `SystemExit` naming the key, never a silent fallback to someone else's resources.
3. **One concern per `cloud` commit,** each naming the keys it now requires.
4. **Values never appear on `cloud`.** Not in code, specs or commit messages; the branches hold them.

## Keys

**Site** (`site/wrangler.toml` on the deployment branch; runtime vars in both `[vars]` and `[env.preview.vars]`):

| Key | Replaces | Unset |
|---|---|---|
| `STAFF_DOMAIN` | the `auth.ts` fallback domain | no staff domain |
| `ROOT_LABEL` | the `view.ts` fallback label | a generic label |
| `STORE_BUCKET` | `index.ts`'s default index bucket | store not ready (503) |
| `DATA_BUCKET` | the sweep/plan-sweep data bucket in `sweepDispatch`, `cwBatch`, `api/sweep/*` | dispatch routes 503 |
| `GCP_PROJECT` | `gcp.ts`'s project (falls back to the SA key's `project_id`) | 503 |
| `BUCKET_REGIONS` (JSON) | `gcp.ts`'s bucket→region map | every job in `BATCH_REGION` |
| `SWEEP_IMAGE`, `CF_ACCOUNT_ID` | the sweep job's image and account | dispatch 503 |
| `SWEEP_S3_ENDPOINT` | `cwBatch`'s S3 endpoint | plan-sweep 503 |
| `D1_DB_ID`, `D1_DB_NAME` | (new, 10-03) the D1 the `sweep` executor's jobs record their runs to | sweep dispatch and undo 503 |
| `REPO_URL` (build) | the GitHub link in `SiteKbd`/`SiteNav` | this repo |
| `STORE_SCHEME` / `STORE_BUCKETS` (existing) | the plans' default bucket shape, the `marin-` regexes in `actions.ts`, `sweepDispatch`, `api/sweep/jobs.ts` | plans routes refuse |

**Store registry:** `site/src/stores.ts` loads `site/src/stores/*.ts` modules (`import.meta.glob`); `cloud` keeps `r2.ts` (and any generic examples), and each deployment branch adds its own entry module. New optional `Store` fields: `contact` (attribution-rules contacts) and `about` (the About text). `STORE` becomes required at build. `TypedPrefix`, `TokenModal` and the peer-deployment omnibar link read the store.

**dt-cloud** (the deployment's job scripts, or the operator's env):

| Key | Replaces |
|---|---|
| `DATA_BUCKET` | every `oa-…` data-bucket default in `cli.py`, `index_footer`, `publish`, `sweep`, `digest`, `batch` |
| `SITE_URL` (then `GCS_USAGE_URL`) | `site.py`'s default site |
| `GCP_PROJECT`, `JOB_SA`, `JOB_IMAGE`, `LISTING_REGIONS` (JSON) | `batch.py` / `gcp.py` constants; `submit-listing -b` becomes required |
| `ACCESS_LOG_BUCKET`, `ACCESS_BUCKETS` | `access.py`'s log bucket and fleet |
| `SII_BUCKETS`, `WANDB_ENTITY` (+ `--run-root`) | operator-CLI defaults |
| `WARM_PATHS` | `warm.py`'s fleet paths (default: the scan's top-level buckets) |
| `CW_BUCKET`, `CW_ENDPOINT` | `sweep.py` defaults (required) |
| digest config file (`-C`) | the OA hosts and quotas in `digest.py`'s presets |

**Tooling:** `site/deploy`, `site/dev` and `site/cf-status` source an optional per-branch `site/deploy.env`. The pre-push guard reads git config. Playwright and `subtree-check` take `BASE_URL` and derive their cases from the deployment.

## Sequence

0. **Branches (no-op):** gcs and cw-s3 add every key above with today's values: `wrangler.toml`, the image-baked job scripts (not `batch-submit.sh`, whose cron body would need a `pulumi up`), `site/deploy.env`, local `.envrc`.
1. `STAFF_DOMAIN` without a default.
2. Generic `ROOT_LABEL` fallback.
3. `STORE_BUCKET` required.
4. Plans: the bucket shape from `STORE_SCHEME`/`STORE_BUCKETS` everywhere.
5. Sweep executor config from env, with a golden test: the Batch spec built under each branch's env equals today's.
6. The store registry: (a) the glob loader, inline entries kept and modules winning by key; branches add their modules; (b) the inline OA entries removed, `STORE` required.
7. Unfurl titles from the registry.
8. `REPO_URL`.
9.–16. dt-cloud keys, in the table's order.
17. Tooling.
18. Cosmetic sweep, including people's names in source and tests.

Each `cloud` step merges into gcs and cw-s3 and deploys to their dev stacks (`site/deploy --dev`) before prod. m3 (via `local`) is affected only by step 6: its `laptop` entry becomes a module on `local`.

## Open

- cw-s3's About text: generic, or its own.
- Whether secret-name defaults count as OA-specific or as neutral names.
- Renaming the `CW_*` / `GCS_USAGE_*` env vars (needs job-spec edits).
