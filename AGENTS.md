# disk-tree

Cloud storage usage analyzer: a scanning/indexing CLI (`disk-tree`), a cloud overlay (`dt-cloud`), and a Cloudflare-hosted site (`site/`) over the indexes.

## Project Vision

Track storage usage across object stores: S3, R2 and GCS buckets.

Key goals:
- **Always-ready index**: Scheduled scans (the r2 demo's daily ingest) so the index is there when you need it
- **Fast indexing**: Shard a bucket's listing across workers (`bulk-list`), aggregate out of core (`import`)
- **Site**: Treemap visualizations, diffs and an age lens over each scan's path index

## Architecture

### Python Backend (`src/disk_tree/`)

**Listing** (`find/bulk*.py`): `bulk-list` shards a bucket's object listing (`gcs://`, `s3://`,
`r2://` via its S3 endpoint) across worker processes into layer-1 listing parquet shards
(`hf://<namespace>`: a Hugging Face org's buckets + repos over the Hub API, one rate-limited process). The
canonical columns include `bucket, name, size_bytes, created, storage_class_id, generation, version`. `generation` is the legacy nullable GCS integer; `version` is the shared nullable opaque string (exact decimal GCS generation or S3/R2 ETag). Listing adapters, every import engine and CW's index union preserve it on file rows. ETags are content validators rather than unique rewrite identities; deletion also checks reviewed size/mtime and sends the reviewed token as an atomic precondition. Historical missing tokens stay NULL.

**Aggregation** (`find/import_listing.py`, `find/index.py`, `find/aggregate_{duckdb,stream}.py`):
`import` aggregates a listing bottom-up into the canonical layer-2 frame — columns `path`, `size`,
`mtime`, `kind`, `parent`, `uri`, `n_desc`, `n_children`, `n_files`, `depth` (+ opt-in pivots,
`mtime_mean`, labels), sorted `(depth, path)` so parquet pushdown prunes by depth and path prefix.
Three engines, byte-identical output: `pandas` (in memory), `duckdb` (out of core), `stream`
(O(depth) over sorted listings).

**Output** (`storage/`, `sqla/model.py`): each imported bucket's layer-2 is adopted into the write
dir (`<DISK_TREE_ROOT>/scans/<uuid>.parquet`, or `--to <dir|url>`) and recorded as a `Scan` row
(`path`, `time`, `blob`, root `size`/`n_children`/`n_desc`/`mtime`) in `<DISK_TREE_ROOT>/disk-tree.db`.
cw-s3's jobs read that blob back (`ls $DISK_TREE_ROOT/scans/*.parquet`) as `dt-cloud index-write`'s input.

**CLI** (`cli/`):
```bash
disk-tree bulk-list URI    # Shard URI's object listing (gcs:// | s3:// | r2:// with `-E <endpoint>`) into
                          # layer-1 parquet shards at `-o` (`-a` adaptive range-splitting, `-P` procs;
                          # gcs:// `-c`: each object's CRC32C as a `crc32c` column, what `dt-cloud dups -l` proves copies by).
                          # `hf://<ns>[/<kind>[/<name>]]` (`find/bulk_hf.py`, `HF_TOKEN`) lists a Hugging Face
                          # namespace as bucket `<ns>`: `buckets/<b>/…`, `{models,datasets,spaces}/<repo>/…`
                          # (current tree; LFS deduped by oid) + `<repo>/@history/@unlisted` (what
                          # `usedStorage` bills beyond it), so non-`git` bytes sum to the billing page;
                          # `storage_class_id` public 1 / private 5 / unbilled git 6 (`import -s hf -p
                          # storage_class_id`). `-L` per-object history via `lfs-files` (write token), `-D`
                          # repo-level dates (50→1000 entries/page); `-w` = requests in flight (429s at 8)
disk-tree import -l GLOB…  # Aggregate listing(s) into a canonical layer-2 per bucket (`-b`; default every
                          # bucket in the listings): `-e pandas|duckdb|stream`, `-p` pivot sums, `-m`
                          # mtime_mean, `-i` also cut the path-store tiers, `-w/--to` write dir or URL

disk-tree recompress PATH…  # Rewrite v1 layer-2 listings as v2 in place, lossless (spec `listing-slim.md`
                          # phase 2): files, dirs (recursive `*.parquet`, sidecars skipped) or fsspec URLs
                          # (`gs://`, `r2://`, `s3://`). Streams by row group (never a whole file in memory):
                          # drops `uri` (scan root → metadata; refused unless `uri == <root>/<path>` on every
                          # row) and the `sum_*` pivots equal to `size`, re-encodes under
                          # `$DISK_TREE_PARQUET_CODEC` in ≤64K-row groups to a `.v2.tmp` sibling, verifies
                          # (row count + order-insensitive digest of `(path,size,mtime,kind)`), then swaps
                          # (atomic rename; copy + delete on a URL). v2 input is skipped; a failure leaves
                          # the original untouched, exit 1. `-n` dry-run, `-k` keeps `<stem>.v1.parquet`,
                          # `-j` JSON. Per-file old/new size + ratio, and totals
disk-tree listing-format PATH…  # The audit: each parquet's listing format (v1 | v2 | not-a-listing), codec,
                          # row groups, rows, size (+ root/implied for v2) from the footer only; `-j` JSON

disk-tree tiers L2        # Cut the path store's sorts (spec `path-store.md` §1.2/§4.1) from a layer-2 — a
                          # local path or an fsspec URL (copied once through `blobfs.open_read`): `path` =
                          # every row (objects + dirs) sorted `(depth, path, …labels)`; `bysize` = the same
                          # rows sorted `(⌊log2 size⌋ desc, path, …labels)`, size 0 last, the bucket computed
                          # in SQL (never stored); over label slices each row carries its path's total `tot` and
                          # the sort keys on `⌊log2 tot⌋` (spec `bysize-path-total.md`; the `-by-usr` copy stays
                          # per slice). 8K-row groups (`-r`), `tier`/`sort` (+ `bucket: log2`) in
                          # the parquet metadata, the source's listing format inherited. `-t path,bysize`
                          # (default both), `-s STEM` (may be a URL: cut locally, uploaded), `-g` writes the
                          # `.groups.json` footer sidecar beside each (`find/groups.py`; carries `b_min` now),
                          # `-v usr` extra sorted copies led by those columns, `-j` JSON; `-m` DuckDB memory limit (default 8GB — an
                          # external sort, spills to `-T`, default `.duckdb-tmp` beside the stem; unbounded it took
                          # 28 GB for 57M rows), `-p` threads. Prints rows, groups,
                          # bytes, KV per tier. `import -i` does the same at import time (bare `-i` = both;
                          # the `dirs`/`objects`/`coarse` tiers are retired). The cloud overlay's
                          # `dt-cloud index-write` (cw) and `dt-cloud path-index -P` (gcs, the r2 demo)
                          # cut the same two sorts from their bucket unions — objects as rows, L2 column
                          # names — under `path-index[-bysize][-by-user].parquet` (spec §4.2–4.4)
dt-cloud path-index -p BUCKET [-K LO:HI] -P DIR/path-index.parquet …  # One part of a sharded index
                          # (`index_parts.py`): one bucket's objects (`-K`: first key segment in [LO, HI)), its sorts
                          # (no sidecars) + `part.json` / `roots.parquet` / `age.parquet` / `pfx.parquet` beside -P
dt-cloud path-index-merge -o SNAP -P DIR/path-index.parquet [-S] PART_DIR…  # The parts k-way merged by each
                          # sort's key into exactly the one pass's bytes (same 8K groups, a split bucket's root rebuilt),
                          # + sidecars, `attr.tsv`, `age.json`/`meta.json`. Scan step: `options.index.parts`
                          # (`{bucket: [bounds]}`; `{}` = per bucket) → scan.nf's `index_part` fan-out, then `index` merges
dt-cloud index-recut SRC OUT  # Re-cut a published generation's `bysize` from its `path` sort into a NEW
                          # generation dir (never over one): `-c` checks root + depth-1 views against per-path
                          # sums over `path` (`bysize_check`); then `index-sync -v bysize -g <gen>` points at it
dt-cloud dups -o OUT INDEX  # A scan's duplicate-candidate groups (specs/dups.md): `tree` (same relative path, size,
                          # `n_files` in ≥2 buckets) and `moved` (same basename, size, `n_files`, other paths) over paths ≥
                          # `-f` (1 GiB; slices summed first, the `bysize` sort's `tot` prunes); maximal groups, per-copy
                          # `nested` / `suspect` / `keep`, `-l` the scan's `bulk-list -c` listing proves copies by every object's
                          # crc32c (`exact`; a differing `moved` copy dropped), `-g N` crc32c spot checks on GCS (groups `-l` left
                          # unsettled) → `dups.parquet` + `dups.json`
                          # (the `dups` scan step writes them beside the index; `/api/dups` + `/dups` read them, `DUPS=1`)
dt-cloud admins check -d DB -o ORG [-c CONFIG] [-j] [-x HANDLE]…  # A deployment's admin list (`admin_emails.github`: the handles recorded
                          # on `/admin`) against a GitHub org's owners (`gh api orgs/ORG/members?role=admin`; run as an org
                          # member): who is an owner but not an admin, and the reverse; rows without a handle are counted.
                          # Read-only (handles only, `wrangler d1 execute --remote`), never a sync; exit 1 on drift, 3 when the comparison
                          # isn't available (no `github` column or no table: the recipe unapplied). `-x`: a
                          # difference that is meant (an owner left off the list), reported on its own line, not drift
dt-cloud interval-store append [-c] [-n] SCAN_ID  # Append one scan to the interval store as a run beside its
                          # base generation (spec `interval-store.md` §2.7; profile `-P`/$INTERVAL_STORE_PROFILE):
                          # prepare → ranges (Batch) → publish (cut, binary-counter merges, manifest) → R2 → prune,
                          # strictly in scan-id order (exit 3: not published / not next; `-c` catches up)
dt-cloud static-names build -g GEN -L LAYOUT -s SCANS -o STORE -x off|MIN,TAIL  # The static name index's base generation
                          # on ONE machine (`static_local.py`): the staged (Batch) path's stages in-process — ranges,
                          # coalesced versions, suffix shards, catalog, drill (`build_drill`) + `roots-measure/`, and
                          # anchored search (`names/` + `anchors/`: `^q`, `^q$`, heavy `q$`, path terms; `-A` skips it) — over a
                          # work dir (`-w`), then copied to `<STORE>/static-names/GEN/` (a dir or `r2://`/`s3://` URL;
                          # never over a generation). SCANS + `-L` templates (`{id}`, `{gen}`) find the `path` sorts.
                          # The drill is chunked as the staged path (`-c` rows, default 4M: suffix rows per member-loop
                          # chunk, roots per short piece and per measured / rolled-up q range; roots spill as parquet),
                          # so its working set is `-c`-bound, not the corpus's; byte-identical at any `-c`. Logs a
                          # `build GEN: drill/<step>` line per sub-step (seconds, disk / RSS peaks, row counts)
dt-cloud static-names add -g GEN -L LAYOUT -s SCANS -o STORE  # Append every scan after GEN's newest as a run (append, shards,
                          # catalog delta, drill run, + `names/`/`anchors/` when GEN's base has them — an anchorless GEN
                          # gets them only by a rebuild into a new GEN — `manifests/<id>.json`), then the binary counter's carries; a URL
                          # store's generation is mirrored into `-w`, new files go up data → `meta.json`s → manifests.
                          # Small deployments' path (the r2 demo's daily ingest); gcs/cw run the stages on Batch
dt-cloud interval-store migrate-object-version -g OLD -O NEW -o STORE  # Adopt string object versions without raw listings:
                          # fold existing ranges/runs; native GCS generations become exact strings, unknowns stay NULL.
                          # `-R` names borrowed source ranges. Source and CURRENT stay unchanged pending verification/cutover.
                          # A first-observed future token opens its own interval; it never fills earlier unknown history.
dt-cloud interval-store build -g GEN -L LAYOUT -s SCANS -o STORE  # The interval store's base generation on ONE machine
                          # (`interval_local.py`, spec `interval-store.md` §2.8): ranges, path/read versions, fold,
                          # owner slices, every served sort — the staged stages in-process over `-w` — then copied to
                          # `<STORE>/interval-store/GEN/` (a dir or `gs://`/`r2://`/`s3://` URL; `scans.json` last;
                          # never over a generation). The staged per-range Batch stage is `interval-store build-ranges`
dt-cloud interval-store add [-g GEN] -L LAYOUT -s SCANS -o STORE  # Append every scan after GEN's newest as a run (per-range
                          # append over the open versions refolded from base + runs, run cut, `manifests/<id>.json`),
                          # then the due carries; a URL store mirrors into `-w`, uploads data → `meta.json`s → manifests.
                          # Reports peak work-disk/RSS per stage and scan. Every interval-store key goes through
                          # `blobstore.Store` (a profile's bare bucket name = `gs://`); `r2`/`r2-copy` are no-ops when the
                          # store is the serving R2 bucket. Every 2^L scans (`-C L`, default 5; `none`: never) it compacts
                          # base + runs into a new generation (`interval_compact`: a fresh build's bytes), then moves
                          # `interval-store/CURRENT.json` to it (the site follows it when `INTERVAL_STORE_GEN` is unset);
                          # `-g` defaults to that pointer. `interval-store current -o STORE [-s GEN]` prints / sets it
dt-cloud interval-store backfill-attribution -O NEW -o STORE [-s ROOT]  # The roster attribution table
                          # (specs/attr-interval-table.md): each scan's `attribution.parquet` (`path-index -a` writes it
                          # beside the sorts; key `(depth, path)`, change columns `user`, `source`, `evidence`) versioned
                          # in the store's ranges, runs (`append.attribution` / `build|add -A`; a scan without one is
                          # uncovered), carries and compaction; served sort `attribution` `(path, vf)` (`extras.ts`). This
                          # folds GEN into NEW with the table over every scan, checked against GEN's runs' table;
                          # `attribution-convert` first writes the parquet beside older scans' `attr.tsv`
dt-cloud interval-store backfill-churn -O NEW -o STORE [-g GEN]  # GEN (base + runs) folded into NEW with churn on the folded
                          # path versions (specs/diff-progressive.md §3): `churn_cum` (the path's cumulative churn at `vf`, each
                          # file's |Δsize| rolled up to its ancestors, counted after the last v1 → v2 scan) and `churn_end` (at
                          # `vt`, closed pieces), `"churn": true` in `scans.json`; a fresh `build`'s bytes. New builds (`churn`
                          # after `fold`), appends and compactions carry it; source and `CURRENT.json` untouched
dt-cloud interval-store backfill-level -g GEN -o STORE  # The level index (specs/diff-progressive.md §3b, what
                          # `/api/diff/level` reads): one row per child version transition keyed `(parent depth, parent
                          # path, ts, name)`, cut into GEN's `served/level.*` and each listed run's, nothing else written
                          # (not `scans.json`, not `CURRENT.json`); needs churn. Appends, carries and compactions then carry
                          # it. New builds opt in with `build -l` (`"level": true`); without either, no served byte changes
dt-cloud interval-store backfill-generation -O NEW -o STORE -s ROOT -l TEMPLATE  # A generation folded into NEW (a dir or
                          # URL store; never over one) whose object versions carry their GCS `generation`, read once from
                          # the raw listings (`{id}` templates under ROOT): each version the first listed within it, earlier
                          # ones NULL. Source and `CURRENT.json` untouched: verify, then `current -s NEW`. Opt-in per
                          # deployment (gcs: `path-index -G`); off, no file carries the column (specs/lean-scans.md §3.1).
                          # `-N` (a repair): GEN already carries it; only its NULL object versions are filled (with `-S`,
                          # from the scans whose listings have them all: gcs's depth-2/3 objects had none before 2026-10-22)
                          # `-j N` (also `add`'s compaction; `build`'s cut): N key ranges, then N served sorts, at a time on
                          # cursors of one DuckDB database (`-p`/`-M` shared; each ends in a serial pyarrow writer), same bytes
dt-cloud interval-store layout-sim GEN REQUESTS  # `/api/diff/level` reads of a generation's served sort (`-s`, default
                          # `path`; GEN a dir or `gs://`/`s3://`/`r2://` URL through `blobstore.Store`) simulated offline
                          # (specs/diff-progressive.md §3b): per `{path, from, to}` request, rows decoded and GET runs for
                          # today's kept groups, `-p` page sizes within them, `-b` band counts (`(band, depth, path, vf)`),
                          # and `-l` a dedicated level sort: one row per child version transition, keyed `(parent depth,
                          # parent path, b, child)`, one file per base / newest-manifest run (`-f N`: N even files), one
                          # run a file touched; `-C` its size over the whole generation (transitions, estimated bytes).
                          # Runs merge as the site's `planRuns` (`RUN_GAP`/`RUN_BYTES`/`RUN_READS`
                          # mirror `site/functions/_lib/index.ts`) at the sort's bytes/row; row groups cached under `-c`
                          # (`depth, path, vf, vt`), `-t` fetches over one footer, `-j` processes. JSONL out, p50/p90/p99/max
                          # summary (rows, runs, GET rounds at 2 and 3 waves) to stderr
dt-cloud interval-store level-sim GEN REQUESTS  # `readLevel`'s two waves priced over GEN's real `level` sort: per `{from, to, dirs}`
                          # read (one line per chain round) the groups, pages and page-index spans as the reader plans them, each wave's
                          # gets, rounds, bytes and predicted time (`-T` first byte, `-R` throughput, 6 slots) under `-g` gaps × `-C` caps,
                          # `-b` `fit:B` (closes gaps while a round is saved within B fetched bytes), and `whole` (no index wave). Fetches
                          # only sidecars and the kept groups' page indexes (cached under `-c`); gets and bytes match live `lv-*` spans exactly
                          # `-s 256,512,1024` (+ `-F` sidecar row-group sizes): a table per mode, today / held page indexes (cold,
                          # colo warm, warm) / the sort cut in small groups with no page index, from the real file's page indexes
dt-cloud image-sha [-e SHA [-a]] [-j]  # The running image's source revision (`image_sha.py`): `$DISKY_GIT_SHA` /
                          # `$DISKY_GIT_DIRTY`, which a deployment's image build stamps (gcs: `job/build.sh` → Dockerfile
                          # `ARG`/`ENV` + OCI labels `org.opencontainers.image.revision`, `disky.dirty`). Prints `<sha>`,
                          # `<sha> dirty` or `unstamped`; `-e` exits 1 unless the image is that sha (or a ≥7-char prefix),
                          # clean (`-a` accepts dirty) and stamped. Benchmark harnesses (`interval-store cut`, `layout-sim`)
                          # log the stamp at start and take `-E/--expect-sha` (+ `--allow-dirty`): refuse to start on any
                          # other image. Job entrypoints (`scan head`/`step`, the `interval-store` stage tasks) log it
dt-cloud batch submit -c COMPONENT [-L REGION] [-b BUCKET]… [-X] JOB_ID SPEC  # The shell's Batch submit, through `batch.submit_job` (the
                          # one place a job is created): labelled from `$DISKY_LABELS` + `-c` on the job and its allocation policy
                          # (`cost_labels.py`), in `-L`, else `$BATCH_REGION`. There is no default region (`deploy.batch_region`): neither
                          # set is an error, here and in `job status|logs|watch|metrics`, `batch-cancel` (`-r`), `sweep sync-progress`,
                          # `scan-run task`, and `job submit-listing` for a bucket `$LISTING_REGIONS` doesn't name. A job whose VMs
                          # (`allocationPolicy.location.allowedLocations`, else the region) run outside the location of a bucket it
                          # mounts (gcsfuse volumes) or names (`-b`) is refused (`batch.check_region`: one `buckets.get` per bucket
                          # per process; a multi-region bucket takes its continent's regions, a dual-region its two) unless
                          # `-X/--cross-region` (`job submit-listing -X` too; `$BATCH_CROSS_REGION=1` for the submitters with no flag:
                          # the static-names / interval-store runners, `scan-run task`). A bucket whose location can't be read is
                          # noted on stderr and not checked; a bucket reached only by URL and not named isn't seen
dt-cloud soft-delete -b BUCKET… [-E ENDPOINT] [-o ROOT…] [-j]  # Each bucket's soft-delete retention now (`soft_delete.py`): GCS's (`NAME`, read with
                          # `storage.buckets.get`, and GCS restores nothing from a bucket whose policy is off), or an S3-style store's (`s3://NAME`:
                          # versioning plus the narrowest noncurrent rule; null = kept until purged). What a sweep's undo has to work with. `-o`: the day's `<ROOT>/<date>.json`; the opt-in scan step `soft-delete` writes it under
                          # `snapshots/soft-delete/` for each `gs://` and `s3://` source, and the site (whose own account can't read bucket
                          # metadata) reads the newest: a recorded undo on a bucket it shows off is "undo unavailable" (its control
                          # disabled, the route refusing), one on a bucket it doesn't name is "unverified". `sweep undo` asks GCS itself
dt-cloud diff-bench -a BEFORE -b AFTER [-S STORE] [-v DATE:PATH]… CASES  # The table a store-layout change ships with (no CURRENT move
                          # without it): the browser's diff + map requests (diff-check's CASES, `-v` views) on two hosts, each
                          # cold (`-n` random w×h cache buckets no page asks for) then warm (`-s`: also each diff's `summary=1`, which no page asks); per request kind, median cold
                          # `Server-Timing` total, the paired Δ (B − A), B-slower count + sign-test p; answers must agree
                          # (totals, rows, nodes). Same reader flags on both (`DIFF_DESCENT` moves the tier). Exit 1 on an error
                          # or a disagreement, 2 if B is slower (p < `-p`); `-o` every sample as TSV
disk-tree tiers plan SIDECAR P THR  # The reader's span selection run offline over a tier's `.groups.json`
                          # (phase 0's instrument): for `path` it mirrors `readRects` exactly (depth rect
                          # `dP+1..`, path range `[P/, P0)`, `b_max ≥ thr·atten^(d−dP−1)` per group); for
                          # `bysize` it is `b_max ≥ thr_min ∧ p_max ≥ P/ ∧ p_min < P0`. Reports groups
                          # selected, rows they hold, bytes (compressed chunks), and — from the parquet —
                          # rows that actually pass, i.e. the decode waste. `P` = `.` for the root; `-a`
                          # attenuation, `-d` max depth, `-t` tier (default: from the name), `-C` skips the
                          # count, `-j` JSON. Measured on a 20K-child flat dir at 2048-row groups: `path`
                          # decodes 10 groups / 20,015 rows for 1,250 answers, `bysize` 1 group / 2,048
```

**What `batch.check_region` can't see:** it reads a spec's gcsfuse mounts and the buckets `-b`/`buckets=` name, at `batch.submit_job`, so a job's region is checked only that far. A bucket the job reaches only by URL (a `gs://` path in its script or args, not mounted or named) isn't seen; name it. Nor is any job that doesn't go through `submit_job`: Nextflow's tasks (its `google-batch` executor posts them), the site's REST submits (`_lib/sweepDispatch.ts`, `_lib/cwBatch.ts`), `BatchCron` bodies (Cloud Scheduler posts them; `BatchCron` checks only their labels), and raw `gcloud batch jobs submit`. Those keep a job beside its buckets by configuration alone.

### Site (`site/`) and widget packages

`site/` is the hosted/serverless app: Vite + React + TypeScript, with Pages Functions over R2 / GCS +
D1 (r2.rbw.sh, the gcs/cw deployments). Its chart-lib-free DIY-SVG/canvas widgets come
from two workspace packages:
- **`@open-athena/treemap`** (`packages/treemap/`) — the SOTA treemap core + layout/color primitives:
  `<Treemap>` (SVG + canvas renderers, shared-edge tiling, dust texturing, per-cell `ring`
  emphasis, hover/pin events), `<VoronoiTreemap>` (`/voronoi` subpath), `useHoverPin`, `squarify`,
  `DEFAULT_PALETTE`/`ageFade`/`parseQuery`, `styles.css`. Disk-agnostic; the intended external
  consumer (e.g. file-tree) pins this.
- **`@disk-tree/react`** (`packages/react/`) — `<TimeSeries>`/`<BytesOverTime>` built on the core
  (which it re-exports for back-compat).

Digest and publish knobs: `dt-cloud digest -W KEY` writes only a month's preview bundle (`/admin/slack`), posting nothing. `/api/digest-reapply` re-applies the digest to chosen messages from `/admin/slack`, as a Batch job named by the `DIGEST_REAPPLY` var (unset: 503). `options.interval-store.publish_on_failure`: a failed append copies the scan's whole served set to R2, for a site that reads only R2 under `publish-r2 -S`.

### D1 schema (`site/schema.sql`)

One current-state file per branch (spec `done/d1-migrations.md`): new databases are created from it
(`wrangler d1 execute <db> --remote --file site/schema.sql`) and the tests build from it (`sqliteD1()`,
`conftest.d1_db`). A change is a recipe `site/migrations/<slug>.sql` plus `schema.sql` regenerated in the
same commit (`dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql -o site/schema.sql`;
`dt-cloud d1-schema check -p` verifies, and CI runs it); the recipe is applied to each of the branch's
databases (`--file site/migrations/<slug>.sql`; `cloud`'s only one is the r2 demo's `disk-tree-demo-db`),
then `git rm`'d. `dt-cloud d1-schema check -d <db> [-c <wrangler config>]` compares a live D1, read-only.

**Deleting D1 rows:** anything that can match more than a few thousand rows goes through `index_footer.d1_delete(table, where, …)` (≤ `D1_DELETE_ROWS` = 5,000 a statement, until one deletes none), never one `DELETE … WHERE`: D1 fails a statement deleting millions of rows (`7500 internal error`; gcs's `scan retire`, 2026-10-18).

### Cloud site auth routes (`site/functions/`)

One gate (`_lib/auth.ts`, `@open-athena/auth` over D1): `identify` → `Identity` (`via: session | grant | public`), `requireViewer` / `requireStager` / `requireAdmin`.
- `/auth/google` (+ `/callback`, `/onetap`) — Google OIDC → email session; `/auth/email/*` — emailed code / magic link → email session
- `/api/auth/*` — the package's routes: `whoami`, `exchange` (`?key=` share link → grant session), `logout`, request-access, admin grant/request/log console
- `/api/token` — a session's personal agent token (a non-expiring Bearer grant: the base scope + assign, never admin; grants can't mint one)
- `/api/me` — the caller's canonical owner id (`_lib/me.ts`): what `user=me` / `lens=user:me` / `owner: '@me'` resolve to
- `/api/admins` — the admins (`/admin`'s "Admins" section): every path to the `admin` scope (the list's rows with who added whom and when, the break-glass addresses, live admin-scope links, the staff domain where it is one), add / remove on the list, each logged to `admin_edits`; the last listed admin can't be removed
- `/a/<sha256>.<ext>` — **public, no gate**: an activity post's sender face (`_lib/publicAvatar.ts`), only bytes a Slack / Discord post stored in `public_avatars`, named by their own SHA-256 (a profile's or grant's face, else an identicon seeded by an HMAC of the actor under `SESSION_SECRET`); anything else 404s

**A path-restricted link** (a grant carrying `<tag>:hide:<prefix>`, `_lib/restrict.ts`): every route it can reach says what it gets in `functions/gates.test.ts` `RESTRICTED`. Blocked (`RESTRICTED_BLOCKED`, or by the route): whatever can't be filtered, including a name filter (`q=` on `/api/subtree`, `/api/diff`, `/api/series`: its sums count hidden hits) and an owner, class or lens scope at or above a hidden dir (`o=`, `cl=`, `lens=` on the same three: its sums break down what's hidden; under one is a 404, as unscoped). Filtered: the plain views, `/api/annotations`, `/data/rules.json`.

