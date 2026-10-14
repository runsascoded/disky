# Search extensions: costed options for the static name index

Status: spec only (2026-10-09), nothing built. It prices five extensions of the exact, indexed name search beyond one plain substring, each grounded in the index formats of `specs/architecture/static-name-search.md` and `specs/static-append.md`. The measurements are cheap reads of gen `2026-10-08c` metadata (`sidecar.parquet` 29 MB, `catalog/members.parquet`, `roots-measure/`, `drill/aliases.parquet`) and a 60-row-group sample of the 2026-10-08 scan's `path` sort. No Batch was spent. Where an answer needs a census, the census is priced as a gated first step.

## Baseline: what the index answers today

**The query.** A path matches literal `q` when its lowercase full path contains `q`. For `q` without `/`, that means some segment contains `q`. The filter draws the **match roots** under view root `P`: the outermost matching paths, i.e. the paths whose lowercase name contains `q` and whose lowercase parent path doesn't (first hits). Each root's subtree is counted whole.

**The index** (gen `2026-10-08c`, plus one run per scan):

| Piece | Rows / size | Role |
|---|---|---|
| Suffix shards `sx/` | `(s, depth, path, usr, vf, vt, size, n_files)`, sorted `(s, path, usr, vf)`; 13.2B rows, 262 files, 135.7 GB; 8K-row groups | `s` = every lowercase suffix of ≥ 3 characters of the basename, untruncated. `path` keeps its case. `q`'s rows are the contiguous range of `s` starting with `q`. |
| `sidecar.parquet` | per row group: `(file, rg, s_min, s_max, offset, length, rows)`, 1,615,743 groups | Bounds any `s`-range by whole groups. No path keys. |
| Catalog `catalog/` | 3.07M cells, 25 MB | Per-bucket answers for the **members**: 82,616 long literals (suffix range > V = 100K rows) and 16,897 one- and two-character ones. |
| Drill `drill/` | roots 47.2B rows (299 GB, aliased to 35,915 long classes), rollups 24M cells | A member's match roots under any P. A heavy `(q, dir)` (> R = 100K roots) reads instead a per-child rollup: K = 256 kept children plus an exact remainder. |
| Runs `deltas/<id>/` (+ `drill/`) | ~1.1 GB light + ~2.15 GB drill per scan | The same layouts per scan, combined by smallest `vt`. |

**Light vs heavy.** A literal whose suffix range the group index bounds by `MAX_ROWS` (V plus two groups) is **light**. Its range is read once, folded to first hits (`FirstHits`), and cached per isolate and per colo. Every other literal is **heavy** and goes to the drill.

**Indexed-only** (`FILTER_INDEXED_ONLY=1`, gcs; `indexedOnly.ts`). Only one literal is accepted, unscoped. Every other form is a 400 with a code: `unsupported-{regex,glob,exclusion,terms,slash,scope}`, `scan-not-indexed`, or `term-too-common` (a heavy literal with no heavy source).

**Columns the postings do not have:**
- `kind`: it is dropped at coalescing (`CINTERVAL_SCHEMA` = depth, path, usr, vf, vt, size, n_files).
- A case-preserving key: `s` is lowercase. `path` keeps its case, so a reader can re-test case on rows it already holds.

## Measurements used below

