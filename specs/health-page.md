# `/health`: the state of the append-only stores

Every deployment gets a stock `/health` page: each append-only store's stack drawn as binary-counter shards over the scan timeline, every scan's coverage (and its gaps), and freshness. `/scans` stays the per-run job table; `/health` is the state of the stores. They link to each other, and both are in the ☰ menu and the omnibar's Pages.

## Model: pyrmts' health timeline

Ryan's pyrmts consumers draw their `/health` as dyadic shards over a timeline: awair uses `pyrmts-react`'s `<CoverTimeline>`; ctbk still has a hand-rolled copy (`CoverBars`). disky **reuses `pyrmts-react` as-is**, from the pyrmts `dist` branch at the SHA the site already pins `pyrmts` / `pyrmts-cfw` at (`ad35ff8`; its `pyrmts` transitive resolves to the dist head, as `pyrmts-cfw`'s already did). `CoverTimeline` takes `PyramidTierCoverStatus[]` rows (`pyrmts`' `cover-status.ts`): one row per tier, one segment per shard, each `present | pending | missing`. disky's rows come from its own stacks (`stackTiers`, `coverageTiers`), not from `pyramidCover` (which reads a D1 shard registry the run stores don't have).

What the page adds around it:
- **Row labels as an HTML column pinned over the SVG's label gutter** (the SVG's own labels hidden): the timeline scales with its width (its viewBox is fixed-aspect), so on a phone it keeps a 720 px minimum and scrolls inside itself, starting at its newest end; the labels stay put.
- **A day axis** under each timeline (the component's gridlines are monthly) at the same x mapping, and one legend for all timelines (the per-timeline legends hidden).
- Theming: the `--pyrmts-*` vars set from the site's tokens (light and dark), the portaled `.tt-tooltip` restyled like the site's tooltips.

pyrmts follow-ups worth upstreaming (not done here): a `labels: false` / external-label mode and a pluggable axis (days vs months) in `CoverTimeline`; an `end` that reads inclusively in the tooltip (a run's segment ends at the next scan, so its tooltip reads "10-08 → 10-10" for scans 10-08 and 10-09).

## API: `GET /api/health`

`site/functions/api/health.ts`, gated `requireViewer` (like `/api/scan-runs`). Held in the colo cache for 60 s keyed by store and generations (`?fresh` bypasses it); `cache-control: private, max-age=30`.

Sources, each optional (an unconfigured store is absent; no D1 = no per-scan / job data):
- **Interval store** (`INTERVAL_STORE_GEN` + `INDEX_R2`): `interval-store/<gen>/scans.json` (the base), the newest manifest under `manifests/` (`manifests.ts`' `newestManifest`; its R2 `uploaded` is the store's last append or merge), and per listed run whether `served/{path,bysize}{,.groups}.parquet` are all on R2 (`r2_publish` copies the manifest after them).
- **Static name index** (`STATIC_GEN` + `INDEX_R2`): `static-names/<gen>/scans.json`, the newest manifest, and per tier dir (the base and each run) the markers `catalog/meta.json` (light), `drill/meta.json`, `anchors/meta.json` (+ `anchors/start/meta.json` when the base carries the starts-with catalog), as `static_runner.has_tier` checks them.
- **Merge status** (each store): `<root>/merging.json` on `INDEX_R2` (`readMerging`), read by key — see *Merges in progress* below. Absent or malformed: none (it never breaks the store's read).
- **D1**: `index_schema` `path` rows (`pathScans`: the per-scan path store) and `scan_runs` (`loadScanRuns`: the head runs' scans and statuses).

The document (`src/healthModel.ts` `HealthDoc`, DOM-free, shared with the page):
- `stores[]`: per store its read (gen, base scans, newest manifest, revision / `revises`, upload time, runs with `r2` and static `tiers`), plus `carries` (the due merges: `append_runner.plan_carries` ported as `planCarries`, drill-mixing and the compaction level respected), `compaction` (`run_scans` of `2^level`, highest run level, `due` when two adjacent runs sit at `level − 1`), `served` (the scans each tier serves: the base plus the runs up to the first one without the tier, the readers' cut rule) and `cut` (that first run).
- `coverage[]`: per scan, cells `path`, `interval`, `light`, `drill`, `anchors`, `r2`, each `present | missing | pending | na`, and its `gaps`. `path` is the per-scan store or the interval store; a store's columns are expected from its base's first scan on (`na` before it, or when the tier isn't configured); a scan newer than a store's newest while its job is running is `pending` (its job matched by scan id over every `scan_runs` row, downstream jobs included, `started_ts` not needed; or, when some job is running, any scan newer than every store's newest); `r2` is `missing` when a store lists the scan but its run isn't whole on R2.
- `stores[].merge`: the store's merge status record as `MergeState` (`mergeState`, aged at the document's `now`): `status` `merging`, `stuck` (no heartbeat for `STUCK_S` = 30 min, or held past its `lease_s`, default `LEASE_S` 4 h) or `failed`, with `age`, `beat_age` and `why`; null when no record.
- `gaps`: missing counts per column; `freshness`: the newest scan and its age (from its job's earliest `started_ts` when one is recorded, else the scan id's time: a date-only id is 00:00 UTC), and per store its newest scan, age, and last manifest upload's age.

The compaction level is `COMPACT_LEVEL` (5, `append_runner`'s) unless the manifest carries `compact_level` (no profile key exists yet on `cloud`; the reader takes one when the runner writes it).

## Page: `/health`

`src/HealthPage.tsx` (React Query `['health']`, 60 s refetch):
- **Freshness**: the newest scan, each store's newest scan and last append, and the last scan job (status chip, start, duration, phase bar), from `/scans`' own fetch. Both pages order runs by `runTime` (`scanRunsModel.ts`): `started_ts`, else the scan id's time, else `updated_ts` — the in-job runs before `6842ed63` have `started_ts` NULL and sorted last.
- **Window** (7d / 30d / 90d / all) for every timeline, and the legend.
- **Per store**: facts (base span, runs, newest manifest and revision, newest scan), the compaction gauge, a merge in progress (`mergeLine`: "merging since 06:00 UTC: 2 runs → deltas/a_b (L1)", the date too when it isn't today; a chip `merging` (blue), `stuck` (amber, with why: "no heartbeat for 40m") or `failed` (red, with the error); the holder job), the due carries not being merged, any cut tier, and the stack timeline: `base`, rows `L<level−1>` … `L0` (each run a shard over its scans' span; a due carry's output a `pending` shard on its level, labelled "merging of …" / "stuck merge of …" instead when a merge has planned it — a failed merge holds nothing, so its carry reads as due again; a planned output R2's manifest doesn't list yet is drawn on its level too), and for the static index `drill` / `anch` rows (each tier dir present or missing). A run not whole on R2 is `missing`.
- **Coverage per scan**: the gap counts, a coverage timeline (one row per live column, one shard per scan), and a table of the scans with gaps (or every scan), newest first.

Shared with `/scans` (`src/scanRunsUi.tsx`, extracted from `ScansPage.tsx`): the scan-run list fetch (one React Query entry), `Status` chip, `PhaseBar`, phase colors, `fmtDur` / `utc`; the page rides `/scans`' `staged-page scans-page` frame and styles.

## Merges in progress

The merge lease lives in the job's GCS scratch bucket (`<scratch>/<prefix>/<gen>/merge.lease.json`), which the site has no binding for, so the job holding it mirrors it to R2 beside the store: `<prefix>/<gen>/merging.json` (`append_runner.StatusMirror`, `MERGE_STATUS`).

- **Record**: `{holder, owner, state: merging | failed, started_ts, updated_ts, heartbeat_s, lease_s, manifest, merges, done[, error]}` — `holder` the merge's Batch job name (`$DT_JOB_NAME`, which `Runner.carries` sets on the merge job; else the lease owner), `merges` the carries still planned from the newest manifest (`{inputs, output, level, first, last, scans}`; the first is the one building), `done` the merged runs published so far (`{output, manifest}`), times in epoch s.
- **Lifecycle** (`merge_pending`): written once the lease is taken and a carry is due, rewritten as each merged run's revision lands (the next carry planned), its `updated_ts` refreshed every `HEARTBEAT_S` (5 min) by a daemon thread; removed when the job ends cleanly (nothing more due, or `-N`) — any stale or failed record an earlier holder left included; on a raise, kept as `state: failed` with the error (the lease is released; the next merge job overwrites it). A job killed outright (preempted, OOM) leaves it `merging` with a heartbeat that stops: `/health` calls it stuck after 30 min. A held lease (another merger) leaves the record alone.
- **Best-effort**: every write's failure is logged (`merge status: write failed, not fatal: …`) and swallowed; the R2 client has short timeouts (5 s connect, 10 s read, 2 attempts), so a status write never holds a merge up. A job without the R2 env (`R2_ENDPOINT`, `R2_BUCKET`) logs that /health won't see it and merges the same (`r2_merge_status`).
- **Credentials**: the merge job (`static_merge carry`, `interval_append carry`) gets the R2 copy's env and secrets (`job_spec(r2=True)`) when the R2 account is the stages' own (`Runner.merge_r2`: `r2_sa` unset or equal to `sa` — gcs and cw both): a job runs as one account, and the merge's writes the data bucket, so with a separate R2 account the merge runs without the record.
- **Readers**: no reader lists a generation's root (manifests are found under `manifests/`, by name: `newestManifest`), so the record is never read as data.

## Not covered

- **GCS-side state** ahead of R2 (a manifest published on GCS whose R2 copy hasn't run) is invisible for the same reason: the page shows what the readers serve.

## Tests

- `src/healthModel.test.ts`: `planCarries` (N-way chained carry, drill-mixing, compaction level), `storeHealth` (due carry, gauge, served / cut), coverage over a fixture with a scan missing from the interval store and a drill-less static run (plus a run not whole on R2 cutting the light stack, and an unconfigured store), freshness, and the stack → shards layout (exact segments per row); a gcs-shaped fixture (2026-10-10) with NULL-`started_ts` running and succeeded rows: the running scan `pending` (gaps once its job stops), the unmatched-running-job fallback, ages from `started_ts`. `src/scanRunsModel.test.ts`: `runTime` ordering over NULL starts.
- `functions/_lib/health.test.ts`: `/api/health` over a fake `INDEX_R2` and the cw-lineage D1: both stores, a revision manifest, the gaps, a broken run, a missing `scans.json`, no stores; a store's merge record (merging, stuck once its heartbeat stops, malformed or absent → none).
- `src/healthModel.test.ts` (merges): `mergeState` (fresh, at the 30 min edge, stuck by heartbeat, stuck by lease, the default lease, failed), `mergeLine` (one / two merges, another day, failed), the planned output's pending shard in place of the due carry (merging / stuck / failed → due again, a planned output R2 doesn't list yet, one already listed skipped), `healthDoc` aging it at its `now`.
- `cloud/tests/test_static_merge.py` (status record, over real runs): written / rewritten / removed across a two-carry merge (and what R2 holds mid-build, i.e. after a kill: `merging`, heartbeat stopped), the heartbeat advancing `updated_ts` only, a raise → `failed` kept until the next merge, failing writes never failing the merge, a held lease leaving it alone. `test_append_runner.py`: the R2 sink's put / delete and its env. `test_static_runner.py` / `test_interval_append.py`: the merge job's R2 env and `DT_JOB_NAME` (none with a separate R2 account).
- Mutation check (merges, each reverted, each fails ≥ 1 test): stuck at `>=` 30 min, no lease rule, the due carry not replaced, a failed merge still drawn, a listed output drawn, the date always dropped, the record's `merges` unchecked, aging at the wall clock; Python: no record write, no `failed` mark, no clear, write failures raised, no heartbeat, `started_ts` reset per write, holder ignoring `$DT_JOB_NAME`, R2 env regardless of account.
- Mutation check (manual, each reverted): served-prefix → all runs, dropping the drill-mixing guard, carrying into the compaction level, never `pending`, ignoring `r2`, and an off-by-one span end each fail 1–4 tests. The 10-10 fixes: reverting `summarize`'s sort to `started_ts ?? 0`, the pending rule to scan-id-only, and the ages to the scan id each fail (3 tests).

## Verification

Local stack with synthetic data (`site/./dev --local-db` over an untracked local `wrangler.toml`: local D1 seeded with `index_schema` / `scan_runs` rows, local R2 with both stores' `scans.json`, manifests and run files; generator in `tmp/health-local/gen.py`), checked in Chrome in dark and light, desktop and phone width. Not deployed.

Merges in progress (2026-10-10): the same stack plus a `merging.json` per store put to local R2 (the interval store's fresh: blue `merging` chip, its L1 carry drawn as a `merging` pending shard and dropped from the due line; the static index's 3 h without a heartbeat: amber `stuck` chip with "no heartbeat for 3h04m"), checked in Chrome in dark and light at desktop width.
