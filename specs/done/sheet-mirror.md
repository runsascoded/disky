# Sheet mirror — opt-in "tabular data → Google Sheet tab, on a schedule"

**Status (2026-09-28): done** on `cloud` (branch `sheet-mirror`) — see [What shipped](#what-shipped) for the deviations from the proposal below. Not yet adopted by any deployment. (Proposed from the gcs session; base work, for the root session.) Ryan: "it can just be an optional thing each app opts into or not, specifying tabular data that updates on a schedule, and providing a nice way to update a gsheet where the Version History looks nice." Defaults and stubs were left to the author's judgment; the choices below are that judgment.

## Why now

gcs's `sheet-sync/` (hourly `/users` "who still needs to mark" → Percy's sheet) was hard-wired to marks. It had been failing silently since **2026-09-07** (its `dt-cloud report` read a per-scan `tree.json` the newer scans no longer publish), and the marks demolition removes its data source outright. Its Scheduler trigger `gcs-sheet-sync-hourly` is **paused** (2026-09-28); the Cloud Run job, image, grant token (`gcs-sheet-sync-token` in Secret Manager) and the sheet itself are left in place. gcs drops `sheet-sync/` from its delta. Nothing is lost that this spec doesn't replace.

## Shape

An app opts in by declaring **mirrors**; with none declared, nothing is built, deployed, or scheduled. No deployment enables one by default.

```yaml
# <deployment>/sheet-mirror.yml  (gcs: next to site/wrangler.toml; absent = off)
site: https://gcs.oa.dev            # the deployment's own API
token_secret: gcs-sheet-sync-token  # read-only grant, Secret Manager
schedule: "5 * * * *"
mirrors:
  - sheet: 1k_11LA…RFc               # the Google Sheet key
    tab: Storage by user
    source: owners                   # a named exporter (below)
    key: user                        # stable row identity (sort + diff)
    footer: "⟳ Auto-synced hourly from {site}/users — edits are overwritten; add derived views in another tab"
```

### Sources: named exporters over the live API, not new queries

`dt-cloud export <source> -u <site> [-d <scan>] -o out.csv` — each source is a thin reader of an endpoint the site already serves, with a fixed column contract:

| source | endpoint | columns |
|---|---|---|
| `owners` | `GET /api/owners?date=<latest>` | `user, bytes, objects, <storage-class mix…>` |
| `staged` | `GET /api/plans/staged` | `prefix, staged_by, staged_at, note, bytes` |
| `runs` | `GET /api/sweep/jobs` (gcs) / plan-sweep runs (cw) | `run, mode, by, started, state, deleted_bytes` |

Keep it to these three to start; adding a source is one function plus its column list. A `json:<path>[,<jq-ish field list>]` escape hatch is tempting but is exactly the generic query layer to avoid until a second app wants something the named ones don't cover.

### Writer: `sheet-push`, made key-aware

`sheet-push` already has the Version-History properties: cell-level diff against the tab, only changed cells written, numeric normalisation (`0.0` vs `0`), a footer "last change" stamp that advances only when data changes, other tabs untouched. Two changes:

1. **Stable order by `key`** (`-k/--key <column>`), with the existing rows' order preserved and new keys appended at the end. Today the diff is positional, so one user added mid-table shows every later row as changed; key-aware placement makes an addition one new row and a removal one cleared row (compacted only when the tab is otherwise unchanged).
2. **Docstring/names drop "mark-status"**; the command is the generic CSV → named-tab writer.

Everything else stays: named tab only (`-w`), ADC as the job SA (Editor on the sheet), `-n` dry run.

### Runner: one template job, per-deployment config

Move `sheet-sync/{Dockerfile,build.sh,deploy.sh,sync.sh}` to the base as `deploy/sheet-mirror/`, parameterised entirely by the YAML above: `sync.sh` loops over `mirrors`, runs `export` then `sheet-push -k` per entry. `deploy.sh <config.yml>` upserts the Cloud Run job + Scheduler + the two IAM bindings (secret access, run.invoker), idempotently, as today. **Drop** the healthcheck piggyback: `health.yml` on the default branch now checks both hosts hourly.

## Stubs to ship

- `deploy/sheet-mirror/example.yml` — one commented `owners` mirror; no real sheet id.
- `dt-cloud export --list` — prints the sources and their columns.
- Tests: exporter column contracts against recorded endpoint JSON; `sheet-push` key-aware placement against a fake worksheet (append, remove, reorder-stable, no-op writes nothing).

## Adoption

- **gcs:** once this is on `cloud`, a `sheet-mirror.yml` with one `owners` mirror into Percy's sheet (a new tab, "Storage by user", leaving the old "mark status" tab for him to delete), re-run `deploy.sh`, then unpause/replace the trigger. Reuses the existing grant token.
- **cw-s3:** opt in or not; `staged` + `runs` are the natural pair.

## What shipped

