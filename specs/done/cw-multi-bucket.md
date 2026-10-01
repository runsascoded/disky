# cw-s3: scan every CoreWeave bucket, not just the quota bucket

**Why.** The 9/16 quota incident thread surfaced `hero-checkpoints` (US-EAST-08A, created 8/21): the hero run checkpoints there instead of into `marin-us-east-02a`, so it is the reason the run survived the 1 PB ceiling. It is 89 TiB / 402K objects (61 TiB under `tmp/ttl=14d/`, 11 TiB under `tmp/ttl=1d/`, 8.8 TiB under `marin/`), has Marin's ten TTL rules, no noncurrent-version rule, versioning never enabled, quota unknown, and cw-s3 has never scanned it. The other buckets our keys see (`marin-us-east-01a` empty, `marin-us-west-04a` 0.17 TiB, `rhoarnet-us-east-08a` 1.9 TiB, not Marin's) are noise and stay out.

**Shape.** The gcs deployment's shape: the store root is the provider, buckets are its children. The cw job already writes bucket-prefixed index paths (`marin-us-east-02a/marin/…`, depth 1 = the bucket) and a `tree.json` whose root wraps one bucket node, so the site's readers need no change — the job just lists, imports and indexes N buckets into the same scan, and the few places that hard-code *the* bucket (digest quota line, lifecycle snapshot, sweep plan prefixes) learn which bucket they are talking about.

## 1. Config: `CW_BUCKETS`, first = primary

- `CW_BUCKETS` (space-separated, ordered) is the deployment's bucket list; **the first is the primary**: the quota bucket, the sweep default, the intended-lifecycle bucket. Default `marin-us-east-02a hero-checkpoints` in `job/cw-run.sh` and `job/cw-batch-submit.sh`, so the scheduler body (which sets only `CW_BUCKET`) needs no edit; `CW_BUCKET` stays the primary (`${CW_BUCKETS%% *}` when unset) for `dt_cloud.sweep` / `lifecycle` / `expire-manifest` defaults.
- Site: `functions/_lib/cwBatch.ts` `CW_BUCKETS = ["marin-us-east-02a", "hero-checkpoints"]`, `CW_BUCKET = CW_BUCKETS[0]`.
- `stores.ts`: desc "the Marin CoreWeave buckets", `rootLabel: 'all buckets'`.

## 2. Job: one scan, N buckets (`job/cw-run.sh`)

Per bucket `b` in `CW_BUCKETS`: `bulk-list s3://$b` → `$WORK/listing/$b/`; `import -b $b` with `DISK_TREE_ROOT=$WORK/l2/$b` (one layer-2 per bucket, bucket-relative paths as today); copy to `cw-l2/$SNAP_ID/$b.parquet` (already bucket-named). Then, over all `<bucket>=<l2>` pairs:

- `python job/cw-webdata.py <outdir> <bucket>=<l2>…` (positional pairs replace `<l2> <outdir> -b`): root `{n: label, b: Σ, o: Σ, c: [bucket nodes in CW_BUCKETS order]}`; one byte floor (`min_frac × Σ`) folds each bucket's small dirs; `age.json` rows are the union (`d1` stays the first path component *within* a bucket, as gcs's fleet does); `meta.json` gains `buckets: {<name>: {total_bytes, total_objects}}` in list order, `total_bytes`/`total_objects` = the sums.
- `dt-cloud index-write -o <dir> <bucket>=<l2>…` (`write_index(sources=[(bucket, l2)…])`): the index rows are the UNION ALL of each bucket's rows (same `index_rows_sql`), sorted `(depth, path)`; the coarse floors derive from the fleet total (Σ depth-1 rows — at 767 + 89 TiB `round(log2)` is still 50, so the floors don't move). Summary `bucket` → `buckets: [...]`. A bare `<l2>` positional keeps meaning `$CW_BUCKET` (the tests' and ad-hoc form).
- `dt-cloud lifecycle pull -b a -b b -o lifecycle.json`: with more than one `-b` the file is `{<bucket>: Rules[]}` (one `-b` stays the bare `Rules[]`); the site reads both shapes (§4). `diff`/`push` stay single-bucket: `job/cw-lifecycle.json` is the primary's intended state; hero-checkpoints' live rules are Marin's ten and nothing tracks an intended state for it yet (out of scope: a per-bucket intended file).
- `WARM_PATHS` default adds `hero-checkpoints`, `hero-checkpoints/tmp`, `hero-checkpoints/marin`.
- `cw-batch-submit.sh` `vars()` passes `CW_BUCKETS` through when set.

Cost: hero-checkpoints is 402K objects — seconds of listing next to the primary's 30M; no sizing change.

## 3. Digest (`dt_cloud.cw_digest`): the quota line is the primary's

`rows_from_meta` reads the primary's `total_bytes`/`total_objects` from `meta.buckets[<primary>]` when present (older scans: the flat totals, which were the primary's), so month-to-date, weekly and daily deltas and `% of 1 PB` stay a2a across the switch. Each `Scan` also carries `extra: {bucket: TiB}` for the other buckets; the daily reply's tail and the OP headline append ` · <bucket> <TiB> TiB (Δ)` per extra bucket (Δ against the prior reply scan; omitted when the prior scan lacks the bucket). `load_tree` picks the bucket node by name (`c[] where n == primary`, else `c[0]`), so the diff treemap stays the primary's. The primary is `CW_BUCKET` (env, default `marin-us-east-02a`).

