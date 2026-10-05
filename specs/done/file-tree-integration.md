# disk-tree ⇄ @rdub/file-tree: the two halves join

Written from the `@rdub/file-tree` session (2026-08-31). Companion spec:
`~/c/js/file-tree/specs/tree-sources-and-treemap.md` — read it first; this one
is the disk-tree side of the same integration and refers to its interfaces.

## The thesis

file-tree and disk-tree are two halves of one thing, split by what they know
about a tree. **file-tree is horizontal + lazy**: it stands at a prefix, lists
its immediate children (and their own sizes), and drills by navigation — it
never holds more than the level on screen, which is why a directory row shows
`—` for its size. **disk-tree is vertical + materialized**: a scan walks the
whole subtree once and stores recursive rollups (one row per file *and* per
directory, the directory rows carrying the subtree total), plus history and
diffs. The number file-tree is missing — a directory's recursive size — is
exactly the number disk-tree computes.

Today the two libraries already coexist in **marin-gcs-usage** (the `gcs`/`cw-s3`
branches), but disconnected: `@disk-tree/react`'s `<Treemap>` is the dashboard,
`@rdub/file-tree`'s `<FileTree>` is the raw scan browser at `/files`, and they
share only a bucket and a `← treemap` link. Joining them — and doing it in the
libraries rather than in the product fork — lets each library own the half it's
best at and lets marin (and disk-tree's own `ui/`, and a dozen other file-tree
consumers) *compose* them.

file-tree is adding a third source seam, **`TreeSource`** — sibling to its
`Store` (bytes + listing) and `TableSource` (pushed-down rows) — that abstracts
"a recursively-sized tree, over time, diffable," and a treemap view that wraps
**our** `<Treemap>`. This spec is the reciprocal work in disk-tree so the two
snap together.

## What file-tree is building (context)

- `TreeSource` interface: `children({path,depth,snapshot}) → {node, children}`,
  plus optional `snapshots()`, `diff({a,b,path})`, `scan()/scanStatus()`, and a
  `capabilities: {history,diff,scan,lazy}` flag set that gates UI chrome.
- A neutral node, `TreeNode` — `{path,name,kind,size,nChildren?,nDesc?,mtime?,
  mtimeMean?}` — deliberately a camelCase mirror of our `Row`
  (`storage/base.py` columns `path,size,mtime,kind,parent,uri,n_desc,n_children,
  depth` + `mtime_mean`).
- Three implementations mirroring its three SQLite modes: `walkTreeSource`
  (walk any store in JS, no scan infra), `snapshotTreeSource` (read precomputed
  rollup parquet/JSON from a bucket), `httpTreeSource` + `createTreeHandlers`
  (its own client/server protocol) — **plus a thin `diskTreeTreeSource` adapter
  that speaks our existing Flask API directly**, so a live disk-tree server
  needs no changes to drive file-tree's treemap.
- A `renderers/treemap` that wraps `@disk-tree/react`'s `<Treemap<TreeNode>>`
  via its accessors (`getSize`, `getChildren`, `hasChildren`, `loadChildren`,
  `getLabel`, `cellHref`, `colorForCell`/`lens`).

Our contracts are already a near-perfect fit — the work below is mostly making
them *addressable* and *stable*, not rewriting them.

## Half A — disk-tree adopts `<FileTree>` for its browse surfaces

disk-tree's `ui/` hand-rolls a directory/bucket browser: `DataTable`,
`ScanDetails`'s row list, `S3BucketList`. That listing-of-a-store is precisely
`<FileTree>`'s job, and marin already proved a disk-tree-adjacent product can
mount it. Adopt it:

- Mount `<FileTree>` (from `@rdub/file-tree/react`) for the "browse the raw
  scanned objects / a bucket prefix" surfaces, backed by an `HttpStore` against
  a small `/files/{list,get}` endpoint (our `server.py` gains it, or reuses the
  scan store), and its `ParquetViewer`/JSON renderers for the blob files.
- Keep `<Treemap>` and the rest of `@disk-tree/react` for the *aggregate* views
  — that's the half file-tree does **not** replace. The split is clean:
  file-tree renders "what's in this prefix"; `@disk-tree/react` renders "how big
  is everything under it, over time."
- Net for `ui/`: delete the hand-rolled dir-listing/table code, gain file-tree's
  breadcrumbs, filtering, format viewers, download affordance, URL state — for
  free and shared with every other file-tree consumer.

