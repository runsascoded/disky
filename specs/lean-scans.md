# Lean scans: append straight into the stores, keep only what can't be derived

**Status:** draft, 2026-10-15; partly built 2026-10-18 (on `cloud`, nothing run against a deployment). It extends [interval-store.md] §4–§5 (retention, migration) from gcs to every deployment.

**Built (2026-10-18):**
- **P1, `dt-cloud interval-store export [-g GEN] [-M MEM] -o STORE [-p THREADS] [-t path,bysize] [-w WORK] SCAN_ID DEST`** (`cloud/src/dt_cloud/interval_export.py`): scan D's rows are the versions live at D over the base's served sort and every run's (`slices` on a sliced generation, else `path`; `combineLive`'s smallest `vt`), so it works from any copy of the store, R2 included. Written in the scan's own format: v2 as the layer-2 columns the stores read, cut by `write_tiers` into `path-index.parquet` + `path-index-bysize.parquet` (8K groups, `tier`/`sort`/`bucket` metadata); v1 as `path, depth, usr, b, o, wts, wb, c2, c3, c4, a`, `path` only. Not reproduced: columns no store carries (`created`, `uri`, classes beyond 2–4, B5). `test_interval_export.py`: every fixture scan (three v1 and one v2 in the base, four v2 runs merged by carries) exports to its original sort's count and Σ hash of owner slices (`slice_rows_sql`, `SV_HASH`), `bysize` too.
- **Per-scan verify markers:** `interval-store verify` now also writes `verify/<id>.json` per sampled scan (`interval_verify.marker`: views, diffs, `pass` = had views and every view and diff equal); `interval_verify.verify_scans` verifies given scans against a generation and writes theirs.
- **P3, as `dt-cloud scan retire`** (`cloud/src/dt_cloud/scan_retire.py`; the spec's `index-retire`). See its docstring for the class table. Differences from §2's proposal: no grace between the D1 delete and the files (the IS serves every retired scan; only the fallback reads per scan, and it finds no rows), and the `path` pointer stays unless `-P` (S1 hasn't shipped). Order: backup (`-b`, sizes checked) → D1 (`footer_cache`, `index_row_groups`, `index_schema`) → files. A dry run never verifies (a scan without a marker is `needs-verify`). `test_scan_retire.py`: the keep window, a scan missing from the static index, a failed marker, a missing marker (dry run: `needs-verify`; `-m`: skipped; `-x`: verified now and retired), every class deleted / kept, the exact D1 rows planned and left, backup ≡ deleted, a dry run changing no file, store key or D1 row, `-P`.
- **Pipeline step `retire`** (`scan_steps.py`, a post step: after `interval-store`, `static-names` and `index-sync`, before healthcheck / warm-cache / probe / digest). Off unless a profile lists it in `steps`; `options.retire`: `keep` (required, ≥ 1), `execute` (default false: a dry run, the plan in the task log), `static_gen` (default `options.static-names.gen`), `store` / `static_store` (default the interval-store / static-names steps', else `$R2_ARCHIVE`), `layer2` (also the `<layer2>*.parquet` listings), `copies` (more roots, `r2` = `$R2_ARCHIVE`, run after the data store with `-R`), `backup`, `markers_only`. Soft: a failure ends the phase failed with a warn alert, never the run. Layout: the other steps' `<layer2>index/{gen}/path-index.parquet` (gcs's legacy `listing/{id}/path-index.parquet` needs a by-hand run with a second `-L`).
- **S1 (2026-10-18), the scan list from the store:** `pathScans` (the scan list every reader shares: `/data/<store>/scans.json`, series, health, OG cards, `/staged`, the union) and `indexedScan` (`date=` / `d=` resolution) list D1's `path` pointers **and** every scan the interval store's current generation holds (`storeHeldScans`: base + runs), where the primary serves it for every request (`PATH_STORE=intervals`; not under `opt-in`, not for a secondary store). The Slack action's newest scan reads the same list. So a scan the job appends without per-scan pointers, or whose pointers `scan retire -P` dropped, stays listed and resolvable. Starts (`scan-times.json`) still come from `index_schema.started`: only date-only ids need them, and only gcs/cw have those. `intervalStore.test.ts` (the S1 case: D1-only, store-only and shared scans, `opt-in`, exact and slug resolution).
- **Not built:** `pending/` staging, `--path-only`, B4, P2, R2 `trash/` moves (retire deletes; back up with `-b` first on R2).

**Goal (Ryan):** lean, harmonized deployments (gcs, cw-s3, m3, r2, hf, the r2.rbw.sh demo). Each deployment keeps one full-history copy of each index its site serves (the interval store's sorts and slices, the static name index), denormalized for fast serving at any scan, plus the raw listings (§3). A scan adds only its delta to those; it leaves no full per-scan copies behind. Every deployment runs the same search: the prebuilt static name index only (`FILTER_INDEXED_ONLY=1`), with no per-scan search sidecars. And a deployment can be left alone for months or years.

**Short version:**
- The per-scan `path`/`bysize` sorts, their `.groups.*` footers, the D1 footer rows and the search sidecars all become **transient**. The job writes them to a work dir (or a `pending/` key, so a failed append can catch up), the two stores append from there, a parity check runs, and they are deleted. §1 lists the blockers. None is fundamental, but five need code first: catch-up, verify's source, an export from the store, the site's per-scan-only readers, and the scan list in D1.
- The existing per-scan artifacts go once the interval store serves their scans and a per-scan parity marker exists (§2). That needs a new retire command, because `index-gc` never deletes a pointed generation.
- Raw listings: **recommend (d)**. Keep a rolling 30 days of raw listings, plus one keyframe per month in an archive class. The path-level interval store becomes the long-term source of truth, with a new `interval-store export` to materialize any scan (§3). An object-level store (b) is the upgrade path only if exact `generation` history beyond 30 days becomes a requirement.

## 0. What each deployment writes per scan today

| | raw listing kept | layer-2 kept | per-scan `path`/`bysize` (+ footers, D1 rows) | search sidecars | interval store | static index | `FILTER_INDEXED_ONLY` |
|---|---|---|---|---|---|---|---|
| **gcs** | layer-1, forever, 10.7 GB/scan (`job/run.sh:289`, [interval-store.md] §0) | no (`dir-cache/` 19.1 GB/scan instead, `job/run.sh:366`) | 21.7 GB/scan, `-u bysize` copy (`job/run.sh:365-368`), D1 rows `index-sync -r/-p` (`:504`, `:533`), `index-gc -r 30` (`:629`) | only with `PATH_INDEX_SEARCH` (`:368`) | staged Batch append (`:478-485`), `PATH_STORE=intervals` | staged Batch (`:456-464`) | `1` |
| **cw-s3** | no: layer-1 shards are transient (`job/cw-run.sh:160`) | yes, GCS `cw-l2/<scan>/<bucket>.parquet` (`:195-196`) + R2 archive via `publish-r2` (`:255-260`) | `index-write` → `cw-l2/<scan>/index/<gen>/` (`:206-210`), `index-sync` + `index-gc` (`:239-241`) | no | one machine, `add -s $R2_ARCHIVE -L 'cw-l2/{id}/index/{gen}/path-index.parquet'` (`:274-278`) | staged Batch (`:226-227`) | `1` |
| **hf** | no (`job/hf-run.sh:108`, `$WORK`) | yes, `listing/<scan>/<ns>.parquet` on R2 (`:142`) | `index-write -S` (`:121`), uploaded (`:141`), `index-sync` | yes (`-S`; `SEARCH_SIDECARS=1`) | one machine `add` | one machine `add`/`build` (`:154-155`) | unset |
| **r2 demo** (r2.rbw.sh) | **no**: listings stay in the runner's `work/` (`.github/workflows/daily-ingest.yml:76-82`) | no | `path-index -S -P` (`:85`), uploaded to `listing/<date>/index/<gen>/` (`:96`), `index-sync -v path -v bysize` (`:108`) | yes (`SEARCH_SIDECARS=1`, `site/wrangler.r2.toml:49`; no anchors, `:52`) | one machine, `-s r2://$BUCKET -L 'listing/{id}/index/{gen}/path-index.parquet'` (`:142-148`) | one machine (`:123-129`) | unset |
| **r2** (r2.oa.dev, branch `r2`) | **no**: `job/r2-scan` bulk-lists into the Batch work dir only | no | written **twice**, ~246 MB/scan each (rows 66 + trigrams 62 + `bysize` 55 + `path` 44 + age pyramids ~19 MB): GCS `oa-r2-usage-data/scans/<id>/<gen>/index/` (2.2 GB total) and R2 `oa-r2-usage-index/listing/<id>/index/<gen>/` (1.7 GB), as measured by r2.oa.dev's /meta scan | prod yes; dropped on dev (`38c0523b`: no `path-index -S`, no `SEARCH_SIDECARS`), image not rebuilt | one machine, gen `2026-10-15r2` on R2, `PATH_STORE=intervals` | one machine, `2026-10-15r2` + `2026-10-15meta` (the /meta store, a union member, ~12 KB/scan, same per-scan readers) | dev only (`38c0523b`) |
| **m3** | layer-1 capture shards `r2://disk-tree/captures/m3/root/<stamp>/` (m3 `AGENTS.md`), retention unstated | no | `path-index -g -P` (m3 `infra/aws/ingest.sh:126`), `index-sync` (`:147`) | no | **none** (m3 `site/wrangler.toml` has no `INTERVAL_STORE_GEN`) | one machine (`:152-167`) | unset |

Branch refs (`job/*.sh`, m3 files) are on the deployment branches (`gcs`, `cw-s3`, `hf`, `m3`). Everything else is on `cloud`.

## 1. Append straight from the job's work dir

### 1.1 The proposed per-scan flow

1. List → layer-1 (raw listing, §3 decides how long it is kept).
2. Aggregate → the scan's `path` sort (v2, the input both stores read) into the job's work dir. No `bysize`, no `-by-user` copies, no `.groups.json`, no search sidecars (`-S` off), no coarse tiers. The snapshot JSONs (`meta.json`, `age.json`) and, until they're replaced, the age pyramids are still written.
3. Stage the `path` sort at `pending/<id>/path-index.parquet` in the store's bucket (§1.2 B2 says why it can't simply stay local).
4. `static-names add` and `interval-store add` (or the staged `runs add -c` / `append -c`), both reading `pending/`.
5. `interval-store verify` for that scan, against the staged sort. Write a marker `interval-store/<gen>/verify/<id>.json` with the parity result.
6. Once both stores' newest manifests list the scan and the verify marker passes, delete `pending/<id>/`.
7. No `index-sync` for `path`/`bysize`, and none at all once the age pyramids move (§1.4).

### 1.2 Blockers found in the code

**B1: local layouts.** One-machine commands: **no blocker.**
- `interval-store build/add` and `static-names build/add` take `-s` as "a dir or URL" (`cloud/src/dt_cloud/interval_local.py:221`, `:256`; `static_local.py:582`, `:620`).
- `find_scans` lists a local root through `blobstore.Store`, and for a local root it reads the files in place, with no copy (`static_local.py:171-188`: `mount = local if src.url else Path(root)`).
- A layout needs exactly one `{id}` segment (`static_names.py:115-122`), and that id must parse as a scan id (`:137-140`). So the local layout is `<work>/scans/{id}/path-index.parquet` with `-s <work>/scans`. Not `-s <work>` with `{id}`-less keys, and not a work-dir root with unrelated files under the listed prefix.

**B1': staged (Batch) appends.** They need the sort in a GCS bucket. This is a constraint, not a blocker.
- `interval_append.load_config` refuses any `bucket`/`scratch` that is not a whole GCS bucket (`interval_append.py:446-473`).
- `prepare_scan` pins the sort's GCS generation and checksum from the profile's `layouts` in the store bucket (`:654-674`).
- The range tasks read the sort through the bucket's mount (`_src`, `static_names.py:222-226`).
- The static chain does the same (`static_append.py:525`).
- So for gcs (both stores) and cw (its static index), step 3's `pending/` is a GCS key in the profile's bucket. It is never a published `index/<gen>/` dir, and it is deleted at step 6.

**B2: catch-up.** This is a real blocker for "never written", and staging fixes it.
- Appends read only the pending scans' sorts.
  - The interval store refolds the open versions from the base's range files plus the runs' deltas (`interval_local.py:182-185`, `open_state_sql`), or reads the staged path's scratch `state/`.
  - The static index refolds from `cintervals/` plus each run's `cdelta/` (`static_local.py:484-490`).
  - The drill reads store tiers, not scans (`static_local.py:509-531`).
  - So adding several scans needs only those scans' sorts, never an older scan's.
- **But a scan whose append failed is lost for good once a later scan appends.**
  - `add` appends only ids `> have[-1]` (`interval_local.py:277-278`, `static_local.py:642-643`).
  - The staged chain refuses anything but the next scan (`interval_append.py:663-664`).
  - So if D's sort lived only in a dead job's work dir and D+1 appends tomorrow, D can never enter either store.
  - Keeping D's sort under `pending/` until step 6 makes `add -c` (or `add`'s "every scan after the newest") append D then D+1, in order.
  - A lifecycle rule on `pending/` (e.g. 30 days) backstops a stuck one. The job also alerts when `pending/` holds more than one scan.

**B3: a new base generation needs every scan's sort.** This is a blocker for any rebuild. It needs `interval-store export`, which isn't built.
- `build` for either store reads every scan's `path` sort (`interval_local.py:234`, `static_local.py:598`).
- Compaction does not need them:
  - static compaction (branch `compaction`, `f8b2b01b`, not on `cloud`) folds the base and the runs' `cdelta`s. Only its acceptance commands (`ref-*`) rebuild sampled parts from scans.
  - interval compaction is unbuilt, but designed as `Carry.build` over the base and every run ([interval-store.md] §2.7 "Compaction").
- A *new* generation does need every scan's sort. That covers a rule or schema change, a new column (e.g. B5's classes), a `-x` change for the static index, or recovery from a bad generation.
- Fix: `dt-cloud interval-store export -g GEN -d ID -o PATH` writes scan ID's per-path rows (one row per `(depth, path, usr)` from the slice table `sv`, or per `(depth, path)` from `pvl` on an unsliced generation) as a v2 `path` sort. `build` then reads that output through `-L`.
- Exactness is already proven at append time: "A range raises unless its open state is D's rows exactly (count and Σ hash)" ([interval-store.md] §2.7 step 2), and `build` checks every scan's reconstruction (`interval_local.py:12-14`).
- Test: export ∘ append is the identity on every fixture scan, byte for byte after the sort. This is prerequisite **P1**.

**B4: the run's `scans.json` names a source that won't exist.**
- `add` records `{"bucket": base["bucket"], "scans": [scan]}`, where `scan["src"]` is relative to `-s` (`interval_local.py:179`, `static_local.py:482`).
- `interval-store verify` re-reads the sort from `scans["bucket"]/src`, or from `-m` (`interval_verify.py:392-422`). So verify has to run while the sort exists (step 5, before step 6), and reading the run's `scans.json` afterwards must not 404 on the `src`.
- Record the root actually used (`scans["bucket"]` from `find_scans`) in the run's `scans.json`, not the base's. That's a one-line fix in each `append_scan`, and it is honest provenance either way.

**B5: hf's storage classes 5/6 aren't in either store.**
- The interval kernel carries `c2`–`c4` only (`static_names.py:231-238`, `V2_OPTIONAL`). The site reads classes 2–4 only, from every source (`site/functions/_lib/index.ts:75`, `:860-882`).
- So hf's public/private/git split (`hf-run.sh:5`, classes 1/5/6) lives only in hf's per-scan sorts and layer-2. No reader uses it yet.
- Before hf drops its per-scan sorts (or its layer-2, §3), the kernel's class columns must generalize (`c<k>` for each class a deployment has). That means a new hf base generation, which needs P1 or the sorts still in place. This is prerequisite **P2** (hf only).

### 1.3 What reads per-scan sorts today, and what replaces it

**Site** (`site/functions/`):

| Reader | Today | Replacement |
|---|---|---|
| `openIndex` per-scan path (`_lib/index.ts:435`) | only a date the generation doesn't hold (older than its base) | **landed (2026-10-18):** a date the store holds never falls through to per-scan. `ivRefuseHeld` (`:695`): a variant outside `IV_SORTS`/`IV_SLICE_SORTS` (a v1 `user`/`coarse*` tier, a sliced read of a generation found without slice sorts) is "not synced … (the interval store holds the scan and doesn't serve the variant)", which the existing "not synced" handling turns into its honest answer (`tryOpen` → no tier, `LensUnavailable` → 409, `largest` → 409); a scan a broken run cut out is a 503 (next row). Dates the store doesn't hold keep the per-scan read until they are retired too. What stays per scan asks for it explicitly: `perScan(env)` (age pyramids, the over-time archive, search sidecars) |
| broken-run fallback (`ivMarkBroken`/`ivRetry`, `_lib/index.ts:620-735`) | a broken run's scans, and every later run's, are refused | **landed (2026-10-18):** `loadIvState` (`:780`) records the scans a cut removes (`IvState.refused`: the broken run's and every later run's → the run's key); `openInterval` (`:879`) and `openIndex` throw `IvUnavailable` (`:671`) for them, never reading per-scan or answering from the stack below the run. `ivRefusal(e)` (`:680`) maps it (and an uncaught `IvTierBroken`) to a 503 + `Retry-After` (the cut's remaining window, ≤ 30 s) + `no-store`, wired into `subtree`, `diff`, `series` (any refused point refuses the whole series, not a chart with a gap), `largest`, `digest-image`, `estate`, `owners`, `assignments`. Scans before the run still answer exactly (a later run only adds versions opened after them and close records of versions they see live). `pathGens` keys a refused date as the store's, so a cached answer (the store's, append-only) still serves. The store publishes append-only (data → `meta.json`s → manifests last), so a broken run is a bug or a transient read error, not a half-written state (comment above `IvRun`) |
| `/api/largest` (`api/largest.ts`) | already ported before this pass (`b3ccce9a`, `8314740d`): under `PATH_STORE=intervals` a held scan reads the store's `bysize` plus runs (`readLargestIv`, `_lib/index.ts:2556`: a size bucket at a time over every tier, versions combined by `combineLive`, live at D), same response shape; `largestIntervals{,.v2}.test.ts` assert it equals the per-scan sort's answer on every fixture scan, path and n (`kind=file` and `kind=dir`), reading nothing per-scan | **landed (2026-10-18):** the read runs under `ivRetry` and a broken run's scan is a 503 (`ivRefusal`), tested route-level against a seeded per-scan sort that a fallback would have read. One documented difference stays: an object whose key is also a folder's prefix (`bk/m/twin` beside `bk/m/twin/f0`) is folded into the folder by the store, so it is never listed |
| search sidecars (`_lib/view.ts:886`, `:970`, `:1051`; `_lib/search.ts:100-103`) | `SEARCH_SIDECARS=1` on r2 and hf: `perScan(env)` `rows-search`/trigrams | `FILTER_INDEXED_ONLY=1`, `SEARCH_SIDECARS` unset (gcs and cw already). Anchored terms (`^q`, `q$`) are then declined where no `anchors/` is built (r2: `wrangler.r2.toml:52`): the same behavior everywhere |
| `/api/path-index` (`api/path-index.ts`) | ranged raw download of a scan's per-scan `path` sort | **landed (2026-10-18):** a scan the deployment's interval store holds (`ivHolder`, `_lib/index.ts:705`: `PATH_STORE=intervals` or `opt-in`, the primary store, a cut-out scan included) is a 410 naming the export (`pathIndexGone`: `dt-cloud interval-store export -g GEN -d ID -o <path>`), before any per-scan read and whether or not its file still exists. No on-demand export here. Other scans serve as before |
| age pyramids (`api/age-pyramid.ts:116`, `:133`) | per-scan `age-pyramid-<bin>` through D1 pointers, now via `perScan(ctx.env)` (2026-10-18) so `openIndex`'s held-date refusal never applies | stay per-scan until an interval age index replaces them. They're small, live in the same `index/<gen>/` dir, and §2 keeps them |
| over time (`_lib/overTime.ts:67`) | an archive-level `over-time` variant, not per scan; read via `perScan(env)` (2026-10-18) | unaffected |
| extras `attr.tsv` (`_lib/extras.ts:129`, gcs) | per-scan provenance sidecar beside the sorts | keep. It can't be derived later: attribution inputs are mutable (`job/run.sh:292`, `attribution-wandb.parquet`). §2 keeps it |
| lens, owner pool, owner totals, prefixes (`_lib/ownerTotals.ts:74`, `:148`; `_lib/prefixes.ts:62`) | `slices(env)` → the store's slice sorts | already the store wherever the generation is sliced (`IV_SLICE_SORTS`) |
| `user` / `coarse*` variants (`_lib/view.ts:365-385`, `_lib/index.ts:749-752`) | v1-era tiers | the store's slice sorts. A held date with slices refuses the old variant (`:751`) |

