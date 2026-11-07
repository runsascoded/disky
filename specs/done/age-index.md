# Age index: per-path created-day strata (`AgeChart` for every path)

## Problem

`AgeChart` ("Bytes by creation date") is stuck whole-fleet on any drilled path.
The served artifact `age.json` carries strata **per top-level dir only** (rows
`{d: created-day, d1: top-level-dir, b, o}`), so `scopeAgeRows` can only filter
to a top-level dir and explicitly bails for any deeper prefix
(`roots.some(r => r.includes('/')) → whole`). Drilling to `tmp/ttl=14d` shows
the same histogram as `tmp` and as the bucket root — verified on prod + preview.

`age.json` is exactly the root-hardcoding artifact class we are moving off (cf.
`view-serving.md`, `path-index-lazy-drill.md`): the data must be available for
**every** path, computed once and served by prefix, like the treemap/subtree.

The index cannot be scoped after the fact: it carries only `TreeNode.d` = a
bytes-weighted **mean** created-day per path (`wts/wb`), never a distribution.

## Phase A — a floored per-path created-day index (this pass) — IMPLEMENTED

**Serving: option (1), the variant infra** — register an `age` index served by
the same D1-footer row-group machinery as every other tier. Chosen over
direct-parse because it scales and is the shape B (pyrmts-cfw) slots into. Two
findings made (1) far cheaper than feared:

- The D1 footer's per-group stats `d_min/d_max` are **`depth`**, not day, and
  `p_*`/`b_*` are path/bytes — so the age index (sorted `(depth, path, day)`)
  fits the existing stat model unchanged. The **only** gap was `usr`, which the
  age schema lacks; `index_footer._group_rows` now reads it optionally
  (`u_min/u_max` NULL when absent), and the `(depth, path)` rectangle prunes the
  age index like any other.
- The age index is a **standalone** index, not a path-index tier, so it keeps
  its own base name `age-index.parquet` and `indexKey('age')` special-cases it;
  everything else (`openIndex`, `selectSpans`, `fetchGroupJson`, the group cache)
  is reused as-is.

**Producer — `cloud/src/dt_cloud/index.py` `write_age_index`** (called by
`write_index`, so `index-write` emits it in the same pass; the CW job's
`cw-run.sh` copies `*.parquet` and `index-sync` syncs all variants, so no job
change beyond the copy-glob fix). It reads the **file** rows (`kind='file'`,
`mtime`) — the path-index reads only the pre-rolled `kind='dir'` rows, which
carry a scalar `mtime_mean`, never a day distribution — and rolls each file's
`mtime`-day-bucketed size/count up to every ancestor prefix. Two-stage to keep
the intermediate small: aggregate files to `(parent-dir, day)` first, then
explode **that** to ancestors (`distinct-dirs × days`, not `files × depth`).
(gcs's `viz.py`/`age_days` producer is a convergence-time follow-up, not this
cw pass.)

### Artifact — `age-index.parquet` (per scan, beside `path-index.parquet`)

One row per `(path, created-day)`, descendant-inclusive:

| col | type | meaning |
|---|---|---|
| `path` | VARCHAR | prefix, bucket-prefixed (`depth + 1`), `''` = fleet root |
| `depth` | INT | `len(split(path,'/'))`; bucket = 1, **fleet root = 0** |
| `day` | BIGINT | created epoch-day (site rolls to day/week/month) |
| `b` | BIGINT | bytes under `path` created on `day` |
| `o` | BIGINT | objects under `path` created on `day` |

Sorted `(depth, path, day)`, `ROW_GROUP_SIZE 8192` — the same prefix-range +
row-group-prune contract as the path-index. Because each `path` row is already
descendant-inclusive, **`/api/age?path=P` is a point lookup** (P's own day
rows), not a subtree sum. A synthetic **depth-0 fleet-root row** (`path=''`, all
buckets summed per day) makes the multi-bucket root view a point lookup too.

*Gotcha:* `SUM(BIGINT)` is HUGEINT in DuckDB, which parquet stores without the
int64 min/max stats the footer + reader need — the aggregates are cast back to
`BIGINT`.