This is optional-but-recommended: it's the "we eat our own reciprocal dogfood"
half, and it keeps disk-tree's browser from drifting from the one a dozen apps
use. Sequence it after Half B/C (which unblock file-tree first).

## Half B — disk-tree as a `TreeSource` backend

Three sub-parts, in priority order. B2 is the smallest and lights up the most.

### B1 — publish snapshots file-tree's `snapshotTreeSource` reads (Layer 1)

file-tree's Layer 1 reads precomputed rollup rows from a bucket with **no live
Python**. Our at-rest parquet is already the right shape and the right layout:
sorted `(depth,path)` in 64K-row groups, with `path_prefix_bounds`
(`storage/base.py:20-29`) giving "descendant of" as a min/max-prunable range —
so `children(path, depth=d)` is a `depth == d && path_prefix` predicate that
prunes to a handful of row groups. To make it consumable as a static snapshot:

- Define a stable published layout, e.g. `snapshots/<id>/tree.parquet` (the
  scan blob as-is), `snapshots/<id>/diffs/<a>-<b>.parquet` (the diff blob), and
  a small `snapshots.json` index at the root: `[{id, time, size, n_desc}]` (the
  denormalized `Scan` columns `model.py:114-118`, so the index needs no parquet
  read). This is what marin's job already does in spirit
  (`snapshots/<date>/{tree,age,meta}.json`) — generalize it to the parquet
  contract so it's not marin-specific.
- Document the row schema as the **public** contract (it already lives on
  `StorageBackend.save`, `storage/base.py:60-71`). file-tree maps
  snake→camel at the boundary; the columns don't change.
- Optional nicety: because the snapshot is a parquet, file-tree can read it
  through its own SQLite-VFS / hyparquet path and reuse the colo block cache
  from its `sqlite-and-table-sources` work — no server at all. Nothing for us to
  do here beyond keeping the layout range-friendly, which it is.

### B2 — expose the live API as a `TreeSource` (Layer 2, adapter — no rewrite)

Our Flask API is *already* a `TreeSource` in all but name. file-tree ships a
`diskTreeTreeSource({baseUrl})` that maps:

| `TreeSource` method | our endpoint | notes |
| --- | --- | --- |
| `children({path,depth})` | `GET /api/scan?uri=&depth=N` (`server.py:391`) | returns `{root,children,rows}` of `Row`; map `uri`→drill key, snake→camel |
| `snapshots()` | `GET /api/scans/history?uri=` (`server.py:1015`) | `(path,time)`-keyed scans → `{id,time,size}` |
| `diff({a,b,path})` | `GET /api/compare` (`diff_index.py:383-391`) | `status ∈ {added,removed,changed,touched,unchanged}` maps 1:1 |
| `scan()` | `POST /api/scan/start` (`server.py:1787`) | returns `{job_id,path,status}` |
| `scanStatus(id)` | `GET /api/scan/status/<id>` (`server.py:1818`) | `pending\|running\|completed\|failed` |
| progress | SSE `/api/scans/progress/stream` (`server.py:1859`) | optional live `itemsFound`/throughput |

The only things worth tidying on our side so the adapter is thin:

- **CORS**: file-tree clients are cross-origin (a static SPA hitting the Flask
  server). `flask-cors` is already a dep; make sure the `/api/scan*` routes
  advertise it, and expose the headers a browser needs.
- **A snapshot id that round-trips.** `/api/scan` takes `uri` + optional
  `scan_id`; history returns times. Make `Snapshot.id` = the integer scan id
  (stable, opaque), accepted by `/api/scan?...&scan_id=` and `/api/compare`, so
  file-tree can pin a view to a snapshot and diff two by id.
- **`children` depth default.** file-tree wants ~1–2 levels per drill; our
  `depth=2` default (`ui/src/api.ts`) is right — just document it as the
  contract.

Cost: near-zero. This is what makes a *live* disk-tree server — and marin's —
drive file-tree's treemap immediately.

### B3 — scan dispatch (already done)

`POST /api/scan/start` + `GET /api/scan/status/<id>` + the SSE progress stream
already are file-tree's `scan()`/`scanStatus()`/progress. No new work; B2's CORS
tidy covers it. This is the "click a button in the file-tree UI to dispatch a
scan of a tree it cares about" the integration wants — it exists today, it just
needs to be reachable cross-origin.

## Half C — ship `@disk-tree/react` as a pinnable dist (the reciprocal API)