**Jobs** (`cloud/src/dt_cloud/`, `job/`):

| Reader | Replacement |
|---|---|
| `index-sync` (`cli.py:914`) for `path`, `bysize`, `-by-user`, coarse | dropped. Only `-A` (age pyramids) remains until those move (§1.4) |
| `index-gc` (`cli.py:1063`; `gen_gc.py:10-19`: deletes only generation dirs **no pointer names**) and `footer-evict` (`cli.py:1126`) | moot for new scans. §2's retire command handles the backlog |
| `index-blob`, `index-recut`, `bysize_check` (`cli.py:1231`, `:755`) | moot (they cut or check per-scan sorts) |
| `publish-r2` (gcs `job/run.sh:426-435`, cw `job/cw-run.sh:259`) | copies only the snapshot JSONs, age pyramids and `attr.tsv`. No sorts or `.groups.*` |
| `interval-store verify` (`interval_verify.py:392-422`) | runs at step 5 against `pending/` (B4) |
| static verification (`static_append.py:840`, `static_catalog.py:835`, `static_roots.py:1321`, `static_anchors.py:1452`) | same: in-job, against `pending/` |
| `-u bysize` (`bysize-by-user`, gcs `job/run.sh:367`) | the store's `slices-bysize-user` |
| `cascade-a2a` gate (gcs `job/run.sh:338`) | gate mode only, reads its own import's tiers. Point it at `pending/` |
| `warm-cache` (`cli.py:1342`), `probe`, `healthcheck` | they replay site requests, so they follow the site. `healthcheck`'s "footer synced" check (`healthcheck.py:8`) becomes "the store's manifest lists the scan" |
| `dir-cache/` (gcs) | keep the latest 7. Only the next scan's attribution reads it ([interval-store.md] §4) |

