# The drill's newly heavy directories at scale

## Context (gcs, 2026-10-18 / 10-19)

gcs's daily drill (`sn-drill-2026-10-19-0701-090351`, n2-highmem-48) ran 3 h 20 min, of which `drill long` took 2 h:

| step | wall | cores |
|---|---|---|
| delta roots (1,608,757,752 roots; 510,110,739 of them 3,854 new members' whole history) | 16 min | |
| rollups up to newly heavy (31,149 heavy touched, 1,944 restated, 6,246 base counts) | 25 min | |
| newly heavy: 13,862 directories, 460,128,742 prior roots | 99 min | ~8 |
| `long: write` (1.6B roots, 785,544 row groups of 2,048, 21 GB) | 33 min | ~3–4 |

The 10-18 drill had the same wave: 11,591 newly heavy, 777,599,488 prior roots, 617,646,825 delta roots (843 new members).

### Why ~13K newly heavy two days running

Heavy is per `(class, dir)`: `S(c, dir)` (stored root rows under `dir`, over the tiers) > R = 100,000, sticky. Not a persistence bug: the 10-19 manifest lists `deltas/2026-10-18T0701` as a prior tier, and of 10-19's 15,806 full headers (13,862 newly heavy + 1,944 restated) only 1,617 have a 10-18 full header for the same class and directory (≤ the 1,944 restated). The wave is data:

- `marin-us-east5/evaluation-checkpoints/passk-august-v1` grows by millions of files a day (new paths: 3.0M on 10-17, 8.8M on 10-18, 13.2M on 10-19), named like `…-000123.json`. Each of the ~40K classes whose term those names contain gains roots there, and the classes cross R one wave after another: 3,706 full headers there on 10-18 (as many at `evaluation-checkpoints`), 5,892 on 10-19.
- Heaviness is closed upwards, so each crossing at `passk-august-v1` is also one at `evaluation-checkpoints` and `marin-us-east5` (10-19: 4,338 / 4,338 / 3,559 newly heavy directories at the three).
- 3,854 literals entered the catalog on 10-19 (`-000000`, `-000000.json`, … one family per shard number): each new member is a class of its own carrying its whole history, so its directories over R go heavy at once (6,860 of 10-19's full headers are new members').

It tapers once the classes matching that tree are all heavy there; a new tree of that rate starts another.

### What a newly heavy directory costs, and why it is O(changes)

A full rollup needs the directory's history (its children's series); a non-heavy directory has no rollups, so the history is its roots. But a directory going heavy today was at most R yesterday, so the prior roots read are ≤ R per `(class, dir)` (10-19: 460M / 13,862 ≈ 33K), and each goes heavy once. Amortized, it is O(R × keys that ever go heavy), which today's rows caused. The O(history) part is the new members: a literal entering the catalog brings its whole history into the run's roots (510M of 10-19's 1.6B) and its directories' rollups.

## Done: chunked newly heavy directories (`newly_heavy_rollups`)

Before, one directory at a time: per directory a ranged read of each prior tier (index then groups: serial round trips, 2,510 s at 0.7 cores), then ~10 small DuckDB statements (combine 1,783 s, rollups 1,428 s); 0.41 s a directory, bound on per-directory latency, not data.

Now consecutive `(q, dir)` in chunks of ≤ `NEWLY_HEAVY_CHUNK_ROWS` (8M) estimated rows (`newh.rows` = the `S` that made the directory heavy):

- the chunk's top ranges (a directory not under another of the chunk's, same class) are read once, their groups prefetched together (`TierReads.hold`, 64 at once; a group shared by two ranges decoded once); nested ones (`passk-august-v1` under `evaluation-checkpoints` under `marin-us-east5`) are cut from them by range, not reread;
- today's rows per top range, then one combine (GROUP BY) and one `full_rollups` for the chunk.

Same files byte for byte at any chunk size (`test_newly_heavy_in_chunks_writes_the_same_bytes`); checkpoint parts are per chunk.

To measure on the next gcs drill: the `newly-heavy directories b/n (chunk …)` lines and the resource stages.

## Open

1. **Shard the newly heavy loop across Batch tasks** (gcs's proposal) if the chunked loop is still long. Each directory's rows depend on it alone, and the steps before it are checkpointed (`roots`, `probes`, `settle`, `counts`), so N tasks can restore the checkpoint, take chunks `[a, b)` of the shared `newly_heavy_chunks` order, and write their `heavy` parts; one task appends them in order and writes the kind. Costs N × the restore (the checkpoint's `dr` is 1.6B rows on 10-19) — worth it only if the loop stays well over that.
2. **`long: write`** is one Python thread over `write_sorted` (a `write_table` per 2,048-row group, `on_group`'s `as_py`s) and 33 `ORDER BY`s over the 1.6B-row `dr`, one per 50M-row file, each filtering `dr` by a `q` range. Files are cut at `q` boundaries and independent: sort `dr` once, then write the files N at a time on cursors (`static_names.each`), as `fold_gen` / `cut_gen -j` do.
3. **New members' whole history**: a literal entering the catalog costs its history once; 3,854 in one scan is the `passk-august-v1` naming family. Whether a run should take a literal's history (vs. the next base) is a policy question, not a bug.
