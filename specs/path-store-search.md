# Path-store search: a segment-name index for the filter view

**Status:** implemented at fixture scale, 2026-10-01; the filter syntax (§1: AND, NOT, `*`, quoting; regex a hidden fallback) replaced regex 2026-10-02 (branch `seg-idx`, off `cloud`); pluggable syntaxes (§1.1: `simple`, `regex`), a 3-character minimum term, and explicit `partial` / `approximate` flags instead of silent cuts 2026-10-02 (branch `query-syntax`); writer behind `-S`, reader on whenever a generation has the sidecars. Search performance (§3.2) 2026-10-02 (branch `search-perf`): merged range reads, read/memory/wall budgets, a per-isolate result cache, and **layout v2** (§2: the store's rows name-major in place of the names file); the reader serves v1 and v2 generations. gcs build and request costs unmeasured (§7). Base work (`cloud`). Builds the "vocabulary + block index + trigrams" tiers of [diff-and-search] §"The (prefix × query) product space" for the **served** path store ([path-store]); the August sidecar (`src/disk_tree/sidecar.py`, removed from `cloud` in `360be7d`) served only the local engine.

## 0. Problem

The filter view (`q=` on `/api/subtree` and `/api/diff`, `view.ts` `readView` "the filter view") finds its **match roots** — the outermost paths under the view root whose full path matches the query — by reading rows and testing each path (`filter.ts` `matchRoots`). On a store generation there are no coarse tiers, so phase 1 is the plain view's thresholded read of the whole subtree (`readSubtree` at `thrAt`), then `matchRoots` over what came back. Measured on gcs.oa.dev (2026-10-01, ~777M rows per scan):

- 2–5 s per filtered view, most of it decoding rows that don't match;
- matches below the read's byte threshold are invisible (a small `ttl=7d` dir under a big bucket never comes back);
- a query with no match falls through to the widest read the view can make; on old v1 scans that read whole buckets and hit Cloudflare's 1102 (CPU/memory).

A Worker has ~128 MB and limited CPU; a request should read a few MB. The fix is an index from the query to the rows that can be match roots, so phase 1 reads only those.

## 1. Query semantics (what the index must reproduce)

The default syntax, `simple` (§1.1), shared by the server and the client. Case-insensitive throughout; every term is tested against a node's **full index path** (`bucket/dir/sub`). Adopted 2026-10-02 in place of regex as the filter's syntax; the box's placeholder is a short example ("filter paths, e.g. ckpt -tmp") and a `?` beside it opens the generated help (§1.1).

| syntax | meaning |
|---|---|
| `tomat` | substring anywhere in the path |
| `a\|b` | OR; binds looser than AND: `a b\|c` = (a AND b) OR c |
| `a b` | AND: the path contains every term, in any segments (`ckpt final` matches `x/ckpt/run/final` and `x/ckpt-final`) |
| `-x` | NOT: nothing whose path contains `x` counts (below) |
| `*` | any characters within one segment (`[^/]*`), also in a term with `/` (`ckpt*final` matches the name `ckpt-run-b-final`) |
| `"a b"` | a quoted term is literal: spaces, a leading `-`, `\|`, `*` |
| `…/…` | a term with `/` is a substring of the full path, like any other |
| `/…/` | a JS regex (flag `i`) over the full path — an **undocumented fallback**, never served by the index (§5) |

A blank alternative (`a|`) adds nothing; an empty quoted term is dropped; a lone `-` is a literal term (and so too short, below).

**Minimum term length.** Every positive term needs at least 3 literal characters in a row — for a `*` term, its longest piece — since the index is trigram-based; exclusions (`-x`) are exempt, and so is the `/…/` fallback. A query that fails is not run: the parse returns a typed error (`short-term`, "type at least 3 characters (“gr”)"), which the box shows inline and the API answers with a 400. A term with `/` counts all its characters, so `abc/de` passes while its last-segment tail (`de`) has no trigram and is answered by the names scan (§4.2).

**The predicate.** A query is `pos ∧ ¬neg` on the lowercased path: `pos` = OR over alternatives of AND over their positive terms; `neg` = OR of the NOT terms. **Negatives apply to the whole query**, whatever alternative they were written in (`a -x|b` excludes `x` from `b`'s matches too); an alternative with no positive term — a query of only negatives — makes `pos` everywhere true ("everything except"). Both halves are monotone along a root-to-leaf chain (a substring of a prefix is a substring of every extension), so the predicate is an interval on every chain: false, then true from the first path where `pos` holds, then false again from the first path where `neg` does.

**Match roots** under view root `P` are the paths strictly under `P` where the predicate holds while it holds on no strict ancestor below `P` — on each chain, the first `pos` path, unless `neg` already holds there. If `P` itself matches, `P` is the single match root.

**NOT subtracts.** Under each match root `r`, the **excluded paths** are the outermost descendants where `neg` holds. `r`'s total is its own total minus theirs (scoped by the page's owner/class axes like any aggregate), every node between `r` and an excluded path is likewise net of the excluded paths under it, and the excluded paths and their subtrees are not drawn — so the treemap's `(other)` fold (P − Σ kept) is computed on the net values with no new arithmetic; an excluded direct child also leaves its parent's child count. `/api/subtree` returns them as `excluded`; `matched` is net. A filtered diff subtracts each side's own excluded paths, including from the point lookups it makes on the other side.

**Lemma (what the last segment of a path that becomes true must satisfy).** Let `p` be a path where a monotone term set becomes true (a match root: some alternative's AND holds at `p` but not at `parent(p)`; an excluded path: some NOT term), `s` its last segment. Some term of that set holds at `p` but not at `parent(p)` (a prefix of `p`'s string), so its occurrence touches `s`:

- a slash-free term (with or without `*`, which never matches `/`): the occurrence lies inside `s` — `s` contains the term (for `*`: matches it as an unanchored `[^/]*` pattern);
- a term with `/`: the occurrence's last `/` is the separator before `s` (no `*` can supply it), so `s` **starts with** the part after the term's last `/` (pieces joined by `[^/]*`, anchored). A term ending in `/` constrains `s` not at all (every child of a dir named `…t` qualifies); the index does not serve it (§5).

So every match root's — and every excluded path's — last segment is in a **candidate name set** computable from the query alone: for match roots the union of the positive terms' candidates (AND is a union here, the exact filter does the rest), for excluded paths the NOT terms'. The index maps a query to that set, the set to rows, and the rows' full paths are tested with the very predicate `parseQuery` returns — results are exact by construction, never "close".

### 1.1 Syntaxes

Parsing is separate from evaluation. A **syntax** (`site/functions/_lib/queryAst.ts` `QuerySyntax`) is `{ id, parse(q), describe() }`: `parse` returns a syntax-neutral **AST**, null ("nothing to filter by"), or a typed error (`short-term` | `invalid-regex` | `unknown-syntax`); `describe` returns the help the box renders. The AST is `{ alts: Matcher[][], neg: Matcher[] }` — an OR of AND-groups of positive matchers, AND NOT any negative matcher (negatives are the whole query's, per the decision in §7) — and a `Matcher` is `sub` (a lowercase substring), `glob` (literal pieces joined by `[^/]*`) or `regex` (a full-path JS regex, flag `i`). Everything downstream reads only the AST:

- `pathQuery.ts` `compileQuery(ast)` → the path predicate (with its `pos` / `neg` halves);
- `searchQuery.ts` `planPositive(ast)` / `planNegative(ast)` → the index plans (§4.1), null for an unindexable matcher (a `regex`, a term ending in `/`) → the thresholded read, flagged `approximate` (§5);
- `view.ts` takes the NOT-subtraction inputs from the predicate's `neg` and `planNegative`; `search.ts` and `buildDiff` see only the predicate and the plans.

The registry (`querySyntax.ts` `SYNTAXES`) has two:

| id | what a query is |
|---|---|
| `simple` (default) | the terms of §1, the `/…/` fallback included |
| `regex` | the whole query is one full-path JS regex (flag `i`); no terms, no exclusion, no minimum; never served by the index |

**Selection: `qs=<id>`**, on the page URL and on `/api/subtree` / `/api/diff`; absent, the deployment's default — `QUERY_SYNTAX` on the Functions, `Store.querySyntax` on the client (they mirror each other like `STAGING` / `Store.staging`) — else `simple`. A `qs=` was chosen over an in-box prefix (`re:…`): any prefix is itself a valid regex and a plausible path substring, so it would collide with both syntaxes' own text; a separate parameter leaves the query text exactly what the user typed, keeps every existing URL (`?f=…`, `q=…` with no `qs`) meaning what it did, and gives the help card a place to switch syntaxes (it sets `?qs=`, cleared when it picks the default). An unknown `qs=` is a 400 (the box shows it). The client always sends the syntax it parsed with, so the box's help and the server agree even if the two defaults drift. Cache keys carry `qs`.

**Errors are not "no matches".** The client parses as the user types; a query that doesn't parse is shown under the box (red) and not applied. The API answers a bad `q=`/`qs=` with `400 bad query: <message>`, before auth or any read, and the box shows that message too.

**Help** is generated: the `?` beside the box is a pinnable floating-ui tooltip (`src/QueryHelp.tsx`) rendering the active syntax's `describe()` — a syntax picker, a one-line summary, a table of forms with one example each (`simple`'s `-x` row: "exclude, like GitHub search"), and notes (the minimum length, NOT's scope). A new syntax brings its own help.

## 2. Artifacts (per index generation, beside the `path` sort; zero D1 rows)

Written by `disk_tree.find.search.write_search` from the generation's `path-index.parquet` (any sort with `path` and `size` works). Layout **v2** (2026-10-02, §3.2), three files under the generation dir `index/<gen>/`:

### 2.1 `path-index.rows.parquet` — the store's rows, name-major

Every row of the `path` sort (owner slices included), with every one of its columns, after one more: `id` (int32), the vocabulary id of the row's last path segment (`name`). The vocabulary is every distinct last segment over the store (objects' basenames, dirs' names, buckets), numbered **`Σ size desc, name`** — an impact order (Anh & Moffat), so every candidate list is heaviest-first and any budgeted prefix of it is "the heaviest matches" (§4.3). Rows are sorted `(id, path-sort order)`: a name's rows are contiguous, so a candidate name is verified and its rows read in one place — no names file to consult and no `path` group to decode for it.

Written in row groups of `rows_rg_rows` (1024) rows: candidates scatter in impact order, so a query decodes about one group per candidate name, and smaller groups decode less (§3.2). `path` is plain-encoded, `usr`/`kind` dictionary-encoded, statistics on `id` only. The vocabulary itself (`id, name, Σ size`) is a temporary table of the build (DuckDB); the rows stream through pyarrow, which writes exact group sizes (DuckDB's `COPY` flushes in 2048-row steps).

### 2.2 `path-index.trigrams.parquet` — trigram → name-id postings

One row per (trigram, name) with the trigram in the name: `tri` int32 (`c0<<16 | c1<<8 | c2`), `id` int32, sorted `(tri, id)`. Trigrams are taken from DuckDB `lower(name)` and **only all-ASCII trigrams are kept**; the reader likewise extracts only all-ASCII trigrams from the (lowercased) query. That keeps the index sound across case-mapping differences between DuckDB, Python and JS (`'İ'` lowercases to `i` in DuckDB, `i̇` in JS; `'K'` (Kelvin) to `k` in both): an ASCII run in a name stays the same ASCII run under any of them, and extra non-ASCII trigrams are never looked up. Unchanged from v1 (same ids, same file).

### 2.3 `path-index.rows-search.parquet` — the directory

What makes the two data files range-readable without parsing their footers (a gcs-sized postings footer is ~10 MB of thrift): one row per data row group of both files, the `.groups.parquet` idea ([path-store] §1.6) generalized:

| col | |
|---|---|
| `file` | int8: 0 = rows, 1 = trigrams |
| `rg` | the data file's row-group ordinal |
| `row_start`, `row_end` | its absolute rows |
| `k_min`, `k_max` | its key range: `id` for rows, `tri` for trigrams |
| `rg_json` | the compact `RowGroup` the reader revives (`[num_rows, codec, [[data_page_offset, size, dict_offset|0], …]]`) |

Sorted `(file, rg)`, 512 rows per directory row group, zstd, statistics on `file, rg, k_min, k_max` only. Key-value metadata: `search_v=2`, both data files' flat schemas (`rows_schema`, `trigrams_schema`: hyparquet `SchemaElement`s), `names`, `rows`, `postings`, `path_groups` and `path_rows` (the `path` sort's, a consistency check).

A reader opens it with one tail read (colo-cached), prunes its own row groups by those stats, and decodes only the directory groups it needs — a directory under 1 MiB is read whole on first use (one round trip instead of two). The v2 directory has its own name so a reader that only knows v1 finds no directory and reads as if the generation had none (`approximate`, §4.4) instead of failing on a layout it can't read; the current reader opens `rows-search.parquet`, else `search.parquet` (v1), else serves without the index.

### 2.4 Layout v1 (read, no longer written)

Generations written before this layout (r2's, from 968d3b5's ingest on: 2026-10-02 the first) carry `path-index.names.parquet` (one row per name: `id, name, n, b, n_rgs, rgs` — `rgs` the comma-separated `path`-sort row groups holding the name's rows, NULL past `rg_cap` = 512 groups — in 2048-row groups), the same `trigrams.parquet`, and `path-index.search.parquet` (`search_v=1`, `file` 0 = names, `names_schema`). The reader verifies candidates on the names groups, then reads the verified names' `path` groups (§4.3). The fixtures keep a v1 copy (`fixtures/v2-search/path-index.{names,search}.parquet`, kept as committed) and `search.test.ts` runs both layouts.

### 2.5 Parameters

| | default | why |
|---|---|---|
| `rows_rg_rows` | 1024 | the decode unit; §3.2 measures 512 / 1024 / 2048 |
| `postings_rg_rows` | 16384 | postings row group ≈ 40 KB (a multiple of DuckDB's 2048-row step) |
| directory rows/group | 512 | as `.groups.parquet` |

## 3. Sizes (gcs, estimated; to be measured — §7)

Inputs: ~777M rows/scan; distinct dir-segment names ~15.5M, distinct basenames ~101M (probe 2026-08-19 over 594M objects; ~104M at 613M), so N ≈ 115–120M names.

| artifact | rows | est. bytes/scan |
|---|---|---|
| rows (v2) | ~777M | ≈ 1.3× the `path` sort (r2: 16.5 vs 12.5 MiB) — replaces v1's names file (~120M names, est. ~2–4 GB) |
| trigrams | ~2.4B (≈ 20 distinct ASCII trigrams per name) | ~5–7 GB (sorted ids, zstd) |
| directory | ~760k (rows at 1024) + ~150k | ~150–300 MB (`rg_json` per rows group); its footer ~1,800 groups |

v1 was ≈ 7–11 GB/scan beside ~27.5 GB of sorts (+25–40 %); v2 adds a second copy of the `path` sort's rows (≈ 1.3× its size) and drops the names file — the price of reading a candidate's rows where it is verified (§3.2). **Decision for the user** (v2's size on gcs). **Decision for the user:** the basenames are ~85 % of it; a `dirs`-only vocabulary (~15.5M names → ~1 GB) is exact for directory roots but cannot find an object whose own name is the only match (`.safetensors` under non-matching dirs) — so a dirs-only reader would have to keep today's fallback for every query. Not built; `all` is the only mode.

### 3.1 Measured: r2 (2026-10-02)

The r2 demo's daily ingest builds the sidecars since `cloud` 968d3b5 (`path-index -S`). Over a 1.24M-row `path` sort (12.3 MiB, 152 groups), `write_search` took 0.7 s at 604 MB peak RSS (locally, on the 2026-10-01 sort). It wrote names 4.2 MiB (222K names, 109 groups), trigrams 16.5 MiB (6.0M postings, 368 groups) and the directory 0.02 MiB: 20.7 MiB, about 1.7× the `path` sort. The whole `path-index -S` step on GHA took ~8 s.

Filter latency on r2.rbw.sh, comparing the 2026-10-01 generation (no sidecars) with 2026-10-02 (sidecars), same deploy. Each figure is the median `server-timing` total of 7 cold requests (random `minArea`); `m` is the number of matches.

| `q` | view | no index | index |
|---|---|---|---|
| `gbfs` | root, `depth=1` | 137 ms, m=0 | 63 ms, m=1 |
| `gbfs` | root, `full=1` | 469 ms, m=1 | 167 ms, m=1 |
| `tripdata` | root, `depth=1` / `full=1` | 48 ms, m=0 | 65 ms, m=1 |
| `2019` | root, `depth=1` | 53 ms, m=0 | 193 ms, m=185 |
| `2019` | root, `full=1` | 73 ms, m=16 | 70 ms, m=185 |
| `2019` | `ctbk`, `depth=1` | 52 ms, m=0 | 64 ms, m=179 |
| no match | any | 42–49 ms | 31–37 ms |

The non-index path misses matches: it found 0 for `tripdata` at the root and 0 or 16 of `2019`'s 185. The index finds them all, at the same latency or better, except `2019` at the root (193 ms). There the trigrams are unselective (369 candidates) and the 185 matches cost an extra group read.

### 3.2 Search performance (2026-10-02)

**Before** (`cloud` 89ba0fd, layout v1, r2.rbw.sh live, 2026-10-02): `q=2019`, root, `full=1`, 426 matches, took 8.9 / 12.3 s server time (9.5 / 13.1 s wall), `search;dur=7766/11437;desc="pos trigrams c369 n78 g60"`, `fetch` summed 2.5M / 3.5M ms. `json` (c1741 n29 g40): 8.0 s search, 14.6 s total. `parquet` (16,021 candidates): Cloudflare 1102 after 26.7 s. The 4× budget raise earlier that day had turned a 193 ms partial answer (185 of the 426) into this.

**Why.** The reads were already 32 at a time (`GROUP_READS`); the cost was their number. hyparquet plans **one GET per column chunk** when a read names its columns, and a store read projects 14 (`V2_ROW_COLUMNS`): 60 `path` groups were 840 GETs, plus 78 names groups each a colo-cache `match` + GET + `put`. A Worker holds at most **6 simultaneous connections** (fetch, Cache API, R2 and KV calls alike), so ~900 subrequests queued 6 at a time — the summed `fetch` (2.5M ms over a 9 s request) is ~300 reads waiting at once. The decoded-group LRU (24 MB) can't hold the 60 groups, so every request paid it again.

**Latency model.** `searchBench.test.ts` (opt-in: `SEARCH_BENCH_DIR`, a local copy of one generation) runs `buildView` over real files with every store GET and Cache API call taking one of 6 slots for 60 ms + bytes at 50 MB/s (cache calls 5 ms). On r2's 2026-10-02 generation it reproduces the live request: 10.5 s wall, `search` 10.1 s, `fetch` summed 2.8M ms, 840 `path` GETs, 78 names GETs. Rows below are its numbers for `q=2019` (root, `w=1408 h=896 minArea=19098`); "CPU-bound" is the same with zero latency (this laptop's CPU; a Worker's is likely somewhat slower).

| | GETs (search) | wall | `search` | CPU-bound `search` |
|---|---|---|---|---|
| before (v1, as deployed) | 840 `path` + 78 names + 5 | 10.5 s | 10.1 s | — |
| v1 + merged reads | 6 `path` (7.5 MiB) + 3 names (4.0 MiB) + 5 | 1.55 s | 1.35 s | ~0.8 s |
| **v2** (rows, 1024-row groups) | 11 rows (8.2 MiB) + 3 directory + 2 postings | **1.0 s** | **0.8 s** | **0.44 s** |

What changed, by direction:

1. **Concurrency → fewer reads.** Each row group is one range read of its projected columns' span (`index.ts` `chunkSpan`), and neighbouring groups' spans merge into one read (`planRuns`: gaps ≤ `RUN_GAP` 512 KiB are read and dropped, runs ≤ `RUN_BYTES` 2 MiB) — a read costs a ~60 ms round trip where 512 KiB more of it costs ~10 ms. At most `RUN_READS` = 6 in flight (more only queues and holds buffers). This applies to every `path`/`bysize` read (`readGroupsCached`: plain views, phase 2, diffs, point lookups), the search sidecars (`readDataGroups`, `readDirGroups`) and the rows (`readRows`). Budgeted reads hand runs over **in order** (`runsInOrder`: fetched 6 at a time, decoded in sequence), so a cut keeps a prefix — the heaviest names in v2.
2. **Fewer names reads → no names reads.** With merged reads v1's verification is 3 GETs, but what remained was CPU: 60 `path` groups × 8K rows decoded for 551 matching rows (~10 ms each; the `path` column is 340 ms of the 600, and **`fzstd` decompression ~300 ms of that** — pure-JS zstd at ~80 MB/s), plus 78 names groups (~125 ms). Decoding a group's `path` column first and the other columns only for groups with a hit, building rows only for the hits (`readGroupsAt`), saves ~10 %. The scatter is intrinsic: candidates are spread over impact order, so any layout keyed by id touches about one group per candidate, and decode cost ∝ groups touched × rows per group. v2 makes that the *only* decode — the candidate's rows are where its name is — in small groups: for `2019`, 154 rows groups / 158K rows (1024) vs 60 `path` groups / 491K rows + 78 names groups / 160K. Group size measured (same 369 candidates, pyarrow, zstd, plain `path`): 512 rows → 195 groups, 100K rows, 203 ms decode, 20.0 MB file; 1024 → 154, 158K, 315 ms, 17.4 MB; 2048 → 124, 254K, 493 ms, 16.0 MB. 1024 keeps the gcs directory at ~760k rows; 512 would halve the decode again for twice the directory.
3. **The pixel threshold — not built.** Folding sub-threshold roots into `(other)` without reading them needs each *match root's* aggregate, and a root is a property of the query, not of a name: a name's rows can sit under another match root (`…/2019/` holding `2019-01-24.….parquet`), under or outside the view root, or under an excluded path (NOT). A per-name `Σ size` only bounds the unread roots' bytes from above (nested rows double-count). And the response lists every root (`matches`, `matched` — the series sums them per scan), so unread roots would drop out of the list, not just the drawing. The bound could back a "≤ X more in N names" note on a cut result (later).
4. **A wall-clock budget, and a memory one.** `wallMs` (1.5 s per `searchRoots` call: the positive search and the NOT search each) is checked before each stage and each group; `keptRows` (50,000 matching rows) bounds the request's memory and the roots it returns. Either stops the search where it is, `partial` with a reason. `parquet` on r2 goes from 1102 to ~1.0–1.4 s (model), 50K matches, "more than 50,000 matching rows"; `json` likewise.

Also: a **per-isolate result cache** (`searchRoots`' `key`: the query's AST, positive or negative part; with the generation dir, the root and the limits): a filter's first paint (`depth=1`) and its full view run the same search back to back, as do re-renders at other widths, and the second is free. A result the wall clock cut is not kept.

**v2 on r2's data** (2026-10-02's `path` sort, built locally): `write_search` 1.3 s, 670 MB peak RSS; rows 16.5 MiB, trigrams 16.5 MiB (identical to v1's), directory 0.12 MiB (1,229 + 368 groups' rows) — 33 MiB, 2.6× the `path` sort (v1: 20.7 MiB, 1.7×).

## 4. How the Worker answers a query

### 4.1 Plan (pure; `searchQuery.ts`)

`planPositive(query)` and `planNegative(query)` return `null` (the index can't serve it → today's path) or an OR of **branches**, one per term (`termBranch`), each a trigram formula plus an exact name test:

- a term's literal pieces (split at `*`) give `AND(tri(piece))` over all pieces — the `*` itself constrains nothing;
- slash-free: test `name.toLowerCase()` contains the term (a `[^/]*`-joined pattern when it has `*`);
- with `/`: the same over the part after the last `/`, anchored at the name's start; an empty part (the term ends in `/`) → `null`.

`planPositive` is `null` for the regex fallback and when an alternative has no positive term (then `pos` holds at `P` itself, and `P` is the match root — no search needed); `planNegative` is `null` without NOT terms. Formulas simplify by absorption (`ckpt | ckpts` = `ckpt`).

A formula of `TRUE` (a term under 3 characters, or only wildcards and short pieces) is still served — by the names scan of §4.3, not trigrams.

### 4.2 Candidates

1. **Directory**: open `rows-search.parquet` (v2), else `search.parquet` (v1): the footer, then the directory groups whose `file`/`k_min`/`k_max` stats admit each needed trigram (a directory under 1 MiB whole).
2. **Postings**: a trigram whose postings span more than `triRgs` (32) row groups (> ~128k names) is unselective and treated as `TRUE` — sound, it only widens the candidate set; an AND keeps its selective children, an OR with an unselective child is `TRUE`. The selective trigrams' postings groups are range-read and intersected/unioned per the formula. A query whose formula ends up `TRUE` uses the names scan instead.
3. **Candidates' groups**: candidate ids ascending (= heaviest first) → the `file` 0 groups whose `k` range holds one (v2 rows, v1 names), at most `rowsRgs` (256) / `namesRgs` (128) of them; past it the search is cut ("the row read hit its budget (256 row groups)" / "the name search hit its read budget (128 name groups)") and keeps the heaviest. The **names scan** (formula `TRUE`) takes groups `0, 1, …` up to the same budget — "the heaviest names that match".

### 4.3 Rows, roots, guardrails

4. **Rows** — v2: the candidates' groups are read (merged, in id order) and each group's `path` column tested with the query predicate under `P`; the other columns are decoded, and rows built, only for the hits. v1: the names groups are decoded and each candidate tested with its branch's exact name test; the verified names' `rgs` are unioned in id order until `pathRgs` (96) `path` groups (a NULL `rgs` or the budget stops the union: "the row read hit its budget (96 path groups)"), and those groups read the same way (`readGroupsAt`, through the decoded-group LRU when a group is there). A cut is never silent: the view and `/api/diff` return `partial: true` and `partialReason`, and the box shows "partial results: …" beside the match count (§4.4).
5. **Roots**: each kept row's root is its **shallowest matching prefix** below `P` (computed from the path string). A root's rows are taken only when they are known **whole**: v2 — its name's id is below the first unread candidate group's `k_min` (a name's rows are contiguous, so only a name straddling into an unread group is partial); v1 — every group in its name's `rgs` was read (owner slices of one path can straddle a group boundary). Not truncated ⇒ every root's rows were read (by the lemma, its name is a candidate and all its rows are in the groups read), so the roots and their aggregates come straight from the rows — the same set `matchRoots` returns over all rows. Truncated ⇒ the roots are those of every matching row read (lighter names' included, when they share a group with a heavier one), and a root not known whole — or not read at all (an ancestor lifted from a deeper match) — is fetched with one `readAsks` point lookup on the `path` sort (≤ `liftGroups` = 120 groups; wider → those roots are dropped, with a reason). The lookup also covers the one non-truncated way a root could go unread — DuckDB's and JS's lowercase tables disagreeing on a name — instead of failing the request. A search **cut before it found any root** says nothing about the query, so the view falls back to the thresholded read of §5 rather than report "no matches" — flagged `partial` ("the search stopped before finding a match (…); showing a thresholded read").
6. **Excluded paths (NOT)**: the same search with the negative part's predicate and `planNegative`, under `P`, keeping the outermost hits that lie under a match root; their rows give the aggregates to subtract (§1). A cut NOT search marks the view truncated and `partial` ("exclusions: …"). Without the index they come from the rows phase 1 read, and in both cases any further outermost `neg` path among phase 2's rows is subtracted too (only reachable without the index).
7. **Phase 2** is unchanged: the roots' subtrees at one forest threshold (from the net matched total), attenuated per root, from `bysize`/`path` (`readSubtree`), rows at or under an excluded path skipped and every kept node net of the excluded paths under it.

**Budgets** (`SEARCH_LIMITS`; `ViewOpts.searchLimits` overrides), each a `partial` reason when hit:

| | default | bounds |
|---|---|---|
| `triRgs` | 32 | postings groups one trigram may span and still constrain (wider = unselective, `TRUE`; sound) |
| `rowsRgs` | 256 | v2 rows groups decoded (≤ 262K rows) |
| `namesRgs` | 128 | v1 names groups decoded |
| `pathRgs` | 96 | v1 `path` groups decoded (~800K rows) |
| `keptRows` | 50,000 | matching rows kept — the request's memory and the roots returned ("more than 50,000 matching rows") |
| `liftGroups` | 120 | groups the lifted roots' point lookup may touch ("N match roots too wide to look up") |
| `wallMs` | 1500 | per search call, checked before each stage and each group ("the search hit its time budget (1.5 s)") |

So a request reads, cold: the directory footer (cached per colo and isolate), the directory groups it needs (or all of a small one), the selective trigrams' postings groups (~40 KB each, ≤ 32 per trigram), and — v2 — ≤ 256 rows groups as merged reads of ≤ 2 MiB, 6 in flight; never an unbounded read, decode or result. **No match is cheap**: an empty candidate set, not truncated, returns "no matches" without touching the rows or the store's sorts.

### 4.4 Ordering, response

`/api/subtree`'s `matched` (the per-root list behind bulk actions and the series) is ordered **bytes desc, then path**, whichever way the roots were found; `matches` stays path-sorted, and `/api/diff`'s `matched` stays the path-sorted union of both sides. `truncated` is true when the search was budget-cut (or as before, the node cap). Two completeness flags say when a filtered response may hold fewer matches than exist, each with a reason the client prints next to the match count (and in the diff's heading); `/api/diff` merges both sides':

| flag | when | reason (examples) |
|---|---|---|
| `partial` + `partialReason` | a search budget stopped the search (§4.3) | "the row read hit its budget (256 row groups)", "more than 50,000 matching rows", "the search hit its time budget (1.5 s)", "the name search hit its read budget (128 name groups)", "1 match root too wide to look up" |
| `approximate` + `approximateReason` | the match roots came from a thresholded read, not the index (§5) | "this scan has no search index; small matches may be missing" (no sidecars, v1); "…and this view is too big to scan…" (the v1 `V1_FILTER_SCAN_OBJECTS` skip); "this query can’t use the search index…" (regex, a term ending in `/`); "a user lens filters only the rows it read…"; exclusions-only variants when only the NOT part lacked the index |

Both are absent when the index answered completely (or the view root itself matched with no exclusions). The coarse first paint's old `partial` flag is now `firstPaint` (`ViewOpts.firstPaint`). `tier` reads `search+<sort>` when the index found the roots, and `Server-Timing` gains `search;desc="<pos|neg> <mode> c<candidates> r<rows groups>[ cut]"` (v2) or `"… c<candidates> n<names groups> g<path groups>[ cut]"` (v1).

## 5. Fallback

`depth=N` (`maxDepth`, the first paint and the diff's `depth=1`) caps only what phase 2 returns, never where matches are found: phase 1's fallback read and a lens's read under a query search every depth (until 2026-10-02 the fallback read stopped at `dP + N`, so a `depth=1` request with every match at depth ≥ 2 reported none).

The filter view uses the thresholded phase 1 (and finds excluded paths among the rows it read), and flags the response `approximate` (§4.4), when: the generation has no `search.parquet` (every generation written before this, all v1 scans), a term ends in `/`, or the query is a `/…/` regex. The regex keeps its full-path meaning (`/ckpt.*final/` matches across segments) and stays undocumented; the index never plans it. A user lens without claims also keeps the old read, and a lens view with claims filters after the read (`nameFilter`), which matches by the predicate but does not subtract excluded paths (open).

## 6. Cross-scan reuse (later)

Vocabulary churn is ~0.1–0.2 %/scan (gcs, [path-store] §4.7), so the names' text and the trigram postings — ~90 % of the bytes — barely change between scans; only `n`, `b` and `rgs` (row-group ordinals of that scan's `path` sort) are per scan. A later layout: an append-only cross-scan vocabulary (stable ids, postings over it, sealed in K-scan groups like the store archive) plus a small per-scan `id → (n, b, rgs)` table. Cost: ids stop being an impact order (first-seen instead), so the budgeted prefix stops meaning "heaviest"; a per-scan rank column restores it for the names scan, not for postings. Not built.

## 7. As implemented, and what is open

- **Writer** — `src/disk_tree/find/search.py` `write_search(path_sort, *, con, rows_rg_rows, postings_rg_rows, dir_rg_rows)` (local files; sets `preserve_insertion_order` for its queries, which `viz` turns off), layout v2 only. `dt_cloud.index.write_sorts(search=True)` runs it on the cut `path` sort and reports `{names, rows, postings, files}` as the `path` entry's `search` (and `write_index`'s summary `search`); `dt-cloud path-index -S` / `index-write -S` (off by default). The generation dir's upload carries the files (r2's `daily-ingest.yml` copies the dir recursively and passes `-S`).
- **Syntax** — `site/functions/_lib/queryAst.ts` (AST, `QuerySyntax`, typed `QueryError`), `querySyntax.ts` (`simple` / `makeSimple({minTerm})`, `regex`, `SYNTAXES`, `resolveSyntax`, `parseAst`), `pathQuery.ts` (`compileQuery(ast)` → the predicate carrying `ast`, `pos`, `neg`; `parseQuery(q, syntax?)`), `scope.ts` `queryParam` (the handlers' `q=`/`qs=` → predicate or 400), re-exported for the client by `src/filterTree.ts`; `App.tsx`'s box (`?qs=`, inline errors, `FilterNote.tsx` for the flags) and `QueryHelp.tsx` (the generated help). No filter parser exists on the `dt-cloud`/engine side (the local engine's `filter` CLI left `cloud` in `360be7d`); `@rdub/treemap`'s `parseQuery` is the widget library's own display filter and is unchanged.
- **Reader** — `site/functions/_lib/searchQuery.ts` (`termBranch`, `planTerms`, `planPositive`, `planNegative`, `trigrams`, formula `and`/`or` with absorption), `search.ts` (`openSearch` (v2, else v1), `searchRoots(env, h, pred, plan, root, limits, key?)`, `readRows` (v2), `SEARCH_LIMITS`, the result cache), `index.ts` (`chunkSpan`, `planRuns`, `runsInOrder`, `RUN_GAP` / `RUN_BYTES` / `RUN_READS`, `decodeColumns`, `readGroupsAt` (path-first, `stop`), `readGroupsCached` (merged reads for every span read), `readFooterBytes`, `cachedRange`, the group LRU, the handle's `dir`). `view.ts` `readView`'s filter branch runs whenever the root doesn't match or the query has NOT terms: phase 1 by `searchRoots` on a store generation without a lens (keyed by the query's AST), the old reads otherwise; then the excluded paths and the net totals (§1, §4.3); `buildDiff` gets both through `readView` on each side and nets its point lookups by `Read.excl`.
- **Tests** — `tests/test_search.py` (the exact name-major rows and their group sizes, the full postings list, directory rows and key-value metadata, statistics only on the key columns, owner slices kept together and in order, encodings, the Kelvin sign and a non-ASCII trigram, rejects); `cloud/tests/test_index.py::test_write_index_search`; `site/functions/_lib/querySyntax.test.ts` (parse tables per syntax incl. typed errors, the 3-character minimum, the registry and `qs=` resolution, help examples parse, `queryParam`); `pathQuery.test.ts` (predicate tables over a path list for `simple` and `regex`); `functions/api/query400.test.ts` (a bad `q=`/`qs=` is a 400 with its message on `/api/subtree` and `/api/diff`); `src/queryHelp.test.ts` (the help card rendered from `describe()`); `src/filterNote.test.ts` (the note's error / partial / approximate rendering); `searchQuery.test.ts` (formulas and name tests per term shape, declined plans, NOT branches, absorption); `search.test.ts` over `fixtures/v2-search/` (`gen.py` `write_v2_search`: 2 buckets, 6,047 rows, 3 `path` groups, 6,044 names, 24,199 postings, v2 rows in 12 groups of 512, v1's names and directory kept as committed, 2 directory rows per group): roots = `matchRoots` over every row for 24 queries (substrings, AND, `*`, quoted, `/` terms, AND with NOT) × 5 roots × both layouts, the roots' rows exact and equal across layouts, the blob-served copy equal, stage counts for `ttl` per layout, the names scan, no `path` read on no match, each budget's cut per layout (`rowsRgs` keeping whole names heaviest first, `keptRows`, `wallMs`, `pathRgs`, `namesRgs`, a lifted ancestor in each), v2's rows as one merged read, the result cache (a repeat reads nothing; a wall-cut result isn't kept), whole-view equality with the pre-index read at threshold 0, a sub-budget match only the index finds, `matched` order, the fallback on a cut-before-any-root, and a filtered diff; NOT: `tmp -ckpt`'s net totals, `excluded` and drawn tree exactly, only-negatives, index vs pre-index views equal for 8 NOT queries × 3 roots at threshold 0, an excluded path below the pixel budget only the index subtracts, and a filtered diff with NOT; coverage: exact `partial`/`approximate` reasons on the pre-index and index views, a cut search's view and diff, the cut-before-any-root fallback (row, path and time budgets), the v1 cap skip, and each budget's reason; the `regex` syntax end to end (= the `/…/` fallback = the sidecar-less read, flagged `approximate`). The engine's own short-needle cases use `makeSimple({ minTerm: 1 })`. `index.test.ts`: `planRuns`, `chunkSpan`; `pathStore.test.ts`: a whole-sort read is one merged GET of the projected span. `searchBench.test.ts`: the latency model of §3.2 (skipped without `SEARCH_BENCH_DIR`).

Open, needing a production build/measurement:

- **Verify on r2 after a deploy** (reader first; §3.2's model, not a Worker, produced the numbers). The existing v1 generation: `curl -s -D tmp/h.txt -o tmp/b.json 'https://r2.rbw.sh/api/subtree?cv=2&w=1408&h=896&path=&date=2026-10-02&q=2019&full=1&minArea=19098'` → 426 matches, no `partial`, `server-timing` with `search;desc="pos trigrams c369 n78 g60"` at ~1–1.5 s (was 7.8–11.4 s) and `fetch` summed under ~1 s over ~6 reads (was 2.5–3.5M ms). After the next ingest writes v2 (the date's dir lists `path-index.rows-search.parquet`: `/v1/files/list?prefix=listing/<date>/index/`): `search;desc="pos trigrams c<n> r<m>"`, ~0.5–0.9 s for a `2019`-sized query. `q=parquet` → a 200 in ~1–1.5 s, `partialReason` "more than 50,000 matching rows" (was a 1102 after 27 s). The isolate keeps results: a repeat of the same `q`/root on a warm isolate skips the search (`search` ≈ 0) — measure cold with the first request after a deploy, or an equivalent query (`2019 2019`).
- **Deploy order**: the site (reader) before the writer reaches an ingest. A v1-only reader ignores a v2 generation (no `search.parquet`: `approximate`, not an error).
- **Worker CPU**: `fzstd` (pure JS, ~80 MB/s) is ~90 % of decoding a `path` column; a wasm zstd imported as a module (Workers can't compile wasm at runtime) would speed every read, plain views included, by an estimated 3–5×. Not built.
- gcs: v2's build (a second full sort of the store, by name id: ~777M rows) and size (≈ 1.3× the `path` sort, §3) on gcs's Batch class; a broad query's directory reads (candidates scattered over ~1,800 directory groups: each a read; bounded by `rowsRgs` and `wallMs`, not separately); cw likewise.
- request cost on real queries (`ttl`, `ckpt`, `checkpoints`, `.safetensors`, a 2-char needle) and the budgets' defaults, on gcs.
- the `dirs`-only or bytes-coverage-cut vocabulary (§3) if the size is unwelcome.

Decisions for the user:

- **Per-alternative NOT.** Negatives are the whole query's (`a -x|b` = (a OR b) AND NOT x). Scoping them to their alternative would make the predicate non-monotone along a path (`b` could hold again below an `x` that ended `a`), which the root/exclusion model doesn't express.
- **Vocabulary scope** (§3): all names (exact for every query, ~7–11 GB/scan est. for v1; v2 adds ≈ 1.3× the `path` sort and drops the names file) vs dirs only (~1 GB, but object-name queries fall back).
- **v2 on gcs** (§3.2): the rows sidecar makes a `2019`-sized query ~1.5× faster end to end (model) and ~1.8× less CPU than merged reads on v1, for a second copy of the store's rows per scan. The reader serves either; the writer emits v2 only (a v1 option is a small revert if gcs should stay on v1).
- **`-S` default**: off until measured; on for r2 first?

[diff-and-search]: diff-and-search.md
[path-store]: path-store.md
