# Half A — disk-tree's `ui/` adopts `<FileTree>`

The reciprocal-dogfood half of `specs/done/file-tree-integration.md`: disk-tree's `ui/`
mounts `@rdub/file-tree`'s `<FileTree>` for its browse surfaces so both libraries
compose on one page — `<FileTree>` renders *"what's in this prefix"*,
`@disk-tree/react` renders *"how big is everything under it, over time."* Now
**unblocked**: file-tree shipped a consumable `<FileTree>` (`walkTreeSource` +
list↔treemap toggle, live at file-tree.rbw.sh), pinnable by git SHA.

Status: **planned** (design only; no code yet). Sequence after B/C (done).

## What we're adopting (verified against `~/c/js/file-tree`)

- `<FileTree store routeBase treeSource? treemapRenderer? …>` (`@rdub/file-tree/react`).
  `store: Store` backs the listing + file viewers; optional `treeSource: TreeSource`
  fills the recursive dir sizes (the `—` cells); `treemapRenderer` is our own
  `@disk-tree/react` `<Treemap>` (already wired on their side).
- `Store` = `list(prefix,{cursor,limit}) → {entries:Entry[],cursor?}` +
  `get(path,range?) → GetResult` + `capabilities`. `Entry = {key,kind,size?}`.
  `HttpStore(apiBase)` hits `/list?prefix=&cursor=&limit=` and `/get?path=&Range`.
- `TreeSource` = `children({path,depth}) → {node,children}` (`TreeNode =
  {path,name?,kind,size,nChildren?,nDesc?,mtimeMean?}`) + `snapshots()` + `diff()`
  + `scan()`/`scanStatus()` + `capabilities:{history,diff,scan,lazy}`.
- **No `diskTreeTreeSource` / `httpTreeSource` exists in file-tree yet** — the
  adapter is ours to write (upstreamable to file-tree later).
- `@rdub/file-tree` publishes a **package-at-root `dist` branch** (`r/dist` →
  name `@rdub/file-tree`; single-package repo `runsascoded/file-tree`, so no
  `package_dir` fix needed — unlike `@disk-tree/react`). Git-pinnable directly.

## Surfaces: adopt vs. keep

| `ui/` surface | today | Half A |
| --- | --- | --- |
| scan child listing (`ScanDetails` `DetailsTable`) | hand-rolled `<table>` | **`<FileTree>`** |
| `DataTable.tsx` | generic hand-rolled table | **retire** (folds into FileTree) |
| S3/GCS bucket *browse* (`/s3,/gcs,/r2,/ssh/*` → `ScanDetails`) | same table | **`<FileTree>`** |
| aggregate viz (Treemap / Scatter / Histograms / Voronoi) | `@disk-tree/react` | **keep** (the half FileTree doesn't do) |
| scans list `/` (`ScanList`), `/recent` | custom | **keep** (a scans list, not a store) |
| bucket list `/s3` (`S3BucketList` top level) | custom summary | **keep** the summary; browse-into adopts FileTree |
| compare `/compare/*` (`CompareView`) | custom diff map | **keep** for now (diff ≠ listing; revisit via `TreeSource.diff`) |
| app shell: routing, Rescan+SSE progress, delete, library switcher, units | custom | **keep** |

## Backing `<FileTree>`: reuse the live API (recommended)

Two adapters over the **existing** Flask API — **no new backend for the common
case** (reuses Half B2, already CORS-clean):

1. **`diskTreeTreeSource({baseUrl})`** → the recursive-size seam:
   `children` ← `GET /api/scan?uri=&depth=N` (snake→camel `Row`→`TreeNode`);
   `snapshots` ← `/api/scans/history`; `diff` ← `/api/compare`;
   `scan`/`scanStatus` ← `/api/scan/start` + `/status/<id>` (+ SSE progress);
   `capabilities = {history:true, diff:true, scan:true, lazy:true}`.
2. **`diskTreeStore({baseUrl})`** → `Store` for the listing + viewers:
   `list(prefix)` ← `/api/scan?uri=&depth=1` (immediate children → `Entry[]`);
   `get(path,range)` ← file bytes.

**The one real backend gap is `Store.get`.** `/api/file/preview` exists but is
preview-shaped (size-capped, no Range); FileTree's parquet/csv/sqlite viewers
need **Range reads**. So add a small **`GET /api/files/get?path=&Range`** (local:
`send_file` with Range; S3/GCS: presign or proxy) — the only new endpoint.
`list` needs none (map `/api/scan`). Prefer this over a full `HttpStore` +
`/api/files/list` reimplementation, since `/api/scan` already does fresher-child
patching and depth pushdown for free.

## Dependency wiring

- Pin `@rdub/file-tree` by git SHA: `"@rdub/file-tree": "github:runsascoded/file-tree#<dist-sha>"`
  in `ui/package.json` (dist branch is package-at-root — confirmed).
- Add to root `.pds.json` (`localPath: ../../js/file-tree`, `github:
  runsascoded/file-tree`, `distBranch: dist`) for tandem dev; `pds l file-tree`
  during co-dev, `pds gh file-tree` to pin a build.
- `@disk-tree/react` stays `workspace:*`. Both libs then live on the same page.

## Phased sequence (smallest-first)

0. **Adapters + pin.** Add `diskTreeTreeSource` + `diskTreeStore` (in `ui/src/`,
   or a shared module), pin `@rdub/file-tree`, unit-test the snake→camel mapping
   against a mock/live server. Add `/api/files/get` (Range).
1. **One read-only route as proof.** Mount `<FileTree store treeSource
   treemapRenderer>` on `/gcs/*` (bucket browse, no delete/select) behind a flag,
   beside the existing table; confirm dir sizes + the list↔treemap toggle render
   from disk-tree data in our own shell.
2. **Port disk-tree affordances.** Multi-select + bulk delete + per-row rescan +
   fresher-child indicators onto FileTree via `renderCell`/actions — or decide a
   thin custom action layer wraps it. Extend to `/file/*` (local, with delete).
3. **Retire the hand-rolled table.** Delete `DetailsTable`/`DataTable`; unify all
   browse routes (`/file,/s3,/gcs,/r2,/ssh/*`) on `<FileTree>`.
4. **Compare.** Leave `CompareView` custom, or add a FileTree diff-listing mode
   over `TreeSource.diff` (defer — diff is disk-tree-specific).

## Open questions / risks

- **Multi-select + bulk delete + per-row rescan (biggest risk).** These are
  disk-tree-specific; FileTree's `DirListing` extensibility is `renderCell`
  (cell content) — selection state + bulk actions may not be first-class.
  Verify FileTree's action/selection API before Phase 2; may need a custom layer
  (which erodes the "delete the hand-rolled code" payoff).
- **Path conventions.** disk-tree routes are absolute URIs (`/file/Users/ryan`,
  `gcs://b1`, via `uriToPath`); FileTree is `routeBase` + root-relative splat +
  `rootPrefix`. Published snapshot `path` is already root-relative (root `.`)
  with `uri` absolute — aligns with FileTree — but the router glue needs care.
- **`Store.get` for viewers.** Range support + large files (parquet/sqlite
  viewers stream); S3/GCS objects need presign/proxy, not local `send_file`.
- **Fresher-child patching** stays server-side in `/api/scan`, so the adapter
  inherits it — no client work. Good.
- **Scan dispatch parity.** FileTree's `capabilities.scan` + `TreeSource.scan()`
  should drive the *same* `/api/scan/start` + SSE the current Rescan button uses
  — reconcile so there's one dispatch path, not two.
