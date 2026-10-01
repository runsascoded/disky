# Scan browser: file-tree over the GCS listing/snapshot parquet

Add a browsable file/prefix explorer to the gcs.oa.dev site, backed by `@rdub/file-tree` over our GCS data bucket — so anyone (OA-internal for now) can navigate `listing/<date>/<bucket>/` and preview the raw per-object parquet shards (and the snapshot JSONs) with zero local tooling. Consumes the new file-tree `gcs` store (`~/c/js/file-tree/specs/gcs-store.md`).

## Ask / context

Our daily job writes complete, dated, per-object listings:
```
gs://oa-gcs-usage-dvx/listing/<date>/<bucket>/shard-NN-NNNN.parquet   (+ _SUCCESS.json)
  schema: bucket, name, size_bytes, created, storage_class_id         (~250k rows/shard)
gs://oa-gcs-usage-dvx/snapshots/<date>/{tree,meta,age}.json           (aggregated viz artifacts)
```
Today these are readable only via `gs://` + a parquet tool, and only by principals with Viewer+ on `oa-internal-450019`. A file-tree browser turns that into point-and-click: dir navigation + a parquet viewer (paginates by row group, surfaces rg metadata) + JSON/markdown/CSV renderers. This is the "make the raw scans actually consumable" streamlining that should land **before** we announce the scanning infra.

**Verified (2026-08-05):** GCS's S3-compatible XML API returns ListObjectsV2-shape XML and honors range GETs (`206` + `Content-Range`) against our bucket — so file-tree's store machinery works unchanged; see the file-tree spec.

## Architecture: proxy-worker, not direct-to-browser

The browser must not hold GCS credentials, and the bucket shouldn't be made public. So run the `gcs` store **server-side** behind file-tree's `createHandlers`, and hit it from the browser via `HttpStore`:

```
<FileTree store={HttpStore('/v1/files', { presign:? })} />   ─HTTP→   Pages Function /v1/files/*
                                                                        └ createHandlers(GcsStore(...), { basePath:'/v1/files' })
                                                                          └ GCS XML API (server-side creds)
```

- **Deploy as a Cloudflare Pages Function** (`site/functions/v1/files/[[path]].ts`), not a standalone Worker — it then sits on the **same origin** as the site (no CORS) and **inherits the same CF Access gating** (OA-only) automatically. Matches the existing CF Pages + CF Access deployment (see `ops/specs/gcs-oa-dev-site.md`, memory `cf-oa-account`).
- **Auth v1 = GCS HMAC key** stored as a CF secret (simplest in the Workers runtime — SigV4 via file-tree's bundled `aws4fetch`, zero token-minting). Create a read-only HMAC key scoped to a viewer SA on `oa-gcs-usage-dvx`. (v2 option: file-tree's bearer mode + an SA-JWT→OAuth `getToken` in the Function, avoiding HMAC keys — defer unless we want to drop the HMAC credential.)
- **`prefixes` allow-list:** `['listing/', 'snapshots/']` — the Function exposes only these, never the whole bucket.

## Components

### 1. file-tree dependency via `pds` (tandem dev)

The `gcs` store doesn't exist in a published file-tree yet, so develop both sides together (same pattern as `scrns`):
- `pds init ~/c/js/file-tree` in `site/`, then `pds l file-tree` → point at the local build; iterate on `GcsStore` (in the file-tree clone / a file-tree session) and this integration with hot-reload.
- Ship order: file-tree `gcs` store lands + publishes a dist branch → `pds gh file-tree` (pin by SHA) here → later `pds n` once on npm.

### 2. Pages Function (`site/functions/v1/files/[[path]].ts`)

```ts
import { GcsStore } from '@rdub/file-tree/stores/gcs'
import { createHandlers } from '@rdub/file-tree/server'

export const onRequest: PagesFunction<Env> = async (ctx) => {
  const store = GcsStore({
    bucket: 'oa-gcs-usage-dvx',
    prefixes: ['listing/', 'snapshots/'],
    accessKeyId: ctx.env.GCS_HMAC_KEY_ID,
    secretAccessKey: ctx.env.GCS_HMAC_SECRET,
  })
  const handlers = createHandlers(store, { basePath: '/v1/files' })
  return (await handlers.handle(ctx.request)) ?? new Response('not found', { status: 404 })
}
```
Secrets: `wrangler pages secret put GCS_HMAC_KEY_ID` / `GCS_HMAC_SECRET` (or the Pages dashboard). `nginx.conf`/Dockerfile path (the container variant of the site) needs the equivalent if that deployment is kept.

### 3. Site route + nav (`site/src/`)

The site is currently a single-page treemap (no router). Add `react-router-dom` and a `/files/*` route:
```tsx
<Route path="/files/*" element={
  <FileTree store={HttpStore('/v1/files')} routeBase="/files" rootPrefix="listing/" />
} />
```
- Header nav link "Browse scans" → `/files/listing/`.
- Renderers to include: `parquet` (the shards), `json` (tree/meta/age), `markdown`/`csv` as they come free.

### 4. Nice-to-haves that make it actually pleasant

- **`listing/latest/` pointer** (the streamlining item): publish a stable `latest` prefix/manifest from the daily job so `/files/listing/latest/` is always the newest complete scan. Cheap job addition.
- **Deep-link from the treemap:** clicking a bucket/prefix in the existing treemap opens that prefix in the browser (`/files/listing/latest/<bucket>/<prefix>`) — cross-nav between the aggregate view and the raw shards. (Mirrors the disk-tree deep-link idea.)
- **Column-aware parquet default view** (upstream to file-tree if worth it): default sort/columns for the listing schema.

## Access model

- **v1: OA-internal only** — the Function inherits the site's CF Access app; the bucket stays private. This matches the current gating and needs no new decision.
- **Non-OA (Stanford/marin collaborators): out of scope here** — same open access question as pointing people at gcs.oa.dev. Options when we get there: a public read-only prefix + bucket CORS (then a *direct* `GcsStore` in-browser, no proxy), signed URLs, or an R2 mirror. Ties into the multi-store R2 direction (`disk-tree-engine-and-multistore.md`).

## Phases

1. **file-tree `gcs` store** lands (its spec) — blocks everything; `pds l` for tandem dev in the meantime.
2. **Pages Function** + HMAC secret; verify `/v1/files/list` + range `get` against the live bucket behind Access.
3. **Site route + nav**; parquet + JSON preview working end-to-end (CIC on gcs.oa.dev).
4. **`latest` pointer** in the daily job + treemap deep-links.
5. (later) non-OA access; R2 mirror.

## Out of scope

- Aggregation/diff/time-series/treemap-of-deltas — that's the disk-tree engine (`disk-tree-engine-and-multistore.md`); this is the *raw* browser, complementary to it (file-tree = per-object shards; disk-tree = aggregated views).
- Writing to the bucket; anything beyond read + preview.

## References

- file-tree store spec: `~/c/js/file-tree/specs/gcs-store.md` (the `GcsStore` this consumes).
- Verified XML/range probe: `tmp/gcs-xml-probe.sh` (ListObjectsV2 shape + `206`/`Content-Range` against `oa-gcs-usage-dvx`).
- Deployment/gating: `ops/specs/gcs-oa-dev-site.md`, memory `cf-oa-account`.
- Raw scan layout: `gs://oa-gcs-usage-dvx/listing/<date>/<bucket>/` (schema `bucket,name,size_bytes,created,storage_class_id`).
