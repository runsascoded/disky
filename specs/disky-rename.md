# Finish the rename: disk-tree → disky

The project is **disky** (GitHub `runsascoded/disky`, the macOS app, the menu bar). "disk-tree" survives in the CLI, the Python package, env vars, config/log dirs, tab titles, docs, and every cloud resource name. Ryan (2026-10-17): purge it from all branches and deployments; pay the debt down incrementally, in each deployment, without thrashing anything before the big presentation (~2026-10-19).

## Principles

- **New names everywhere; old names keep working until every deployment has moved.** Each rename lands with a fallback (alias, env-var fallback, dir fallback), and a later step removes it.
- **New deployments are born "disky".** Stack examples and defaults name new resources `disky-*` now. Existing stacks keep their names (pinned in each `Pulumi.<stack>.yaml`) until their own migration step.
- **Driven from `cloud`**, merged down (`cloud` → `local` → m3/app; `cloud` → gcs/cw-s3). Each deployment's spec lists the steps it still lacks as gaps.

## Steps (roughly in order; each is independently shippable)

### 1. User-facing (low risk, do first)
- **Tab titles / page text:** `disk-tree — <label>` → `disky — <label>` (`site/src/stores/*.ts`, `stores.ts` `hostStore`, OG card site names, `index.html`, sign-in wall copy).
- **Trash folder:** `~/.Trash/disky/` — **done** (`local` 56a7fe38).
- **Docs:** README, AGENTS.md/CLAUDE.md per branch, specs going forward.

### 2. One `disky` command
Today there are two: the Python engine CLI `disk-tree` (on PATH via the venv) and the app binary `disky.app/Contents/MacOS/disky` (`scan now`, `job scan|drain`, not on PATH). Merge them into one command a person types:
- The **app binary is the front door** (it is what an install ships, e.g. the M1 has no engine venv): `disky <cmd>` handles its own subcommands (`scan`, `job`, `connect`, …) and passes engine subcommands (`du`, `trash`, `reclaim`, `overcount`, `repos`, `dispatch`, …) to the engine. A bonus is that engine commands run as the app's children, so they inherit its Full Disk Access (`disky trash restore` can read `~/.Trash`).
- The app installs a `disky` symlink on PATH (`~/.local/bin/disky` or similar).
- The engine's console script becomes `disky-engine` (or similar; the app finds it via `disky.json`), with `disk-tree` kept as an alias until the LaunchAgents' and drainer's command lines move.
- Open question for the app session: how the app locates the engine (the `disky.json` `jobs` already name the venv's python).

### 3. Env vars and config/log dirs (with fallbacks)
- `DISK_TREE_*` → `DISKY_*`: one helper reads `DISKY_X` and falls back to `DISK_TREE_X` (warn once). Grep-driven, mechanical. Deployments' `.envrc` / job defs / secrets move at leisure.
- `~/.config/disk-tree/` → `~/.config/disky/`: read the new dir, fall back to the old; a `disky migrate-config` (or first run) moves it. `disky.json` already lives there.
- `~/Library/Logs/disk-tree/` → `~/Library/Logs/disky/` (the app writes these; app session).

### 4. Code identifiers (mechanical)
- Python package `disk_tree` → `disky` (dist name `disk-tree` → `disky`); `dt-cloud` may stay or become `disky-cloud`.
- npm `@disk-tree/react` → `@disky/react` (dist branch `dist/react` frozen legacy already; `@rdub/treemap` is unaffected), site package name.
- Do this in one commit on `cloud` (pure rename + import rewrite), right after a round of merges so deployment branches carry few deltas; git rename detection carries the rest.

### 5. Cloud resources (last; mostly invisible)
- **New deploys:** example configs (`infra/*/stack/Pulumi.stack.example.yaml`, `site/wrangler.example.toml`) default to `disky-*`.
- **Pages → Workers + Assets:** any deployment that has to make a new site project anyway makes a Worker with static assets (the project's default architecture, dOGI-friendly), not a new Pages project. m3: `disk-tree` / `disk-tree-dev` Pages → a `disky` Worker (+ dev), moving disk.rbw.sh / dev.disk.rbw.sh. Downtime is acceptable (~0 DAUs).
- **Renamable via Pulumi** (recreate, cheap): Batch queue / job def, CloudWatch log group, the capture queue + trigger Worker.
- **Not renamable in place:** R2 bucket `disk-tree` (new bucket + copy + new scoped token), D1 `disk-tree-m3-db` / `disk-tree-demo-db` (export → import). Do these only alongside another migration of the same store, or leave.
- OA deployments (gcs, cw-s3) already use `gcs-usage`-style infra ids; their user-facing strings follow steps 1–4 via `cloud`.

## Sequence

The presentation (~10/19) sets a freeze: until then, only changes no deployment can notice break, i.e. strings. Everything that renames something a process, job, cron or another session looks up waits until after it.

| when | step | where | who |
|---|---|---|---|
| now | 1: titles / page text | `cloud` (demo title, desc, objects note), `local` (laptop + capture titles), deployment stores on their next merge | root, m3; each deployment's own store strings (hf: `stores/hf.tsx`) |
| now | 3a: the env helper | `cloud`: `env("X")` reads `DISKY_X`, falls back to `DISK_TREE_X` (warns once); every engine read goes through it. No deployment sets anything new yet | root |
| after 10/19 | 2: one `disky` command | app (`wt/app`) + engine console script `disky-engine`, `disk-tree` kept as an alias | app session |
| after 10/19 | 3b: deployments set `DISKY_*` | each deployment's `.envrc` / job defs / Batch env, at its own pace; config dir fallback `~/.config/disky` → `~/.config/disk-tree` | each session |
| after a merge round | 4: `disk_tree` → `disky` package rename | one pure-rename commit on `cloud` right after every deployment has merged it, so their deltas are small | root, announced to all sessions first |
| last | 5: resource names | new deploys born `disky-*` now (examples); existing stacks only alongside another migration of the same resource | each session |
| after 2–4 everywhere | remove fallbacks | aliases, `DISK_TREE_*`, the old config dir | root |

Docs (README, AGENTS.md) follow each step: prose says "disky" now; command lines say `disk-tree` until step 2 makes `disky` the command.

## Status
- 2026-10-17: spec written (from disky-m3). Step 1: trash folder done on `local` (56a7fe38), laptop/capture tab titles on `local` (f9053265), demo title/description on `cloud`. Step 3a on `cloud`: `disk_tree.env` (`get` reads `DISKY_X`, else `DISK_TREE_X`; children are handed `DISKY_X`); every engine read goes through it (`ROOT`, `SCAN_DIRS`, `R2_ENDPOINT_URL`, `PARQUET_CODEC`, `NOTIFY`, `BACKEND`, `SCAN_BATCH_ROWS`, `PARALLEL_FINALIZE_MIN_ROWS`). No deprecation warning yet: it would only add noise until 3b gives deployments a reason to switch. The config dir waits for 3b: `~/.config/disky` already exists (the app's `disky.json`) without a DB or scans, so a naive new-dir-first fallback would point the laptop at an empty root; it needs an explicit migrate.
