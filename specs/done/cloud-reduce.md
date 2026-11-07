# Cloud reduce — scan a ~full laptop disk with zero local footprint

Phase 2 of `done/remote-scan-targets.md`. Phase 1 put the *persistent* blob
in the cloud and proved serve-from-cloud; the scan itself still ran on the
laptop, so the crisis case ("disk ~full, need to scan *it*") still needed local
scratch for the reduce and for the blob before upload. This phase removes
that: the pipeline splits so the only step that touches the full machine
barely writes to it, and everything downstream runs where there *is* disk.

    capture (laptop)         gfind → layer-1 listing shards streamed to R2
                             bounded memory, zero local disk
    reduce (anywhere)        shards → layer-2 scan blob (+ Scan row) → R2
                             a CI runner / VM / the laptop once its SSD is back
    serve (CFW / Flask)      range reads over the blob — Phase 1, unchanged

This is the same offline job marin's publish side runs and the one `apps/cfn/`
scaffolds; the only difference is the capture *source* (a laptop's listing vs.
bucket listings). So the reduce is built input-agnostic and the "local scan,
stored and served by the cloud" story becomes `apps/cfn/`'s headline demo,
with the R2 fleet union as a second input later — not a separate build.

## `disk-tree capture PATH --to URL`

Walks with the same `gfind` / excludes / sudo / error handling as `index`
(`backend.list`), but instead of building a frame emits **canonical layer-1
listing shards** — exactly what the bucket listers write:
`bucket, name, size_bytes, created, storage_class_id`, with `bucket` = the
scan root and `name` = the path relative to it. One batch (`-n`, default
200K rows) is buffered at a time, written through `blobfs` (so `r2://` works;
the listers' own `_write_shard` only knows fsspec's registered schemes), then
freed. Nothing is written locally.

Layout: `<to>/<host>/<root slug>/<stamp>/shard-NNNNN.parquet` +
`_SUCCESS.json` (`format, version, scheme='file', root, host, time, n_rows,
n_shards, error_count, error_paths`). Prints the capture dir.

**Files only.** Every engine treats a listing row as a file and derives
directories from name prefixes, so directory rows would read as phantom files.
Consequences, accepted: empty directories are not captured; directory
`mtime`s are derived, not recorded. No bytes are lost — APFS directories hold
0 blocks (measured: `%b` = 0 for every dir under `~/c`). Symlinks are captured
as files with their own block size, as `index` does.

**Unsorted.** Shards are in walk order; sorting 7M names is exactly the
scratch-hungry step the crisis can't afford. The DuckDB engine handles
unsorted input on the reduce side, where the disk is.

## `disk-tree reduce CAPTURE [--to URL]`

