# Path-agnostic serving: retire `tree.json` / `age.json`, fold the WAL server-side

> Superseded 2026-09-06 by `view-serving.md`: same target, restated from scratch (index *tiers* replace the `tree.json` coarse tier, objects become leaves, the scope axes `k`/`o`/`t`/`q` move server-side, AL as-of + state). §1's audit and the shipped mechanics in §2.1/§2.3 remain the reference for what exists.

Ryan, 2026-08-29: "I want a design that is fast no matter the path. No min-blob floors, no depth floors. These are big tries and should be used as such, not shipped as one big JSON to the client. Serving up-to-date requests means replaying a WAL of online actions on top of the daily index — is that hooked up?"

This spec answers that with the current state (audited, not from memory), the target design, and the order to get there. It supersedes the totals half of `exact-fate-totals.md` (same algorithm, renamed: the API is `marks`, not `fates`) and finishes `path-index-lazy-drill.md` step 4.

## 1. What actually ships to whom today (2026-08-29 scan)

| artifact | size | floor | who reads it | how |
|---|---|---|---|---|
| `listing/<date>/path-index.parquet` | 7.5 GB, **220 M rows** (path × group × user slices), 3,358 row groups × 65,536 rows, sorted `(depth, path)` | **none** | `/api/subtree` (tier 2), `/api/path-index` (raw) | hyparquet range reads over the S3-compat store; edge-cached by `(date, path, w, h)` |
| `snapshots/<date>/tree.json` | 29.5 MB | `MIN_FRAC = 0.0002` of parent, per node | **browser, whole** — any lens, `?f=`, mark mode (i.e. every signed-in visit), pinned legend row; `/users`, `/user/:id`; `/api/todo` and `/api/subtree` tier 1 server-side | one GET, walked client-side |
| `snapshots/<date>/age.json` | 1.2 MB, 17 k strata `(day, top-level dir, group, user, last-read day)` | strata only at top-level-dir granularity | browser, whole | stacked client-side; can't follow a drill |
| `snapshots/<date>/meta.json`, `series.json`, `rules.json` | 6 KB, small | — | browser | fine |
| D1 actions ledger (`keep_prefixes`, `owner_prefixes`) | 6.8 k live marks | — | `/api/actions` → **browser, whole**, folded into a prefix trie (`useMarkIndex`) | resolves per cell; totals walk the loaded tree |
| per-object listings `listing/<date>/<bucket>/*.parquet` | 600 M objects, ~1.2 GB/bucket, 73 files/bucket, **not path-sorted** | — | pipeline only | — |

So: yes, `tree.json` and `age.json` are shipped whole, and the root is the only path they're "fast" for — the floor makes every deeper answer approximate, and the 29 MB is paid before a marker sees a single keep/sweep number. The WAL is only replayed client-side, and only partially: marks re-color and total on the client (against the floored tree), claims re-attribute only on `/users` (`allUserFates`), and the map's user coloring / legend rollups still show scan-time attribution. `/api/subtree` knows nothing about the ledger. `/api/todo` replays it server-side but over `tree.json`.

## 2. Target: one index family, one WAL fold, every read is O(answer)

### 2.1 The index is already a trie — read it like one

A parquet file sorted `(depth, path)` is a level-order trie: the descendants of `P` at depth `depth(P)+k` are one contiguous run of rows, found by binary search over row-group `path` min/max. A subtree query to depth `k` is `k` range reads, wherever `P` is. That's the property "fast no matter the path" needs, and `/api/subtree` tier 2 already exploits it. What's wrong is the granularity: 65 k-row groups (~7 MB) mean a small subtree can pull a 7 MB group for a 30-row answer, and a Worker has 128 MB / 30 s.

