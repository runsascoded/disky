# Index landscape: what we built, what depends on what, and what we'd keep

Status: audit, 2026-10-10. Read-only survey of `cloud` @ `d84d9c64`, `gcs` @ `a79b93da`, `cw-s3` @ `0ba9ad72` and `m3` @ `ef17b432`. No live cloud state (GCS, R2, D1, KV, VMs) was queried; where a claim depends on it, the text says so. File refs are to `cloud` unless prefixed `gcs:`, `cw:` or `m3:`.

## Why this audit (Ryan's framing)

- We have effectively built a small database: several independent index structures, plus readers and a planner that choose among them.
- The worry: special cases hung off the indexes, text search especially, may be arbitrary and brittle ("whack-a-mole"). How much of it is principled?
- **Per-deployment tailoring is the tension.**
  - Disjoint index sets per deployment are harder to reason about and more brittle.
  - But fleets really differ: long hex runs are common in cw and rare in gcs, so one index set can't be optimal for both.
  - If we tailor, it should be **declarative**: index shape is a function of measured corpus properties, re-evaluated regularly. The result should be the same for a new fleet and a long-running one, with hysteresis ("activation energy") so expensive backfills don't thrash.
  - Not a self-driving database.
- **Preference order:**
  1. simple bespoke indexes that suffice;
  2. off-the-shelf components (D1, R2, Workers and parquet/SQLite have given the "60–80%");
  3. if general machinery is unavoidable, build it generally and reusably, filling a real gap in the ecosystem, not grown organically with fleet-specific special cases.
- A working stack is landing now. This is the moment to reassess and pay down debt: make it look like what we'd have built had we known the end state, without scars from abandoned approaches.

## Executive summary

- **The thresholds are principled; the structure is where the mole-holes are.**
  - V = R = 100K, row groups of 8K, `START_MAX_ROWS` / `START_MAX_HITS` and the 50M-row shard target are each derived from a measurement. K = 256 is the exception: no rationale was found.
  - No reader or builder holds a term list or a per-fleet branch. Heaviness always comes from index statistics.
  - The brittleness comes from elsewhere:
    - five independent liveness stacks inside the static index (light, catalog, drill, anchors, starts-with catalog);
    - two path stores with a per-date fallback between them;
    - eleven deployment flags whose settings drifted for historical reasons.
- **89 reader decision points**:

  | Class | Count |
  |---|---:|
  | (a) intrinsic limit, stated honestly | 17 |
  | (b) safety guard | 19 |
  | (c) capability skew across deployments or generations | 30 |
  | (d) legacy or transition path | 23 |

  The 53 points in (c) and (d) are the debt. Most of them disappear with one capability manifest per index plus three deletions (per-scan reads for store-held dates, v1 tiers, the query box).
