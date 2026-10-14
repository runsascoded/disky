# Footer LRU: path-store footers in D1 as a cache

The site plans row-group reads from footer rows in D1 (`index_row_groups`, written by `dt-cloud index-sync`). A (scan, variant) without rows there reads its cold footer (`<tier>.groups.parquet`) instead. Today D1 is bounded by a fixed retention: `dt-cloud index-gc -r 30` (gcs) keeps the newest 30 scans' sorts. This spec replaces that with an LRU in the shared base, so every deployment gets it: the scans people actually read stay in D1, whatever their age.

## Table

Migration `site/migrations/cw/0022_footer_cache.sql` (0021 is reserved for another change). One row per (scan, variant, generation), `variant` as D1 names it (a secondary store's `<store>:<variant>`):

| column | meaning |
|---|---|
| `date`, `variant`, `gen` | the PK, as `index_row_groups`' prefix |
| `state` | `filling` / `hot` / `evicting` |
| `rows`, `bytes` | the entry's rows, and their text columns' lengths + 64 per row |
| `started_ts` | epoch s of the fill claim, or of the demotion |
| `filled_ts` | epoch s the last batch landed |
| `last_access_day` | epoch day of the newest read |

Each deployment applies the migration before deploying the code. Without the table, the reader runs exactly the pre-LRU query and queues nothing.

## Reader (`site/functions/_lib/footerCache.ts`, `openIndex`)

One query per handle open returns whether the pointer's generation has any rows, and its entry:

- `hot`: plan from D1. Record the access.
- no entry, rows present: plan from D1. This is an untracked set: a sync from before the table, or a variant the LRU doesn't manage. Fills always claim before their first row, and eviction deletes rows before its entry, so rows without an entry are always a complete set.
- no entry, no rows: a **miss**. Serve the cold footer and queue a fill.
- `filling`: serve cold. If the claim is older than 15 minutes, its isolate died, so queue a fill that takes it over.
- `evicting`: serve cold. Never refilled until the entry is gone.

A footer with more than 200K rows is never filled. That cap keeps a fill inside a `waitUntil`.

**Writes never run on the request's path.** `openIndex` queues them, and `_middleware.ts` starts them after the response with `waitUntil(footerCacheDrain())`.

- **Fill**:
  1. Claim with `INSERT … ON CONFLICT DO UPDATE … WHERE state = 'filling' AND started_ts < stale`. If the claim changes no row, the fill stops (`busy`).
  2. Insert one D1 batch per cold-footer group. Each batch is `INSERT OR IGNORE … SELECT * FROM (VALUES …) WHERE EXISTS (entry still 'filling')`, with literal statements of at most 90 KB. Once the claim is gone (evicted or retired), no further rows land.
  3. Mark the entry `hot` in one statement, which also checks that the claim is unchanged and that `COUNT(*)` equals the rows inserted.

  Two isolates filling at once are safe: one claim wins, and the duplicate rows are ignored.
- **Access**: `UPDATE … SET last_access_day = today WHERE … AND last_access_day < today`. It is queued only when today is past both the stored day and the day this isolate last wrote, so page views make at most one write per entry per day per isolate.

## Eviction

The same pass runs in two places: `evictFooters` (TS) and `dt_cloud.index_footer.evict` (Python).

1. Read the `hot` entries ordered by `(last_access_day, date, variant, gen)`, oldest first. Demote entries to `evicting` until the remaining ones fit the budget. Readers go cold at once.
2. For each entry that was demoted at least `EVICT_GRACE_S` (120 s) ago, delete its rows, then its entry. The grace must be longer than a reader handle's 60 s lifetime.

The budget is `FOOTER_CACHE_MAX_ROWS` (default 1,000,000 rows, about 0.5 GB with indexes) and, optionally, `FOOTER_CACHE_MAX_BYTES`. Neither default is specific to a deployment.

The pass is exposed in three ways:

- `POST /api/footer-cache[?dry=1]`: admin only. Runs one pass and returns `{budget, hot, demoted, deleted}`. `GET` returns per-state totals.
- `dt-cloud footer-evict [-B rows] [-y bytes] [-g grace] [-n] [-W]`: the scan job's call. By default it runs a pass, waits out the grace, and runs a second pass that deletes this pass's victims. `-W` runs one pass only. `-A` first *adopts* pre-LRU store sorts: each pointed sort with rows in D1, no entry, and a cold footer on disk becomes `hot` with today as its read day, so the first evictions order them by scan, oldest first. Run `-A` once after the migration.
- `index-gc -r N` keeps its meaning. It is now LRU-coherent: a retired sort's entries go to `evicting` before its rows are deleted, then the entries are deleted. A scan it retires is refilled on its next read, and retired again by the next run. To switch a deployment from `-r N` to the budget, run `footer-evict -A` once, then `footer-evict` in place of `-r`.

## Writer coherence (`dt_cloud.index_footer`)

All of the following apply only where the table exists. The writer checks once per call with `sqlite_master`.

- `sync_d1`, for a store sort with its cold footer written: first delete the entries of the swept generations, then claim `filling`, upload the rows, and mark `hot` with the same count check. A reader serves cold while a re-sync of the pointed generation runs. Other variants (the age pyramids, over-time) stay untracked and are never evicted.
- `gc_d1`: deletes the swept generations' entries before their rows.

## Not done / open

- The response bodies are unchanged, so `RESPONSE_V` is not bumped.
- The local wrangler path of `sync_d1` (`-L`) writes no entries. Its sets are untracked, and readers trust them as before.
- The interval store's handles (`openInterval`) are unaffected: they never use `index_row_groups`.
