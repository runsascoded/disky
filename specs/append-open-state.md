# Appends that cost O(the day's changes): carried open state

Status: 2026-10-22. Item 3 of `specs/scan-cs3.md`. Phase 2 (the catalog's carried counts) built and tested; not yet on gcs's image.

## What actually scales with the scans since the base

The ask was to stop refolding each key range's open versions from the base's `cintervals` plus every run's `cdelta`. Checked against the code and gcs's logs, that refold is not what grows on gcs:

- **Per-range append (Batch, both stores): already carried.** `static_append append` reads `state/<prev>/copen/r####.parquet` (scratch bucket) and writes `state/<D>/copen/`; `interval_append ranges` reads and writes `state/<D>/{pvl,sv,svt}/`. `rebuild_open` / `rebuild-state` is only the recovery path for a lost state. gcs's static `append` stage is flat at 6–8 min (10-19: 363 s; 10-22T1545: 424 s, 26 scans past the base).
- **The one-machine paths do refold.** `static_local.append_scan` calls `rebuild_open` per range per scan, and `interval_local` builds `open_state_sql` from base + every run's deltas. These are the r2 demo's daily `add`s (small stores), not gcs.
- **The static catalog step is the O(scans) term on gcs.** `catalog_delta` → `static_catalog.append` expands *every* scan's `cdelta` since the base into suffix rows twice per run: once (`xo`, the opened rows) to find which prefixes could cross `V`, and once (point events) for the new members' history. Its compute went from 5.6 min with one run (10-09) to ~29 min with 26 (10-22T1545: `append.s` 1718 s; the stage took 3409 s with a spot retry). Since 10-19 it is 23–90 min per scan.
- **Others, not in scope here:** the drill's `pnew_sql` anti-joins the scan's opens against the base's whole `cintervals` plus every run's `cdelta` (~1 min measured; O(corpus), not O(scans)); drill and anchors read the base + live runs' tiers (O(log scans) tiers, each read by range). Anchors is now the longest stage (40–90 min) and is the next target; it is not a refold.

So this spec carries two things: the catalog's candidate counts (gcs's win), and the open versions on the one-machine paths.

## 1. The catalog's carried counts (`xcount`)

### What the refold computes

`static_catalog.append` decides membership from `n(k)`: the number of opened suffix rows, over every scan since the base, that start with literal `k` (length ≥ 3). It walks levels: at length 3 every prefix of an opened suffix row is **evaluated**; a prefix is **kept** when `base.upper(k) + n(k) > V`; at length `L + 1` the children of kept length-`L` prefixes are evaluated. The kept prefixes, with `n(k)` and the last scan's share `nl(k)`, are the candidates.

### The state

`xcount.parquet`: `(q VARCHAR, n BIGINT)`, sorted by `q`, one row per **evaluated** prefix through scan `D`, `n` = its count over the scans since the base. Kept is derived (`base.upper` is fixed for a generation), and `n` only grows, so kept only grows: once a prefix is kept, its children are evaluated forever after.

### The incremental walk (scan `D`, state through `D−1`)

`T` = `D`'s opened suffix rows (its `cdelta` alone). Rows under consideration start as `T`. At each length `L`:

1. Count the rows by `left(s, L)`; add each key's previous `n` from the state (the state's length-`L` keys are exactly the children of the prefixes kept through `D−1`, all still kept). The union is the length-`L` evaluated set, and its new `n`.
2. Keep those with `upper + n > V` (candidates, with `nl` = `T`'s count).
3. A key kept now but not before (`upper + n_prev ≤ V`) that is in the state has history whose children were never evaluated. Its rows come from the **runs' suffix shards** (`sx/`, range-read through each run's `sidecar.parquet`): a run's row is an open of that run iff `vf ≥` the run's first scan. They join the rows under consideration. (A key kept now that isn't in the state has no history: its parent was newly kept and its rows are already in, or it first appears in `D`.)
4. Drop rows whose length-`L` prefix isn't kept, and rows of length `L`.

The output state is every evaluated key with its new `n`. It equals the refold's set and counts by construction; the tests compare both.

### The new members' history

A literal that becomes a member at `D` needs its whole history as events. The refold uses the base's suffix rows plus every `cdelta` since the base, expanded. The incremental path uses the base's rows (as now) plus each run's `sx` rows in the members' ranges, as point events: `+` at `vf` when `vf ≥` the run's first scan, `−` at `vt` when the row is closed; plus `D`'s `cdelta` events. A run's `sx` is its scans' `cdelta` suffix rows merged by `(s, path, usr, vf)` with the smallest `vt`, so these are the same events (an open and its close in one merged run are one row carrying both; a close of an older version carries only the `−`). Events are summed, so the cells are byte-identical.

### Trust: published, digested, inside the lease

- **Where:** `gs://<scratch>/static-names/<gen>/state/<D>/catalog/xcount.parquet`, then `xcount.json` = `{scan, scans_since_base, rows, n_sum, digest}` written after it. Beside the per-range `copen`, under the generation (a compaction starts a new generation, and its first append refolds).
- **Read only when:** `<D−1>` is the newest earlier manifest's last scan (the run is published, so its catalog stage finished), `xcount.json` exists, its `scan` is `D−1` and `scans_since_base` = the runs' scan count, and the parquet's row count, `sum(n)` and digest (`bit_xor` of `hash(q, n)`) match it. Anything else (missing, partial, a stage killed between the two files, a mismatch) is not an error: the step refolds from every `cdelta`, as today, and writes the state for `D`. The first scan after rollout does exactly that.
- **Written** in the catalog stage, before `catalog/meta.json` (the stage's done marker, which `publish` requires). A rerun of the stage rewrites the same bytes: the inputs are fixed once `D`'s `cdelta` exists.
- **Lease:** stages run only while the coordinator holds `append_runner`'s coordinator lease (`specs/done/append-coordinator-lease.md`); the state is written by the catalog stage and read by the next scan's, both inside a lease, so no other coordinator appends between. A coordinator that loses its lease mid-stage leaves at worst a parquet without its `xcount.json`, or a pair for a scan that never gets published; neither is trusted (the scan check and the digest), so the next holder refolds or rewrites it.
- **Published bytes:** unchanged. Nothing new under `deltas/`, nothing on R2; the run's `catalog/` files are identical either way.
- **Prune:** `prune` already drops every `state/<prev>/` once `D`'s `copen` and `done/` are complete; `xcount` goes with its day.

### Retention (`FOLDED_RETAIN`)

Unchanged: `cdelta/` stays on folded runs. It is O(changes) (~20–30 MB a scan on gcs) and still has readers: the compaction (`static_compact.fold_range`), `rebuild-state`, the drill's `pnew_sql`, and this refold fallback. Dropping it needs those moved first; not worth it for its size.

## 2. One-machine paths: open versions kept between invocations

`static-names add` / `interval-store add` refold every range from the base + every run on every scan. Keep the open versions instead, as the staged path does: under the store's generation, `state/<D>/` (static: `copen/r####.parquet`; interval: `{pvl,sv,svt}/r####.parquet`) plus a `state/<D>/state.json` digest written last, and the catalog's `xcount` beside them. The next `add` reads them when `D` is the newest manifest's last scan and the digest matches, else refolds. The previous day's state goes once the new manifest is up. Readers never list `state/`.

## Phases

1. This spec.
2. **Built.** `static_catalog`: `RunTier`, `write_xcount` / `trusted_xcount` / `xcount_digest`, `_candidates_refold` / `_candidates_carried`, `append(state=, history=, state_out=, scan=)`. `static_append`: `run_tiers`, `carried_inputs`, `catalog_delta(..., state=, history=, state_out=, scan=)`, `runs catalog` reads and writes `state/<D>/catalog/` (`-R` forces the refold; `-S` the scratch bucket) and uploads `meta.json` last. Logs `catalog <D>: xcount carried from <prev> | refold`. `cloud/tests/test_catalog_state.py`: three appends past a two-scan base (a carry into a level-1 run before the third), V = 3 and 12, all three fixture flavors: each run's `cells.parquet` / `index.parquet` and `xcount` byte-identical to the refold's; the history reads exercised (prefixes first kept from the runs' shards at V = 12; new members); untrusted states refused (another scan, another window, parquet without JSON, JSON without parquet, a digest mismatch with Σ n unchanged, a truncated parquet); a rewrite killed midway leaves no JSON. Not covered by a fixture: a hex-run rule on the carried path (both paths expand with the same `kept_sql`, as the run's shards were cut).
3. The local paths' carried open versions (static first, then interval).

## Expected effect on gcs

The catalog step drops from ~29 min of compute (growing ~1 min a scan) to `D`'s own expansion plus the state merge and a few range reads: a few minutes, flat. With `shards ∥ catalog`, the phase's chain becomes append (~7) → max(shards ~3–5, catalog ~5) → drill ∥ anchors (30–90; anchors the long pole) → publish + R2 (~3).

## Rollout

The first scan on the new image refolds (as today) and writes `xcount`; the next reads it. Verify with a manual scan: its catalog stage logs `xcount: refold` then, on a second manual scan, `xcount: carried`, and the run's `catalog/` files match a refold of the same scan (`runs catalog --refold` into a scratch dir and compare bytes).
