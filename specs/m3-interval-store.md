# m3: adopt the interval store

From the 10/19 talk prep (`~/c/oa/disky-deep-dive`): the deployments table shows every deploy on the interval store except m3. Ryan: "is m3/app not on the interval store? that's a problem, flag with m3".

## Where m3 is (2026-10-16)

- `site/wrangler.toml` sets no `PATH_STORE` (only `wrangler.example.toml` mentions it, commented out), so the site serves per-scan path + bysize sorts.
- R2 `disk-tree/interval-store/` is empty.
- The ingest (AWS Batch Fargate Spot, queue `disk-tree-m3`, triggered by `captures/m3/` → Queue → `disk-tree-capture-trigger`) never runs `dt-cloud interval-store add`.
- 24 laptop scans (2026-09-29 → 2026-10-16T1002, 2×/day since 10-11) + 2 NAS scans, each a full per-scan path store: exactly the "full scan's worth of data for every new scan" `specs/interval-store.md` exists to stop.

The others: gcs (gen `2026-10-09b`), cw-s3 (`2026-10-14cw`), r2 (`2026-10-15r2`), hf (`2026-10-14hf`) and r2.rbw.sh (`cloud`, `site/wrangler.r2.toml` `PATH_STORE = "intervals"`) all serve from it, with per-scan appends.

## Ask

1. Catch up on `cloud`'s interval-store code (`dt-cloud interval-store build|add|verify`, compaction, `_lib/index.ts` `openInterval`) via `local`.
2. Build a base generation from the existing laptop scans into R2 `disk-tree/interval-store/` (profile JSON as gcs's `job/interval-store.json`), and verify it against the per-scan stores (`interval-store verify`, as hf did: 11/11 views equal).
3. Add `interval-store add` to the Fargate ingest after the path-index step, so each capture appends a run.
4. Serve it: `PATH_STORE = "opt-in"` first (`ps=iv`), check the treemap, diff and series views on dev.disk.rbw.sh, then `PATH_STORE = "intervals"`.
5. NAS store: same, or say why not (77K files, so it matters less).
6. Afterwards, decide whether to prune the per-scan path stores in R2 `disk-tree` (propose; don't delete without Ryan).

## Done when

disk.rbw.sh serves from the interval store, and each new capture appends a run. Report the base row count and the latest delta's row count, so the talk table's "# intervals (stored)" cell for M3 + NAS can be filled in (today it says "—").
