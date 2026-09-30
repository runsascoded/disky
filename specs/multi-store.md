# Multi-store deploys: secondary stores at their own URL paths (first user: a `/meta` self-scan)

Status: **phases 1 and 2 implemented on the base** (2026-09-29; not yet deployed or migrated anywhere); phases 3 and 3b open. Proposed 2026-09-29 from the cw-s3 session (Ryan: "(a) multi-store base sg … yes spec it, this is a good direction").

## Motivation

A deploy today serves exactly **one store**: the build picks `STORE` (`wrangler.toml` `[vars]`, read by `vite.config.ts`), and the Functions' per-scan tables and caches are keyed by scan id alone. Ryan wants cw-s3.oa.dev to also show a treemap of **our own storage**, meaning the scan and index data the deployment writes, at a separate URL tree (`/meta`) on its own schedule (daily).

Data points for the first secondary store, measured 2026-09-29:

- `gs://oa-gcs-usage-dvx` ≈ 5.4 TiB: `listing/` 2.45 TiB and `access/` 2.43 TiB (gcs), `cw-l2/` 308 GiB (cw; 100 scans ≈ 3.1 GiB each, of which ≈ 3.6 GiB raw per-object listing vs 0.56 GiB indexes on the latest), plus about 75 GiB of leftovers (`central2-listing/` 27, `scratch/` 21, `listing-archive/` 15, `sweep/` 12).
- R2 `oa-cw-s3-usage-index`: 308 GiB / 4,343 objects, a full mirror of `cw-l2/` plus `snapshots/`. `wrangler r2 bucket info` reports 0 B (stale metric); count via the S3 API.

Self-reference is fine: each meta scan sees the previous meta scan's output, which is honest and small. Nothing about scanning the deploy's own bucket needs special handling beyond keeping meta out of the primary store's views.

## Relationship to `federated-scans.md`

That spec's phase 2 is a **union** of N locations into one Map. This one is its sibling: N **separate** stores, each with its own root, scans, schedule and URL path, served by one deploy. Design both on the same `{store, prefix, creds}` location shape so a secondary store can itself later be a union.

## Design