- **Footer-in-D1** (`c78a34e`, **shipped**). The 1102 was the cold-isolate parse of the ~5 MB thrift footer, whose cost scales with row-group count. So the footer moves into D1: `index_schema` (the schema, one row/scan) + `index_groups` (per-group depth/path/bytes stats for pruning + the *stripped* RowGroup metadata a read needs — offsets/sizes/codec/encodings/type, BigInts as strings). `openIndex` returns a **D1 handle** — row-group selection is a SQL range query, and the metadata is fetched only for the ≤N groups a query actually reads, so the footer is *never parsed on a cold isolate*. Reads reconstruct a subset `FileMetaData` (schema + selected groups) and hand it to hyparquet, which reads byte-identical rows (proven across first/mid/last groups). A parsed-footer fallback stays for any scan D1 hasn't been synced for. Populated per scan by `gcs-usage index-sync` (pyarrow footer → D1 via chunked `wrangler d1 execute`), wired into `run.sh` (cf-pages-token as a Batch secret).
- **Row groups → 8 k** (`c905031` pipeline + caps): with the footer in D1, row-group *count* no longer touches cold-start, so 8 k rows/group ships — a deep drill decodes ~8 k rows/group, not 64 k (the row-decode half of the fine-tier 1102s). Group caps raised to match (each group 8× cheaper). Depth-partitioning is no longer needed for this — the seam that mattered was the footer, not the file layout.
- **Columns:** **Shipped** (`78b3bc8`): `a` (last-read day, subtree MAX) now in the index and emitted by `/api/subtree`, so the `read` lens colors drilled views from the index, not `tree.json`. `d` (written) stays derived from `wts`/`wb` (mean created day) — a stored min-created `d` is optional.
- **Store:** move the index to **R2** (zero egress to Workers; today every range read is GCS egress via the S3-compat endpoint). The job writes both; the site reads R2. *(Not started — the biggest remaining cost lever.)*

### 2.2 Age strata per path (`/api/age?date&path`)

`age.json` becomes a second index, `age-index.parquet`: rows `(path, depth, day, team, usr, b, o, a)` for every path at `depth ≤ D` (D = 4 covers every drill anyone has done; count = Σ paths≤D × active days ≈ tens of millions of rows, same serving pattern as 2.1). Deeper paths: on demand from a **path-sorted per-object listing** — the daily job re-sorts each bucket's listing by `name` (it already sorts the 220 M-row index; 600 M objects on the highmem-16 node is the same order of cost) so objects under `P` are one range and the histogram is exact, cost ∝ objects under `P`, which is small precisely when `P` is deep. Until the re-sort ships, the chart hides below `D` (as it hides under lenses today) — never a floored or fleet-wide number pretending to be scoped.

### 2.3 The WAL, folded once, applied everywhere

The ledger is the OLTP side: marks and claims since the scan. Fold it **server-side** into a prefix trie keyed by `(scan, max(action_id))` — cached in KV, rebuilt on write (6.8 k prefixes → ~300 KB, ms to fold) — and apply it in every read path:

