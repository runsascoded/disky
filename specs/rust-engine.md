# Rust engine: a self-contained disky

**Status:** in progress (2026-10-01). Phases 1–2 done (`capture` to a local dir or `r2://` / `s3://` / `file://`); phase 3 done app-side (disky captures in-process), the cloud-side ingest trigger pending on `m3`. Ryan: "i like the sound of porting to Rust"; support **both** a local-only mode and the hosted mode.

## Why

Today disky's jobs run `m3`'s checkout: `.venv/bin/python aws/laptop-scan` (Python `disk-tree capture` → R2, `aws/submit` → AWS Batch reduce/ingest → D1 + R2 → disk.rbw.sh). That's fine for one laptop, impossible to hand to anyone else. A Rust engine inside the app makes disky self-contained (no Python, no Homebrew `gfind`, no checkout), and is the prerequisite for any App Store build (sandboxed apps can't exec a venv; see `tauri-native-app.md`).

## Compatibility contract

The Python engine stays the reference. Rust writes the **same formats**, so every consumer (DuckDB reduce, Batch ingest, the path store, `site/`) reads either:

- **Layer 1 (capture):** `<to>/<host>/<root slug>/<YYYY-MM-DDTHH-MM-SSZ>/shard-NNNNN.parquet` + `_SUCCESS.json`, exactly `disk_tree/cli/capture.py`'s output (schema, snappy, 64K row groups, `batch_rows` per shard, manifest keys incl. `container`).
- **Layer 2 (scan blob)** and the path-store tiers: Python's `reduce` / `tiers` output (phase 4).

Each phase lands with a parity test against the Python implementation (`tests/test_dt_*.py`, skipped when the binary isn't built).

## Phases

1. ✅ **`dt-capture`, local target** (`apps/tauri/crates/dt-capture`). In-process `dt-walker` walk (`Walker::walk_records`, a new `Sink` callback API) → arrow-rs parquet shards → manifest, plus the APFS container (`apfs.rs`, the twin of `disk_tree.apfs`). Files only, sizes = blocks × 512, names relative to the root (lossy UTF-8, like `run_gfind`), `--one-fs`, CloudStorage excluded.
   - Parity (`tests/test_dt_capture.py`): identical rows, dtypes, parquet schema, shard layout and manifest (bar `time`) vs `disk-tree capture` on a fixture with a symlink, a non-ASCII name, an empty dir and a 2-row batch size.
   - Real tree (`~/c/oa`, 836,855 files): identical name sets, 3 value diffs (files another session edited between the runs); **7.2 s vs 24.0 s** (3.3×). `disk-tree reduce` reads the Rust capture (946,913 layer-2 rows).
2. ✅ **Remote target** (`-t r2://bucket/prefix`, `s3://…`, `file:///dir`): `target.rs` writes each shard and the manifest whole (one PUT; shards are bounded by `batch_rows`) through `object_store` 0.14 (S3 builder; `aws-lc-rs` TLS) on a current-thread tokio runtime. `creds.rs` resolves what the Python engine does: endpoint = `DISK_TREE_R2_ENDPOINT_URL`, else the bucket's (or `defaults`) `endpoint_url` in `buckets.yml`; credentials as botocore does (a bucket `profile:` wins, then `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, then the shared files' `AWS_PROFILE` or `default` section, keys from `~/.aws/credentials` or `config`); region `auto` for R2, the profile's or `AWS_REGION` for S3. Like `blobfs.fs_for`, a bad target fails before the walk. The printed capture dir is a URL under `--to`.
   - Parity (`tests/test_dt_capture.py`): the fixture test also runs with `file://` targets (the store code path), plus the identical up-front refusal for `r2://` with no endpoint.
   - Real R2 (2026-10-01, `AWS_PROFILE=m3`, a scratch prefix, deleted after): identical object lists, parquet schema, rows and manifest (bar `time`/`container`) vs `disk-tree capture -t r2://…`.
3. **disky runs it in-process:** ✅ app side. The scan job may name a `to` instead of a `command` (`jobs.rs`): `disky job scan` then applies the job's `env` to its own process (credential lookup reads it), captures `settings.scan_root` with `dt-capture` linked into the app (`-o` for `/`; `host` from the job, else `DISK_TREE_HOST` / the hostname), and runs the optional `then` command with the capture dir appended. The walk is the app binary's own, the strongest TCC form. Log lines go to `index.{out,err}.log` as `aws/laptop-scan`'s did. The run state (`last_start`/`last_end`/`last_exit`, interrupted-run retry) is unchanged: a SIGTERM now kills disky itself, which leaves `last_end < last_start` and so still reads as interrupted.
   - m3 since 2026-10-01: `{"to": "r2://disk-tree/captures", "host": "m3", "env": {"AWS_PROFILE": "m3", …}, "then": {"command": [<m3 venv python>, "aws/submit"], "env": {"AWS_PROFILE": "r"}}}`, replacing `aws/laptop-scan`.
   - **Ingest trigger:** Ryan chose the cloud-side option, so the laptop needs only R2 write. `then` = `aws/submit` is the bridge until it lands. The design (R2 event notification on `_SUCCESS.json` → Queue → a Worker that signs Batch `SubmitJob`, deduped by an `_INGEST.json` marker) is m3's spec `capture-ingest-trigger.md` (the `aws/` infra is on `m3`). Cutover: drop `then`.
4. **Local-only mode:** no cloud at all. Rust `reduce` (layer 1 → layer 2, the DuckDB engine's aggregation: per-dir size/count roll-up, depth, mtime stats, `(depth, path)` sort, 64K row groups) into `~/Library/Application Support/disky/scans/`, and the read subset of the `/api/*` contract served by the app on loopback (the same contract `ui/functions` implements over R2 with hyparquet: `/api/scans`, `/api/scan`, history). The window then loads `site/`'s SPA against the local API. Diffs between local scans: port `diff-index` later.
5. **Hosted mode for other people:** per-user storage + auth on a shared deployment (the path store is already multi-store; `@open-athena/auth` handles sign-in). Upload = phase 2 against a per-user prefix with a scoped credential minted by the site (no AWS keys on the laptop). Shape TBD with Ryan.

## Open questions

- Phase 4 UI: serve `site/`'s built SPA from the app with a local API shim, or point `ui/` (the older Flask-era UI) at a Rust API? `site/` is where the product is; prefer it.
- Distribution: Developer ID + notarization (Ryan is looking into the cert) before anyone else installs it.
