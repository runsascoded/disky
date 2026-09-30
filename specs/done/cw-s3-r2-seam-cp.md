# CP manifest: cw-s3 → `cloud` — store seam + `publish-r2` (2026-09-23)

**Landed on `cloud` (2026-09-23):** `1edb394` → `79ce59f` (`storeTarget` + `v1/files`/`todo` on the seam; `storeCreds`/`storeReady` + the readiness guards were already here; `cw-l2/` and `cw-sweep/` dropped from the allow-lists — a cw deploy sets `STORE_PREFIXES`), `a602991` → `c7da7f6` (`publish-r2`, with the served-subset layout parameterized: `-s`/`$SNAPSHOTS_SUBDIR` for the snapshots subdir, `-l`/`$LAYER2_PREFIX` as a `{scan}` template defaulting to the base's `listing/{scan}/index/`; cw passes `-s cw -l 'cw-l2/{scan}/'`). `541de55` skipped: `cloud/uv.lock` here already pins pyarrow 22. The IaC notes below stay open for the `cfn` extraction. CP cursor: cw-s3 `aa0e97b`.

From cw-s3 (`~/c/oa/marin-gcs-usage/wt/cw-s3`, branch `cw-s3`, HEAD `aa0e97b`). Two base-worthy commits to land on `cloud` (adapted), plus IaC notes for the `cfn` extraction. Context: cw-s3 `specs/r2-serving-migration.md` (`635461e`) — cw is adopting the base's S3-over-R2 `STORE_*` serving; these generalize the base's own seam so that when cw-s3 rebases (`cw-s3-next` off `cloud`) its seam port collapses to the cw-only delta. Same flow as the chart CP (`d830571` → your `3150d99`): land here first, then cw rebases onto it.

## Land on `cloud`

1. **`1edb394` functions: store seam — `storeTarget` + `/v1/files`, `path-index`, readiness guards on `STORE_*`.**
   - New for the base: `storeTarget(env)` (endpoint / bucket / region in one place) in `_lib/index.ts`, and `v1/files/[[path]].ts` moved onto the seam — the base's raw-store browser still hardcodes GCS, which is inconsistent on the r2 deploy (the Map serves R2 while `/v1/files` browses GCS). Also `api/path-index`, `api/todo`, and `storeReady(env)` guards where the base lacks them.
   - Already in the base (cw catching up — skip on CP): `storeCreds` / `storeReady` themselves, `data/[[path]]` on the seam.
   - **Drop on CP (cw-only):** `cw-l2/` and `cw-sweep/` in the default allow-lists (`_lib/index.ts` default prefixes, `api/path-index` prefixes, `v1/files` prefixes) — the base's tiers live under `listing/<date>/index/<gen>/`.
   - Tests: `_lib/index.test.ts` "store seam" specs are generic.

2. **`a602991` cloud: `dt-cloud publish-r2` — copy a scan's served subset GCS→R2, idempotent on size+md5.**
   - Generic "publish a scan's served subset from one S3-compatible store to another" (`cloud/src/dt_cloud/publish.py`, `tests/test_publish.py`, the `cli.py` command). The prefix set is parameterized (`-p`); the defaults are cw's layout (`snapshots/<subdir>/<scan>/`, `cw-l2/<scan>/`) — adapt the defaults to the base's (`listing/<date>/index/<gen>/`) or leave them to `-p`.
   - `boto3` via the existing `s3` extra (lazy import). cw-s3 `541de55` separately re-locks `uv.lock` for the `overtime` extra (pyarrow 22); your lock post-`0b82648` presumably already carries those pins — take it only if yours is also stale.

## Not for CP (cw deployment delta)

`b338ece` (cw-digest quotas), `12d3ce2` (`cw-run.sh` 4b warm-cache stage), `aa0e97b` (`wrangler.toml` R2/KV stanzas), and the `cw-l2/` allow-list lines called out above.

## IaC — for the `cfn` extraction, when you take `CfnDashboard`

`~/c/oa/marin-gcs-usage/cf/cfn_dashboard.py` (untracked, mgu main checkout) gained: `Store.r2_bucket` / `r2_location` / `r2_token_permission_groups[_ids]`; component children `r2` (`R2Bucket`) and `r2-token` (`ApiToken`, R2 bucket-item read+write, account-scoped); exports `r2_bucket`, and as secrets `r2_s3_access_key_id` (= the token id) / `r2_s3_secret_access_key` (= sha256 of the token value — R2's S3-credential derivation, so the token *is* the credential). Also a `kv_name` for the edge cache's global `CACHE_KV` tier. Gotcha: permission groups resolve by name via the list API, which a read-scoped CI token can't call — CI `preview` needs `r2_token_permission_group_ids` set explicitly. All generic; belongs in the extracted component.

## Also FYI (measured, no action)

walkDiff (pyrmts index-free walk) at cw scale, fleet root, in workerd: ~2.3–4 s wall, 44 range GETs / ~3.5% of the pair's bytes / 0 footer reads via a `.groups.json`→`RowGroupIndex` adapter, **10 serial dependent rounds** — round-bound against cross-provider GCS, not CPU-bound (pyrmts `cpuMs` over-counts awaited I/O). That's the "why R2" behind these commits; details in the cw-s3 spec §1 and the `diff-perf` notes. Bench harness lives in cw-s3 `tmp/walkdiff-bench/` (untracked).