### 1.4 D1: what becomes unnecessary, and what the site still needs

- **Unnecessary for `path`/`bysize`/`user`/`coarse*`:** `index_row_groups`, `footer_cache` (`_lib/footerCache.ts`, `api/footer-cache.ts`), and those variants' `index_schema` rows. The store's group index is `.groups.parquet` on R2 ([interval-store.md] §2.5).
- **Still read from `index_schema`, so it needs a move first (S1, site):**
  - the scan list (`pathScans`/`pathScanStarts`, `_lib/index.ts:385-400`);
  - scan-arg resolution (`_lib/scanArg.ts:30-33`);
  - the scan picker's times (`_lib/scanTimes.ts`);
  - `/data` listings (`data/[[path]].ts:61`);
  - the Slack action's newest scan (`slack/actions.ts:45`);
  - health coverage (`_lib/health.ts:116`, `src/healthModel.ts:199-232`).

  Built for the scan list and scan resolution (S1, above): under `PATH_STORE=intervals` they also list the store's scans, so `scan retire -P` is safe there for any scan the store holds. Health's coverage reads the same list. Still on `index_schema`: date-only scans' starts. The original proposal: all of them key on `variant = 'path'` rows. list scans from the store (`ivScans`, `_lib/index.ts:710`, already per isolate), and carry each scan's `started` into its run's `scans.json` / manifest `stamps` (from the snapshot `meta.json`, which `index-sync -m` reads today). Then `index_schema` keeps only the age-pyramid and over-time rows.
