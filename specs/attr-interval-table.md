# Attribution history as an interval-store table

**Status:** v2, 2026-10-23. Reviewed by root (v1 `d8069b00`: approved with the decisions in §8); implemented behind the profile flag `append.attribution` (§3.2), off everywhere until gcs's rollout (§7). Cross-deployment: the code is `[cloud]`; only gcs attributes today. **Not built yet** (the spec moves to `done/` when both land): the staged base build (`build-ranges`) of the table, which a staged generation gets from `backfill-attribution` meanwhile (§2); and §7 step 5, stopping the per-scan `attr.tsv` writes once the table-reading site is on prod.

**Goal (Ryan, 2026-10-23):** stop writing a full `attr.tsv` (+ `attr.tsv.idx.json`) per scan. It maps every attributing prefix to `(user, source, evidence)`, and after `scan retire` it is the last per-scan copy whose cost grows with the number of scans ([lean-scans] §2 kept it, `scan_retire.py` class `extras`). Its history goes into the [interval store] as one more versioned table, sharing the store's key ranges, generations, appends, carries and compaction.

**Out of scope:** user assignments (the D1 actions ledger, applied at read time by `ownerAssignments`) stay in D1. `ck.txt` (the other `extras` file) is untouched. The static name index and its compaction are untouched.

## 1. Before

- `path-index -a` (and `path-index-merge`, `index_parts.py:515`) calls `prefix_labels` (`viz.py:33`) on the scan's attribution parquets (`job/run.sh:292`: `attribution-2026-07-20.parquet`, `attribution-wandb.parquet`, which is rewritten in place) and the scan's listing dirs (path-glob rules expand against them), then `write_extras` (`extras.py`) wrote `attr.tsv`: one line `key \t user \t source \t evidence` per prefix, sorted by key, `''` for a missing source or evidence, plus a 64 KB block index.
- The inputs are mutable, so the map can't be recomputed for an old scan. That is why retire kept it.
- The site (`_lib/extras.ts`) found the scan's generation dir through the D1 `path` pointer (`schemaRow(..., 'path')`), range-read the blocks covering `[P, P0)` plus one block per ancestor of P, and answered `provenance(path, user)`: the deepest attributing ancestor-or-self whose user is `user`. `view.ts:2136` reads it beside every view; `subtree.ts:153` keys the response cache on `hasExtras`.
- **The `retire -P` gap (fixed here):** `scan retire -P` deletes the `path` pointer, after which `extras.ts` fell back to `listing/<date>` and found no sidecar, so provenance disappeared for those scans even though `attr.tsv` was kept. `extras.ts` now finds the sidecar through the scans' layout (`SIDECAR_LAYOUTS`: `listing/{id}/index/{gen}`, the newest generation dir holding one, listed; then `listing/{id}`), never through D1 (`served.test.ts`: provenance with no `index_schema` row). A scan the table covers doesn't read the sidecar at all.
- A latent bug fixed on the way: a NaN `source` in the prefix frame printed as `nan` in `attr.tsv` (`attr_map` now normalizes it to a missing value).

Churn: a prefix's attribution changes when a roster input changes or a path-glob rule matches a new dir. Most prefixes keep the same `(user, source, evidence)` scan after scan, so the map has the same shape as the store's other tables: few versions per key.

## 2. The table

### 2.1 Rows and columns

One row per attribution version, per attributing prefix (`interval_store.AT_SCHEMA`):

| Column | Type | Role |
|---|---|---|
| `depth` | UINT8 | key: the path store's depth of `path` (`count('/') + 1`: a bucket is 1) |
| `path` | VARCHAR | key: the attributing prefix, an index path (`<bucket>[/<dir>…]`, no scheme, no trailing slash) |
| `vf`, `vt` | BIGINT | the version's `[vf, vt)` in scan epochs, `OPEN` while open: the store's own interval columns, kept as named (§8.1) |
| `user` | VARCHAR, nullable | **change**: the canonical user the prefix is attributed to; NULL for a rule attributing it to nobody (which `attr.tsv` printed as `None`) |
| `source` | VARCHAR, nullable | **change**: which rule or input attributed it (`manual`, `wandb-run`, …) |
| `evidence` | VARCHAR, nullable | **change**: the supporting detail |

- Identity is `(depth, path, vf)`, as `pvl`'s (`interval_append.IDENT`). The map is one user per prefix, so there are no slices.
- Deltas add `op` (1 opened, −1 close record), as every other table's.
- Descriptive names throughout: the range dir and the served sort are both `attribution`.
- `user` is quoted wherever SQL names it (`interval_store.ident` / `qcols`, also in `read_parquet` projections).

