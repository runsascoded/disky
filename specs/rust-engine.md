# Rust engine: a self-contained disky

**Status:** in progress (2026-10-01). Phases 1–2 done (`capture` to a local dir or `r2://` / `s3://` / `file://`); phase 3 done (disky captures in-process; m3's R2-event ingest trigger live since 2026-10-02); phase 4 (local-only mode: Rust reduce, local read API, app wiring, the filter, diffs) done; open items below. Ryan: "i like the sound of porting to Rust"; support **both** a local-only mode and the hosted mode.

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
4. **Local-only mode:** no cloud at all. The laptop reduces its own capture, serves the read API the site's SPA calls, and the app window loads the SPA against it.
   - **4a ✅ reduce** (`crates/dt-index`, `dt-index -d ASOF -P INDEX_DIR -o SNAP_DIR CAPTURE_DIR`): what `dt-cloud path-index -g` writes. Both sorts are written row for row (`path-index.parquet` `(depth, path)` and `-bysize` `(⌊log2 size⌋ desc, path)`), with the same schema, zstd, 8K row groups and `tier`/`sort`/`bucket` kv, plus `meta.json` (bar its timestamps) and `age.json`. `Reducer::push` takes rows one at a time (a capture's shards, or later the walk itself), folds per-dir totals, rolls them up every ancestor prefix and folds `n_desc` bottom-up. It matches DuckDB on `epoch(created)::BIGINT` (round half to even), exact `wts` (i128), byte-order paths, NULL-last `d1`. The `.groups.*` footer sidecars are skipped, since a local reader has the footer.
     - Parity (`tests/test_dt_index.py`): a `/` capture and a nested-root (`/Users/ryan`) capture, every age bucket, half-second stamps, a zero-size file, `a-b` vs `a/b`, 20K bulk files over 3+ row groups: frames, schema, kv, row-group sizes, meta and age all equal.
     - Real tree (`~/c/oa`, 837K files → 947,555 rows): identical tables, meta and age; **2.2 s / 0.5 GB RSS vs 5.6 s / 1.2 GB**.
     - Found on the way: `path-index` gave a nested root's files their parent's depth (`len(split(name))+1`, missing the bucket's extra segments). Fixed as `[cloud]` `786bc13` (on `cloud` as `d0d59d4`).
   - **4b ✅ local server** (`dt_index::view` + `dt_index::http`, `dt-serve [-a ADDR] [-l ROOT_LABEL] -w WEB_DIR SCANS_DIR`, default `127.0.0.1:7792`): the read subset of the site's API from a scans dir (`<scan id>/{path-index*.parquet, meta.json, age.json}`), plus the SPA built with `VITE_STORE=laptop VITE_AUTH_MODE=public` (an `index.html` fallback for client routes).
     - Routes: `/data/laptop/scans.json`, `/data/laptop/<scan>/{meta,age}.json`, `/api/subtree` (`view.ts`'s unscoped fold, plus `depth=`), `/api/series` (+ `split=roots`), and `/api/age-pyramid` (the empty plan). Other `/api`, `/auth`, `/v1` paths return 404.
     - Row-group selection comes from the footer's statistics. `path` gives the `(depth, path)` ranges; `bysize` gives the groups whose max size clears the threshold. It reads whichever decodes fewer rows, the same choice the Worker makes.
     - Parity vs prod disk.rbw.sh (2026-10-02, the same capture reduced locally: 8,364,555 rows / 1,022 groups, identical to prod's index): 7 `/api/subtree` views were compared (root, `~`, `~` at `depth=1`, `~/c/oa`, `Library` at `minArea=6`, `~/Library/Caches`, `Applications` at `atten=1.5`). Every node's name, kind, b, o, d, f, ag and child order is identical, as are the threshold, node count and chosen sort, except the root label (`ROOT_LABEL`). The series point matches too.
     - Perf (8.4M-row scan): 13–100 ms per view, 3 ms per series, 340 MB RSS. CIC: the SPA renders the map, drills, `~` crumbs, table and age column with no console errors.
     - The reduce of that scan: 23 s / **3.2 GB RSS** with a `String` + `Row` per file; with file rows held columnar (one path buffer + per-column vecs) **17 s / 1.87 GB**, identical output. The rest is the per-dir maps (~1M dirs, three of them alive at once in `finish`); next lever if it matters.
   - **4c ✅ app wiring.**
     - **The scan job indexes locally** when it has `"local": true`, when the site is `local`, or when it isn't configured at all (local is the app's default mode). The walk feeds `dt_index::Reducer` directly: `dt_capture::walk` with no target, or `capture_tee` when an R2 `to` is also set, so one walk serves both. The scan is written whole to `<local_dir or ~/Library/Application Support/disky/scans>/<YYYY-MM-DDTHHMM UTC>` (built in `<id>.tmp`, then renamed in), keeping the newest `keep` (default 8, ~300 MB each for the whole disk). Progress phase `index` while it writes.
     - **The GUI serves** `dt_index::http` on `127.0.0.1:7792` over that dir, with the bundled SPA (`Contents/Resources/web`, built by `scripts/build-web` in `beforeBuildCommand`: `laptop` store, `AUTH_MODE=public`, `~` = the builder's `$HOME`).
     - **Site `local`** ("This Mac (local)" in the menu's Site submenu and Settings) points the window there.
     - Verified on m3 (2026-10-02): `disky job scan` with `{"local": true}` walked `/` (7,440,516 files, 285 errors) and indexed 8,387,739 rows in **2.5 min, 1.88 GB peak RSS**. The app's server lists it, and the SPA renders the whole machine (455 GiB) in Chrome.
   - **4d ✅ the filter (`q=`, `qs=`)** in the local server (`dt_index::query` + `view.rs` `filtered`), exact where the site approximates.
     - **Syntax:** `query.rs` ports `querySyntax.ts` / `pathQuery.ts`.
       - `simple`: AND, `|` OR, `-x` NOT, `*` within a segment, `"…"` literal, `/…/` regex, the 3-character minimum.
       - `regex`: one case-insensitive regex (the Rust `regex` crate, not JS's).
       - A parse error is a 400 `bad query: …`, which the box shows under itself.
     - **Semantics:** `readView`'s filter branch.
       - The match roots are the outermost paths under P that the query holds.
       - The exclusions are the outermost paths under a root that its negative part holds; their bytes and children come off every ancestor up to the root.
       - One threshold applies, the matched total's pixel budget, attenuated from each root's depth.
       - Responses carry `matches` / `matched` / `excluded` and `m: 1` on match nodes. A root matching with no negatives is the plain view, marked. `/api/series` takes `paths=`, summing the match roots per scan.
     - **Exact:** every row under P is read, in two passes in parallel over row groups.
       - Pass 1 decodes only `path`/`depth`/`size`. Substring and glob tests are monotone along a path, so only each chain's top is a candidate.
       - Pass 2 decodes full columns only for the groups that hold a root's or exclusion's own row, or a descendant big enough to keep.
       - Ancestor checks probe only the depths where roots sit.
       - One deliberate difference from the site: a root under the view's threshold folds into its parent's `(other)` instead of becoming a tile, and `matches` / `matched` are capped at the 50K heaviest (`truncated`). The site draws every root it found, but its reads keep that number small; read exactly, `/\.py$/` holds ~300K roots.
     - **Perf** (whole disk, 8.5M rows; machine at load ~18): 0.2–0.8 s for typical filters at the root, 0.2–0.3 s under `~/c/oa`. `test|tests` takes 1.1 s, and `/\.py$/` (~300K roots) 5.3 s. The first version, single-threaded with every column decoded, took 4–6.5 s for typical filters and 20 s for `/\.py$/`.
     - **vs prod** (disk.rbw.sh `2026-10-02`, its capture `22-13-23Z` reduced locally, 8,477,715 rows on both sides):
       - Where prod's answer is complete, the matches agree exactly, bytes and objects. That covers the 15 largest `node_modules` under `~/c/oa`, the 1 `ckpt` above prod's floor, and all 56 of prod's root `node_modules` matches.
       - Prod misses 61 more that are over its own per-depth threshold, at depths 6–10 (e.g. `~/c/hccs/ctbk/wt/deckgl/www/node_modules`, 1.0 GB, depth 9). It returns nothing for `*.safetensors` or `test|tests`. These responses are flagged `approximate` ("this scan has no search index; small matches may be missing"), but the misses aren't small. The laptop store has no search sidecars, so its thresholded fallback is the normal path. Handed to root as `specs/filter-no-index-misses.md` (in `~/c/disky`); not fixed here.
     - Test: `view.rs` `filter_view` (roots, an exclusion's net bytes, an only-negatives query, a matching root, no match). CIC: the bundled SPA filters (`?f=node_modules+-.pnpm`: 5.7 GiB matched, 107 prefixes) and shows the short-term error, with no console errors.
   - **4e ✅ diffs (`/api/diff`)** in the local server (`view.rs` `diff`), `buildDiff`'s walk read exactly.
     - **Semantics:**
       - Both scans are read at one byte floor, the larger side's pixel threshold. Expansion runs level by level: a node expands when it's on either side, its totals differ, and something is named under it.
       - A name one side lacks gets a point lookup on that side.
       - `(other)` is the parent less its named children on each side.
       - Rows are the skeleton, then the changed frontier by |Δ| (`top`, default 500).
       - `summary=1` returns totals only; `depth=N` caps the walk.
     - **Exact:** the lookups are point reads of the `path` sort with no budget, so `lookups_capped` is always false (the site caps at 240).
     - **With `q=`:**
       - Each side is its filter view, planned by its own matched bytes, then re-read at the larger threshold.
       - Lookups count only matched, un-excluded bytes. A match root's ancestor sums its roots' net bytes, from a sorted list of every root.
       - `matched` is the union of both sides' match roots.
     - **Refactor:** `subtree` now reads (`read` → `Read`: kept aggregates, threshold, the `(other)` base, folds, marks, and the filter's roots / exclusions / nets), then renders. The diff walks the same `Read`s. `/api/subtree` output is byte-identical to before on 18 views (3 paths × plain + 5 filters, whole-disk scan).
     - **vs prod** (disk.rbw.sh `2026-10-02` → `2026-10-03`, both captures reduced locally; `2026-10-03` = `10-15-03Z`, 8,533,237 rows on both sides): the root, `Users/ryan` and `Users/ryan/c` diffs are identical to prod row for row, in order (293 / 391 / 130 rows; same totals, threshold, tier, expansions, lookups). Local takes 0.09–0.25 s; prod takes 3.9–5.3 s.
     - **Filtered** (local only; prod's filter is approximate there): 1–3.6 s at the root. Totals equal each side's filtered `/api/subtree` root, and the `added` rows are real (that day's wrangler / workerd installs).
     - Test: `view.rs` `diff_view` (changed / removed / added / unchanged rows and their order, totals, `summary`, a filtered diff's rows and `matched` union). CIC: the bundled SPA's Diff section on two local scans (`10/2 → 10/3`, +3.3 GiB; with `?f=node_modules+-.pnpm`, +11 MiB), no console errors.
   - Open:
     - `~` is baked in at build time (fine for one user, wrong for anyone else's build).
     - Reduce memory (~1.9 GB for the whole disk) is transient, in the scan job's process.
5. **Hosted mode for other people:** per-user storage + auth on a shared deployment (the path store is already multi-store; `@open-athena/auth` handles sign-in). Upload = phase 2 against a per-user prefix with a scoped credential minted by the site (no AWS keys on the laptop). Shape TBD with Ryan.

## Open questions

- Phase 4 UI: serve `site/`'s built SPA from the app with a local API shim, or point `ui/` (the older Flask-era UI) at a Rust API? `site/` is where the product is; prefer it.
- Distribution: Developer ID + notarization (Ryan is looking into the cert) before anyone else installs it.