- **Not regenerable, so D1 stays:** sign-in and access (`allowed_emails`, `grants`, tokens), plans and sweep history, `scan_runs`, access and session logs, the ownership ledger ([d1-migrations.md] table). `owner_prefix_aggs` is a per-`(scan, idx)` cache (`_lib/ownerTotals.ts:8`, `:58`). It stays, keyed by the store generation.

### 1.5 Fallback when an append fails

1. **Default:** the sort stays in `pending/`. The next run's `add -c` / `append -c` appends it before the new scan (B2). Meanwhile the scan isn't listed (S1 lists from the store): an honest gap, never a wrong answer.
2. **`pending/` lost too:** regenerate the sort from the raw listing, while §3 still holds it:
   - gcs: `path-index` over `listing/<id>/`;
   - cw, hf: `index-write` over the kept layer-2;
   - m3: the capture shards.
   - Caveat for gcs's owner axis: `path-index -a` reads today's attribution parquets (`job/run.sh:292`, `attribution-wandb.parquet` is rewritten in place). A regenerated old scan's `usr` slices can differ from what the scan carried. Plain views and the access lens (`-x` reads the dated `access/agg/*`, `:295`) come out the same.
3. **Past the raw window (§3) and not appended:** the scan is gone. That's acceptable only because B2 alerts long before 30 days.
4. **r2 demo:** keeps no raw listing at all today (`daily-ingest.yml:76-82`), so `pending/` is its only copy. §3's rolling window adds one.

