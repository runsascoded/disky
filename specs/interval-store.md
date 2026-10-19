# Interval store: change intervals in place of per-scan path stores

**Status:** draft v9, 2026-10-15. Base work: on `cloud` (opt-in, `PATH_STORE=opt-in` + `ps=iv`).
- v1 (2026-10-07, unreviewed) recommended object-only intervals plus keyframes.
- v2 decided for objects and directories as one interval table, with no keyframes.
- v3 reports the prototype: every gcs scan built, verified and served from R2 on dev (§6). It also folds `last_read` into the version key, on that evidence (§2.2).
- v4: closed versions in dyadic time segments (§2.4), the owner-slice table and its reader (§2.1, §2.6), shared footer and group decodes in the reader (§2.6), generation `2026-10-09b` (§6).
- v5: the diff gap, in the reader alone (§2.5, §2.6, §6.2): one-request footers, footer groups read whole, one cached row group for every scan, lookups bounded by the other scan's version. A lens reads the user-first slice sort. Fixed: the lens's unread-region double count, and the opt-in isolate's shared miss memo. The gcs-scale parity sample over the slice sorts (§6.2).
- v6: the per-scan append, built (§2.7, `dt-cloud interval-store append`), the reader's base + runs (§2.7), and the first real runs (§6.3).
- v7: plain views at per-scan speed, in the reader alone (§2.5, §2.6, §6.4): state, footers, footer groups and decoded row groups held in the colo cache across isolates; a depth band reads `path`; a view or diff reads only the runs begun by its scans.
- v9 (2026-10-15): compaction, built and automatic on one machine (§2.7 "Compaction"): `interval-store add` folds the base and every run into a new generation every 2^L scans, and the site follows `interval-store/CURRENT.json` when `INTERVAL_STORE_GEN` is unset.

Nothing here deletes anything. §4 is a proposal for Ryan.

**Goal (Ryan, 2026-10-09):** *"I'm more concerned about parts of the stack where we're storing a mostly-redundant full scan's worth of data for every new scan that comes in, as opposed to leveraging the mostly-constant-over-time nature of our subject fleets."* A scan in which little changed should cost little: the store should grow with the fleet's changes, not with its size.

**Units.** A scan is identified by its id, `YYYY-MM-DD[THHMM]` (gcs has sub-daily scans now, e.g. `2026-10-09T1236`), and stamped with its epoch. Nothing assumes one scan per day. Deployment specifics (bucket, the scans the parity check samples) live in a profile JSON the deployment carries (gcs: `job/interval-store.json`), passed as `-P PATH` or `$INTERVAL_STORE_PROFILE`.

## 0. Where we are

Every gcs scan writes a full copy under `gs://oa-gcs-usage-dvx/listing/<scan>/`, kept forever:

| Artifact | Bytes per scan | What reads it |
|---|---:|---|
| path store `index/<gen>/path-index{,-bysize,-bysize-by-user}.parquet`, plus `.groups.{json,parquet}` and `attr.tsv` | 21.7 GB | the site: subtree/treemap, diff, series, filtered views' phase 2, age and read lenses, `/staged`. Footers in D1 for the latest 30 scans, `.groups.parquet` for older ones |
| `dir-cache/` | 19.1 GB | the next scan's attribution |
| raw listings `<bucket>/*.parquet` | 10.7 GB | `path-index` (the store is cut from them), sweep manifests |

- `listing/` holds 3.32 TB today (71 scans, 2026-07-30 → 10-09) and grows ~51 GB per scan: ~$66/month now, ~$370/month after a year of daily scans at GCS Standard.
- D1 holds ~218 MB of footer rows per path-store scan. That's why only 30 scans fit there.

## 1. Evidence before the prototype

`static-names/2026-10-08` (`specs/architecture/static-name-search.md`) ran pyrmts' gaps-and-islands kernel over every gcs scan's `path` sort:
- keyed by `(depth, path, usr)`;
- objects and directories, with every value a change column.

What it found:
- 1,153,480,980 versions for 70 scans, against ~780M rows for one scan. That's ~1.6 versions per key.
- Coalesced on `(size, n_files)`: 797,994,947 versions.
- Every scan's opened and closed versions equal ClickHouse's independent ingest.

The **per-scan append** is already solved (`specs/static-daily-append.md`, `static_append.py`):
- `append_intervals` over the open versions only;
- each scan's opens and close records as a run;
- runs merged on a binary counter (`pyrmts.runs`, min-`vt` combine);
- immutable manifests, ~$0.40 per scan.

## 2. Decisions

### 2.1 Rows: one table of objects *and* directories, one row per `(depth, path)`

**Directories are stored as intervals, beside objects.** v1 rebuilt directory totals at read time from keyframes plus signed object deltas, to avoid "multiplying churn by the ancestors touched". Measured (§6): over the store's 71 scans, directories hold 536M versions over 223M paths, 2.4 versions per directory. Ancestors are shared, so the fan-out is bounded by the directories that hold the changes, not by changes × depth. In exchange:
- a read at any scan is one range read of stored totals, as today: no keyframe, and no summing of every changed descendant's delta up the tree;
- the treemap's query stays a 3-sided range query over one sorted file (`path-store.md` §1.3), with a fourth side: `vf ≤ D < vt`;
- no scan is a full copy.

**Row identity is `(depth, path)`, one row per path.** A path's owner slices fold into the row (`us`, below). A path that is both an object and a prefix is one row whose `kind` is `dir`. Today's store keeps one row per `(path, usr)` slice. Folding the slices changes two things:
- **Thresholds are exact.** Today's `bysize` read thresholds *per slice* (`readSizeRects` tests `size ≥ thrAt(depth)` on each row). So a drawn directory counts only its slices over the threshold, and a directory whose slices are each under it goes missing. This is a live prod bug, measured in §7.
- **The user lens needs slices.** A lens reads U's slice of each path with every value. So the store has a second interval table, the owner slices (`build -S`, `sv/`): one row per `(depth, path, usr)` — the per-scan store's own rows, a v1 index's duplicates summed — with every value and `last_read` in the key, `usr` NULL where no owner is named. It is served in the per-scan store's three sorts:
  - `slices` `(depth, path, usr, vf)`;
  - `slices-bytotal` `(⌊log2 tot⌋ desc, path, usr, vf)`: keyed on the path's total, as the per-scan `bysize` is since `bysize-path-total.md`. `fold -S` splits each slice version where its path's total changes (`svt/`, the pieces checked to tile the slices);
  - `slices-bysize-user` `(usr, ⌊log2 size⌋ desc, path, vf)`.
