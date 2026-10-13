# Anchored name search: `^q`, `q$`, `^q$`

Status: 2026-10-09: built for gcs gen `2026-10-08c` and its two runs (10-09, 10-09T1236), verified 1,110/1,110 against brute force, on GCS and R2, served by dev (`dev.gcs.oa.dev`). Code on branch `anchors` (not on prod). Built from option 2 of `specs/search-extensions.md` (2b ends-with heavy, 2c the name index), with Ryan's syntax `^q` / `q$`. The heavy-`^q` drill (2d) is not built; heavy `^q` degrades instead ("Heavy `^q`" below: a starts-with catalog at the fleet root, scoped reads below, branch `anchor-degrade`).

## Semantics

- `^q`: some name (path segment) starts with `q`. `q$`: some name ends with `q`. `^q$`: some name is `q`. Case-insensitive, like every term.
- A path matches when **some segment** matches. That is monotone down the tree, so match roots work as for a contains literal.
- A **match root** under view path P is a path under P whose name matches and **none of whose ancestor segments match**. The test is per segment: `b1/big-tomato/tomato.txt` is a root of `^tomat` (`big-tomato` doesn't start with `tomat`), though a contains-style test on the parent path would drop it.
- P itself matching (some segment of P) is the plain view, as for a contains literal.
- In the predicate (`pathQuery.ts`, the path-store fallback and the client), an anchor is a segment boundary: `^` = the path's start or right after a `/`, `$` = its end or right before a `/`. This also defines anchored globs (`^ckpt*final`) and terms with `/` (`^tmp/ttl`), which only the path-store fallback serves.

## Syntax (`simple`)

- An unquoted `^` opening a term and an unquoted `$` closing it are anchors: `{ kind: 'sub', text, start?: true, end?: true }` (globs alike).
- Quoted, they are literal (`"^a"`, `abc"$"`); anchors around nothing (`^`, `$`, `^$`) are the characters themselves.
- Negatives take anchors (`ckpt -^tmp`).
- In the `regex` syntax, `^` anchors the bucket, not a name; its help says so.
- **Indexed-only** (`FILTER_INDEXED_ONLY=1`) accepts one anchored literal without `/` or `*`: `^q` from 2 characters, `q$` from 3, `^q$` from 1. Shorter ones are `anchor-too-short` ("add the dot: `.gz$`"). Everything else is refused as before.

## Index

Per tier (the base generation, and each run under `deltas/<id>/`), new keys only:

| Key | What |
|---|---|
| `names/` (`shards.json`, `sx/s####.parquet`, `sidecar/`, `sidecar.parquet`) | The **name index**: one row per version (depth ≥ 1), `s` = `/` + the lowercase name, in the suffix shards' layout (`SX_SCHEMA`, sorted `(s, path, usr, vf)`, 8K-row groups, shards cut at three-character prefixes). `/` occurs in no segment, so `^q` is the range `/q…` and `^q$` the key `/q`. A run's rows are its `cdelta`'s versions (opens, and close records with their final `vt`), combined across tiers like suffix rows (equal `(s, path, usr, vf)`: the smallest `vt`). |
| `anchors/{end,exact}-rollups-index[.top].parquet`, `anchors/rollups/{end,exact}-s####.parquet` | Rollups of the heavy `(k, dir)`, in the drill's layout (`ROLLUP_SCHEMA`, two-level index). `end`: `k` an exact suffix (from `sx/`); `exact`: `k` = `/` + a name (from `names/`). `dir` is a directory or `''` (the fleet root; children = buckets). |
| `anchors/meta.json` | `R`, `K`, `rg` (the shards' row-group size), `idx_rg`, per rollup set its `row_groups`; a run's `scans`. Written last: the tier's liveness marker. |
| `anchors/keys-{end,exact}.parquet`, `anchors/keys/`, `anchors/census-start.*` | GCS only: per row group of the base's shards, its `s` and `path` bounds (the run builder's bounds), and the `^q` census. |

**No roots files.** `q$`'s rows are the suffix rows with `s == q` (a suffix of the name equal to `q` is exactly "the name ends with `q`"; at most one per version), sorted by `path`. So the rows under P are the key range `[(q, P/), (q, P0))` of the shards, bounded by whole row groups through the `s` and `path` column statistics, which the writer already stores untruncated. `^q$` is the same over the name index. The Worker cuts the `path` bounds from each shard's footer once (`StaticNames.keys`, cached apart from the group index as `keys-v1`); no key files are served.

**Heavy.** `(k, dir)` is heavy when more than R rows of `k` lie at or under `dir`, over every tier (rows, close records included, not roots: the dispatch bounds rows). Heavy directories are closed upwards. A heavy `(k, dir)`'s rollup: per child of `dir`, the running Σ size, n_files of the first hits under it, one cell per change; K = 256 kept children by peak bytes plus an exact remainder. Header `b` = rows under `dir`, `o` = children holding first hits.

## Dispatch (`staticAnchors.ts` `AnchoredSource`; Python `static_anchors.AnchoredReader`)

Per (key, P):

1. **Light.** The key's whole range (all tiers) ≤ `MAX_ROWS` (V + 2 groups) → read once, folded to first hits, held per isolate and per colo per stack version, cut to P. `q$` reads every tier's suffix shards (so it covers every scan), `^q` / `^q$` the anchored tiers' name index.
2. **Scoped** (`q$`, `^q$`). Over the anchored tiers, the groups meeting `[(k, P/), (k, P0))`: their rows ≤ `R + 4·Σ rg` → read them, keeping the rows under P. Each tier's bound is at most its true rows plus four groups: two straddling P's range, and the two groups at the ends of `k`'s run that hold other keys.
3. **Rollup.** Otherwise the true rows exceed R, so the builder made `(k, P)`'s rollup in some tier. The tiers' rollup rows stack newest first down to a full header (`DRILL_RULES.stack`).

`^q` has its own bound and no step 2–3: see "Heavy `^q`" below (the fleet root from the starts-with catalog, scoped reads below it).

**Tiers.** The base and the manifest's runs, up to the first run without `anchors/meta.json`. Answers carry the scans of the tiers they read (`Found.scans`), so a scan past the anchored stack is `scan-not-indexed` (a light `q$` excepted). A generation without `anchors/meta.json` declines every anchored key past light `q$`.

**Keys.** Anchored keys travel as `termKey`s: `/q` (`^q`), `q/` (`q$`), `/q/` (`^q$`). A static literal never holds `/`, so a key is unambiguous. `SuffixHits` hands them to the anchored source; the drill never sees them. `FILTER_STATIC_ANCHORS=0` turns the source off. Static responses are versioned 7: before this, `^q` / `q$` were plain literals.

## Building

**Base** (`dt-cloud static-names anchors …`, Batch through the deployment's `job/static-names.sh` with `MODULE=static_anchors`):

1. `names`: the base's `cintervals` → `names/` (one task: DuckDB sorts, `write_run_shards` cuts).
2. `census`: name-index prefixes with ≥ 50K rows → `anchors/census-start.{parquet,json}` (the heavy-`^q` count).
3. `rollups -k end` (the suffix shards) and `-k exact` (the name index), from a shared queue, biggest shard first. Per shard: the keys with more than R rows, `heavy_dirs` bottom-up, the first hits, `rollup_cells`, and the shard's key bounds.
4. `index`: both rollup sets' two-level indexes and key tables, then `meta.json`.

**Runs** (`anchors run -d ID`, level 0). The run's `names/` comes from its `cdelta`. Its rollups:

- Candidate keys are those whose rows over every tier can pass R: the run's count plus each prior tier's sidecar bound (cumulative group rows).
- For each `(k, dir)` that was heavy before (some prior tier's rollup) and that the run's rows touch: a delta header (`kind` −1: kept, `b` + the run's rows, `o`) and the cells dated at the scan. Each series' last value moves by the run's first-hit events (an open adds, a close record subtracts). A child no tier has kept is kept while fewer than K are (then every child holding roots is kept, so it is new); otherwise it goes to the remainder.
- For each `(k, dir)` the run makes heavy: candidates are bounded by the prior tiers' key bounds; where `bound + run rows > R` the prior rows are read exactly (≤ R, by stickiness). Past R, a full restatement (`kind` 0) is built from every tier's first hits under `dir`.
- `o` is exact while fewer than K children are kept; after that, a delta adds only the new kept children (a lower bound). It is shown as the "N others" count only.
- Merged runs (the counter): `names/` via `merge_shards`; rollups stacked per `(k, dir)` (`merge_rollups`).

## Tests

- **Python** (`cloud/tests/test_static_anchors.py`). Base + two runs + the runs merged, R = 3, K = 2, small groups. Every key (20: `q$`, `^q`, `^q$`, including buckets, absent keys and short `^q`) at every path on every scan, through the reader at three whole-range bounds (light, scoped, rollups), equals brute force from the versions. The name index is one row per coalesced version. The scan-file brute-force SQL equals the version brute force. Mutations caught: a contains-style parent test, a stack ignoring the runs, a largest-`vt` combine.
- **TypeScript** (`staticAnchors.test.ts` on `fixtures/static-anchors/`, built by the Python builder). The same sweep through `AnchoredSource` for the base, the first run and both runs (16 tests), plus a run without `anchors/meta.json`, a generation without anchors, a mutated parent test, keys, `SuffixHits` routing; syntax, predicate and indexed-only cases.

## Built: gcs `2026-10-08c` (2026-10-09)

All runs were spot n2-highmem-16 in us-east1 through the deployment's `job/static-names.sh` (`MODULE=static_anchors`, `ENV_JSON` setting `STATIC_NAMES_PROFILE=gcs`).

| Stage | Wall | Output |
|---|---:|---|
| `names` (1 task) | 20 min | 797,994,947 rows (exactly the coalesced versions), 13 shards, 7.5 GB |
| `census` (1 task) | 3 min | 7,863 prefixes ≥ 50K rows; **2,788 over V** (4.07B rows summed over the prefix chains) |
| `rollups -k end` (16 tasks, 262 shards) | 24 min (5.5 task-hours; the largest shards ~15 min) | 3,864 heavy exact suffixes, 29,938 heavy dirs, 3,122,781 cells |
| `rollups -k exact` (6 tasks, 13 shards) | 6 min (0.26 task-hours) | 194 heavy exact names, 2,476 heavy dirs, 374,902 cells |
| `index` (1 task, n2-highmem-4) | 2 min | `meta.json`; key tables (1.5 GB, GCS only) |
| `run -d 2026-10-09` | 12 min | 479 heavy suffixes touched, 1,843 delta headers, 63 newly heavy dirs (149 probes, 16.2M prior rows); names 38 MB |
| `run -d 2026-10-09T1236` | 16.5 min | 825 heavy suffixes touched, 2,221 deltas, 100 newly heavy (393 probes, 39.6M rows); names 90 MB |
| R2 (`r2-batch`, `-x anchors/meta.json`, then `-o anchors/meta.json`, per tier) | 6 × ~2.5 min | base 307 objects, 7.54 GB; runs 38 MB and 90 MB |

**Heavy `^q` census.**

- Measured over V: `^step` 18.7M, `^shard` 36.6M, `^part-` 38.4M, `^data` 59.3M, `^.zarray` 2.4M, `^config` 418K, `^results` 380K, `^train` 288K, `^eval` 225K, `^model` 173K.
- Light (< 50K): `^ckpt`, `^tokenized`, `^qwen`, `^llama`, `^tmp`, `^events`, `^marin`, `^_success`.
- By band: 1,548 prefixes hold 100–250K rows, 307 hold 250–400K, 933 more than 400K.
- A heavy-`^q` drill would be the size of the long drill, and is not built: those terms decline (`term-too-common`).
- Raising the `^q` whole-range bound to 400K rows would serve 1,855 of the 2,788 at a cold decode of a few seconds. This is a reader constant, not an index change.

**Verification.**

- Cases: 34 keys (18 `q$`, 8 `^q`, 8 `^q$`). For each, the fleet root, then the two largest children, then the largest child at each level down to depth 4: 236 (key, P), from the Python reader on the base.
- `anchors brute` (5 tasks, 11 min) over the scans 2026-10-08, 10-01 (v2), 09-15 (v1), 10-09 and 10-09T1236.
- `anchors query`: the Python reader over GCS (base and both runs).
- **1,110 / 1,110 (key, P, scan) equal**: 950 non-empty, 29.2M children. By source: 66 rollup, 86 scoped roots, 70 light; plus 10 plain and 4 declined.
- Files: `gs://oa-gcs-usage-dvx/static-names/2026-10-08c/anchors/verify/{cases.jsonl,answers.jsonl,verify.json,brute/}`.

**Dev** (`dev.gcs.oa.dev`, a throwaway `gcs` + `anchors` merge), cold:

- `?f=.json$`: fleet root 111,277,916 objects, from the rollup; subtree 2.0 s, diff 2.0 s, series 1.2 s.
- `marin-us-central1?f=.json$`: subtree 3.4–4.1 s, diff 4.2 s, series 2.4 s.
- `?f=^config.json$`: 412,876 objects, 1.1 GB; subtree 1.2–2.0 s, diff 2.1 s, series 1.2 s.
- `?f=^ckpt$`: no matches on every scan (brute force agrees); subtree 0.25 s.
- `?f=^train`: `term-too-common`, inline.
- `^a`, `gz$`: `anchor-too-short`.
- Warm, every view is 60–210 ms.

**Cost.** About 10 spot VM-hours ≈ $2.2, plus R2 egress of 7.7 GB ≈ $0.9, plus laptop reads of GCS for the cases and the reader (a few GB) ≈ $0.3–0.5. **≈ $3.5 in all.**

- Per scan, the `anchors` stage is 12–17 min on one spot VM (~$0.05) and 40–90 MB to R2 (~$0.01). It runs beside the drill (`runs add` submits both at once, since 2026-10-10), so it adds nothing to the per-scan wall time while the drill is longer.
- Storage: R2 +7.6 GB (~$0.11/month), GCS +9 GB.

## Heavy `^q`: graceful degradation (2026-10-09, branch `anchor-degrade`)

Before: a `^q` whose range held more than V + 2 groups declined everywhere (`term-too-common`), map, diff and series alike. No aggregate of a prefix existed: the contains catalog and the drill are keyed by contains literals, the `end` / `exact` rollups by a whole suffix or a whole name, and the census holds only row counts. So even the fleet root's per-bucket table had nothing to read.

**Dispatch for `^q`** (`staticAnchors.ts`, mirrored by `AnchoredReader`):

1. **Whole range** ≤ `START_MAX_ROWS` (400K + 2 groups) rows over the anchored tiers: read once, folded, held, cut to P (as before, with the larger bound). Abandoned past `START_MAX_HITS` (150K) first hits, which then counts as heavy.
2. **Fleet root, heavy:** the **starts-with catalog** (`anchors/start/`): per prefix with more than R rows, per bucket, the running Σ size and n_files of its first hits (the rollup layout at `dir = ''`, K = 256 kept buckets, stacked newest tier first). Exact per bucket on every scan its tiers cover. The answer is `bucketsOnly` + `scopedBelow` (the client flags "per-bucket totals only at the top: inside a bucket, this term is searched where its matches are few enough").
3. **View path P, heavy:** the groups of the prefix's range whose `path` bounds meet `[P/, P0)`; read when they hold ≤ `START_MAX_ROWS` rows and fold to ≤ `START_MAX_HITS` hits, else `term-too-common`. P's rows are **not** contiguous within the range: it is sorted `(s, path)`, so they are contiguous per name. A group spanning many names (`step-1000`, `step-1001`, …) has wide path bounds and is not pruned. This is the limit of the scoped read (below).
4. **First paint:** a view's first-paint request for the fleet root answers from the catalog while the range isn't held yet (`HitOpts.firstPaint`, through `indexedGate` and `readView`); the full request does step 1. So the root shows bucket tiles at once and the full read replaces them.

**Index** (per tier, new keys only): `anchors/start/{meta.json, start-rollups-index.parquet, start-rollups-index.top.parquet, rollups/start-s####.parquet}`. `meta.json` is the catalog's own liveness marker, written last (tiers published before it keep their `anchors/meta.json`). The reader uses the prefix of anchored tiers that carry one. Its answers carry those tiers' scans.

- **Base** (`anchors start -g GEN`): per name shard, the census prefixes past R in the shard's three-character range, summed per `(prefix, bucket, vf, vt)` one prefix length at a time (no `(row, prefix)` pairs materialized), then `rollup_cells` at level 0.
- **Run** (`start_run`; `anchors start -d ID`, and `anchors run` builds it for new runs): candidates are every prefix of the run's names whose run rows plus each prior tier's name-sidecar bound over `[k, k + U+10FFFF)` pass R. A prefix heavy before gets a delta header and cells at D (`delta_cells`, mode `start`). One the run makes heavy is restated from every tier's first hits (prior rows ≤ R, read exactly).
- **Merged runs:** `merge_rollups` over the inputs' `start` files, when every input carries one.
- **Runner:** the anchors stage is done once `anchors/meta.json` and (when the base has a catalog) `anchors/start/meta.json` are there. `anchors run` on a run whose anchors exist builds only its catalog. R2 copies `anchors/start/meta.json` before `anchors/meta.json` (`R2_SERVED` lists the catalog).

**Built (gcs `2026-10-08c`)**, spot n2-highmem-16 us-east1 through `job/static-names.sh run start`:

| Tier | Wall | Output |
|---|---:|---|
| base | 5.3 min (13 shards, 11–50 s each) | 2,788 prefixes (= the census over V), 89,474 rows, 720 KB |
| run 2026-10-09 | ~3 min | 3,757 rows |
| run 2026-10-09T1236 | ~3 min | 7,053 rows |

R2: `r2-batch` per tier (`-o anchors/start/ -x anchors/start/meta.json`, then `-o anchors/start/meta.json`; it needs `-b oa-gcs-usage-dvx`, since its `ENV_JSON` has no profile). Cost: ~15 VM-minutes ≈ $0.10, plus the brute-force job (3 tasks) ≈ $0.15. **≈ $0.3 in all.**

**Measured: the Worker reader, cold, on a laptop over GCS ranged reads** (`StaticNames` + `AnchoredSource`; fetched sizes include the name shard's group index, which the colo caches):

| View | Source | Time | Read | Heap held (after GC) / peak |
|---|---|---:|---|---|
| `^train` root, whole (before the hit cap) | 246,555 hits from 311K rows | 4.6–6.2 s | 22 MB | **72 MB / 135 MB**, cache JSON 36 MB |
| `^eval` root | 38,921 hits from 246K rows | 2.7–6.3 s | 19 MB | 16 MB |
| `^train` root, capped | catalog (whole abandoned at 150K hits) | 3.3 s | 22 MB | 5 MB |
| `^step` / `^data` / `^config` root | catalog | 1.5–2.4 s | 14–20 MB (footer) | 2–6 MB |
| `^train` in a bucket | scoped, 196–254K rows, 37–83K hits | 9–10.5 s (incl. the abandoned whole read) | 44 MB | 32 MB |
| `^config` in a bucket | scoped, 25–123K rows | 2.4–10 s | 29 MB | 3–9 MB |
| `^results` in us-central1 | scoped, 319K rows, 84K hits | 12 s | 44 MB | 37 MB / 129 MB |

Why the hit cap: a 400K-row read can hold ~400K first hits at ~300 B each (≈ 120 MB), past a Worker isolate's 128 MB. 150K hits ≈ 45 MB held. Peaks are garbage awaiting GC (one group's decoded columns at a time).

**Verified** (`anchors query` over GCS vs `anchors brute -o anchors/verify-start/brute` on 2026-10-08, 10-09, 10-09T1236): `^train`, `^train-`, `^config`, `^step`, `^data` at the root, every bucket and a deeper path. **84 / 84 (case, scan) equal**: 5 catalog roots, 23 scoped. 15 declined, 1 plain. Files: `gs://oa-gcs-usage-dvx/static-names/2026-10-08c/anchors/verify-start/`.

| Term | Root | Buckets | Deeper |
|---|---|---|---|
| `^train` (311K rows) | catalog, 870 TB | 6/6 scoped (196–254K rows read) | `us-central2/tokenized` scoped |
| `^train-` | catalog | 6/6 scoped (180K) | `us-central2/raw` scoped |
| `^config` (418K) | catalog | 5/6 scoped; `us-central1` past the hit cap | `us-central{1,2}/checkpoints` scoped |
| `^step` (18.7M) | catalog, 768 TB | only `us-west4` (360K); 5 declined (0.5–18.7M) | `us-west4/checkpoints` scoped; `us-central2/checkpoints`, `eu-west4/checkpoints` declined (12.3M, 467K: no pruning) |
| `^data` (59.3M) | catalog, 1.69 PB | 6/6 declined (26–53M) | `us-central2/raw` declined (26M); `eu-west4/datakit` plain |

**What's still missing:** a per-directory aggregate for heavy prefixes below the root, the `^q` analog of the `exact` rollups (rollups at each heavy `(prefix, dir)`), or a copy of each heavy prefix's rows sorted by path so a scoped read is one contiguous range. Either is the size of the long drill (4.07B prefix-row pairs over the 2,788 prefixes), which is why `^step` / `^data` below the root still decline. A cheaper middle step: the catalog at level 1 too (per `(prefix, bucket)`, children = top-level dirs), built the same way (`heavy_dirs` capped at `lvl ≤ 1`).

**Left.**

- Compaction (`static_compact`) must rebuild `names/` and `anchors/` for the new generation (the base stages over it).
- The anchors stage could run in parallel with the drill, which reads none of its outputs.
- ~~The `^q` bound decision (above).~~ Raised to 400K rows with a 150K first-hit cap: "Heavy `^q`" below.
- The "N others" count after K kept children in a delta is a lower bound.