file-tree wraps our `<Treemap>` as a lazy-loaded **optional peer**. But
`@disk-tree/react` is source-only today (`package.json`: `main`/`types` →
`./src/index.ts`, no build, `files: [src]`, ESM). A consumer's Vite compiles the
raw TS fine, and marin consumes it as `workspace:*` — but a `tsup`-built library
like file-tree, and any external app, wants to **pin it by SHA** the way every
`@rdub` dep is pinned.

- Publish a **dist branch via `npm-dist`** (the standard Git{Hu,La}b Action for
  this), so `@disk-tree/react` is addable as `github:runsascoded/disk-tree#<dist-sha>`
  (or the `pds gh` flow). `<Treemap>` pulls no d3 — only the `./voronoi` subpath
  does, and it's optional — so the dist is dependency-light.
- Treat `<Treemap>`'s `TreemapProps<T>` accessor surface as a **stable public
  API**: it is now file-tree's dependency, not only disk-tree's UI's. The
  generic-over-`T` design (`Treemap.tsx:7-12`) is exactly what makes this safe —
  file-tree instantiates `Treemap<TreeNode>` and never touches our node shape.
  Version it; note breaking accessor changes.

This half is what turns "disk-tree happens to have a treemap" into "disk-tree
*publishes* the treemap file-tree depends on" — the durable, versioned seam.

## What this unlocks: marin-gcs-usage stops forking both libraries

marin's `gcs` branch is **346 commits / +27k lines** diverged from disk-tree
`main`, carrying its own copies of the scan engine *and* both React libs. Once
Halves A–C land, marin becomes a **composer**:

- Its `/files` browser is already `<FileTree>` — unchanged.
- Its treemap dashboard consumes `@disk-tree/react` via the dist pin instead of
  a forked `packages/react/`.
- Its snapshot serving (`/data/[[path]].ts`, `/api/subtree`) becomes a
  `snapshotTreeSource` (B1) or a `diskTreeTreeSource`/`httpTreeSource` (B2),
  and the treemap↔browser link stops being hand-wired — it's the same
  `TreeSource` driving both a filled-in dir listing and the map.
- The Marin-specific parts (attribution, mark & sweep, auth/lens) stay in
  marin, layered as `TreeSource`/`Store` decorators and `<Treemap>` `lens`
  hooks — not as forks of the engine.

Not a goal of *this* spec to do that migration, but it's the payoff that makes
Halves A–C worth prioritizing, and it should shape marin's next branch-parity
pass.

## Division of labor / sequencing

1. **C** (dist branch) — unblocks file-tree from even importing `<Treemap>`.
   Cheapest, do first.
2. **B2 + B3** (CORS + snapshot-id round-trip on the existing API) — lights up a
   live disk-tree server (and marin) as a `TreeSource` with near-zero code.
3. **B1** (published snapshot layout + `snapshots.json`) — the static, no-live-
   Python tier; generalize marin's snapshot job to the parquet contract.
4. **A** (disk-tree `ui/` adopts `<FileTree>`) — reciprocal dogfooding; after
   file-tree's dir-view integration exists to adopt.

file-tree's side starts at its Layer 0 (`walkTreeSource`, no disk-tree at all)
so its interface hardens against the mock fixture before B1's format or C's API
freeze against it.

## Open questions (shared with the file-tree spec)

- **Snapshot identity.** Integer scan id (what B2 proposes) vs ISO time vs a
  content digest. A digest would let file-tree's Layer-1 reader reuse its block
  cache's version keying for free; we emit ids/times. Likely: `id` = our scan
  id (opaque), `time` for display.
- **Partial scans.** Our virtual-root / `scan_status: 'partial'` responses set
  `size: None`. file-tree's `TreeNode.size` is `number | null` to carry that —
  confirm the treemap renders a null-size dir sanely (skip vs zero-area).
- **Reflink / block-size sizing.** disk-tree's 512-byte-block and reflink-aware
  sizing (`specs/reflink-aware-sizing.md`) means `size` can differ from naive
  bytes. Fine — it's still one recursive number per node; just document which
  notion of "size" a snapshot carries so a file-tree tooltip can label it.

## Status

**disk-tree side built** (2026-08-31, disk-tree session). C/B2/B3 already
existed on `main`; B1 shipped. Half A stays open (blocked on file-tree shipping
a consumable `<FileTree>`). Paired with
`~/c/js/file-tree/specs/tree-sources-and-treemap.md`.

## As built (disk-tree side) — what file-tree can rely on today