| What | Source | Result |
|---|---|---|
| Exact suffixes `s == X` holding > V rows | `sidecar.parquet`: groups with `s_min = s_max = X` (each full group is 8,192 rows of that exact suffix) | **3,563** certainly heavy (≥ 13 full groups), **4,007** possibly (≥ 11); together they hold 7.32B rows. About 2,150 classes once equal-count suffix chains are aliased (`son` = `json` = `.json`), holding ~3.7B rows. Lengths 3–29. |
| Common extensions, `s == q` rows vs `q`'s contains range | sidecar / `members.parquet` | `.json` 113.3M vs 227.6M; `.success` 105.2M; `.parquet` 75.2M; `.gz` 67.5M; `.jsonl.gz` 59.5M; `.mp3` 32.0M; `.npy` 4.3M; `.txt` 3.7M; `.npz` 3.3M; `.jsonl` 2.4M vs 114.0M; `.bin` 2.2M; `.zst` 1.7M; `.csv` 1.0M; `.pkl` 0.84M; `.zarr` 0.49M vs 2.9M; `.log` 0.25M. **Light** (< V): `.safetensors` ≤ 82K, `.pt` ≤ 98K, `.ckpt`, `.tar`, `.yaml`, `.png`, `.arrow`, `.h5`, `.md5` ≤ 16K; `.index` ≤ 8K, against its contains range of 9.1M. |
| Roots vs suffix-range rows per member | `roots-measure/q` vs `members.parquet` | Σ roots / Σ range = 0.93. Per member: p10 0.94, median 1.00. Low for path-like words: `eval` 0.33, `train` 0.67, `part-` 0.69, `checkpoint` 0.71, `shard` 0.73. |
| Plausible query terms | `members.parquet`, `roots-measure/{q,dirs}` | Not members (always light): `ckpt`, `wandb`, `tokenized`, `llama`, `olmo`, `fineweb`, `dclm`, `safetensors`, `.pt`, `gcs`, `dpo`. Members but light at the fleet root (≤ R roots): `qwen` (34K), `sft` (29K), `marin` (1.1K), `podcast` (7). Heavy at the root, with the number of heavy buckets: `.json` 6, `.parquet` 6, `shard` 6, `tmp` 4, `step` 3, `log` 2, `config` 2, `results` 2, `final` 1, `train` 1, `test` 1, and `eval`, `checkpoint`, `model` 0. |
| File vs directory rows | 491,520 rows of the 10-08 scan (60 random row groups; clustered by `(depth, path)`, so the ratios are rough) | 70% files, 30% dirs. No path ends in `/`. Every file has `n_files = 1`, and so do 34% of the sampled dirs, so `n_files` can't tell them apart. |
| Uppercase in names | the same sample | 4.2% of names hold an uppercase letter. 4.9% of the ≥ 3-character suffix positions contain one. No non-ASCII names in the sample. |
| Scan formats | `scans.json` | 61 v1 scans (07-30 → 09-29: **directories only**; `V1_SELECT` writes every row as `dir`) and 9 v2 scans (09-30 →: files and directories). |

Prices used: spot n2-highmem-16 ≈ $0.22/VM-hour (the 2026-10-08c build: 165 VM-hours ≈ $35–40); GCS → R2 egress $0.12/GB; R2 $0.015/GB-month; GCS Standard ≈ $0.02/GB-month.

## 1. Exclusions and compound queries (`tomat -mp3`, `a b`, `-b`, `a|b`)

### Semantics

As the path-store filter defines them (`queryAst.ts`), over the lowercase full path:

- `a b`: the path contains every term, in any segments.
- `-b`: the path contains no `b`. Negatives are the whole query's.
- `a|b`: OR, looser than AND.

The match roots are the outermost matching paths. A term without `/` lies inside one segment, so "the path holds `t`" means "the path is at or under one of `t`'s match roots `S_t`". `staticBool.ts` (parked, `filter-bool-parked` bf58d793) proves the static forms from that:

- **conjunction** `C = ∪ₜ {r ∈ S_t : r's path holds every term}`;
- an **exclusion** subtracts the negative's roots under each root;
- **exclusions alone** take P as the single root, less the negatives.

### Light terms (every term light at P)

- Read every term's first hits (one cached suffix-range read each, ≤ V rows, all in parallel), then filter as above. Exact.
- Latency is about the slowest term's cold read (~0.3–0.7 s), or ~0 when the terms are warm.

### Heavy terms (parked algorithm)

- **A positive term heavy at P** (a rollup) is read under each candidate that the light terms give, at the candidate itself or grouped at an ancestor. The read descends through rollups whose remainder rules a child out.
- **A negative term heavy under a root** is enumerated through its rollup where it can be: a kept child holding the term is read by `at`, one that doesn't is recursed into, and only when the remainder is zero. Otherwise the path becomes a **cut**: its rollup total is subtracted and nothing inside it is drawn.
- **It declines when:**
  - every positive term is heavy at P (there are no candidates to start from);
  - the work passes 24 reads or 400K hits;
  - a second negative term would cut;
  - an owner pool meets a cut (moot under indexed-only, which already refuses scopes);
  - a date is uncovered.

**How often it declines on gcs.** The first rule dominates, and it depends on P:
- **At the fleet root**, a term with more than 100K roots is heavy. That is 78,361 of the 82,616 long members, plus 986 short literals. So two common words together (`train test`, `.json tmp`, `final step`) decline at the root.
- **One bucket down**, most of them are light: of the 14 plausible terms heavy at the root, only `.json`, `.parquet` and `shard` are heavy in all six buckets; `train`, `test` and `final` are heavy in one bucket, and `eval`, `checkpoint` and `model` in none.
- **Specific terms** are mostly not members (`ckpt`, `llama`, `fineweb`, `dclm`, `wandb`), so `<specific> <common>` and `<common> -<specific>` are served at every P.

