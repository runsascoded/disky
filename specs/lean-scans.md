# Lean scans: append straight into the stores, keep only what can't be derived

**Status:** draft, 2026-10-15. A proposal. Nothing here has been built, deleted or deployed. It extends [interval-store.md] §4–§5 (retention, migration) from gcs to every deployment.

**Goal (Ryan):** lean, harmonized deployments (gcs, cw-s3, m3, r2, hf, the r2.rbw.sh demo). Each scan keeps only what can't be derived elsewhere. Every deployment runs the same search: the prebuilt static name index only (`FILTER_INDEXED_ONLY=1`), with no per-scan search sidecars. And a deployment can be left alone for months or years.

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
| `openIndex` per-scan path (`_lib/index.ts:431-466`) | any date or variant `openInterval` returns null for (`:741-759`): a scan the generation doesn't hold, variants outside `IV_SORTS`/`IV_SLICE_SORTS` (`:521-530`) | none. Every listed scan is in the store (§1.4 S1 lists scans from the store). Anything else is an honest "not indexed" |
| broken-run fallback (`ivMarkBroken`/`ivRetry`, `_lib/index.ts:560-622`) | a broken run's scans, and every later run's, "read per-scan" | **change**: with no per-scan sort, the retry must refuse (503 + `Retry-After`, the broken TTL is 30 s) rather than read a missing pointer. A run is never served partially, and there is no fallback to a missing index: an honest refusal, never a wrong answer |
| `/api/largest` (`api/largest.ts:14-19`, `:65`, `:72`) | `perScan(env)`: pages a per-scan `bysize` | **port** `readLargest` (`_lib/index.ts:2356`) to the folded `bysize` plus runs: objects only, live at D (`vf ≤ D < vt`, combined across tiers as `combineLive`). Or retire the endpoint. Until then it 409s on every store-only scan, by its own contract (`largest.ts:18-19`) |
| search sidecars (`_lib/view.ts:886`, `:970`, `:1051`; `_lib/search.ts:100-103`) | `SEARCH_SIDECARS=1` on r2 and hf: `perScan(env)` `rows-search`/trigrams | `FILTER_INDEXED_ONLY=1`, `SEARCH_SIDECARS` unset (gcs and cw already). Anchored terms (`^q`, `q$`) are then declined where no `anchors/` is built (r2: `wrangler.r2.toml:52`): the same behavior everywhere |
| `/api/path-index` (`api/path-index.ts:1-20`) | ranged raw download of a scan's per-scan `path` sort | retire for store-only scans, or serve P1's export output (written on demand to a short-TTL key) |
| age pyramids (`api/age-pyramid.ts:116`, `:133`) | per-scan `age-pyramid-<bin>` through D1 pointers | stay per-scan until an interval age index replaces them. They're small, live in the same `index/<gen>/` dir, and §2 keeps them |
| over time (`_lib/overTime.ts:67`) | an archive-level `over-time` variant, not per scan | unaffected |
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

  All of them key on `variant = 'path'` rows. Proposal: list scans from the store (`ivScans`, `_lib/index.ts:710`, already per isolate), and carry each scan's `started` into its run's `scans.json` / manifest `stamps` (from the snapshot `meta.json`, which `index-sync -m` reads today). Then `index_schema` keeps only the age-pyramid and over-time rows.
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
- Until that lands, the raw-listing window must cover open plans (as in (d)).

## 4. Rollout

**Prerequisites (root, `[cloud]`), in order:**
- **P1:** `interval-store export` + the identity test (B3).
- **B4:** the run's `scans.json` root.
- **S1:** the scan list and starts from the store (§1.4).
- **Site:** the broken-run refusal and the `largest` port (§1.3).
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
