# Change metrics: added, deleted and modified objects beside bytes and objects

**Status:** draft v1, 2026-11-26; Ryan's go 2026-11-27 on phases 0 and 1 with the adjustments in §10. Written for the root `disky` session, from Ryan's ask below and the root session's analysis (§9 lists where the code corrected it).

**The ask (Ryan, 2026-11-26):** "#rows as a metric analogous to #bytes and #objects that should be tracked / viz'd about fleets. Adds/Dels/Mods would be broken out, ideally… it generalizes from 2 to 5 such metrics, makes the required abstraction / monoid handling clearer?"

**In short:**
- Five metrics per path and scan: **bytes** and **objects** are *levels* (a value at a scan); **added**, **deleted** and **modified** objects are *flows* (counted between a scan and the one before it). All five sum up the tree and across deployments. Over time a level is sampled and a flow is summed.
- The trend series are **gross** flows: each adjacent scan pair's changes, summed over a window. They're additive in time, so one cumulative column per path makes any window a subtraction, as `churn_cum` does for bytes. **Net** flows (endpoint against endpoint) stay a diff-view notion for single files, plus Δobjects, which both readings agree on.
- **Conservation:** Δobjects = added − deleted over any window, gross or net. So only *added* and *modified* need storing. *Deleted* is derived from the object level the reader already holds.
- **First cut (phases 0–2): per-scan counters at the root and depth 1, nothing per path.** They come from the append's own delta (a file-level classification of the rows it opens and closes), live in a small `changes.json` sidecar per store tier, and are served on `/api/series` root points. Reader and job cost are about zero. The over-time chart, the fleet page and the digest draw them.
- **Per-path series and count-area maps come later (phase 3):** two cumulative columns on the folded path versions, cut and backfilled like churn. It's a store-layout change, so it waits on `diff-bench` before any CURRENT move.

## 0. What exists today

- **Levels per scan.** `meta.json` (`viz.snapshot_meta`) carries `total_bytes` and `total_objects` from the scan alone. `/api/series` serves `{date, bytes, objects}` per scan for any path (interval store: `readRootAgg`; D1 over-time groups: `overTime.ts`). The fleet reads root `/api/series` per deployment (`fleet/functions/_lib/fleet.ts` `fleetObjects`: the newest scan per UTC day) and draws "objects (primary store)" (`fleet/src/charts.tsx`). The over-time chart has a bytes/objects toggle (`SizeOverTime.tsx` `?obj`).
- **Net object deltas.** The quota digest's `Scan.dobjs` (`digest_quota.rows_from_meta`) is `total_objects` minus the previous scan's: Δobjects, i.e. added − deleted, with no breakdown. `nominal.store_line` reports bytes and objects with a weekly byte delta.
- **Gross bytes.** `churn_cum` / `churn_end` on the folded path versions (`interval_churn.py`, [diff-progressive §3][dp]): Σ|Δsize| per file, rolled up to ancestors, counted after the churn base (`churn_from`). It's served in `path` and `bysize`, and the level reader (`/api/diff/level`) projects it.
- **Store versions per scan.** Each append range's `ranges/r####.json` records `opened` / `closed` / `delta` / `open` per table (`pvl`, `sv`, `svt`, `attribution`). These count **store rows** (directory versions, read-day splits, owner and class changes), not object changes (§9).
- **Object identity.** `version` (string: an exact decimal GCS generation, or an S3/R2 ETag) is a change column on the path versions when a generation carries it (`interval_store.change_cols`). The legacy int64 `generation` is *carried* (`first`), not a change column. A NULL → token transition opens its own version even with size and mtime unchanged (`interval_identity.migrate`).

## 1. Definitions

### 1.1 Events per object, between adjacent scans

Take scan *s* and its predecessor *s⁻* in the store's `scans.json` order. For each **object** (a `kind = 'file'` row) path *p*:

| *p* at *s⁻* | *p* at *s* | event |
|---|---|---|
| absent | present | **added** |
| present | absent | **deleted** |
| present, identity *i* | present, identity *j ≠ i* (§1.2) | **modified** |
| present | present, same identity | none. A version split by `last_read`, owner (`us`) or storage class alone is a **touch**, not counted (§8 Q5) |

