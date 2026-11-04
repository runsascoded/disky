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
cw's jobs read that blob back (`ls $DISK_TREE_ROOT/scans/*.parquet`) as `dt-cloud index-write`'s input.

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
                          # clean (`-a` accepts dirty) and stamped. Job entrypoints (`scan head`/`step`, the
                          # `interval-store` stage tasks) log it
dt-cloud diff-bench -a BEFORE -b AFTER [-S STORE] [-v DATE:PATH]… CASES  # The table a store-layout change ships with (no CURRENT move
                          # without it): the browser's diff + map requests (diff-check's CASES, `-v` views) on two hosts, each
                          # cold (`-n` random w×h cache buckets no page asks for) then warm; per request kind, median cold
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
- `/api/token` — a session's personal agent token (a non-expiring Bearer grant, the base scope only; grants can't mint one)
- `/a/<sha256>.<ext>` — **public, no gate**: an activity post's sender face (`_lib/publicAvatar.ts`), only bytes a Slack / Discord post stored in `public_avatars`, named by their own SHA-256 (a profile's or grant's face, else an identicon seeded by an HMAC of the actor under `SESSION_SECRET`); anything else 404s

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

Every deployment (gcs, cw, r2, hf, the r2 demo, …) ships the current full feature set. A new deployment, or a feature landing on `cloud`, reaches each of them without being asked; only a store that can't support a feature skips it, and its deployment spec says which and why. The baseline today:
- **Hosts:** prod and a dev stack: `dev.<host>` (a `branchAliases` CNAME onto the Pages branch `dev`), wrangler's `[env.preview]` with prod's bindings, preview-env secrets, every origin + callback (prod, dev, the `pages.dev` alias, localhost) on the Google OAuth client.
- **Auth:** app sign-in (Google OIDC + emailed codes), staff domain, admin console, personal agent tokens.
- **Serving:** the interval store as the Map's path store (`PATH_STORE = "intervals"`, following `interval-store/CURRENT.json`, once `interval-store verify` / a `ps=iv` parity check passes on the first scans); search from the prebuilt static name index only (`FILTER_STATIC`, `NAME_SUMMARY_STATIC`, `FILTER_STATIC_HEAVY`, `FILTER_INDEXED_ONLY = "1"`), no per-scan search sidecars. Per-scan `path`/`bysize` sorts are on their way out (`specs/lean-scans.md`).
- **Job:** `scan-run supervise` (phases, alerts, `/scans` records, cost labels), `DT_TRIGGER` (`schedule` in the cron body, `manual` from hand submits; with migration `scan-run-trigger`): the run's and the scan's trigger (`/scans`, the digest's reply scan), `index-gc`, cache warm-up (`warm-cache`) and the serving probe (`probe -c`) after each scan, the Slack digest thread.
- **Meta:** the "data behind this site" store (`STORES_JSON` `meta`) with its own static index.

A deployment's spec lists the baseline items it doesn't have yet as gaps, and those gaps are open work, not follow-ups to propose.

## cw

This branch is the cw.oa.dev deployment (renamed from cw-s3 on 2026-10-17, specs/done/rename-cw.md: cw-s3.oa.dev redirects; cloud resource ids keep `cw-s3`): the shared `cloud` base (everything above) plus the CoreWeave store. This section covers what is cw's own.

The repo is **public**: no emails or other PII in tracked files or commit messages (name + GitHub handle at most). Sizes stay behind the site's auth.

### Branches

- `cw` merges the local `cloud` branch (`git merge refs/heads/cloud`; bare `cloud` is ambiguous with the `cloud/` dir). Never rebase onto it.
- Changes that belong in the shared base are tagged `[cloud]` (or `[local]`, for the `local` branch the m3/app deployments merge) and cherry-picked up by the root session; this branch keeps only what the cw deployment runs.
- D1 schema: `site/schema.sql`, one current-state file (specs/done/d1-migrations.md). A change is a pending recipe `site/migrations/<slug>.sql`, folded in with `dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql -o site/schema.sql`, applied to cw's D1, then `git rm`'d; a recipe `cloud` deletes that cw's D1 hasn't applied is restored in the merge. `dt-cloud d1-schema check -d oa-cw-s3-usage-db` compares (read-only).

### Layout (cw's own)

- `job/profiles/`: the scans as Nextflow profiles (`dt-cloud scan head <profile>`): `cw.yml` (the CoreWeave buckets, submitted by `cw-batch-submit.sh`; `PIN=1 DRY=1` prints the cron body) and `cw-meta.yml` (the `meta` store: the data + R2 serving buckets, `cw-meta-submit.sh`, the `cw-meta-snapshot` cron).
- `job/cw-*`: the one-VM scan driver (`cw-run.sh`, `NF=0`) and the one-shots (`cw-reindex*`, `cw-overtime*`, `cw-publish-submit.sh`, `cw-recompress-submit.sh`, `cw-interval-submit.sh`: the interval store's base `build`, catch-up `add` and parity `verify` on Batch). `job/build.sh` builds the `:latest` image (`JOB=cw-run.sh`). `job/batch-submit.sh` is gcs's submitter, kept until the ops scheduler program takes a checkout per cron.
- `job/icons/arrows/` + `gen-{arrow-avatars,delta-arrows}.py`: the digest's trend-arrow avatars (hosted on `oa-cw-s3-usage-icons.pages.dev`); `job/icons-cw/` is where `cw-digest` renders OP plots before deploying them to that project's `cw` branch. `job/slack/`: the CoreWeave Usage Bot manifest.
- `job/digest.yml`: the monthly Slack thread's config (`dt-cloud digest -T cw -C job/digest.yml -m YYYY-MM -c <channel>`, `-n` dry run; `cw-digest` = `digest -T cw`); the engine + `cw` template are `cloud`'s `digest.py` / `digest_cw.py`. `cloud/tests/test_digest_cw_config.py` pins the file to the `cw` preset.
- `site/wrangler.toml`: the cw store's config (`STORE = "cw"`, `AUTH_MODE = "app"`, `STORE_BUCKETS`, `SNAPSHOTS_SUBDIR = "cw"`, the meta store, the plan-sweep executor). `[env.preview]` is the dev stack.
- `cf/`: Pulumi for the `oa-cw-s3-usage` Pages project, domain, D1, KV and R2 (stack `cw`; its component keeps the name `cw-s3`). `infra/gcp/`: the Batch side (stack `cw`).

### Dev workflow

- `site/dev`: Vite on **:3263** + `wrangler pages dev` on **:3264** (`devPort` in `site/package.json`; `./dev --refresh` seeds a local D1 from a prod export).
- `site/deploy` (run from `site/`): manual deploy to cw.oa.dev, then pushes the branch + `cw-prod` to `o` (`-P` skips the push). `--dev` deploys the preview branch (dev.cw.oa.dev; prod D1). `--status` compares deployed vs HEAD.
- D1: a pending recipe applies with `wrangler d1 execute oa-cw-s3-usage-db --remote --file site/migrations/<slug>.sql` (separate from deploy; needs `CLOUDFLARE_ACCOUNT_ID`); a new database starts from `--file site/schema.sql`.
- Tests: `PYTHONPATH=$PWD/src:$PWD/cloud/src pytest tests cloud/tests`; `cd site && pnpm build && pnpm vitest run`.
- Serving health: `dt-cloud healthcheck -u https://cw.oa.dev -s cw/` with a `cw`-scoped token (Secret Manager `cw-s3-job-grant`).
