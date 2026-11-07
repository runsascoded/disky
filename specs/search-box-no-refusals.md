# The search box answers, fast and exact, or not at all

**Status:** site side implemented on `cloud` 2026-10-15 (commits below); §3.1 built (`26dca187`, live per deployment after its generation rebuild); the rest of §3 planned, not built. Path terms (§2.1) parked the same day behind `SEARCH_PATH_TERMS` (default off): the baseline box is the small subset that is always fast and exact, and a term holding `/` is `unsupported-slash` by design.

Every deployment runs one search: the prebuilt static name index (`FILTER_INDEXED_ONLY=1`, no per-scan search sidecars). The box means **literal text matched against names along a path**, answered exactly from that index; what it can't answer is not a half-built search style. The goal is that no form a user types reaches a "not supported" refusal. This spec records what each former refusal does now, and the index changes that close the rest.

## 1. What the box means

Syntax `plain` (`site/functions/_lib/querySyntax.ts`; `queryParam(…, indexed)` under the flag, the client's `indexedSyntax(pathTerms)`):

- `text`: a file or folder name contains it (case-insensitive). Every character is itself: `*`, `?`, `(`, `.`, a `/…/` query. `qs=` is ignored.
- `^text`, `text$`, `^text$`: a name starts with / ends with / is it.
- `a/b`: **unsupported by design** (`unsupported-slash`, up front on every route and in the box: never a walk, never an approximate answer). With `SEARCH_PATH_TERMS=1` (off by default; §2.1) it is a **path term**: the lowercase path contains the text as written, `/` the separator; `^` / `$` anchor its ends to segment boundaries.
- `a b` (AND), `a|b` (OR), `-x` (NOT), `"…"` (quotes), `\^` / `\$` (escapes), as before (`specs/search-extensions.md` §1, §2).
- Owner pools (`o=owned`, `o=unowned`, `o=!a,b`) apply.

## 2. Each former refusal, now

| Former code | Now | Commit |
|---|---|---|
| `unsupported-glob`, `unsupported-regex` | Gone. `*`, `?`, regex characters and `/…/` are literal text (`plain`), any term length (no `short-term` either: the index answers every literal) | `c475f982` |
| `unsupported-slash` | **Kept, by design**: a term holding `/` is refused up front (subtree, diff, series, filter-cover, name-summary and the box; `rejectQuery(q, pathTerms(env))`). Path terms answered exactly (§2.1, `e797c8db`) are parked behind `SEARCH_PATH_TERMS=1`; with it, left: a tail under 2 characters (`slash-tail-short`, §3.3; an empty one, `x/`, names `x$`) | `e797c8db`, parked after |
| `unsupported-scope` | Owner pools answered (§2.2). Left: a person lens or a class scope (§3.4, §3.5); a heavy literal's rollup under a pool (`owner-too-common`, §3.4) | `b4e0cebd` |
| `scan-not-indexed` (the publish window) | The r2 ingest lists a scan only after its appends land, as gcs does (§2.3). Left: a failed append; cw's waits (§3.6) | `c9243667` |
| `anchor-too-short` | Unchanged: `^a`, `ab$` need §3.2 | — |
| `anchor-not-indexed` | Only on a generation built without `anchors/`: the one-machine builds now build them (§3.1); r2 and m3 until their generations are rebuilt. Now also names path terms | `26dca187` |
| `unsupported-kind` (`is:file`) | Unchanged: needs the kind bit (`specs/search-extensions.md` §3, rides the next compaction) | — |

`term-too-common` and `too-broad` stay: they are budgets ("too common to answer exactly here; open a folder"), not missing forms.

### 2.1 Path terms (parked: `SEARCH_PATH_TERMS=1`)

Ryan's call (2026-10-15): the baseline search is a small subset that is always fast and exact, so path terms are off by default. The code stays and its tests run with the flag on. Off: `rejectQuery` / `rejectAst` refuse any term holding `/` (`unsupported-slash`, the client's box too: `/api/filter-caps` sends `pathTerms: true` only with the flag, and the help lists `a/b` only then), and `literalKey` / `boolPlan` / `staticLiteral` never key one, so a deployment that isn't indexed-only walks it as before `e797c8db` (or reads its search sidecars), flagged as any non-static query. Cache: `RESPONSE_V` 27 (a cached 26 held the static answer); with the flag on, a query holding a path term keys `<gen>.27.p`. Tests: `indexedOnly.test.ts` "path terms parked" (every route, alone or in a compound, every tail, over a store that throws on any read), `staticFilter.test.ts` (`^bk/fil`: `static+path` with the flag, the search sidecars without), `staticBool.test.ts` "parked", `queryHelp.test.ts`, `staticGen.test.ts` (the cache tags).