- A path's events at *s* are exactly one of these, or none. A path deleted and re-created between two scans is a **mod** (or nothing, if its identity came back unchanged). A path created and deleted between two scans is invisible. Every count here is **sampled at the scan cadence**: the store sees states, not API events (§1.5).
- **Objects only.** Prefixes aren't objects in an object store: a directory's adds/dels/mods are the sums over the objects under it (§2), and a directory appearing is not an event. An object shadowed by a prefix (`a/b` beside `a/b/c`: one row with `kind = 'dir'`, [interval-store §2.1][is]) is counted with its prefix. Its own rewrites aren't seen, and a file ↔ dir `kind` flip is classified as the file row's add or del. Phase 0 counts these flips per deployment (expected ~0) and the tests pin the rule.
- **Synthetic rows** (hf's `<repo>/@history/@unlisted` residual) count like any object: an add when the residual first exceeds 0, a mod when its size moves. That keeps the conservation law (§2.2) exact, since `objects` counts them too.

### 1.2 Modified: the identity rule

A pair of present versions is a **mod** iff:
1. **both carry a version token** (`version`, or the legacy `generation`): the tokens differ. The token alone decides, so a same-token size change (hf's dedup flips a duplicate path's size to 0) is not a mod, and a byte-identical re-upload (same ETag, new `LastModified`) isn't either (§8 Q2);
2. **otherwise** (either token NULL): the size differs, **or** the mtime differs where both scans' mtimes are write times (below).

A NULL → token transition with equal size and mtime is a touch, not a mod. The day a deployment starts carrying versions (or a `backfill-generation -N` repair fills NULLs), its files would otherwise all count as modified.

**Whether mtime is a write time** is data, not a deployment default. The lister knows its `created` column's meaning. Proposed: `bulk-list` records `mtime: "write" | "coarse"` in its shard metadata. `path-index` carries it into the scan's sort metadata, and the interval store's `scans.json` scan entry records it (absent: `"write"` for gcs/s3/r2 schemes, `"coarse"` for `hf://`, by URI scheme, for historical scans).

| store | token | `mtime` | mods by |
|---|---|---|---|
| GCS (gcs) | `generation` (exact), where `path-index -G` listed it. NULL on depth-2/3 objects before 2026-10-22 until a `-N` repair | `timeCreated`: per generation, so a rewrite always moves it | token. The fallback is exact too: `timeCreated` moves iff the generation does |
| CAIOS / S3 (cw) | ETag as `version`, once its generation is migrated (`migrate-object-version`) | `LastModified`: every PUT / copy | ETag. Fallback: size or `LastModified` |
| R2 (r2, the r2 demo) | ETag as `version`, if migrated | upload time | as S3 |
| HF (hf) | none today (`bulk_hf` sets `generation` NULL). The tree's LFS `oid` / `xetHash` is read but not kept | `lastCommit.date` only with `-D`. hf's profile lists `-L` without it, so `created` is the repo's `lastModified` for every file in it (`coarse`) | size only, until hf carries the oid (or the git blob id) as `version` (§8 Q10). `@history` rows are content-addressed by path (`…/<oid>` where a ref pushed several), so an old version replaced is a del + add, never a mod |

The basis travels with the numbers: every counter set records how its mods were decided (`mods_by: {token, size, mtime}` counts, §3.1), so a page can footnote "~N by size/mtime" where tokens are missing.

### 1.3 Gross and net

For a window *(D1, D2]* and a path *P*:
- **Net** adds/dels/mods compare the two endpoint states only: objects under *P* live at *D2* and not at *D1*, live at *D1* and not at *D2*, and live at both with a different identity. This is what a two-scan diff means.
- **Gross** adds/dels/mods are each adjacent pair's events (§1.1), summed over the pairs in the window. For one adjacent pair, gross = net.
- Per object, gross counts every state change and net counts at most one, so **gross total ≥ net total** at every node (the same shape as churn ≥ |net bytes|). Per category, either can be larger. A file added, modified and deleted inside the window is gross 1 + 1 + 1 and net 0. A file deleted, then re-created with a new identity, is gross del 1 + add 1 and net mod 1.
- **added − deleted is the same in both readings**: Δobjects over the window (§2.2).

**Recommendation:** gross is the trend series (over-time chart, fleet, digest) and the diff map's change counts. It's additive in time and costs one subtraction per window (§2.3). Net is shown where it's exact and cheap: a file cell's own label (added / deleted / modified), and Δobjects. A directory's net breakdown over a multi-scan window needs a walk over every version opened in the window under it (§2.3). It isn't served (§8 Q3).

### 1.4 Where counting starts; gaps and cadence

- **Base.** Counting starts after the churn base (`interval_churn.churn_from`, gated by `counts_at`): at the scan where objects are first listed after directory-only (v1) scans, every object "appears" because it's newly listed. Points before it carry no change fields: **absent, not zero**. A counted scan with no changes is all zeros.
- **A missing scan** (gcs 2026-08-10) folds its changes into the next scan's pair. The point says so (`since`: the predecessor's id) and the tooltip reads "since <date>".
- **Cadence.** gcs scans sub-daily and the others daily, so gcs sees intermediate states the others don't (a file rewritten twice between two daily scans is one mod). Cross-deployment comparisons of change counts carry that bias. Charts bucket flows per UTC day or week, never per scan, so a sub-daily scan doesn't draw as a dip (§4.2).

### 1.5 Names

