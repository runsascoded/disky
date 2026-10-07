# m3 on `site/`: the laptop as a store

**Status:** deployed since 2026-10-01; capture-trigger cutover verified 2026-10-02. Branch `m3`. The implementation notes below retain the original proposal and measurements.

## Current deployment (2026-10-05)

`site/` serves disk.rbw.sh and dev.disk.rbw.sh with in-app auth. disky.app captures the configured scope in-process to R2; `_SUCCESS.json` triggers the capture Worker, which submits the 1 vCPU / 8 GiB Batch ingest. The ingest publishes the path index and snapshot, then syncs D1 `disk-tree-m3-db`. The CLI reads the same index with `du -p`; there is no scheduled scan blob or Python capture wrapper. The drainer trashes real deletions and requests a refresh with `disky scan now`. Infrastructure lives in `infra/aws/` and `infra/cf/`; deployment uses `site/deploy-m3`.

The ingest carries the capture's APFS container measurements into `meta.json.disk_space` using `disk-tree capture-meta`. The site shows free bytes, capacity, percentage used, and a meter above the map at every drill depth. These are physical container measurements at the scan, shared across its volumes; they are independent of the treemap's apparent sizes. The tooltip explains APFS sharing and system volumes. Snapshots without a measurement omit the indicator; no capacity is inferred from their tree totals. The display follows the site's SI/IEC unit preference.

## Why

As a rule, a deployment ships one app, `site/`. gcs.oa.dev, cw-s3.oa.dev and r2.rbw.sh all run it. disk.rbw.sh is the exception: Pages project `disk-tree` is built from `ui/`, whose Pages functions (`ui/functions/`) re-implement the Flask server's `/api/scan` contract over raw scan blobs in R2. That is a second app, with a second auth (Cloudflare Access on `/auth/sso`, which `site/` and `@open-athena/auth` have already dropped). It also has a second copy of staged-delete and of the OG cards, and none of `site/`'s over-time, diff, owner or age features.

Goal: disk.rbw.sh runs `site/`, with this laptop as one more store, like `gcs`, `cw` and `r2`. `ui/functions/` is retired. The Flask app (`disk-tree-server` + `ui/`) is sunset as a deployment target (see "The Flask app" below).

## The two data models are layers, not rivals

- **Scan blob** (what `ui/` reads): hybrid chunked parquet plus a SQLite row, and a `.scan.json` manifest when the blob is remote. It is the *producer* format. It supports incremental rescans, chunk reuse across scans, and patching in fresher child scans. It is cheap to write from a laptop.
- **Snapshot library plus path index** (what `site/` reads):
  - per-store, per-date `snapshots/<store>/<date>/`;
  - a union path index sorted `(depth, path)`, with its footer in D1, so row groups of about 8k rows cost nothing to locate;
  - over-time groups and diff indexes.

  It is the *serving* format: edge-native, and the only one that answers series, diffs and ages.

For serving, the second is strictly better. The first stays as the laptop's write format, or is skipped entirely. The cloud stores never make scan blobs: they go listing shards → `dt-cloud path-index` → snapshot. The laptop can do the same with `disk-tree capture` (layer-1 listing shards, bounded memory, zero local disk, streamed to R2).

## Plan