- **Only with an owner axis.** A generation is *sliced* iff some scan in it names an owner (`usr` neither NULL nor '' on some row; a missing `usr` column names none). `build_range` counts the versions naming one (its digest's `owners`); the base build records the answer in `scans.json` as `"slices": true | false` (the local build after its versions stage, the staged `cut` from every range's digest, `gen_sliced`), and `stats.json` carries it too. An unsliced generation (cw-s3, r2.rbw.sh: no owner axis) builds no `sv/`, `svt/`, `sv-digest/` or slice sort, in the base or its runs (`build -S`, `fold -S` and the slice sorts' `cut` are no-ops there; its runs carry `pvl` and `path`/`bysize` alone): each slice sort would be about the size of `path` again. Appending a scan that names an owner to an unsliced generation is refused (`add` before appending anything, the staged `ranges` task too): its owners need a new base generation. A generation from before the flag reads as sliced. No override.

  Their group indexes carry `u_min`/`u_max`. A slice sort *is* the per-scan sort as intervals, so the per-scan reader's owner logic (lens rects, `ownerOk`, owner totals) runs on it unchanged.
- **Slices barely add versions.** Most paths are one slice: 1,156,339,350 slice versions against 1,156,324,516 folded path versions (609.2M open in both).

### 2.2 Version key

| Column(s) | Role | Why |
|---|---|---|
| `kind`, `size`, `n_files`, `n_children`, `n_desc`, `mtime`, `wb`, `c2..c4`, `us`, `last_read` | **change** | a different value is a different tile |
| `dr` = `round(wts / wb)`, the size-weighted mean stamp to the second | **change** | the age lens's `d` |
| `wts` = Σ `mtime_mean · mtime_w` | **carried** (`first`) | the exact float sum drifts in its last bits between scans when nothing changed |

`us` holds a path's owner slices as one string:
- `''` when no owner is named;
- the owner's name when the path is exactly one row and it is named;
- otherwise JSON `[["usr", bytes], …]`, every named owner including zero-byte ones.

**`last_read` is in the key (decided on measurement).** v2 kept read days in their own interval table, to keep read churn out of the versions:
- **The separate table is small:** 40.0M read-day versions against 1.118B path versions.
- **Its lookup is slow:** a view's ~1–2K tiles are scattered across hundreds of the read sort's groups. On dev that lookup cost 1.8–8.7 s per view.
- **Folding is cheap:** folding the read days into the path versions (`interval-store fold`: split each version where its read day changes) costs **+3.4%** (1,118,487,204 → 1,156,324,516 versions).
- **It removes the second read entirely.**

Carrying `last_read` with pyrmts' `carried=` instead would make a mid-run scan's read day wrong.

### 2.3 Encoding

- Each version holds `[vf, vt)` in epoch seconds of the scan. `vt` is `OPEN` (2106-01-01) while the version is open.
- A version opens at its first scan and closes at the first scan after its run. A path absent from a scan closes; a path that comes back opens a new version.
- `scans.json` lists the scans in order: id, source, epoch. A missing scan (gcs 2026-08-10) has no stamp and is never asked for.
- A read at `D` takes the rows with `vf ≤ D < vt`.

### 2.4 Layout: sorted files, open versions then dyadic time segments

| File | Order | Serves |
|---|---|---|
| `served/path.parquet` | `(depth, path, vf)` | P's own row, children by name, small subtrees, the diff's point lookups, change scans |
| `served/bysize.parquet` | `(⌊log2 size⌋ desc, path, vf)`, size 0 last | every thresholded subtree read (the treemap at any P), top-K |

- **Segments.** Each file holds the versions still open at the generation's last scan (segment 0), then the closed ones in **dyadic time segments** (`seg_sql`). A closed version live at scans `i..j` (by index in `scans.json`) goes to the smallest aligned block of scans holding both: level `k = bit_length(i ^ j)`, block `i >> k`. Each segment is sorted as above and cut at its boundary, so no row group mixes segments.
  - A read at the newest scan prunes every closed group by `vt_max ≤ D`, and decodes what a per-scan copy would.
  - A historical read at D touches one block per level (⌈log2 n⌉ + 1); every other block's groups fail `vf_min ≤ D < vt_max`, because a block's versions live only inside it. Within a block a version crosses the block's midpoint, so it is live at most of the block's scans.
  - Measured on the 71 gcs scans (`path`): open 609.2M rows; L0 70 blocks / 28.4M; L1 34 / 1.8M; L2 17 / 3.9M; L3 9 / 8.0M; L4 4 / 13.8M; L5 2 / 85.9M; L6 1 / 219.6M (the v1 era, closed at the 09-30 switch); L7 1 / 185.7M (the sweeps, which straddle scan 64).
  - The cost: `path` 16.79 → 19.24 GB, `bysize` 14.82 → 16.75 GB (+14%; a path's versions no longer sit together in one segment).
- **8K-row groups,** zstd, dictionary on `kind`/`us`.
- **No per-scan bysize copy.** A version's bucket is fixed, because its size is. So one bysize file serves every scan: the same predicate, with liveness added.
- **Columns:** `depth, path, vf, vt, kind, size, n_files, n_children, n_desc, mtime, wts, wb, c2, c3, c4, us, last_read`. `-1` marks a value a v1 scan didn't have.

### 2.5 The group index: `.groups.parquet` on R2, no D1

Beside each sorted file is `<sort>.groups.parquet`: one row per row group, with
- exact bounds (`d`, `p`, `b`, `vf`, `vt` min and max);
- its segment and row span;
- the compact column-chunk metadata (`rg_json`).

This is the site's cold-footer format with four time columns added, read in `pq` mode. There is one index per sort per generation, not one per scan (~141K groups, ~21 MB), and nothing goes in D1. A Worker reads a footer group's bounds columns to plan, and `rg_json` only for the groups it selects (`readFooterJson`). The per-scan cold tier benefits from that change too.
- An interval store's footer serves every scan, so its decoded footer groups are cached per isolate without the scan in the key, and a decode in flight is shared (`footerOnce`).
- Decoded footer groups have an LRU of their own (12 MB). In the row groups' LRU they evicted the groups a warm read wanted.
- **The footer** (gcs `2026-10-09b`: 527 KB, 276 footer groups of 512 tier groups) is opened once per isolate for every scan (`ivFooter`), in one suffix read (`ByteStore.tail`: the last 1 MB and the object's size), started beside `scans.json`. Opening a sort also opens its sibling's (`path` ↔ `bysize`, `slices` ↔ `slices-bytotal`). A size probe, a tail and a head read in series, per side of a diff, were 3–6 sequential requests.
- **A footer group is read whole** (bounds and `rg_json`, ~95 KB), once: planning a read and fetching its groups' metadata is one round trip, not two.
- **Across isolates, through the colo cache** (v7; all immutable per generation and revision, so keyed by the object key, `?rev=` included). A Worker decodes parquet several times slower than a laptop, and a new isolate used to open everything again:
  - the state (`scans.json` and the newest manifest, `ivStateDoc`) for `IV_STATE_TTL` (a minute): a new isolate skips three R2 operations in series (`get`, `list`, `get`);
  - each footer, compact (`openIvFooter`: its footer groups' byte ranges and bounds, ~40 KB of JSON against 527 KB of thrift); the thrift is parsed only to decode a footer group the colo doesn't hold;
  - each footer group, decoded (`ivFooterDoc`: its columns as JSON);
  - each **row group**, decoded (`coloGroup`): columnar JSON, gzipped (~2 MB, ~0.3 MB stored), put by the first isolate to decode it, without the read waiting. Measured on a laptop, an 8K-row group of `bysize` decodes in 25 ms, 20 of them zstd (`fzstd`, pure JS: a Worker compiles no wasm at runtime); its JSON parses in 2.3 ms. A group's ints are exact as JSON numbers (a value past 2^53, a non-finite float or a byte array leaves the group uncached, `groupDoc`).
  - The isolate holds a group as those columns (`IvGroup`, v8), not as shaped rows: a thresholded read shapes only the rows its cheap test on `depth`, `path` and `size` passes (`Pre`, `IvGroup.pick`). Shaping all 8K rows (`groupRows` + `toRow`) cost more than the parse, for the few rows a drilled view keeps.

### 2.6 Reads

Every read is today's `index.ts` plan, with one more conjunct on each group (`vf_min ≤ D < vt_max`) and on each row (`vf ≤ D < vt`):
- **Subtree / treemap at (P, D):** P's row, then `bysize` (`readSizeRects`). Planning `path` too is skipped for subtrees over `IV_PATH_PLAN_ROWS` (256K rows): on R2 footers, planning `path` decodes most footer groups for a read it never picks.
  - **A group straddling size buckets** has the whole keyspace as its path range (its top bucket's rows end it, its bottom bucket's begin it), so a drilled prefix selected it whatever it held. Its neighbours bound it (`sizeNeighbours`, v8, from the footer's `b_min` and `seg`): its top bucket's rows follow the previous group's, at or past that group's `p_max` when it is all of that bucket; its bottom bucket's rows precede the next group's, at most its `p_min` when it is all of that bucket; a bucket whose largest size is under the read's floor holds nothing it keeps. Unscoped `bysize` and `slices-bytotal` reads only (the user-first sorts lead with `usr`).
  - **A depth band** (`depth=N`, a capped diff's views) plans `path` first and reads it when it holds at most `IV_BAND_ROWS` (64K rows); past that (a flat directory's children) it plans `bysize` too and reads the one holding fewer rows. `path` holds a depth's rows under P together; `bysize` reads a group per size bucket of every segment (`marin-us-central2` at depth 1, 10-04: 8 groups against 19).
- **Runs a request reads** (`asOfScans`, v7): a view names its scan, a diff its two, and their handles leave out the runs begun after the newest of them. Those runs' rows are versions opened later (live at none of the named scans) and close records of versions still live at every named scan they were live at, so no kept row changes; only `vt` would (a version a later run closes reads as open), and a diff's bounded lookups compare only the scans it named. Reads that don't name their scans (series, filters, `openIndex` alone) read every run, as before.
- **Diff (D1, D2):** both sides are as-of reads at one threshold; the walk's lookups are `path` point reads, one plan per side per level (`readAsks`).
  - Both sides read the same files. A row group is cached once, with every version, keyed without the scan (`ivGroupKey`); a read keeps its scan's live versions (`cachedGroup`). One being fetched for one side is waited for by the other (`groupsInFlight`). The same holds for a view and its walk. Cached per scan, a root diff's ~20 groups were ~40 entries, past the row-group LRU.
  - **Lookups are bounded by version.** A name one side lacks is looked up on that side, and the other side's row says which version can answer: a path's versions never overlap. If the other scan's version is live at this scan, it is the answer, with no read. Otherwise the version wanted closed by the time the other opened (`Ask.vtLe`), or opened once it closed (`vfGe`). Groups whose `vt_min` / `vf_max` rule that out are never read, so the open segment is skipped for a name added since. This holds only for unscoped, unfiltered diffs, whose rows are one version per path.
  - Version boundaries also define a **change-only read**: the paths under P that differ between the two scans are exactly the versions with `vf` or `vt` in `(D1, D2]`. The `path` sort's layout can't prune by time, though: its open segment's `vf` ranges span every scan. Measured over 52 diffs, that read selects a median of 2,818 groups (117 MB), against 260 for the two as-of reads; at the root, 38,803 against 359.
  - The change list that is cheap is the per-scan runs (§2.7): each run *is* one scan's changes. So a change-only diff reads the runs for the scans in `(D1, D2]`, and needs a `vf`-ordered cut only for spans older than the runs.
  - A change-only diff isn't the same diff, though. An expanded node's `(other)` is its total less every named child on each side, unchanged children included, and the change list doesn't name those. So it either keeps the as-of reads for the named children or reports `(other)` as the unchanged remainder too. That is a product decision, and the runs exist only once the job appends (§2.7). The bounded lookups above are its reader-side half, available now.
- **Series:** P's versions *are* its history, so one `path` point read can replace one read per scan. Not wired in.
- **Filtered views:** phase 1 searches the scan's own search sidecars; phase 2 reads the interval store.
- **Lenses:** age (`d`) and read recency (`a`) come from the row.
- **User lens, owner pools, owner totals, owner prefixes, class scopes:** the slice sorts (`slices(env)` maps `path`/`bysize`/`bysize-user` to `slices`/`slices-bytotal`/`slices-bysize-user`).
  - A lens view's assigned regions take their totals from the folded `bysize` (one exact row per path). A path-first read of the slices selects a run per depth per dyadic segment, and a big user's root regions went past `decodeSpans`' cap. A held date's v1 `user` and coarse sorts are refused, so an owner read of a held date never mixes in per-scan rows. A generation found without its slice sorts still falls back to per-scan stores.
  - An unsliced generation (`scans.json` `"slices": false`, held in the state with the scans) has no owners: a sliced read there reads the folded sorts (`IV_UNSLICED_SORTS`: `path`, `bysize`, and `bysize` for `bysize-user`), a path's one slice being its whole row with `usr` NULL — what the per-scan reader reads of a scan without `usr`. `intervalUnowned.test.ts`: over `g7` (`gen.py g7`: `v2`, `v2-search`, `v2`, no `usr` column, built by `interval_local.build_gen`), every lens view, owner pool, root total and history, lens or pool diff and owner fold equals the per-scan reader's, reading only `path`/`bysize`.
  - A lens view reads U's own rows from the user-first `slices-bysize-user`, as a per-scan lens reads `bysize-user`, also for the big subtrees that plan the size sort alone (`IV_PATH_PLAN_ROWS`). The path-first `slices-bytotal` mixes users in every group: a big user's root there selected most groups over the threshold, and michael-ryan's root on 2 of 5 sampled v1 scans was refused as too wide.
  - `intervalSlices.test.ts` also covers assigned regions: 26 of a lens's regions, 2 unread. They are drawn from the folded `bysize`, each counted once.
  - `intervalSlices.test.ts`: over three scans built from the per-scan fixtures (`v2-slices`, `v2-lens`, `v2-slices`), every lens view, owner pool, root total and lens or pool diff equals the per-scan reader's, with no per-scan read. One deliberate difference: a path that is both an object and a prefix is `dir` (the per-scan reader takes whichever row it merged last).

### 2.7 Per-scan append and compaction (`static_append`'s tiering)

**Built** (`cloud/src/dt_cloud/interval_append.py`): `dt-cloud interval-store append [-c] [-M] [-n] [-w SECS] SCAN_ID` and `dt-cloud interval-store merge [-n] [-w SECS]`, profile-driven (the interval profile's `append` section, gcs: `job/interval-store.json`; `$INTERVAL_STORE_PROFILE` or `-P`; overrides `$INTERVAL_STORE_SRC`, `$INTERVAL_STORE_IMAGE`, `$R2_ENDPOINT`). Its own command, not a stage of the static `runs add` chain: the two stores have their own generations, bases, manifests and compaction schedules, and a stage there would tie the store's append to the name index's (a failure, a timeout, a catch-up). It runs on the same runner as that chain (`append_runner`, since 2026-10-10; specs/done/index-landscape.md recommendation 1, specs/done/static-append.md "One runner for both stores"): `pending_scans` (strict scan-id order; exit 3 when SCAN_ID is not published or an earlier one is pending, `-c` catches up), stage skipping, `BatchRunner` (polls to a 30 s cap), `job_spec` (labels `purpose=interval-store`, cost label `component=interval-store`), the deferred carries (`plan_carries`, `merge_pending`, revisions, the lease), the R2 copy (`r2_publish`, as the R2 account from Secret Manager), prune. This module is the store's stage list and its merged-run build (`interval_append.carry`).

For each scan D, each stage skipped when its output exists (a rerun resumes):

1. **prepare** (local): pin D's newest `path` sort (GCS generation, size, crc32c, source format) as `deltas/<D>/scans.json`, refusing any scan but the next.
2. **ranges** (Batch, `append_tasks` tasks over the base's 256 key ranges, assigned longest first to the least-loaded task by the previous scan's open rows per range — its `ranges/r####.json` counts — or interleaved where there are none, e.g. the first run; every task computes the same plan): per range, `append_intervals` over the open versions (the previous scan's state in the scratch bucket, or for the first run the base's range files) and D's rows, for the three version tables the store serves:
   - `pvl` (one row per path, `last_read` in the key: the folded sorts);
   - `sv` (owner slices: `slices`, `slices-bysize-user`);
   - `svt` (slices split where their path's version changes, with its total: `slices-bytotal`).

   Each writes its delta `deltas/<D>/<t>/r####.parquet` (opened versions `op` 1, close records `op` −1) and the next open state `state/<D>/<t>/` (scratch); `ranges/r####.json` last. A range raises unless its open state is D's rows exactly (count and Σ hash).
3. **publish** (Batch, one task): the run's five served sorts (`deltas/<D>/served/{path,bysize,slices,slices-bytotal,slices-bysize-user}.parquet` + `.groups.parquet`, open versions then close records), its `meta.json`; then `manifests/<D>.json` (the base's scans, the newest earlier manifest's runs — a scan's or a revision — plus D's at level 0, each run scan's epoch), written once and only after every file of every run it lists exists (`publish_run`). **No carries** (until 2026-10-10 publish merged inline, pairwise, a carry to level j merging about 2^j scans one level at a time inside the job: its level-1 publish took 182 s, an L3 was due ~10-17 and an L4 ~10-25 against the 60 min job cap).
4. **r2** (as the R2 account): each listed run's served files, checked on R2, then the manifest. The publish task's second runnable (one provisioning; runnables run in order and stop at a failure, so the copy follows a written manifest; a retried task's publish skips its manifest, `-s`, and the copy skips objects already there). Its own job when the manifest already exists (a rerun), or when the profile's R2 account is another service account.
5. **prune** (local): every earlier scan's `state/` in the scratch bucket, once D's state is complete and published; 8 deletes at a time. Then the runs a carry folded, on GCS (below).

Then, once after the scans (not per scan of a catch-up), the **merge stage** (deferred carries, as the static index's: specs/done/static-append.md "Deferred carries"), unless `-M`:
- **Plan** (`plan_carries`): the binary counter replayed over the newest manifest's runs; carries that chain fold into **one N-way merge** (`[L1, L0, L0]` → one L2 of three inputs); none reaches the profile's compaction level L (`append.compact_level` in the profile JSON, `$INTERVAL_STORE_COMPACT_LEVEL` over it; gcs: 5, so a compaction is due after ~32 scans, and the stage then logs it; `none` / `null`: unbounded, never due — specs/done/static-append.md "Unbounded cascade"). The runner passes it to the job as `carry -L <L|none>`.
- **Merge** (a Batch job, `python -m dt_cloud.interval_append carry … -m /gcs/BUCKET`, one task on the profile's machine, Batch's 4 h cap), one planned run at a time into its own dir `deltas/<first>_<last>/` (`build_merged_run`): per key range each table's deltas merged (`merge_range`: identity `(depth, path[, usr], vf)`, `vt` min, `op` max), its five served sorts cut from them, `meta.json` (the inputs' stamps) uploaded last. A dir holding keys this merge doesn't write is refused; a whole one (its `meta.json` naming the planned scans, every reader file there) is reused.
- **Revision**: `manifests/<id>.m<NNN>.json` of the newest manifest, the inputs replaced by the merged run (same `scans`, `date`, `stamps`; `rev`, `revises`), written once after every file of every run it lists exists; rebased onto a scan's manifest that lands meanwhile, and re-checked newest after writing. One merger per generation: the lease `gs://<scratch>/interval-store/<gen>/merge.lease.json`, stale after 4 h.
- The runner watches the job `-w` s (default 0: submit and go); if it ended and the newest manifest is a revision, the R2 job for it (`r2 -m <id>.mNNN`: its runs, checked, then it). **Non-fatal**: a failure or an unfinished wait is logged, the append exits 0, the store is as it was (every listed run whole) and the next run plans again. A merge not waited on publishes on GCS; the next scan's manifest builds on it, so that scan's R2 copy carries the merged run. `interval-store merge` is the stage alone (exit 1 on a failure).
- Readers: `ivManifest` (`index.ts`) takes the newest name by `manifests.ts` `newestManifest` (`<scan id>[.mNNN].json` only, code-point order: a revision after its scan's manifest, before every later scan's), so a revision is served once written; a reader holding the earlier state reads runs that still exist (within the grace, below).
- **Folded runs are pruned** (since 2026-10-14; before, nothing deleted the `deltas/<first>_<last>/` dirs a carry replaced, and cw leaked 70% of `deltas/`: 35 GB on GCS, 31 GB on R2). Rule, **time-based grace**: after a manifest is published to a store, every run dir under `deltas/` is deleted from that store when no manifest within the grace lists it (the newest manifest M, and each manifest superseded less than `GRACE_S` = 3600 s ago, its superseded time the next manifest's write time from the store listing: GCS `updated`, R2 `LastModified`, a local or fsspec store's mtime; CLI `-G`, never below 600 s) and its scan span lies inside one of M's runs (a run some carry folded). Readers cache the newest manifest for 5 min, so however many manifests land at once (a catch-up `add`, a carry right after a publish), a reader holding any of them never 404s. Never deleted: a run a manifest within the grace lists; the base generation's files (outside `deltas/`); any manifest; a dir no manifest lists that isn't inside one of M's runs (an append in flight, past M's scans, or a merge in flight, spanning several of M's runs until its revision lists it). One generic pure planner shared by both stores (`append_runner.plan_prune`: the store's keys, its manifests as `(key, mtime)`, the runs of the 2–3 manifests in the window and `now` → the keys to delete; `grace_manifests` picks the window) and its executor (`prune_folded`, logging each run's objects and bytes). Where: GCS by the chain's prune stage (after publish + R2) and by the merge job after its revisions (`carry`; `-n` reports instead); R2 by the R2 job after copying its manifest, planned by **R2's own manifests** (which may lag GCS's); alone, `python -m dt_cloud.interval_append prune [-r] [-n]`. Constraint: on GCS, where scans are appended, a folded run keeps `ranges/` and `scans.json` (`FOLDED_RETAIN`: the next scan's task balancing reads the previous scan's `ranges/` docs); on R2 nothing. The interval store's appends read no other file of an earlier run (the open state lives in the scratch bucket), so nothing else of a folded run is needed later. A rerun of an appended scan now copies the newest manifest (a revision, when one landed), not the scan's own, whose folded runs may be gone; `r2_publish` refuses a manifest listing a run with no files on GCS.

**Same answers as the pairwise carries, byte for byte** (`test_interval_append.py`, fixtures as below): over the four appended scans, publish then merge after each: the newest manifest's stack after every scan is the old pairwise path's (`[L0]`, revision `[L1]`, `[L1, L0]`, revision `[L2]` — the L2 one merge of three runs), every merged run's deltas and served sorts are byte-identical to the pairwise ones, and each revision is the old publish's manifest but for `rev` / `revises`; a backlog of four runs is one 4-way merge to the same bytes; every view at every scan over the last stack is the per-scan reference's. TS: `fixtures/iv/interval-store/g6` (`gen.py tiered`, the deferred path: `2026-08-05.json` unmerged, `2026-08-05.m001.json` the merged run, asserted byte-identical to `g4`'s) serves every view and diff as `g1`, its revision taken over the scan's manifest, the scan's own read while the revision is hidden (`intervalStore.test.ts`); `manifests.test.ts` pins the name filter. Also tested as the static store's are: a merge killed while building, mid-upload and before its revision (servable, lease released, resumed byte for byte); a foreign key in the merged dir; a revision refused while a run lacks a file; a publish landing mid-merge (rebased); the lease held and stale; publish refusing a rewrite, an older scan and a run without its files; prune leaving the lease and revisions alone; the merge stage waited / detached / failing / skipped (`-M`), `merge` exiting 1, the compaction level left to a compaction (per profile, `test_compact_level.py`: plans at L = 3 and 5 and unbounded, `-L` on both stores' merge jobs, the env overrides and validation). Mutations, each killed: pairwise-only folding, carrying into the compaction level, no files check on a revision or on publish, no newest re-check, the lease ignored or not released on failure, no reuse of a whole dir, a fatal merge stage, publish carrying inline, unfiltered manifest names (Python and TS), R2's manifest before the check, merged stamps dropped, and (TS) `ivManifest` ignoring revisions.

**Exact, version for version.** Base + runs equals a full `build` + `fold` + `build -S` + `fold -S` through D, row for row, with each run alone and with runs merged (`test_interval_append.py`). Two things make that so:
- `wts` is carried as the build carries it: a version opened by a read-day change alone keeps its path version's `wts` (`fold` takes it from the path version), and a slice piece opened by its path's change alone keeps its slice version's (`fold -S`).
- The open states carry each path's version start (`pvf`): a slice piece splits where its path's version changes, as `fold -S` intersects slices with path versions.

**The reader** (`index.ts`): `openInterval` reads the newest manifest (R2 `list`, held a minute) and opens each listed run's sort beside the base (`IndexHandle.runs`). Every read plans each tier — a run's group can matter at D once a version in it opened by then (`vf_min ≤ D`), live or not, since its close records end versions an older tier still holds open — and combines a version's rows across tiers (the smallest `vt`) before testing liveness (`combineLive`; `interval_read.Store` likewise). Every run is read at every scan, also before its first: liveness there doesn't need it, but a row's `vt` does, and a diff's bounded lookups trust it (§2.6). A view or diff names its scans and leaves out the runs begun after them (`asOfScans`, §2.6). A broken run (its footer or data missing at open, or a read that fails) is cut out for 30 s: its scans and every later run's read per-scan, the others as before, and the request is retried around it (`ivRetry` in `/api/subtree`, `/api/diff`, `/api/series`), never a failed request.

**Compaction** (built 2026-10-15, `cloud/src/dt_cloud/interval_compact.py`; one machine, `interval-store add`). Carries stop below the compaction level L, so without compaction the runs at level L − 1 pile up, one every 2^(L−1) scans, and every read plans and decodes each of them: read cost grew with the scans, unbounded. Compaction bounds it.
- **Why a new generation, not an unbounded cascade.** An unbounded binary counter (`-C none`, which stays available) keeps at most ⌈log2 N⌉ runs, but a run's closed versions are one segment (`seg_sql` without stamps), so a merged run of 2^k scans has no dyadic time layout: a historical read inside its span decodes its closed rows for every scan, and its open segment holds every version opened since the base, which a newest-scan read decodes beside the base's. Folding into a new base puts every closed version back into dyadic segments (§2.4: one block per level at any scan) and leaves no runs: between compactions at most L runs (levels L−1 … 0), so a read touches the base plus ≤ L runs, a constant; a historical read ⌈log2 N⌉ + 1 base blocks.
- **When.** After an `add`'s appends, carries and uploads: once the newest manifest's runs, carried, hold two runs at level L − 1 (`append_runner.compaction_due`, shared with the staged runner's "level L is due" log), i.e. every 2^L scans past the base. L is `add -C` (or `$INTERVAL_STORE_COMPACT_LEVEL`), default 5 (`static_profile.COMPACT_LEVEL`): r2.rbw.sh's daily ingest compacts about monthly. `-C none`: unbounded carries, never compacted. `-N` skips it with the carries. A compaction interrupted is redone by the next `add`, nothing pending or not: the half-written generation (no successor record names it) is deleted and rewritten.
- **What** (`compact_gen`): the next name in the lineage (`next_gen`: `G` → `G.c001` → `G.c002`), its range files per key range of the old `ranges.json` (kept) — `pvl/` (and with the owner axis `sv/`, `svt/`) are the base's range file plus every run's deltas, one row per version (`IDENT`) with the smallest `vt` (the reader's `combineLive`, kept whole instead of filtered to the open ones); `pv/` and `rd/` are the islands of those `pvl` pieces (contiguous `[vf, vt)` with equal change columns, `dr` as the build derives it, resp. equal `last_read` ≥ 0) — then `interval_store.cut_gen` (the build's own cut: every sort, dyadic segments over every scan's stamp, `stats.json`), `scans.json` (the base's scans, then each run scan's entry from its `deltas/<id>/scans.json`, which a folded run keeps: `FOLDED_RETAIN`) and `compaction.json` (`from`, the manifest and runs folded). No `digest/` (the appends checked every scan's reconstruction).
- **Published** (`interval_local.compact`): the new generation's files (data, then `scans.json`), then `G/successor.json` (`{"gen": new}`), then `interval-store/CURRENT.json` (`{"gen": new}`, one object written whole) when it named G or nothing. `build` writes `CURRENT.json` when the store has none.
- **Found.** `add` without `-g` appends to `CURRENT.json`'s generation, else to the store's only one (adopted: the pointer written; several and no pointer is refused); `-g G` follows G's successor records to the newest (and a pointer left on a compacted generation, a compaction stopped between its two writes, moves to the successor). `interval-store current -o STORE [-s GEN]` prints (sets) it. The site (`index.ts` `ivGen`): `INTERVAL_STORE_GEN` set is a pin, as before; unset, `withPathStore` (`/api/subtree`, `/api/diff`, `/api/series`, a union's members, the digest image) and `/api/health` resolve `CURRENT.json` once per request, held `IV_STATE_TTL` (a minute) per isolate and in the colo cache, so a compaction is served within about two minutes with no config change; no pointer reads per-scan stores. Reads outside `withPathStore` (the owner pages) take only a pin. Every cache key carries the generation (`pathGens`, `pathStoreKey`), so the new one starts cold; its answers are the old one's.
- **Pruned** (`prune_predecessors`, every `add`): each generation the current one was compacted from (the `compaction.json` chain, while unpruned) is deleted from the store once its successor's `compaction.json` is `-G` old (the folded runs' grace, default 3600 s), but for its `successor.json`; never while `CURRENT.json` names it. A deployment that pins `INTERVAL_STORE_GEN` must not compact (r2.rbw.sh's workflow passes `-C none` while `site/wrangler.r2.toml` pins): its pinned generation would leave the store an hour after its successor.
- **Not built:** compaction in the staged (Batch) chain (`interval-store append` / `merge`): its merge stage still only logs a due compaction, and gcs's runs keep piling up at L − 1. The fold is the same per-range work (`compact_range`), a Batch task per range plus a `cut`; gcs pins its generation, so it would also need its own pointer flip.

**Same answers as a fresh build** (`cloud/tests/test_interval_compact.py`, owned and unowned scans): two scans built, eight appended one `add` at a time at L = 2, so the 4th and 8th compact (`g` → `g.c001` → `g.c002`); each compacted generation's served sorts, group indexes and `scans.json` equal a fresh `interval-store build` over the same scans byte for byte, and every range file (`pv`, `rd`, `pvl`, `sv`, `svt`) row for row; every directory's view at every scan over `g.c002` is the per-scan reference's. Also: the pointer and successor records after each; an `add -g g` follows them (and changes nothing with nothing pending); compacted generations kept within the grace and deleted past it but for `successor.json`; an interrupted compaction redone byte for byte with the stale keys gone; `-C none` never compacts; `build` points an empty store only; a sole generation adopted, two refused; `current`; a pointer left on a compacted generation moved; a `memory://` store catching up four scans in one `add` compacts to the local store's bytes and prunes its predecessor. Mutations killed: `rd` islands over unread rows, `pv` islands without `dr`. TS (`intervalStore.test.ts`): unpinned, `withPathStore` resolves the pointer and serves the fixture as the pin does; a pin wins without reading it; a moved pointer is served once the held one is `IV_STATE_TTL` old; no pointer reads per-scan; a request not opted in never reads it.

Measured size of a scan's delta (§6): ~2–3M opens plus ~1–2M close records. That's ~4M rows, ~0.12 GB in both sorts, against 21.7 GB for a per-scan path store. A real run: §6.3.

### 2.8 Any store, and one machine (`blobstore`, `interval_local`)

**The store seam.** Every interval-store read, write and list goes through `blobstore.Store`: a URL root (`gs://`, `r2://`, `s3://`; `r2://`'s endpoint from `DISK_TREE_R2_ENDPOINT_URL` or `buckets.yml`) or a local dir, keys relative to it as the readers' `INDEX_R2` keys are. A profile's `bucket` (and `append.scratch`) may be a URL; a bare bucket name still means `gs://<name>` (`store_url`), so gcs's `job/interval-store.json` is unchanged. `blobstore.run_store` is `append_runner`'s `RunStore` over it (`GcsRunStore` on a whole GCS bucket). The staged commands take the store as `-b` (`build-ranges`, `fold`, `cut`, `verify`, `runs ranges|publish|carry`); DuckDB reads their range files from `-m` (a mount) or the store's URL. When the store *is* the serving R2 bucket (`r2://$R2_BUCKET`), `r2` and `r2-copy` are no-ops (`blobstore.serves`).

What stays GCS-shaped: the staged chain (`interval-store append` / `merge`) submits Batch jobs whose tasks mount the store as a GCS volume, so its profile's `bucket` and `scratch` must be whole GCS buckets (`load_config` refuses others and points at `add`); the GCS → R2 copy (`r2`, `r2-copy`) copies from GCS only.

**One machine** (`dt-cloud interval-store build` / `add`, `interval_local.py`, the model being `static_local.py`'s `static-names build` / `add`): the staged path's stages in-process, in order, over a work dir (`-w`), for a deployment whose scans one machine handles in minutes (r2.rbw.sh's daily GitHub Actions ingest; cw-s3, whose store is R2 with no GCS copy, once one machine is sized for its scans: each stage reports its peaks).
- `build -g GEN -L LAYOUT -s SCANS -o STORE [-k RANGES] [-r RG_ROWS] [-t THROUGH]`: ranges (`plan_ranges`), `build_range` + `fold_range`, `build_slices_range` + `slice_totals_range` (every scan's reconstruction checked, or it fails; the slices only when some scan names an owner, `"slices"` in `scans.json`), the served sorts (every `SORTS` entry, the slice sorts only when sliced, dyadic segments, `stats.json`), then the generation copied into `STORE/interval-store/GEN/`, `scans.json` last. Never over an existing generation.
- `add -g GEN -L LAYOUT -s SCANS -o STORE [-C LEVEL] [-N]`: every scan after the generation's newest, oldest first, as a run (`append_range` per key range, `cut_run`, `meta.json`, `publish_run`), then the due carries (`merge_pending` with `interval_append.carry`). The open versions are refolded from the base's range files and the newest manifest's runs' deltas (`open_state_sql`: one row per version, the smallest `vt`, those open), so no `state/` is kept between runs; a run is byte for byte the staged chain's.
- A URL store's generation is mirrored into the work dir (but the base's `served/`, `rd/` and digests, which no append reads); new files go up data first, `meta.json`s next, the base's `scans.json` (on a build), manifests last. A pending scan's run already on the store (an interrupted `add`, no manifest) is redone, its keys overwritten and the rest deleted.
- Each build stage and each appended scan reports its seconds, peak work-disk GiB and peak RSS GiB (`static_local.DiskPeak`, `rss_peak_gb`).
- The site reads the result as it reads gcs's: `PATH_STORE` (`opt-in`, then `intervals`) and the `INDEX_R2` binding on the store's bucket (`scans.json`, `served/`, `manifests/`, each listed run's `served/`), the generation `CURRENT.json`'s (or `INTERVAL_STORE_GEN=<gen>`, a pin; §2.7 "Compaction").
- `test_interval_local.py`: the build's range files and served sorts are the staged stages' over the same ranges, byte for byte; each run is the staged chain's append (deltas and served sorts, byte for byte), the refolded state is its `state/`; base + runs (merged or not) is a full rebuild version for version, and every directory's view at every scan is the per-scan reference's; an add with nothing pending changes nothing; an interrupted run is redone with its stale keys gone; a `memory://` store gets the local store's files. Over scans naming no owner (v1 `usr` NULL, v2 without the column): build + add write no slice file anywhere and `"slices": false`, base + runs is a full rebuild of `pvl` and every view the per-scan reference's, the staged commands write the local build's files byte for byte (slice stages no-ops), and a scan naming owners is refused (exact message) with the store untouched.

## 3. Not built

- The change-only diff (§2.6: needs the per-scan runs, and a decision on `(other)`), and the one-read series.
- **A floor on the dyadic level.** A historical root still reads one group per small block (L0–L3: ~4 groups of 8K rows for a few rows over the threshold). Putting levels below 4 into L4 would cut that to one, ~30% of a historical diff's groups (§6.2). Deferred to a compaction (§2.7), which rewrites the closed segments anyway (built on one machine; the floor itself is not); a standalone re-cut is ~$5 of GCS → R2 egress for the folded sorts.
- **A lens's unread ancestors' own slices.** An ancestor of U's assigned regions whose own slice is under the threshold is valued from its bands alone, the lens's lower bound (`OwnerLens.value`). On v1 scans per-scan reads a coarse user tier (16 MiB floor) that holds that slice: ahmed-ahmed's `marin-us-east5/tokenized` is 120 MB short on the store (§6.2). A point read of U's slice at those few paths would close it.
- Shrinking close records. Today a close record is the full row. Identity + `vt` + `size` would do; a sweep scan closes 58–126M versions.
- The append in the gcs job (a patch for `job/run.sh`: `specs/done/interval-store-append-gcs.md`), cw's ingest step (`interval-store add`, §2.8), compaction in the staged chain (§2.7; built on one machine), and rebuilding a lost open-version state from the base and the runs' deltas in the staged chain (`interval_local.open_state_sql` does it on one machine).

## 4. Retention of per-scan artifacts (proposal — Ryan decides)

**The gate.** A scan's per-scan artifacts become deletable only once all three hold:
- its reconstruction digest equals that scan's per-path rows (§6; every scan passes today);
- the view parity sample passes;
- the site reads the interval store (§5 phase 3).

| Artifact | Proposal | Why |
|---|---|---|
| per-scan path store (`index/<gen>/path-index*.parquet`, `.groups.*`, `attr.tsv`) | keep the **latest 7 scans**, delete older | the interval store serves every scan; the newest stay as fallback while it is new |
| D1 `index_row_groups` for scans the store serves | stop syncing; retire existing rows with `index-gc` once nothing reads them | the store's group index is on R2 |
| `dir-cache/` | keep the latest 7 scans | only the next scan's attribution reads it |
| raw listings (10.7 GB per scan) | **keep**, Coldline after 30 days | ground truth: a store bug is only recoverable from them |

**What that saves:**
- per scan, ~10.7 GB of listings plus the store's ~0.12 GB delta, instead of 51 GB;
- the 3.3 TB backlog falls to ~0.76 TB of listings plus 31.6 GB of store, ~0.33 TB of it in recent per-scan copies.

**How deletion runs:**
- `oa-gcs-usage-dvx` has 7-day soft delete, so a deletion is recoverable for a week.
- Deletion is an explicit job step gated on the verify marker, never a blind lifecycle rule. A lifecycle rule at ≥ 60 days can backstop it.

## 5. Migration

1. **Build and verify** (done for 71 scans, §6).
2. **Dual-read behind a flag** (done on dev; on `cloud` since v4). The current generation is `INTERVAL_STORE_GEN="2026-10-09b"` with `INTERVAL_STORE_REV` unset; `2026-10-09` + rev 2 is the v3 generation, still on R2.
   - `PATH_STORE=opt-in`: only requests with `ps=iv` read the store (`withPathStore`). `PATH_STORE=intervals` makes it the default.
   - Cache keys carry `iv:<gen>[@rev]` (`pathGens`). `INTERVAL_STORE_REV` re-keys every cache after an in-place re-cut.
   - Anything the store can't serve falls back to per-scan stores: a lens, an owner pool, a scan the store doesn't hold.
   - A parity probe (`interval-store verify`) runs per scan.
3. **Per-scan append in the job,** then the flag on in prod. The per-scan write continues meanwhile.
4. **Retire per-scan writes and apply §4,** after ≥ 14 scans of parity passing. The change-only diff and the one-read series land here.

## 6. Prototype, measured (2026-10-09)

**Generation `interval-store/2026-10-09`:** 71 scans, 2026-07-30 → 10-09; v1 indexes through 09-29, v2 store generations after.

**Build** (`dt-cloud interval-store build-ranges`, then named `build`; 32 spot n2-highmem-16 tasks × 256 key ranges, ~41 task-hours; then `fold`, `cut`):

| | Rows | Bytes |
|---|---:|---:|
| per-scan per-path rows, all 71 scans | 20,231,739,106 | ~1.5 TB of path stores |
| path versions (`pv`) | 1,118,487,204 | 14.1 GB (range files) |
| read-day versions (`rd`) | 40,026,364 | 0.22 GB |
| folded versions (served) | 1,156,324,516 | `path` 16.79 GB, `bysize` 14.82 GB, groups 2 × 21.6 MB |
| open at 10-09 (= its per-path rows) | 609,228,304 | |

- **Versions by kind:** objects, 583M versions over 576M paths (1.01 per path; objects are only in the v2 scans, 9/30 onwards); directories, 536M over 223M (2.4 per path).
- **Churn per scan,** opened / closed path versions:
  - the v1 era: median 149K / 123K;
  - 09-30, the v1 → v2 switch: 777M / 217M (every row reopened with objects and structural counts);
  - v2 scans: 1.9–4.5M opened, 0.6–1.9M closed;
  - sweep scans: 10-05 closed 58.1M, 10-06 closed 125.7M.
- **Per-scan reconstruction:** for every key range and every scan, the count and Σ hash of the scan's per-path rows equal those of the versions live at it. **256/256 ranges, 18,176/18,176 (range, scan) pairs equal**, for path versions and read days. The fold equals versioning with `last_read` in the key on the fixtures (`test_fold_equals_versioning_with_read_days`).

**View parity** (`interval-store verify -P gcs`, 4 tasks). Each sampled scan's views are computed twice:
- from that scan's own per-scan `path` sort (independent SQL, exact per-path totals);
- by the reference reader over the served store.

Sample: 20 scans; per scan, the root, a depth-1 band, two buckets, and dirs at depths 2–9. Compared: every tile, every `(other)`, every field (`k b o d a cb us`, and `f` where the scan knows its children). Plus diffs between consecutive sampled scans.
- **Views: 240 / 240 equal**, over all 20 sampled scans (07-30 → 10-09; v1 and v2), ~330K tiles. 216 of them (292,733 tiles) are in `verify/t*.jsonl`; the rest are 10-03 and 10-05 from the task logs, whose task was cancelled on a slow reference lookup. 36 views on 10-06 / 10-08 / 10-09 ran against the folded store (read days from the rows).
- **Diffs: 64 / 64 equal** between consecutive sampled scans, over ~200K diff rows and ~10K changed paths. The sweep scan 10-05's diff was left out: its reference does one SQL lookup per one-sided path, and it was the cancelled task.
- **Verify's cost (2026-10-19).** The 10-01 → 10-09T1236 run (gen `2026-10-09b`, one n2-highmem-16 task) spent 9–10 min per scan on views and 5 min to 2 h on diffs, linear in the diff's one-sided paths: each was two DuckDB queries over the scan's 7.5–8.4 GB parquet (~50K row groups), ~4 s apiece whatever they read (the same floor as a deep view's 25 s), plus ~13 groups of store reads. The per-scan sort is now loaded once into a native DuckDB table (zonemaps over the same `(depth, path)` order: a point query in milliseconds), each diff's one-sided paths are looked up in one read per side (`Scan.lookup_many`, `Store.lookup_many`), and `Store.changes` (a cost statistic, never compared; 60–200M rows a root diff) runs only with `verify -c`.

**Read cost per view,** row groups / rows / bytes decoded. The per-scan figures are the per-scan Worker's plan from that scan's footer index; the interval figures are the store reader's actual reads.
- 180 views with both costs, across 15 scans. Medians: row groups 35 (interval) / 22 (per-scan); rows decoded 287K / 180K; **bytes fetched 3.8 MB / 5.6 MB** (sums 731 MB / 860 MB).
- By era, median bytes (interval / per-scan): v1 root 1.7 / 4.0 MB, v1 subtrees 3.8 / 5.7 MB; v2 root 5.0 / 0.8 MB, v2 subtrees 6.8 / 2.2 MB.
- Rows decoded, v2 roots: 598K / 33K.
- **Where the interval store reads more:** historical v2 reads, which decode closed versions of their paths (the time-partitioning in §3).
- **Where it reads less:** v1-era reads. Its rows are per path, with fewer columns, and per-scan v1 coarse/fine tiers can't threshold.

**Dev timings** (dev.gcs.oa.dev; the store on R2 `oa-gcs-usage-index`; Server-Timing `total` in ms, wall in parentheses). Each pair of columns is cold then warm; warm is the same read with the response cache keyed apart.

| View | Interval cold | Interval warm | Per-scan cold | Per-scan warm |
|---|---:|---:|---:|---:|
| root, 10-09 | 2,196 (3,669) | 124 (915) | 731 (1,440) | 337 (485) |
| `marin-us-central2`, 10-09 | 370 (1,151) | 423 (669) | 667 (1,355) | 320 (518) |
| root, 10-03 | 1,136 (3,388) | 148 (272) | 1,230 (1,789) | 331 (461) |
| root, 09-15 (v1) | 880 (3,086) | 109 (284) | **413 query too wide** | 413 |
| `marin-us-east5`, 09-15 (v1) | 546 (2,228) | 163 (401) | 2,544 (5,273) | 884 (1,757) |
| `marin-us-central1/checkpoints`, 10-09 | 2,953 (4,709) | 1,114 (2,768) | 1,201 (4,241) | 716 (2,343) |
| diff 10-08 → 10-09, root | 5,170 (6,040) | 1,412 (2,969) | 2,359 (3,508) | 594 (802) |
| diff 10-01 → 10-09, `marin-us-central2` | 5,035 (7,088) | 3,644 (4,489) | 2,698 (4,174) | 1,384 (2,966) |
| filtered `q=checkpoints`, root, 10-09 | 3,589 (5,356) | 1,221 (2,924) | 2,169 (3,086) | 1,238 (1,575) |
| filtered `q=tokenized`, `marin-us-central2` | 1,019 (2,610) | 381 (660) | 646 (1,981) | 265 (527) |

- **Subtree views are at parity or faster once warm,** and they serve v1-era roots that per-scan reads refuse.
- **Diffs are 2–3× slower.** The walk's one-sided point lookups each plan R2 footer groups, where per-scan does them as one D1 query. This is the next lever: batch the lookups per level, or use the change-only read.
- **Cold numbers carry a fresh isolate's footer decodes** (`footer` 0.1–2.3 s summed).

### 6.1 Generation `2026-10-09b` (v4): dyadic segments, slices, reader sharing

**Built from the same range files** (`cut -g 2026-10-09 -O 2026-10-09b`; nothing in `2026-10-09` was rewritten). Same answers: every unscoped view and diff measured below has the same tree as on `2026-10-09`, up to the order of equal-sized siblings.

| File | Rows | Bytes | Groups |
|---|---:|---:|---:|
| `path` | 1,156,324,516 | 19.24 GB | 141,227 |
| `bysize` | 1,156,324,516 | 16.75 GB | 141,227 |
| `slices` | 1,156,339,350 | 19.24 GB | — |
| `slices-bysize` (per slice; superseded by `slices-bytotal`, unread) | 1,156,339,350 | 16.74 GB | — |
| `slices-bytotal` | 1,156,356,048 | 18.34 GB | 141,230 |
| `slices-bysize-user` | 1,156,339,350 | 16.74 GB | — |

- **Slice build** (`build -S`, 32 spot n2-highmem-16 tasks, 18.5 task-hours): 256/256 ranges and **18,176/18,176 (range, scan) pairs reconstruct exactly**.
- **On R2:** 36.0 GB of folded sorts and 71.1 GB of slice sorts, 16.7 GB of which is the unread per-slice `slices-bysize`.
- **`fold -S`** (8 tasks): 1,156,339,350 slice versions → 1,156,356,048 pieces with their path's total.

**Local timings,** in ms: the TS reader (`buildView` / `buildDiff`) run in Node on the laptop, against R2 (the interval store) and GCS (per-scan stores). Per-scan footers come from a local copy of gcs's D1 (row groups through 10-04). Cold means a fresh module graph and colo cache; warm is the same read again. Each figure is the best of 2. The laptop is a long way from both stores, so absolute times are higher than a Worker's; compare within a row.

| View | Interval v3 cold | v3 warm | Interval v4 cold | v4 warm | Per-scan cold | Per-scan warm | v4 R2 MB |
|---|---:|---:|---:|---:|---:|---:|---:|
| root 10-04 | 3,267 | 45 | 1,750 | 22 | 853 | 82 | 4.6 |
| `marin-us-central2` 10-04 | 3,297 | 583 | 1,947 | 378 | 961 | 444 | 6.4 |
| root 10-01 | 3,141 | 135 | 1,738 | 27 | 813 | 93 | 4.3 |
| root 09-15 (v1) | 2,499 | 29 | 1,785 | 27 | 1,079 | 653 | 4.4 |
| `marin-us-east5` 09-15 (v1) | 3,677 | 149 | 2,482 | 32 | 1,035 | 106 | 6.4 |
| `marin-us-central1/checkpoints` 10-04 | 5,577 | 2,095 | 3,921 | 1,770 | 1,502 | 864 | 11.1 |
| diff 10-03 → 10-04, root | 3,736 | 1,735 | 2,388 | 786 | 1,290 | 118 | 8.5 |
| diff 10-01 → 10-04, `marin-us-central2` | 5,685 | 1,689 | 2,973 | 1,199 | 1,583 | 868 | 10.3 |
| root 10-09 | 1,941 | 21 | 1,400 | 21 | — | — | 2.8 |
| diff 10-08 → 10-09, root | 4,962 | 2,676 | 3,216 | 1,337 | — | — | 8.7 |
| lens `user:michael-ryan`, root 10-04 | per-scan | | 5,293 | 2,716 | 7,896 | 8,383 | 24.7 |
| pool `unowned`, root 10-04 | per-scan | | 2,113 | 71 | 901 | 104 | 6.0 |
| lens `user:michael-ryan`, root 09-15 (v1) | per-scan | | 6,290 | 4,065 | 3,520 | 3,082 | 29.6 |
| diff 10-03 → 10-04, lens `user:michael-ryan` | per-scan | | 6,852 | 4,820 | 16,702 | 18,841 | 31.7 |

- **Historical reads now decode about what the newest does.** At root 10-04, 12 row groups are read (v3: 28; the newest scan: 6; per-scan: 6). The extra groups are one per small dyadic block (L0–L3) plus L7.
- **Owner reads are served from the store.** The v2 lens root and lens diff have the per-scan tree and rows exactly, and are faster. The unowned pool differs only in float rounding of one `(other)`'s class bytes.
  - The v1 lens (09-15) has the same tile bytes. Its `(other)` tiles differ in `f`/`cb`/`d`, because per-scan v1 reads coarse tiers.
  - Two of its tiles show `o` doubled: an unread region with no row gets the manifest's objects twice (`readView`'s unread-region loop starts at the region itself). This is a pre-existing reader bug that the store's exact reads expose. Fixed in v5 (§6.2).
- **Diffs are still slower than per-scan,** cold 1.8–1.9× and warm well behind. Most of the cold gap is footer decode: the store's footers come from R2 groups (~1 s of a cold diff), where per-scan plans in D1. The warm gap is the row-group LRU (24 MB): a diff's ~40 groups of mostly-live rows don't fit, and in v4 groups hold live rows. Levers:
  - a floor on the dyadic level (§3);
  - a larger LRU for interval handles;
  - the change-only diff over per-scan runs (§2.6).
- **Recurring cost of the slice sorts, per scan, once the append runs (§2.7).** A scan's slice delta is about the folded one (~4M rows), so ~0.12 GB per sort. The five served sorts are then ~0.6 GB of runs per scan to copy GCS → R2, about $0.07 of egress per scan (~$2/month at one scan a day), against ~$0.04 for the folded sorts alone. Writing the runs to R2 directly from the job would avoid the egress.

### 6.2 v5: closing the diff gap in the reader (generation unchanged)

**What changed,** all in the TS reader, over `2026-10-09b` as built (§2.5, §2.6):
- footers open in one suffix read, once per isolate, beside `scans.json` and with the sibling sort's; a footer group (bounds and `rg_json`) is one read;
- a row group is cached once, with every version, for every scan (`ivGroupKey`, `cachedGroup`);
- a diff's lookups are bounded by the other side's version (`Ask.vtLe` / `vfGe`), or answered by its row;
- a lens reads the user-first `slices-bysize-user` (§2.6).

**Timings,** local harness as in §6.1 (Node on the laptop, the store on R2, per-scan on GCS with footers from a local D1 copy), best of 2, ms. Cold / warm / *warm colo*: a fresh isolate whose colo cache an earlier isolate filled (footers and footer groups are colo-cached for a day, `cachedRange`), the usual case for a new isolate in production. Before and after ran back to back, before from `cloud` at `2552870b`.

| View | Before | After | Per-scan | After R2 MB |
|---|---:|---:|---:|---:|
| diff 10-03 → 10-04, root | 2,096 / 687 / *1,054* | 1,468 / **28** / *922* | 1,019 / 114 / *881* | 7.6 |
| diff 10-01 → 10-04, `marin-us-central2` | 2,619 / 1,111 / *1,601* | 1,763 / 1,007 / *1,411* | 1,262 / 858 / *1,425* | 10.2 |
| diff 10-08 → 10-09, root | 3,717 / 1,040 / *1,663* | 1,839 / **27** / *1,429* | — | 7.1 |
| diff 10-03 → 10-04, lens `michael-ryan` | 5,683 / 4,725 (§6.1) | 3,818 / 2,513 / *3,264* | 11,156 / 11,301 / *11,005* | 18.7 |
| lens `michael-ryan`, root 10-04 | 4,069 / 2,781 (v5 run before the lens sort) | 2,469 / 947 / *1,742* | 5,431 / 4,999 / *5,457* | 13.7 |
| lens `michael-ryan`, root 09-15 (v1) | 5,705 / 4,177 | 3,238 / 1,907 / *2,349* | 3,081 / 2,392 / *2,874* | 18.1 |

Every diff has the same rows as before. The lens views' trees equal per-scan's (the double-count fix below is in the shared reader).

- **Warm: at or below per-scan.** The root diffs hold everything they read: 19 distinct groups at 10-03 → 10-04, against 40 per-scan-keyed entries before; per-scan's 18 groups fit too. The `marin-us-central2` diff (28 distinct groups) doesn't fit the LRU in either store. Two runs measured it at 865 and 1,007 ms, per-scan at 852 and 858.
- **Cold: about 1.4× per-scan on this harness, at parity with a warm colo.** What's left is sequential requests: footers, then footer groups, before each row-group read (rootagg, views, walk). Per-scan plans on a D1 that is a local SQLite file here, so its planning costs nothing; in a Worker each of its span and `rg_json` queries is a D1 round trip. Group decodes are even now (iv 19, per-scan 18 at the root diff), but an iv group costs ~18 ms to decode on the laptop against ~12.5 ms (18 columns; the `path` column alone is 6 ms).
- **The bounded lookups:** unbounded, the 10-08 → 10-09 walk's 17 added names read 12 `path` groups, none holding an answer. Group reads per diff (`ngroups`, both sides) with the bounds: 10-03 → 10-04 root 40 → 32, 10-08 → 10-09 41 → 31.

**The levers, as asked:**
- **(b) a larger or dedicated cache: not a larger cap.** A decoded `Row` measures ~370 B on the heap, against the LRU's 160 B estimate (`ROW_BYTES`), so the 24 MB (estimated) cap already holds ~55 MB of rows in a 128 MB isolate. Sharing one entry per group across scans is what makes the working set fit. The estimate is left as is; correcting it would shrink every store's cache.
- **(a) the change-only diff: its reader half, now.** The full read needs the per-scan runs (the job's append, §2.7) and a decision on what `(other)` means (§2.6). The version bounds come from the same interval structure and need neither.
- **(c) a floor on the dyadic level: deferred to compaction.** At the 10-03 → 10-04 root, 10 of the 21 distinct groups (before the bounds) were L0–L3 blocks; an L4 floor would make them ~2–4, ~30% fewer decodes and bytes for a historical diff. It needs a new generation, ~$5 of GCS → R2 egress for the folded sorts alone. The compaction (§2.7) rewrites closed segments anyway.

**Fixed in the reader:**
- **The unread-region double count.** `lensAgg` already counts an unread region's objects (and an unread ancestor's, from its bands); the loop after it added each unread region's manifest objects again, to the region and to every unread ancestor up to P. It now adds them only where the objects fold left a node to the bytes' shape. An unread object region is drawn as a `file`. Test first: 26 assigned regions, 2 unread, both stores (`intervalSlices.test.ts`).
- **The opt-in isolate's miss memo.** `tryOpen` remembered a missing tier per `(store, date, variant)`. An iv read of a v1 date finds its `path` sort a store sort, so it marked the date's coarse tiers missing for a minute; a per-scan read of that date in the same isolate then skipped them. On gcs, every v1 lens and pool read failed ("query too wide", or a crash) after an iv read of the same date. The memo is now keyed by path store (`pathStoreKey`). Test first (`intervalStore.test.ts`).
- **A lens on the path-first slice sort.** See §2.6: michael-ryan's v1 roots were refused, and v2 lens reads cost 2–3× what they now do.

**Parity at gcs scale** (the local harness, `wt/iv-diff/tmp/bench/parity.test.ts`, untracked; read-only, R2 and GCS): 10 scans (v1 08-15, 09-01, 09-10, 09-20, 09-29; v2 09-30 → 10-04). Per scan, the root under lenses `michael-ryan` and `ahmed-ahmed` and pools `unowned` and `owned`; lens diffs between consecutive scans; `unowned` diffs between the v2 ones. Every node compared field by field.
- **v2 lens views: 10/10 equal.** Lens diffs: 9/9 equal, except 3 names in 08-15 → 09-01, where both sides hit `LOOKUP_CAP` and the cap's shared counter races between the sides. `unowned` diffs: 4/4 equal.
- **v2 `unowned` views:** one `(other)`'s class bytes, float rounding.
- **v2 `owned` views: ~320 fields per scan, all the per-scan §7 undercount.** Root totals are equal. Per-scan drops owners' slices under the threshold from drawn tiles (the store's `us` lists also carry `percy-liang`, `root`, `runner`, …), so their bytes and objects land in `(other)`: `marin-us-central2/checkpoints` is 1.6M objects short per-scan.
- **v1 views: tile bytes equal, except one.** `(other)` `f`/`cb`/`d` differ, as in §6.1 (per-scan v1 reads coarse tiers). ahmed-ahmed's `marin-us-east5/tokenized` is 120 MB / 78 objects short on the store, and a few unread ancestors lack `d`/`a`/`cb`: an ancestor of assigned regions whose own slice is under the threshold is valued from its bands (§3).

**Cost of this round:** R2 reads only (no egress fee, a few thousand class B operations); GCS egress from the per-scan reads of the timing and parity runs, ~3 GB, ~$0.40. No writes, no re-cut.

### 6.3 The per-scan append, real (2026-10-10)

**2026-10-09T1236** (the first scan past the base `2026-10-09b`), `dt-cloud interval-store append -P gcs 2026-10-09T1236` from the laptop driving Batch (us-east1; the staged `dt_cloud` on the profile's pinned image):

| Stage | Wall | Batch | Notes |
|---|---:|---:|---|
| prepare | 8 s | — | pin the scan's `path` sort |
| ranges | 744 s | 16 spot n2-highmem-16 × 645 s run (+53 s provisioning) | 256 ranges, median 18.2 s, max 55 s, 5,543 range-seconds |
| publish | 261 s | 1 × 82 s | the cut: 5 sorts in 65 s; no merge (the first run) |
| r2 | 142 s | 1 n2-highmem-4 × 24 s | 664 MB |
| prune | 2 s | — | nothing earlier |
| **total** | **1,162 s** | | |

- **The run:** 1,106,575 versions opened, 5,310,963 closed (paths gone since the 07:00 scan), 605,023,916 open after. Every range's open state equals the scan's rows exactly (count and Σ hash, for `pvl`, `sv` and `svt`: 256/256). Served: 6.4M rows per sort, 785 groups, 664 MB for the five.
- **Cost, ≈ $0.77:** ranges 16 × 698 s × $0.22/VM-hour ≈ $0.68; publish and r2 ≈ $0.01; GCS → R2 egress 0.66 GB × $0.12 ≈ $0.08. Standing: the newest open state in the scratch bucket (~23 GB, ≈ $0.46/month; earlier ones pruned).
- **Wall time** was ~4 min of polling at the static chain's backoff (to 120 s) over jobs of 24–82 s; the append now polls at most every 20 s. The ranges stage is I/O: each range reads the open state (here the base's whole range files, ~4× the open rows; a later scan reads its predecessor's open state alone) and the scan's rows twice. Levers, unmeasured: a smaller machine (`INTERVAL_STORE_MACHINE`; DuckDB is sized to it), more tasks for wall time.
- **2026-10-10, three bounded speedups** (after a 19.2 min run: ranges 697 s, publish 273 s, r2 112 s of which ~60 s provisioning, prune 59 s): ranges balanced across tasks by open rows (contiguous blocks of 16 put the 8M-row ranges together: slowest tasks 536 / 574 s against a mean of 315 s; ranges hold 1.0M–8.2M open rows); the R2 copy folded into the publish task; prune's deletes parallel. Unmeasured until the next scan's append.

**Parity** (`wt/iv-append/tmp/bench/parity.test.ts`, untracked: the TS reader in Node, the store on R2 vs per-scan on GCS with footers from `.groups.parquet`; read-only). For 2026-10-09 (the base's last scan, read with the run beside it) and 2026-10-09T1236: the root and all six buckets, lenses `michael-ryan` and `ahmed-ahmed` and pools `unowned` and `owned` at the root; and 10-09 → 10-09T1236 diffs, plain, both lenses and both pools. Every node, every field.
- **25 of 27 equal exactly**: all 14 plain views, all 4 lens views, both 10-09 pool views, all 5 diffs.
- The two pool views at 10-09T1236 differ only in float rounding of fractional class / owner bytes in three `(other)` folds (`cb` 7017443922487.375 vs .367; summation order), as §6.1 found for the base.
- Reader timings (laptop, ms, cold / warm): root 709 / 36, buckets 274–831 / 580–1,783, lens views 2,071–3,965; per-scan 148–1,217 and lenses 2,097–6,271. Diffs: plain 771 (per-scan 1,851), lens `michael-ryan` 1,853 (10,559).

### 6.4 v7: plain views at per-scan speed (2026-10-10, generation unchanged)

**Where a plain view's time went** (dev, 10-04: the iv root cold had `footer` 1,121 ms and `groups` 552 ms). Profiled locally (`wt/iv-plain/tmp/bench/`):
- **A new isolate started from nothing.** Its first read listed the manifests and read the newest and `scans.json` (three R2 operations in series), parsed each footer's thrift (527 KB per sort, and each run's), decoded tens of footer groups, and decoded every row group again.
- **Decoding is mostly zstd.** An 8K-row `bysize` group decodes in 25 ms on the laptop, 20 ms of it in `fzstd` (pure JS); a Worker is several times slower. The same group as columnar JSON parses in 2.3 ms.
- **A depth band read `bysize`.** `marin-us-central2` at depth 1 read 24 groups where per-scan reads 2 from `path`.
- **A historical view read every run.** At 10-04 the run `2026-10-09T1236_2026-10-10` cost its footers and a group per sort, and changed nothing the view kept.
- The rest is the store's shape. A historical view also reads a group per small dyadic block (L0–L3) and L7; at the newest scan, each run adds groups: `marin-us-central2` at 10-10 reads 27 against per-scan's 14.

**What changed** (the reader alone, §2.5–§2.6): state, compact footers, footer groups and decoded row groups held in the colo cache across isolates; a depth band reads `path`; a view or diff reads only the runs begun by its scans.

**Timings** (the local harness: Node on the laptop, R2 for the store, GCS + a local D1 copy for per-scan, so per-scan's planning is free here, unlike on a Worker). Best of 2, ms: cold / warm isolate / *new isolate over a warm colo*, the usual case on a Worker. "Before" is `cloud` at `191678cb`, run back to back with "after".

| View | iv before | iv after | per-scan |
|---|---:|---:|---:|
| root 10-04 | 1,930 / 32 / *1,236* | 1,595 / 30 / *534* | 707 / 108 / *754* |
| root 10-04, depth 1 | 4,166 / 24 / *2,535* | 275 / 3 / *118* | 246 / 40 / *166* |
| `marin-us-central2` 10-04 | 3,042 / 874 / *2,044* | 2,332 / 519 / *540* | 2,243 / 957 / *1,734* |
| `marin-us-central2` 10-04, depth 1 | 2,225 / 906 / *2,604* | 1,471 / 563 / *518* | 504 / 471 / *543* |
| diff 10-03 → 10-04 | 2,163 / 592 / *1,991* | 2,934 / 53 / *358* | 1,281 / 208 / *1,439* |
| root 10-10 | 1,467 / 45 / *1,492* | 1,121 / 26 / *219* | |
| root 10-10, depth 1 | 1,399 / 7 / *1,043* | 704 / 2 / *94* | |
| `marin-us-central2` 10-10 | 3,192 / 865 / *1,968* | 3,399 / 575 / *597* | |
| `marin-us-central2` 10-10, depth 1 | 2,348 / 1,922 / *5,575* | 689 / 385 / *415* | |
| diff 10-09 → 10-10 | 2,625 / 52 / *2,253* | 2,106 / 54 / *329* | |

- **Over a warm colo, the store is now at or below per-scan everywhere measured,** 1.1–4× faster. The bucket views' remaining ~400 ms is the scan's per-scan sidecars (`extrasFor`, 3.15 MB from GCS), the same in both stores.
- **Cold (the colo's first read of a generation's objects) is still slower than this harness's per-scan,** whose D1 is a local file. The "after" cold runs shared the laptop with the cloud test suite; the cold column is noisy.
- **Answers are unchanged.** A/B over R2 (`ab.test.ts`): `cloud`'s reader and this one, plus this one in a fresh isolate over the colo it filled. 7 scans (v1 09-15; 10-01, 10-03, 10-04, 10-09, 10-09T1236 in a run, 10-10), the root and 6 buckets, full and depth 1; 12 diffs (consecutive scans, root and `marin-us-central2`, full and depth 1, plus 10-04 → 10-10); lens and pool views at 10-04 and 10-10; a lens diff. Every tree and diff is identical JSON, except:
  - the depth-1 views' `tier`, now `path`;
  - on the v1 scan, the depth-1 bucket views' `(other)` `f`. A v1 scan never knew its directories' children, so `f` is what the read saw: 603 children read from `path` against 0 from `bysize` (per-scan's coarse tier sees 7). Bytes and objects are equal.
- **Not done:**
  - **An L4 floor on the dyadic levels** (§3) would cut a historical read's L0–L3 groups (4 of 10–17) to ~1. It needs a re-cut into a new generation (~$5 of GCS → R2 egress for the folded sorts; the slice sorts copied within R2), and the append re-pointed at it. Deferred to the compaction, as before: over a warm colo, those groups now cost a JSON parse each.
  - **A D1 group index** (per-scan's design) would replace the colo's footer and footer-group docs, at ~218 MB of D1 rows per sort pair (like a per-scan scan's) plus ~1.6K rows per run. It would help only the cold colo, and needs a prod D1 write: not proposed until a Worker shows the cold colo matters.
  - **A wasm zstd** (vendored, imported as a module) would cut every cold decode, per-scan's too.

### 6.5 v8: a drilled bucket's full view (2026-10-10, generation unchanged)

On Workers (dev, `b15df9fe`, colo warm) `marin-us-central2` 10-04 full took 1,768–1,985 ms on the store against per-scan's 1,595–1,797, all of it `groups` + `rows` (~365 ms warm). Profiled locally (`tmp/iv-drill-bench/`):
- **19 groups against per-scan's 12** (2 of them P's own row, from `path`). The 17 of `bysize`: 11 in the open segment, the same groups per-scan reads, and 6 closed ones, each the top group of a dyadic block (L0–L3, two of L7) holding the 10-04 versions of directories that changed since (`checkpoints`, `datakit`, …). Their yield: 170, 10, 8, 2, 596 and 8 rows of 8,192.
- **4 of the 11 open groups kept no row,** 3 of them straddling a size bucket boundary (35→34, 34→33, 33→32): a straddler's path range is the whole keyspace.
- **The group's CPU** over a warm colo, per 8K-row group, laptop, sequential: inflate 3.1 ms (`DecompressionStream`; Node's zlib 1.6), `JSON.parse` 2.4, `groupRows` 2.5, `toRow` ~2. A drilled view keeps a few percent of the rows it shapes.

**What changed** (the reader alone): neighbour-bounded size groups (§2.6), and groups held as columns, shaping only the rows a read's cheap test passes (§2.5). The closed blocks' top groups are the layout's: a version live at 10-04 and closed since sits in its block's top group with every other size, and only a re-cut changes that (the L4 floor of §3, at the compaction, doesn't remove them either: it merges L0–L3 blocks, one top group instead of four).

**Timings** (the local harness, best of 2, ms; the subtree read `rows` over a warm colo, new isolate; groups including P's row):

| View | `rows` before | `rows` after | groups before → after | per-scan groups |
|---|---:|---:|---:|---:|
| `marin-us-central2` 10-04 | 131 | 73 | 19 → 16 | 12 |
| `marin-us-central2` 10-10 | 173 | 87 | 27 → 22 | 14 |
| `marin-us-east1` 10-04 | 218 | 94 | 31 → 22 | 18 |
| `marin-us-central1/checkpoints` 10-04 | 252 | 124 | 38 → 33 | 25 |
| root 10-04 | 86 | 43 | 12 → 12 | 6 |
| diff 10-03 → 10-04 `marin-us-central2` | 308 | 152 | 44 → 38 | 27 |

Every other view measured is the same or faster (28 cases: the root and six buckets at 10-04 and 10-10, full and depth 1, historical 10-01 and v1 09-15, five diffs).

**Answers are unchanged.** A/B over R2, `cloud`'s reader against this one, cold, warm and in a fresh isolate over the colo the first filled: 115 cases (7 scans × the root, six buckets and `marin-us-central1/checkpoints`, full and depth 1 for the root and `marin-us-central2`; 8 scan pairs × root, two bucket and a depth-1 diff; owned and unowned pools and a `michael-ryan` lens at the root and `marin-us-central2`, 10-04 and 10-10; a pool diff), identical JSON. `intervalStore.test.ts`: over 450 size reads of g1, g4 and g5 (every scan, six prefixes, five floors), the pruned plan reads exactly the rows of every min/max candidate, and no pruned group holds a row the read keeps (58 of 1,839 candidates pruned); mutation-checked (the previous-group bound applied to every bucket; the floor skip dropped).

**Not done:** the colo copy stays gzipped. Stored raw, a group would skip its inflate (~half of what's left of its CPU) at ~7× the colo bytes; worth measuring on a Worker if the re-time still shows the store behind.

## 7. The per-scan `bysize` undercount (a prod bug this found)

**The bug.** The per-scan Worker thresholds `bysize` reads per owner-slice row: `readSizeRects` keeps `size ≥ thrAt(depth)` per row, and gcs dir rows are `(path, usr)` slices. Two consequences:
- a drawn directory's `b`/`o`/`a`/`us` count only its slices over the threshold;
- a directory whose slices are each under the threshold, but whose total clears it, isn't drawn at all.

The missing bytes land in the parent's `(other)`. Totals at the root are unaffected (the root reads every slice), but tiles are wrong.

**Measured on dev, which reads prod's per-scan data:** the root view at 1280×768, per-scan `bysize` against exact per-path totals (the interval store, equal to the per-scan reference):

| Scan | Threshold | Tiles | Tiles short | Tiles missing | Σ shortfall over tiles | Bucket tiles short |
|---|---:|---:|---:|---:|---:|---:|
| 09-30 | 41.5 GB | 4,430 | 94 | 5 | 1.9 TB | 197 GB, 434,570 objects |
| 10-01 | 41.6 GB | 4,429 | 94 | 5 | 1.9 TB | 197 GB |
| 10-03 | 40.6 GB | 3,967 | 94 | 6 | 1.9 TB | 197 GB |
| 10-05 | 39.3 GB | 3,657 | 93 | 6 | 1.95 TB | 197 GB, 489,793 objects |
| 10-07 | 39.3 GB | 3,643 | 92 | 6 | 1.80 TB | 197 GB |
| 10-09 | 39.5 GB | 3,653 | 92 | 6 | 1.80 TB | 197 GB, 489,799 objects |

- **Worst tiles:** `marin-us-central1/data/datakit/normalized/nemotron_cc_v2_1` 421 of 2,653 GB short (16%); `…/normalized/cp` 153 of 962 GB; `…/normalized/finepdfs` 135 of 832 GB; `marin-us-west4/checkpoints/delphi-prefix-sources` 157 of 345 GB.
- **By bucket:** `marin-eu-west4` 98 GB, `marin-us-east5` 55 GB (55,633 objects), `marin-us-central2` 16 GB, `marin-us-west4` 11 GB (295,477 objects), `marin-us-east1` 9 GB, `marin-us-central1` 7 GB.
- **Which scans and views:** every v2 scan (09-30 onwards, where the per-scan reader picks `bysize`), and any thresholded view that reads `bysize`: root, buckets and big dirs. The verify sample's emulation found 8 of its first 24 v2 views affected.
- **Not affected:** v1 scans (coarse/fine tiers, read by `path`) and `depth=1` bands read from `path`.
- **Fixes:**
  - in the per-scan store, give each slice row its path's total in a sort column (bucket on the path's total), or read every slice of the selected paths in a second pass;
  - the interval store's one-row-per-path design has no such case for unscoped reads, and its owner-slice size sort is keyed on the path's total (`slices-bytotal`).

## Open questions

- Should ClickHouse (ch-store) feed from this store instead of its own ingest, so there is one history?
- Should the raw listings themselves be interval-coded (objects, `generation` as the change key), ending the per-scan write entirely?