### 2.2 Files

| Where | File | Written by |
|---|---|---|
| base | `attribution/r####.parquet`, sorted `(depth, path, vf)`, per key range of `ranges.json`; `attribution-digest/r####.json` | `build -A` (`build_attribution_range`), compaction (`compact_range`), `backfill-attribution` |
| base | `served/attribution.parquet` + `.groups.parquet` | `cut_gen` (the sort is in `served_sorts` only with the table) |
| run | `deltas/<D>/attribution/r####.parquet` (a covered scan's; an uncovered scan writes none) | `append_range` → `append_attribution` |
| run | `deltas/<D>/served/attribution.parquet` + `.groups.parquet` | `cut_run`, when the run holds the table's deltas |
| scratch | `state/<D>/attribution/` | the staged append's open state (an uncovered scan rewrites the previous one unchanged); one machine refolds it (`open_state_sql`) |

It is one more entry in the existing registries, not a special case: `SORTS`, `SUB_SCHEMA`, `served_sorts(sliced, attributed)`, `interval_append.STATE_SCHEMA`/`IDENT`/`HASH`/`CHECK_COLS`/`tables(sliced, attributed)`/`run_sorts`, `merge_range` (over the inputs holding it), `compact_range`, `copy_base`, `R2_SERVED`. The key is `pvl`'s `(depth, path)`, so the store's ranges, generations, manifests, carries, compaction, prune and R2 copy handle it with no new partitioning. The staged `cut -i` keeps its task indices (`CUT_TASKS`: the attribution sort after `stats`). Not built: the staged base build (`build-ranges`) of the table; a staged generation gets it from `backfill-attribution` (§4).

### 2.3 The served sort: `attribution`, ordered `(path, vf)`

- **Path first, not depth first.** The only read is "every prefix under P, plus P's ancestors" (`extrasFor`). Under `(path, vf)` the keys under P are one contiguous span per segment (`[P, P + '0')`), and an ancestor is a point read; group bounds `p_min`/`p_max` prune them (the reader's exact-path test).
- Dyadic segments as every sort (`seg_sql`), 8K-row groups, zstd, no size column.
- It is not a path-store tier: no view, diff, lens or filter reads it.

### 2.4 Coverage: which scans the table answers for

A scan's map exists only if its `path-index` ran with `-a`. A scan without one (a failed attribution input, a deployment that doesn't attribute) reads as "unknown", never as "nothing attributed":
- **An uncovered scan contributes no input.** Versions are computed over the covered scans only (`build_attribution_range` numbers them consecutively for the kernel); the append carries the open state over it unchanged. A version live across an uncovered scan does not mean that scan had it.
- **A covered scan with an empty map** closes every version: it reads as an empty map, "nothing attributed", distinct from uncovered (`test_interval_attribution`, `intervalAttribution.test.ts`).
- **Recorded:** the base's `scans.json` says `"attribution": true` when it has the table, and each covered scan's entry names its file (`"attribution_src"`); each run's pinned `deltas/<D>/scans.json` says `"attribution": true` in an attributed lineage (and names the file when covered); each run's `meta.json` has `"attribution"`: its covered scans; each manifest has `"attribution"`: the run scans the table covers (present, maybe empty, once a run carried it; a carry's revision keeps it, its scans being unchanged). A reader knows coverage from the state it already reads (`scans.json` + the newest manifest).
- **A run holds the table's sort iff one of its scans is covered**; `missing_files` checks the sort for exactly those runs.
- **A generation without the key has no table** (every generation today; cw, r2.rbw.sh, hf, m3 for good): no file, no key, no sort, and every other byte unchanged (`test_the_table_leaves_every_other_file_as_it_was`).
- **The table may open at a run.** A base without `attribution/` is an empty open state, so the first appended scan with a map opens every prefix. Turning attribution on needs no new generation; history before it needs one (§4).
- **Once a lineage carries it, appends continue it** (`lineage_attributed`), with or without the flag: an uncovered scan then just isn't covered.

### 2.5 Exactness

For every covered scan D the rows live at D (base + runs, combined per identity with the smallest `vt`) equal D's `attribution.parquet`, row for row:
- `build -A`: count + Σ hash per `(range, scan)` in `attribution-digest/` (`eq`), or the build fails;
- each append: the next open state is D's rows in the range, count + Σ hash, or the range raises; then the parity check (`check_tables`) compares the store's answer at D over the published base and runs with D's rows, row for row, and the scan's marker sums it with the path tables' (`append_marker`; `tables.attribution`);
- `interval-store verify` (and `scan retire`'s on-demand verify): for every scan the table covers, `attribution_parity` compares the whole map at D with the scan's file (`rows`, `store`, `missing`, `extra`), recorded on the marker; a mismatch fails it.

## 3. Writes

### 3.1 Input

`path-index -a` / `path-index-merge` / `index-extras` write `attribution.parquet` beside the sorts (`extras.write_extras`; `ATTRIBUTION_SCHEMA`: `depth, path, user, source, evidence`, typed NULLs, sorted by path). It is the append's input: found beside the scan's `path` sort (`attribution_src`: `<dir>/path-index.parquet` → `<dir>/attribution.parquet`). The staged `prepare` pins it in `deltas/<D>/scans.json`; one machine's `add -A` names it in the scan's entry (`attach_attribution`). The store never parses `attr.tsv`; only `attribution-convert` (§4) does, once. `attr.tsv` (+ index) keeps being written until the table-reading site is on prod (§7 step 5), then stops.

### 3.2 The profile flag

`append.attribution: true` in the interval profile (gcs: `job/interval-store.json`); default false, no deployment default in `[cloud]` code; a non-boolean is refused. One machine: `build -A`, `add -A`. Not refused: a scan with no `attribution.parquet` (uncovered, logged).

### 3.3 Stages

| Stage | Change |
|---|---|
| `build -A` | `build_attribution_range` per range over the covered scans; `"attribution": true` |
| `cut`, `cut_gen` | the `attribution` sort when the generation has the table |
| append ranges (staged `ranges`, one machine's `add`) | `append_attribution`: `append_intervals` over the previous open state (the previous run's `state/` when it carried the table, else the base's, empty without one) and D's map; an uncovered scan's state rewritten unchanged |
| publish, `cut_run` | the run's `served/attribution.parquet` when it holds deltas; `meta.json` and the manifest record coverage |
| carries (`merge_range`) | merged over the inputs holding it; coverage kept by the revision |
| compaction (`compact_range`, `fold_gen`) | the base's file and every run's delta, one row per identity with the smallest `vt` (the versions as built: no islands to re-form); `"attribution": true`; covered scans ride their entries |
| `r2`, `r2-copy`, prune | the new files ride the existing listings (`served/`, range dirs; `R2_SERVED`) |
| `scan retire` | class `attribution` (§5) |

**Tested** (`cloud/tests/test_interval_attribution.py`, on the shared fixtures plus maps with an evidence change, a user change, a source change, a prefix removed and back, one added, an uncovered base scan and run scan, a covered empty map): base + runs (merged or not) equal a fresh `build -A`, version for version; a compaction's and a backfill's served sort are a fresh build's bytes; the staged state chain gives each run the one-machine `add`'s deltas byte for byte; every covered scan reads its map; the markers carry the check; the store's other files are the unattributed store's, byte for byte. The existing interval suites run unchanged on unattributed generations.

## 4. Migration: history from the kept sidecars

1. **`dt-cloud interval-store attribution-convert [-g GEN] -o STORE [-s ROOT] [-n]`**: beside each of GEN's scans' `attr.tsv` without an `attribution.parquet`, write one (`extras.attribution_from_tsv`: the same map, `''` → NULL; refuses lines out of order, repeated or without a user). The one place an `attr.tsv` is parsed.
2. **`dt-cloud interval-store backfill-attribution [-g GEN] -O NEW -o STORE [-s ROOT] [-R RANGES_GEN]`** (`interval_attribution.py`): GEN's base and newest manifest's runs folded into NEW as a compaction folds them (`fold_gen`), and NEW's table built from every scan's `attribution.parquet` (`build_attribution_range`; a scan without one uncovered).
3. **Run-vs-built check:** where GEN's runs already carried the table (the flag turned on first, §7), the built table's live rows at every scan they cover must equal theirs (count + Σ hash per range and scan), else it refuses naming the `(range, scan)` pairs; a scan they cover without a file under ROOT is refused too.
4. Published as `backfill-generation` publishes (data first, `scans.json` last; `CURRENT.json` untouched), with `attribution-backfill.json` (`from`, the manifest and runs folded, the covered, uncovered and checked scans). `scan retire`'s lineage follows it, so GEN's markers hold for NEW.
5. Verify NEW (`interval-store verify` over every covered scan, §2.5), then `interval-store current -s NEW` and the profile's `append.gen` / `ranges_gen`.

It reads one small file per scan and writes no per-scan copy. It runs on one machine (mgu or a Batch task), never the laptop.

## 5. Reads and retirement

**`extras.ts` at scan D, for a view rooted at P:**
1. `ivAttribution(env, D, P)` (`index.ts`): when the request reads the store and its table covers D (`IvState.attribution`, from `scans.json` and the newest manifest: colo state doc `state=3`), open the `attribution` sort over the base (when it has the table) and the runs holding it (`openIvAttribution`; a request naming its scans leaves out the runs begun after them), plan the groups whose exact path bounds touch `[P, P0)` or an ancestor of P, decode them, combine per `(path, vf)` (smallest `vt`), keep `vf ≤ D < vt`. `viewOfRows` answers provenance from those rows.
2. Else (not reading the store, or D uncovered): the per-scan sidecar as before, found through `SIDECAR_LAYOUTS` (§1).
3. Past `IV_ATTR_MAX_ROWS` rows planned, the read is too wide: no extras for that view (null), never a partial map.
- `hasExtras` is true when the table covers D (or a sidecar exists); the response cache already keys on the generation (`pathGens`), so a backfilled generation re-keys it.
- **Vitest** (`intervalAttribution.test.ts`, fixture `fixtures/iv-attr/`, `gen.py`): every covered scan at every view root reads its map's scope, in a generation whose base has the table and one where it opened at a run; an uncovered scan is `undefined` (and `hasExtras` false), the empty map `[]`; the table's provenance equals the sidecar's and the map's; a request naming its scans reads no later run; an uncovered run's absent sort is never read; the cap returns null; a generation without the table never opens it.

**`scan retire`:** a new class `attribution` (`attr.tsv`, `attr.tsv.gz`, `attr.tsv.idx.json`, `attribution.parquet`), deletable, but held (kept, `held` reason in the report) unless the serving generation's table covers the scan and its marker compared the table equal (`attribution_vouched`: the append's `tables.attribution`, or verify's `attribution`). The keep window (`-k`) and grace apply as to every class. `ck.txt` stays `extras`, kept.

## 6. Deployments

| Deployment | Attributes? | Effect |
|---|---|---|
| gcs | yes (`path-index -a`) | the table, once the flag is on (§7) |
| cw (cw.oa.dev) | no `-a`, no owner axis | none: no flag, no files, no key; `extras.ts` returns null as before |
| r2.rbw.sh (the r2 demo) | no | none (one-machine `add` without `-A`: nothing runs) |
| hf, m3 | no | none |

A deployment that starts attributing turns the flag on; its next appended scan opens the table (§2.4), and its earlier history, having no map, stays uncovered.

## 7. Rollout (gcs)

1. **Land** the `[cloud]` code (flag off: no generation changes, no behaviour change; scans start writing `attribution.parquet` beside `attr.tsv` from the next image).
2. **Flag on:** `append.attribution: true` in `job/interval-store.json`, rebuild the job image before the 07:00 daily. The next appended scan opens the table in the current generation (no new generation); its run's marker carries the check.
3. **Site:** deploy the table-reading `extras.ts` (dev first: `site/deploy --dev`, then prod). Steps 2 and 3 land in either order: the site falls back to the sidecar for any scan the table doesn't cover.
4. **Backfill:** `attribution-convert` over the current generation (the kept scans' `attr.tsv` → `attribution.parquet`), then `backfill-attribution -g <current> -O <next>` on mgu or Batch (its run-vs-built check covers the scans step 2 appended); `interval-store verify` on `<next>`; `interval-store current -s <next>`; bump the profile's `append.gen` / `ranges_gen`; rebuild the image.
5. **Stop writing `attr.tsv`** once the table-reading site is on prod and the backfilled generation serves (a `write_extras` change; `publish-r2` stops requiring/copying it, `test_scan_steps.py`'s `require` list).
6. **Retire:** `scan retire` then deletes covered, vouched scans' maps (`attribution` class) outside the keep window. End state: an attribution file exists only for the newest `KEEP` scans; per scan the store adds only the map's changes.

## 8. Decisions (root's review of v1)

1. **`vf`/`vt` kept** (the shared kernel and reader read them by name). A store-wide rename is a separate change, done at a compaction.
2. **Input = `attribution.parquet`** (descriptive columns, typed NULLs), written by `path-index -a` / `path-index-merge`; the store never parses `attr.tsv`. Exactness is row equality against it (count + Σ hash per range and scan, plus verify's whole-map comparison). `attr.tsv` (+ index) is written only until the table-reading site is on prod. Retire's new class covers `attribution.parquet` too and stays inside the keep window.
3. **Coverage in manifests** (§2.4), at no read cost.
4. **Flag first** (`append.attribution`): the table opens at the next run, no new generation; then the backfill, with the run-vs-built check.

[lean-scans]: lean-scans.md
[interval store]: interval-store.md
