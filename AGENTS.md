# disk-tree

Disk/cloud space usage analyzer: a scanning/indexing CLI (`disk-tree`), a cloud overlay (`dt-cloud`), and a Cloudflare-hosted site (`site/`) over the indexes.

## This worktree: `m3` — the laptop disk-cleanup deployment

This worktree (`~/c/disky/wt/m3`, branch `m3`) is **this Mac as a deployment**: the disk-cleanup loop below runs *on this branch's own code*, so a missing CLI feature or UX tweak is made right here and committed on `m3`; upstream decides what to take and how (commits tagged `[cloud]` or `[local]`, cherry-picked up by the root session — `specs/one-clone-layout.md`; m3-only commits are prefixed `m3:`). **`m3` merges `local`, never `cloud`** (since 2026-10-01, `specs/branch-layout.md`): `local` = `cloud` + the laptop-only code (capture, APFS/extents, drainer/trash, `du`/`scans`, walker seam, app-link, laptop executor, `~` display, age strata, migrations `0007`/`0008`); `cloud` has none of it, so merging `cloud` here would delete it all. Tag laptop-only commits `[local]` and ones every site uses `[cloud]` (the old `[base]` tag is retired). It replaced the old `~/.disk` workspace (a source-less dir that had turned into a tight loop of asks against upstream). The cleanup loop's own state is `log.md` (tracked, this branch only) and `tmp/`.

The global `~/.claude/CLAUDE.md` conventions still apply (git usage, `tmp/` scratch, commit style, etc.).

## The tool

`disk-tree` is this worktree's own build: `direnv` activates `.venv` here (`uv sync --all-packages --all-extras --all-groups` after a merge from `local` — a bare `uv sync` drops the `r2` extra's `s3fs`, and the agents' R2 writes fail), so `disk-tree` is on `PATH` in this dir; from elsewhere it's `~/c/disky/wt/m3/.venv/bin/disk-tree`. Its index is **global and cwd-independent** — DB + parquet blobs + duckdb under `~/.config/disk-tree/`, plus any external-volume search-path entry. So a scan run here lands in the same always-ready index every disk-tree session reads. Full command reference: the rest of this file.

## The cleanup loop

1. **Scan** — the scheduled scan (06:00 / 18:00) already publishes one: `disk-tree capture` of `/` → the Batch ingest → the site's path index in R2. For a fresh one now, the menu bar's **Scan now** (or `disky scan now`); ~15 min end to end. (A local `disk-tree index /Users/ryan` blob still works, `-C` to force fresh, but nothing schedules it any more.)
2. **Find the weight** — `disk-tree du -p r2://disk-tree/listing/laptop /Users/ryan -d1` (the newest path index in R2, ~3–7 s; needs `AWS_PROFILE=m3` + `DISK_TREE_R2_ENDPOINT_URL`, and `-p` can be `$DISK_TREE_PATH_INDEX`; without `-p` it reads a scan blob) (heaviest children per level, no re-walk; `-d` depth, `-n` top-N, `-a` files too). Descend into the fat nodes.
3. **Find the *real* win** — `disk-tree overcount PATH` (apparent vs `exclusive` = bytes only this subtree holds, i.e. what a delete actually frees) and `disk-tree reclaim PATH…` (true freed bytes for a keep-set, via extent intersection). macOS-only.
4. **Audit repos** — `disk-tree repos ROOT` before deleting code dirs: `recoverable` = clean tree + all branches pushed to a remote; `DELETABLE` also needs zero untracked. `-r` filters to recoverable, `-m` a size floor.
5. **Delete, with confirmation** (see Safety) — then re-scan the touched paths to confirm the space actually came back.

## The reflink caveat — reported size ≠ freed bytes

`disk-tree`'s sizes are **per-path block counts**. APFS clones (reflinks) and hardlinks let several paths share one set of extents, and each linking path is charged the full amount — so a subtree's reported size is an **upper bound** on what deleting it frees. The big one: **uv's default macOS link mode is `clone`, so `.venv`s share extents with `~/.cache/uv`** — deleting a venv frees far less than its size (measured: 35 dormant `.venv`s reported 16.9 GiB → freed 9 GiB). **Always check `reclaim`/`overcount` before trusting a size as reclaimable**, and don't thrash active venvs to reclaim bytes that clone-dedup already shares. For a whole-volume overcount, `apparent_total − df_used` is exact (only for a scan covering the entire volume).