**Floor (bounds size).** A floor-free `(path × day)` explode is
`paths × distinct-created-days-per-path` — 10–50× the path-index. So floor to
prefixes whose total bytes clear the **finest coarse tier's** byte floor
(`coarse_floor(fleet, max(COARSE_EXPS))`): every prefix the treemap can drill to
as a page is covered; a prefix below the floor returns no rows and the chart
notes it's too small to stratify. Owner/`a` (last-read)/`d1` (top-level-dir)
slices are **out of scope for A** (Phase B restores rich strata).

### Serving — `GET /api/age?path=<prefix>&date=<scan>`

`functions/api/age.ts`: `requireViewer`, then `openIndex(env, date, 'age')` +
`readPoint(h, depth, path, ['path','depth','day','b','o'])` where
`depth = path === '' ? 0 : path.split('/').length`. Returns
`{ rows: [{d, b, o}] }` (day-granular; the FE rolls to week/month). A scan with
no age index synced (pre-backfill) returns `{ rows: [] }`, not a 500.

`readPoint` is a new export in `_lib/index.ts`: a raw-record (`day,b,o`, not the
shared `Row` shape) point lookup over the same span-select + prune path
(`readGroup` refactored to a raw `readGroupRaw` + a `toRow` map).

### FE

- `App.tsx`: `AgeChart` fetches `/api/age?date=<scan>&path=<drillPath>`
  (react-query, keyed on scan + drill) instead of the `age.json` scanQuery — so
  it follows the **drilled path**, not the whole fleet.
- `ageScope.ts` + `scopeAgeRows` deleted (the server scopes now); the
  `ageAll`/`ageScoped` plumbing and the "per top-level dir" note dropped.
  `matchedRoots` stays (it still feeds `SizeOverTime`).