## 4. Site

- **Lifecycle fold** (`src/lifecycle.ts`, `LifecycleFold.tsx`): `parseLifecycle(json): Record<bucket, LifecycleRule[]>` — an array is `{[primary]: rules}` (scans before this spec), an object is taken as is. The fold diffs per bucket (`lifecycleDiff` unchanged, keyed by rule ID within a bucket), renders a `bucket` column, title `N rules · M buckets`. A bucket present only on one side reads as all-`new`/all-`removed`.
- **Plans** (`_lib/plans.ts`): `canonicalPrefix(raw)` resolves the bucket from the raw (`s3://<b>/…` or `<b>/…` for `b ∈ CW_BUCKETS`, else the primary) and stores `s3://<b>/<rel>/` — today a `hero-checkpoints/x/` item would silently canonicalize under the primary. `snapshotPlan` groups items by bucket; a plan spanning more than one bucket throws `PlanSpansBuckets` (dispatch → 400 with the buckets named; the executor is single-bucket and stays so), the keep carve-outs are filtered to the plan's bucket, and `plan.json.bucket` is that bucket. Dispatch passes `CW_BUCKET: <plan bucket>` into the Batch env; `jobs.ts` reflects deletion bands under `s3://<summary.bucket>/…` (the executor's summary already records `bucket`).
- Readers, marks (`s3://<bucket>/…` from the treemap path), series and the diff need no change. The unscoped size-over-time series steps up by hero's bytes at the first multi-bucket scan; scope to a bucket (`paths=`) for a continuous line. The first diff against a single-bucket scan shows `hero-checkpoints` as new, which is true.

## 5. Tests (exact-equality specs)

- `cloud/tests/test_index.py`: `write_index([(A, l2a), (B, l2b)])` → rows = both buckets' rows sorted `(depth, path)`, floors from the fleet sum, summary `buckets: [A, B]`.
- `cloud/tests/test_cw_digest.py`: `rows_from_meta` on a `buckets` meta → primary `tb` + `extra`; reply/OP strings with the extra clause; a mixed month (flat then `buckets` metas) has continuous deltas.
- `cloud/tests/test_lifecycle.py`: the multi-bucket `pull` file shape (`dump_map`).
- `site/src/lifecycle.test.ts`: `parseLifecycle` both shapes; per-bucket rows.
- `site/functions/_lib/plans.test.ts` (new): `canonicalPrefix` per bucket form; `groupItems` (the pure part of `snapshotPlan`) — one bucket, two buckets → throws.
- `job/cw-webdata.py`: a synthetic two-bucket run under `tmp/` asserting the `tree.json` root/bucket shape and `meta.buckets` (script, not pytest — the job dir has no test harness).

## 6. Rollout

1. Land + push; CI green.
2. `JOB=cw-run.sh job/build.sh` (Cloud Build → `:cw`), then one manual `job/cw-batch-submit.sh` so the next 12-hourly run isn't the first to exercise the path; the scan appears in the site's list with both buckets under the root.
3. CIC on preview + prod: root shows two bucket tiles; drill into `hero-checkpoints/tmp/ttl=14d`; the lifecycle fold shows both buckets; the filter box across buckets (`ttl=14d` matches in both).
4. The next `sender` reply (12:01Z) carries the ` · hero-checkpoints … TiB` clause; the OP headline too.
5. Ask CoreWeave (Isaac's thread) for the US-EAST-08A bucket's quota; until known the digest shows hero's size without a headroom figure.