- **`dt_cloud.sheet_mirror`** — the exporters (`SOURCES`), the writer's plan (`plan_sheet` / `push`), and the config (`load_config`, `plan_lines`, `env_lines`). `dt-cloud export <source> -u <site> [-d <scan>] [-s <subdir>] [-e <executor>] [-o out.csv]`, `export --list`. Reads go through `site.creds` / `site.get_json`; `SiteError` now carries the HTTP `status` (a staged prefix that 404s in the subtree API is a legitimate blank, anything else propagates). The latest scan comes from `/data/<subdir>/scans.json` (`-s`, default `$SNAPSHOTS_SUBDIR`, as `healthcheck`).
- **Column contracts, as shipped:**
  - `owners`: `user, bytes, standard, nearline, coldline, archive` — biggest first. **Deviation:** no `objects` column: `/api/owners` carries per-user bytes + class mix only (`UserOwned = {b, mix}`), objects exist only at the top level. The "storage-class mix…" is fixed to the four GCS classes (`CLASS_NAMES`), so the contract doesn't vary by scan; an unknown class id raises.
  - `staged`: `prefix, staged_by, staged_at, note, bytes` — oldest first (so new stagings append). **Addition:** plan items carry no size, so `bytes` is one `GET /api/subtree?date=<scan>&path=<prefix>&depth=1` per item (the prefix's total in the latest scan; blank when the prefix isn't in it). No open plan → a header-only CSV.
  - `runs`: `run, mode, by, started, state, deleted_bytes` — oldest first. **The executor choice:** explicit `-e sweep|plan-sweep` (config: `executor:`), required, never guessed — `Store.executor` is baked into the SPA build and not served, and *both* route families exist on every deployment (each filters Batch jobs by its own `gcs-sweep-*` / `cw-sweep-*` prefix), so the wrong one would silently answer an empty list. Rows are the executor's `GET /api/<executor>/jobs` (live Batch state; the 20 most recent), joined to the D1 `deletion_runs` rows `/api/plans/staged` returns (`SELECT *`, so schema-agnostic across gcs/cw) for `by` (gcs's job env `USER`, else the run's `actor`) and `deleted_bytes` (would-delete for dry runs; blank until the executor has written its row). Join key: gcs records a run under `<date>-p<plan>/<stamp>` with the job's run dir as `plan` (`= /api/sweep/jobs`'s `plan`); cw records it under the job id. Only the *open* plan's runs are joinable — a closed plan's runs show with blank `deleted_bytes`.
  - Times are `YYYY-MM-DD HH:MM` (UTC).
- **`sheet-push`** — generic CSV → named-tab writer; mark-status naming/docstring gone. `-k/--key`: `place` keeps existing rows at their positions by key, clears a removed key's row (a hole), and appends new keys in source order; holes are compacted away only on a run whose data block is otherwise unchanged, and that compaction doesn't advance the "last change" stamp. The footer is recognised by its stamp (the last non-blank row), with exactly one separator row above it, so trailing holes survive until compaction. Everything else kept (cell-level diff, numeric normalisation, stamp advancing only on data change, `-n`). Two behaviour changes: `-w` is now **required** (named tab only — no first-tab default), and a header-only CSV is accepted (an empty `staged` set clears the tab's rows). `data_changed` now also counts rows that disappeared from the bottom of the block (previously only the new table's rows were compared).
- **Config** — the proposed shape plus an optional `subdir:` and a `gcp:` block (`project`, `service_account`, `job`, optional `region` [us-central1], `image` [`<region>-docker.pkg.dev/<project>/cloud-run-source-deploy/<job>:latest`], `trigger` [`<job>-trigger`]) — what `build.sh` / `deploy.sh` need, instead of hard-coded project defaults in a public repo. Validation: known source, `key` among its columns, `executor` iff `runs`, no two mirrors on one `(sheet, tab)`, unknown keys rejected. `{site}` in `footer` expands. `dt-cloud sheet-mirror plan <config|->` prints one line of `shlex.quote`d `source= site= subdir= sheet= tab= key= footer= executor=` assignments per mirror (`sync.sh` `eval`s each); `sheet-mirror env` prints the deploy variables.
- **`deploy/sheet-mirror/`** — `Dockerfile` (installs the root `disk-tree` from source first: `dt-cloud`'s path dependency on it is uv-only), `cloudbuild.yaml`, `build.sh <config>` (Cloud Build from a minimal staged context rather than the whole repo), `deploy.sh <config>` (job upsert with the config as `$SHEET_MIRROR_CONFIG_B64`, secret + `GCS_USAGE_TOKEN`; trigger create-or-update; the two IAM bindings), `sync.sh [config]` (loop; one failing mirror doesn't stop the rest, the job exits non-zero if any failed), `example.yml` (one `owners` mirror, placeholders only), `README.md`. Healthcheck piggyback dropped.
- **Tests** (`cloud/tests/test_sheet_mirror.py`): each source's CSV exactly, over canned endpoint JSON shaped after the Functions' responses (`cloud/tests/fixtures/sheet_mirror/{gcs,cw}.json` — synthesized, not recorded from prod); key-aware placement against a fake worksheet (append, remove → one cleared row → compaction on the next unchanged run, reorder-stable, positional contrast, no-op writes nothing); the config helpers and their validation errors; `example.yml` parses.

Open: no deployment has adopted it (gcs's steps under Adoption stand, with the `gcp:` block filled in; `trigger: gcs-sheet-sync-hourly` would retarget the paused trigger, which `deploy.sh`'s `update` leaves paused — `gcloud scheduler jobs resume` it, or let a new `<job>-trigger` replace it and delete the old one); nothing here was run against GCP, Sheets or a live site.
