# Remote scan targets — stream a scan to the cloud when local disk is tight

Motivating case ("crisis"): the **boot disk is ~full**, I need to scan *it* to
decide what to delete, but there's no local room for the scan output (blob +
scratch) — and my external SSD may or may not be attached. Today the blob store
is local-only (boot disk, or an opted-in external volume when mounted); with
neither available the scan has nowhere to land.

This spec adds a **cloud store as a first-class scan target**: write the scan
to `r2://…` (or any fsspec URL) instead of local disk, and read/serve it back
from there. It reuses an abstraction disk-tree already has rather than bolting
on a new one.

## The key realization: blobs are a search path, entries can be URLs

CLAUDE.md already says: *"Blob storage is a search path, not a single
directory."* `config.scan_read_dirs()` returns an ordered list of dirs; the
newest-writable one is the write target (`scan_write_dir()`); `Scan.blob` holds
a **basename**, resolved against the search path (`resolve_scan_blob`).
Unplugging a volume just drops its dir out of the path.

A cloud target is **one more entry on that path — an `r2://bucket/prefix` URL
instead of a local dir**. Nothing about the model changes: the blob stays a
basename, the DB row is identical, reads resolve across the path, and the
"external volume" mental model ("opt in by creating a dir, it becomes the write
target while present") carries over verbatim to "point at a bucket prefix, it
becomes the write target when local is tight / when asked."

This is deliberately *not* "make `Scan.blob` a URL." Keeping the URL in the
*dir* (search path) not the *blob ref* means the same scan is portable across
stores (re-point the dir, the basename still resolves), and every existing
`resolve_scan_blob` caller keeps working.

## Two phases (don't conflate them)

The pipeline is **capture** (`gfind` walk → raw listing) → **reduce**
(aggregate → tree blob) → **serve** (read the blob). Local-disk pressure hits
capture-scratch, reduce-scratch, and the persistent blob differently:

### Phase 1 — remote blob (this spec's implementation)

Scan runs locally (gfind + reduce, using local *transient* scratch), but the
**persistent blob is written to the cloud** and read back from there. Removes
the GBs that *accumulate* on boot disk (the `~/.config/disk-tree/scans` store is
already 1.1 GB and grows per scan) — the transient reduce scratch is reclaimed
immediately after. This is the right tool for *"disk is getting tight"* and for
keeping the persistent store off the boot disk indefinitely. It is **not**
zero-footprint: the reduce still needs some local scratch, so *"0 bytes free"*
is out of scope here (the user frees a little space first, or Phase 2).

Scope:
- Search-path entries may be fsspec URLs; `scan_write_dir()` may return one.
- Parquet file-backend `save`/`load`/`adopt_parquet` do fsspec-aware IO. Reads
  keep predicate pushdown — pyarrow/pandas read `r2://…parquet` via range GETs
  using row-group stats (the `(depth, path)` sort + 64K row groups already make
  this efficient; same pushdown as local).
- `disk-tree index --to <url>` (and a configured default remote) select the
  cloud target for a scan.
- **Space-aware UX**: before writing, check free space on the local write
  target (`shutil.disk_usage`); if it's under a threshold (or below an estimate
  of the blob size), **warn and suggest `--to`**; with a configured default
  remote + opt-in, auto-redirect. The user can also just always pass `--to`.

### Phase 2 — capture/aggregate split + cloud reduce (later, not now)

For *true* zero-local-footprint ("0 bytes free"): stream the raw `gfind`
listing straight to `r2://…` (≈0 local write — just the pipe buffer), then run
the **reduce on a fat compute env** (GCP Batch / a CI runner / a container /
the laptop once the SSD is back), reading listings from and writing the tree
to the object store. This is the same offline job marin's publish side runs and
the one `apps/cfn/` will scaffold — so Phase 2 largely *is* the CFW-demo offline
job pointed at a personal scan. **The reduce is a Batch/container job, not a
Cloudflare Worker** — a 7M-row reduce peaks ~3.8 GB, far past a Worker's
128 MB / CPU-time envelope. CF's role stays *serving*.