## 2. One-time retention per deployment (the backlog)

[interval-store.md] §4 proposes keeping the latest 7 per-scan path stores and retiring D1 rows through `index-gc`. Two things in the code change that plan:
- `index-gc -F` can't do it: it deletes only generation dirs no `index_schema` row names (`gen_gc.py:10-19`), and a served scan's dir is always named. `-r` only moves row groups to cold footers (`cli.py:1068`).
- A scan's `index/<gen>/` dir also holds files that stay: age pyramids (cw `job/cw-run.sh:210` "path-index + coarse tiers + age-index"), `attr.tsv` on gcs, and the snapshot JSONs' siblings. So retention is **per file, by variant**, not per dir.

**New command (P3):** `dt-cloud index-retire [-n] [-k KEEP] [-g GRACE] -G IV_GEN [DATES…]`. For each scan it does, in order:
1. The gate (below), and refuses the scan otherwise.
2. Deletes D1 `index_row_groups` and `footer_cache` rows, then the `index_schema` rows, for the retired variants: `path`, `bysize`, `bysize-user`, `user`, `coarse*`, the search sidecars.
3. After `GRACE`, deletes those variants' files: `path-index*.parquet` except `age-pyramid-*`, plus `.groups.{json,parquet}`, `rows-search`/`trigrams`/`rows`. It deletes them from every copy (GCS, then the R2 copy), never touching `attr.tsv*`, `age-pyramid-*`, `over-time*` or snapshots.

It is `-n` first, everywhere, and prints bytes per scan and store.