- **`/api/subtree`**: each returned node carries its resolved mark (`{prefix, action, who, ts, own}`) and, where a claim covers it, its `usr` slices re-pointed at the claimant. The map, legend rollups and cell tooltips then show the claims-applied estate the way `/users` already does. The client keeps a trie of the marks *under the drilled path* only (returned with the subtree), not the whole ledger.
- **`/api/marks/totals?date&path`** (was `/api/fates`) — **shipped incl. `?path=` scoping** (`9f9beca`/`c95d068`): the exact algorithm over the index; estate at the root, or a drilled subtree P via `?path=` (`buckets`→`[P]`, ledger filtered to under-or-at P, P's residual takes its inherited fate). The map's rollup is now **exact at every depth** — the client walk (`subtreeFateTotals`) is only the instant fallback shown while the exact fetch is in flight (marked ≈). Estate cached in D1 (`mark_totals`); scoped uses the per-isolate memo (cheap — a subtree touches ~a dozen row groups). `?marks=1` returns the per-mark manifest, now scopable = the **sweep executor's per-subtree input**. Consumes: map root+drilled rollup, `/users`. Still to consume: the digest bot.
- **`/api/todo`**: same trie + index (largest undecided prefixes = index rows at each depth minus covered ones), no `tree.json`.
- **Lenses / `?f=` filters** (`specs/path-index-lazy-drill.md` "follow-up"): server-side predicates on the same query — `usr = X ≥ 60%` (My files / pinned user), `team = communal`, name regex on `path` — evaluated per row before the pixel-budget fold. The client stops re-aggregating anything.

  **Architectural finding (2026-08-30), from starting the implementation.** The user/team lenses can't ride the existing `by-path` index efficiently: the pixel-budget read prunes row groups by the `b` (total-bytes) stat, but a lens filters by *lens* bytes (`usr=X` / `team=T`), which isn't in any row-group stat, and the lens also re-bases the root's bytes downward (so the correct read threshold is lower → *more* rows, not fewer). Reading all rows under P and filtering in JS is fine for a small drilled subtree but impossible at the root (220 M rows). So efficient user/team lenses need the **`by-user` / `by-team` sorted index copies** this spec already anticipates (§5): the daily job writes `path-index-by-user.parquet` sorted `(usr, depth, path)` (and `by-team`), whose row groups prune by `usr`/`team`; a lens then reads that copy for the lens bytes + the `by-path` copy for the totals, applies the ≥60% `filterTree` prune, and pixel-folds. That's a pipeline change (two more parquets + two more D1 footer-syncs) — a footer-in-D1-scale effort, not a serving tweak. **The `todo` lens is the exception** (mark-state, not an index stat): it reads the normal pixel-budget subtree and prunes decided nodes via the ledger trie — no index copy needed.

Freshness: scan daily, WAL live, caches keyed by the WAL head so a new mark invalidates exactly what it changes. The client can still apply its own just-written action optimistically (it has the prefix) until the next fetch.

### 2.4 What the browser receives, after

Per view: ≤ pixel-budget nodes (a few hundred), the marks under the drilled path, age strata for the drilled path, `meta.json`, `series.json`. Nothing O(estate). `tree.json` is no longer read by the site (the job can keep writing it for the og:image renderer and as a debugging artifact until nothing needs it, then stop); `MIN_FRAC` goes away with it.

## 3. Query engine: parquet-over-ranges in Workers vs a real DB

Argued both ways:

- **Keep parquet + range reads in Pages Functions** (what exists): no infra, zero idle cost, scales with the edge, and every query above is a few binary searches plus ≤ ~1 MB reads — well inside Worker limits once row groups shrink. Weakness: anything that isn't a prefix-range (ad-hoc SQL, joins, the deep-path age histogram before listings are sorted) doesn't fit.
- **A query service (DuckDB on Cloud Run, or a Postgres/ClickHouse with the index loaded)**: SQL for everything, including the listings; but an always-on process to babysit, cold starts, and a second auth boundary. D1 can't hold 220 M rows (10 GB cap, and the daily reload would be the job's slowest step).

Recommendation: the CFN path — the workload *is* prefix ranges over a trie, that's what the layout gives for free — and the DuckDB service only if 2.2's on-demand histogram or a future ad-hoc need outgrows Workers. `/api/path-index` (raw parquet with Range) already gives DuckDB users the escape hatch today.

## 4. Order