- **Per-deployment differences:**
  - Two rest on measured corpus properties: the hex-run share, and whether owner labels exist.
  - Two are input availability (access logs on gcs only; dir-cache serves gcs's re-attribution).
  - The other ~10 are historical: rollout order, config drift, or which session built what.
- **Anchored search is not one folded index.**
  - `$` is implicit in the suffix shards.
  - `^` uses a real sentinel (`/` + name), but those rows live in a separate `names/` shard set.
  - Heavy anchored terms use three separate rollup families.
  - Interleaving `names/` into `sx/` is +6% rows and is free at a compaction. It would also give heavy `^q` below the root through the existing contains machinery.
- **The two append pipelines share about 380 lines of copy-pasted machinery.** The interval store still carries pairwise inside `publish`, which is the failure mode the static pipeline fixed in `5343fa32`. One runner removes ~250–350 LOC and fixes that.

## 1. Inventory: every index and tier built or served

Deployments: **gcs** = gcs.oa.dev, **cw** = cw-s3.oa.dev, **r2** = r2.rbw.sh demo, **m3** = the personal laptop deployment.

### 1.1 Per-scan path store

Layout: `<l2root>/index/<gen>/`, one generation per run (`gcs:job/run.sh:76-82`).
- gcs: l2root = `listing/<scan>`, on GCS.
- cw: `cw-l2/<scan>`, on GCS, copied to R2 (`cw:job/cw-run.sh:244`).
- r2: R2 `disk-tree-demo/listing/<date>`.
- m3: R2 `disk-tree/listing/<store>/<date>`.

Pointers and footers are in D1 (`index_schema`, `index_row_groups`). File names come from `indexKey` (`site/functions/_lib/index.ts:288-303`), which mirrors `INDEX_VARIANTS` (`cloud/src/dt_cloud/index_footer.py:533-560`).

| Tier | Answers | Builder / when | Size (spec) | Deployments | Readers | Status |
|---|---|---|---|---|---|---|
| `path` (`path-index.parquet`, v2 objects + dirs, sorted `(depth, path)`) | P's row, point lookups, diff asks, depth bands | `find/tiers.py:135` `write_tiers`. Reached from `dt-cloud path-index` (`cli.py:405`), `index-write` (`cli.py:3327`), `disk-tree tiers` and `import -i`. Per scan: `gcs:job/run.sh:348-372`, `cw:job/cw-run.sh:221`, `.github/workflows/daily-ingest.yml:61-66`, `m3:infra/aws/ingest.sh:55` | gcs 14.64 GB/scan, 777M rows, 94,864 groups; cw 1,018 MiB (`path-store.md:226`) | all | `openIndex` (`index.ts:397`) → `view.ts` → `api/subtree.ts:147`, `diff.ts:113`, `series.ts:218-230`; `ownerTotals.ts:40`, `prefixes.ts:62`, `api/path-index.ts:56` | built + read. For gcs dates the interval store holds, read only for scan existence, search and `/api/path-index` |
| `bysize` (`(⌊log2 size⌋ desc, path)`; keyed on `tot` over label slices) | thresholded subtree reads | same writers; re-cut by `dt-cloud index-recut` (`cli.py:3361`). The gcs v2 scans were re-cut into `index/20261009T213000Z/` (`bysize-path-total.md:61`). Unverified: whether the pointers were synced on prod | gcs 12.89 GB/scan; cw 1,188 MiB | all | `view.ts:436-451`, `keyedOnTotal` (`index.ts:2186`), `readSizeRects` (`index.ts:2205`) | built + read |
| `bysize-by-user` | lens / user-estate views | `path-index -u bysize` (`gcs:job/run.sh:352`) | ≈ `bysize` | gcs | `view.ts:436,447`, `lensSorted` (`index.ts:168`) | built + read |
| `user` (`path-index-by-user`, v1) | v1 lens reads | not written on v2. Retired from D1 when a v2 `path` syncs (`index_footer.py:450-463`) | — | gcs v1 dates | `lensSort` (`view.ts:2162-2167`), `rootRows` (`view.ts:516`) | read only (legacy) |
| `coarse{16,20,24}[-user]` (v1 dir-only floor tiers) | v1 threshold reads | not built. D1 pointers survive: `retire_d1` covers only `SORT_VARIANTS` (`index_footer.py:544,570`) | — | gcs v1 dates (pre-2026-09-30) | `view.ts:55-56,483,507,683-693,1527`; `filter.ts:60`; `interval_verify.py:249,369` | read only (legacy) |
| v1 dir-only `path` (`index_schema.version=1`, wire columns `b,o,wts,…`) | everything on v1 dates | not built | fine tier 220M rows (`path-store.md:86`) | gcs history | `toRowV1` (`index.ts:1172`), `V1_ROW_COLUMNS` (`index.ts:775`); mixed-version diff folds file rows (`view.ts:1900-1902`) | read only (legacy) |
| `dirs` / `objects` tiers | — | gone: `TIERS=('path','bysize')` (`find/tiers.py:60`), `parse_tiers` rejects others (`:98-102`) | — | — | only stale references in gcs `GATE=1` mode (`gcs:job/run.sh:320-323`, `cascade-a2a` `cli.py:5102`), which would now fail | neither (stale reference) |
| `.groups.json` footer blob | whole-footer fallback | `write_tiers -g`, `sync_d1` (`index_footer.py:431-433`), `index-blob` (`cli.py:3690`) | gcs 86 MB/sort, too big for a Worker (`path-store.md:93,226`) | all | `openBlob` (`index.ts:823-846`), only when there are no D1 rows and no `.groups.parquet` | built; read only as the last fallback (legacy) |
| `.groups.parquet` cold footer (`groups_v=1`, 512 rows/group, `find/groups.py:77`) | footer stats past D1 retention | same writers; `index-blob -P` backfill | ~48 MB/scan for 3 gcs sorts (`path-store.md:106`) | all; used only on gcs (`-r 30`) | `openFooter` (`index.ts:933`), `pqGroups` (`:1148`) | built + read |
| D1 footers (`index_row_groups` + `index_schema`) | row-group selection as SQL (`selectSpans1` `index.ts:1768`, `selectSizeSpans1` `:2104`) | `dt-cloud index-sync` (`cli.py:3513`, `sync_d1` `index_footer.py:389`), per scan in every job, pointer last | ~218 MB/scan for 3 sorts at 8K; gcs `INDEX_RETAIN=30` ≈ 6.5 GB (`gcs:job/run.sh:549-559`) | all; cw has no retention | `openIndex` (`index.ts:410-418`), `pathGens` (`:333`), `pathScans` (`:361`), `indexedScan` (`scanArg.ts:25`) | built + read |
| Retention / GC | — | `index-gc` (`cli.py:3579`): `gc_d1`; `-r N` → `retire_d1`; `-F` → `gen_gc.py` (2-day grace). gcs runs `-r 30` (`gcs:job/run.sh:559`); cw runs `-R -F` with no `-r` (`cw:job/cw-run.sh:228,272`) | — | gcs, cw | — | built |
| Search sidecars (`path-index.{rows,trigrams,rows-search,names,search}.parquet`) | segment-name search on the filter view (the no-static-index fallback) | `path-index -S` / `index-write -S` | r2: ~21 MiB beside a 12 MiB `path` (`daily-ingest.yml:4-5`) | r2 only (gcs only with `PATH_INDEX_SEARCH=1`, off; `gcs:job/run.sh:353`) | `search.ts:35-39,387`; `view.ts:842,894-895` (under the interval store through `perScan()`) | built (r2) + read (probed everywhere) |
| Layer-2 scan blobs | builder input | `disk-tree import`, cw `cw-l2/<scan>/<bucket>.parquet` (`cw:job/cw-run.sh:209-210`) | cw ≈ 3.6 of 4.2 GiB/scan (`listing-slim.md:7`) | cw, m3, local | builders only | built |
| Listing formats v1 / v2 (`listing_format.py:47-52`) | — | `disk-tree recompress` (`cli/recompress.py:26`) | v2 ≈ 15–23% of v1 (`listing-slim.md:31`) | — | tiers inherit the format | independent of `index_schema.version` |

### 1.2 Interval store (gcs only)

Data lives in `gs://oa-gcs-usage-dvx/interval-store/<gen>/`, served from R2 `oa-gcs-usage-index` under the same keys. The profile is `cloud/src/dt_cloud/interval_profiles/gcs.json`. gcs prod reads it: `PATH_STORE="intervals"`, `INTERVAL_STORE_GEN="2026-10-09b"` (`gcs:site/wrangler.toml:53-54`, commit `1db67e1a`; whether this is deployed was not checked). `specs/interval-store.md:173-178` still calls this opt-in, and `specs/interval-store-append-gcs.md` still says the append is off by default. Both are stale: the job defaults `INTERVAL_STORE_APPEND:-1` (`gcs:job/run.sh:418-438`).

| Artifact | Answers | Builder / when | Size | Readers | Status |
|---|---|---|---|---|---|
| Range files `pv/ rd/ pvl/ sv/ svt/ digest/ sv-digest/`, `ranges.json`, `scans.json` | input to cut, verify, append | `interval-store build/fold` (`interval_store.py:568,647`), once per generation | pv 14.1 GB, rd 0.22 GB (`interval-store.md:229-230`) | `cut`, `verify`, append open state | built (GCS only) |
| `served/path`, `served/bysize` (+ `.groups.parquet` with `vf/vt/b_min/seg`, `GROUPS_SCHEMA` `interval_store.py:398`) | every plain view at any held scan | `interval-store cut` (`:689`) + `r2-copy` (`:802`) | 19.24 / 16.75 GB, 141,227 groups each; footer 527 KB (`interval-store.md:282-283,119`) | `openInterval` (`index.ts:662`), `ivFooter` (`:486`), `overTiers` (`:1745`), `combineLive` (`:1510`), drilled views `sizeNeighbours` (`:2082`), `IvGroup` (`:1436`) | built + read |
| `served/slices`, `slices-bytotal`, `slices-bysize-user` | owner lens / pool / class / `by` views | cut | 19.24 / 18.34 / 16.74 GB (`interval-store.md:284-287`) | `slices(env)` → `IV_SLICE_SORTS` (`index.ts:456,479`); `view.ts:1681`; `ownerTotals.ts:40`; `prefixes.ts:62` | built + read |
| `served/slices-bysize` | nothing | no longer cut: dropped from `SORTS` in `fb9ded0c` (`interval_store.py:386-395`); excluded from `RUN_SORTS` (`interval_append.py:60-62`) | **16.74 GB on GCS and on R2** (`interval-store.md:285,290`) | none (`IV_SLICE_SORTS` maps elsewhere) | **stored, unread** |
| `served/reads` | `last_read` for gens whose `path` lacks it | cut; copied by `R2_SERVED` (`interval_store.py:799`); runs never carry it | from rd 0.22 GB | `ivLastRead` (`index.ts:733`), called only when `path` lacks `last_read` (`view.ts:1404`). `2026-10-09b`'s `path` has it (cut from `pvl`) | built; **unread on the live gen** |
| Runs `deltas/<first>[_<last>]/served/<sort>.{parquet,groups.parquet}` + `meta.json`, `ranges/`, `scans.json` | the scans since the base | `dt-cloud interval-store append -c` (`interval_append.py:849`), daily in the gcs job, 60 min cap, non-fatal | ≈ 0.66 GB/run, ≈ $0.77/scan; scratch open state ~23 GB (`interval-store.md:333-340`) | `openIvRun` (`index.ts:713`) | built + read |
| `manifests/<scan>.json` | which runs are live | publish | tiny | `ivManifest` (`index.ts:567-586`, R2 list) | built + read |
| `served/<sort>.json`, `served/stats.json`, `verify/` | — | cut, `verify` (`interval_store.py:735,767`) | small | none on the site | built |
| Old gen `2026-10-09` (v3, rev 2) | — | — | — | only if `INTERVAL_STORE_GEN` pointed there | stored; no GC exists for interval generations |
| `interval_read.py` | Python reference reader | — | — | `interval_verify.py:21`, tests | not a serving path |

### 1.3 Static name index (gcs prod; cw preview)

Location: GCS `gs://oa-gcs-usage-dvx/static-names/<gen>/`, R2 `oa-gcs-usage-index` (gcs) and `oa-cw-s3-usage-index` (cw). Never in D1 or KV. `R2_SERVED` (`static_names.py:1523`) lists what is copied to R2. Live gens:
- gcs `2026-10-08c` (`gcs:site/wrangler.toml:103`).
- cw `2026-10-10cw` (`cw:site/wrangler.toml:204-217`, preview env only).

| Artifact | Answers | Builder / when | Size (spec) | Deployments | Readers |
|---|---|---|---|---|---|
| Suffix shards `sx/` + `shards.json`, `sidecar/`, `sidecar.parquet` | contains literal of ≥ 3 characters (light range); `q$` (rows with `s == q`) | `static_names.py` `suffix-map :1301`, `shards :1347`, `sidecar :1379` (SQL `suffix_sql :662`, `plan_shards :628`), once per gen. Runs: `static_append.py` `shards :606` | gcs 13.2B rows, 262 files, 135.7 GB (`search-extensions.md:13-14`); cw 1.47B rows, 33.5 GB (`static-hex-runs.md:102-103`); one run 1–2.4 GB | gcs, cw | `staticNames.ts` `StaticNames :398`, `FirstHits :333`; `TieredNames` (`staticRuns.ts:229`); `SuffixHits` (`staticFilter.ts:109`); `staticAnchors.ts:357,393`; `nameSummaryStatic.ts:101` |
| Path keys (per-group `path` bounds) | scoped reads under P | cut from shard footers at read time (`staticNames.ts:214,443-452`); GCS-only copies `anchors/keys-*` | key tables 1.5 GB, GCS only (`anchored-search.md:82`) | gcs | `staticAnchors.ts:156` |
| Contains catalog `catalog/` (`cells`, `index`, `meta.json`, `members.json`) | per-bucket totals for heavy literals and every 1–2 character literal; membership | `static_catalog.py` `census :553`, `members :588`, `answers :625`, `short :711`, `assemble :752`. Runs: `append :358` | gcs 3.07M cells, 25.2 MB (`architecture/static-name-search.md:203`); cw 6.0 MB | gcs, cw | `StaticCatalog` (`staticCatalog.ts:72`), `TieredCatalog` (`staticRuns.ts:269`), `catalogRoot` (`staticFilter.ts:183`), `staticDrill.ts:630` |
| Catalog census (`catalog/census/`) | prefix counts ≥ 50K, to choose V | `census :553`, `census-check :910` | 159,970 prefixes | build time only | none |
| Drill `drill/` (long/short roots, rollups, two-level indexes, `aliases.parquet`, `meta.json`) | heavy contains literals at any P: roots, else per-child rollups (K = 256) | base `static_roots.py` (`measure :650` … `index :1170`); runs `static_drill.py` `build :1135` | gcs 299 GB served, ~85 VM-h ≈ $19 (`architecture/static-name-search.md:319-330`); cw 40.2 GB; one run ~2.15 GB | gcs, cw | `Drill` (`staticDrill.ts:482`), `DrillSource` (`:667`) |
| Name index `names/` | `^q` (range `/q…`), `^q$` (key `/q`) | `static_anchors.py` `names :1060` (`names_sql :116`); runs from `cdelta` (`:1287`) | 798M rows, 13 shards, 7.5 GB; runs 38–90 MB (`anchored-search.md:78,83-84`) | gcs | `staticAnchors.ts:156,309,357,393` |
| `anchors/` end + exact rollups | heavy `(q$, dir)` and `(^q$, dir)` | `rollups :1131`, `index :1187`; runs `run_rollups :499`; merges `merge_rollups :633` | 3.5M cells; R2 base 7.54 GB incl. names; per scan 12–17 min, ~$0.05 (`anchored-search.md:80-85,115`) | gcs | `staticAnchors.ts:408-417` |
| Starts-with catalog `anchors/start/` | heavy `^q` at the fleet root, per bucket | `start :1221`; runs `start_run :347` | 2,788 prefixes, 720 KB, ≈ $0.3 (`anchored-search.md:140-144`) | gcs | `staticAnchors.ts:281,311-322` |
| `^q` census `anchors/census-start.*` | heavy `^q` heads | `census :1089` | 7,863 prefixes ≥ 50K | build time | `start_heads_sql` |
| Hex-run rule (recorded in `scans.json`, `catalog/meta.json`, `anchors/meta.json`) | drops the inner suffixes of ≥ 16-digit hex runs | profile `hex_runs="16,8"` (`static_profile_examples.py:15-37`); runtime `hex_runs.py` | cw: kept 46.7% of rows; `sx/` and drill −72% (`static-hex-runs.md:100-107`) | cw gen only; gcs at its next compaction | `hexRuns.ts:61-67`, `staticFilter.ts:332-349`, `staticRuns.ts:131-137` |
| Runs `deltas/<id>/` (mini-gens: `sx/`, `catalog/`, `[drill/]`, `[names/, anchors/]`, GCS `cdelta/`) | per-scan append | `static_runner.py` `runs add` (`:520`) | `cdelta` 21.9 MB; `copen` state 4.4 GB scratch (`static-append.md:99-100,389`) | gcs; cw only if `STATIC_NAMES=1`, **never set** (`cw:job/cw-run.sh:283`) | `Tiers` (`staticRuns.ts:64`) |
| Manifests `<id>.json` + revisions `<id>.m<NNN>.json` | live runs | `publish :703`; deferred carries `static_merge.py` (`plan_carries :81`, `publish_revision :392`) | — | gcs (cw-s3 lacks `5343fa32`) | `latestManifest` (`staticRuns.ts:191`) |
| Base generations (`scans.json`, `ranges.json`, `cintervals/`, `chist/`) | immutable base | `static_names` `scans :989` … `hist :1238` | gcs `2026-10-08` (uncoalesced, **150.6 GB on R2, superseded**) and `2026-10-08c`; cw `2026-10-09cw` (no hex rule, 118.7 GB, superseded) and `2026-10-10cw` | — | `dirOnlyScans` (`staticRuns.ts:49`) |
| Compaction (`static_compact.py`, spec `static-compaction.md`) | fold base + runs into a new gen equal to a rebuild | branch `compaction` (`f8b2b01b`, `d1525689`), **not on `cloud`**; does not yet rebuild `names/` / `anchors/` (`anchored-search.md:174`) | — | — | — |

### 1.4 Caches

| Cache | Answers | Lives | Deployments | Code |
|---|---|---|---|---|
| Edge cache (finished JSON) | subtree / diff / series / filter-cover | colo Cache API, `v${CACHE_V}`, 1 d; partial answers colo-only for 120 s | all | `edgeCache.ts:23,42,48-51,84,107-109`; `subtree.ts:171-201`, `diff.ts:129-148`, `series.ts:150-262`, `filter-cover.ts:86-118` |
| KV tier `CACHE_KV` | global second tier of the above, 30 d | KV, key = SHA-256 of the cache URL | gcs, cw. **gcs preview writes prod's namespace** | `edgeCache.ts:55-58,73-74,112` |
| Colo reader cache | byte ranges, footers, compact iv footers, iv footer-group docs, iv state (60 s), `.groups.json` blobs | `index-footer.cache`, 1 d | all | `index.ts:819,870-928,1009-1076,593-603` |
| Colo decoded row groups + `coloPutsSettled` | iv decoded groups across isolates | colo, gzipped columnar JSON | gcs only (cw/m3 lack `b15df9fe`) | `index.ts:1466-1488,1653-1691`; `_middleware.ts:29` |
| Static-name colo caches | static group indexes, catalog, hits, anchored hits, drill | colo, 7 d, tags `index-v2`, `keys-v1`, `catalog-v1`, `top-v1`, `aliases-v2`, `hits-v1`, `anchored-hits-v1`, `roots-v1` | gcs, cw preview | `staticNames.ts:567-579`, `staticFilter.ts:195-256` |
| OG card cache | share images | colo, 1 d | gcs | `og/serve.ts:113-170` |
| Isolate memos | handles, footers, iv states, LRUs (24 MiB groups, 12 MiB footers), miss memos | module Maps; several unbounded (`index.ts:485,2130`, `ownerTotals.ts:35,97`, `stagedSlack.ts:344`) | all | `index.ts:394,485,529-530,1529-1566,2226`; `view.ts:336,545`; `extras.ts:31,38`; `search.ts:96` |
| `warm-cache` | prefills colo + KV for home-page requests | job, after publish | gcs (`gcs:job/run.sh:493-501`), cw (`cw:job/cw-run.sh:288-304`) | `warm.py`, `cli.py:3798-3850`. Inferred from code: it never sends `depth`, while first paint uses `depth=1` (`App.tsx:455,785`) |

### 1.5 Auxiliary indexes and sidecars

| Item | Answers | Builder / when | Lives / size | Deployments | Readers | Status |
|---|---|---|---|---|---|---|
| `attr.tsv` + `.idx.json` (64 KB blocks) | provenance of a node's top owner | `write_extras` (`extras.py:65`) in `path-index -a`; backfill `index-extras` (`cli.py:3770`) | gen dir; 165K prefixes (`done/index-extras.md:7`) | gcs | `extras.ts:126` via `view.ts:1688,1705` | built + read. Probed (D1 + GET, 60 s miss memo) on every deployment (`extras.ts:37,56-63`) |
| `ck.txt` | — | excised (`76cdd173`) | — | — | — | gone; `done/index-extras.md` still describes it |
| dir-cache (`dir-stats.parquet`, `age-days.parquet`) | lets REPROC skip the 595M-row object scans | `path-index -c` (`viz.py:314-358`), daily | `listing/<scan>/dir-cache/` | gcs | REPROC and experiments; no site reader | built; read off-path |
| Age pyramid (8 bins `1h…8d`) | created-time histogram per path | `write_age_pyramid` (`index.py:360,439-448`) in `index-write` | ~47.5 MB × 8 per cw scan | cw (+ `meta` store) | `api/age-pyramid.ts:23-74`, `agePyramid.ts` | built + read |
| Phase A `age-index.parquet` | — | `write_age_index` (`index.py:61-69,288-321`) has no production caller | — | none | dead branch `index.ts:291` | dead code |
| `age.json` | — | written by every `path-index` (`viz.py:544-574,610`) | — | gcs, r2, m3 | none | **written, unread** |
| Over-time groups (`over-time.parquet`, K = 16) | `/api/series` in ⌈N/16⌉ reads | `over-time-groups` (`overtime.py:44`) via `cw:job/cw-overtime.sh` | 142–259 MB/scan (`obs-axis-indexing.md:41,92-97`) | cw | `overTime.ts:50-105`, `series.ts:194-215` | built + read; `over-time.scans.json` written, unread |
| Read lens (`access/state`, `access/raw` ~34 GB/day, `access/agg` ~25 MiB/day; `last_read` column) | last-read age | `dt-cloud access ingest` (`access.py:287,412`), `path-index -x` | GCS | gcs | `view.ts:272,1404-1413`, `prefixes.ts:45` | built + read |
| D1 `owner_totals` | per-user owned bytes (ledger folded over the store) | request time, cached per (scan, ledger head) | D1 | gcs | `ownerTotals.ts:39-71`, `api/owners.ts`, `estate.ts` | built + read |
| `scan_runs` (+ phases, outputs) | `/scans` page | `scan-run record` | D1 | gcs, cw | `scanRuns.ts:15-27` | built + read |
| `/staged`, plans | not an index (D1 tables `plans`, `plan_items`, `stage_batches`, …) | — | D1 | gcs, cw, m3 | `staged.ts:17-35`, `api/plans/*` | — |
| ClickHouse / serving box (hot L1/L2, `/coarse`, name-summary, box subtree/diff) | box answers | `serve-query`, `ch-ingest` (`gcs:job/run.sh:458-470`, when `CH_STORE_URL` is set) | VM n2-highmem-8 + 2 TB pd-ssd ≈ $728/mo (`ch-store-retirement.md:131-137`) | gcs preview only (`gcs:site/wrangler.toml:141-162`: `QUERY_BOX_URL` is a trycloudflare quick tunnel) | `queryBox.ts:44-83`, `hotL1.ts`, `hotL2.ts`, `api/coarse.ts:10`, `nameSummary.ts:23-45` | retirement prepared, not executed (`ch-store-retirement.md:3`); VM state not checked |

## 2. Reader decision points that depend on what exists

Classes: **(a)** intrinsic limit, stated honestly; **(b)** safety guard; **(c)** capability skew across deployments or generations; **(d)** legacy or transition path.

| # | Point | file:line | Behaviour | Class |
|---:|---|---|---|---|
| 1 | `storeReady` | `index.ts:242`; `subtree.ts:47`, `diff.ts:35`, `age-pyramid.ts:27`, `owners.ts:25`, `prefixes.ts:18` | no store configured → 503 | c |
| 2 | `intervalsOn` | `index.ts:435` | needs `PATH_STORE=intervals` + `INTERVAL_STORE_GEN` + `INDEX_R2` | c |
| 3 | `withPathStore` | `index.ts:439-446` | `PATH_STORE=opt-in` + `?ps=iv`; any `store=` turns it off; wraps only subtree/diff/series | d |
| 4 | `INTERVAL_STORE_REV` | `index.ts:473-474` | cache re-key for an in-place re-cut; set nowhere | d |
| 5 | `FILTER_STATIC` + `INDEX_R2` | `staticFilter.ts:236` | static index on, else the path-store search | c |
| 6 | `STATIC_GEN` required | `staticNames.ts:33-37` | unset or malformed → throw | b |
| 7 | `FILTER_STATIC_HEAVY` | `staticFilter.ts:154-189,244` | off → catalog root only, `term-too-common` below. No deployment runs static without it | d |
| 8 | `FILTER_STATIC_ANCHORS=0` | `staticFilter.ts:249` | kill switch, set nowhere | d |
| 9 | `FILTER_INDEXED_ONLY` | `indexedOnly.ts:58,77-90`; `view.ts:869`; `subtree.ts:94,140`; `diff.ts:79,107`; `series.ts:119-121,191`; `filter-cover.ts:65,82` | gcs: 400 before any read; cw: falls back to the walk | c |
| 10 | `NAME_SUMMARY_STATIC` | `nameSummary.ts:7-11`, `nameSummaryStatic.ts:28` | `/names` from the static index vs the query box | d |
| 11 | `STATIC_MAX_ROWS` (400K) | `nameSummaryStatic.ts:31,128,178` | `/names` read ceiling → 503 | b |
| 12 | `STATIC_BENCH` | `api/static-bench.ts:1-7` | dev timing route | d |
| 13 | `QUERY_BOX_URL` gate | `queryBox.ts:44-48` | box answers for subtree/diff/series | d |
| 14 | `QUERY_BOX_COARSE` / `HOT_L1` / `HOT_L2` | `api/coarse.ts:10`, `hot-l1.ts:14`, `hot-l2.ts:9` | flag off → 404 | d |
| 15 | Box error → Worker | `queryBox.ts:57-82`; `series.ts:160` | 409/501/503/timeout → Worker fallback | d |
| 16 | `CACHE_KV` absent | `edgeCache.ts:73` | colo only | c |
| 17 | `CACHE_V` manual bump | `edgeCache.ts:42` | cache invalidation (fragile: dev shares prod's KV) | b |
| 18 | `RESPONSE_V = 15` | `staticFilter.ts:303-327` | static answer shape version | b |
| 19 | `latestManifest` / `ivManifest` without `list()` | `index.ts:567-569`; `staticRuns.ts:191-198` | no manifest → base only | c |
| 20 | Bad manifest / no `scans.json` | `index.ts:583,599` | throw | b |
| 21 | Broken or unstamped iv run | `index.ts:545-557,615,651` | stack cut for 30 s; `ivRetry` | b |
| 22 | Date not held by the iv store | `index.ts:674-675` | per-scan fallback | d |
| 23 | Variant not served by the iv store (`age-*`, `over-time`, unsliced `user`) | `index.ts:664,672` | per-scan | c |
| 24 | `reads` covers base scans only | `index.ts:677` | null past the base | a |
| 25 | `ivNoSlices` | `index.ts:684-686,704-708` | gen without slice sorts → per-scan (60 s memo) | c |
| 26 | Held date + sliced + `user` / `coarse*` | `index.ts:668-671` | "not synced" (never mixes per-scan rows into a lens) | b |
| 27 | `asOfScans` horizon | `index.ts:679-681` | drops runs past the asked scan | a |
| 28 | `perScan()` for search | `index.ts:453`; `view.ts:842` | search reads the per-scan store | c |
| 29 | `ivLastRead` | `view.ts:1404`; `index.ts:733-768` | `reads` sort only when `path` lacks `last_read` | d |
| 30 | iv sort planner (`IV_PATH_PLAN_ROWS` 256K, `IV_BAND_ROWS` 64K) | `view.ts:65,68,433-443` | cost-based sort choice | a |
| 31 | No D1 pointer | `index.ts:411`; `view.ts:2174`; `subtree.ts:208`; `diff.ts:153` | "not synced" / 409 `LensUnavailable` | c |
| 32 | Footer mode | `index.ts:415-426,823-846,839` | D1 → `.groups.parquet` → `.groups.json` | d |
| 33 | Footer version checks | `index.ts:845,936` | `groups_v`/`v` ≠ 1 → throw | b |
| 34 | Row shape by `index_schema.version` (1/2/3) | `index.ts:157,782-790,1253` | per-version decoders | d |
| 35 | `keyedOnTotal` | `index.ts:2186` | `bysize` keyed on `size` (pre-re-cut) vs `tot` | d |
| 36 | `lensSorted` / `lensSort` | `index.ts:168`; `view.ts:2162-2167` | by-user sort when the deployment has one | c |
| 37 | `TooWide` (> 4000 groups), size-span cap | `index.ts:1772,1805,2112,2123`; `subtree.ts:210`, `diff.ts:155` | 413 | a |
| 38 | Read-day lookup > 400 groups | `index.ts:758-761` | throw (may surface as 500) | b |
| 39 | `D1_ERROR` | `subtree.ts:211`, `diff.ts:156` | 503, retry-after 5 | b |
| 40 | `tryOpen` refusals | `view.ts:338-375` | coarse refused on store gens; v1 `user` refused on a store date; missing by-user → `path` | d |
| 41 | Coarse tiers for v1 dates | `view.ts:55-56,483,507,683-693,1527`; `filter.ts:60` | v1 threshold reads | d |
| 42 | Mixed v1/v2 diff | `view.ts:1900-1902` | folds file rows | d |
| 43 | `V1_FILTER_SCAN_OBJECTS` 500K | `view.ts:77,920` | walk cap | d |
| 44 | `indexedScan` | `scanArg.ts:25-37`; `subtree.ts:129-130` | no per-scan `path` pointer → 404, **even under the iv store** | b |
| 45 | `unindexedScans` / `meta.json` fill | `series.ts:38,141,251-254` | series gaps for unindexed scans | d |
| 46 | `staticLiteral` | `staticFilter.ts:42-49` | only one positive substring without `/` is static | a |
| 47 | `ANCHOR_MIN {end:3, start:2, exact:1}` | `indexedOnly.ts:74,85-88` | `anchor-too-short` (follows from ≥ 3-character suffixes and 3-character shard prefixes) | a |
| 48 | `shortLiteral` | `staticFilter.ts:52,162` | 1–2 character literals never range-read | a |
| 49 | `staticKey` scan membership | `staticFilter.ts:263-268` | scan outside the gen → `scan-not-indexed` | a |
| 50 | `dirsOnly` / `scan-dirs-only` | `staticFilter.ts:274-288`; `nameSummaryStatic.ts:174-175`; `staticRuns.ts:49` | v1 scans flagged; cross-cutover diff refused | d |
| 51 | `covers` | `staticFilter.ts:64`; `view.ts:861` | drill/anchored tiers lag light tiers → `scan-not-indexed` or fallback | c |
| 52 | Rollup with owner scope | `view.ts:861,868` | rollups carry no owners → fallback / `unsupported-scope` | a |
| 53 | Light bound `MAX_ROWS = V + 2·8192` | `staticFilter.ts:90`; `static_anchors.py:64` | over it → heavy source | a |
| 54 | `catalogRoot` | `staticFilter.ts:183-189` | non-member > 2 characters declines | a |
| 55 | `declined()` | `staticFilter.ts:356-360` | `term-too-common` vs `scan-not-indexed` | a |
| 56 | Light-tier liveness (`catalog/meta.json`), broken-tier cut | `staticRuns.ts:59-63,73-75,106-111` | cut, never a 500 | b |
| 57 | Hex rule mismatch, run vs base | `staticRuns.ts:131-137` | run treated as broken | b |
| 58 | Drill liveness (`drill/meta.json`) | `staticDrill.ts:530-543` | drill stack ends at the first run without it | c |
| 59 | Anchor liveness (`anchors/meta.json`) | `staticAnchors.ts:243-245`; `static_merge.py:318-323` | anchored stack ends at the first run without it | c |
| 60 | Anchors under a different hex rule | `staticAnchors.ts:239-241` | anchored tiers = none | b |
| 61 | `anchorless()` → `anchor-not-indexed` | `staticAnchors.ts:262-272`; `view.ts:869`; `series.ts:190-191` | gen without anchors (cw) → 400; light `q$` still served | c |
| 62 | Starts-with catalog presence | `staticAnchors.ts:247-248,311-312` | none → heavy `^q` root declines | c |
| 63 | `^q` `firstPaint` | `staticAnchors.ts:281-285`; `subtree.ts:140` | root answered from the catalog first | b |
| 64 | `START_MAX_ROWS` 400K / `START_MAX_HITS` 150K | `staticAnchors.ts:54,58,326-331,377-380` | `term-too-common` (measured isolate memory) | a |
| 65 | Scoped bound `R + 4·Σrg` (anchors) vs `R + 2·Σrg` (drill) | `staticAnchors.ts:399-402`; `staticDrill.ts:399` | roots vs rollup | a |
| 66 | Rollup missing where the bound says heavy | `staticAnchors.ts:416`; `staticDrill.ts:630,647` | invariant throw | b |
| 67 | `hexQuery` / `hexNote` | `staticFilter.ts:332-349`; `hexRuns.ts:61-67` | note under the gen's rule; never a refusal | c |
| 68 | `/names` anchored | `api/name-summary.ts:19-31` | `anchor-not-indexed` on every deployment, gcs included | c |
| 69 | `/names` `/` refusal, `notIndexed` 400, `unavailable` 503 | `api/name-summary.ts:15-18`; `nameSummaryStatic.ts:136-138,169` | refusals, never a box answer | b |
| 70 | Filter fallback chain | `view.ts:893-923` | static → `searchRoots` → thresholded walk (`approximate`) | d |
| 71 | Search-sidecar probe | `search.ts:96`; `view.ts:894-895` | probe, then a 60 s miss memo (gcs and cw build none) | c |
| 72 | `/api/filter-scans` | `api/filter-scans.ts:14-16` | null without a static store | c |
| 73 | `/api/filter-caps` | `api/filter-caps.ts:17`; `indexedOnly.ts:110-112` | advertises `indexedOnly` only; help always lists `^`/`$` | c |
| 74 | Extras probe | `extras.ts:51,56-63` | D1 + GET per scan per 60 s; misses on cw/r2/m3 | c |
| 75 | Extras without a gen dir | `extras.ts:52-53` | `listing/<date>/` | d |
| 76 | Extras range > 8 MB | `extras.ts:103` | skipped | a |
| 77 | Age pyramid not synced | `age-pyramid.ts:51-55`; `App.tsx:678-680` | 200 empty; chart unmounted | c |
| 78 | Age pyramid unindexed scan | `age-pyramid.ts:41` | 404 | b |
| 79 | Over-time groups | `overTime.ts:51-58,86-90,106` | no table / no groups / error / unsealed tip → per-scan | c |
| 80 | Over-time only unscoped | `series.ts:194-195` | scoped series read per scan | a |
| 81 | Read lens availability | `App.tsx:726`; `gcs:job/run.sh:283` | `meta.access` gate | c |
| 82 | Owners / ledger | `owners.ts:19-24`, `estate.ts:33`, `assignments.ts:28`, `ledger.ts:40,43` | no ledger → 404/503. Unverified: crafted `?lens=user:x` on cw/r2/m3 may hit a missing table | c |
| 83 | `scan_runs` table | `scanRuns.ts:24`; `api/scan-runs/[[path]].ts:21,29` | no table → 503 / `configured:false` | c |
| 84 | Staged tables | `staged.ts:21-27` | none → "nothing staged" | c |
| 85 | Plans | `api/plans/[[path]].ts:120,148,152,229,244` | `!STAGING` → 404; no `STORE_BUCKETS` → 503 | c |
| 86 | Series lens on a secondary store | `series.ts:93` | 400 | c |
| 87 | `ownerTotals` version / `uo` recompute | `ownerTotals.ts:79-81,109-111` | recompute on an old cache shape | d |
| 88 | `coloPutsSettled` | `_middleware.ts:29` | keeps the isolate alive for colo puts | b |
| 89 | Partial answer → colo 120 s | `edgeCache.ts:91,105-109` | never KV | a |

**Totals: (a) 17, (b) 19, (c) 30, (d) 23 = 89.**

Reading the classes:
- (a) and (b) are healthy. Each limit is measured or structural, and the refusals name their reason.
- (c) splits into two kinds:
  - **Inputs that genuinely differ** (owners/ledger, access logs, label slices): about 8 points.
  - **What happened to be built where** (anchors, drill, over-time, age pyramid, search sidecars, extras, KV, iv store, indexed-only): about 22 points.
- Most of the second kind goes away once every deployment builds the same standard set, and the reader reads capabilities from one manifest instead of probing files.
- (d) is mostly two transitions that can end:
  - **v1 → v2**: points 34, 40–43, 50.
  - **Per-scan → interval store**: points 3, 22, 29, 32, 35, 45.
  - Plus the query box: points 10, 13–15.

## 3. Per-deployment differences

Code is identical across deployments. `git diff cloud...gcs` and `git diff cloud...cw-s3` touch only `job/`, `infra/`, `wrangler.toml`, `stores/*.tsx`, migrations and specs. So every difference below is config, job scripts, or what was built. Branch lag:
- `cw-s3` is 7 `cloud` commits behind: it lacks deferred carries, `coloPutsSettled` and the iv drilled views.
- `m3` merges `local` and is ~143 commits behind (merge-base `e1db595e`).

| Index / flag | gcs | cw | r2 | m3 | Why | Basis |
|---|---|---|---|---|---|---|
| Path store builder | `path-index` | `index-write` over per-bucket `import -e stream` | `path-index -S` | `path-index -g` | different job histories | historical |
| By-user sorts, `usr`, `attr.tsv` | `bysize-user` only | none | none | none | "CoreWeave has no ownership signal" (`index.py:29`); `usr` is empty on cw (`cw:specs/cw-static-names.md:126`) | **measured / input** |
| `bysize-user` but not `user` on gcs | yes | — | — | — | lens reads user-first `bysize` (`gcs:job/run.sh:344-348`) | design choice, unmeasured |
| Read lens (`last_read`) | yes | no | no | no | needs GCS usage logs | **input availability** |
| dir-cache | yes | no | no | no | REPROC re-attribution skips object scans (`gcs:job/run.sh:333-336`) | workflow (gcs-only REPROC) |
| Search sidecars | off (`PATH_INDEX_SEARCH`) | no | yes | no | gcs: "off until measured"; r2: cheap on a small corpus | r2 measured; rest historical |
| Age pyramid | no | yes | no | no | ported from the cw session's age-index work | **historical** |
| Over-time groups | no | yes | no | no | same | **historical** |
| Interval store | yes (`2026-10-09b`) | no (no `interval_profiles/cw.json`, `interval-store.md:185`) | no | no | built and verified for gcs first | **historical / sequencing** |
| Static names (light + drill) | prod, appended daily | base `2026-10-10cw` on preview only; per-scan append off (`STATIC_NAMES` never passed, `cw:job/cw-run.sh:283`) | no | no | "dev only until the per-scan runs are scheduled"; IAM decision open (`cw-static-names.md:152-163`) | **historical / rollout** |
| Static anchors | yes | no (`Profile.anchors` defaults off, `static_profile.py`) | no | no | anchors landed for gcs on 10-09; cw's profile never got `anchors=True` | **historical** |
| Hex-run rule | profile `16,8`, but live gen `2026-10-08c` predates it | on (`2026-10-10cw`) | n/a | n/a | 79% of cw suffix rows sit in ≥ 16-hex runs vs 1.1% on gcs (`cw:specs/cw-static-names.md:126`, sampled); gcs adopts it at compaction (`static-hex-runs.md:55`) | **measured** |
| `FILTER_INDEXED_ONLY` | on | off (falls back to the walk) | — | — | Ryan 10-09 "just support what's fast and robust", applied to gcs only | **config drift** |
| D1 footer retention | `-r 30` (measured: 218 MB/scan vs the 10 GB cap) | none (unbounded, ~4 scans/day) | none | none | gcs measured; cw never set | gcs measured; cw **accident** |
| File GC `-F` | no | yes (`cw:specs/storage-consolidation.md`) | no | no | cw reindexes left old gen dirs | **historical** |
| `CACHE_KV` | yes (shared with preview) | yes | no | no | KV added per deployment over time | historical |
| `INDEX_R2` binding | prod + preview | preview only | — | — | follows static/iv rollout | historical |
| Query box vars | preview only | — | — | — | ClickHouse experiment remnant | **legacy** |
| Row groups 8K | yes | yes | yes | yes | 32K made drills 4× dearer and hit the decode cap (`gcs:job/run.sh:341-344`) | measured, uniform |
| Cadence | daily (+ `T HHMM` extras) | 6-hourly | daily | per capture | fleet churn and budget | operational |

Stale text found along the way:
- `gcs:site/wrangler.toml:94` says HEAVY "stays dev-only", but `:107` sets it.
- `site/wrangler.example.toml:112`.
- `staticNames.ts:31` names `2026-10-09cw`.
- `interval-store.md:173-178` and `interval-store-append-gcs.md` still describe the store as opt-in and off.
- `api/path-index.ts:16-17` lists v1 wire names.
- `age-pyramid.ts:46`.
- `cw:job/cw-run.sh:222`.
- `gcs:infra/cf/README.md:45`.
- `done/index-extras.md` (`ck`).

## 4. Anchored search: what was built, versus a folded suffix index

**What exists** (`specs/anchored-search.md`):
- `sx/` holds every suffix of ≥ 3 characters of each lowercase basename (`static_names.py:405,662-673`). A basename has no `/`, so no `sx` key contains one.
- **`$` is already folded in.** Every suffix ends at the name's end, so `s == q` means "the name ends with `q`", with at most one row per version. `q$` reads `sx/` as-is: "the suffix shards already are the anchored roots file" (`search-extensions.md:151`).
- **`^` uses a real sentinel, stored apart.**
  - `names/` holds `'/' + name` for every version at depth ≥ 1, at any name length (`static_anchors.py:116`).
  - So `^q` is the range `/q…` and `^q$` is the key `/q`.
  - It is exactly the suffix that starts at the `/` of `parent/name`, in `sx`'s layout (`SX_SCHEMA`, sorted `(s, path, usr, vf)`, 8K-row groups, 3-character shard prefixes). But it is its own shard set.
- **Heavy anchored terms do not use the drill.** They have three more families:
  - `anchors/` `end` rollups (keyed on an exact suffix);
  - `exact` rollups (keyed on `/` + a name);
  - `anchors/start/`, a per-bucket starts-with catalog for heavy `^q` at the root only.
- Ryan's belief is half right. The sentinel is there, but it lives in a parallel structure, not interleaved in the shards.

**Q: Is `names/` just the sentinel suffix stored separately?** Yes, with one superset property: it includes 1–2 character names, which `sx/` (≥ 3 characters) omits. That is why `^q$` works from 1 character (`indexedOnly.ts:74`).

**Q: Could it be interleaved into `sx/`, and at what cost?**

| Aspect | Cost |
|---|---|
| Ordering | `/` (0x2F) sorts between `.` and `0`, so the `/…` keys form their own 3-character prefixes: the same shard planner, ~13 more shards |
| Rows / bytes | +798M rows on 13.2B (+6.0%), +7.5 GB on 135.7 GB (+5.5%); per scan about +1.7M rows (38–90 MB) |
| Read amplification | ~none: contains and `q$` reads never touch `/` keys (a static literal never holds `/`, `staticFilter.ts:45`); at most one shared boundary group |
| Build time | ~+6% on suffix-map/shards (33 + 14 min wall at 32 tasks, `architecture/static-name-search.md:127`), replacing today's 20-min one-task `names` stage |
| Real cost 1 | needs a new generation (~165 VM-h ≈ $35–40 plus ~$16 R2 egress, `search-extensions.md:39`). **Free at a compaction**, which rewrites the base anyway |
| Real cost 2 | every builder that walks `sx` would see `/` keys (census, members, answers, `chist`, `static_roots`). Either exclude them, or let them in deliberately (next row) |
| Upside if let in | the 2,788 heavy `/`-prefixes become catalog members with drill roots and rollups. That is **heavy `^q` below the root through the existing contains machinery**: today's gap, which declines `^step`/`^data` inside buckets (`anchored-search.md:170`). Cost: ~4.07B prefix-row pairs, "the size of the long drill" (≈ +$19 build, +~200 GB R2) |
| Semantic seam | the contains first-hit test (parent contains `q`) equals the anchored test only if the parent path is also `/`-prefixed. That is one predicate change, at bucket segments (`static_anchors.py:96-113`) |

The specs never considered interleaving; no text mentions a sentinel or interleaving. The separation followed from the anchors rule "per tier … new keys only" (`anchored-search.md:23`), which avoided a rebuild. It also let cw skip anchors through a profile flag.

The FM-index was rejected explicitly for R2: "sequential round trips on R2" (`architecture/static-name-search.md:378`), and counts are occurrences rather than distinct weighted documents (`short-query-index.md:240`). That rejection stands. What is proposed here is a suffix array with a sentinel, which is what `sx` already is.

**Q: Could the `end` / `exact` rollups merge with the drill?** Yes, cheaply.
- **Already shared:** `ROLLUP_SCHEMA`, `GroupFile` two-level indexes, `heavy_dirs` / `rollup_cells`, `DRILL_RULES.stack`. The term keys `q/` and `/q/` can't collide with contains literals.
- **What differs:**
  - Aliasing: the drill has it, anchors don't.
  - The first-hit predicate.
  - The scoped bound: `R + 4·Σrg` vs `R + 2·Σrg`, the "+2" being the extra key-run straddle.
  - Separate liveness markers and separate per-scan stages.
- **What merging buys:** `end`, `exact` and `start` become kinds beside `long` and `short` under `drill/`, with one marker and one tier-cut rule. That deletes reader points 59–62 and `AnchoredSource`'s parallel stack logic.
- **Cost:** re-indexing ~3.5M cells (MB-scale), and coupling the per-scan drill and anchors builds. That coupling is fine once every deployment builds both.
- The starts-with catalog (`/q` × bucket) and the contains catalog (`q` × bucket) have the same semantics in different layouts. With interleaving, the first subsumes the second.

**Recommendation.** At the gcs compaction, fold `names/` into `sx/` and let `/` keys through the catalog and drill. `end` / `exact` become drill kinds, and `anchors/start/` disappears into the catalog.
- One index (suffixes with a sentinel), one catalog, one drill, one liveness marker.
- Heavy `^q` works at every depth.
- The `^q`-specific bounds (`START_MAX_*`), `firstPaint` and `bucketsOnly + scopedBelow` all go.
- Marginal cost ≈ the heavy-`^q` drill (~$19 compute, ~200 GB R2 ≈ $3/month). If that is too much, interleave anyway and exclude `/` keys from the drill: still one shard set, with heavy `^q` kept at its current degradation.

## 5. The two append pipelines

| | Static names | Interval store |
|---|---|---|
| CLI | `static-names runs add [-c …] SCAN` (`static_runner.py:520-542`), `runs merge`, `runs carry` | `interval-store append [-c -n -P] SCAN` (`interval_append.py:849-876`) |
| Ordering | `pending_scans` (`static_runner.py:79-94`); exit 3 not-next (`:50`) | imports `pending_scans` (`:446`); `NOT_NEXT=3` redefined (`:66`) |
| Prepare | `static_append.py:535-555`; unconditional write | `interval_append.py:606-623`; adds a ts check and `if_generation_match=0` |
| Per-range append (Batch, 16 tasks / 256 ranges) | contiguous blocks (`static_append.py:571-604`) | balanced by open rows (`assign_ranges` `:262-277`) |
| Tier builds | shards ∥ catalog, then drill ∥ anchors (`static_runner.py:345-412`) | none: 5 sorts cut inside publish (`cut_run` `:208-219`) |
| Publish | local, seconds, level-0 only (`static_append.py:662-700`) | Batch, **inline pairwise carries** (`push_run` `:771`, `:789-807`) |
| Merges | deferred N-way, manifest revisions `.m<NNN>`, lease (`static_merge.py:81-102,235-263,392-427`) | inline. Level 5 only logged (`:810-811`). No lease, no revisions |
| R2 | separate Batch job, markers last, `r2-verify`, manifest last (`static_runner.py:414-436`; `static_names.py:1537-1600`) | second runnable of the publish task (`:516-528`), its own `r2_cmd` (`:818-846`); a third copy in `interval_store.py:802-833` |
| Verify | optional `-t` brute force | inline per range (count + Σ hash, `:119-128,183-186`) |
| Prune | `static_append.py:456-469` | `interval_append.py:626-652` (threaded) |
| Lost-state rebuild | `rebuild-state` (`static_append.py:733`) | none (`interval-store.md:185`) |
| Profile | `Profile` (`static_profile.py:21-94`), Python examples or JSON, `STATIC_NAMES_*` | JSON `interval_profiles/gcs.json`, mapped onto the same `Profile` (`:344-377`), `INTERVAL_STORE_*`. No cw profile |
| Measured | ≈ 76 min/scan, ≈ 63 min without carries; ≈ $0.53/day (`static-append.md:56,395`) | 19.4 min, ≈ $0.77/scan (`interval-store.md:367-384`) |

**Near-duplicates** (interval side ≈ 380 LOC, plus ~60 LOC of the triplicated R2 copy):
- `Runner` (`interval_append.py:408-511` vs `static_runner.py:213-369`);
- `gcs_runner`, `append_cmd`/`add_cmd`, the `job_spec`/`job_id`/`task_command` wrappers;
- `_gcs`/`_latest_manifest`/`_state`/`_scans_of`;
- `prepare`;
- `prune_plan`/`prune_state`;
- `manifest`/`missing_files`;
- the R2 copy/verify;
- TS `ivManifest` (`index.ts:567-586`) vs `latestManifest` (`staticRuns.ts:191-198`);
- ~150 test LOC.

Divergences that look accidental:
- The interval store resolves the newest manifest with a plain `sorted()`, with no `manifest_keys` filter (`interval_append.py:434-435,588-592`). It is correct for revisions only by lexical luck, and untested.
- Different poll intervals.
- Different prepare preconditions.

**One runner (`dt_cloud/runs/`).**

Generic core:
- `pending_scans`, `prepare` (with precondition and ts check);
- `Runner.one/run/stage/concurrently`, `BatchRunner`, `job_spec`;
- `publish_level0`;
- `plan_carries`, `merge_pending`, `publish_revision` and the lease;
- `r2_publish` (runs with markers last, verify, manifest);
- `prune(state_complete)`;
- one `Profile` loader keyed by prefix;
- one TS `latestManifest`.

Each index plugs in a `Pipeline`:
- `prefix` / `job_prefix`;
- `range_stage`, with or without balanced assignment;
- `state_complete`;
- `tier_stages` (static: shards ∥ catalog → drill ∥ anchors; interval: `cut`);
- `run_files`;
- `merge_build` (static `build_merged_run`; interval `merge_range` × k + `cut_run`);
- `merge_eligible` (drill parity);
- `r2_order`;
- `manifest_extra` (interval `stamps`);
- an optional `verify_stage`.

What it gives:
- **Net −250–350 Python LOC, −100–150 test LOC, −15 TS LOC** (estimates from line ranges, not a prototype).
- The interval store gets deferred N-way carries, a lease, the manifest filter and a path to `rebuild-state`.
- The static pipeline gets balanced ranges, threaded prune, and publish + R2 in one VM.
- **Urgency:** the interval store's pairwise inline carries grow as 2^level inside a 60-min job timeout.
  - Its level-1 publish already took 182 s.
  - Its base is `2026-10-09b`, with daily runs: an L3 carry falls around 10-17 and an L4 around 10-25.
  - Static fixed exactly this failure in `5343fa32`.
  - Compaction (L5) is unbuilt for the interval store. In the same framework it is `merge_build` over the base plus every run.

## 6. Recommendation

### 6.1 Target end state (what we'd build today)

One **standard index set**, built identically on every deployment. A component is absent only when its *input* is absent, never because of a tuning choice.

| Layer | Standard component | Notes |
|---|---|---|
| Ingest artifact | per-scan `path` sort (v2) | input to the interval append; not served once the store holds every date |
| Tree / diff / series | **interval store**: `path`, `bysize` (+ `slices`, `slices-bytotal`, `slices-bysize-user` **iff** the corpus has labels) | one store answers any scan, so per-scan `bysize`, per-scan D1 footers and the over-time groups become unnecessary |
| Text search | **static names**: `sx/` with sentinel rows (`/name`), one catalog, one drill whose kinds include `end`/`exact`/`start` | hex rule on everywhere (below) |
| Age | an age histogram as a derived sort of the interval store, or not at all | decide once; today it is cw-only by accident |
| Read lens | `reads` / `last_read` **iff** access logs exist | input availability |
| Caches | colo + KV + isolate, as today, with one `CACHE_V` per deployment namespace (stop dev sharing prod KV) | — |
| Runner | one `runs/` framework for both stores: level-0 publish, deferred N-way carries, revisions, compaction as an L5 merge | — |
| Capability manifest | each generation's manifest lists the tiers and kinds each run carries, plus the build rules (hex rule, labels) | the reader reads it once instead of probing five `meta.json` markers; `/api/filter-caps` advertises from it |

**Tailoring, declaratively.** After the standard set, only three knobs remain:

| Knob | Rule | Evaluated | Hysteresis |
|---|---|---|---|
| Hex-run rule | **on everywhere** with fixed `H=16, T=8`. On gcs it affects 1.1% of rows, so it costs nothing and drops a dimension. A switch would be a self-driving knob with no payoff | — | — |
| Label slices (`usr`) | build iff the share of non-empty `usr` in the census > 1% | at each base build or compaction | turn off only below 0.1% for two consecutive compactions |
| Access / read lens | build iff an access-log source is configured | at deploy (input config) | — |
| Light bound V / R, shard target, row group | fixed constants (measured, uniform) | — | — |

Census stats come from the census that every base build and compaction already runs (the hex share and the `usr` share are both sampled there, as in `cw-static-names.md:126`). A new fleet's first build and a long-running fleet's compaction compute the same function of the corpus, so they reach the same shape. Hysteresis is the compaction boundary itself: a rule changes only when a rebuild that is already paid for runs. That is the "activation energy", with no extra backfill. This is a profile computed from data at a fixed point, not a self-driving database.

### 6.2 Deletion list

| Drop | Payoff | When |
|---|---|---|
| `served/slices-bysize` in iv gen `2026-10-09b` (GCS + R2) | 33.5 GB stored; no code change | now |
| Static gen `2026-10-08` (uncoalesced) on R2 / GCS; cw `2026-10-09cw` | ~150.6 GB + ~118.7 GB R2 | now (verify no pointer) |
| iv gen `2026-10-09` (v3) and its `reads`-only path (`ivLastRead`, `view.ts:1404`; `reads` in `SORTS` / `R2_SERVED`) | reader point 29; one sort per cut | now; add an interval-gen GC to the runner |
| ClickHouse / serving box: `chstore/`, `box/`, `bench/` (~19.4K + 3.6K LOC), ~22.8K test LOC, `queryBox.ts`, `hotL1/L2`, `api/coarse.ts`, `/coarse` and `/hot` routes, `QUERY_BOX_*` vars, `ch-ingest` in `gcs:job/run.sh:458-470`, `job/ch-store*`, `infra/cf` `queryBoxHost` | reader points 10, 13–15; ≈ $710/mo (`ch-store-retirement.md`); the largest code mass in the repo | ~~on Ryan's go~~ **code done 2026-10-10** on `cloud` (tag `ch-store-final`; `HotSearchForm` / `HotTotals` kept for `/names`, now in `HotSearch.tsx`); VM and disk deleted |
| `FILTER_STATIC_HEAVY`, `FILTER_STATIC_ANCHORS`, `NAME_SUMMARY_STATIC`, `STATIC_BENCH`, `PATH_STORE=opt-in` / `?ps=iv`, `INTERVAL_STORE_REV` | reader points 3, 4, 7, 8, 10, 12; 6 env vars | now (each is set one way everywhere it exists) |
| `age.json` writer (`viz.py:544-574`), Phase A `age-index` code (`index.py:61-69,288-321`, `index.ts:291`), `over-time.scans.json`, gcs `claims` table | dead writes and dead code | now |
| `dirs`/`objects` references in gcs `GATE` mode (`gcs:job/run.sh:320-323`, `cascade-a2a`) | broken stale path | now |
| `.groups.json` blob fallback (`index.ts:823-846`) | reader point 32 (one of three modes) | once every live gen has `.groups.parquet` (check D1) |
| v1 reader (`toRowV1`, coarse tiers, v1 `user`, mixed diff, `scan-dirs-only`, `V1_FILTER_SCAN_OBJECTS`) | reader points 34, 40–43, 50 (~7) | when gcs v1 dates (pre-09-30) are re-cut as v2, or once the interval store holds them; ~100 v1 dates is a one-off recompress plus re-tier |
| Per-scan `bysize`, `bysize-user`, per-scan D1 footers and `index-gc` for interval-store deployments | ~13 GB/scan GCS (bysize), ~218 MB/scan D1; reader points 22, 35, 31 | after (i) scan existence moves to the store's `scans.json` (today `indexedScan` and `pathScans` read per-scan D1, `scanArg.ts:25-37`, `index.ts:361`) and (ii) search is off `perScan()` |
| Over-time groups (cw) | reader point 79, one cw stage | when cw has an interval store (series come from the store's versions) |
| Search sidecars (r2) | reader points 70–71 | if r2 gets the static index; otherwise keep as the small-corpus search (cheap, measured) |
| dir-cache | daily writes | keep while REPROC uses it; reassess with the interval store (attribution is a slice property) |
| `names/`, `anchors/` (end/exact/start) as separate families; `AnchoredSource`'s stack; `START_MAX_*`; `firstPaint` | reader points 59–63, 65; three builder stages | at the gcs compaction (§4) |
| Duplicated runner code | −250–350 Python LOC, −100–150 test LOC | before the iv counter reaches L3 (~10-17) |

### 6.3 Off-the-shelf check

| Tier | Candidates | Verdict and why | Cite |
|---|---|---|---|
| Path store / interval store | ClickHouse (self-hosted SCD-2), DuckDB on a box, MotherDuck, BigQuery, Aurora + `pg_trgm`, Cloud Run / GCE suspend, Iceberg/Hudi | **ClickHouse was tried and retired.** Warm 0.20/1.46/4.33 s, 105/105 exact, but ≈ $728/mo always-on and broad search never got interactive. It ended up serving only name search, which moved to static R2 (`ch-store-retirement.md:5,137`; `serving-options.md:301-377`; `done/ch-store.md:554`). Suspend/resume failed the 10 s gate (~30 s). BigQuery: on-demand pricing ruinous. Aurora: poor latency per dollar. Iceberg: vocabulary only. **Not evaluated:** R2 Data Catalog (managed Iceberg on R2) and R2 SQL. The interval store is effectively an SCD-2 table with footers in D1, the shape Iceberg standardizes, but no Worker-side Iceberg reader exists, and our pruning keys (dyadic levels, `b_min`, neighbour bounds) are beyond Iceberg's manifest stats. Worth a timeboxed look before generalizing (below) | `serving-options.md:142-191`; `done/storage.md:21-29` |
| Static names | SQLite FTS5 trigram (D1 or HTTP-VFS), FM-index, DuckDB-wasm, ClickHouse ngram/bloom | FTS5: "not bounded by the result; still needs postings". FM-index: sequential round trips on R2. ClickHouse ngram/bloom rejected inside the CH work. **Not evaluated:** Tantivy/Quickwit (object-storage search). Infix search there needs n-gram tokenization, the same postings-blowup FTS5 had, and Quickwit is a server, not a Worker reader. Suffix-ordered postings on R2 (ours) remain the right call | `architecture/static-name-search.md:375-378`; `clickhouse.md:80,112` |
| Footers / planner | parquet footers parsed in the Worker | rejected for size (86 MB/sort); D1 footers + `.groups.parquet` are ours | `path-store.md:93` |
| Caches | Cache API, KV | already off the shelf | — |
| Runner | GCP Batch + ours | Batch is off the shelf; the runner is ~1K LOC of scheduling/manifests. A workflow engine (Workflows, Dagster) would add a service without removing the manifest logic, which is the hard part | — |
| D1 / R2 / Workers | — | the substrate; keep | — |

**Is there a real ecosystem gap?** Yes, one: an *append-only, versioned (SCD-2), row-group-pruned parquet store on object storage, planned from a SQL-held footer and readable from an edge isolate under 128 MB*. Both stores are instances of it: interval store = versions of tree rows; static names = versions of suffix rows. If we generalize, extract exactly that:
- the `runs/` framework (counter, revisions, compaction, R2 publish);
- the footer-in-D1 / `.groups.parquet` planner;
- the colo-cached group reader.

Extract it as one library with both stores as clients. Everything fleet-specific stays out of it.

### 6.4 Sequencing

| When | Step |
|---|---|
| Now (no rebuild) | Delete `slices-bysize`, superseded generations, dead writers and code, stale `GATE` references, one-way flags. Fix stale docs. Decide `FILTER_INDEXED_ONLY` for cw (recommend on, matching gcs). Set cw D1 retention (`-r`). Stop gcs preview writing prod KV (own namespace or `CACHE_V` prefix). |
| Before ~10-17 (iv L3 carry) | ~~One runner; the interval store gets deferred N-way carries, the manifest filter~~ (done 2026-10-10, `append_runner`; live once gcs's job image is rebuilt) and an interval-gen GC (open). |
| ~~On Ryan's go~~ | ~~Execute `ch-store-retirement.md`: VM, tunnel, timer, then the code deletion.~~ VM, disk and `cloud` code done 2026-10-10. |
| Before the gcs compaction | Move scan existence / scan lists (`indexedScan`, `pathScans`) to the store's `scans.json`. Write the capability manifest format. Land `static_compact` on `cloud` with `names/` folded into `sx/` and anchors as drill kinds (§4). Build interval-store compaction in the shared runner (adds the L4 floor, `interval-store.md:182`). |
| **gcs compaction (~2026-11-08, L5 of `2026-10-08c`)** | New static gen: hex rule on, `kind` bit (`search-extensions.md:261`, ~$15 marginal), split `path` (`static-append.md:417-421`), sentinel rows interleaved, unified catalog and drill. Readers switch via `STATIC_GEN`; the old gen stays until Ryan deletes it. Interval-store compaction the same week. |
| After the gcs compaction | Drop per-scan `bysize`, `bysize-user`, D1 footers and `index-gc` for gcs once v1 dates are re-cut or folded into the store. Remove the v1 reader. |
| cw | Profile `interval_profiles/cw.json` + the append in `cw-run.sh` (a 6-hourly cadence reaches L5 in 8 days, so the compaction matters sooner there). Enable the static per-scan append on prod (`STATIC_NAMES=1`, IAM decision in `cw-static-names.md:152-163`). Rebuild at cw's next compaction with the unified static format (gaining anchors). Then retire over-time groups, and fold the age pyramid into the store or drop it. |
| r2 / m3 | Small corpora: the per-scan store is adequate. Either adopt the standard set (cheap at their size, and it makes r2 the reference small deployment, recommended for r2) or freeze them as "per-scan only" with the per-scan reader kept as a supported mode. If both move, the per-scan reader becomes ingest-only. |

### 6.5 Top recommendations

1. ~~**One runner for both append pipelines, before the interval store's L3 carry (~10-17).**~~ **Done 2026-10-10** (`append_runner.py`; specs/static-append.md "One runner for both stores", specs/interval-store.md §2.7). The interval store has deferred N-way carries (a separate non-fatal `carry` job, revision manifests, the lease, resumable merges), the manifest name filter (Python and TS, one `newestManifest`), threaded prune shared; outputs byte-identical to the pairwise path on fixtures. Not done here: `rebuild-state` for the interval store, the static pipeline's balanced ranges and publish + R2 in one VM, an interval-generation GC.
2. **Fold anchored search into the base index at the ~11-08 compaction.**
   - `/`+name sentinel rows go into `sx/` (+6% rows, free at compaction).
   - `end`/`exact`/`start` become drill kinds and catalog rows.
   - One liveness marker. Heavy `^q` works at every depth.
3. **A standard index set everywhere, with tailoring only by input presence.**
   - Hex rule on everywhere (1.1% of gcs rows, so no switch is needed).
   - Labels built iff the census finds them, evaluated at compaction (the hysteresis).
   - A per-generation capability manifest replaces five `meta.json` probes and most of the 30 (c) branches.
4. **Finish the transitions, then delete.**
   - Move scan existence off per-scan D1.
   - Re-cut or fold the v1 dates.
   - Then remove the per-scan serving path for store-held deployments and the v1 reader (~13 (d) branches).
5. **Execute the ClickHouse retirement and the immediate deletions.**
   - The box code (~45K LOC with tests, ≈ $710/mo).
   - `slices-bysize` (33.5 GB), superseded static and iv generations (~270 GB R2).
   - Six one-way flags.
   - Dead writers: `age.json`, `age-index`, `over-time.scans.json`.

## Open questions and unverified claims

- Live state was not checked:
  - whether gcs prod is deployed with `PATH_STORE=intervals`;
  - whether the re-cut `bysize` pointers synced;
  - which D1 v1 coarse/`user` pointers remain;
  - whether the ClickHouse VM, tunnel and timer still run;
  - whether cw prod's KV is bound;
  - live R2 run stacks.
- The gcs 1.1% hex share is a sample over coalesced-version ranges, not a full census.
- The `?lens=user:x` latent 500 on cw/r2/m3 (reader point 82) was inferred from code.
- The LOC estimates in §5 are from line ranges, not a prototype.
- K = 256 has no recorded rationale.
