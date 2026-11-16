# Why `cloud`'s diff walk is slower cold on cw (#44)

Status: closed 2026-11-15 at option 1. cw keeps its level-by-level walk (`410f0338e`) and gcs stays at `DIFF_LOOKUP_CAP = "32"`. `cloud`'s heaviest-first walk is parked on `w/cloud-walk` (`57897f629`); none of the four locality attempts below tied cw's walk on cold latency. Data: `tmp/difflk/st/` (untracked).

## Setup

Two dev previews with per-step traces (`stN` Server-Timing entries: asks and depths per side, null / found answers, span queries, halvings, footer groups, row groups per step):

- `st-a`: cw's walk (`cc73b68b8` + traces)
- `st-c`: `cloud`'s walk at `DIFF_LOOKUP_CAP = 240` (`98914cd25` + traces)
- `st-af` / `st-cf`: the same, plus the candidate `readAsks` change below

The 10 cases in `tmp/difflk/cases.json` (root over 6 h, 1 d, 1 w, 2 w and 1 mo; four buckets over 2 w; `marin-us-east-02a` over a day) ran cold on fresh canvas buckets, with host order alternated.

## What it is not

- **Store latency.** Neither walk fetched from the store. `fdoc-r2` (footer docs from R2) was 0 on both; row-group `fetch` was 129 ms in total over 80 `cloud`-walk requests. Everything was served from the colo cache, so there is no per-GET latency to compare with gcs.
- **The colo cache confound (root's hypothesis 4).** Real, but not the cause. The colo key's host is synthetic (`https://index-footer.cache/…`), so every `*.pages.dev` alias shares one colo cache, and "cold" in `diff-bench` means only a cold edge response. Both walks were colo-served, and the gap holds with host order alternated.
- **`gone` lookups.** They answer by inference, with no read. Per wide case, the nulls actually read were 106–188 on `cloud`'s walk and 228–246 on cw's.
- **`GroupUnread`.** It only throws, never re-reads; every request returned 200.
- **Budget semantics.** Answered lookups are similar: about 240 on both at cap 240.
- **Round count alone.** `cloud`'s walk ran 6–10 rounds and cw's 8–11 levels. What differs is the cost per step.

## Mechanism

### 1. Span planning, per (side, depth)

`readAsks` plans one rectangle per (side, depth) over that depth's [min, max] ask path. It halves a rectangle when it selects more than `4n + 16` row groups for its `n` asks.

- cw's walk reads one depth per step: 1–2 span queries, about 10 footer groups.
- `cloud`'s heaviest-first rounds mix 5–7 depths per side, so each round re-plans nearly every depth block.

Root over 1 w:

| | cw | `cloud` |
|---|---|---|
| span queries | 8 | 120 |
| halvings | 0 | 30 |
| footer groups | 44 | 534 |
| walk | 0.31 s | 3.4 s |

One round of 17 sparse asks at depth 10 took 1.5 s through 4 serial halvings.

### 2. Row-group locality (dominant)

The store is sorted `(depth, path)`.

- cw's asks for one level are siblings under a few parents, so they share row groups: root over 1 w read 78 groups for about 270 answers.
- `cloud`'s heaviest names are scattered across depths 2–10 and many directories, so most land in a group of their own: 292 groups for 189 answers, about 1.5 groups per answer against 0.3.

Each group is a decoded colo doc, about 0.3 MB gzipped and about 2 MB of JSON. `cloud`'s walk also finds more rows (83 found answers vs 28 on that diff): the gained named rows were Σ8.9 PB at cap 240, against Σ10 TB lost.

### 3. Dependent rounds at small caps

At cap 32, each heaviest-first round takes the few names that fit. Root over 1 w ran 7 rounds for 32 lookups: walk 940 ms, against 346 ms for cw's walk over its whole budget.

## The `readAsks` candidate (not adopted)

On footer-planned handles: one exact `selectSpans` pass over every ask's own rectangle. Parked on `w/readasks-one-pass` (`2712aae73`, with a test).

It removes mechanism 1: on root 1 w, 120 span queries became 15, 30 halvings became 0, and 534 footer groups became 93.

`diff-bench -n 2`, full diff, cold medians, answers agreeing:

| | A | B | Δ med | B slower | p |
|---|---|---|---|---|---|
| `cloud` walk `st-c` → `st-cf` | 1159 | 1098 | −85 | 8/20 | 0.50 |
| cw walk `st-a` → `st-af` | 824 | 932 | +176 | 16/20 | 0.012 |
| `st-a` → `st-cf` (#44 with the fix) | 681 | 1002 | +290 | 14/20 | 0.12 |

The exact pass tests every footer group and row group against every ask, O(groups × asks), and that costs more than cw's contiguous single-depth ranges. A version that binary-searches the sorted ask keys per group (O(groups · log asks)) would remove that regression, but its ceiling on `cloud`'s walk is the ~85 ms of mechanism 1. Mechanism 2 stays.

## gcs

gcs and cw both serve diffs from the interval store (`PATH_STORE = "intervals"`). That handle is always footer-planned (`mode: 'pq'`); D1 span tables back only per-scan sorts hot in D1, for scans outside the interval generation. So gcs plans its lookups exactly as cw does.

gcs runs `cloud`'s walk at `DIFF_LOOKUP_CAP = "32"`, and its 2026-11-13 sweep recorded the same cold time as the reverted walk, not a faster one (`ca439fcd5`).

The gcs session ran these traces on its own deployment (2026-11-16): `83ee66d4f` on gcs-prod `933ff8e84`, aliases `st-g32` and `st-g240` (preview `DIFF_LOOKUP_CAP = "240"`), `probe.py -n 2` over gcs's 11-13 cases (root over 1 d, 1 w, 2 w, 1 mo and 11-04 → 05; four regional buckets over 2 w; one over 1 d), all 80 requests cold misses. Files: `~/c/disky/wt/gcs/tmp/difflk/st-g/` (untracked).

`diff:full` medians, cap 32 → 240:

| | cap 32 | cap 240 |
|---|---|---|
| total | 1662 ms | 3790 ms |
| walk | 835 ms | 2572 ms |
| rounds | 4 | 6 |
| row groups (`ngroups`) | 116 | 215 |
| footer groups | 108 | 492 |
| fetch (store GETs) | 340 ms | 1004 ms |

- **Cap 32 on gcs is not cheap.** The walk costs about what cw's did at cap 32 (835 vs 940 ms); "flat" meant it matched gcs's earlier walk, not that it was fast. gcs's level-walk traces weren't collected, so whether the level walk would be faster on gcs, as on cw, is unmeasured.
- **Wide rounds lose locality on gcs too.** At 240 the middle rounds take 30–66 names across 4–7 depths and decode 135–400 footer groups each (0.6–5 s a round). Mechanisms 1 and 2 are not cw-specific; cap 32 is what keeps gcs's rounds narrow.
- **gcs pays store latency; cw doesn't.** Row-group `fetch` is 340 ms median at cap 32 (up to 7 s on a bucket diff), against ~0 on cw (colo-served). On gcs, fewer row groups (option 2) saves GETs, and smaller docs save less.

## D1 handles

The D1 loop has the same per-depth [min, max] and halving shape: each halving is a D1 query, not a footer decode. A single pass doesn't fit D1 as is: each rectangle binds 4 values, and D1 allows 100 bound parameters per query, so about 24 asks per query. That would mean chunked parallel queries. No deployment's diff lookups use D1 handles today.

## Attempts to restore locality (all parked, none adopted)

Each attempt was benched against cw's walk (`st-a`) on the same 10 cases, cold full diff, with `diff-bench -n 2` and the named rows `value.py` reports against `st-a`.

| Attempt | Branch | Cold full median (ms) | Δ med | slower | p | Named rows lost / gained |
|---|---|---|---|---|---|---|
| (b) riders, cap 32 | `w/walk-b` `c4c073535` | 784 → 1008 | +170 | 14/20 | 0.12 | Σ321 / Σ7,733 TB |
| (b) riders, cap 96 | | 734 → 1460 | +661 | 10/10 | 0.002 | Σ59 / Σ8,849 TB |
| (b) riders, cap 240 | | 732 → 968 | +236 | 13/20 | 0.26 | Σ8 / Σ8,933 TB |
| (b2) row-group budget 80 | `w/walk-b2` `3e37146d3` | 665 → 1045 | +455 | 16/20 | 0.012 | Σ22 / Σ7,754 TB |
| (b2) row-group budget 120 | | 628 → 1076 | +387 | 9/10 | 0.021 | Σ9 / Σ8,287 TB |
| (b3) b2 + footer budget 60 | `w/walk-b3` `c039e0c78` | 684 → 1024 | +476 | 16/20 | 0.012 | Σ22 / Σ7,754 TB |
| (c) sibling runs, cap 32 runs | `w/walk-sib` `ee36713c8` | 877 → 1964 | +804 | 16/20 | 0.012 | Σ177 / Σ7,994 TB |
| (c) sibling runs, cap 240 runs | | 898 → 1772 | +747 | 16/20 | 0.012 | Σ22 / Σ7,754 TB |

**(b) Riders.** A round's decoded row groups also answer the pending names whose every row they hold, on every tier: a depth strictly between a multi-depth group's first and last, or a path strictly inside a single-depth group's bounds. Riders take no budget.

They are not free. At cap 32, against `cloud`'s walk without them (`dl-c` → `wb32`): 868 → 890 ms, Δ med +112, 15/20 slower, p 0.041. An answered name settles its children into later rounds. Riders only trade latency for coverage.

**(b2) Row-group budget.** A round plans its candidates' groups without decoding (`planAsks` `whole`, an exact per-ask `pointGroups` on footer tiers, memoised per name), then takes names by weight while the row groups they add fit.

It bounds the groups read, but not the plan: planning every candidate decoded a footer group per scattered name (root 1 w: 90 footer groups, 206 names then skipped).

**(b3) Footer budget first.** Candidates are priced in footer groups from the footer index's own bounds (`footerCover`, no read), and only those that fit 60 are planned. That bounds planning too (root 1 w: 61 footer groups, walk 1.1 s, from 1.85 s), but the round still decodes about 200 row-group reads over 4 rounds, against cw's walk's ~80.

**(c) Sibling runs (option 2).** On b3, a round takes its heaviest names, then each one's whole sibling run: the pending names under the same parent on the same side. Runs are admitted whole, heaviest run first, while their groups fit, and `LOOKUP_CAP` counts runs, not names (`WALK_V` 5).

The runs are short. On root over 1 w, they added 35 names at cap 32 and 11 at 240: a heavy name's parent seldom has other pending children, because its siblings were drawn on both scans or answered already. So rounds still decode about 180–230 row groups (cap 32: 229 over 7 rounds; cap 240: 177 over 4), against cw's walk's ~80. Charging per run let cap 32 run more rounds, which made it slower still.

**Conclusion.** cw's level walk is cheap because its lookups are siblings: one depth at a time, a few parents, so a few footer groups and row groups hold them all. Heaviest-first picks names from many parents at many depths; on a `(depth, path)` sort they share neither. Each budget bounds one cost and leaves the walk paying more than the level walk for the deep names it adds, which are its whole point: Σ7.7–8.9 PB gained.

## A separate lever: the docs each diff reads

On both walks, a diff's lookup cost on cw is colo Cache API reads of decoded docs:
- footer-group docs, about 1 MB of JSON each;
- row-group docs, about 0.3 MB gzipped and 2 MB of JSON.

cw's walk reads about 50–80 of each on a wide diff. Server-Timing shows the waits: `footer` about 0.5 s and `groups` about 0.5 s median, summed over concurrent reads. It cannot show the JSON parse, because Workers' clock advances only across I/O; `gjson`, the parse step it does time, reads 19 ms median.

A compact or binary encoding, or smaller docs, would shrink each read and parse on every diff, whichever walk. Size it by wall time (`diff-bench`'s cold medians), not Server-Timing, before building it.

**Row-group docs, done (2026-11-16, `96df30bad` `[cloud]`):** the colo copy is a gzipped binary columnar doc (`groupBin.ts`, key `&g=2`): numbers as float64 typed-array views, each string column one UTF-8 decode, JSON only for booleans, mixed columns and lone surrogates. Benched as `enc-a` (binary) against `enc-c` (`11811e868`, JSON) with an experiment-only `CACHE_V` (both aliases otherwise share the dev stack's response cache), a warm-up pass, then `diff-bench -n 2` in both host orders: 50 cold pairs a kind, answers agreeing.

| kind | JSON → binary (ms) | Δ med | binary slower | p |
|---|---|---|---|---|
| full diff, wall | 1730 → 1468 | −309 | 13/50 | 0.001 |
| full diff, Server-Timing | 992 → 767 | −206 | 9/50 | 6e-6 |
| depth 1, wall | 544 → 449 | −55 | 16/50 | 0.015 |

Footer-group docs (~1 MB of JSON each) are the next candidate, benched the same way before building.

## Options

1. **Keep cw's walk (current).**
2. **Restore locality in `cloud`'s walk.** Tried as (c), sibling runs (2026-11-16): +747–804 ms cold median against cw's walk. The runs are too short to restore locality, so this option is closed.
3. **Smaller per-diff docs.** Row-group docs done (above); footer-group docs next.
4. **The sorted-key `readAsks` pass, as `[cloud]`.** Worth up to ~85 ms on `cloud`'s walk; it must not regress cw's walk.