Deferred here; called out so Phase 1 doesn't paint into a corner.

## Cross-machine / the metadata DB (revisit later, toggle separately)

Phase 1 keeps the **SQLite metadata DB local** (boot disk). Consequence: a
blob-in-cloud scan is discoverable only from *this* machine — only its DB holds
the pointer. Raw blobs alone aren't self-describing.

To make cloud scans usable from *another* machine (or the CFW demo), the
metadata must travel too. Two routes, **built and toggled independently** of the
blob-target switch:
- **Static manifest** — `disk-tree snapshots DEST` already emits
  `snapshots.json` (the `Scan` rows: id/path/time/stats + a pointer to each
  `tree.parquet`). That *is* "the DB in the cloud, lite": portable, read-only,
  no-live-Python. Publish it beside the blobs and any consumer can list+read.
- **D1** — "the DB in the cloud, full": mutable, queryable, server-side. Schema
  maps 1:1 (both SQLite). Earns its place when the cloud side needs to *mutate*
  the scan list (record scans, reflect deletes) — i.e. alongside the CFW demo's
  step-4 delete flow. `snapshots.json` is the smell-free choice for read-only;
  D1 is the upgrade when writes appear.

So the "totally zero-local-footprint" vision = Phase 2 (capture+reduce in cloud)
+ metadata in manifest/D1. Both are additive to Phase 1 and independently
switchable; neither blocks shipping remote blobs first.

## Config / UX surface (Phase 1)