**The gate.** Every condition must hold for scan D:
1. D is in the serving generation (`INTERVAL_STORE_GEN` of the deployed site): in the base's `scans.json`, or a run in the newest manifest on **R2** (the reader's store; R2's manifests may lag GCS's, [interval-store.md] §2.7 step 4).
2. The site serves the store by default (`PATH_STORE=intervals`), and §1.3's per-scan-only readers are handled on that deployment: `largest` ported or retired, the broken-run refusal, sidecars off, and S1 for the D1 scan list (or D's `path` pointer stays, minus its files, until S1 lands).
3. A **verify marker** for D passes. This is new for the backlog: `interval-store verify` (`interval_verify.py`) run over every retired scan, not the profile's sample. It covers the root, every depth-1 node, and each lens and pool the profile names, and diffs D against its predecessor. Exact, except the documented float rounding in `(other)` folds ([interval-store.md] §6.3).
4. D is older than the newest `KEEP` scans (7, the spec's fallback window), and the store has held D for at least 14 scans of passing parity ([interval-store.md] §5 step 4).
5. **Grace:** at least 2× the longest reader cache past the pointer delete (handles 60 s `HANDLE_TTL`, `_lib/index.ts:427`; edge and KV response caches keyed by generation). The default is 2 days, as `index-gc -m`.
6. D isn't referenced by an open plan's `as_of`. Sweeps read *listings*, not sorts (`sweep_manifest.py:11-15`), so this matters to §3's raw window, not here. The check is cheap and is kept for both.

**Order, per deployment:**
1. Ship S1, the broken-run refusal and the `largest` port to the site. Set `FILTER_INDEXED_ONLY=1` and unset `SEARCH_SIDECARS`.
2. Run the verify markers for every scan.
3. `index-retire -n`.
4. Ryan's go.
5. Retire the D1 rows (instant and reversible by `index-sync`, while the files exist).
6. Wait out the grace, then watch probes and healthcheck for two scans.
7. Delete the files on GCS (gcs has 7-day soft delete, [interval-store.md] §4), then the R2 copies.

R2 has no soft delete. On r2, hf and cw's R2-only files, `index-retire` **moves** the files to `trash/<date>/…` with a 30-day prefix lifecycle rule, rather than deleting them.

## 3. Raw listings archive

Today's redundancy is a full listing per scan. A day's listing differs from yesterday's by roughly the store's delta. gcs is the only deployment that keeps layer-1. cw and hf keep layer-2 per scan, and r2 keeps nothing (§0).

**What the canonical listing holds:** `bucket, name, size_bytes, created, storage_class_id, generation` (CLAUDE.md "Listing"; `src/disk_tree/find/bulk*.py`). `generation` is exact GCS object identity. It is null for S3, R2 and HF. Sweep manifests use it, or `created` within 1 s where it's null (`sweep_manifest.py:11-15`).

**What the path-level interval store already holds** of the listing, since v2 scans (2026-09-30): every object as a row (583M object versions over 576M paths, 1.01 per path, [interval-store.md] §6), with `path` = `bucket/name` (depth 1 = bucket), `size`, `mtime` (the object's `created`), and classes 2–4 as byte pivots (an object is in one class: Standard = `size − c2 − c3 − c4`). It also holds what the listing does not: owner slices and `last_read`.

It lacks three things:
- `generation`;
- classes beyond 4 (B5);
- for v1-era scans (before 2026-09-30), objects at all: v1 indexes were dirs only.

### The options

**(a) Keep as is** (gcs layer-1 forever, Coldline after 30 days, per [interval-store.md] §4; cw and hf layer-2 forever).
- **Storage:**
  - gcs: 10.7 GB/scan. 365 daily scans is 3.9 TB a year, more with sub-daily scans.
    - Year 1 ends at ~30 × 10.7 GB Standard (~$6.4/month) + ~3.6 TB Coldline (~$14/month at $0.004/GB-month).
    - Year 5: ~19.5 TB, ~$80/month. Coldline reads cost $0.02/GB, ~$0.21 per scan regenerated.
  - cw: 54.5 GiB for ~99 scans after the 2026-09-30 zstd re-encode (~0.55 GiB/scan), on GCS and in the R2 archive (`cw-run.sh:257` "R2 is the archive"). At 151 scans that's ~83 GiB per copy (extrapolated, unmeasured).
  - hf, r2: not measured here (`disk-tree listing-format` / `aws s3 ls --summarize` over `listing/*/`).
- **Regenerates:** everything, for any scan. Layer-2, the path store, both stores, an exact deletion manifest (with `generation`). The gcs owner-axis caveat applies (§1.5).
- **Cost to maintain:** nothing new.
- **Risk:** lowest. But it's the unbounded linear growth the goal is about, and r2 still has no archive.

**(b) Object-level interval store** over the canonical columns as the long-term source of truth, raw listings kept for a rolling N days.
- **Design:** key `(bucket, name)`; a version changes on `generation` (GCS) or `(size, created, storage_class_id)` (S3/R2/HF). Same versions, dyadic segments, runs and binary counter as the path store, without dirs or slices.
- **Storage (gcs):** base ≈ one listing (~10.7 GB or less sorted by key under zstd). Per scan, the objects opened plus closed:
  - objects churn ~1.01 versions per path ([interval-store.md] §6), so a typical day is ~1–5M rows, ~0.02–0.1 GB at the listing's ~18 B/row;
  - a sweep day closes 58–126M versions (§3 "Not built"), ~1–2 GB.
  - About 10.7 GB + ~50 GB a year, plus N × 10.7 GB rolling. For cw, hf and r2 it's proportionally tiny.
- **Regenerates:** the exact listing at any scan after the base, so everything (a) does, `generation` included.
- **Cost to build:** a new kernel. `append_intervals` is path-keyed and carries derived columns, so it needs an object variant. Plus its own base build, append, compaction, prune and verify (export ∘ append = identity).
- **Cost to maintain:** gcs staged on Batch, on the order of the path store's append (≈ $0.77/scan measured, [interval-store.md] §6.3), so ~$20–25/month. That's *more* than (a)'s Coldline bill for gcs's first ~18 months.
- **Risk:** a third store to babysit for years (compaction, generations, B2-style catch-up), for a column (`generation`) only sweeps use, and sweeps work on recent scans. This is [interval-store.md]'s open question ("Should the raw listings themselves be interval-coded?").

**(c) Keep only layer-2 per scan** (cw's and hf's model today).
- **Storage:** smaller than layer-1 for cw (layer-1 is never kept). For gcs, layer-2 rows include dirs (~780M rows/scan against ~595M objects), so it isn't obviously smaller than gcs's layer-1. Not measured. It's still a full copy per scan.
- **Regenerates:** the path store and both stores, exactly (`index-write` reads it). It loses `generation`, so a deletion manifest can't be built from it with GCS identity.
- **Risk:** the same linear growth as (a), at a smaller constant. It's redundant with the store, which holds the same per-path rows, verified at append.

**(d) Recommended. A rolling raw window plus monthly keyframes. The path-level interval store is the long-term source of truth, with `interval-store export` (P1).**
- **What's kept:**
  - Raw listings, layer-1 where the deployment lists it, for the last **N = 30 days**. Also anything an open plan's `as_of` names (§2 gate 6): the sweep manifest's identity check needs that scan's listing (`sweep_manifest.py:11-15`).
  - The first scan of each month as a **keyframe**, moved to an archive class: GCS Archive is $0.0012/GB-month, 365-day minimum; on R2, Infrequent Access.
  - cw and hf apply the same rule to their layer-2 (they keep no layer-1). r2 starts keeping its listing (it keeps none today).
- **Storage:**
  - gcs: 321 GB rolling Standard (~$6.4/month, flat) + 128 GB/year of keyframes (~$0.15/month/year).
  - cw: ~17 GiB rolling (30 days × ~2 scans/day × ~0.28 GiB), from ~83+ GiB growing, per copy.
  - Year 5 for gcs is ~$7/month, against (a)'s ~$80.
- **Regenerates:**
  - within 30 days: everything, exact deletion manifests included;
  - any scan, ever: its path sort, by export. That gives a new static or interval generation, any view, diff or lens, and layer-2-equivalent rows;
  - any keyframe month: a fresh re-aggregation from its raw listing (insurance against a bug *upstream* of the path sort, in `path-index` or `import`, that the append-time digest can't see).
- **Loses:**
  - `generation` and exact per-object listing rows older than 30 days, except keyframes;
  - for hf, classes 5/6 until B5;
  - for v1-era gcs scans (before 2026-09-30), objects. The v1 indexes never had them, so nothing is lost relative to today's sorts, only relative to today's raw listings.
- **Cost to build:** P1 (export, small: it reads `pvl`/`sv` at one `D` as the reader does) and a lifecycle job (keyframe tagging + prefix lifecycle rules). No new store.
- **Risk:**
  - the store is the only copy of old scans, so a store-corrupting bug past 30 days is unrecoverable except at keyframes;
  - mitigations: per-range digest at append; the verify marker per scan (§2 gate 3); immutable generations and manifests; GCS soft delete; R2 `trash/`.
  - The owner axis is already store-only today (attribution inputs are mutable, §1.5), so (d) adds no new loss there.

**(e) Other variants considered:**
- (d) without keyframes: saves ~$0.15/month a year, but loses the only defense against an aggregation bug found late. Not worth it.
- (d) plus `generation` as an unserved side column of the store's object versions: closes (d)'s one gap for sweeps on old scans. It costs ~4–5 GB on gcs's base (high-entropy int64 per object) plus a store schema change. Defer until a sweep needs an old `as_of` (gate 6 already keeps those listings).

**Recommendation: (d)**, with P1 built and tested before any deployment drops a listing older than 30 days. (b) is the path if exact object history becomes a product requirement.

### 3.1 Object versions live in the interval store (Ryan, 2026-10-15)

The one thing an open plan needs from a raw listing is each object's identity at the plan's scan, so a reviewed deletion never resolves to a replacement at the same path (`sweep_manifest.load_holds` / `apply_hold`: same `generation`, else `created` within 1 s). The interval store answers that instead, so raw listings can follow (d)'s window even with plans open:

- **Default: `mtime` (created) is the version.** Every lister already records it, the interval store already carries it per object, and the executor's fallback rule already treats "same name, `created` within 1 s" as the same object. GCS objects are immutable (a replacement is a new creation), S3/R2/CW `LastModified` is per second, so only a same-size overwrite inside one second would pass — not worth more machinery by default.
- **Opt-in per deployment: the store's native version** — GCS `generation`, S3/R2/CW `ETag` (in every `ListObjectsV2` page; `aws s3 ls` just doesn't print it), HF the LFS `oid` / blob id. One nullable column in the listing (`generation` exists for GCS since 2026-10-07; the S3 lister would add `etag`), carried by the interval store's versions like `mtime`, and used by the hold (and a guarded delete: GCS `ifGenerationMatch`) where present.
- Until a deployment has it (gcs: below), the raw-listing window must cover its open plans (as in (d)); gcs, with a backfilled generation, can let them go.

**Built (2026-10-18, GCS `generation`; approved by Ryan 2026-10-18):**
- **Off means absent.** A deployment without the opt-in carries no `generation` column anywhere — per-scan sorts, interval-store range files, deltas, served sorts, exports — not even an all-NULL one. Its files are byte-identical to before (`tests/test_generation_off.py`: per-scan sorts, a whole store, every export, against digests taken before the change; `UPDATE_GOLDEN=1` rewrites them). Only gcs turns it on.
- **Plumbing: `path-index -G` / `$PATH_INDEX_GENERATION`** (`viz.write_path_index(generation=True)`): each object row of the per-scan `path`/`bysize` sorts carries the listing's `generation` as the store's last column, NULL on dirs (`test_viz.py::test_generation_is_opt_in`). `index-write` passes a layer-2's `generation` through when a source has it (`index.store_columns`). The engine's `import` (cw, hf: no generations) doesn't produce it.
- **Interval store: `scans.json` `"generation": true`** (`interval_store.has_generation`; a build sets it when some scan's sort carries the column, `generation_of`; the staged `cut` records it from the folded files). Then `generation` is the last column of the path versions (`pv/`, `pvl/`), of a run's `pvl` deltas and open state (before `pvf`), and of the served `path` sort only (`GENERATION_SORT`): `bysize`, `reads` and the slice sorts never carry it. It is carried, never a change column (`carried(generation)`: the version's first non-NULL in a build; an append gives a version opened at the scan its object's, a continuing version keeps its own; a piece opened by a read day alone takes its path version's). A GCS rewrite is a new object created — and `updated`, the version's `mtime` — after the scan that listed the old one, so a new generation always opens a version by `mtime` already; a metadata update (new `updated`, same generation) opens a version with the same generation. Threaded through append (`append_range`, `state_schema`/`delta_schema`), carries (`merge_range`, `cut_run`, `carry`), compaction (`interval_compact.fold_gen`), the staged stages (`ranges`, `publish`, `carry`), `export` (last column of a v2 export, from the `path` sort's live version; joined by path on a sliced generation) and `verify` (`interval_verify.generation_parity`: per sampled scan, the scan's objects with a generation against the store's live version; recorded in `verify/<id>.json` as `generation: {objects, equal}` and required for `pass`, only where both sides carry it). `test_interval_generation.py`: versions through build, append and carries equal a rebuild's, compaction keeps them, exports reproduce each scan's, the off store has the column nowhere, verify passes with every object equal.
- **Served-read cost: none on row data.** The site projects `IV_ROW_COLUMNS` and fetches one byte span from the first to the last projected column's chunk (`chunkSpan`); `generation` is the sort's last column, so it is never fetched or decoded. What grows: the `path` sort's size by the column (high-entropy int64 per object version: on gcs's ~583M object versions on the order of 4–5 GB of R2, one copy, not in `bysize`), one chunk record per row group in `.groups.parquet` `rg_json`, one schema element. A per-scan `path` sort read whole by `search.ts`'s unprojected spans would fetch it (gcs serves search from the static index only).
- **Class columns named once.** `interval_store.CLASS_COLS` (`{2: "c2", 3: "c3", 4: "c4"}`) names the store's storage-class columns for every schema, statement, hash and Python reader; `IV_CLASS_COLS` in `site/functions/_lib/index.ts` for the site's interval reader. A deployment that names its classes (gcs: `nearline_bytes`, `coldline_bytes`, `archive_bytes`) changes those two lines on its branch; per-scan wire names (`sum_storage_class_id_<k>`, a v1 index's `c<k>`) are not the store's and stay. A fold (`interval_compact._versions_sql`: compaction, backfill) reads an older generation's class columns by position (they follow `wb`), so the rename rides the next new generation.
- **Backfill: `dt-cloud interval-store backfill-generation [-g GEN] -l TEMPLATE [-M MEM] -o STORE -O NEW [-p THREADS] [-r RG] -s ROOT [-w WORK]`** (`interval_backfill.py`). Folds generation GEN (default the current; the base and the newest manifest's runs) into the new generation NEW, as a compaction writes one (`fold_gen`), with each object version's `generation` from the raw listings (`TEMPLATE` under `ROOT`, `{id}` the scan id, globs allowed; a URL root's shards are copied in a scan at a time): the generation of the first scan within `[vf, vt)` whose listing has one. Each listing is read once into `(depth, path, generation, first_ts, last_ts)` (`gen-index.parquet`, sorted by path), which each key range joins (`filler`); a scan whose listing has no `generation` column, or none at all, is skipped (reported). Versions that ended before the first listing with generations stay NULL, as do dirs. NEW gets `"generation": true` and `backfill.json` (`from`, `manifest`, `runs`, `scans`, `listings`, `listed`, `filled`); it is published data first, `scans.json` last, never over an existing generation, and refused on a source that already carries it. GEN and `CURRENT.json` are untouched: verify NEW (`interval-store verify`, `export`), then `interval-store current -s NEW`; `add -g NEW` appends what GEN gained meanwhile (from the scans' sorts). No `successor.json`/`compaction.json`, so nothing prunes GEN automatically. Tests: the backfilled generation's versions are exactly the ones a build over generation-bearing sorts gives (earlier versions NULL), `backfill.json` exact, the source byte-identical after, refusals, an append onto it carries `generation` and its verify marker passes with every object equal, and a source with renamed class columns backfills to the same bytes.
- **Raw listings retire with their scan (2026-10-18):** `scan retire -1 TEMPLATE` (class `layer1`; scan step `options.retire.layer1: true` → `<layout.listing>*/shard-*.parquet`), under the same selection as the per-scan sorts. A scan whose shards carry a non-null `generation` keeps them (`held`) unless its marker compared every object's generation equal (`generation_parity`): until then the listing holds what the store doesn't. So the storage a listing adds is O(changes) once the IS carries it, not O(#scans). `-b` backups copy server side within one store (gcsfs rewrite, s3fs `CopyObject`/`UploadPartCopy`), 8 at a time. `interval-store verify-generation -g GEN -s ROOT -l TEMPLATE [IDS]` compares the store's generations at each scan with the scan's raw listing itself (so a scan whose sort predates `path-index -G` is covered too) and records it on the scan's marker (`generation.against: listing`, `views_gen`: where the views were compared; `pass` then needs every listed generation equal). Markers are found through compactions and backfills (`backfill.json` `from`: the same versions).
  - **gcs would run** (on a Batch VM or node `mgu`, not a laptop; the source generation's range files are mirrored into `-w`): `dt-cloud interval-store backfill-generation -o gs://<interval-store bucket> -O <current>.g -s /mnt/<data bucket> -l 'listing/{id}/*/*.parquet' -w /stage/work -M 100GB -p 16 -j 8` (`-j`: 8 key ranges, then 8 served sorts, at a time on one DuckDB database; each ends in a serial pyarrow writer, the serial fold's bottleneck: 256 ranges × ~21 s and ~33 min per sort; same bytes at any `-j`) (`-s` a gcsfuse mount of the listings' bucket, so shards are read in place; listings from 2026-10-07 on carry `generation`), then `interval-store r2-copy -g <current>.g`, verify, `current -s <current>.g`, and `PATH_INDEX_GENERATION=1` on `path-index` in the job so later appends carry it (an append to a generation with it from a sort without it gives new versions NULL; `add` warns of the opposite case). The class-column rename on the gcs branch rides this same rebuild.
- **Sweeps from the store: `sweep manifest [-g GEN] [-H auto|listing|store] [-i STORE]`** (`sweep_manifest.HoldSource`). An item staged as of another scan resolves its objects' identity from that scan's raw listing when it has shards (or no store is given: `auto`, the default), else from the interval store: `interval_export.object_identities` reads the `path` sort's row groups whose paths meet the items and whose versions may be live at the scan (`.groups.parquet` `p_min`/`p_max`, `vf_min`/`vt_max`; base and the newest manifest's runs, the smallest `vt`), the object versions live there, as `(name, generation, created)` — the version's `generation` (NULL without it) and created second (its mean stamp, `wts / wb`; else `mtime`). `apply_hold`'s rule is unchanged: same generation where both have one, else `created` within 1 s. Tests: the store's identities at a scan exact; a held item resolves the same manifest from the listing, from a store with generations, from one without (by `created`), and with the listing deleted (`auto`), and through the CLI (`-H store -i`, refused without `-i`) — unchanged objects kept (a metadata update keeps its generation), the replaced one (`rew`: a new generation) and a new one refused.
- **Not built:** S3/R2/CW `ETag` and HF `oid` (no lister records them yet); the staged (Batch) build's ranges read the flag from the sources per range, not once; a guarded delete by `ifGenerationMatch` in the executor.

## 4. Rollout

**Prerequisites (root, `[cloud]`), in order:**
- **P1:** `interval-store export` + the identity test (B3).
- **B4:** the run's `scans.json` root.
- **S1:** the scan list and starts from the store (§1.4).
- **Site:** the broken-run refusal and the `largest` port (§1.3). Done 2026-10-18 (`largest` was already ported; the refusal, the held-date guard in `openIndex` and the `/api/path-index` 410 landed). Still open on the site: S1 (§1.4), and `SEARCH_SIDECARS` off where set (r2, hf: a config change, §1.3's sidecar row).
- **`pending/` staging:**
  - `path-index`/`index-write` write only the `path` sort when asked (`--path-only`: no `bysize`, `-by-user`, coarse tiers or sidecars);
  - a verify-marker writer;
  - a `pending/` cleaner gated on both manifests + the marker.
- **P3:** `index-retire`.
- **P2:** classes generalized (needed for hf before its layer-2 goes).

**Per deployment, the checklist** (each step a go from Ryan where it writes to prod):
1. Site:
   - `PATH_STORE=intervals`;
   - `FILTER_INDEXED_ONLY=1`;
   - `SEARCH_SIDECARS` unset;
   - S1, `largest` and the broken-run refusal deployed;
   - CIC: root, a drill, a diff, a lens (gcs), `?f=`, `/api/largest`.
2. Job:
   - aggregate to `path` only;
   - stage at `pending/`;
   - `static-names add` + `interval-store add` from `pending/`;
   - verify marker;
   - clean `pending/`;
   - drop `index-sync` (`path`/`bysize`), `index-gc` and `publish-r2`'s sort copies;
   - the "already scanned" check becomes "the interval manifest lists the id".
3. Backlog: §2 order (`index-retire -n` → go → D1 rows → grace → files).
4. Raw listings: (d)'s lifecycle (rolling 30 days, monthly keyframes, `as_of` holds), applied `-n` first.
5. Watch: `probe`/`healthcheck` for two scans, `pending/` empty, the stores' manifests advancing.

**Order:**
1. **r2 demo** (r2.rbw.sh). Smallest, one machine, already `PATH_STORE=intervals`.
   - Steps 1–2 change `daily-ingest.yml`:
     - drop `-S` (`:85`) and the tier upload (`:96`);
     - stage to `pending/` and keep `-L 'pending/{id}/path-index.parquet'` for the two `add`s (`:123`, `:142`);
     - drop `index-sync` (`:108`) once S1 ships;
     - make the `scanned` check (`:66-71`) the manifest.
   - Step 4 *adds* a listing upload (`listing/<id>/<bucket>/`), rolling 30 days, since r2 keeps none today.
   - The r2 session adopts these as they land.
2. **r2** (r2.oa.dev). The demo's one-machine shape plus a GCS duplicate of every per-scan tree: drop both copies' writes, retire both backlogs (GCS 2.2 GB, R2 1.7 GB), and add a rolling 30-day listing upload (it keeps none today). /meta has the same per-scan readers at ~12 KB/scan.
3. **hf.** Same one-machine shape (`hf-run.sh`). Its layer-2 rule waits on P2.
4. **cw-s3.**
   - The interval store is already one machine from R2 (`cw-run.sh:274-278`).
   - The static index is staged, so `pending/` is a GCS key in the static profile's bucket (B1').
   - The R2 archive copy of layer-2 follows (d)'s window.
   - It has 151 scans of per-scan tiers to retire (110.8 GiB measured 2026-09-30, several generations per old scan).
5. **gcs.** Both stores staged (`pending/` in the profile bucket); `dir-cache/` latest 7; `attr.tsv` kept; the largest backlog (21.7 GB/scan of path stores, ~71+ scans); raw listings (10.7 GB/scan) to (d).
6. **m3.** It needs an interval store first: `interval-store build` from its existing per-scan sorts, *before* any retention, plus `INTERVAL_STORE_GEN` and `PATH_STORE` in m3's `site/wrangler.toml`. Then the same checklist. Capture shards (`captures/m3/…`) get (d)'s window.

## Open questions

- N = 30 days for the raw window: long enough for a sweep's review cycle and to notice an aggregation bug? Gate 6 covers open plans whatever N is.
- Keyframe cadence (monthly) and class (GCS Archive has a 365-day minimum and retrieval fees; Coldline is 90 days).
- `/api/path-index`: retire it, or serve export output on demand?
- Age pyramids: an interval age index (the store has `wts`/`mtime_mean` per version), so `index_schema`, `index-sync` and the last per-scan files go entirely.

[interval-store.md]: interval-store.md
[d1-migrations.md]: d1-migrations.md
