# Anchors: a tight bound for "near" `(k, dir)`

## Problem

A run's anchors (`static_anchors.run_rollups`, `start_run`) find the `(k, dir)` that become heavy with the run (prior rows + the run's > `R` = 100,000). Exact prior rows are expensive (`probe_prior_rows` reads every tier's rows of each key), so a cheap bound picks the candidates first (`near_candidates`), and only those are probed.

On cw's backlogged 2026-10-20T1806 scan (n2-highmem-16, 7,142 s, cgroup peak 123 GiB) the bound let almost everything through:

| mode | near `(k, dir)` | keys probed | prior rows read | newly heavy | probe time |
|---|---|---|---|---|---|
| `end` | 37,324,100 | 3,889,815 | 401,432,349 | 7 | 50 min + 17 min `pd` |
| `exact` | 2,635,173 | 211,005 | 11,590,500 | 1 | 3.5 min + 2 min |
| `start` | (no bound) | 1,014 keys, 1,340 batches | 207,870,298 | 0 | 41 min |

About 620M prior rows were read to find 8 newly heavy `(k, dir)`.

## Why the bound is loose (hypothesis, to confirm in step 1)

`near_candidates` bounds a `(k, dir)`'s prior rows by the rows of every tier's row group whose `(s, path)` statistics meet `[(k, dir/), (k, dir0))`. The bound is good for a group holding only `k` (`s_min = s_max = k`): its `p_min`/`p_max` are `k`'s own paths. It is useless for a **mixed** group, which holds other keys: its path statistics span the other keys' paths, so it meets every `dir` and counts all its rows (up to `SX_RG` = 8,192) toward every `(k, dir)`.

- In the base, a key has at most two mixed groups (its first and last), so the error is about 16K rows.
- In a run tier, a key's rows are usually few and sit inside one mixed group, so each run tier adds up to 8,192 rows to every `(k, dir)` of the key. With 13+ run tiers that alone passes `R`.

So a key touched by the scan with a few tens of thousands of rows overall gets every one of its touched dirs marked near.

`start` has no bound at all: every touched prefix not already heavy is probed (`near = rd ANTI JOIN ps`).

## Status

- **Step 2 is done** (`near_candidates(con, R, run_counts)`). The base is bounded by its groups. The run tiers are bounded by the lesser of their groups and the key's exact rows over them (`rc`, from their `s` column only). Measure it on the next cw scan: compare its `anchors <mode>: near … prior rows …` line with the 10-20T1806 numbers above.
- **Measured on cw's 10-21T0602** (image 09a0fbe6, through 14581b65): 2,699 s in all, against 10,048 s for 10-21T0002 on the old bound. The probes dropped by about 10,000×, and the rows read from 750M to 87M.

  | mode | near `(k, dir)` | keys probed | prior rows read | newly heavy | wall |
  |---|---|---|---|---|---|
  | `end` | 17,034,425 | 483 | 78,405,326 | 164 | 30 min |
  | `exact` | 1,949,780 | 30 | 4,416,997 | 15 | 14 min |
  | `start` | — | 16 probes | 3,841,634 | 20 | 1 min |

  Old bound, 10-21T0002: `end` 52.4M near, 5.19M keys, 504M rows, 31 newly heavy; `exact` 3.2M, 271K, 14.8M, 2; `start` 1,961, 231M, 23.
- **Step 1** (`near-probe`) is not needed. The per-query timings (`ANCHOR_QUERY`) showed that the probe dominated, but not because of the bound: a batch read `s` from its first key to its last, so 483 sparse keys read nearly the whole shard. The fix is `key_clusters` (1cad878e, gap 4M since 509cbb60). With it and the fold fixes (4f978b55, 509cbb60), 10-21T1202's anchors took 1,155 s, with `end` near 6.8M over 430 keys and 46M rows probed.
- **Left:** `rc` reads the large run tiers' `s` in full (~73 s on 10-21T1202). Its fix is step 2's per-tier census, taken only if `rc` stays a top cost.
- **Step 3 is done** (`start_run`'s `ck`). A prefix's prior rows are bounded by the base's groups over its range plus the run tiers' rows under it, counted from their `s` column (`rcs`). Before, every tier's two partial end groups were counted whole, so almost every prefix passed R. Measure it with the scan's `start` probes and probe_rows.

## Plan

1. **Measure (read-only).** Add `dt-cloud static-names anchors near-probe -g GEN -d SCAN -m MOUNT -o OUT`: run steps 1–3 of `run_rollups` for one mode over a published generation's tiers and the scan's run, writing nothing to the store, and report:
   - `near` per bound variant;
   - the exact `pd` counts;
   - the newly heavy;
   - each tier's share of the bound, split into pure and mixed groups.

   Run it on cw's 10-20T1806 and on gcs's latest scan, to confirm that run tiers' mixed groups are the slack.
2. **Exact run-tier counts.** Run tiers are small next to the base. Count their rows per `(k, dir)` exactly for the keys in `ck`, reading `s` and `path` with `s` pushdown. Bound only the base by groups, so near is `n + base_bound + runs_exact > R`. The probe then reads only the near keys' rows, as today.
   - If (1) shows merged runs (L2+) are too big to count this way, give each tier a per-key census instead: `(s, rows)` for keys with ≥ `F` rows, written with the tier.
     - A key absent from the census bounds at `F − 1` per tier.
     - A run's census is exact, and a merged run's is the sum of its inputs'.
     - A compaction recounts it.
3. **`start`.** Gate prefixes the same way: a prefix's prior rows bounded by the tiers' groups over the name index's `/prefix` range, base by groups and runs exact. Probe only those that can pass `R`.
4. **Tests.**
   - Near under the new bound ⊇ the exact newly heavy, on fixtures with mixed groups in run tiers. Assert the near set exactly.
   - The anchors output is byte-identical to today's, since only what is probed changes, not what is written.

## Ownership

Root session on `cloud`. cw holds its `near_candidates` changes until this lands.