- `AgeRow.d1` is now optional; `ageModes` is data-driven (an axis is offered
  only if the rows carry its value), so Phase A shows **`date` only** — on cw
  the `user`/`read` age axes never had data (cw's `age.json` was `{d,d1,b,o}`),
  so the sole loss is the `tree`/`d1` coloring, which is meaningless once drilled.

### Removals (post-cutover — NOT done yet)

- `age.json` emit in `job/cw-webdata.py` (and gcs `viz.py`) + its `/data/*`
  allow-list entry — **kept until `/api/age` is verified live** (the deploy-order
  safety net).

### Deploy order (nothing runnable in-session: no listing data / D1)

A ships as reviewed-but-unrun batch/serving code. Order:

1. Deploy the CW job image (new `dt-cloud` CLI + `cw-run.sh` copy-glob) and let
   a scan (or a **backfill** of `age-index.parquet` over published scans via
   `index-write`/`index-sync`) land the index + its D1 footer.
2. **Then** deploy the FE (`site/deploy`) — before the backfill it would 404 →
   empty chart (handled: `{rows:[]}`), so the order matters only for polish.
3. Verify `/api/age` live at a few drill paths, then remove the `age.json`
   producer + fetch.

**Batch job is edited in place in GCP — confirm before deploying the producer.**

## Phase B — pyrmts multi-scale pyramid (follow-up)

Replace A's fixed day buckets with [`pyrmts`](~/c/pyrmts) tiered time pyramids:
`(shard × bin)` tiers, a planner picking the finest tier under a bin-budget (or
an exact `targetBin` via ragged decomposition), `stitch` monoid re-aggregation,
and the FE hook driving the budget off the viewport width. A's `/api/age` +
`AgeChart` wiring survive — B swaps the backend behind the same endpoint and
lets the day/week/month toggle become `bin_budget`/`targetBin`.

Grounded on the pyrmts map (packages `pyrmts` core, `pyrmts-cfw` serve; awair =
time-only reference, ctbk = time+space + D1-footer reader reference):

### Reuse directly

- **Model + planner:** `Pyramid`/`Tier`, `planQuery` (finest tier under
  `binBudget`), `targetBin` ragged decomposition, `binsInRange`. Bins
  `1min|1h|1d|1mo|1y` are first-class (calendar `mo`/`y` handled). Config via a
  `pyramid.yml` (`parsePyramidYaml`/`pyramidFromConfig`), bundled as raw text.
- **Combine fork (decide first):** our metrics are plain additive `b`/`o`
  totals. `stitch`'s monoid re-aggregation expects pyrmts *state columns*
  (`sum` → `[n, sum, sumSq]`), which would pull the Python `pyrmts`
  `write_tier_parquet` into the producer to emit them. Alternative: store bare
  `b`/`o` and do our own trivial sum-combine across segments, using pyrmts only
  for the *plan* (tier pick + ragged decomposition), not `stitch`. Lean DIY
  combine — it keeps the producer as our own DuckDB (path-major, no pyrmts
  Python dep) and the combine is one `GROUP BY`.
- **Serve:** `pyrmts-cfw` `serveQuery({ pyramid, request, shardIndex?,
  watermarks? })` as the body of a Pages Function (`/api/age` becomes a
  `serveQuery` call, or a new `/api/age-pyramid` during migration). Params
  `from`/`to`/`bin_budget`/`smooth` + one per dim.
- **Watermarks:** `D1ShardIndex`/`CachedShardIndex` (tables `pyramid_watermarks`,
  `pyramid_shards`).
- **Producer layout:** Python `write_tier_parquet` (RG-pruning-friendly shard
  layout) — reuse it from `write_age_index`'s file-row source instead of A's
  single-file COPY.
- **FE:** `usePyramid`/`buildQueryUrl` with `binBudget = ceil(chartPx /
  targetPxPerBar)` — the pyrmts-native replacement for `AgeChart`'s
  auto-granularity `useState`.
- **Dep:** `github:runsascoded/pyrmts#<sha>&path:/js/packages/pyrmts` (+
  `pyrmts-cfw`), or a `.pds.json` for local dev; peer `hyparquet`.

### Build ourselves

- **The hierarchical `path` axis** (pyrmts has no prefix axis). **Crux — the
  sort.** pyrmts's writer sorts `binCol` *first* (time-major: RG stats prune a
  time *window* across all dims — awair/ctbk's query shape). Our hot query is
  the opposite: **one drilled path across all time**, so we want **path-major**
  (`(depth, path, dt)`, like A's `age-index.parquet`) — a path's whole history
  contiguous, RG-pruned by `path`. High path cardinality (millions of prefixes)
  rules out key-template sharding per path (awair's `{device_id}` trick). So B
  does **not** take pyrmts's file layout wholesale: it **reuses the time
  planner + `stitch`** over our own path-major tiered parquet (`(depth, path,
  dt)`, one file set per tier/bin), served through our D1-footer reader wired as
  a custom `StorageBackend.fetchSegment`. The `path` prefix range (`P/`..`P0`)
  is the segment filter, exactly A's contract. A minimal-prefix-cover step (port
  geo's `vocabCover`/`minimalCover` DP over the path containment forest,
  analogous to `pickResolution`) is a later upgrade for wide selections; a
  `(dt, path)` time-major inverse pyramid ("which paths moved in this window")
  is a second pyramid, added only if a view needs it.
- **Keep our D1-footer / RG-in-R2-or-GCS reader.** pyrmts always reads the 64 KB
  parquet footer per shard; our `_lib/index.ts` (D1 footer + synthetic
  `FileMetaData`) is the cheaper path we already run. Slot it in as a custom
  `StorageBackend.fetchSegment` (ctbk does exactly this in `avail_geo.ts` with
  its `rg_manifest.ts`) so `serveQuery` calls our reader, not pyrmts's footer
  fetch.
- **A GCS `Storage` shim** (`head`/`getRange`/`get` over GCS via the existing
  S3Store) mirroring `pyrmts-cfw`'s `r2Storage` — only if we use pyrmts's
  parquet path anywhere; the D1-footer backend above avoids it for the hot path.
- **The Python `Source.read_window`** raw→long ingest over the L2 file rows
  (`kind='file'`, `mtime` → floored to the base-tier bin, per `(path, dt)`).
  App-specific ingest is expected to be ours.

### Migration

A's `age-index.parquet` is the throwaway B replaces. Ship B behind the same
`/api/age` (or a parallel endpoint + a FE flag), verify parity, then retire A's
producer/variant. The **column-cube** alternative (time buckets as monoid
columns on path-keyed rows) stays rejected: its O(cols×RGs) footer cost per
query is harsh in a Worker; the tiered pyramid is the safer cut.