- **"Rows" is taken:** path-store rows, D1 rows, interval-store version rows, a diff's rows. Proposed umbrella name **changes**, with **added / deleted / modified** (objects). Wire fields `added`, `deleted`, `modified`, `changed` = their sum, matching `bytes` / `objects` ([descriptive-field-names][dfn], which retired `b` / `o`). In prose and code comments, "adds / dels / mods" is fine.
- **Store versions** are their own metric: the interval-store versions a scan opens and closes per table (`pvl`, `sv`, `svt`, `attribution`), what the store grows by. Its ratio to changes is the store's **amplification** (ancestors opened per changed object): the number gcs's stable-cs3 priority (O(#changes) storage, [cs3 harmonization][cs3]) is about. Wire `versions: {opened, closed}`; shown on `/scans` and the fleet's ops view, not beside bytes (§4.5).

## 2. The algebra (the "monoid" Ryan asked about)

### 2.1 Three axes, one operation each

| metric | kind | up the tree | across time | across deployments / a group's buckets | a union's members (different scan times) |
|---|---|---|---|---|---|
| bytes, objects | level | sum | **sample**: the value at a time (the newest scan at or before it). A window's change = end − start (a group: subtraction) | sum | carry each member's newest value forward, then sum (`carryForward`, today) |
| added, modified (and deleted) | flow | sum | **sum** over the scan pairs in the window: cumulative(end) − cumulative(start) | sum | sum each member's flows **since the union's previous point**; never carried forward (that double counts) |

- In pyrmts terms all five are `sum` metrics on the tree axis. On the time axis a level is an interval-encoded state (`vf ≤ D < vt`) and a gross flow is a prefix-sum column. Ratios (turnover = changed / objects, % of bytes changed) are computed **after** aggregating numerator and denominator, never aggregated themselves.
- Plainly: *(added, deleted, modified)* per (path, scan pair) is a vector in ℕ³ under component-wise `+`, a commutative monoid. Rolling up the tree, summing deployments and concatenating adjacent windows are all that `+`. It has inverses over ℤ, so time needs no dyadic index, just a prefix sum per path. **Net flows are not a monoid on counts**: net(*D1*, *D3*) ≠ net(*D1*, *D2*) + net(*D2*, *D3*). Only the object *states* compose, so net needs both endpoint states (a walk).

### 2.2 Conservation: deleted is derived

For any path *P* and window, gross or net: **Δobjects(*P*) = added(*P*) − deleted(*P*)**, where objects(*P*) = `n_files` of *P*'s version live at the time (0 where *P* is absent). So:
- **Storage** needs cumulative *added* and *modified* only; **deleted = added − Δobjects**, from the two versions the reader already reads.
- **No `*_end` columns either.** Churn needs `churn_end` because a path going away contributes churn (its bytes). A path that goes away at scan *t* has no adds or mods under it at *t*, only dels, and those are derived. So its last piece's cumulative *added* / *modified* are its values for as long as it's absent, and a reappearing path continues from them.
- **Bytes have the analogous law:** Δbytes = added bytes − deleted bytes + Σ over mods (new − old), and churn = added bytes + deleted bytes + Σ over mods |new − old|. Byte-weighted flows are an option (§8 Q4), not the first cut.
- **It's a test.** Every per-scan counter set satisfies added − deleted = Δ`total_objects` (meta.json) at the root and Δ`n_files` at each depth-1 path, exactly.

### 2.3 What prefix sums give, and what needs a walk

| question | how |
|---|---|
| gross changes of *P* over *(D1, D2]* | *A*(D2) − *A*(D1), *M*(D2) − *M*(D1), dels by §2.2: two point reads (phase 3); root / depth 1 from the per-scan counters (phase 1) |
| a series of *P*'s per-scan changes | phase 1: the counters (root, depth 1). Phase 3: *P*'s own versions (one `(depth, path)` range of `path`). Any change under *P* opens a version of *P* (its size, count, mtime or `dr` moves; otherwise a forced split, as churn's, measured 0 on gcs), so its versions are exactly its change points |
| a count-area map over a window (area = changed objects) | true treemap: children's gross changes sum to the parent's, so the level reader's canvas bound ([dp §3][dp]) holds as for churn |
| net breakdown of a directory over a multi-scan window | a walk over every version opened in *(D1, D2]* under *P*: O(changes in window). Not served (§8 Q3); a CLI can (`changes --net`, §6) |
| a file's own net state | its two versions: exact, free |

## 3. Data

### 3.1 Per-scan counters (phase 1): computed from the append's delta

**From a run's `pvl` delta, O(delta rows).** A scan's run already holds, for every path that changed at *D*, its opened version (`op` 1) and the closed version's full row (`op` −1, `vt` = *D*). For `kind = 'file'` rows, a full join on `(depth, path)` classifies every object event at *D*:

```sql
WITH o AS (SELECT depth, path, size, mtime, version FROM delta WHERE op = 1 AND kind = 'file'),
     c AS (SELECT depth, path, size, mtime, version FROM delta WHERE op = -1 AND kind = 'file')
SELECT coalesce(o.path, c.path) AS path,
  CASE WHEN c.path IS NULL THEN 'added' WHEN o.path IS NULL THEN 'deleted'
       WHEN o.version IS NOT NULL AND c.version IS NOT NULL THEN CASE WHEN o.version <> c.version THEN 'modified' END
       WHEN o.size <> c.size OR (:mtime_write AND o.mtime <> c.mtime) THEN 'modified' END AS event  -- NULL: a touch
FROM o FULL JOIN c USING (depth, path)
```

Then group by event and by the root and the path's depth-1 ancestor (`split_part(path, '/', 1)`). It runs once in `publish` (staged: the one task already reads every range's delta to cut the served sorts) and in `interval_local.append_scan` (r2, the r2 demo, hf: `interval-store add`). One function, `interval_changes.run_changes(con, delta, *, mtime_write)`, serves both.

**Over a base build, from the pieces.** `build` (local and staged) and the backfill (below) classify every adjacent pair from the folded pieces with the same window `interval_churn.events_sql` uses (`lag` over `(depth, path)` by `vf`), extended with the identity columns. Phase 3 reuses the same events, rolled up to every ancestor.

**Where they live: a `changes.json` sidecar per store tier.**
- The base generation's `interval-store/<gen>/changes.json` covers its scans. Each run's `deltas/<key>/changes.json` covers its scans, and a merged run's is the union of its inputs'. Compaction writes the new generation's from the old base's and the current runs' (no folded run is read: `FOLDED_RETAIN` is unchanged).
- Immutable, written once with the tier's other files, before its `meta.json`. It's on the R2 copy's file list (`r2_publish`). For a run published with it, `missing_files` treats it as a reader file.
- Shape (per scan, sparse: root `""` always, depth-1 paths only where nonzero):

```json
{"v": 1, "from": "2026-09-30", "depth": 1,
 "scans": {"2026-11-25": {"since": "2026-11-24T1236", "mtime": "write",
   "paths": {"": {"added": 812, "deleted": 40, "modified": 113, "mods_by": {"token": 100, "size": 0, "mtime": 13}},
             "<bucket>": {"added": 800, "deleted": 40, "modified": 13, "mods_by": {"token": 0, "size": 0, "mtime": 13}}},
   "versions": {"pvl": {"opened": 3104221, "closed": 1985310}, "sv": {"opened": 3301011, "closed": 2100877}}}}}
```

- `versions` is the existing `ranges/r####.json` counts summed per table (runs), or the versions with `vf` = the scan's stamp per table (a base). It costs nothing extra.
- **Size:** ~150 B per nonzero path per scan. A deployment with tens of buckets and ~100 scans per generation is tens of KB, read once per generation and run and cached with the state (`IV_STATE_TTL`).

**Backfill without a new generation.** `dt-cloud interval-store backfill-changes -g GEN -o STORE` writes `GEN/changes.json` and each listed run's sidecar where missing. Like `backfill-level`, it adds keys and never overwrites. A pinned deployment (the r2 demo pins with `-C none`) keeps its pin. The base's pairs come from its pieces (a per-range Batch task where the staged chain has one, else in-process), and runs' from their deltas. Generations pruned before it ran have no history to recover. Scans only in per-scan stores (before the interval store's first scan) get none: the reader's points there are absent (§1.4).

### 3.2 Store versions

The sidecar's `versions` (§3.1). Its trend (opened per scan, and opened / changed) is the cs3 health number. No new computation.

### 3.3 Per-path cumulative columns (phase 3)

- **Columns:** `added_cum`, `modified_cum` (int64, non-null after the churn base) on the folded path versions (`pvl` pieces), served in `path` (and `bysize` where the level reader reads it, as churn is). Each is *P*'s gross count up to the piece's `vf`. There are no `*_end` columns (§2.2).
- **Written by** the same rollup as churn: `churn_gen` (base, `backfill`), `apply_run` (append: opened versions get theirs, and a path whose counts moved inside a piece splits), carries (`max`), compaction (kept). It's one pass emitting three sums (churn, added, modified) per (ancestor, scan) instead of one. The events are the same, and gcs had 26.0 M (directory, scan) rows over 35 pairs ([dp §3][dp]).
- **Backfill:** `interval-store backfill-changes -c -O NEW` folds a generation into a new one with the columns, byte for byte a fresh build's (as `backfill-churn`). `scans.json` gets `"changes": true`; source and CURRENT are untouched.
- **Cost:** two int64s per version. On file rows they're mostly 0 or 1 (how often the path was re-created or rewritten since the base), which RLE / dictionary encode to almost nothing. On directories they grow like churn but are smaller numbers. Expect less than churn's two columns; phase 3a measures it (`path` and `bysize` bytes before/after per deployment). The Worker projects named columns, so readers that don't ask for them fetch no more bytes.

## 4. Reads and UI

### 4.1 `/api/series`

- **Root and depth-1 points gain the flow fields** when the store has counters for that scan: `{date, bytes, objects, added, deleted, modified, since?}`. `split=roots` traces carry them per root. A group (`g=`) sums them per date, as `sumPoints` sums bytes. A union sums each member's flows since the union's previous step (§2.1: a new `carryFlows` beside `carryForward`). Points without counters have no flow fields.
- The body gains `changes: {from, mods_by}`: the counting base and the window's mod basis totals, for the footnote.
- **No new parameter.** The fields ride on the plain root series, so a store's `series_scope` reader (the fleet's tokens; `SUMMARY_PARAMS` = `store` only) gets them with no gate change. `functions/gates.test.ts` keeps its rows. The cached body changes shape, so bump the series' `RESPONSE_V` / `CACHE_V`.
- **Below depth 1** (phase 1): no flow fields, and the chart says "changes are tracked at the store root and its top level". Phase 3 fills any path from its versions (one range read of *P*'s versions in `path`, as `readPathHistoryRows`, then differences of the cumulative columns).

### 4.2 The over-time chart (`SizeOverTime.tsx`)

- The metric toggle (`?obj`) becomes three: **bytes · objects · changes** (`?m=changes`; `?obj` kept as an alias).
- *changes* draws the objects line (level) on top and a **flow strip** under it on the same x-axis: signed bars per UTC day (per ISO week when more than ~90 days are shown). **Up: added (green) with modified stacked on it (neutral)**, i.e. objects written. **Down: deleted (red)**, objects removed. Net (added − deleted) is a dot per bar. Colors follow the diff's polarity convention (green = grew, red = shrank). Mods are neither, so they get a neutral hatch (§8 Q6).
- At `split=roots` the strip stacks per bucket in the map's slot colors (up and down separately). The tooltip gives the bucket's three numbers.
- Tap or hover a bar: "Nov 18 · +12,410 added · −88,201 deleted · ~3,120 modified · net −75,791 (2 scans)". With `since`: "since Nov 16". With a fallback basis: "~3,120 modified (3,000 by size/mtime: no version recorded)".
- The x-range brush and the diff window work as today. The strip sums whatever the window covers, which matches the diff's gross counts (§4.4).

Phone width (~360 px):

```
Objects · changes        [1w 1m all]
 1.24M ━━━━━━━━━━╮____╭━━━━━━━━━━━━
                 ╰━━━━╯
 ─────────────────────────────────
 +   ░     ░░          ░
     ▆ ▃ ▅ ██ ▂ ▄ ▃ ▅ ▆▆ ▂ ▃ ▄
 0 ───────────────────────────────
     ▂     ▁▅          ▇█
 −
   Nov 1       Nov 15       Nov 26
 ▆ added  ░ modified  ▂ deleted · net
```

### 4.3 The fleet app (`fleet/`)

- **Data.** `readDeployment` already reads root `/api/series`. `fleetObjects` keeps the newest scan per UTC day for the level. A new `fleetChanges` **sums** every scan's flows per UTC day (summing a level would be wrong, and so would sampling a flow). `FleetDeployment` gains `changes: ChangeDay[] | null` (`{t, added, deleted, modified, scans}`), and `errors.changes` where the series has none.
- **Panel.** "changes / day" beside "objects (primary store)": a row per deployment on its own scale, like `CostPanel` (signed counts don't share a log axis, and gcs's churn would flatten r2's). Each row is up/down bars in the deployment's color: written up, deleted down. Its label is the 7-day totals (`+2.1M −1.8M ~0.4M`). Hover reads a day and links its newest scan's run page.
- **Summary row:** a `7d Δ` cell (`+2.1M −1.8M`, mods in its tooltip), collapsing into the phone layout's `data-label` like `/meta · 7d` (§8 Q7).
- **The card** (dOGI, four panels) is unchanged in the first cut (§8 Q7).

```
changes / day        (each row its own scale)
gcs  ▆▃▅█▂▄▃▅▆▂▃▄▅▆   +2.1M ~0.4M
     ▂▁▃▅▁▂▁▂▃▁▂▁▂▁   −1.8M
cw   ▁▁▂▁▁▁▃▁▁▁▁▂▁▁   +88K ~2K
     ▁▁▁▁▁▁▁▁▁▅▁▁▁▁   −90K
r2   ▁▁▁▁▂▁▁▁▁▁▁▁▁▁   +1.2K ~40
     ▁▁▁▁▁▁▁▁▁▁▁▁▁▁   −1.1K
hf   ▃▁▁▁▁▅▁▁▁▁▁▂▁▁   +310 ~12
     ▁▁▁▁▁▁▁▁▂▁▁▁▁▁   −95
```

### 4.4 The diff view (phase 3)

- `/api/diff/level` rows gain `added`, `deleted`, `modified` (gross over the window, from the two versions it already reads: two more projected columns; dels by §2.2). A file row is labeled with its net state. The cell tooltip reads "+1,204 added · −88 deleted · ~310 modified (summed over 7 scans)".
- **An area option `m=changes`:** area = changed objects, a true treemap for the same reason as churn (§2.3). It complements churn's bytes: a million tiny files rewritten is invisible by bytes and dominant by count.
- The diff header's totals: root gross counts from the counters, available from phase 1 for any window.

### 4.5 Digest and run threads

- **Quota template daily reply:** `Scan.dobjs` (Δobjects) becomes the breakdown when counters exist: "+812 −40 ~113 objects" (net still = +772). Same arrow math. Its absence keeps today's text.
- **Run thread** (`#<deploy>-admin`, `nominal`): the finished scan's line gains "changes +812 −40 ~113 · store versions +3.1M". It's what the run produced, and it shows amplification at a glance.
- The cost template (gcs) and the monthly OP aren't changed in the first cut (§8 Q8).

## 5. Parity

Every deployment serves the interval store ([CLAUDE.md][claude] baseline), so the counters reach all of them from the shared append and the backfill. What history can supply:

| deployment | append path | mods basis | history | gaps |
|---|---|---|---|---|
| gcs | staged (Batch `publish`) | `generation` (exact), else `timeCreated` (equivalent) | from the churn base 2026-09-30, over every generation's base and runs still on the store | depth-2/3 objects before 2026-10-22 have NULL `generation`. The fallback is exact here, so no gap in mods |
| cw | staged | ETag if its generation carries `version`, else size + `LastModified` | from its churn base | ETags only where its listings had them (phase 0 checks `object_version`) |
| r2 | `interval-store add` | as cw | from its churn base | as cw |
| the r2 demo | `interval-store add` (daily ingest, `-C none`, pinned) | as cw | full (one generation, unpruned) | none |
| hf | `interval-store add` | **size only** (`mtime` coarse, no token) | from its churn base | same-size rewrites are invisible, and a dedup size flip reads as a mod, until hf carries `version` (§8 Q10). Depth 1 is its namespace, so the phase-1 split is root-only there |

- The `meta` store ("data behind this site") has its own interval store and counters, same rules.
- A store whose generation predates `version` gets the fallback rule, and its `mods_by` says so. Nothing in the classifier is keyed on a deployment.

## 6. Phases

Each phase lands on `cloud` as `[cloud]` commits with no deployment defaults. Deployment wiring and backfills are each session's.

**Phase 0: measure (read-only).**
- `dt-cloud interval-store changes -g GEN -o STORE [-s SCAN]… [-j] [--net D1..D2]` prints per-scan counters from a generation's pieces and runs (§3.1) without writing. `--net` gives a window's exact net breakdown by walk, for comparison. It's phase 1's classifier behind a CLI, which the backfill reuses.
- Run it per deployment (gcs as a Batch job in its bucket's region, the rest locally) over its newest ~10 scans. Report per scan: counters, `mods_by`, kind flips, touches, and the §2.2 identity against `meta.json`.
- Checks: each deployment's `scans.json` flags (`churn`, `object_version` / `generation`), its churn base, and its mtime semantics.
- **Exit:** the identity holds on every scan of every deployment, and the touch / kind-flip counts are recorded in this spec.

**Phase 1a: counters in the store.**
- `run_changes` in `publish` and `append_scan`. Counters in `build`, carried by `carry` and `compact`, and `backfill-changes` (sidecars only). `r2_publish` and `missing_files` know the sidecar.
- Tests (`cloud/tests/test_interval_changes.py`, exact equality throughout):
  - **The fixture.** s0 is the churn base. a/x: 10 B throughout. a/y: 20 B; 25 B at s1; same size and new token at s2; a `last_read` change only at s3. a/w: added at s1, deleted at s3. b/z: deleted at s1, re-created with a new token at s2. b/q: token NULL until s2, then known with the same size and mtime.
  - **Root counters**, `{scan: (added, deleted, modified)}`: `{s1: (1, 1, 1), s2: (1, 0, 1), s3: (0, 1, 0), s4: (0, 0, 0)}` exactly; s0 has none.
  - **Depth-1 counters:** `a: {s1: (1, 0, 1), s2: (0, 0, 1), s3: (0, 1, 0)}`, `b: {s1: (0, 1, 0), s2: (1, 0, 0)}`.
  - `mods_by` per scan; the identity added − deleted == Δobjects per scan and path.
  - **Windows:** gross (s0, s3] == (2, 2, 2); `--net` (s0, s3] == (0, 0, 2).
  - **The same sidecars three ways:** a base built over s0..s4 equals s0..s1 built plus s2..s4 appended one `add` at a time, merged, and compacted at L = 2, byte for byte.
  - **Bases:** a staged-path fixture equals the local one; the backfill over a generation without sidecars writes exactly the build's and touches no other key (listing diff == the sidecar keys).
  - **The mtime rule:** an `hf://`-scheme fixture whose every mtime moves with equal sizes gives `modified == 0`, and the same rows with `mtime: "write"` give one mod per file.
  - **Edges:** a file → dir flip is a del, pinned; a NULL → token transition is a touch.
- Mutations, each killed: the mtime rule ignored, a token compared when one side is NULL, a touch counted, merged sidecars not unioned, compaction dropping run scans.

**Phase 1b: reads and pages.**
- `/api/series` flow fields, `carryFlows`, the `RESPONSE_V` bump, `SizeOverTime` `?m=changes`, and the fleet's `fleetChanges` plus its panel and summary cell.
- TS tests (exact):
  - **`/api/series` root** over a fixture generation with sidecars: `points` == the literal list `[{date, bytes, objects, added, deleted, modified}, …]`, pre-base points without flow fields; `split=roots` traces likewise; a group's points are the members' sums.
  - **A union** of two members at different scan times: `carryFlows` points == a hand-computed list (and a carry-forward mutation fails it).
  - **Fleet:** `fleetChanges` over two scans in one day and one the next == `[{t, added: a1 + a2, …, scans: 2}, {…, scans: 1}]`, while `fleetObjects` keeps the newest.
  - **Charts:** a pure `changeBars(points, from, to, bucket)` == an exact bar list (day and week buckets).
  - **Gates:** `gates.test.ts` unchanged and passing (no new param).
- **Bench:** no served sort changes and no CURRENT move. The series reads one more small cached object per tier, so measure root `/api/series` cold and warm `Server-Timing` on dev before and after (`diff-bench` doesn't sample series today: add a `series` request kind, §8 Q11), and the fleet document's read time.
- Ship per deployment after its backfill.

**Phase 2: posts and ops.**
- The quota digest's breakdown, the run thread's line and `/scans`' store-versions column. Tests: `rows_from_meta` with counters → `Scan` rows equal a literal list, and the rendered reply text equals literal strings with and without counters.

**Phase 3: per-path cumulative columns (a store-layout change).**
- **3a:** cut one generation per deployment with the columns (`backfill-changes -c -O NEW`) and measure its size against its source (`path`, `bysize`, total).
- **3b:** readers. Per-path series from versions, the level reader's fields, and `m=changes`.
- Tests:
  - A backfilled generation equals a fresh build byte for byte; base + runs (merged, compacted) equal a fresh build version for version (the churn suites' fixtures, `iv-churn`).
  - Every `(from, to, directory)` level answer's `added` / `deleted` / `modified` equals one computed from the scans' files alone (as `diffLevel.test.ts` does for churn).
  - A path's series from its versions equals phase 1's counters at root and depth 1 exactly.
- **No CURRENT move without the table:** `dt-cloud diff-bench -a <old> -b <new>` (map and diff cases cold and warm, plus `-v` views and the series kind) per deployment, the size table from 3a beside it, and Ryan's go. A pinned deployment moves its pin with the same table.

**Phase 4 (optional, separate):** hf carries a version token (§8 Q10), byte-weighted flows (§8 Q4), class transitions (§8 Q5).

## 7. Costs

- **Phase 1 job:** one join over the run's file delta rows per scan. gcs's file events per pair have a median of ~2.8 M and peak at 126 M ([dp §3][dp]), seconds to a few minutes in DuckDB inside a task that already reads them.
- **Phase 1 backfill:** one events pass over a generation's file pieces, about the events step of `backfill-churn`.
- **Reader:** ≤ one sidecar per tier, so base + ≤ L runs, each tens of KB, immutable, colo-cached.
- **Phase 3:** churn's rollup plus two sums, and two columns (measured in 3a).

## 8. Open questions for Ryan

1. **Name:** "changes" = added / deleted / modified objects (wire `added`, `deleted`, `modified`), with "store versions" for interval-store rows. Or did "#rows" mean store versions after all?
2. **Mods = content changes:** where both versions carry a token, the token alone decides, so a byte-identical re-upload (same ETag, new `LastModified`) is not a mod. Or count every rewrite?
3. **Gross everywhere but file labels and Δobjects:** a two-scan diff over a long range shows summed-per-scan counts, labeled so. A directory's exact net breakdown is a walk (CLI only). Acceptable, or do you want net served for small windows?
4. **Byte-weighted flows** (added / deleted / rewritten bytes): three more numbers per sidecar entry, nearly free in phase 1. Churn already gives the gross byte total. Want them?
5. **Storage-class transitions** (lifecycle: gcs Nearline / Coldline / Archive) are touches here. A sixth counter, "reclassed"?
6. **The strip:** modified stacked on added ("written up, deleted down"), or mods as their own neutral row?
7. **Fleet:** a `7d Δ` cell in the summary table, the chart panel only, or both? And should the dOGI card get a changes panel?
8. **Posts:** the quota digest's daily breakdown and the run thread line, proposed. Also the cost template (gcs) or the monthly OP?
9. **Phase-1 depth:** root + depth 1, generic. hf's depth 1 is its one namespace, so it gets root only until phase 3. OK, or a per-store `changes_depth` (a deployment knob)?
10. **hf identity:** keep the tree's LFS `oid` / `xetHash` (or the git blob id) as `version` in `bulk_hf`, so hf mods aren't size-only and dedup flips stop reading as mods? It's a listing change on hf's branch.
11. **`diff-bench` gains a `series` kind** for phase 1b's before/after (and phase 3's)? Without it, the series check is a hand-run `Server-Timing` sample.
12. **Phase 3 at all:** per-path change series, count-area maps and diff-cell counts, for two columns per version (measured in 3a before deciding).

## 9. Corrections to the root session's analysis

- **"Root counters may be nearly free from versions opened/closed per scan": no.** Those counts (`ranges/r####.json`) are store rows. They include every changed ancestor directory's versions, `last_read`-only splits, owner and class changes, and count a mod as one close plus one open. They're the separate *store versions* metric (§1.5). Object counters need a file-level classification of the delta (§3.1). That's cheap, O(delta rows) in a task that already reads it, but it's new code.
- **The mods fallback is mtime first, not size.** On gcs/cw/r2 the listing's mtime is a per-write stamp (`timeCreated` per generation; `LastModified` per PUT), so size alone misses same-size rewrites (checkpoints overwritten at a fixed size). Size-only is hf's case, where `created` is the repo's `lastModified` and would mark every file in a touched repo modified.
- **Three flows need two columns:** conservation (§2.2) derives deleted from added and the object level. And no `*_end` columns are needed, unlike churn.
- **Version tokens appearing** (`migrate-object-version`, `backfill-generation -N`) open versions without a content change. The classifier must treat NULL → token as a touch, or the first day with tokens counts every object modified.
- **Unions:** today's carry-forward is right for levels and double counts flows (§2.1).

## 10. Ryan's answers (2026-11-27) and the first cut's adjustments

**Ryan (11-27):** he mostly cares about added and deleted, to notice floods of new objects or rewrites. He asked whether surfacing the object count better (the Slack plots) would be enough, would like adds / dels / mods over any slice eventually, leans to storing changes (maybe both changes and store versions), and asked why hf needs content ids (aren't mtimes enough?).

**Answered (root session):**
- Net change is additive across scans (it telescopes); set-difference counts between a window's ends aren't. Net also hides balanced churn (checkpoint rotation: as many deleted as added) and rewrites, so the gross added and modified counts stay. The series are gross per scan pair, summed as prefix sums (§1.3, §2.3).
- The object count goes into the Slack plots first (phase 0, existing data), then the counters (phase 1).
- Adds / dels / mods over any slice come from `vmd` at query time (#98); the precomputed counters stay at the root and depth 1.
- Store versions (the interval store's opened / closed rows) are opens = added + modified and closes = deleted + modified, plus every ancestor directory's and every touch's versions. They stay a per-run pipeline number (on `/scans`), not a series; the series store **changes**.
- hf: per-file commit dates exist only in the slow `expand=true` listing mode (otherwise every file carries its repo's `lastModified`), so hf's mtimes can't decide mods. The tree's content ids are free in the listing and exact.

**Go (11-27)** on phases 0 and 1, with these adjustments to §6:
1. **Phase 0 first:** the object count, which the Slack threads already have, beside the bytes the quota digest's plot draws (site-rendered: `/api/digest-image`, `site/src/digest/`), from existing scan data and no new store data (§10.1). The old phase 0 (the read-only `changes` measurement CLI) is folded into phase 1's tests and the backfill.
2. **Phase 1** as §3.1, §4.1, §4.2, §4.5 and §6 describe: per-scan counters (added, deleted, modified, and the byte-weighted flows of §8 Q4) at the root and depth 1 from the interval-store append's delta; served on root `/api/series`; the over-time chart's objects line with signed per-day bars (written up, deleted down); the quota digest's daily reply and the run thread. The fleet panel and the `7d Δ` cell may be a later commit.
3. Changes are the series; store versions stay a per-run number (above).
4. hf: `bulk_hf` keeps a content id as `version`: the LFS oid or xet hash, and for plain git files the tree entry's `oid`. A modified hf file is then detected by more than its size. mtime is not relied on there (`coarse`, §1.2).
5. Gross per scan pair, summed (above).
6. Per-path columns (phase 3) are out of scope.

So the open questions of §8 close as recommended: (1) "changes", with "store versions" for interval-store rows; (2) the version token decides; (3) gross everywhere but a file's label and Δobjects; (4) byte-weighted flows yes; (5) storage-class moves ignored; (6) written up, deleted down; (7) both the cell and the panel, card later; (8) the quota digest and the run thread only; (9) root + depth 1; (10) yes; (11) yes, with phase 1b's bench; (12) after 3a's measurement.

[dp]: diff-progressive.md
[is]: interval-store.md
[dfn]: descriptive-field-names.md
[cs3]: deployment-cs3-harmonization.md
[claude]: ../CLAUDE.md