The fixture sweep (668 / 694 / 498 of 726 served with the light / mixed / heavy sources) is not a real-data rate. Measuring the real rate is step 1 of shipping.

### Making declines rare

The first three changes need no index work.

1. **Rewrites before reading:**
   - a positive term containing another drops the shorter one (`json .json` → `.json`);
   - a term in P's own path is true everywhere and is dropped;
   - two terms that alias to one drill class (`aliases.parquet`: identical root sets) collapse into one.
2. **Co-descent** for "every positive term heavy at P":
   - read every positive term's rollup at P; a child is a candidate only where every term's cell is non-zero on the dates;
   - descend into the candidates (one batched read per level) until some term is light under each one, then run the parked algorithm there;
   - **exact** wherever every rollup it passes through has a zero remainder on the dates, or the term holding the remainder has a zero cell there;
   - **declines** only where two terms' remainders are both non-zero (the "N others" child sets can't be intersected).
   - The cost is about 2–3 round trips (0.6–1.5 s cold), under the same 24-read budget.
3. **OR by inclusion–exclusion.**
   - Per object (and so per child, per date): holds `a` or `b` = holds `a` + holds `b` − holds both.
   - So `a|b` = A + B − (A ∧ B), and A ∧ B is the conjunction above. Any OR whose AND-groups are answerable is answerable, by summing with signs.
   - The union of the two root forests is drawn as the outermost roots of `S_a ∪ S_b` (light); with a heavy term it is drawn per child from the summed rollups.
   - Today `a|b` is `unsupported-terms`.
4. **Pair rollups** (precomputed `(a ∧ b, dir)` for popular pairs): not recommended. The pair space is unbounded, and the rewrites plus co-descent cover the natural cases.

### UX for a declined case (indexed-only)

- **A new code `too-broad`.** Message: "“train” and “test” each match too many files here to combine. Open a folder, or make a term more specific."
- **Each term's own total at P** is shown beside the message (from the drill: one read per term, already done by the attempt), with links that apply one term alone.
- **The map stays on its last good view**, and the filter box keeps the query; this is the existing refusal rendering (`refusalOf`, c1a59162).
- **Diff and series** decline per date the same way. A series names the declined dates the way it names `unindexed` ones today.
- Off indexed-only, a decline falls back to the path-store walk, as the parked code already does.

### Index changes and cost

- **None for the forms above.** Per-owner rollups (`(q, dir, usr)` cells, one more rollup set per kind; parked spec) matter only if scoped filters are opened up. Indexed-only refuses scopes, so skip them.

### Reader changes, tests and effort