**Who is an admin:** one function, `isAdmin(env, email)` (`_lib/auth.ts`); `scopesFor` puts the `admin` scope on a session by it, so `requireAdmin`, `Identity.admin`, whoami and the client's admin UI follow, and a Slack Dry-run or Reject click (`slack/actions.ts`) asks it of the clicker's Slack profile email. A real delete never starts from Slack: the card's "Delete for real…" is a link to `/staged`, on every deployment, and a click on the old dispatching button gets an ephemeral link there. With `ADMIN_LIST = "1"` an admin is an `admin_emails` row or a break-glass address (the secret `ADMIN_BOOTSTRAP`) and nothing else: staff keep every other scope, an empty or missing list means no admins (never a fallback to the domain; `/admin` says so), and a listed address is admitted by the list. A row may carry the person's GitHub handle (`github`), so the list can be checked against a GitHub org's owners (`dt-cloud admins check`). Unset, the staff domain is admin (plus `admin_emails` rows where `ADMIN_EMAILS`). The only other admin is a grant holding the scope (`admin` or `*`), which only an admin could mint, and which with `ADMIN_LIST` nobody can: the gate's store (`grantStore`) refuses to insert one, whichever path asks (`POST /api/auth/grants`, a request's approval: 403), or to re-key one (`/rotate`), or to give one back access (a later or cleared expiry, a higher cap, sessions that outlive the expiry, re-enabling: 403; shortening and disabling pass); the grant the package mints by itself for an address the policy admits (`POST /api/auth/request`) goes in without the scope. A live admin grant keeps working under the list, stays on `/admin`, and can be revoked. Agent tokens never carry it. `functions/gates.test.ts` enumerates every Functions route and asserts its gate per identity: a new route needs a row there.

**Rolling out `ADMIN_LIST`:** merging changes nothing while it's unset, and `/api/admins` edits the list from the merge on, recipe applied or not (without `site/migrations/admin-list.sql` it stores no GitHub handles). To turn it on:
1. Apply the recipe, seed the list on `/admin`, and check it (`dt-cloud admins check`).
2. List the live admin grants (`/admin`'s Admins section, "link" rows; `created_by = 'policy'` rows are ones the package minted for a request, whose token nobody was given) and have them revoked. Each is an admin whatever the list says.
3. Set `ADMIN_LIST` and `ADMIN_BOOTSTRAP` on **every environment sharing that D1, at the same time**, prod and dev stack alike: a dev stack on prod's D1 with the list off lets every staff sign-in edit the list from dev.

## Development

During ongoing implementation, treat a concrete bug report about the code we own as an implied request to fix it: reproduce, implement, verify, and coordinate the deployment handoff without requiring a second prompt. Honor explicitly read-only requests. Code-fix authority does not imply unrelated infrastructure applies, permission changes, or another session's live cutover. Report separately what is fixed locally and what is actually deployed.

When another agent sends a useful, in-scope handoff, proactively review, implement or adopt, test, and coordinate it. A review queue tracks work; adding an item is not completion. Prefer coherent shared-code picks over merging deployment branches, preserve unrelated changes, and leave deployment-specific applies and live cutovers with their owners.

```bash
# Python setup — one uv workspace: the engine (root) + the cloud overlay
# (`cloud/`, package `dt-cloud`) share ONE `uv.lock` and one `.venv`.
uv sync                                                 # engine only
uv sync --all-packages --all-extras --all-groups        # engine + dt-cloud, every extra, test groups
disk-tree bulk-list -a s3://<bucket> -o work/listing/<bucket>
disk-tree import -e stream -s s3 -b <bucket> -l 'work/listing/<bucket>/shard-*.parquet'

# Site
pnpm install
cd site && pnpm dev
```

## Packaging / Distribution

`uv build` builds the engine's wheel (`src/disk_tree`; CLI only); there is no release workflow.
`dt-cloud` (`cloud/`) is a workspace member installed from the lock, not published.

## Data Flow

1. `disk-tree bulk-list` writes a bucket's sharded object listing
2. `disk-tree import` aggregates it by directory into a layer-2 parquet (+ a `Scan` row), or
   `dt-cloud path-index` aggregates the listings directly (gcs, the r2 demo's daily ingest)
3. `dt-cloud path-index` / `index-write` / `disk-tree tiers` cut the path store; `dt-cloud index-sync`
   publishes its footers to D1
4. `site/`'s Pages Functions read the path store (row-group range reads) and render treemaps / diffs

## Config

Default paths (override with `DISK_TREE_ROOT`):
- `~/.config/disk-tree/disk-tree.db` — SQLite `Scan` rows (`import`)
- `~/.config/disk-tree/scans/` — `import`'s layer-2 blobs
- `~/.config/disk-tree/buckets.yml` — per-bucket `endpoint_url` / `profile`

`import --to <dir|url>` (`config.set_write_target`) writes the blobs elsewhere — a local dir or an **fsspec URL** (`r2://bucket/prefix`, `s3://…`, `gs://…`); `DISK_TREE_SCAN_DIRS` (colon-separated, the first is the write target) sets it from the env. `r2://` rides s3fs with the bucket's endpoint from `DISK_TREE_R2_ENDPOINT_URL` or its `buckets.yml` entry. Every parquet read/write goes through `blobfs.py` (the local-vs-URL seam), and blobs are written in 64K-row groups so a `depth`/`path` pushdown over R2 fetches kilobytes.

**Cross-account credentials** — a `buckets.yml` entry (or `defaults`) may carry a `profile:` naming an AWS credential profile (`blobfs.bucket_profile`), so a source and a target in *different* accounts each authenticate with their own key. It threads to every S3/R2 seam: the `s3fs` blob IO (`_s3fs(endpoint, profile)`) and the `boto3` bulk lister (`S3BulkLister(profile=…)`, `bulk-list -f`). No profile → ambient credentials (env / default profile), the single-account default. Cross-account needs per-bucket endpoints too, so leave `DISK_TREE_R2_ENDPOINT_URL` unset (it globally overrides all per-bucket endpoints).

Stream-engine tuning knobs (env, all with measured defaults — see the constants block in `find/aggregate_stream.py`):
- `DISK_TREE_FLUSH_ROWS` — output row-group size (read-side: smaller = less fetched per directory browse, bigger footer)
- `DISK_TREE_PARALLEL_FINALIZE_MIN_ROWS` — below this the finalize stays serial even at `-j N`
- `DISK_TREE_SCAN_BATCH_ROWS` — pass-1 read-batch size (partition balance / seek granularity)

## Tests

```bash
pytest tests/                    # engine
cd cloud && pytest               # dt-cloud (same venv; sync with --all-packages first)
```

Tests build their listing fixtures inline (`tmp_path` parquets). CI and the job images install `--frozen` from the workspace lock (`deploy/sheet-mirror/Dockerfile` is the reference recipe: `uv sync --frozen --no-dev --no-editable --package dt-cloud --extra …` into `UV_PROJECT_ENVIRONMENT=/usr/local`); a plain `pip install .` resolves fresh and ships pins the tests never ran.

## Performance

- Depth column enables parquet predicate pushdown (only load needed rows)
- The site reads the path store by row-group range reads, planned from footers in D1 (`index-sync`)

## Deployments: parity by default

Every deployment (gcs, cw-s3, r2, hf, the r2 demo, …) ships the current full feature set. A new deployment, or a feature landing on `cloud`, reaches each of them without being asked; only a store that can't support a feature skips it, and its deployment spec says which and why. The baseline today:
- **Hosts:** prod and a dev stack: `dev.<host>` (a `branchAliases` CNAME onto the Pages branch `dev`), wrangler's `[env.preview]` with prod's bindings, preview-env secrets, every origin + callback (prod, dev, the `pages.dev` alias, localhost) on the Google OAuth client.
- **Auth:** app sign-in (Google OIDC + emailed codes), staff domain, admin console, personal agent tokens.
- **Serving:** the interval store as the Map's path store (`PATH_STORE = "intervals"`, following `interval-store/CURRENT.json`, once `interval-store verify` / a `ps=iv` parity check passes on the first scans); search from the prebuilt static name index only (`FILTER_STATIC`, `NAME_SUMMARY_STATIC`, `FILTER_STATIC_HEAVY`, `FILTER_INDEXED_ONLY = "1"`), no per-scan search sidecars. Per-scan `path`/`bysize` sorts are on their way out (`specs/lean-scans.md`).
- **Job:** `scan-run supervise` (phases, alerts, `/scans` records, cost labels), `DT_TRIGGER` (`schedule` in the cron body, `manual` from hand submits; with migration `scan-run-trigger`): the run's and the scan's trigger (`/scans`, the digest's reply scan), `index-gc`, cache warm-up (`warm-cache`) and the serving probe (`probe -c`) after each scan, the Slack digest thread, and on a staging deployment the `staged-card` step (`dt-cloud staged-card`; the site's prod env names the job's grant in `STAGED_CARD_GRANT`, its dev stack does not).
- **Meta:** the "data behind this site" store (`STORES_JSON` `meta`) with its own static index.

A deployment's spec lists the baseline items it doesn't have yet as gaps, and those gaps are open work, not follow-ups to propose.

### ClickHouse development

Start with `specs/architecture/README.md` for the production serverless design, ClickHouse experiment/read-contract boundaries and RAM/disk alternatives, with Mermaid diagrams. Keep these summaries synchronized when deployment status or accepted read contracts change; the detailed measurement journal remains `specs/ch-store.md`.

The `ch-store` branch explores an append-only historical query service (`cloud/src/dt_cloud/chstore/`; design and measurements in `specs/ch-store.md`). Its local worktree is `wt/ch-store` under the main clone; root-level `git status` does not show that worktree's changes. Run large ingests and benchmarks on the dedicated GCS `ch-store` VM using `job/ch-store.sh`; keep the laptop for source edits, small fixtures, and browser checks. `push` syncs source and node scripts; `push-src` syncs Python packages without re-uploading unchanged scripts. Both replace source on the VM and must wait for active computation to finish. `serve` restarts its query service. The preview at dev.gcs.oa.dev uses this service; production cutover is a separate step.

Run `cloud/tests/test_chstore.py` and `cloud/tests/test_chserve.py` sequentially against a remote ClickHouse server (`CLICKHOUSE_URL`, optionally via an SSH tunnel). Ingestion uploads to one shared server staging file, so separate pytest processes can overwrite each other's fixtures even though their databases are isolated. Preserve the VM's data disk when resizing; record benchmark machine size, service thread count, and whether caches were cold.

Prefer `job/ch-store.sh test cloud/tests/test_chnarrow.py cloud/tests/test_chserve.py -q` for fixture suites: it stages the small tests/fixtures and runs them beside ClickHouse in an ephemeral container, avoiding a WAN round trip per SQL statement. Do not overlap independent test invocations or benchmarks with ingestion/build work. Do not edit `job/ch-store.sh` while an invocation is running; a shell can resume reading a modified script at its old file offset.

For long remote CLI work, use `job/ch-store.sh py-bg TAG ARGS…`; a named detached container survives laptop sleep/SSH disconnection and retains its logs and exit state. Inspect with `py-status TAG` (JSON Docker state) and `py-logs TAG` (save to local `tmp/`). Existing tags are refused, not overwritten. Check both `Running: false` and `ExitCode: 0` before launching dependent stages; source syncs, fixtures and benchmarks must still wait for the active computation to finish. Detachment does not keep local agent orchestration running while the laptop sleeps.

The numeric historical experiment is bounded, not an incremental fleet-wide index: `ch-narrow-build`, `ch-narrow-history`, `ch-narrow-bench -H`, and `ch-narrow-response-bench` keep discovery and complete-body measurements distinct. `serve-query -N TARGET` (or `NARROW_TARGET=TARGET job/ch-store.sh serve`) opts into its exact prefix/selected dates and falls back to canonical serving elsewhere. Keep frozen hierarchy and historical blob coverage limitations explicit; full-body capped-list parity does not establish omitted root identities.

The dev-only `/coarse` viewer and `/api/coarse` are a separate byte/count contract: ordinary leaf modes serve literal exact/substring/suffix matches over leaf basenames, one level per drill, with exact folded remainders and common two-date partitions. Matching directory names are refused in these modes, not silently treated as leaves. Summaries and packed vocabulary have a four-entry process LRU; pattern tables are rebound per request and cleaned up. `ch-narrow-coarse-bench` validates these partitions with an independent complete immediate-child oracle; its subsecond timings are not rich canonical-body or all-history acceptance. Keep `QUERY_BOX_COARSE=1` preview-only. OA Pages deployments need the OA credentials loaded by the `gcs` worktree's direnv, not the root clone's ambient personal-account token.

`levels=2..4` opts into bounded batched refinement at one global byte threshold, with common nested diff partitions (single-date ≤K heavy children per level; diff≤2K). One level remains the default. Preserve exact zero-byte counts and validate every expanded partition. The prepared vocabulary `Set` benchmark is rejected for poor real-data pruning; do not enable it in serving or rerun a broad suite for it.

`ch-coverage-bench` explores positive single-literal full-path substring matches that include directories: deduplicate laminar subtree intervals, sum outer-root weights, and use ordinary rollups inside covered ancestors. Its contract is exact bytes/object counts only, not matching-path counts; an outer-root count is not a matching-path count. The dev viewer's explicit `mode=coverage` uses packed root positions and cumulative byte/object arrays (`-r/--resident-roots` in the benchmark), so requests do not depend on the closed build session's temporary tables. It shares the four-entry LRU, refuses more than 2M outer roots (about 64 MiB of packed locator payload), and serves one level per drill with aligned two-date partitions. Do not mislabel its object totals as matching paths or enable it in production. Neither this prototype nor frozen DFS range summaries solve stable-ID allocation or arbitrary Boolean/owner/age queries.

`ch-coverage-bench -g ancestry` tests outer-root discovery via opaque parent IDs and selected-parent ancestor membership, using the existing numeric-parent/hierarchy indexes and temporary candidate sets. It does not copy a billion-row ancestry table. Snapshot presence must be applied first; missing candidate parents/hierarchy are errors. Subsequent range lookup still uses frozen DFS positions, so this flag is not an incremental serving implementation. The live coverage plan remains interval deduplication unless separately validated and selected.

`chstore/stable_ids.py` and `ch-stable-id-bench` are bounded session-only identity staging, not a durable allocator. Reservations/input must remain immutable across retry; the caller must supply all relevant known path/parent bindings and hold the future single-writer/publication protocol. Do not pass the entire billion-row dictionary into its bounded validation or treat staging success as a committed generation. Original path spelling is identity, independently of lowercase search names. No fleet identities are allocated or changed by the synthetic benchmark.

`chstore/reservations.py` is a single-node persistent reservation journal, not the ClickHouse publication authority. Hold its local process writer lock through external writes, resume a pending batch with identical checkpoint metadata, and pass its exact `reserved_count` to staging. `observe_commit` requires a separately verified committed manifest and binding fingerprint; hashes supplied to that method do not perform verification. Readers must not use journal state as their visibility barrier. No fleet journal/bootstrap is deployed; disk loss and multi-machine coordination remain separate work.

`stable_ids.checkpoint_fingerprint`/`stage_reserved` verify complete bounded identity inputs and root scope, not scalar/rich scan contents. Known bindings must come from the immutable predecessor, not whichever dictionary is latest after publication. `identity_publish.IdentityPublisher` is an isolated identity-batch publication prototype: immutable attempt rows stay hidden until verified markers, generation-pinned readers select one equivalent complete attempt, and partial/conflicting attempts must never be mixed. Its tables use explicit insert/part-directory fsync; no cross-table transaction is assumed. `ch-identity-publish-bench` creates/drops only a fresh owned synthetic database and refuses an existing artifact directory. Do not bootstrap the fleet, enable the daily hook, garbage-collect old data or call this complete scan publication based on these fixtures.

The identity publisher can read a previously audited immutable `(id,depth,path)` baseline plus its committed overlay without copying the baseline. The journal's initial counter must exceed every baseline ID, and commit manifests pin the baseline declaration. That declaration is not an audit or content checksum: the caller must ensure immutability/uniqueness and retain the existing baseline parent/lineage reader. `source()` returns overlay bindings only; `known_source()` includes baseline lookup rows.

`ch-scoped-pattern-bench` explores subtree-first leaf basename substring queries without enumerating global name IDs. It refuses union intervals over 1M nodes and matching nonleaf nodes, validates complete immediate-child byte/count partitions, and never samples accepted contributions. Its temporary posting index is session-owned and must not enter the process LRU. This is not a live fallback or global broad-query acceptance.

`ch-short-query-census` is a sizing instrument, never sampled search acceptance: exact global vocabulary character/window counts, hash-sampled distinct substring support at lengths 1..7, <=100K sampled names, <=2048 characters/name, 1 GiB/30-second query caps. Sample distinct support is a lower bound, not a linearly extrapolated global count. See `specs/architecture/short-query-index.md` for measured envelopes and proposed full-path first-activation/FM alternatives.

`chstore/order_keys.py` explores stable subtree intervals from root-to-node opaque ID sequences: `[key, key[:-1] + [last_id + 1])`. Alphabetical sibling order is not required for range sums/byte-rank discovery. Appending descendants leaves existing keys/bounds unchanged; do not call this an implemented incremental reader. `ch-order-key-bench` is synthetic single-name geometry, with an initial dense-range scalar oracle and an appended-descendant check. `ch-order-key-sample-bench` is an explicitly capped real-data geometry sample, never complete-query/FTS acceptance or a fleet storage estimate. Physical table comparisons must remove the unused lineage column from the scalar baseline. Logical UInt64 lineage values are depth× wider than one UInt64 rank; compression, real name-ordered pruning and historical summary maintenance remain acceptance gates.

`ch-order-key-history-bench` checks signed additive own-leaf history under unchanged bounds against complete synthetic snapshot sums, including deletion/resurrection. It does not sum recursive directory rollups or preserve non-additive maxima, and it does not implement durable publication or maintained query summaries. Keep these scalar screens distinct from complete TM/dTM latency.

`ch-order-key-posting-history-bench` validates complete exact-basename leaf scalar history inside one prefix on two frozen dates; over 300K union leaves is refused, not sampled. `key_history.build_deltas` bounds the state grid before expensive uniqueness checks and rejects duplicate/invalid lineage/state inputs. This is not a TM or a persistent incremental pipeline. Leaf-pattern/coverage vocabulary discovery retains at most budget+1 names and refuses over-budget patterns before posting scans; accepted queries must remain complete.

For fleet snapshot construction, `ch-narrow-build -b N` restricts both snapshot rows and closure membership to sampled disjoint `(depth, path)` ranges. `-R snapshots-partial` may reuse a completed legacy snapshot only when its exact successful creation/root queries and written-row counts match the requested source/prefix/dates; it refuses unverified partial tables. It is not per-range resume. `ch-narrow-hierarchy-stream TARGET` separately tests O(depth) directory ancestor construction over completed parent paths; `ch-narrow-history -a` opts into that construction instead of repeated string ancestor joins. Keep correctness fixtures separate from real-data speedup claims.

---

## Marin GCS usage — API for agents

The user-facing guide (token, HTTP API, recipes for finding, staging and assigning prefixes) lives in [`API.md`](API.md), which the gcs build also serves unauthenticated at <https://gcs.oa.dev/llms.txt> (the `llms-txt` plugin in `site/vite.config.ts`). Document API changes there, not here.

---

### Dev context — gcs deployment

`API.md` is the **user context**; the shared dev guide at the top
covers the engine, site, packages and Python setup. This section is what's
specific to the gcs deployment. The repo is **public**: no emails or other PII
in tracked files or commit messages (name + GitHub handle max); sizes and $
stay behind the site's auth.

#### Branch

`gcs` is a deployment branch (`specs/branch-layout.md`): shared code lands on
`cloud` and merges in (never rebase); this branch carries only gcs's delta —
`job/`, `cf/` (Pulumi stack), `site/wrangler.toml`, gcs's D1 schema
(`site/schema.sql`), branding, and this section.

#### Job

`job/` — daily snapshot pipeline on **GCP Batch** (`run.sh` entrypoint,
`batch-submit.sh`, `build.sh` → Cloud Build image). Cloud Scheduler cron
`gcs-usage-snapshot-daily` 07:00 UTC runs the `:latest` image built from this
branch (live in GCP, body edited in place, never regenerated). A rebuild is how
`job/` or pipeline changes reach prod.

`build.sh` images the checkout it lives in, stamped with its `git rev-parse HEAD`
and dirty state (`dt-cloud image-sha` reads it back). From a worktree, run that
worktree's own `job/build.sh` (another checkout's is refused); a dirty tree is
refused unless `ALLOW_DIRTY=1`. A benchmark on a throwaway tag:
`IMAGE=…:bench-<sha7> job/build.sh`, then the job runs
`dt-cloud image-sha -e <sha>` (or the harness's `-E <sha>`) before measuring.

#### Dev workflow

- `site/dev` — Vite on **:3253** + `wrangler pages dev` on **:3254** (a stale
  `oa_auth` cookie on localhost disables the DEV_IDENTITY stub → sign-in wall).
- `site/deploy` — **manual** deploy to gcs.oa.dev (pushing git does NOT deploy);
  moves the local `prod` branch pointer to the shipped SHA and pushes the branch
  + `prod` to GitHub (`o`) by default (`-P` to skip). `site/deploy --status`
  compares deployed vs HEAD; `site/deploy --dev` ships to dev.gcs.oa.dev.
- D1 schema: `site/schema.sql` is gcs's own (`specs/done/d1-migrations.md`). A
  change is a recipe `site/migrations/<slug>.sql` plus `dt-cloud d1-schema dump
  site/schema.sql site/migrations/<slug>.sql -o site/schema.sql`; apply with
  `wrangler d1 execute oa-gcs-usage-auth --remote --file migrations/<slug>.sql`
  (separate from deploy; needs `CLOUDFLARE_ACCOUNT_ID` inline), then `git rm` it.
  `dt-cloud d1-schema check -d oa-gcs-usage-auth` diffs the live schema. A `cloud`
  merge touching `schema.sql` conflicts by design: keep gcs's file, dump cloud's
  recipe into it.

#### Data flow

Daily Batch job: per-bucket DIY listings → `gs://oa-gcs-usage-dvx/listing/<date>/`
(+ `dir-cache/`) → `dt-cloud path-index` cuts the path store under
`index/<gen>/` (one generation per run, never overwritten) and `index-sync`
publishes its footers to D1. D1 keeps the latest 30 scans' footers
(`INDEX_RETAIN`); older scans are read via each tier's `<tier>.groups.parquet`
cold footer file. D1's `index_schema` row is the pointer. `webdata` writes
`snapshots/<date>/{tree,age,meta}.json` (+ `series.json`, `rules.json`), which
the site reads straight from the bucket (`functions/data/[[path]].ts`; no site
rebuild on new data). Owner assignments live in D1 (actions ledger) and apply on
top of the latest scan. Access logs ingest on the same job for the read-recency
lens.
