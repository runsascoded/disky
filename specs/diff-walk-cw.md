# Why `cloud`'s diff walk is slower cold on cw (#44)

Status: root-caused 2026-11-15. cw stays on its level-by-level walk (`410f0338e`); `cloud`'s heaviest-first walk is parked on `w/cloud-walk` (`57897f629`). Data: `tmp/difflk/st/` (untracked).

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

gcs runs `cloud`'s walk at `DIFF_LOOKUP_CAP = "32"`, and its 2026-11-13 sweep recorded the same cold time as the reverted walk, not a faster one (`ca439fcd5`). Why gcs's cap 32 is flat where cw's was +601 ms (mechanism 3) needs these traces on gcs: its tree shape (how many heavy names, at how many depths) is the variable. I can't measure gcs without its token.

## D1 handles

The D1 loop has the same per-depth [min, max] and halving shape: each halving is a D1 query, not a footer decode. A single pass doesn't fit D1 as is: each rectangle binds 4 values, and D1 allows 100 bound parameters per query, so about 24 asks per query. That would mean chunked parallel queries. No deployment's diff lookups use D1 handles today.

## Options

1. **Keep cw's walk (current).**
2. **Restore locality in `cloud`'s walk.** Take a heavy name's siblings with it, so a round reads whole sibling runs under the heaviest parents. Measure the gained rows and the cold time against cw's walk on these cases.
3. **The sorted-key `readAsks` pass, as `[cloud]`.** Worth up to ~85 ms on `cloud`'s walk; it must not regress cw's walk.
