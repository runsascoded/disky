# `disk-tree fetch` / `pull` / `sync`: config-driven personal deploy

Written 2026-08-15 (this session, from the personal-deploy design discussion). `main` is the
personal deploy of DT — track your own buckets (mostly S3 + R2) with dated scans, no domain
overlays; consumer forks (mgu) carry attribution/teams/etc.

## Verbs (git-shaped, deliberately)

The workflow is a one-way mirror (bucket → local index) with a natural layer split, so git's
fetch/pull vocabulary transfers cleanly (same pattern as `ghpr` / `thrds`):

| verb | does | git analogy |
|---|---|---|
| `fetch [BUCKET…]` | bulk-list → dated raw-listing shards (layer-1); no import | fetch (download, don't apply) |
| `pull [BUCKET…]` | fetch + import (layer-2 scan registered in SQLite) | pull (fetch + integrate) |
| `sync` | pull every configured bucket | — (the cron entrypoint) |

No `push` — nothing flows back to the bucket; a verb missing where the workflow has no
direction is a feature. `--force` follows the analogy too: `fetch -f` re-downloads (re-lists);
`pull -f` / `sync -f` redo the local side (re-import, reusing a complete listing).

## Cadence is the scheduler's job

Rejected `disk-tree daily`: only managed inventories (S3 Inventory, GCS SII) are daily-by-
nature, and the primary path here is DIY `bulk-list`, whose cadence is whatever the operator
schedules (12h, hourly, weekly). Both stages are idempotent per `(bucket, --date)` — fetch
skips when the dated listing dir has `_SUCCESS.json`; pull skips when a Scan row exists at
`(path, midnight-UTC-of-date)` — so any crontab frequency is safe to re-run:

```
0 */12 * * *  disk-tree sync
```

## Config: `<DISK_TREE_ROOT>/buckets.yml`

```yaml
listings: /path/or/url     # optional; default <DISK_TREE_ROOT>/listings
defaults:                  # optional; per-bucket keys win
  procs: 6
  threads: 8
  engine: stream           # pandas | duckdb | stream
buckets:
  - s3://my-bucket         # bare-string shorthand
  - uri: r2://my-r2-bucket
    endpoint_url: https://<acct>.r2.cloudflarestorage.com
  - uri: gcs://my-gcs-bucket
    prefix: some/subdir
    region: us-east-1
    pivot_sums: [storage_class_id]
    mean_mtime: true
```

Unknown keys error (typo guard). Raw listings land at `<listings>/<YYYY-MM-DD>/<bucket>/`
(matching the mgu `listing/<date>/<bucket>/` convention); scans record midnight-UTC of the
date as their snapshot time, which doubles as the idempotency key (one scan per bucket per
date; `pull -f` replaces the row in place rather than duplicating).

## Non-goals (v1)

- Access-log ingest in `sync` — follow-up once `dt access` sources are config-driven
  (earlier sketch: `sync --scans-only` / `--access-logs-only` split, two crontab lines at
  different frequencies).
- Scheduling itself (no daemon; cron/launchd/systemd own the timer).
- Remote listings-root freshness checks beyond `_SUCCESS.json` (fsspec handles `gs://` etc.).

## Status

- [x] `cli/sync.py`: config load (+schema errors), bucket selection, `fetch`/`pull`/`sync`
- [x] Refactors: `import_bucket()` extracted from `import_cmd` (adds `replace=` for in-place
      re-import); `bulk_list_uri()` extracted from `bulk-list`
- [x] `pyyaml` dependency
- [x] Tests (`tests/test_sync.py`): config parsing/errors in-process; command flows via
      subprocess against pre-populated dated listings (complete `_SUCCESS.json` ⇒ no cloud
      SDK touched); idempotency, force-replace, per-bucket extensions, named selection
- [ ] Live smoke against a real S3/R2 bucket (ryan)
- [ ] Access-log ingest plane in `sync` (follow-up)