- `DISK_TREE_SCAN_DIRS` already colon-separates dirs in priority order — allow
  `r2://bucket/prefix` (and other fsspec URLs) as entries. A URL entry is
  treated as "mounted" when reachable (lazy — don't probe on every import).
- `~/.config/disk-tree/buckets.yml` (the existing bulk-sync config) or a small
  `remote_scan_target` config key names the default cloud target, so `--to`
  needn't be retyped.
- `disk-tree index` flags:
  - `-t, --to <url>` — write this scan's blob to `<url>` (an fsspec dir URL).
  - a low-space **warn+suggest** by default; an opt-in auto-redirect when a
    default remote is configured (flag name TBD in impl, e.g. `--auto-remote`).
- `disk-tree scans dirs` already prints the write target + every read dir with
  blob counts — extend to show URL entries and their reachability.

## Out of scope

- Phase 2 (capture streaming + cloud reduce), the CFW-demo offline job.
- Moving the metadata DB (manifest/D1) — documented above, deferred.
- Non-parquet backends (sqlite/duckdb table stores) writing remote blobs —
  Phase 1 targets the parquet file-backend blob only (the default hybrid
  backend's parquet layer); table-store backends keep local semantics.

## Sequence

1. Config: URL-aware search path (`scan_read_dirs`/`scan_write_dir`/
   `resolve_scan_blob`), reachability handling, `scans dirs` display. Tests.
2. Parquet file-backend fsspec IO (`save`/`load`/`adopt_parquet`), pushdown
   preserved. Tests (round-trip + depth/prefix pushdown against a URL blob,
   using a local `file://` or a moto/minio-style stub so CI stays offline).
3. `disk-tree index --to` + space-aware warn/suggest. Tests.
4. CIC/manual: real `r2://` scan of a small local dir, read it back through the
   server, confirm the treemap renders from the cloud blob.

## Landed — Phase 1, steps 1–3 (2026-09-06)

- **`blobfs.py`** — the one local-vs-URL seam: `is_url`/`join`/`fs_for`
  (`r2://` → s3fs with the endpoint from `DISK_TREE_R2_ENDPOINT_URL` or the
  bucket's `buckets.yml` entry; everything else `fsspec.url_to_fs`),
  `exists`/`size`/`mtime`/`read_schema`/`read_table`/`read_parquet`/
  `write_parquet`/`write_table`/`remove`/`put`/`list_parquets`. fsspec is
  imported lazily (it stays an optional extra); remote `exists` hits are
  remembered (blobs are immutable UUIDs), misses never are.
- **`config.py`** — URL entries in `DISK_TREE_SCAN_DIRS` (`split_dirs` splits
  on `:` not followed by `//`), URL = always "mounted", `_ensure_dir` no-ops for
  a URL, `resolve_scan_blob` is two-pass (local dirs first, so a local blob never
  costs a round-trip), `set_write_target()` + `on_write_target_change` (resets
  the backend singleton) — what `--to` uses.
- **Backends** — `HybridBackend`/`ParquetBackend`/`adopt_parquet` route every
  parquet read/write/exists/remove through `blobfs`; pushdown is unchanged
  (row-group stats read from the footer, only overlapping groups fetched). A
  chunked hybrid scan's child blobs land remotely too.
- **`diff.py` / `diff_index.py` / `server.py`** — chunk-map, `resolve_chunk_for_path`,
  `load_scan_table`, the server's child-chunk follow and its in-place delete
  rewrite all go through `blobfs`, so the auto diff-index after `index --to`
  and serving/deleting over a remote scan work.
- **`index -t/--to`, `-R/--auto-remote`**, low-space check (`DISK_TREE_LOW_SPACE_BYTES`,
  default 5 GiB; `DISK_TREE_REMOTE_SCAN_TARGET` names the default remote):
  warn + suggest by default, redirect with `-R`. `scans dirs` shows URL
  entries with blob counts / reachability; `scans move` refuses URLs.
- **Local-only, skipped for a remote blob** (by design, not ported): the vocab
  sidecar (`vocab` refuses; the filter's indexed path degrades to brute), the
  reclaim sidecar / `--extents` (skips with a note), `migrate*`.
- **Tests** (37, all offline): `test_blobfs.py`, `test_remote_scan_dirs.py`,
  `test_remote_storage.py` (both backends × `memory://`, chunked hybrid,
  `file://`), `test_index_remote.py` (CLI end-to-end over `file://`: `--to`,
  read-back through `scans dirs`, low-space warn, `-R` redirect, diff index
  over remote blobs).
- Gotchas found: pyarrow swaps fsspec's `LocalFileSystem` for its native one on
  write, so `auto_mkdir` is ignored — `blobfs._ensure_parent` makes the parent
  for the local driver only. `uv.lock` pins **`s3fs 0.4.2`** (2020-era, vs
  `fsspec 2026.7`): it imports and constructs, but is the piece step 4
  exercises for real.

## Step 4 — real `r2://` round-trip + CIC (done 2026-09-06)

Target `r2://file-tree-demo/disk-tree/scans` (Ryan's demo bucket, isolated
prefix), endpoint via `DISK_TREE_R2_ENDPOINT_URL` + `AWS_PROFILE=cf` (no
`buckets.yml` on this machine), everything under an isolated `DISK_TREE_ROOT`
so the real DB never saw the test scan:

- `index -C -t r2://…` of a 3-file dir: hybrid blob (6.6 KiB) written straight
  to R2 (`aws s3 ls` confirmed the object); **no local `scans/` dir was created
  at all** — zero persistent local footprint for the blob.
- Read-back: `scans dirs` → `* r2://… (1 blobs, remote)`, local `(absent)`;
  `du` rendered the tree from the cloud blob.
- Serve: `disk-tree-server` with `DISK_TREE_SCAN_DIRS=r2://…` listed the scan,
  and opening it in Chrome (`/file/…/r2-src`) hit `/api/scan?…&depth=2` → 200
  and rendered the listing + treemap from the R2 blob; no console errors.
- This exercised `s3fs 0.4.2`'s read *and* write paths through pyarrow's
  fsspec handler against R2 — the lock pin is fine as is.
- Test blob deleted afterwards; prefix left empty.

Phase 1 is complete. Phase 2 (capture/aggregate split + cloud reduce) and the
metadata routes (`snapshots.json` manifest / D1) remain as scoped above.
