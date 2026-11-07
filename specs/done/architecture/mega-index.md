# One consolidated store and name index ("mega-index")

Status: **retired 2026-10-10** with the ClickHouse store ([clickhouse.md] status); the code is at tag `ch-store-final`.

Evidence checkpoint: 2026-10-08, ch-store VM resized to n2-highmem-32 (32 vCPU, 251 GB, 2 TB pd-ssd). The goal: every scan, base tree and text search, in one interval-coded store, so a day costs O(changes) and any historical scan stays queryable without its own index.

## What exists

ClickHouse `default` is the append-only history (`ingest.py`, [clickhouse.md] §1): `nodes` versions of `(depth, path, usr)` opened at `vf`, `closures` closing them at `vt`, `names` (every lowercase basename ever seen, trigram index), `scans`. It holds 68 scans, Jul 30 – Oct 6. Scans before 09-30 are an older source format; 09-30 is a complete rebaseline (`scan_epochs`), so v2 queries read from there.

A path is one row per owner slice: `usr = ''` is the **unowned** slice, not a total. Path totals are the sum over slices (Oct 5 `marin-us-central1`: 608.8 T unowned + 166.9 T owned = 775.7 T).

## Daily append

| Scan | Machine | Threads / pairs | Wall | Opened | Closed |
| --- | --- | --- | ---: | ---: | ---: |
| 10-01 … 10-04 | n2-standard-8 | 8 / 4 | 548–595 s | 2.7–3.1M | 1.3–1.5M |
| 10-06 | n2-highmem-32 (cold cache after resize) | 32 / 16 | 807 s | 2.64M | 125.9M |

More cores did not help: 10-06 is the deletion day (125.9M closures, the Oct 5→6 drop of ~123M paths) and ran on a cold page cache, so this is not a like-for-like speedup measurement. The append is range-parallel FULL JOINs; the next lever is fewer, larger pairs on a warm cache, not more threads.

**Verification:** Oct 6 reconstructed as-of from `default` equals the standalone `daily_scalar_oct06_fleet_a` exactly: 606,128,523 paths, 25,862,761,745,080,647 bytes summed over paths, 3,396,993,072 objects summed, and the order-insensitive digest `sum(cityHash64(path, b, o))` 12515680132438215233 on both. (The full as-of reconstruction is a 705 s scan; it is a verification, not a serving path.)

## Name search over every scan

`chstore/mega_names.py`, `ch-mega-names`, `ch-mega-names-build`. A literal on scan D:

1. vocabulary: names containing it whose live span covers D (`name_spans`);
2. postings: rows with those names live on D (opened by D, not closed by D);
3. first hits: name matches and the parent path does not (a slash-free literal sits in one segment, so this is exactly "no ancestor matched"; the same rule as `hot_l1.oracle`);
4. bucket totals over first hits, summed across owner slices.

Exactness: a brute-force first-hit oracle over every 1–3-character substring of the fixture's names, on every ingested day, with the store's projections, all-time postings and span postings, each with and without `name_spans` (`cloud/tests/test_chmega_names.py`). On real data, 30/30 `(date, literal)` answers equal the per-scan index (Oct 6) and the frozen union (Oct 4/5).

Two findings shaped the index:

- **The vocabulary must be filtered by live span.** `names` is all-time: Oct 6 `3p` drew 10,429,581 names (33.6 s) where Oct 6 itself has 26,696. `name_spans(l, first, last)` (earliest version → last closure, or never while any is open) is a superset filter built from two per-name aggregations, no join: 136,869,079 names, 90,131,651 open (exactly Oct 6's distinct names), 147 s.
- **The store's own `by_name` projections are too coarse.** `nodes` is partitioned per scan (343 active parts) in 8,192-row granules, so each name costs a granule per part: Oct 6 `5418` (33K names, 108K versions) read 228M rows / 32 GB in 9–12 s. Name-sorted postings in 256-row granules, unpartitioned, fix the layout.

### Postings size per span

| Span | `_nodes` rows | `_nodes` bytes | `_closures` rows | `_closures` bytes | Insert | With `OPTIMIZE FINAL` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| all time (68 scans) | 1,149,549,991 | 12.36 GB | 543,418,112 | 4.59 GB | 128 s | 858 s |
| 10-01 … 10-06 (6 scans) | 794,642,579 | 7.61 GB | 188,510,700 | 1.12 GB | 467 s | 1,240 s |
| 10-04 … 10-06 (3 scans) | 790,449,380 | 7.39 GB | 184,317,501 | 1.02 GB | 466 s | 762 s |

For comparison, Oct 6's per-scan name index alone is 9.5 GB (`daily_name_index`, 4m43s). A span costs one rebaselined snapshot plus each day's churn: three scans and six scans differ by 0.5%; all 68 scans (with the pre-09-30 format's dirs-only rows and the rebaseline) cost 1.9× one span. The span builds pay a sorted-merge anti-join against earlier closures (466 s); the all-time build copies without one (128 s). Daily upkeep would append the day's opened versions and closures, which the ingest already isolates.

### Latency (8 threads unless noted; warm second run; n2-highmem-32)

Every `(date, literal)` with a reference equals it: 120/120 across runs (Oct 6 vs its per-scan index, Oct 4/5 vs the frozen union).

| Literal | Names / rows on Oct 6 | Per-scan index (Oct 6) | Store projections | Postings 10/4–6 | Postings all time | All time, 32 threads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `3p` | 26,696 / 38,363 | 1.56 s | 35.7 s | 1.23 s | 1.18 s | 0.73 s |
| `6h` | 20,321 / 26,830 | 1.48 s | 0.97 s | 1.20 s | 1.14 s | 0.70 s |
| `5418` | 19,774 / 28,000 | 1.32 s | 10.5 s | 3.49 s | 2.90 s | 2.07 s |
| `nce_in` | 27,253 / 27,348 | 1.30 s | 1.89 s | 0.47 s | 0.45 s | 0.41 s |
| 36-char UUID literal | 2,000 / 10,638 | 0.29 s | 0.33 s | 0.08 s | 0.06 s | — |
| `58.index.npy` | 1,045 / 90,426 | 0.29 s | 0.47 s | 0.19 s | 0.14 s | — |
| `tensorstore` | 30 / 38 | 0.15 s | 0.36 s | 0.07 s | 0.07 s | — |
| `zzqx` (no match) | 0 / 0 | 0.08 s | 0.04 s | 0.05 s | 0.05 s | — |

The per-scan column is the reference build (`hot_l1.build(daily=True)`, uncapped, no serving overhead), not the served lane. The 10/4–6 and earlier all-time runs used a tuple `NOT IN` for the closure filter; the all-time column is the shipped hash-join form. Older scans answer the same way: Sep 15 (pre-rebaseline format) 0.07–1.95 s.

What remains slow:

- **1–2 character literals** spend ~0.8 s (0.38 s at 32 threads) scanning `name_spans` without the trigram index. Precomputing every 1–2 character literal into the catalog (workstream A) removes this.
- **Literals whose names carry many versions** (`5418`: 19.8K names, 108K versions over all time) read a 256-row granule per name: 2–3 s. Fewer parts (the 10/4–6 table kept 19 after `OPTIMIZE`) and smaller granules are the next levers.
- **Hot-on-the-day literals** are slow by construction (`3p` on Oct 4: 10.4M live names, 19.6M rows, ~10 s); they are catalog literals there and never reach this path.

## Daily upkeep

`ch-mega-names-append DATE STEM…` (`mega_names.append`) adds one ingested scan to the name index: the `nodes` partition `vf = D` (versions it opened) and the `closures` partition `vt = D` (versions it closed) go into each postings stem as a staged side table attached whole, and their per-name deltas into `name_spans`. `name_spans` is an `AggregatingMergeTree` of `(l, min first, sum versions, sum closed, max closed_last)`; a name is live on D when `min(first) <= D` and either `sum(versions) > sum(closed)` or `max(closed_last) > D`, so readers `GROUP BY l` and appended rows need no rewrite. `name_index_log` records what each target covers; an append at or before it is refused, so a scan is never added twice.

Verification on the VM (2026-10-08): `name_spans` and postings `m` built through Oct 5 (91 s and 117 s unmerged), then Oct 6 appended:

| Target | Rows appended | Wall |
| --- | ---: | ---: |
| `name_spans` | 44,208,945 name deltas | 13.6 s |
| `m_nodes` / `m_closures` | 2,635,102 / 125,927,390 | 9.7 s |

`m` then equals the all-time build `all` exactly (`ch-mega-names-digest`: 1,149,549,991 / 543,418,112 rows, identical order-insensitive digests), and `name_spans` equals a full rebuild per name (136,869,079 names). Oct 6 is the heaviest day there is (126M closures); an ordinary day is ~3M opened and ~1.5M closed. A final merge after the append (`OPTIMIZE … FINAL`) took 152 s + 374 s; it is optional (readers are correct over any number of parts).

## Serving

`serve-query -P STEM` (`MEGA_POSTINGS=STEM job/ch-store.sh serve`) binds the dated name-summary runtime to the consolidated index (`mega_names.binding`: the stem's and `name_spans`' coverage, and each covered scan's bucket geometry from depth-1 `n_desc`). In the existing bounded cold lane (one slot, 5 s, 16 threads, 200K-name budget):

- a daily scan's unregistered literal answers from the consolidated index instead of the scan's own name index (its geometry must equal the catalog's at boot);
- every scan without a frozen or daily catalog becomes available as `consolidated-store-v1`: every literal on demand, no catalog. Scans that have catalogs keep them.

### Served latency (Oct 6, the workstream-A T-curve picks, 8 per band, through `/api/name-summary`)

Same box and lane (16 threads, 5 s); "per-day" is the scan's own name index (`ch-daily-name-index`), "consolidated" is `m` (all 68 scans, merged). Cold drops the OS page cache before each request. Wall seconds, p50 / max:

| Paths per literal | per-day cold | consolidated cold | per-day warm | consolidated warm |
| --- | ---: | ---: | ---: | ---: |
| 8.5K–10K | 1.99 / 2.21 | 0.80 / 0.90 | 0.64 / 1.78 | 0.48 / 0.72 |
| 25K–30K | 1.50 / 2.86 | 0.61 / 2.92 | 0.49 / 1.04 | 0.31 / 2.69 |
| 85K–100K | 2.25 / 3.49 | 0.60 / 3.83 | 0.65 / 1.39 | 0.37 / 3.39 |

All 48 answered. The consolidated index removes the cold floor (the per-scan `nodes_by_name` page misses): median cold latency drops 2.5–3.7×. Its tail is the same shape as before, driven by distinct matching names (`48.parquet` 3.4 s, `nk080` 2.9 s, `bb-` 2.7 s warm: the postings join over every version those names ever had).

Scans with no catalog (every literal on demand):

| Literal | Sep 15 cold / warm | Oct 1 cold / warm |
| --- | ---: | ---: |
| `3p` | 3.58 / 3.59 | refused: > 200K names |
| `6h` | 1.52 / 0.81 | 1.33 / 1.05 |
| `5418` | 4.78 / 4.20 | deadline (5 s) |
| `gof`, `nk080`, `116.tok` | 0.30–0.83 / 0.09–0.44 | not run: lane quarantined after the deadline |

A request that hits the 5 s deadline is cancelled and its HTTP transport can't be proven idle, so the lane quarantines and recovers ~7 s later; requests in that window get 503 (busy), never a partial answer. A finished query that lingered in `system.processes` used to quarantine the lane the same way after a *successful* answer (`bb-`, every time); verification now polls for up to 1 s.

## Consolidated catalog ("mega-catalog")

`chstore/mega_catalog.py`, `ch-mega-catalog-build`, `ch-mega-catalog-check`, `serve-query -K` (`MEGA_CATALOG`). Every scan in the store gets its own catalog — its registry (every literal with ≥ T = 100K direct name-substring paths at any length, plus every literal of ≤ 2 characters present at all: the complete length domain) and each registered literal's first-hit bucket bytes/objects — kept as versions in four tables of the store, appended from each scan's changes.

### Design as built

| Table | Rows | Holds |
| --- | --- | --- |
| `catalog_names (l, d, n)` | per name per scan it changes | the change in the name's live paths at scan `d`; a base scan holds full counts. Summed from the latest base through D: D's weighted vocabulary |
| `catalog_multi (depth, path, d)` | paths that ever had ≥ 2 live owner slices in the epoch | every other path has at most one live slice, so its day's events decide it |
| `catalog_terms (term, vf, member, paths)` | a version when a tracked literal's registration or count changes | the registry: member on D = the newest version at or before D |
| `catalog_cells (term, bucket, vf, b, o)` | a version when a tracked literal's bucket total changes | first-hit totals; the newest version at or before D |

Every literal ever registered stays **tracked**: its cells keep moving while it is below the threshold, so re-registration costs nothing. A literal registered for the first time (an **entrant**) gets a complete answer from the consolidated name index (`mega_names.answer`, ≤ 400 per scan), else from one pass over the scan.

**Appending scan D** costs its churn, not the scan:

1. **Counts** (`counts_append`): each path the day's `changes` touch moves its name by `[live after] − [live before]`. A path not in `catalog_multi` had at most one live slice, so only-closes is −1 and closes-and-opens is 0 (its opened slices are live after); only-opens is +1 unless a slice was already live. Only those pure opens, plus touched multi-slice paths, are read from the store, as versions opened minus closures (no version join). Reading every touched path instead cost 394 s on Oct 6 and exceeded 64 GiB on Aug 17.
2. **Registry**: the native census (`native/hot_frequency.cpp`) over the maintained vocabulary (`SELECT l, sum(n) … GROUP BY l`), exactly as the single-scan census over the scan's paths.
3. **Answers**: the day's signed `changes` rows stream through `native/catalog_delta.cpp` (16 streams over primary-key ranges): one Aho–Corasick automaton over every tracked literal; a row adds `sign·(size, n_files)` to each literal its name contains and its parent path does not. That equals the change in first-hit totals because a directory's rollup is its own version: when anything under it changes, its old version closes and a new one opens.
4. **Versions**: only changed registrations and cells are written; the scan is logged in `name_index_log` (stem `catalog`), and a rerun drops an unlogged scan's partial rows first.

A **base scan** (the store's first, or a source-format switch, where `ingest` records no `changes`) computes all three from its live rows. The census stays a full pass over the vocabulary rather than incremental: a watermark scheme (exact counts kept down to T/2, a positive-delta census to catch risers) is sound, but a literal crossing up from below the watermark still needs an exact recount over every name containing it, which on an upload-heavy day is a vocabulary pass anyway, and it adds drift state. The census is 12 s at the v1 store's 15M names.

### Backfill (2026-10-08, n2-highmem-32, 16 kernel streams, census 32 threads)

All 68 scans, Jul 30 – Oct 6, in 4,259 s of processing (71 min) — against ~13 min per scan for a single-scan catalog build from its own scalar database (~14.7 h for 68, plus building those databases).

| Scans | Wall p50 (max) | Counts | Census | Delta kernel | Entrants | Registered | Versions written per scan (terms / cells) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Jul 30 base (211.5M paths) | 297 s | 216 s (hash grouping; now in key order) | 12 s | — | 70 s (full pass) | 10,862 | 10,862 / 48,268 |
| 60 v1 appends | 15 s (447 s, Aug 25) | 1.7 s | 11.8 s | 0.5 s (42 s, Aug 17) | median 0 (272 on Aug 25: 387 s one by one) | 10,724–11,207 | 5,307 / 16,294 |
| Sep 30 base (777.1M paths) | 738 s | 351 s | 147 s | — | 237 s (full pass) | 98,085 | 96,143 / 262,610 |
| 6 v2 appends | 267 s (375 s, Oct 6) | 13 s (77 s, Oct 6) | 144 s | 2.7 s (113 s, Oct 6) | 92 s (~58 a day, one by one) | 70,662–98,242 | 17,614 / 80,336 |

Oct 6 is the deletion day: 127.8M touched paths, of which only 1.84M (pure opens and multi-slice paths) were read from the store; Aug 17 rewrote 71.8M v1 directories and read 11,958. Each scan's net path change matches its scan's slice-count change to within the multi-slice paths (Oct 3: +1,401,026 both ways; Oct 5: −53,635,710 paths vs −53,635,889 slices).

Sizes: what is served — `catalog_terms` (553,774 rows) and `catalog_cells` (1,819,081 rows) — is **24 MB for all 68 scans**; Oct 6's single-scan catalog artifact alone is 30 MB. The appender's state, `catalog_names` (220M rows), is 1.44 GB; `catalog_multi` 0.3 MB.

The v2 append is now census-bound (144 s: 86 s of it reading the 126M-name vocabulary) plus first-time registrations (~1.6 s each from the name index). Next levers: read the vocabulary in key order, and answer a scan's entrants in one batched postings pass.

### Exactness on real data

| Scan | Reference | Registered | Result |
| --- | --- | ---: | --- |
| Oct 6 | the published `dated-l1-oct06-catalog-complete-b` (independent: per-scan scalar database, `hot-l1-stream` kernel) and its census counts | 70,662 | equal: registry, every count, every nonzero bucket total |
| Sep 15 (v1) | a fresh single-scan build from its live rows (`ch-mega-catalog-check -f`, 410 s) | 10,725 | equal |
| Oct 3 (v2) | a fresh single-scan build from its live rows (1,011 s) | 98,242 | equal |

A fresh build of Oct 6 from live rows exceeds the 64 GiB statement cap: its live-path step anti-joins against every v2 closure through Oct 6 (~130M, the deletion days) in one set. Oct 6 is covered by the independent published catalog above; a fresh check there needs its live-path step split by key range too. Locally (`cloud/tests/test_chmega_catalog.py`): every scan of a five-day fixture equals a brute-force catalog over every substring of every name, a fresh build, a resumed build and a mid-history base; disabling the multi-slice rule fails six of the seven tests.


### Served (dev, `serve-query -P m -K catalog`, 1 cold slot, 16 threads, 5 s)

The 24 T-curve picks plus `3p 6h 5418 gof nk080 116.tok`, through `/api/name-summary` on the node, cold (page cache dropped before each) then warm. Wall seconds, p50 / max per band (all bands below T, so on demand):

| Scan | 8.5–10K cold | 25–30K cold | 85–100K cold | 8.5–10K warm | 25–30K warm | 85–100K warm |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Oct 6 (daily catalog) | 0.84 / 0.94 | 0.61 / 3.02 | 0.63 / 4.13 | 0.50 / 0.71 | 0.32 / 2.83 | 0.40 / 3.52 |
| Oct 1 (consolidated catalog) | 0.80 / 0.90 | 0.59 / 2.99 | 0.60 / 3.92 | 0.50 / 0.73 | 0.32 / 2.90 | 0.39 / 3.29 |
| Sep 15 (consolidated catalog) | 0.39 / 0.64 | 0.36 / 3.00 | 0.62 / 0.84 | 0.16 / 0.37 | 0.12 / 2.80 | 0.29 / 0.48 |

| Literal | Oct 1 cold / warm | Sep 15 cold / warm | Before the catalog (Oct 1) |
| --- | --- | --- | --- |
| `3p`, `6h` | catalog, 29–38 ms / 26–37 ms | catalog, 31–36 ms / 22–33 ms | refused (> 200K names); 1.3 s |
| `5418` | deadline (5 s) / 4.94 s | 4.71 s / 3.72 s | deadline, then the lane quarantined |
| `gof`, `nk080`, `116.tok` | 0.16–3.97 s / 0.09–2.20 s | 0.30–0.82 s / 0.10–0.43 s | not run (quarantined) |

89 of 90 requests answered (all 30 warm on each scan; the miss is `5418` cold on Oct 1 and Oct 6, which exceeds the 5 s deadline). A deadline now fails only that request: in a back-to-back burst on Oct 1 (`5418 3p gof 5418 nk080 6h`) all six answered, where the same sequence before returned three instant 503s after the deadline. `5418` (below T: tens of thousands of names with many versions) is the one literal still at the budget cold.

## Below-T cost: what an on-demand answer reads (2026-10-08)

`5418` is below T on every scan (~28K live paths on Oct 6) yet took >5 s cold. Measured over 40 non-member literals
(10K–400K all-time postings rows, chosen across bands and name counts) on Oct 6, Oct 1 and Aug 15, the served
postings statement alone (`mega_names.answer`'s), cold = page cache + ClickHouse caches dropped, n2-highmem-32,
16 threads. Every variant's answers equal the current one's on all 120 (term, date) pairs. Scripts:
`tmp/cost/{candidates,curve,segtree,ranges}.py` (worktree scratch); data on the VM under `/data/cost-curve/`.

**What drives it.** Not live paths, not all-time postings rows, not names:

| `m_*` layout | date | warm p50 / max (s) | cold p50 / max (s) | corr(warm, rows read) |
|---|---|---:|---:|---:|
| 16 parts (as appended) | Oct 6 | 1.15 / 2.13 | 3.47 / 4.95 | 0.87 |
| | Oct 1 | 1.05 / 2.60 | 3.19 / 5.67 | 0.87 |
| | Aug 15 | 0.20 / 1.74 | 1.14 / 3.37 | 0.98 |
| merged to 1 part (`OPTIMIZE FINAL`, 6 min) | Oct 6 | 1.04 / 2.11 | 3.68 / 5.36 | 0.79 |
| | Oct 1 | 0.98 / 2.35 | 3.53 / 5.66 | 0.79 |
| | Aug 15 | 0.19 / 1.49 | 1.18 / 3.36 | 0.96 |

- Merging cut rows read 2.3× (Σ 342M → 149M on Oct 6) and changed latency little: rows read are dominated by
  one 256-row granule per matching name per table (per part, before the merge), not by versions.
- Warm latency tracks rows read (CPU). Cold latency does not track rows, bytes, names or a granule estimate
  (`Σ(rows + 256)` per name: corr 0.1–0.5): it is page-cache misses at the names' positions in the sort order.
  Literals whose names sort apart (hex/shard numbers: `11979`, 3.7K names, 4.2 s; `c296`, 10K names, 4.1 s) cost
  far more per name than ones whose names cluster (`pio`, 96K names, 1.7 s; `motio`, 59K, 1.4 s). Mark-cache
  reload is ~0.4 s of it (`5418`: 4.4 s with marks cached, 4.9 s without; 1.9 s warm).
- So no per-name census weight bounds cold latency: a weight that covered `11979` (~1 ms per scattered name)
  would register every literal in a few thousand names. The bound that holds is operational: the all-time
  postings are ~18 GB compressed on a 251 GB box, so keep them resident (warm max ≤ 2.6 s over the curve at
  8–16 threads), and treat cold as the post-restart case.

**Segment tree over the scan axis** (scans numbered 0..N−1, H = 128 leaves; a closed version stored at the
canonical nodes of its `[vf, vt)` — 4.18 per version over the full data, so 2.28B tree rows + 608M open rows =
1.7× today's 1.70B; a query reads D's 8 root-to-leaf nodes plus the open table `vf ≤ D`, no closure anti-join).
At true density (every name in `[part-0, part-1)` of the full postings: 64.6M versions, 41.2M closures, 13.1M
names; tree 1.50 GB vs 1.35 GB, 1.11× bytes), 12 shard-number literals:

| date | Σ rows read current → tree | Σ cold (s) | Σ warm (s) |
|---|---|---|---|
| Oct 6 | 26.6M → 27.1M (×1.02) | 22.5 → 14.8 (×0.66) | 8.91 → 6.39 |
| Oct 1 | 26.6M → 27.1M (×1.02) | 22.3 → 14.8 (×0.66) | 8.96 → 6.40 |
| Aug 15 | 26.4M → 14.2M (×0.54) | 18.2 → 16.0 (×0.88) | 8.24 → 5.16 |

Rows read stay far above live-at-D (`5418` Oct 6: 4.0M read for 10,463 live): the floor is ~1.8 granules per
matching name in any 256-row-granule layout. The tree's gain is dropping the closures table and its hash join:
~1.5× cold and ~1.4× warm on recent dates, ~1.1× / 1.6× on old ones, for 1.6–1.8× rows, 1.1× bytes, and an upkeep
that rewrites ~4 rows per closed version (delete the open row, insert the canonical copies). Not built full-size.
(pyrmts's multi-scan encoder (b) reads plain interval rows with an overlap filter; this dyadic observation-axis
index is a candidate pyrmts feature.)

**Cost-weighted census, implemented but not built** (`ch-mega-catalog-build -w rows -W NAME_ROWS`): a literal's
weight is its names' postings rows through the scan (`{stem}_cost`, per name per scan: versions opened plus
closures recorded) plus `NAME_ROWS` per name, over every name seen by the scan (a superset of `span_live`'s, so a
non-member's on-demand read is bounded on every scan, past and future); membership is monotone. `serve-query`
reports it as `threshold_rows` + `name_rows`, which `/names` accepts. Tests: brute force, fresh, resumed, and the
bound itself (every non-member's read below the threshold); they fail without closures in the weight. Given the
cold findings above it bounds rows read (warm), not cold latency, so it is paused pending a decision.

## Precomputed catalogs day to day

Bucket answers (`(literal, bucket)` cells of the dated L1 catalogs) change a lot between consecutive scans, because a bucket total moves whenever anything under a busy literal changes:

| From → to | Literals in both | Cells unchanged | Nonzero cells unchanged | Literals entirely unchanged |
| --- | ---: | ---: | ---: | ---: |
| 10-04 → 10-05 | 71,182 | 74.4% | 53.5% | 46.8% |
| 10-05 → 10-06 | 48,657 | 77.3% | 63.1% | 67.2% |

Interval-coding cells would save about 4× on a 22 MB/day artifact: not worth it at bucket level. Deeper drill tiles (smaller scopes) should be far more stable and are the better candidate.

## Recommendation

Ship the consolidated store as the one source for name search over every scan, dropping per-scan name indexes, per-scan catalogs and per-scan scalar databases:

1. Daily: append the scan (`ch-ingest`, ~9–13 min), then `ch-mega-names-append` (O(changes): 23 s for the 126M-closure Oct 6), then `ch-mega-catalog-build -e DATE catalog` (one append: ~15 s at the v1 store's size, ~4–6 min at v2's, census-bound).
2. Serve registered literals of every scan from the consolidated catalog (`serve-query -K catalog`), and the rest — below the threshold, by the scan's own census — from the consolidated name index (`-P`) behind the bounded lane (name budget, deadline; a deadline now fails only its own request).
3. Keep the per-scan catalogs only where they already exist (Oct 4–6) until the consolidated catalog replaces them in serving.

Storage: one all-time name index (17 GB) replaces 9.5 GB per scan; the catalog for every scan is 24 MB served plus 1.44 GB of appender state. Both grow by a day's churn.

[clickhouse.md]: clickhouse.md