## Safety (destructive ops)

- **Confirm before every destructive op** — delete, `rm`, empty-trash, cache-clear. Show the target and the *measured* freed bytes (`reclaim`/`overcount`, not the apparent size) first.
- **Look before you delete** — inspect the target; never delete something you haven't examined. Prefer pruning/moving a cache over a hard delete.
- **Pair each cache-clean with its refill cost** — a cache you clear that repopulates on next use (browser, build, uv, pnpm) buys little; prefer genuinely dormant data. Chrome needs per-profile care (logins/Superhuman live in App Support, not Caches).
- No `sudo` deletes. Prefer `disk-tree delete` (updates the index) or Trash over unrecoverable `rm`.

## Crisis mode — near-full boot disk, external not always attached

The point of a cleanup session is the disk is often already tight, and **x6 (the external SSD) isn't always mounted** — so avoid writing scan bytes to the boot disk during a crunch:

- **A mounted external volume auto-opts-in** as the scan write target once it has a `<volume>/disk-tree/scans` dir; unplugging just drops it from the search path (reads still resolve any local blob first).
- **R2 recipe (working end-to-end as of 2026-09-09, commit `5c32460`):** for a scan, set `AWS_PROFILE=m3` (R2 token "disky m3 laptop": Object R&W on `disk-tree` only, in `~/.aws/credentials`) and `DISK_TREE_R2_ENDPOINT_URL=https://0dcad5654e9744de6616f74b8df4af63.r2.cloudflarestorage.com`, then `disk-tree index --to r2://disk-tree/scans /Users/ryan`. To *read* a remote blob back (`du`, `scans chunks`, …) both env vars must be set **and** `DISK_TREE_SCAN_DIRS=r2://disk-tree/scans:/Users/ryan/.config/disk-tree/scans` (the URL to reach it, plus the local dir so discovery isn't dropped). The three bugs the first real run hit (multipart `InvalidPart`, crash on an unmounted cached blob, the since-removed diff step failing a persisted scan) are all fixed; the `tmp/dt-r2.py` wrapper is obsolete.
- **No external? Stream to cloud.** `disk-tree index /path --to r2://disk-tree/…` (or `capture PATH -t r2://disk-tree/…` for the bounded-memory, zero-local-disk split pipeline) writes the blob straight to the **dedicated private R2 `disk-tree` bucket** and reads it back through the same search path. Listing metadata is small; a laptop capture never has to touch the near-full boot disk.
- The store itself is a cleanup target: `~/.config/disk-tree/` holds the blobs + a ~0.8 GB `scans.duckdb` — `disk-tree index -g` GCs old scans, `disk-tree scans move` relocates blobs off-boot.

## The deployment: disk.rbw.sh, two LaunchAgents, `infra/aws/` + `infra/cf/`

- **disk.rbw.sh** (also `disk-tree.pages.dev`): the Pages project `disk-tree`, serving **`site/`** since the 2026-10-01 cut-over (specs/m3-site.md Phase 4), deployed from this worktree by `site/deploy-m3` (no CI). Auth is `@open-athena/auth` in-app (Google sign-in, emailed codes, share links; the `allowed_emails` / `admin_emails` rows in D1 `disk-tree-m3-db`); no Cloudflare Access. `ui/`'s Pages Functions and its D1 `disk-tree-auth` no longer serve anything (the files stay in the tree until `cloud` drops them).
- **`com.runsascoded.disky.scan`** (a plain plist disky.app writes into `~/Library/LaunchAgents/`, running `disky job scan --scheduled` in-process; SMAppService registration was dropped 2026-10-01 after a launch-constraint crash on rebuild): wakes every 15 min and scans only once a slot in `~/.config/disk-tree/disky.json` `settings.schedule` has passed: (06:00 and 18:00 local), capturing in-process: the scan job's `to` writes `$DISKY_SCAN_ROOT` (default `/`, the whole boot volume, one filesystem) as listing shards to `r2://disk-tree/captures/m3/root/<stamp>/` + `_SUCCESS.json`, as `AWS_PROFILE=m3` (token "disky m3 laptop", `disk-tree` bucket only). That marker starts the Batch ingest with nothing more from the laptop (since 2026-10-02, `specs/done/capture-ingest-trigger.md`): an R2 event notification → queue `disk-tree-captures` → Worker `disk-tree-capture-trigger` (`infra/cf/capture-trigger/`) → Batch `SubmitJob`, recorded in the capture's `_INGEST.json` (the job id). `aws/submit -w <capture>` (`AWS_PROFILE=r`) is the manual re-ingest. No `disk-tree index` scan blob since 2026-10-01: `du -p` reads the path index. Run one now: the menu bar's **Scan now** (it sets the force flag; a bare `launchctl kickstart` of the label no longer forces a scan). The scope (`DISKY_SCAN_ROOT`) is disky's `settings.scope`. Logs go to `~/Library/Logs/disk-tree/index.*.log`.
- **`infra/aws/`** (Pulumi, RAC AWS, local encrypted state; `aws/` until 2026-10-02): the shared `BatchIngest` component (`batch_ingest.py`) under the shared program, with m3's names from `Pulumi.rac.yaml` — the Batch ingest (queue `disk-tree-m3`, job def `disk-tree-m3-ingest`, 1 vCPU / 8 GiB Fargate Spot) and the capture trigger's IAM user. `infra/aws/ingest.sh` turns a capture into the `site/` path index + snapshot (`listing/laptop/<date>/index/<gen>/`, `snapshots/laptop/<date>/`) and syncs its footers to D1 `disk-tree-m3-db`. The image is built on CodeBuild from this worktree's sources on `pulumi up` (`infra/aws/build-image`; no local Docker). Secrets live in Secrets Manager, filled from `.envrc` by `aws/put-secrets`. Run one by hand: `aws/submit -w r2://disk-tree/captures/<host>/<root>/<stamp>`; job logs: CloudWatch `/disk-tree-m3-ingest/batch`.
- **`infra/cf/`** (Pulumi, shared `CfnDashboard` + `CaptureTrigger`, local encrypted state; `cf/` until 2026-10-02): the `disk-tree` Pages project, disk.rbw.sh domain + rbw.sh CNAME (adopted), D1 `disk-tree-m3-db` (the `site/` store's; `site/wrangler.toml` binds it), and the separate dev project `disk-tree-dev` → **dev.disk.rbw.sh** (the `site/` preview / staging mirror; same store, index and D1). Run under `direnv` (`.envrc` sets `CLOUDFLARE_API_TOKEN`). Both stacks run the shared config-only programs (`main: stack/` → `infra/{aws,cf}/stack/__main__.py`, from `local`/`cloud`; every m3 name is in `Pulumi.rac.yaml`, the keys in `stack/Pulumi.stack.example.yaml`) run by Pulumi's `uv` toolchain from `infra/pyproject.toml`: `cd infra/<aws|cf> && env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=$PWD/../.venv pulumi preview -s rac` (the worktree's `.envrc` otherwise points uv at the engine's `.venv`).
- **Deploying `site/`**: `site/deploy-m3` (production, `wrangler.toml` → `disk-tree`) or `site/deploy-m3 --dev` (`wrangler.dev.toml` → `disk-tree-dev`). Secrets per project: `STORE_ENDPOINT`, `STORE_ACCESS_KEY_ID`, `STORE_SECRET_ACCESS_KEY` (the "disky m3 laptop"/batch keys), `SESSION_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
- **Full Disk Access (TCC)**: both jobs run as children of `~/Applications/disky.app` (its `agent`/`job` launcher spawns, never execs), so TCC charges their reads to the app's single FDA grant — no re-grant after a uv python upgrade. Without FDA the walk silently skips `~/.Trash` and `~/Library/{Application Support,Containers,…}` (69 GiB on 2026-09-29); check a scan's `~/Library` total before trusting it. (Before 2026-09-30 the grant went to the resolved venv `python3.x`; those System Settings rows can go.)
- **`com.runsascoded.disky.drain`** (KeepAlive, disky.app's plist, `disky job drain`): `aws/laptop-drain` (the same FDA grant as the scan; `CLOUDFLARE_API_TOKEN` comes from `.envrc` via `direnv export json`, so no config holds secrets). It runs `disk-tree dispatch -s -t -x 7d` over the `site/` D1 (`disk-tree-m3-db`: runs the `laptop` executor records from `/staged`; `ui/`'s `disk-tree-auth` retired at the cut-over), writing its heartbeat each poll (the executor refuses a dispatch when that's >3 min stale). Dry runs size; real runs **trash** (rename into `~/.Trash/disk-tree/<run>/…`, `disk-tree trash ls|restore|empty`; emptied after 7 days) and then request a fresh capture (`disky scan now`) so the map moves. Logs go to `~/Library/Logs/disk-tree/drain.*.log`.

## Cleanup log

Keep `log.md` as the running record — each pass: date, what was scanned (free space before/after), what was deleted, and the **measured** bytes freed. It makes cleanup auditable and repeatable, and feeds the next pass (what refilled, what stayed gone).

## Project Vision

Track disk space usage across:
- Local filesystems (laptop, external SSDs)
- Object stores: S3, R2 and GCS buckets

Key goals:
- **Always-ready index**: Run overnight scans so you don't wait when running out of space
- **External media snapshots**: Keep cached views of SSDs even when unplugged
- **Fast indexing**: Shell out to `gfind`/`aws s3 ls` instead of slow Python stat calls
- **Site**: Treemap visualizations, diffs and an age lens over each scan's path index

## Architecture

### Python Backend (`src/disk_tree/`)

**Indexing** (`find/index.py`):
- Local: `gfind -printf '%y %b %T@ %p\0'` → null-terminated, 512-byte block sizes (handles sparse files)
- S3: `aws s3 ls --recursive` → parses listing format
- Excludes CloudStorage paths (`~/Library/CloudStorage`) to avoid blocking on cloud I/O
- Builds DataFrame with columns: `path`, `size`, `mtime`, `kind`, `parent`, `uri`, `n_desc`, `n_children`, `depth`
- `depth` column enables predicate pushdown when loading parquet (major performance win)
- Aggregates sizes upward through directory tree
- Returns `IndexResult(df, error_count, error_paths)`

**Data Model** (`sqla/model.py`):
- `Scan` table: `id`, `path`, `time`, `blob`, `error_count`, `error_paths`, `size`, `n_children`, `n_desc`
  - Root stats (`size`, `n_children`, `n_desc`) denormalized to avoid parquet reads on scan list
- `ScanProgress` table: real-time tracking of active scans
- Results stored as Parquet in `~/.config/disk-tree/scans/<uuid>.parquet`
- SQLite metadata DB at `~/.config/disk-tree/disk-tree.db`
- Index on `(path, time)` for efficient latest-scan-per-path queries

**CLI** (`cli/`):
```bash
disk-tree index [URL]     # Scan a directory, an s3:// bucket, or an r2:// bucket (S3-compatible: lists
                          # through the bucket's endpoint — `DISK_TREE_R2_ENDPOINT_URL` or its
                          # buckets.yml `endpoint_url` — with `r2://` uris). gcs:// has no live lister
                          # and refuses (`UnsupportedBackend`): use `bulk-list` + `import` for it
  -C, --no-cache-read     # Force fresh scan (`index` otherwise returns any cached scan unconditionally)
  -e, --require-external  # Skip (exit 0) if the write target is the boot disk (no opted-in external
                          # volume mounted). For scheduled scans that must land on external media
  -g, --gc                # Garbage collect old scans
  -m, --mean-mtime        # Emit `mtime_mean` (size-weighted mean mtime; feeds the UI age lens)
  -M, --measure-memory    # Track peak memory
  -o, --one-fs            # Don't descend into filesystems mounted below URL. `index -o /` on macOS = the
                          # System volume + the Data volume via its firmlinks, once (the whole machine:
                          # 8.74M entries / 457 GiB vs `~`'s 7.49M / 398 GiB, 2026-09-30). `capture -o` too
  -q, --no-progress       # Suppress the tqdm progress bar (scheduled/redirected runs — keeps logs small)
  -R, --auto-remote       # If the local write dir is low on space (< $DISK_TREE_LOW_SPACE_BYTES, 5 GiB)
                          # and $DISK_TREE_REMOTE_SCAN_TARGET is set, write the blob there instead
                          # (default: warn and suggest `--to`)
  -s, --sudo              # Run gfind with sudo (implies `-C`: a cached scan can't be known to be sudo)
  -t, --to TARGET         # Write this scan's blob to a dir or fsspec URL (r2://bucket/prefix, s3://…,
                          # gs://…) instead of the configured write dir; it joins the search path for
                          # this run, so the scan reads back through it (spec `remote-scan-targets.md`).
                          # A URL target also gets a `<blob>.scan.json` manifest (the Scan row, portable)
  -x, --extents           # Map physical extents → per-dir reclaimable bytes (APFS clones/hardlinks),
                          # written as a `<blob>.reclaim.parquet` sidecar. macOS + local scans only;
                          # exact when the scan root contains the sharing sources (home/full scan),
                          # else an upper bound. `du` shows a `frees` column when the sidecar exists

disk-tree capture PATH -t URL  # The split pipeline for a ~full disk (spec `cloud-reduce.md`): gfind →
                          # layer-1 listing shards streamed straight to a dir/URL (`r2://…`), bounded
                          # memory, zero local disk. Files only (dirs implied; APFS dirs hold 0 blocks).
                          # Prints the capture dir `<to>/<host>/<root>/<stamp>` (+ `_SUCCESS.json`);
                          # m3's AWS Batch ingest (`dt-cloud path-index`) aggregates it
disk-tree capture-meta MANIFEST META  # Copy the capture's physical APFS capacity/used/free bytes into
                          # the snapshot's meta.json (`disk_space`); local paths or fsspec URLs.
                          # The site's capacity bar reports these at-scan measurements, separately
                          # from the treemap's per-path apparent sizes. Missing measurements omit it
disk-tree scans register SRC  # Import `*.scan.json` manifests (one file, or a dir/URL of them) into
                          # this DB. `index --to <url>` writes one beside each remote blob (the Scan
                          # row, portable). Idempotent; put the blobs' dir on `DISK_TREE_SCAN_DIRS`
                          # so they resolve

disk-tree scans           # List cached scans (JSON)

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
                          # in SQL (never stored). 8K-row groups (`-r`), `tier`/`sort` (+ `bucket: log2`) in
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
disk-tree tiers plan SIDECAR P THR  # The reader's span selection run offline over a tier's `.groups.json`
                          # (phase 0's instrument): for `path` it mirrors `readRects` exactly (depth rect
                          # `dP+1..`, path range `[P/, P0)`, `b_max ≥ thr·atten^(d−dP−1)` per group); for
                          # `bysize` it is `b_max ≥ thr_min ∧ p_max ≥ P/ ∧ p_min < P0`. Reports groups
                          # selected, rows they hold, bytes (compressed chunks), and — from the parquet —
                          # rows that actually pass, i.e. the decode waste. `P` = `.` for the root; `-a`
                          # attenuation, `-d` max depth, `-t` tier (default: from the name), `-C` skips the
                          # count, `-j` JSON. Measured on a 20K-child flat dir at 2048-row groups: `path`
                          # decodes 10 groups / 20,015 rows for 1,250 answers, `bysize` 1 group / 2,048

disk-tree du URI          # Top-N heaviest children per level, from the freshest covering scan —
                          # `du -d1 | sort -rh` without a filesystem walk (-d depth, -n top-N,
                          # -a to include files, -j for JSON). Sizes are per-path block counts,
                          # so extents shared via APFS clones/hardlinks are charged to every
                          # linking path — see the caveat under Performance.
                          # Shows a `frees` column (true reclaim) when a `-x` reclaim sidecar exists

disk-tree reclaim PATH…   # What deleting PATHs would *actually* free: maps each file's physical
                          # extents (`fcntl(F_LOG2PHYS_EXT)`) and subtracts blocks the surviving
                          # partner roots still reference (`-p` adds one, `-P` drops the
                          # auto-detected uv/pnpm caches). macOS-only. Measured 2026-08-29:
                          # `oa/marin/.venv` reports 2.84 GiB, frees 249 MiB (91% cloned from
                          # `~/.cache/uv`); ~43 s, dominated by walking 756K partner files

disk-tree repos ROOT      # Delete-safety audit of git repos under ROOT (cleanup companion to `du`):
                          # size + dirty/untracked counts + whether every local branch is on a
                          # github/gitlab remote. `recoverable` = clean tree + all branches hosted;
                          # DELETABLE also requires zero untracked. `-r` filters to recoverable,
                          # `-m` sets a size floor (default 200M), `-j` for JSON

disk-tree overcount URI   # How much URI's apparent size overstates physical bytes (APFS clones +
                          # hardlinks). Per top-level child: apparent vs `exclusive`
                          # (ATTR_CMNEXT_PRIVATESIZE — bytes only this subtree holds, i.e. what a
                          # delete frees) vs `shared`. No open() per file; ~32K files/s. macOS-only.
                          # Measured 2026-08-29: `oa/marin` 18.7 GiB apparent → 3.92 GiB exclusive

disk-tree volumes [PATH]  # The APFS container PATH (default `/`) lives on: each volume's used bytes, mount
                          # point and snapshots, plus free space — what no walk shows (Preboot, VM/swap,
                          # Recovery, the sealed System volume, OS-update snapshots). `-j` JSON. macOS-only

disk-tree stage URI…      # Stage URIs for deletion into a shared open plan (spec staged-delete.md,
                          # CP1). The opt-in "delete" model: nothing dies by inaction
disk-tree staged          # List open plans (staged sets) + recent runs (-j for JSON)
disk-tree unstage URI…    # Remove URIs from every open plan
disk-tree undo RUN_ID     # Undo a deletion run: restore the objects it deleted where the store allows
                          # it (S3/R2 versioning — remove the delete-markers; local has no undo).
                          # Dry by default (report restorable scope); `-f`/`--for-real` restores. Records
                          # the run's `undo_state` (spec staged-delete.md CP5)

disk-tree dispatch [PLAN] # Execute a plan (id/name; default the open `Staged` plan): delete its
                          # staged URIs via the backend, or (default) dry-run + report bytes/objects.
                          # `-f`/`--for-real` deletes + closes the plan; records a run + per-URI bands.
                          # `-s`/`--serve` instead runs the CP4 drainer: poll the edge's D1 (the
                          # browser dispatched runs there; the edge can't reach user buckets) via the
                          # `CLOUDFLARE_API_TOKEN` and execute each enqueued run here, deleting through
                          # `backend_for` (`-i` base poll secs, `-o` once, Ctrl-C stops). Announces
                          # per-run results to Slack/Discord per the `delete:` block in buckets.yml
                          # (`chat`/`undo`/`database_id` + secret ENV VAR NAMES); needs `notify` for chat
```

### Site (`site/`) and widget packages

`site/` is the hosted/serverless app: Vite + React + TypeScript, with Pages Functions over R2 / GCS +
D1 (disk.rbw.sh, r2.rbw.sh, the gcs/cw deployments). Its chart-lib-free DIY-SVG/canvas widgets come
from two workspace packages:
- **`@rdub/treemap`** (`packages/treemap/`) — the SOTA treemap core + layout/color primitives:
  `<Treemap>` (SVG + canvas renderers, shared-edge tiling, dust texturing, per-cell `ring`
  emphasis, hover/pin events), `<VoronoiTreemap>` (`/voronoi` subpath), `useHoverPin`, `squarify`,
  `DEFAULT_PALETTE`/`ageFade`/`parseQuery`, `styles.css`. Disk-agnostic; the intended external
  consumer (e.g. file-tree) pins this.
- **`@disk-tree/react`** (`packages/react/`) — `<TimeSeries>`/`<BytesOverTime>` built on the core
  (which it re-exports for back-compat).

### Cloud site auth routes (`site/functions/`)

One gate (`_lib/auth.ts`, `@open-athena/auth` over D1): `identify` → `Identity` (`via: session | grant | public`), `requireViewer` / `requireStager` / `requireAdmin`.
- `/auth/google` (+ `/callback`, `/onetap`) — Google OIDC → email session; `/auth/email/*` — emailed code / magic link → email session
- `/api/auth/*` — the package's routes: `whoami`, `exchange` (`?key=` share link → grant session), `logout`, request-access, admin grant/request/log console
- `/api/token` — a session's personal agent token (a non-expiring Bearer grant, the base scope only; grants can't mint one)
- `POST /api/app-link` → `GET /auth/app-link?token=` — the macOS app's sign-in hand-off ("Open in disky" in the user menu): a session mints a single-use, 60 s grant for its own email + scopes (same-origin POST only); the app's webview redeems it into an ordinary email session and the grant is revoked. Contract: `specs/app-link.md`

## Development

```bash
# Python setup — one uv workspace: the engine (root) + the cloud overlay
# (`cloud/`, package `dt-cloud`) share ONE `uv.lock` and one `.venv`.
uv sync                                                 # engine only
uv sync --all-packages --all-extras --all-groups        # engine + dt-cloud, every extra, test groups
disk-tree index .

# Site
pnpm install
cd site && pnpm dev
```

## Packaging / Distribution

`uv build` builds the engine's wheel (`src/disk_tree`; CLI only); there is no release workflow.
`dt-cloud` (`cloud/`) is a workspace member installed from the lock, not published.

## Data Flow

1. `disk-tree index /path` runs `gfind` or `aws s3 ls` (`capture` / `bulk-list` → `import` for the
   split and bulk pipelines)
2. Output parsed into DataFrame, aggregated by directory
3. Saved as Parquet, metadata recorded in SQLite
4. `dt-cloud path-index` / `disk-tree tiers` cut the path store; `dt-cloud index-sync` publishes its
   footers to D1
5. `site/`'s Pages Functions read the path store (row-group range reads) and render treemaps / diffs

## Config

Default paths (override with `DISK_TREE_ROOT`):
- `~/.config/disk-tree/disk-tree.db` — SQLite metadata
- `~/.config/disk-tree/scans/` — Parquet blob storage

**Blob storage is a search path, not a single directory.** The DB stays on the boot disk (small, always mounted); blobs may live anywhere on `config.scan_read_dirs()`, since `Scan.blob` holds a basename. Creating `<volume>/disk-tree/scans` on an external volume opts it in — no config needed — and it becomes the *write* target while mounted; unplugging simply drops it out of the search path. `DISK_TREE_SCAN_DIRS` (colon-separated, priority order) overrides discovery, and an explicit `DISK_TREE_ROOT` disables it entirely so tests and alternate profiles stay self-contained. A candidate under an unmounted `/Volumes/<name>` is never written to — that would silently create the directory on the boot disk.

A search-path entry may also be an **fsspec URL** (`r2://bucket/prefix`, `s3://…`, `gs://…`) — the remote-target story for a boot disk too full to hold scan output (spec `remote-scan-targets.md`). `index --to <url>` (or `DISK_TREE_REMOTE_SCAN_TARGET` + `-R`) writes a scan's blob there, and reads resolve it through the same search path — local dirs are checked first, so a local blob never costs a round-trip. `r2://` rides s3fs with the bucket's endpoint from `DISK_TREE_R2_ENDPOINT_URL` or its `buckets.yml` entry. Every parquet blob read/write goes through `blobfs.py` (the local-vs-URL seam); the reclaim sidecar and `--extents` are local-only and skip remote blobs. The shallow sidecar (`<root-stem>.shallow.parquet`, each chunk's top level, written by every hybrid save) follows the blob anywhere, and scan blobs are written in 64K-row groups so a `depth`/`path` pushdown over R2 fetches kilobytes.

**Cross-account credentials** — a `buckets.yml` entry (or `defaults`) may carry a `profile:` naming an AWS credential profile (`blobfs.bucket_profile`), so a source and a target in *different* accounts each authenticate with their own key inside one `index --to` run. It threads to every S3/R2 seam: the `s3fs` blob IO (`_s3fs(endpoint, profile)`), the `aws`-CLI lister (`S3Backend(profile=…)`), and the `boto3` bulk lister (`S3BulkLister(profile=…)`, `bulk-list -f`). No profile → ambient credentials (env / default profile), the single-account default. Cross-account needs per-bucket endpoints too, so leave `DISK_TREE_R2_ENDPOINT_URL` unset (it globally overrides all per-bucket endpoints).

- `disk-tree scans dirs` — show the write target and every read dir, with blob counts (URL entries show reachability)
- `disk-tree scans move [DEST]` — relocate blobs between local dirs (no DB rewrite). Keeps each path's newest scan **and its chunk closure** on the boot disk by default (`-L` to move those too), so browsing the latest scan doesn't depend on the volume being plugged in

Stream-engine tuning knobs (env, all with measured defaults — see the constants block in `find/aggregate_stream.py`):
- `DISK_TREE_FLUSH_ROWS` — output row-group size (read-side: smaller = less fetched per directory browse, bigger footer)
- `DISK_TREE_PARALLEL_FINALIZE_MIN_ROWS` — below this the finalize stays serial even at `-j N`
- `DISK_TREE_SCAN_BATCH_ROWS` — pass-1 read-batch size (partition balance / seek granularity)

## Tests

```bash
pytest tests/                    # engine
cd cloud && pytest               # dt-cloud (same venv; sync with --all-packages first)
```

Test fixtures in `tests/data/` (mock gfind/s3 output → expected parquet). CI and the job images install `--frozen` from the workspace lock (`deploy/sheet-mirror/Dockerfile` is the reference recipe: `uv sync --frozen --no-dev --no-editable --package dt-cloud --extra …` into `UV_PROJECT_ENVIRONMENT=/usr/local`); a plain `pip install .` resolves fresh and ships pins the tests never ran.

## Performance

- Depth column enables parquet predicate pushdown (only load needed rows)
- `StorageBackend.load(path_prefix=)` pushes a subtree restriction down to parquet row-group pruning (rows sorted `(depth, path)`); wired into `du` / `scans` / staged-delete reads
- Denormalized stats avoid parquet reads for the scan list

**Sizes are per-path, not per-extent.** `gfind -printf '%b'` reports blocks allocated to a *path*; APFS clones (reflinks) and hardlinks let several paths share one set of extents, and each linking path is charged the full amount. So a subtree's reported size is an upper bound on what deleting it frees. Measured 2026-08-29: deleting 35 dormant `.venv` dirs totalling 16.9 GiB freed 9 GiB — uv's default macOS link mode is `clone`, so the remainder stayed live in `~/.cache/uv`. Clones are invisible to `stat` (distinct inodes, `nlink == 1`), so inode/link-count bookkeeping catches hardlinks only — and hardlinks are nearly irrelevant here: a census of `$HOME` found `nlink > 1` over-counting just **4.3 GiB of 385.9 GiB (1.1%)**, which is why `%i`/`%n` are *not* indexed. `disk-tree reclaim` (extent intersection, for a custom keep-set) and `disk-tree overcount` (`ATTR_CMNEXT_PRIVATESIZE`, no open per file, for apparent-vs-exclusive) answer the question properly, on demand. The whole-*volume* overcount is free without either — `df` counts shared blocks once, so `apparent_total − df_used` is the number, but only for a scan that covers the entire volume (a subtree's apparent can't be compared to the volume's `df`); for a subtree, `overcount`'s `Σexclusive` is the physical footprint.

## TODOs / Known Issues

- Scheduled captures run at 06:00 / 18:00 through disky.app; the R2 success marker triggers the cloud ingest.
- S3 pagination not explicitly handled (relies on aws cli)