A match root X of a path term holds it and its parent doesn't, so the occurrence ends in X's own name: the term's **tail** (its text after the last `/`) begins X's name (is it, with `$`). Every version of X is therefore a row of the name index's range `/tail…` (exact key `/tail` with `$`). `AnchoredSource` reads that range, whole when it holds ≤ `START_MAX_ROWS` (400K) rows (held per isolate and colo, cut to the view), else scoped to the groups meeting `[P/, P0)` under the same bound, and folds it by the path test (`PathTermHits`: the path holds the term, the parent doesn't). That is every match root, exact on every scan the anchored tiers cover. Keys are `//` + the anchors (`0`/`1`) + the text (`termKey`), so `staticBool.ts` combines path terms with any other term; `termInPath` and the path predicate (`pathQuery.ts`) match them plain, without the hex-run rule (a name index's rule; `/` spans names).

Tests: `staticAnchors.test.ts` "path terms" (every term × every path × every scan × three stacks = brute force from the name rows; scoped reads exact at the bound, `term-too-common` one row under; a mutation dropping the parent test is caught), `staticBool.test.ts` "path terms in a compound" (= brute force from the objects at every root), `indexedOnly.test.ts` (routes: `anchor-not-indexed` on a generation without a name index, `slash-tail-short`).

### 2.2 Owner pools

The static index's hits are owner slices (`usr`), so a pool picks them exactly, as the view already did on deployments without the flag (`staticFilter.test.ts` "an owner pool: the same as the search view"). Under the flag subtree, diff and series now accept `o=`; the series folds the ledger per root as the view's `poolAgg` (`pooledTotal`). A heavy literal's rollup (and the catalog's buckets at the fleet root) carries no owners: `owner-too-common`. Tests: `indexedOnly.test.ts` "owner pools" (flag set = the static view unflagged, on subtree and diff, three pools, a literal and a compound, two roots; the series = brute force per pool; `owner-too-common` on every route).

### 2.3 A scan is listed once it is searchable

gcs's job runs the static append (and the interval append) before `index-sync`'s pointer step, which is what lists a scan; on a failure it publishes anyway (browse and diff work; search refuses that scan) and alerts. The r2 ingest (`.github/workflows/daily-ingest.yml`) now does the same: both appends, then the D1 publish (`!cancelled()`), then a step failing the run if an append failed. The site needs no change for this: a filtered view with no `d=` already opens on the newest indexed scan (`/api/filter-scans`, `floatingScan`).

## 3. Index and pipeline changes (§3.1 done; the rest planned)

### 3.1 Anchors on the one-machine builds (r2, m3) — done (`26dca187`)

`static_local.build` / `add` built no `names/` or `anchors/`, so on r2.rbw.sh and m3 `^q`, a heavy `q$`, `^q$` and every path term were `anchor-not-indexed`. They now chain `static_anchors`' own builders:

1. `build`: after the drill, `build_base_anchors`: `an.write_names` over `cintervals/` (as `static-names anchors names`), then `an.build_tier_local` over `sx/` and `names/` at the drill's R and K (it writes `anchors/start/` too; `meta.json`s last, recording the hex-run rule and the scans). On by default; `-A` / `--no-anchors` skips it. Default on because every deployment should get the full search, and the cost is small at the one-machine builds' scale (the stage logs its seconds and disk / RSS peaks as `build GEN: anchors`).
2. `add` (per pending scan, after its drill run): `run_anchors` → `an.build_run_local` (names from the run's `cdelta/`, both rollup kinds, then `anchors/start/` when every earlier tier carries one) over the base and the manifest's runs, at the base's R and K — when the base has `anchors/meta.json`. A listed run without one under an anchored base is refused (a run built over a gap would never be read).
3. Carries: nothing to wire. `static_merge.run_tiers` already merges `names/` + `anchors/` (`merge_run_local`) when every input carries them.
4. `publish_order` ranks `anchors/meta.json` with `drill/meta.json` (last of the markers, after `anchors/start/meta.json`, as the staged path uploads them).
5. Existing generations: no backfill. An anchorless generation (built before this, or with `-A`) keeps appending without anchors (`add` logs it). It gets them by a rebuild into a new generation: r2 bumps `STATIC_GEN` in `site/wrangler.r2.toml` and the daily ingest builds it (`static-names build` when `scans.json` is missing, seconds), then the site deploys with it; m3 the same with its own `static-names build` and generation pointer (minutes).
6. Tests (`cloud/tests/test_static_local.py`): the base's `names/` and `anchors/` byte-identical to `write_names` + `build_tier_local` over the staged generation's versions and shards (`test_static_catalog._gen`); `^q`, `q$`, `^q$` through `AnchoredReader` (light, rollups, scoped roots, the starts-with catalog, whose members are exactly the prefixes past R) and path terms (the tail's name-index range folded by the path test) equal brute force over the base, base ⊕ level-0 runs and base ⊕ the carry's merged run, every flavor; `-A` builds and appends without them. The staged (Batch) base cuts its rollups per shard (`rollups/<kind>-s####.parquet` + `keys/`), so the two layouts differ by file split; the reader reads either through the rollup index.

### 3.2 Short anchors: `^a`, `a$`, `ab$`

- `^a` (one character): its name-index range `/a…` spans every shard whose three-character prefixes start `/a`. Reader: `StaticNames.selectPrefix(key)` over the shards whose `[lo, hi)` meets `[/a, /a￿)`, one ranged read per shard; scoped reads as `^q`'s. Builder: the starts-with catalog's census from `L = 2` (`prefix_counts_sql`: `range(2, …)`) built across shards (a prefix's rows span them: sum per shard, then threshold), so the fleet root has its per-bucket cells. Then `ANCHOR_MIN.start` = 1.
- `a$`, `ab$`: no index holds suffixes under 3 characters. A **tail index** `tails/`: per version, rows `s` = the name's last 1 and last 2 characters (2 rows per version: ~1.6B on gcs, ~10 GB at the shards' 6 B/row), sorted `(s, path)`, the suffix shards' layout and group index; `q$` for `|q| ≤ 2` reads exact key `s == q` exactly as `q$` reads `sx/` (light, scoped, else rollups). Its end rollups and fleet-root catalog come from `static_anchors.base_rollups(con, tails_files, "end", …)` and `start_base`'s per-bucket form over the same rows. Then `ANCHOR_MIN.end` = 1.
- Cost: one pass over the coalesced versions plus the rollups (~a catalog's cost, ~$3–5 on gcs); per scan, the run's 2 rows per opened version. Ride the next compaction.

### 3.3 Path terms with a short or empty tail, and heavy ones at the top

`ckpt/` (empty tail), `ckpt/a` (one character), and a path term whose tail's range is too big under the view (e.g. `run-7/model` at the fleet root, `^model` being common) are refused (`slash-tail-short`, `term-too-common`). Two changes, cheapest first:

1. **Trailing `/`**: `ckpt/` is "a folder whose name ends with `ckpt`, with something in it": its match roots are such a folder's children, whose bytes are the folder's. With the kind bit (`specs/search-extensions.md` §3) the reader answers it as `is:dir ckpt$` with the roots moved one level down (or drawn at the folders: the same bytes). No extra index.
   - **Why not now, from `ckpt$`'s hits** (decided 2026-10-15, on m3's `^c/disky/` report): the paths holding `x/` are exactly the strict descendants of `x$`'s match roots, so `x/`'s total is Σ over the roots that are folders. The hits carry no kind, and a root's `n` doesn't settle it: an object named `x` and a folder holding one object both have `n = 1` (and on S3 an object `x` beside a folder `x/` is one path, its hit the sum of both). Telling them apart means a path-store lookup per ambiguous root, and the twin can't be split at all, so the rewrite isn't exact without the kind bit. Until then the refusal names the folder term instead: `x/` → "search for the folder itself: “x$”" (`^x/` → `^x$`; `slashTailShort`, `indexedOnly.ts`), which is the same bytes whenever `x` is a folder; a one-character tail (`x/a`) keeps the general wording.
   - **Without the flag** an empty tail is no static literal the sources answer (`AnchoredSource` declines it `slash-tail-short`), so it walks the path store's thresholded read and says so: `approximate`, `APPROX_UNINDEXED` ("this query can’t use the search index; small matches may be missing"), as every walked filter is. Tests: `staticFilter.test.ts` "a path term ending in `/`" (`fill/`, `^bk/fill/` over the fixture's suffix index and over an anchored-like source: the view and `/api/subtree`, first paint and full, flagged, 500 of brute force's 3000 objects; `fill$` exact), `indexedOnly.test.ts` "an empty tail" (every route, the folder wording; `/` alone the general one).
   - The m3 report (`^c/disky/` answered `bysize+path`, 1,163,688 of 1,173,429 objects, reported as served under the flag and without `approximate`) didn't reproduce: the gate refuses it before any read (`rejectQuery` on the raw `q`, every route; dev.disk.rbw.sh answers `400 slash-tail-short` since its indexed-only deploy, `be42d512`, 17:03), and the walked answer carries `approximate` here and on r2.rbw.sh (`ctbk/`: `bysize+bysize`, `approximate: true`). Its numbers are the walk's: likely read before the flag reached dev, or on prod (unflagged), with the flag overlooked.
2. **A pair index** for the rest: per version, rows `s` = each suffix of the parent's name (≥ 1 character) + `/` + the name, sorted `(s, path)` in the suffix shards' layout. A path term's candidates are then the range of its last `x/y` pair (`x` = the part of the term before its last `/`, back to the previous `/`; `y` = the tail, any length): `s` starting with `x/y`, which is exactly the rows whose parent name ends with `x` and name starts with `y`. The range is as selective as the pair, not the tail. Heavy pairs need the drill's roots and rollups over these rows (`static_drill` with `s` from this index). Size: one row per character of the parent's name per version, ≈ the suffix shards again (~13B rows, ~80–135 GB on gcs, ~$15–30 of Batch per base build); per scan, the run's opened versions × their parents' name lengths.

### 3.4 Owners: rollups and person lenses

- `owner-too-common`: the drill's and the anchors' rollups (`(q, dir, kind, child, vf, b, o)`) and the catalog's cells carry no owner. Split them by `usr`: rollup cells keyed `(q, dir, child, usr, vf)` (kept children chosen by total bytes as now; the remainder per `usr`), catalog cells `(q, bucket, usr, vf)`. The reader sums over `usr` for the unscoped view and filters by the pool (or folds the ledger per child, `poolAgg`) for a scoped one. Size: × the distinct owners per child (≤ ~20 on gcs; most children have one), so ~1.5–3× the rollup bytes (17M + 7M cells today).
- `lens=user:<id>` (a person): site-only for light answers: `aggregateHits` with `mine` = the person's slices and `all` = every slice per root, then `lensAgg` (the ledger fold) per root, as the view's search path does; phase 2 (a root's interiors) reads the person's sorts as today. Heavy answers need the owner-split rollups above. Tests: the person-lens view of every literal / compound in `staticFilter.test.ts`'s fixture = the search view's, as for pools.

### 3.5 Storage classes (`cl=`)

Static hits carry no class split. Add `c2, c3, c4` (bytes in Nearline / Coldline / Archive; Standard = the rest) to the suffix shards, the name index and the drill roots (int64, almost all zero: a few % after RLE), and per-class cells to the rollups (or `b2, b3, b4` beside `b`). The reader scales a hit as `classRow` scales a row. Rides the next compaction.

### 3.6 Listing after the append everywhere

- cw (`wt/cw-s3` `job/cw-run.sh`): `index-sync` waits for the append at most `STATIC_NAMES_WAIT_S` (40 min), then lists the scan anyway. Make the wait unbounded (gcs's behavior: list on success or failure of the append, never during it), or raise it past the append's p99.
- cw's `/meta` store (`job/cw-meta-run.sh`): `index-sync` (step 4c) runs before `static-names add` (4d). Swap them.
- Both are deployment-branch files: the cw session owns them.
- A failed append still lists the scan unindexed (`scan-not-indexed` for that scan only, honest). Making the site hide such a scan from the picker would also hide it from browsing and diffs; not proposed.

### 3.7 Largest files on the interval store: object-and-folder twins

`/api/largest` now reads the interval store under `PATH_STORE=intervals` (`readLargestIv`, `b770e54f`), equal to the scan's own size sort (`largestIntervals.v2.test.ts`, `g7`) except for an object whose key is also a folder's prefix (`bk/m/twin` beside `bk/m/twin/f0`): `interval_store.py` folds a path's rows into one version, `kind` = `dir` if any row is, so the object is never listed (`largestIntervals.test.ts`, `g2` / `g5`). Fix: key the versions by `(depth, path, usr, kind)` in `pv` / `sv` (and their `ROW_HASH` / `SV_HASH`), so a twin's object keeps its own row; the Map's readers fold rows by path already (verify with `intervalStore.test.ts`' views). Until then a twin object is missing from the largest-files list on interval deployments.

## 4. Remaining refusals (after this spec's site work)

| Code | When | Closed by |
|---|---|---|
| `unsupported-slash` | any term holding `/`, with `SEARCH_PATH_TERMS` off (the default) | by design; the flag turns path terms on (§2.1) |
| `slash-tail-short` | with `SEARCH_PATH_TERMS=1`: a path term whose tail is under 2 characters, or empty (`x/`: the message names `x$`, the folder, same bytes; unflagged, `x/` walks, flagged `approximate`) | §3.3 (the kind bit for `x/`) |
| `anchor-too-short` | `^a`, `a$`, `ab$` | §3.2 |
| `anchor-not-indexed` | `^q`, heavy `q$`, `^q$`, `a/b` on a generation built without anchors (r2 / m3 until rebuilt) | §3.1 (done; rebuild) |
| `owner-too-common` | a heavy literal under a heavy folder, with an owner pool | §3.4 |
| `unsupported-scope` | a person lens or a class scope with a search | §3.4, §3.5 |
| `unsupported-kind` | `is:file`, `is:dir` | `specs/search-extensions.md` §3 |
| `scan-not-indexed` | a scan whose append failed; cw past its wait | §3.6 |
| `term-too-common`, `too-broad` | budgets: too common to answer exactly here | open a folder (by design) |

`/names` (`/api/name-summary`) still reads plain literals only (`unsupported-slash`, `anchor-not-indexed` there): it is a stepping stone, not the search box.

## 5. Other per-scan readers under `PATH_STORE=intervals`

After `b770e54f` the only `perScan(` readers are the view's search-sidecar reads (`view.ts` `searchIdx`, `searchRoots` for positive and negative plans). They run only with `SEARCH_SIDECARS` and only where the static index didn't answer, which an indexed-only deployment never reaches (it refuses first): they go with the sidecars.