### C — dist branch now reproducibly pinnable ✅ (fixed 2026-08-31)
`.github/workflows/build-dist.yml` (npm-dist, source-first) publishes the `dist`
branch on every push touching `packages/react/`. **Correction to the earlier
claim here:** the branch existed but was *not* directly pinnable — monorepo
`pkgs` mode nested `@disk-tree/react` under a private `disk-tree-workspace-dist`
root, and a git dep has no subdir selector, so `github:runsascoded/disk-tree#<sha>`
resolved to the workspace, not the package. **Fixed** by teaching npm-dist a new
`package_dir` mode (flatten one subdir package to the dist branch root; additive,
`v1` ≥ `de25b2a`) and repointing our workflow to it. The `dist` branch root now
**is** `@disk-tree/react`.

**Pin recipe (verified — `pnpm add` installs `@disk-tree/react@0.1.0-dist.<sha>`):**
```
pnpm add github:runsascoded/disk-tree#<dist-sha>
```
`<dist-sha>` is a commit on the **`dist`** branch (not `main`) — HEAD is
`bdfe23a` as of writing (rebuilds on every `packages/react/` change; pin a
specific dist SHA for reproducibility). `<Treemap>` pulls no d3 (only the
optional `./voronoi` subpath does). Treat `TreemapProps<T>` as the stable public
API; it's generic-over-`T` so file-tree instantiates `Treemap<TreeNode>` without
touching our node shape.

### B2 — live API is already a `TreeSource` ✅ (zero code)
Audited on `main`, all present and CORS-enabled:
- **CORS**: `CORS(app)` already sends `Access-Control-Allow-Origin` on GETs and
  answers POST preflight (`Access-Control-Allow-Headers/Methods`) for every
  `/api/*` route — verified against a cross-origin `Origin`.
- `GET /api/scan?uri=&scan_id=&depth=` → `{root,children,rows}` of `Row`
  (`scan_id` documented "for time-travel"; `depth` default 2).
- `GET /api/scans/history?uri=` → `[{id,time,size,n_desc,n_children,path,scan_path}]`
  — the `snapshots()` list, keyed by **integer `id`**.
- `GET /api/compare?uri=&scan1=&scan2=` → diff by those ids; `status ∈
  {added,removed,changed,touched,unchanged}`.
So the `diskTreeTreeSource` adapter's id round-trip works as-is: history → ids;
`scan?scan_id=` / `compare?scan1=&scan2=` accept them.

### B3 — scan dispatch ✅
`POST /api/scan/start`, `GET /api/scan/status/<job_id>`, SSE
`/api/scans/progress/stream` — unchanged, CORS-covered.

### B1 — `disk-tree snapshots DEST` (new) ✅
Publishes a static snapshot library: `DEST/snapshots.json` +
`DEST/snapshots/<id>/tree.parquet` per scan. `snapshots.json`:
```json
{ "version": 1, "row_group_size": 65536,
  "columns": ["path","size","mtime","kind","parent","uri","n_desc","n_children","depth","mtime_mean?"],
  "snapshots": [ {"id","path","time","size","n_desc","n_children","tree","diffs?":[{"from","to","blob"}]} ] }
```
Each `tree.parquet` is **self-contained** (chunked/hybrid scans materialized via
`load(follow_refs=True)`, internal `child_scan_id`/`n_files` projected out),
sorted `(depth,path)` in 64K row groups so the depth + path-prefix pushdowns
work on the published copy. Selection: newest scan per path by default (`-a`
all, `-s ID` repeatable), `-d` copies existing diff-index blobs between
consecutive snapshots, `-n` dry-run. DEST is a local dir — sync to a bucket
after (`aws s3 sync` / `gsutil rsync`).

**Two contract nuances for file-tree's snake→camel mapping:**
- **`path` is relative to the scan root** (root row `path == '.'`); the absolute
  path is in **`uri`**. Map `uri`→drill key, `path`→relative key.
- **`n_desc` counts the subtree including self** for imported (bucket) scans —
  root of a 4-descendant tree reports `5`, a leaf file reports `1`. (Local
  `gfind` scans historically used files=0; treat `n_desc` as advisory/"subtree
  node count," not an exact descendant count, until harmonized.)

### A — blocked on file-tree
disk-tree's `ui/` adopting `<FileTree>` waits on file-tree publishing a
consumable `<FileTree>` + `HttpStore` (its side is design-only). Sequenced last,
as the spec says. No disk-tree work until then.