1. `/api/marks/totals` + server-side WAL fold (KV-cached trie). Consumers: map rollup, `/users`, digest, executor manifest. Removes the last reason mark mode loads `tree.json`. *(This is also the sweep executor's prerequisite — do it first.)*
   **Shipped 2026-08-29** (`611ec46`): D1-cached per (scan, ledger head), not KV (no KV binding; D1 is shared across isolates). Prod cold compute 10.5 s / 25 row groups for 6,885 prefixes; cached hits are ms. Exact estate: keep 207 Ti · sweep **734 Ti** · undecided 2,426 Ti — `tree.json`'s floor had been hiding ~160 Ti of sweep (576 Ti). Consumers wired: map root rollup, `/users` (no `tree.json` fetch). Still to consume: the digest bot, `?path=` scoping for drilled rollups, the executor (`?marks=1` manifest exists).
2. Index re-layout: `a` column ✓ (`78b3bc8`), **footer-in-D1** ✓ (`c78a34e`), **8 k-row groups** ✓ (`c905031`), **daily auto-sync** ✓ (`101b97f`/`f32285a` — verified in-job on 2026-08-28: pure-Python HTTP-API sync, `cf-pages-token` has D1 write, schema-row-last completeness marker); **remaining**: R2 copy (egress), and `/api/subtree` applying marks + claims per node (so the map's colors/rollups are claims-applied like `/users`, and the client stops fetching the whole ledger). Op note: GCP Batch logs resolved secretVariables (the CF + Slack tokens) into the runnable-command Cloud Log — rotate / add an exclusion.
3. Server-side **user lenses** ✓ (`31c5267`…`c27e1cb`): `by-user`/`by-team` index variants (migration 0014, `viz.py` writes them, `index-sync` syncs all three), `/api/subtree?lens=user:<id>` reads the by-user variant → a treemap of that user's bytes with **no tree.json** (verified: pin/My-files load Calvin's 227 Ti, `treeJson: 0`). Semantics note: this is a treemap of the user's bytes, cleaner than the client's old ≥60%-maximal prune. **Not yet server-side**: team lenses (communal/unclaimed) — a big team's floor-free lens overruns the Worker decode budget (errors "drill deeper" / 1102-guarded), so they stay on tree.json; and todo + name-filter. Robustness: a scan whose lens variant isn't synced (e.g. the daily ran on an image predating them) returns **409**, and the client falls back to tree.json + the client filter (no stuck "loading tree…").
4. `age-index.parquet` + `/api/age`; delete `age.json` reads.
5. Path-sorted listings; deep-path age on demand.
6. Stop writing `tree.json` / `age.json` (og renderer moves onto `/api/subtree`); delete `MIN_FRAC`.

Not in scope: attribution itself (still computed by the daily job; claims are the live override), the access-log ingest.

## 5. Q&A (2026-08-29)

**Sort order.** The index is `(depth, path)` (level order), not `(path,)`. Pre-order gives a whole subtree as one range; level order gives one range per depth, sized by that level only — the pixel-budget drill wants the latter, and a whole-subtree read is still ≤ maxDepth exact ranges, so one copy serves prefix queries of both shapes. A *second sorted copy* is for a different **key**: per-user (`(usr, depth, path)`) and per-group rows are scattered under `(depth, path)`, so lenses and per-user totals would scan the file. Parquet has no secondary indexes — sorted denormalized copies are the indexes (row-group min/max = sparse index); each is a sort in the job and ~7 GB of R2. Plan: `by-path` (exists), `by-user`, later `by-group`.

**Why not a DB.** The scan is an immutable daily snapshot rebuilt whole (220 M rows/day is a write volume no OLTP store wants), the WAL is thousands of rows/day. That is an LSM: immutable sorted runs + a small memtable (D1), merged at read time, and the daily scan is the compaction — the job replays the ledger as of scan time into the index (per-prefix bytes for every live mark/claim), so only post-scan actions are folded live.

**WAL scaling.** Actions are prefix rules, not row edits. Read-time cost: per returned node a longest-prefix match (O(depth)); totals one row lookup per live mark, cached by WAL head. Linear in live marks; at ~10⁵ marks switch to incremental recompute (a new action changes only its subtree — the trie names the affected marks) on top of the scan-time materialization above.

**`/api/actions`** ships the whole ledger to the browser: the `/marks` feed gets server-side `?after=` paging; the map gets the marks under its drilled path inside the subtree response and drops the client trie.