1. **Store registry → per-deploy list.** `STORE` becomes the *primary* store (served at `/`, unchanged URLs). A new `STORES_EXTRA` var (e.g. `"meta"`) names secondary stores, each served under its `Store.path` (`/meta/...`). `src/stores.ts` already gives each store a `path` and `base`; the router mounts one app subtree per configured store. The primary's deep links must not change.
2. **Per-store data seam in the Functions.** Every data route resolves a store from the request: the path prefix for pages, and an explicit `store=<key>` param (default = primary) for `/api/*`, `/data/*`, `/v1/files/*`. Per-store config (today's single-store vars `SNAPSHOTS_SUBDIR`, `BASE_SCOPE`, `STORE_SCHEME`, `STORE_BUCKETS`, `STORE_PREFIXES`, `STORE_BUCKET`/creds, `ROOT_LABEL`) moves into a per-store config object. Suggested shape: a `STORES_JSON` var, with secrets referenced by name (`STORE_META_ACCESS_KEY_ID`, …). Today's single-store vars remain the primary's.
3. **D1: store-scoped index tables.** `index_schema` and `index_row_groups` get `store TEXT NOT NULL DEFAULT '<primary>'`, and the PK becomes `(store, date, variant)`. Otherwise `SELECT DISTINCT date FROM index_schema WHERE variant='path'` (`/api/series`, the scan list, …) would mix meta scans into the primary's history. Same for `pyramid_multiscans` (over-time groups) and any other scan-keyed table. `index-sync` / `sync_d1` take `--store`. Migration: add the column with the primary's key as default, rebuild the PK. **Mind `d1-enforces-foreign-keys`:** don't rebuild a table something references; test with `PRAGMA foreign_keys=ON` on a seeded copy.
4. **Caches.** `edgeCache` keys (`cacheKeyFor`) gain the store key. Otherwise `/api/series?path=` for a meta path could collide with a primary path of the same spelling. `CACHE_V`-style versioning is unchanged.
5. **Auth.** One gate for all stores in a deploy by default: same viewer policy, and each store declares its required scope. Meta is staff-only (`admin` or the staff domain), since it exposes both deployments' data layout. Staging, owners and executors are per-store flags already (`Store.staging`, `owners`, `executor`); meta has all three off.
6. **Ingestion.** The meta store is scanned by the same job image with different targets: `gs://oa-gcs-usage-dvx` via the GCS lister and the R2 bucket via `disk-tree bulk-list -s s3` against the R2 endpoint, as a two-root store (`cw-multi-bucket` style). Output goes to its own layer-2 prefix (`meta-l2/{scan}/`), snapshots to `snapshots/meta/`, and D1 rows with `store='meta'`. It runs on its **own daily Cloud Scheduler job**, independent of cw's 12-hourly one. It warms its own caches (`warm-cache` with the store param).
7. **Ownership.** The GCS bucket is shared (≈95% of its bytes are gcs's). Hosting meta on cw-s3.oa.dev is Ryan's call for now. Keep the store definition deploy-agnostic so gcs.oa.dev can mount it too, or instead.

## Phases

1. **Schema + seam (base):** the `store` column and PKs, `store=` resolution in every data Function (default = primary, so existing deploys are byte-identical), store in cache keys, and the per-store config object. Verify: existing gcs/cw/r2 behaviour unchanged (tests plus a dev-stack diff of `/api/series`, `/api/subtree`, `/api/diff` responses).
2. **Frontend mount:** secondary stores routed under `Store.path`, the store switcher, and the `STORES_EXTRA` build var.
3. **Meta store for cw:** the job targets, scheduler, first scan, `wrangler.toml` config, then CIC `cw-s3.oa.dev/meta`.
3b. **`/meta` is the one storage browser (disky + file-tree).** Ryan (2026-09-29, via cw-s3): "combining best parts of FT and DT for meta browsing".
    - **Navigation is disky's:** treemap + children table, with totals, age, diff and over-time, over the daily meta scan (the whole bucket plus the R2 mirror).
    - **Leaves open file-tree's viewer:** clicking a file cell or row opens `@rdub/file-tree`'s parquet viewer (schema, row-group paging, cell rendering, the byte-unit `renderCell` from `FilesPage.tsx`), reading the bytes live through the gated `/v1/files/<path>`. zstd files need file-tree's `specs/zstd-parquet.md` first (see `listing-slim.md`).
    - **"Live" toggle:** optionally show the current bucket listing (the `/v1/files` list) under the scanned tree for the drilled prefix, since the meta scan is up to a day stale. Useful while a scan is mid-publish.
    - **Retire the `/files` page:** `/files/<path>` redirects to `/meta/<path>`. `/v1/files` stays as the gated raw-read API (DuckDB and agent range reads, the documented examples). First audit which other site pages use it.
    - **Scope asymmetry to resolve:** `/meta` covers the whole bucket, while `/v1/files` is `STORE_PREFIXES`-allow-listed. Leaves outside the allow-list either show size only, or the allow-list grows for staff-only meta reads (a per-store `STORE_PREFIXES` in `STORES_JSON`).
4. **(later)** Converge with `federated-scans.md` phase 2: a store may be a union of locations.

## Phase 1 as built

**The primary's SQL is exactly what it was before stores existed.** No primary read has a `store` predicate, and no primary write names the `store` column; its rows take the column default once migrated. So the primary runs identically on an un-migrated or a migrated D1, and merging this branch needs no migration anywhere. Only a secondary store (`store != 'primary'`) names `store`, in both reads and writes. Its rows are also namespaced by **variant**: `<store>:<variant>` in `index_schema` and `index_row_groups` (`d1Variant` in `_lib/stores.ts`, `d1_variant` in `dt_cloud.index_footer`). Every primary query names a bare variant (`path`, `coarse20`, `over-time`, …), so it can never see a secondary store's rows, even without a `store` predicate. The one primary query that lists variants, `synced_variants`, drops `:`-prefixed ones in Python, keeping its SQL as it was.

**D1** (`site/migrations/cw/0006_store_scoped_index.sql`, `site/migrations/gcs/0030_store_scoped_index.sql`, identical DDL):

- **Only a secondary store needs the migration.** Its queries say `store = ?`, which fails loudly (`no such column: store`) on an un-migrated D1, so a secondary store can't be used there by accident.
- **`'primary'` sentinel.** The primary's rows are `store = 'primary'`, the column default, rather than the deploy's `STORE` key: the cw lineage serves both cw-s3 and the r2 demo. `'primary'` is reserved (not a valid `STORES_JSON` key), and `store=primary` is accepted as a synonym for no param.
- **`index_schema`** is rebuilt (create / copy / drop / rename) with PK `(store, date, variant)`. It's small, and nothing `REFERENCES` it, so the rebuild can't trip D1's FK enforcement. The namespaced variant would already keep the old PK disjoint; the rebuild just makes the store explicit.
- **`index_row_groups` is not rebuilt** (deviation from design item 3). It's the multi-GB table: gcs's DROP COLUMN on it ran past D1's statement limit (gcs `0019`). It gets `store TEXT NOT NULL DEFAULT 'primary'` in place (O(1)), and its PK stays `(date, variant, gen, rg)`. The namespaced variant keeps that PK disjoint across stores, even for the same scan id and gen. The reader's row-group queries are unchanged apart from binding the D1 variant, which for the primary is the same value as before.
- **`pyramid_multiscans` gets no column** (deviation). pyrmts owns its DDL (`CREATE TABLE IF NOT EXISTS` at first sync; the gcs lineage never created it, so an `ALTER` there would fail on a fresh DB) and its read query (`MultiScanD1Index.listMultiScans(dataset)`). Its `(dataset, key)` PK already namespaces it. A secondary store's groups are dataset `<store>:over-time`, and the primary's stay `over-time` (`overTimeDataset` in `_lib/overTime.ts`, `multiscan_dataset` in `dt_cloud.overtime`). A group's footer is then variant `<store>:over-time`, like any other tier.
- **`owner_totals`** (gcs; per-scan ownership cache) is left alone: the ownership surfaces are primary-only (below).
- **Verified with FKs on.** `storeMigration.test.ts` applies each lineage from scratch with `PRAGMA foreign_keys=ON` (`sqliteD1(lineage, {before})`) and seeds `plans`, `plan_items` and index rows. It then applies the migration and asserts the exact rows, `foreign_key_check = []`, `integrity_check = ok`, the new PK columns, and that a secondary store's rows fit beside the primary's under one scan id and gen, while a duplicate is refused. Both files were also applied with `wrangler d1 migrations apply --local` (miniflare's D1, which enforces FKs as D1 does) over a seeded pre-migration DB: rows came through as `primary`, `foreign_key_check` was empty, and `foreign_keys` was 1.

**Rollout order:** merging and deploying this changes nothing for the primary and needs no migration. The migration is required on a deployment's D1 only **before that deployment enables a secondary store**, i.e. sets `STORES_JSON` or runs `dt-cloud … --store <key>`. Until then a `store=` request is a 404 (no such store), and a secondary-store query against an un-migrated D1 fails loudly.

**Functions** (`site/functions/_lib/stores.ts`):

- **`withStore(ctx)`.** No `store=` (or `store=primary`, or empty) returns the context object itself; with the primary's SQL unchanged, its behaviour and responses are unchanged. `store=<key>` returns a copy with an `Env` overlay. A malformed key gets 400, an unknown one 404, and a bad `STORES_JSON` 500.
- **Shared read helpers.** `schemaRow` (the pointer) and `pathScans` (a store's scan list) in `_lib/index.ts` hold the only two-way SQL: the primary's pre-stores statement, or the secondary's `store = ? AND … variant = <store>:<variant>` one.
- **`STORES_JSON = {"<key>": {"scope"?, "vars"?, "secrets"?}}`.** `vars` may set only the per-store vars: `ROOT_LABEL`, `SNAPSHOTS_SUBDIR`, `STORE_SCHEME`, `STORE_BUCKETS`, `STORE_PREFIXES`, `STORE_BUCKET`, `STORE_ENDPOINT`, `STORE_REGION`, `STORE_ACCESS_KEY_ID`, `STORE_SECRET_ACCESS_KEY`. `secrets` maps one of those vars to the **name** of the Pages secret holding its value. The overlay first clears all of them, plus the `GCS_HMAC_*` fallback, so nothing of the primary's leaks into a secondary store. Unknown fields or vars are refused.
- **`BASE_SCOPE` stays deploy-wide** (deviation from design item 2's var list). It's the identity policy's grant (`scopesFor`), so overlaying it would hand every allow-listed viewer the secondary store's scope. A store's `scope` is instead an **extra** scope that `requireViewer` demands on top of the viewer scope: `STORE_SCOPE` on the overlay, 401 for anonymous and 403 for signed-in.
- **Where it's wired.** `/api/{subtree,diff,series,age,age-pyramid,path-index,bench,store}`, `/data/*` and `/v1/files/*`. On a secondary store, `/data/*` reads only `snapshotsPrefix(env)` (its own `snapshots/<sub>/`, never `rules.json`). `/v1/files?store=` is gated by `requireViewer` like the store's data; the primary's proxy keeps no gate of its own, as before (see open questions).
- **Primary-only surfaces.** `/api/{owners,estate,assignments,claims,actions}` return 404 for any other store, and `lens=user:` on subtree/diff/series returns 400: the ownership ledger and identity registry are the primary's.
- **Caches.** `cacheKeyFor(ns, parts, store)` inserts `@<store>/` only for a secondary store. The primary's keys are byte-identical to before, so there's no one-time miss. The index-blob colo cache and the per-isolate memos (`openIndex` handles, the row-group LRU, the extras index) are keyed by store too.
- **Tests** (`stores.test.ts`):
  - config parsing and refusals, and the exact overlay;
  - `withStore` returns the same object for the primary, plus its error bodies;
  - cache keys and over-time datasets;
  - per lineage, the primary's `indexDir` / `openIndex` / `pathScans` on the **un-migrated** schema (rows written the pre-stores way), where a secondary store's reads reject with `no such column: store`;
  - per lineage, on the **migrated** schema, one scan id and gen in both stores, each env reading only its own pointer, rows and scan list;
  - handler-level 404 / 400 / scope-gate responses.

**`dt-cloud`**: `-s/--store` (default `primary`) on `index-sync`, `index-gc`, `index-dir`, `over-time-groups` and `over-time-write`, threaded through `sync_d1`, `gc_d1`, `retire_d1`, `index_dir`, `synced_variants` and `compact_d1`. The primary's statements are byte-identical: `tests/test_index_footer.py` passes unchanged. `tests/test_index_stores.py` runs the writer against the gcs lineage with FKs on. It checks:

- the primary's sync / pointer / gc on both the un-migrated and migrated schemas;
- a secondary store beside it on the migrated one: the same scan id and gen, namespaced rows, per-store `index_dir` / `synced_variants`, and gc / retention that never touch the other store;
- a secondary store failing with `no such column: store` on the un-migrated schema.

**Not done in phase 1**: the dev-stack diff of `/api/series` / `/api/subtree` / `/api/diff` before vs after. Every dev stack here points at a prod D1. The byte-identity argument is that the primary's SQL, context object and cache keys are all unchanged. `warm-cache` has no `--store` yet (phase 3).

## Phase 2 as built

**The primary's build is the single-store build.** With `VITE_STORES_EXTRA` unset, `STORES` is `[primary]` as before, `Root` mounts the same routes, and every request is byte-identical: the store seam returns the URL itself and the global `fetch` for the primary, so no `store=` ever appears. CIC'd on the read-only stack (`API_ORIGIN=https://r2.rbw.sh VITE_STORE=r2 VITE_AUTH_MODE=public`): `/` renders, and its `/data/r2/scans.json`, `/data/r2/<scan>/meta.json`, `/api/subtree`, `/api/diff`, `/api/age-pyramid`, `/api/series` requests carry no `store=`.

**Build var** (`site/vite.config.ts`): `STORES_EXTRA` beside `STORE` in `wrangler.toml` `[vars]`, or `VITE_STORES_EXTRA` in the environment — comma-separated registry keys. It is the FE half of `STORES_JSON`: the Functions' entry gives the store its data (`SNAPSHOTS_SUBDIR`, `STORE_*`, `scope`), the registry row gives it its page (`path`, `base`, `label`, `title`, flags), and the two meet on the key. `resolveStores(primary, extra)` in `src/stores.ts` builds the list (primary first) and refuses, at build time, an unknown key, the primary among the extras, a secondary whose `path` is `/`, or two stores on one path.

**The `meta` registry row**: `key: 'meta'`, `path: '/meta'`, `base: '/data/meta'`, label `Meta`, `rootLabel: 'our storage'`, `owners` / `staging` / `prices` off. `base` matches the data Function's read scope for `store=meta` (`snapshots/<SNAPSHOTS_SUBDIR>/` with `SNAPSHOTS_SUBDIR = meta`): `/data/meta/scans.json?store=meta` → `snapshots/meta/`, `/data/meta/<scan>/meta.json?store=meta` → `snapshots/meta/<scan>/meta.json`. The scheme, buckets and copy are placeholders until phase 3 scans it.

**The request seam** (`src/stores.ts`): `storeQuery(store)` is `''` for the primary and `store=<key>` for a secondary (phase 1's `requestedStore`: no param = the primary); `storeUrl(url, store)` appends it (`?` or `&`, before any `#`); `storeFetch(store)` is the global `fetch` for the primary and a URL-rewriting wrapper (string, `URL` or `Request` input) for a secondary. `src/store.tsx` carries the store as a React context: `<StoreProvider store>` / `useStore()` (the primary outside any provider, so every component that used to read `DEFAULT_STORE` keeps its behaviour), `useStoreFetch()`, `useStoreUrl()`.

**Where it's threaded.** `App.tsx` reads its store from the context (not the pathname) and sends `/api/subtree`, `/api/diff`, `/api/age-pyramid` and `<base>/<scan>/*.json` through `useStoreFetch`, with `store.key` in every query key (two mounted stores can share a scan id and a path spelling); `scan.ts` (`useScans`), `LifecycleFold`, `OgPage`, `SizeOverTime` (`/api/series`), `rules.ts` (`/data/rules.json`, enabled by the subtree's `owners`) likewise. `FilesPage` browses `/v1/files?store=<key>` through file-tree's `HttpStore` `fetch` option, and `/api/store?store=<key>`; the primary's client is built with no `fetch` option, exactly as before. `ChildrenTable` takes `staging` / `owners` from the subtree's store, so a secondary store has no trash gesture and no assignment column. `useDocTitle` uses the subtree's store title. `plans.ts` (the executor and `/api/plans/*`) stays the primary's: staging is off on every secondary store, so those calls never fire there.

**Router** (`src/Root.tsx`): per secondary store, `<path>/files/*` → `FilesPage` and `<path>/*` → `App`, each inside `<StoreProvider>` behind the same `AuthGate`; `<path>/og` already came from the `STORES` map. No `/users`, `/assignments`, `/staged` or `/admin` under a secondary path — those pages are the primary's, and their Functions 404 for any other store (phase 1). A wrong first segment under `/meta` gets `App`'s 404 with a `home` link to `/meta`.

**Store switcher** (multi-store builds only; a single-store build shows nothing new): the ☰ menu (`SiteNav.tsx`) links the subtree's own `Map` (`store.path`) and `Scans` (`<path>/files`) and lists every configured store with the current one marked; the omnibar (`SiteKbd.tsx`) offers `<label> store (<path>)` actions for the *other* stores under Pages, and its `Map (home)` / `Browse scans` entries are store-relative. The per-store `store:<key>` actions `App` used to register (a self-link on a single-store build) moved there.

**Tests** (`src/stores.test.ts`, 14): resolution (the untouched build, a primary, extras trimmed / de-duplicated, the four refusals with exact messages), the exact URLs a primary and a secondary store request (`/data/*`, `/api/*`, `/v1/files/*`, with and without a query, with a fragment), `storeFetch` (the primary's is the given fetch itself; a secondary's over string / URL / Request inputs, init passed through), `storeForPath`.

**CIC'd** with `VITE_STORES_EXTRA=meta` on the same read-only stack: `/meta` mounts (title `Our storage — scan & index data`, crumb `our storage`), requests `/data/meta/scans.json?store=meta` (404 from r2.rbw.sh, which has no `STORES_JSON` — expected), the ☰ menu shows `Map` → `/meta`, `Scans` → `/meta/files`, then `R2 /` and `Meta /meta` (current); switching to R2 renders the primary with its unchanged requests; `/meta/files` requests `/v1/files/list?prefix=&store=meta` and `/api/store?store=meta`.

**Not done in phase 2**: nothing deployed (no `STORES_EXTRA` in any `wrangler.toml`); the login wall's copy stays the primary's (`AuthGate` reads `DEFAULT_STORE`) — a secondary store's `scope` is enforced server-side and surfaces as the scan-list error strip; `index.html`'s `<title>` / og tags are the primary's; `/meta/og` renders but no og image is generated for it (`pnpm shots` is per deploy).

## Open questions

- Route shape for secondary-store APIs: a `store=` param (proposed; minimal churn) vs a path prefix (`/meta/api/...`).
- Whether the meta store should also cover the D1 database size and KV namespace sizes (not objects, but part of "our storage"), shown as synthetic roots.
- `/v1/files` (the primary's) calls no `requireViewer`: it dates from the CF Access era, and both deployments are now off Zero Trust. Is it meant to be open? Phase 1 leaves the primary as it was and gates only `?store=`.
- Should `index_row_groups` eventually carry `store` in its PK, e.g. via a new table filled a `(date, variant)` at a time the way `index_groups` was retired? The variant namespacing makes that unnecessary for correctness.
