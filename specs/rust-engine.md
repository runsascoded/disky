# Rust engine: a self-contained disky

**Status:** in progress (2026-10-01). Phase 1 done (local `capture`). Ryan: "i like the sound of porting to Rust"; support **both** a local-only mode and the hosted mode.

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
2. **Remote target** (`-t r2://bucket/prefix`, `s3://`): stream shards to an S3-compatible store with the bucket's endpoint (`DISK_TREE_R2_ENDPOINT_URL` / `buckets.yml`) and credentials from an AWS profile (`AWS_PROFILE`; parse `~/.aws/credentials` + `config`, since `object_store` doesn't). Candidates: `object_store` (S3 builder with endpoint) or `aws-sdk-s3`; prefer `object_store` (lighter, multipart).
3. **disky runs it in-process:** `disky job scan` (no `command` configured, or `engine: rust`) captures `settings.scan_root` itself — the walk happens in the app binary (strongest TCC form) — then triggers the ingest. Ingest trigger options: keep `aws/submit` (Batch `SubmitJob`, needs AWS creds on the laptop) vs. an R2 event / scheduled ingest that picks up new `_SUCCESS.json`s (no laptop creds beyond R2 write). Prefer the latter for other users.
4. **Local-only mode:** no cloud at all. Rust `reduce` (layer 1 → layer 2, the DuckDB engine's aggregation: per-dir size/count roll-up, depth, mtime stats, `(depth, path)` sort, 64K row groups) into `~/Library/Application Support/disky/scans/`, and the read subset of the `/api/*` contract served by the app on loopback (the same contract `ui/functions` implements over R2 with hyparquet: `/api/scans`, `/api/scan`, history). The window then loads `site/`'s SPA against the local API. Diffs between local scans: port `diff-index` later.
5. **Hosted mode for other people:** per-user storage + auth on a shared deployment (the path store is already multi-store; `@open-athena/auth` handles sign-in). Upload = phase 2 against a per-user prefix with a scoped credential minted by the site (no AWS keys on the laptop). Shape TBD with Ryan.

## Open questions

- Phase 4 UI: serve `site/`'s built SPA from the app with a local API shim, or point `ui/` (the older Flask-era UI) at a Rust API? `site/` is where the product is; prefer it.
- Distribution: Developer ID + notarization (Ryan is looking into the cert) before anyone else installs it.