### Where the ingest runs: AWS Batch in the RAC account
The laptop runs only the walk: `disk-tree capture` streams layer-1 listing shards to `r2://disk-tree/captures/…` with bounded memory and no local disk. Everything after that (path index, snapshot, D1 footer sync, generation GC) is a Batch job in the `r` profile's account (006196295121, us-east-1), which already runs `pyrmts-engine`, `nj-crashes` and `jct` on Fargate Spot. Reasons:
- reproducibility (a pinned image, not the laptop's venv);
- no memory pressure on the laptop;
- private CloudWatch logs; the repo is public, so GHA logs would be world-readable;
- room to grow up to 16 vCPU / 120 GB.

R2 egress is free, so reading the capture from AWS costs nothing extra. Pieces:
- ECR repo `disk-tree`: an image with `disk-tree` + `dt-cloud`.
- A Fargate Spot compute environment plus queue.
- Job definition `disk-tree-ingest`. Start at 4 vCPU / 16 GB, ephemeral storage sized after the spike.
- Secrets Manager entries: the R2 RW keypair for the `disk-tree` bucket, and a CF token for D1.
- Terraform: extend `iac/aws/`, which already declares a Fargate compute environment, queue and job definition for the delete executor and has never been applied.
- The laptop agent: `capture` → `aws batch submit-job`.

GHA (the `reduce.yml` precedent) stays the fallback.

### Phase 0: spike (does a 7.4M-row home scan fit `site/`'s model?)
1. Publish scan 136 (`/Users/ryan`, `c69fe255`, R2) into the snapshot model:
   - a listing from the blob's files, or a fresh `disk-tree capture /Users/ryan -t r2://disk-tree/captures`;
   - then `dt-cloud path-index -l … -P … -o …` into `tmp/`.
2. Measure:
   - wall time and peak RSS (the laptop is the constraint);
   - output sizes;
   - D1 footer row count.
3. Run `site/` locally over it: `site/./dev --local-db` with a `laptop` store stub. CIC `/`, `~/c`, `~/Library` at phone width.
4. Go/no-go. The main risk is path-index build memory on the laptop. The fallback is to reduce in the cloud (`.github/workflows/reduce.yml`, or node `mgu`).

**Phase 0 results (2026-09-29).** Capture `2026-09-29T16-26-51Z` of `/Users/ryan`, then Batch job `88feb8f2` on the `disk-tree-m3-ingest` job definition:

| step | where | wall | peak RSS | output |
|---|---|---|---|---|
| `disk-tree capture` | laptop | 197 s | 423 MiB | 33 shards, 145 MiB in R2, 0 local disk |
| fetch shards | Batch | 3 s | | |
| `dt-cloud path-index` | Batch | 6 s | 1.51 GiB | index 70 MiB (823,733 paths, 6,596,020 objects), snapshot 192 KiB |
| upload | Batch | 4 s | | `listing/laptop/2026-09-29/index/202609291811/`, `snapshots/laptop/2026-09-29/` |

Verdict: **go**. The ingest is trivially small for Batch, so the job definition can drop to 1 vCPU / 4 GiB. Remaining in Phase 0: run `site/` locally over this generation.

### Phase 1: the `laptop` store

**Status (2026-09-29):** done except auth (the Google client) and the deploy:
- `laptop` store in the registry, `site/wrangler.toml` = m3's config; local `site/` renders the home scan and drills (`/Users/ryan/c`). Three `[base]` fixes it needed (staff `BASE_SCOPE`, case-sensitive `/users/*`, leading-`/` roots) are on `cloud`.
- `cf/` (Pulumi, shared `CfnDashboard`) adopted the Pages project, domain and CNAME and created D1 `disk-tree-m3-db`; `migrations/cw` applied.
- The Batch ingest syncs footers to that D1 (token "disky m3 batch d1", D1 Edit only); the 12 h agent runs `aws/laptop-scan` (index for `ui/`, then capture → Batch).

- A `REGISTRY` entry in `site/src/stores.ts`:
  - `scheme: 'file'` and `rootLabel` `~`;
  - `prices: false`, `owners: false`, `staging: true`;
  - the wall copy.
- Local paths as treemap roots. Verify nothing assumes `<scheme>://bucket/`.
- `site/wrangler.toml` for this branch: R2 `disk-tree`, D1 (a new one, or migrate `disk-tree-auth`), `AUTH_MODE=app`, allowlist `ryan@runsascoded.com`.
- Auth: the `site/` Google OAuth client for disk.rbw.sh (a new client, or a redirect added to an existing one: **user step**), `GOOGLE_CLIENT_ID`/`SECRET` secrets, and optionally Resend for emailed codes.

### Phase 2: publishing
- The 12 h index LaunchAgent becomes `capture` (or `index`) plus path-index/snapshot publish plus D1 footer sync, as the ingest does for `r2`.
- An R2 keep-policy for old generations: roughly one generation per 12 h. Decide the retention.

### Phase 3: deletes go to the laptop

**Status (2026-09-29): built and running** — `[base]` commits `48961ba` (trash), `93df62b` (drainer), `be5d729` (the `laptop` executor + migration 0006), m3 wiring `23a705f`. Verified end to end against the live drainer: a dry run left a scratch folder alone, the real run renamed it into `~/.Trash/disk-tree/<run>/`, `disk-tree trash restore` put it back exactly. Not yet exercised through the browser (needs the Google client). Follow-ups: the `/staged` run row for a trashed run (its hold, an admin *Empty now* → the drainer; today `undo`/`purge` there call the GCP routes and 503 on m3); a push channel (Cloudflare Tunnel + service token) if one-poll latency ever matters; **package the agents as a macOS app** so TCC keys on a bundle identity instead of a Python binary (`[package-macos-app-for-tcc-identity]`).

**Design (2026-09-29).** `site/`'s staged-delete flow stays as is (stage from the map → `/staged` → Dry-run → Delete for real, admin-only); only the executor changes.

- **A `laptop` executor** (`Store.executor`, `_lib/executor.ts` `EXECUTOR_KINDS`): `prepare` reads the plan's `plan_items.prefix` rows (`file:///Users/ryan/…/`); `launch(date, digest)` inserts the `deletion_runs` row (`run_id = <plan>-<date>/<ts>`, `scan = date`, `head`/`exec_head` 0, no job) and returns — nothing is submitted anywhere. `refresh` reads back rows the drainer finished. `dateRe` = `YYYY-MM-DD` (a snapshot date). The `GCP_SA_KEY` check in `dispatchPlan` moves into the two executors that need it (`[base]`).
- **The drainer** (`disk-tree dispatch -s`, LaunchAgent `com.runsascoded.disk-tree.drain`) polls this D1 instead of `ui/`'s: `deletion_runs WHERE finished_ts IS NULL`, items from `plan_items.prefix` (it reads `ui/`'s `plan_items.uri` / `batch_job` today — make both column sets work). Dry runs size the set from the local filesystem and write `deleted_bytes` as would-delete; real runs execute, then write `finished_ts`, `deleted_bytes`, `deleted_objects`, per-URI `deletion_bands`.
- **Trash, not `rm`** (`LocalBackend` gains a `trash` mode, the default for a real run): each path is *renamed* into `~/.Trash/disk-tree/<run_id>/<path>` — same-volume rename, instant, no copy; Finder shows it in the Trash. The band records the trash path, so **`disk-tree undo RUN_ID -f` works for local paths** (rename back; today "local has no undo"). Bytes are freed only when emptied: `disk-tree dispatch --empty RUN_ID` (`rm -rf` the run's trash dir), a TTL the drainer applies (7 days), or Finder's Empty Trash. `/staged` shows a run as *trashed, N GiB pending* with an admin **Empty now**. A path on another volume (x6) can't be renamed in; it's refused (or deleted outright with `--rm`).
- **TCC**: writing `~/.Trash` needs Full Disk Access, and the drainer's root process is `direnv` today. Same fix as the scan agent: a Python entry run as the venv's `python` (it reads `CLOUDFLARE_API_TOKEN` via `direnv export json`), so one FDA grant covers both agents.
- **After a real run**: the drainer runs `capture` → `aws/submit` so the map reflects the freed bytes within ~10 min (a cheaper index patch is a follow-up).
- `site/`'s plan dispatch today hands a run to the store's `executor` (`'plan-sweep' | 'sweep'`, cloud jobs). Add a `'drainer'` executor: enqueue the run in D1, and the laptop's `disk-tree dispatch -s` (LaunchAgent `com.runsascoded.disk-tree.drain`) polls and executes it through `LocalBackend`.
- Reconcile the two schemas: `site/`'s plans/staged tables vs `ui/`'s `deletion_runs` (migrations 0001–0009 in `ui/migrations`). The drainer reads the latter.
- After a run, re-publish (or patch) the touched subtrees so the Map reflects the freed bytes.

### Phase 4: cut over and retire

**Status (2026-10-01): cut over.** `site/` serves disk.rbw.sh (deploy `09cbce05`): prod got the `STORE_*` secrets, Google sign-in verified on prod, every data route 401s signed out, `/auth/app-link` live for disky. `ui/`'s `ACCESS_AUD` / `ACCESS_TEAM_DOMAIN` / `ALLOWED_EMAILS` secrets deleted; the drainer polls only `disk-tree-m3-db`. Ryan deleted the Access app `disk-tree` (`/auth/sso` now falls through to the SPA) and pointed the consent screen's privacy URL at `disk.rbw.sh/privacy` (no logo: uploading one would force Google brand verification). Left: deleting `ui/functions/`, `ui/cfn/`, `ui/wrangler.toml` on `cloud` (deleting them on `m3` alone would conflict on every merge). The scheduled scan now captures only; `du -p` reads the published path index. `overcount` measures local extents directly.

- Deploy `site/` to Pages `disk-tree`.
- Delete the Access app `disk-tree` and the `ACCESS_*` secrets.
- Remove `ui/functions/`, `ui/cfn/` and `ui/wrangler.toml` from `m3`. Whether to remove them from `cloud` is a `[base]` question: nothing else deploys them.

## The Flask app

`disk-tree-server` + `ui/` live in the base Python package, shipped via PyPI (`pyproject.toml` bundles `ui/dist`). They are not a branch. They were still worked on in 2026-09 (`server.py` 9 commits, `ui/src` 24), almost all of it for this laptop loop. Once disk.rbw.sh runs `site/`, that loop no longer needs them, so sunsetting is the natural outcome rather than porting `ui/` into another `site/`. What only Flask offers today, all needing a process on the laptop:
- rescan;
- reveal in Finder;
- immediate delete;
- recursive filter;
- mtime histogram;
- file preview.

The CLI keeps all of these, except reveal and preview, which have no CLI command. Any we want in the browser should come back as drainer-style laptop verbs behind `site/`, not as a second app.

## Harmonizing

The `ui/` ↔ `site/` delta should be distilled like the gcs/cw unification: each `ui/`-only feature either ports into `site/` or is consciously dropped. Candidates to inventory during Phase 1:
- hybrid-chunk drill;
- compare view vs `site/` diff;
- recent paths;
- the staged page;
- OG cards (tier B treemaps);
- the phone path-column layout.