- **Rebase the parked branch** onto the tiered drill (`475515b4`: `Drill` reads base + run tiers). `Drill.at` (one path's root rows, added by the parked commit) must combine tiers by smallest `vt` and take each tier's alias, as the reader's `hits` does.
- **Widen `rejectAst`** to accept `alts.length ≥ 1` of `sub` literals without `/`, with negatives, and add `too-broad`.
- **Tests:**
  - the parked sweep (`staticBool.test.ts` against `testBrute.ts` brute force over `objects.json`), extended with OR, the rewrites and co-descent, and run over a tiered fixture (`fixtures/static-drill-runs`);
  - diff and series tests;
  - real data: ~100 compound queries at 10-08, 10-01 and one run date against the batch reference (`wt/hot-preview/tmp/sf-acc/ref.py`), recording served / declined / cut counts per P depth. That count is the decline rate this section estimates.
- **Effort:**
  - ship the parked forms on the tiered drill: **2–3 agent-days**;
  - rewrites, co-descent and OR: **+2–3 agent-days**.

## 2. Anchors: `^tomat` (starts with) and `.json$` (ends with), per name

### Semantics

- `^q` matches a segment (basename) that starts with `q`; `q$` one that ends with `q`; `^q$` an exact name.
- A path matches when **some segment** matches. That predicate is monotone down the tree, so match roots work as today.
- A **match root** is a path whose name matches and **none of whose ancestor segments match**. This test is per segment, not "the lowercase parent contains `q`": `tomato.txt` under `big-tomato/` is a root of `^tomat`, because `big-tomato` doesn't start with `tomat`.
- **Path-store fallback, off indexed-only.** The same predicate as segment regexes: `(^|/)q[^/]*` for `^q`, and `q(?=/|$)` for `q$`.
  - The existing full-path syntax already says something close: `/q` means a segment below the bucket that starts with `q`, and `q/` means a directory whose name ends with `q`.
  - `^`/`$` are the clearer, kind-agnostic spelling.
- **Grammar.** A leading `^` or trailing `$` outside quotes is an anchor. Quoted, they are literal (`"^a"`).
  - A new matcher shape: `{ kind: 'sub', text, start?: true, end?: true }`.
  - In the `regex` syntax, `^` anchors the bucket, not the name; the help says so.
- **Under 3 characters** (`gz$`, `^a`): declined (`anchor-too-short`: "add the dot: `.gz$`"). The shards hold suffixes of ≥ 3 characters only, and a short anchored catalog would need its own short drill (~24 VM-hours today). Most extension queries are ≥ 3 with the dot.

### Ends with (`q$`)

**Light.** The rows are exactly `s == q`, the head of `q`'s range: the groups from the first with `s_max ≥ q` to the first with `s_min > q`.
- Each version holds that suffix at most once, so there is no dedup.
- Keep the row if no ancestor segment (lowercased) ends with `q`.
- The group index bounds the sub-range, so the dispatch needs no catalog. `.index$` (≤ 8K rows) is light although `.index` is a contains member.
- Every run tier is read the same way, combined by smallest `vt`.
- **This is no index change.** Latency is at most today's light read, since the sub-range is never larger than the contains range.

**Heavy.** 3,563–4,007 exact suffixes hold more than V rows, about 2,150 classes. They include the extensions people actually type: `.json`, `.parquet`, `.gz`, `.jsonl.gz`, `.mp3`, `.npy`, `.txt`, `.npz`, `.jsonl`, `.bin`, `.zst`, `.csv`, `.pkl`, `.zarr`, `.log`. Without this work they decline (`term-too-common`) at the fleet root and in most buckets. The contains rollups can't serve them: `.json$` is not `.json` (that also counts `.jsonl`, `.json.gz`), and the first-hit tests differ.

The key observation: **the suffix shards already are the anchored roots file.** Rows with `s == q` are sorted by `path` (sort `(s, path, usr, vf)`), so `q$`'s candidates under P are the contiguous key range `[(q, P/), (q, P0))`. That is the drill's roots layout, holding every version whose name ends with `q`, roots and non-roots alike. So heavy ends-with needs no roots copy, only:

1. **Path keys in the group index.** Per row group, its first and last row's `(s, path)`: `(s_min, p_min, s_max, p_max)`, the drill index's `(q_min, k_min, q_max, k_max)`.
   - Dispatch for `(q$, P)`: groups meeting `[(q, P/), (q, P0))`, their rows summed.
   - At most `R + 2·8,192` rows: read them, keeping first hits under P (exact).
   - Over that: the directory is heavy, and read 2 applies.
   - Source: the first and last row of each group (one `path` decode per group). For the 957,900 groups with `s_min = s_max`, footer statistics give the same.
   - Size: 1.6M groups × 2 paths × ~128 B ≈ 410 MB raw. Adjacent bounds share long prefixes, so ~50–100 MB zstd; as a parallel `sidecar-keys/` per shard, ~+200–400 KB per shard on the per-isolate group index.
   - Runs: `write_run_shards` writes the same keys.
2. **Anchored rollups** for heavy `(q$, dir)`: more than R `s == q` rows under `dir`, all tiers summed.
   - The heaviness test counts rows, a bound on roots, so the dispatch and the builder agree. This is the drill-append `S` rule.
   - Cells as the contains rollups: `(q, dir, kind, child, vf, b, o)`, K = 256 kept plus a remainder, built from first hits only. The rollup at `dir = ''` doubles as the anchored catalog (children = buckets).
   - Estimate: the contains drill has 160,813 heavy `(q, dir)` for 34.2B roots; scaled to ~3.7B anchored rows, that is ~17K heavy dirs and ~2M cells, ~12 MB.
   - Aliases: chains with equal counts share one set (digest-checked, as `drill aliases`).

**Cost (ends-with heavy):**

| Item | One-off | Storage | Per scan |
|---|---|---|---|
| exact-suffix census (counts of every `s` with > V rows, from the shards' `s` column) | ~$0.5 (32 spot × ~5 min, the `census` stage's reader) | – | the run's `s` counts, seconds |
| path keys for the base sidecar | ~$1 (32 spot × ~10 min over 136 GB in-region) | ~50–100 MB on GCS and R2 | written by `shards`, ~KB |
| anchored rollups | ~$1–2: 3.7–7.3B rows through the drill's level loop; the long drill did 34.2B in 28.5 task-hours | ~12 MB | ~12M of a run's 44M suffix rows have a heavy `s`, about a seventh of the drill run's 84M delta roots: +5–10 min on the drill stage, +$0.02–0.05/day, MBs |
| R2 egress | < $1 | | negligible |

Total: **~$3–4 one-off, ~0.1 GB, +$0.05/day.**

### Starts with (`^q`)

**Light, non-member `q`** (its contains range ≤ V):
- Read the range as today and keep the rows where `s` equals the whole lowercased name (the suffix from position 0).
- Keep the row if no ancestor segment starts with `q`.
- No index change.

**Member `q`.** The range exceeds V and the whole-name rows are scattered through it, so even a `^q` with few rows can't be read. That covers `^train`, `^step`, `^model`, `^config`, `^results`, `^shard`, `^part-`, `^chunk`, and every 1–2 character prefix. Without an index they decline, and these are the starts-with queries people type, so light-only starts-with is of limited value.

**The name index** (`names/`): `(n, depth, path, usr, vf, vt, size, n_files)` with `n` = the lowercased basename, sorted `(n, path, usr, vf)`.
- It holds one row per coalesced version (798M), the suffix shards' position-0 rows. `^q` is the contiguous range `n ∈ [q, q⁺)`, bounded by its own group index:
  - ≤ V: read and filter; exact.
  - Over V: heavy `^q`.
- `^q$` (an exact name) is `n == q`, sorted by path, so it is also the drill layout, as ends-with is.
- Size ~8–12 GB (10–15 B/row; the shards average 10.3).
- Build from `cintervals` (4.7 GB): one sort, ~$1–3 on Batch. R2 egress ~$1–1.5. Storage ~$0.2/month on R2 + GCS.
- Per scan: the run's `cdelta` (~1.7M opened + closed versions) → ~30 MB, in the `shards` stage.

**Heavy `^q`** needs the full drill machinery: catalog, roots and rollups per class.
- Rows of `^q` under P are not contiguous in the name index (sorted by name first), so unlike ends-with it needs roots files.
- Size is unknown. A plausible range is 2–8B roots after aliasing (prefix chains like `part-0000…` alias heavily), 15–50 GB, $3–10 to build and $2–6 egress, plus a per-scan drill delta like the contains drill's.
- **Census first.** Prefix counts over the name index: one VM, minutes, ~$0.2, once the index exists. **Ask main before spending.**

### Reader changes, tests and effort

- Parser and matcher: anchors.
- `staticLiteral` → `staticTerm` (`{ text, start, end }`).
- `StaticNames.read`: an exact-range mode.
- An anchored `FirstHits` (segment tests).
- Cache keys: `^q`, `q$`, `^q$` are distinct entries.
- `Drill`: an anchored set (rollups, plus roots from the shards via the keyed sidecar).
- `filter.ts`: the segment predicate for the fallback.
- **Tests:** brute force over `objects.json` with anchored predicates, including a root below a contains-root (`big-tomato/tomato.txt`) and names ending in the term at several depths. Python: anchored census, keys and rollups equal brute force at small V and R. Real data: `.json$`, `.parquet$`, `.zarr$`, `.safetensors$` at the fleet root, per bucket and at depths 2–5 on two dates against `roots drill-brute` with the anchored predicate.
- **Effort:**
  - anchors, light only: **1–1.5 agent-days**, $0;
  - ends-with heavy: **3–4 agent-days** including the per-scan stage and verification;
  - the name index plus light starts-with on members: **2 agent-days**;
  - heavy starts-with: **3–5 agent-days** after the census.

## 3. Dirs vs files (`is:file`, `is:dir`)

### Can the postings tell?

**No.**
- Suffix rows and drill roots carry `(depth, path, usr, vf, vt, size, n_files)`.
- The intervals had `kind`, but coalescing dropped it, and gen `2026-10-08`'s intervals are deleted (only `cintervals/` remain).
- No path ends in `/`.
- `n_files = 1` holds for every file and for ~34% of the sampled dirs.
- A dir could be recognized as "has a child", but that is a global join, not something a reader can see in a suffix range.
- The view's per-root details (`ROOT_DETAILS` = 48, best effort) can't filter exactly.

So `kind` needs a bit in the index.

### Semantics

- **`is:dir q`**: the outermost **directories** whose name contains `q`, with their whole contents. Every ancestor is a directory, so these are exactly today's first hits restricted to directories: a subset of the roots.
- **`is:file q`**: two candidates.
  - **F2 (recommended)**: every **file** whose name contains `q`, including files inside a matching directory. Files don't nest, so nothing is counted twice. This is what `find -type f -name '*q*'` means.
  - **F1**: file first hits only, a subset of the roots. Cheaper, but it hides `x.json` inside `y.json.d/`.
  - **Where they differ:** for most heavy terms, roots ≈ range (median 1.00, aggregate 0.93), so little. For path-like words, by 25–67% (`eval`, `train`, `part-`, `checkpoint`, `shard`).
- **v1 scans** (61 of the 70 base scans) hold no file rows. `is:file` on them declines (`no-file-rows`: "file-level data starts 2026-09-30"); `is:dir` works on every scan.
- **Syntax:** `is:file` / `is:dir` tokens (GitHub-style), combinable with anchors (`is:dir .zarr$`). `ckpt/` already reads as "a directory ending in ckpt" in full-path terms, but that is ends-with, not contains.

### Light terms (with a kind bit)

- **`is:dir`**: filter the first hits by kind.
- **`is:file` F2**: drop the parent test for file rows. Every file row whose name contains `q` is a hit (deduped by `(path, usr, vf)` as today). The range already holds these rows; `FirstHits` discards them today.

### Heavy terms

- Roots files gain the bit and sort `(q, kind, path, usr, vf)`, so each kind is its own contiguous range with its own dispatch bound. A dir-only drill is usually far smaller, so far fewer `(q, dir)` are heavy for it. An all-kinds read becomes two ranged GETs.
- Rollups split by kind: cells per `(q, kind, dir, child)`. The all-kinds view sums the two series, or keeps a third `all` series to stay one read; the kept-K choice is per series.
- **F2 needs extra rows:** the matching files under a matching directory are not roots. Bound: Σ range − Σ roots = 6.0B of 84.9B range rows (7%, multiplicity included), so ≤ ~2–3B extra rows after aliasing, ~15–20 GB. F1 needs none.

### Index changes and cost

1. **Recompute `kind` per version.**
   - Rebuild the intervals from the scans with `kind` kept (38 min on 32 spot, ~$5).
   - Coalesce on `(size, n_files, kind)` (~$2). This adds versions only where a path changed kind (rare); v1 rows are all `dir`.
2. **Suffix map and shards** with a `kind` int8 column: ~$8–10. ≤ 1 bit/row before RLE, so ≤ 1.65 GB over 13.2B rows, likely 0.3–1 GB (sibling files share a kind), about +0.5% on 136 GB.
3. **Catalog** with per-kind cells (~1.5× the cells, ~40 MB): ~$3.
4. **Drill rebuild:** ~$19–25. Roots +1–2 GB; rollups +150–300 MB; F2 +15–20 GB.
5. **R2:** a full generation copy (~435 GB, ~$52 egress), unless done at compaction (below).

Done alone, that is **~$90–100**: as much as a new generation. **The compaction already planned at counter level 5 (≤ 32 days) writes a new generation and a drill rebuild anyway.** Doing `kind` there adds only steps 1–2 and the columns: **~$15 marginal**. The runs must carry `kind` before that compaction: the append reads v2 scans, which have `kind`, so it is one more `ANSWER_COLS` column. Per-scan cost is unchanged.

### Reader changes, tests and effort

- Parser tokens; `Hit.kind`; the kind-aware fold; the drill's per-kind ranges and series; `no-file-rows`.
- **Tests:** brute force with `is:file` / `is:dir` over a fixture holding both kinds, a dir and a file at the same name pattern, nesting, and a v1 scan.
- **Effort: 3–4 agent-days**, plus the compaction it rides on.

## 4. Case-sensitive vs case-insensitive

Ryan: spec the options, not necessarily build them now.

### Semantics

- **CS literal `Q`:** a name contains `Q` exactly. A match root is a path whose name contains `Q` and no ancestor segment contains `Q`.
- **CS roots can sit below CI roots.** `Tomat` under `tomatoes/` is a CS root but not a CI first hit. So CS answers can't be derived from CI hits; they come from rows.

### Light (the CI range ≤ V)

- Read `lower(Q)`'s range (the same bytes as the CI query) and refold with case-preserving tests on `path`.
- **Exact.** Every name containing `Q` has lowercase suffix rows starting with `lower(Q)`, roots below CI roots included. `FirstHits` drops those rows only after reading them, so the refold sees them.
- **Unicode.** This relies on `lower(name) ⊇ lower(Q)` whenever `name ⊇ Q`. It holds for ASCII. Non-ASCII special cases (final sigma, `İ`) should agree between JS `toLowerCase` and Python `str.lower`, but that is untested, so restrict CS to ASCII `Q` at first or add the parity test (section 5, gap 5). The sample had no non-ASCII names.
- **Cache** under a separate key (`cs:Q`). No index change. Latency as a CI light read.

### Heavy (the CI range > V)

The CS rows can't be found without reading the whole CI range. Two options:

- **H0: decline** (`term-too-common`, with "case-sensitive search of a common term"). How often: every CS query whose lowercase is a member, i.e. any common word typed with a capital (`Train`, `README`, `SUCCESS`).
- **H1: an uppercase sub-index**, sorted by the case-preserving suffix: the suffix rows whose case-preserved suffix contains an uppercase letter.
  - **Complete for any `Q` with an uppercase letter:** a name containing `Q` at position `p` has an uppercase letter inside the suffix from `p`.
  - **Size:** ~4.9% of suffix rows in the sample (clustered, so 2–20% is plausible): ~0.3–2.6B rows, ~4–30 GB.
  - **Build:** about a tenth of the suffix build (~$1–3). R2 egress $0.5–4. Per scan: ~5% of the run's rows, seconds.
  - **What it gives:** CS `Q` is a contiguous range of the sub-index. Light when ≤ V (exact, smaller than the CI range). Heavy CS literals (the `.SUCCESS` / `_SUCCESS` families, TitleCase podcast names) need a CS catalog and drill. Those alias to the CI class when the root sets are equal (digest compare), so only classes where case actually splits the set are built. Count unknown: a CS census over the sub-index (~$0.2 once built) prices it. **Ask main first.**
- **Lowercase with CS forced on** (`cs=1` on `readme`): not in the sub-index. Light → refold; heavy → decline.

### UX options

- **(a) Smart case (recommended):** CI unless the query holds an uppercase letter (ripgrep, vim `smartcase`). A lowercase query is today's search; only a capital opts in.
  - **Back-compat:** today `TOMAT` is CI (`indexedOnly.test.ts` asserts it passes as a literal), and existing links with capitals would change meaning. Few exist; uppercase queries are rare.
- **(b) A toggle** (`Aa` in the filter box, `cs=1|0` in the URL), defaulting to CI. Explicit, but a query with capitals still silently ignores case unless the toggle is on.
- **(a) + (b):** smart case by default, with the toggle (and `cs=0`/`cs=1`) overriding. This is VS Code's behavior.
- **Under indexed-only:** a CS heavy decline says so and offers the CI result in one click (`cs=0`).

### Effort

- light CS with smart case and the toggle: **1–1.5 agent-days**, $0;
- the uppercase sub-index: **2 agent-days**;
- heavy CS (census, CS catalog and drill with CI aliasing, per-scan): **3–5 agent-days**.

## 5. Quotes and spaces

### What works (verified)

- **Quoted literals parse as one term.** `"a b"` → `sub('a b')`, `"-draft"` → `sub('-draft')`, `-"a b"` → a negative `sub('a b')`, and `"a|b"`, `"x*y"` are literal (`querySyntax.test.ts`).
- **Indexed-only accepts them.** `rejectQuery('"final ckpt"')` is null (`api/indexedOnly.test.ts`), and `INDEXED_HELP` advertises `"…"` as "literal, spaces included".
- **The static index matches spaces.** Suffix rows are built from the whole lowercased name, spaces included: the base's first row group starts at the suffix `'             0.5-0.5-7e090d'` and holds `' the drawer_20260127_145413.mp4'`. So a space-holding literal is an ordinary key:
  - light if its range ≤ V;
  - a member if heavy;
  - a short literal through the short catalog and drill if ≤ 2 characters (`" x"`; the short pass takes every character and pair of a name, spaces included).
- **Leading spaces inside quotes are kept:** `" abc"` → `sub(' abc')`; the outer `trim()` stops at the quote.
- **Tabs and newlines inside quotes are literal**; outside, any `\s` separates terms.

### Gaps

1. **No static test holds a space.** The `static-filter` / `static-names` fixtures' `TERMS` have no space, so the path from a space literal to the suffix range to first hits is untested end to end. **Fix:** add names with inner, leading and doubled spaces to `fixtures/static-filter/gen.py`, plus terms `"a b"`, `" lead"`, `"  "` (two spaces: a short literal).
2. **An unterminated quote loses trailing spaces.** `"abc ` is trimmed to `"abc` → `abc`; a terminated `"abc "` keeps the space. **Fix:** trim only outside an open quote (scan first, then trim the unquoted tail).
3. **No way to search for a `"`.** `"` always toggles quoting, so `a"b` → `ab` and `"say "hi""` → `say hi`. **Fix:** `\"` inside quotes, or a doubled `""` (CSV style); `\\` for a backslash. Names holding `"` are probably rare (not measured).
4. **A literal that is only spaces after the parse** (`" "`) is a one-character short literal. It is valid, but the map would show every name holding a space; the help can say so.
5. **Lowercasing parity** with the Python build (`str.lower`) for non-ASCII is assumed, not tested. Add a parity test over a few special cases (final sigma, `İ`, `ẞ`).

**Effort: 0.5 agent-day, $0.** No index change.

## Summary

| Option | Index work | One-off $ | Storage (GCS and R2 each) | Per scan | Effort (agent-days) | Declines left |
|---|---|---|---|---|---|---|
| 5 quotes / spaces | none | 0 | 0 | 0 | 0.5 | none new |
| 1 compound (parked + rewrites, co-descent, OR) | none | 0 | 0 | 0 | 2–3 + 2–3 | every positive term heavy and remainders overlapping; budget |
| 2a anchors, light only | none | 0 | 0 | 0 | 1–1.5 | heavy `q$` (~2–4K suffixes, the common extensions); `^q` for any member |
| 2b ends-with heavy | sidecar path keys + anchored rollups | ~3–4 | ~0.1 GB | +$0.05/day, MBs | 3–4 | < 3 characters |
| 2c name index (starts-with on members, exact names) | `names/` | ~2–5 | ~8–12 GB | ~30 MB | 2 | heavy `^q` |
| 2d starts-with heavy | anchored catalog + drill | ~5–16 (census first) | ~15–50 GB | a drill-delta's share | 3–5 | – |
| 3 kind | `kind` bit everywhere | ~15 at compaction (~95 alone) | +1–3 GB (F2: +15–20) | 0 | 3–4 | `is:file` on v1 scans |
| 4 CS light | none | 0 | 0 | 0 | 1–1.5 | CS on any member |
| 4 CS sub-index + heavy | uppercase sub-index (+ CS drill) | ~2–8 | ~4–30 GB (+ CS drill) | seconds | 2 + 3–5 | lowercase `cs=1` on members |

## Recommended order

Ryan's goal: plain substring vs path segments must be seamless first; extensions after.

0. **Plain substring, seamless** (the prerequisite, in flight elsewhere):
   - schedule `runs add` (daily light runs + drill runs), so no indexed scan declines a heavy literal;
   - land the first compaction plan;
   - flip prod.

   No extension should ship before a plain heavy literal is answered on the newest scan.
1. **Quotes and spaces** (0.5 day, $0): close the gaps and test what is advertised.
2. **Compound, light-and-parked** (2–3 days, $0): `ckpt -tmp`, `llama sft`, `-tmp` are high-value and exact today for specific terms. Ship with `too-broad` UX and the real-data decline count; then rewrites + co-descent + OR (2–3 days) if that count says declines are common.
3. **Anchors, light only** (1–1.5 days, $0): `.safetensors$`, `.pt$`, `.ckpt$`, `.index$` and rare `^q` work at once. Heavy ones decline with a clear message.
4. **Ends-with heavy** (3–4 days, ~$4 + $0.05/day): extension queries are likely the commonest real anchored use, and the shards already are their roots, so this is the cheapest heavy extension by far.
5. **Kind**, folded into the first compaction (~$15 marginal, 3–4 days): `is:dir` is a pure subset filter; decide F1 vs F2 for `is:file` then.
6. **Name index → starts-with on members, exact names** (2 days, ~$3), then **heavy starts-with** only if the census (ask main) shows it is affordable.
7. **Case sensitivity: park.** If wanted, light-only smart case is 1–1.5 days at $0; heavy CS only after the uppercase census.

## Open questions for Ryan

- Syntax:
  - `^q` / `q$` (proposed) vs reusing `/q` / `q/` (full-path terms that already mean nearly this)?
  - `is:file` / `is:dir` vs `type:f` / `type:d`?
- `is:file`: F2 (every matching file) or F1 (file first hits only)?
- Case: smart case by default, accepting that `TOMAT` changes meaning, or a toggle only?
- `too-broad`: show each term's own total at P with "apply alone" links, or just the message?