Reads the manifest, pulls the shards to a local work dir if the capture is
remote (the engines glob local paths, and this machine has disk — that's the
point), and runs `import_bucket` with the capture's root/time, `-e duckdb` by
default. `--to` uploads the blob (Phase 1's `set_write_target`); the Scan row
carries the capture's error count/paths; the diff index against the path's
previous scan is built like `index` does (`-D` skips).

`Scan.path` must be byte-identical to `index`'s: the engines' `scan_root =
f'{scheme}://{bucket}'` now goes through `backends.url.canonical`, which
collapses a `file` root to the bare path (`/Users/ryan`) and leaves every
cloud scheme unchanged. `import` also gained `-w/--to` (`-t` is `--time`
there).

Equivalence, tested: for the same tree, `capture → reduce` (pandas and duckdb)
reproduces `index`'s layer-2 rows (`path, kind, parent, uri, size,
n_children, depth`) exactly, minus the empty directory.

## Landed — capture + reduce (2026-09-06)

`cli/capture.py` (`capture`, `reduce`), `canonical()` scan roots in all three
engines + `import_bucket`, `import -w/--to`; 7 offline tests
(`tests/test_capture.py`) including the `index` equivalence for both engines;
full suite 499 green. Real chain against R2 (`file-tree-demo/disk-tree/`,
isolated root): `capture specs/` → shard + manifest in R2; `reduce` fetched the
shard *from* R2, aggregated (duckdb), pushed the blob *to* R2; the local root
held only the 28 K SQLite DB — **no `scans/` dir at all**; `du` read the scan
back from R2. Test objects deleted after.

What the run exposed for step 3: a runner's `reduce` records the Scan row in
*its* throwaway DB, so the laptop never learns about the blob. Hence the next
piece: `reduce` emits `<blob>.scan.json` beside a remote blob, and
`disk-tree scans register URL` imports it — the manifest route from
`done/remote-scan-targets.md`, made concrete.

## Reduce runner + the scan manifest (step 3)

**Manifest.** `reduce --to <url>` and `index --to <url>` write
`<blob>.scan.json` beside a remote blob (`scan_manifest.py`: `format,
version, time, path, blob, size, n_children, n_desc, mtime, error_count,
error_paths`). `disk-tree scans register SRC` (one manifest, or a dir/URL of
them) inserts the Scan rows into the local DB, idempotently (same path+blob
→ skipped), and notes when the blobs' dir isn't on the search path. A local
blob gets no manifest — the local DB already has the row. Tested: a scan
reduced under one root, registered and `du`-read under a fresh one.

**Runner.** `.github/workflows/reduce.yml`, `workflow_dispatch` with inputs
`capture` (URL), `to` (default `r2://disk-tree/scans`),
`engine`, `memory_limit` (5 GB; the runner has ~7 GB RAM, 14 GB disk — a
7M-row reduce peaks ~3.8 GB). `uv sync --extra r2`, then the plain
`disk-tree reduce -D -e … -M … -t <to> <capture>` — the same invocation runs
on a VM / Batch later. Credentials from repo **secrets `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`** and **variable `R2_ENDPOINT_URL`** (s3fs reads the
standard `AWS_*` env; `AWS_DEFAULT_REGION=auto` for R2).

The crisis loop, end to end:

    laptop   disk-tree capture ~ -t r2://disk-tree/captures     # prints the capture URL
    GitHub   Reduce workflow: capture=<that URL>, to=r2://disk-tree/scans
    laptop   disk-tree scans register r2://disk-tree/scans       # DISK_TREE_SCAN_DIRS includes it
    laptop   disk-tree du ~  /  disk-tree-server                        # served from the R2 blob

Verified against R2 with the laptop standing in for the runner (2026-09-06):
capture → R2; a "runner" root reduced *from* R2, leaving blob (3.8 KB) +
`.scan.json` (313 B) in R2; a *fresh* "laptop" root `scans register`ed it
from R2 and `du` read the tree back. Each root held only its 28 K SQLite DB.
Test objects deleted after.

Credentials: an R2 API token `disk-tree-reduce-GHA` scoped Object Read &
Write to the `disk-tree` bucket only (bucket is R2's tightest scope; verified
`file-tree-demo` → AccessDenied). On the laptop the same env applies:
`DISK_TREE_R2_ENDPOINT_URL` for `blobfs` (the `.envrc`'s `R2_ENDPOINT_URL`
is the GHA variable's name, not the one the code reads) and S3 credentials
botocore can see — `AWS_PROFILE=cf` (the admin R2 profile), or the `R2_*`
pair exported as `AWS_*`; a plain `AWS_PROFILE` pointing at an AWS account
fails with "access key has length 20, should be 32".

Manifest times: `Scan.time` is naive *local* wall clock (`index` stamps
`now().astimezone()`, SQLite drops the offset, the UI parses naive as local),
so the manifest carries the writer's offset (a UTC runner → `+00:00`) and
`register` converts to the reader's local wall clock. Before this, a
cloud-reduced scan showed "Scanned -13941s ago" everywhere.

## Serving from the cloud (step 4)

Not a second SPA (`apps/cfn/` was never needed): disk-tree's own `ui/` is
the cloud app, with the **read subset of its `/api/*` contract implemented as
Cloudflare Pages Functions** over the R2 bucket the reduce writes to. marin's
demo ships whole-tree JSON; a 7M-row laptop scan can't, so the Functions read
the parquet the way the Flask server does — `(depth, path-prefix)` row-group
pruning from the footer stats, each group a range read (`ui/cfn/parquet.ts`,
hyparquet). The UI is unchanged in shape; a `GET /api/capabilities` tells it
what this server can do, and `useCapabilities()` hides the rest (scan,
delete, reveal, histogram, filter, preview, compare, library, backend) and
skips the SSE stream. Flask answers the same endpoint with everything on; a
server without it counts as all-on, so older Flask peers are unaffected.

- `ui/functions/api/`: `scans` (from the `*.scan.json` manifests; ids are
  positional in `(time, blob)` order), `scan` (newest covering scan → pruned
  read → rebased to the requested uri; `max_rows` top-N + ancestors, like the
  server), `scans/history` (ancestor scans get the subpath row's stats),
  `capabilities`, benign `scans/running` / `scans/progress`, `[[path]]` → 501.
- `ui/cfn/`: the library (`parquet.ts`, `manifests.ts`, `http.ts`, `env.ts`)
  + vitest suites (18) over a 12-row fixture in 4-row groups
  (`cfn/tests/fixtures/gen.py`), including the handlers end-to-end through a
  directory-backed fake `R2Bucket`.
- `ui/wrangler.toml`: Pages project `disk-tree`, `dist/`, R2 binding `SCANS`
  → the private `disk-tree` bucket (created 2026-09-06; laptop captures list
  every path on the machine, so not the public demo bucket the R2 round-trip
  tests used), `SCANS_PREFIX = scans/`; `pnpm cfn:dev` =
  `wrangler pages dev dist --port 7789` (FE 7788 + 1).
- `aggregate_duckdb` now writes 64K-row groups (`ROW_GROUP_SIZE`): DuckDB's
  ~120K default is too fat for a Worker to decode per listing.

CIC'd 2026-09-06 (`wrangler pages dev` over an emulated R2 seeded with a real
`index --to file://` blob of `packages/`): scan list from the manifests,
`/api/scan` → treemap with `treemap 3.2M` / `react 3.0M`, ancestor history,
no console errors; the Flask peer still shows every control. Not yet: hybrid
chunk following (an `index --to` scan big enough to chunk drills into empty
subtrees), single-child auto-expand, the `/browse` file-tree page.

**Deploy**: in `ui/`, `direnv exec .. pnpm build && direnv exec .. pnpm exec
wrangler pages deploy --project-name disk-tree --branch main`. Auth is the
`disk-tree-wrangler` Account API token (Pages Write + Workers R2 Storage
Read) as `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` in the repo's
`.envrc` — `wrangler login` state is one machine-global file that every
long-running `wrangler dev` rewrites with its own identity (this is how the
first `disk-tree` bucket landed in the OA account), and a Pages config can't
pin `account_id`. Project created 2026-09-06; the R2 binding comes from
`wrangler.toml` on each deploy.

## Landed — the chain for real (2026-09-06)

    laptop   disk-tree capture ~ -t r2://disk-tree/captures
             5,819,971 files → 30 shards, 123 MB, ~5 min, 1 permission error
             (`~/.config/htop`); local footprint: none
    GitHub   Reduce (duckdb, 5 GB cap): 62 s wall, incl. `uv sync` + fetching
             the shards → 270 MB blob + `.scan.json` in r2://disk-tree/scans
    laptop   scans register r2://disk-tree/scans under a *fresh* root: 28 K
             SQLite; `du ~` read 374 GiB / 100 children back from R2 in 14 s
    Pages    disk-tree.pages.dev: /api/scans 0.46 s; /api/scan depth=2,
             2000 rows: 0.85 s, 450 KB; CIC'd scan list → listing → drill to
             `~/c` (84 children) → treemap (1731 cells), no console errors

What the real run shook out, each its own commit: unbounded `s3fs` resolved
to 0.4.2 (latest `boto3` conflicts with `aiobotocore`) whose empty
`x-amz-acl` R2 rejects on multipart — every shard past ~5 MB failed, so
`s3fs>=2024.10`; `LocalBackend` falls back to `find` (GNU on Linux) so the
CLI tests run on CI; ext4 directories hold 4 KiB each, so the `index`
equivalence test subtracts dir-own blocks; the manifest time convention
above; `wrangler pages deploy` rejects `account_id`.

Not done here: the laptop's *real* DB was not registered — the newest
`/Users/ryan` scan would then resolve only through `DISK_TREE_SCAN_DIRS`
= `r2://…` + R2 creds, which the LaunchAgent and `disk-tree-server` don't
have in their env; that's a `scans register` away once the env is
persistent. The first (failed) capture's two shards remain under
`captures/…/2026-09-07T02-25-58Z/` (delete needs a human). The static nav
still shows the Local / S3 / Recent tabs; `s3: false` in capabilities isn't
wired to them.

## Not in scope

The metadata routes (`snapshots.json` manifest / D1) — still as scoped in
`done/remote-scan-targets.md`; the reduce records into the local SQLite DB
today. Root `/` captures: `uri` for a `/` root would be `//path` through the
engines' `f'{scan_root}/{path}'`; verify before relying on a full-disk capture.
