// CF Pages Function: the snapshot data the client reads beside the views.
//
// The app fetches /data/<store>/scans.json (the scan picker: D1's scans), and
// /data/<store>/<scan>/{age,meta,lifecycle}.json and /data/rules.json (the
// files the scan job publishes under `snapshots/`). Read live, so a new scan
// surfaces with no site redeploy, through the served seam (`servedStore`):
// `INDEX_R2` where the job copies them to R2 (`SERVED_R2`), else the store
// seam's bucket. Same-origin behind the app's auth (no CORS). CF Pages serves
// static assets before Functions, so this only works because public/data/ is
// no longer shipped (see the build).
import { withStore } from '../_lib/stores.js'
import { snapshotsPrefix } from '../_lib/shared.js'
import { type Env, requireViewer } from '../_lib/auth.js'
import { genInstant, pathScanGens, pathScans, servedStore, storeReady } from '../_lib/index.js'
import { isScanId } from '../../src/scanSlug.js'

// Scan ids are `YYYY-MM-DD`, optionally sub-daily as `YYYY-MM-DDTHHMM` (no
// colon: it keeps the id safe as an object-key path segment). GCS publishes one
// scan a day so its ids stay date-only; CoreWeave runs ad hoc, several a day.
// `private`: these responses are now auth-gated — browser caching only.
const CACHE = 'private, max-age=300' // daily cadence — ≤5min staleness is fine

export const onRequest = async (ctx0: { request: Request; env: Env }): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  if (!ctx.env.SERVED_R2 && !storeReady(ctx.env)) {
    return new Response('data proxy not configured (missing index store creds)', { status: 503 })
  }
  // Every payload is members-only: the edge (CF Access) session is the identity
  // (a public deploy grants the base viewer scope via PUBLIC_READ).
  const rel = new URL(ctx.request.url).pathname.replace(/^\/data\//, '')
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  // The served seam (`_lib/index.ts` `servedStore`): `INDEX_R2` under `SERVED_R2`, else the store
  // seam (GCS by default, any S3-compatible store once `STORE_*` is set) — same data, same keys.
  // A secondary store (`store=`) reads only its own snapshot dir
  // (`snapshots/<SNAPSHOTS_SUBDIR>/`); the primary, all of `snapshots/`.
  const own = ctx.env.STORE_KEY ? snapshotsPrefix(ctx.env) : 'snapshots/'
  const notFound = () => new Response('not found', { status: 404 })
  const store = servedStore(ctx.env, [own]) // allow-list: only the published snapshots

  try {
    // `<store>/scans.json` → the store's scans, newest-first: D1's, alone. Every view reads the path
    // index, so a scan is servable exactly when D1 points at one (`pathScans`); no store LIST, which
    // sat on every first paint's critical path. The URL names the env's own snapshot dir
    // (`snapshots/<SNAPSHOTS_SUBDIR>/`, the client's `store.base`); any other is a 404.
    const scansM = /^(?:([a-z0-9-]+)\/)?scans\.json$/.exec(rel)
    if (scansM) {
      const prefix = scansM[1] ? `snapshots/${scansM[1]}/` : 'snapshots/'
      if (prefix !== snapshotsPrefix(ctx.env)) return notFound()
      if (!ctx.env.DB) return new Response('scan list not configured (DB)', { status: 503 })
      const dates = (await pathScans(ctx.env, false)).results.map(r => r.date).filter(d => isScanId(d))
      dates.sort().reverse()
      return new Response(JSON.stringify(dates), {
        headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
      })
    }
    // `<store>/scan-times.json` → `{ <scan id>: <instant> }` for the date-only
    // ids whose index generation is a capture stamp: when in that day the scan
    // ran, for the picker. Ids without one are left out.
    if (/^(?:[a-z0-9-]+\/)?scan-times\.json$/.test(rel)) {
      const times: Record<string, string> = {}
      if (ctx.env.DB) {
        for (const r of (await pathScanGens(ctx.env)).results) {
          const at = genInstant(r.gen)
          if (at && !r.date.includes('T')) times[r.date] = at
        }
      }
      return new Response(JSON.stringify(times), {
        headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
      })
    }
    // rules.json → snapshots/rules.json ; else /data/<date>/<file> → snapshots/<date>/<file>
    const key = rel === 'rules.json' ? 'snapshots/rules.json' : `snapshots/${rel}`
    if (!key.startsWith(own)) return notFound()
    const { bytes, contentType } = await store.get(key)
    // A date-only scan id can't say *when* in that day the scan ran, and the
    // daily GCS job publishes ids without a time — but the object itself knows.
    // Splice its lastModified into meta.json so the UI can show the real
    // publish time; sizes move enough over 24h that "which 8/17?" matters.
    if (key.endsWith('/meta.json')) {
      const meta = JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>
      // Newer scans carry `published` as data (the job stamps it; older metas
      // were back-stamped by `dt-cloud stamp-published`). Only a meta without
      // it falls back to the object's mtime — which is the *copy* time, not the
      // publish time, once the served copy lives in R2.
      if (typeof meta.published === 'string') {
        return new Response(bytes, {
          headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
        })
      }
      const dir = key.slice(0, key.lastIndexOf('/') + 1)
      const published = (await store.list(dir)).entries.find(e => e.key === key)?.lastModified
      if (published) {
        return new Response(JSON.stringify({ ...meta, published }), {
          headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
        })
      }
    }
    return new Response(bytes, {
      headers: { 'content-type': contentType ?? 'application/json; charset=utf-8', 'cache-control': CACHE },
    })
  } catch {
    return new Response('not found', { status: 404 })
  }
}
