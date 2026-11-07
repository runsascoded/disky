/**
 * GET /api/dups[?date=<scan>|d=<slug>][&tier=tree|moved][&min=<bytes>][&buckets=<b1>[,<b2>]][&q=<text>]
 *              [&suspect=1][&nested=1][&n=<1..500>]
 *   → { date, summary, groups: [{ group, tier, rel, kind, size, n_files, copies, reclaimable, n_buckets, suspect,
 *        items: [{ path, bucket, size, n_files, mtime, created, last_read, cb, keep, nested, check, check_n, check_eq,
 *                  staged }] }], more, rows, row_groups }
 *
 * The scan's duplicate-candidate report (specs/dups.md §2): `summary` is its `dups.json` as the `dups` step wrote it;
 * `groups` the first `n` (default 100) matching groups of its `dups.parquet`, largest reclaimable first. Filters:
 * `tier`; `min` reclaimable bytes; `buckets` (copies in each; a bucket twice = ≥2 copies in it); `q` (a substring of
 * some copy's path, case-insensitive). Suspect groups (≥ the step's copy threshold: mostly same-size collisions) and
 * groups whose every copy is nested under another group's are left out unless `suspect=1` / `nested=1`. Each copy
 * carries `staged`: the open plan an item of which covers it (live, from D1), else null.
 *
 * No `date` / `d`: the newest indexed scan with a report. A scan without one is a 404. `DUPS=1` gates the route;
 * viewer scope reads it, a path-restricted link can't (its paths span the store). The report part is immutable per
 * (scan, generation, filters) and edge-cached; `staged` is applied per request.
 */
import { type AsyncBuffer } from 'hyparquet'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type Ctx, type Env, json, requireViewer } from '../_lib/auth.js'
import { restrictedForbidden, restrictionFor } from '../_lib/restrict.js'
import { indexDir, storeCreds, storeReady, storeTarget } from '../_lib/index.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore } from '../_lib/edgeCache.js'
import { scanArg } from '../_lib/scanArg.js'
import { type DupFilter, type DupGroup, type OpenItems, readDups, stagedPlan, type Tier, TIERS } from '../_lib/dups.js'

export const DUPS_DEFAULT = 100
export const DUPS_MAX = 500
/** How many of the newest indexed scans a dateless request tries for a report. */
const LATEST_TRIES = 10

const reportStore = (env: Env, dir: string) => S3Store({ ...storeTarget(env), prefixes: [`${dir}/`], ...storeCreds(env) })

/** The report's summary, or null when the scan has none. */
async function summaryAt(env: Env, dir: string): Promise<Record<string, unknown> | null> {
  try {
    const { bytes } = await reportStore(env, dir).get(`${dir}/dups.json`)
    return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>
  } catch (e) {
    if ((e as Error).name === 'NotFoundError') return null
    throw e
  }
}

/** `dups.parquet` as hyparquet's `AsyncBuffer`: its size once, then range reads. */
async function parquetAt(env: Env, dir: string): Promise<AsyncBuffer> {
  const store = reportStore(env, dir)
  const key = `${dir}/dups.parquet`
  const head = await store.get(key, { offset: 0, length: 1 })
  if (!head.totalSize) throw new Error(`${key}: size unknown (no Content-Range)`)
  return {
    byteLength: head.totalSize,
    slice: async (s: number, e?: number) => {
      const end = e ?? head.totalSize!
      const r = await store.get(key, { offset: s, length: end - s })
      return r.bytes.buffer.slice(r.bytes.byteOffset, r.bytes.byteOffset + r.bytes.byteLength) as ArrayBuffer
    },
  }
}

/** The open plans' items, for `staged` (none where the plans tables aren't there). */
async function openItems(env: Env): Promise<OpenItems | null> {
  if (!env.DB) return null
  try {
    const r = await env.DB.prepare("SELECT i.prefix AS key, i.kind AS kind, i.plan_id AS plan FROM plan_items i JOIN plans p ON p.id = i.plan_id WHERE p.state = 'open'")
      .all<{ key: string; kind: string | null; plan: number }>()
    const items: OpenItems = { prefixes: new Map(), objects: new Map() }
    for (const x of r.results) (x.kind === 'object' ? items.objects : items.prefixes).set(x.key, x.plan)
    return items
  } catch {
    return null
  }
}

/** The request's filters, or a 400's message. */
export function parseFilter(sp: URLSearchParams): DupFilter | string {
  const f: DupFilter = {}
  const tier = sp.get('tier')
  if (tier) {
    if (!TIERS.includes(tier as Tier)) return `bad tier (want ${TIERS.join('|')})`
    f.tier = tier as Tier
  }
  const min = sp.get('min')
  if (min) {
    if (!/^\d+$/.test(min)) return 'bad min (bytes)'
    f.min = Number(min)
  }
  const buckets = sp.get('buckets')
  if (buckets) {
    f.buckets = buckets.split(',').map(s => s.trim()).filter(Boolean)
    if (f.buckets.length > 2) return 'bad buckets (one, or a pair)'
  }
  const q = sp.get('q')?.trim()
  if (q) f.q = q
  if (sp.get('suspect') === '1') f.suspect = true
  if (sp.get('nested') === '1') f.nested = true
  return f
}

/** The cache key's filter part: every filter, in a fixed order. */
const filterKey = (f: DupFilter, n: number): string =>
  new URLSearchParams([
    ['tier', f.tier ?? ''], ['min', String(f.min ?? '')], ['buckets', (f.buckets ?? []).join(',')], ['q', f.q ?? ''],
    ['suspect', f.suspect ? '1' : ''], ['nested', f.nested ? '1' : ''], ['n', String(n)],
  ]).toString()

/** The newest indexed scans' `path` pointers (date, generation dir). */
async function newestScans(env: Env): Promise<{ date: string; dir: string }[]> {
  const r = await env.DB!.prepare("SELECT date, dir FROM index_schema WHERE variant = 'path' AND dir IS NOT NULL ORDER BY date DESC LIMIT ?")
    .bind(LATEST_TRIES).all<{ date: string; dir: string }>()
  return r.results
}

export const onRequestGet = async (ctx: Ctx & { waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const { env, request } = ctx
  if (env.DUPS !== '1') return json({ error: 'not found' }, 404)
  if (!storeReady(env) || !env.DB) return json({ error: 'dups report not configured (index store, D1)' }, 503)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (restrictionFor(gated, env)) return restrictedForbidden()
  const sp = new URL(request.url).searchParams
  if (sp.has('store')) return json({ error: 'the dups report is the primary store\'s' }, 400)
  const f = parseFilter(sp)
  if (typeof f === 'string') return json({ error: f }, 400)
  const nRaw = sp.get('n')
  const n = nRaw == null ? DUPS_DEFAULT : Number(nRaw)
  if (!Number.isInteger(n) || n < 1 || n > DUPS_MAX) return json({ error: `bad n (want 1..${DUPS_MAX})` }, 400)
  try {
    let date: string
    let dir: string | null
    let summary: Record<string, unknown> | null = null
    if (sp.has('date') || sp.has('d')) {
      const scan = await scanArg(env, sp)
      if (scan instanceof Response) return scan
      date = scan
      dir = await indexDir(env, date)
      if (dir) summary = await summaryAt(env, dir)
      if (!summary) return json({ error: `no dups report for ${date}` }, 404)
    } else {
      const found = await (async () => {
        for (const s of await newestScans(env)) {
          const got = await summaryAt(env, s.dir)
          if (got) return { ...s, summary: got }
        }
        return null
      })()
      if (!found) return json({ error: 'no dups report for any recent scan' }, 404)
      ;({ date, dir, summary } = found)
    }
    const key = cacheKeyFor('dups', cacheEnvTag(env, request), `${date}/${encodeURIComponent(dir!)}?${filterKey(f, n)}`)
    const hit = await cacheMatch(env, key)
    let body: { groups: DupGroup[]; more: boolean; rows: number; row_groups: number }
    if (hit) body = await hit.json()
    else {
      const page = await readDups(await parquetAt(env, dir!), f, n)
      body = { groups: page.groups, more: page.more, rows: page.rows, row_groups: page.rowGroups }
      await cacheStore(env, key, JSON.stringify(body), {}, ctx.waitUntil?.bind(ctx))
    }
    const items = await openItems(env)
    const scheme = env.STORE_SCHEME ?? 's3://'
    if (items) for (const g of body.groups) for (const c of g.items) c.staged = stagedPlan(c.path, g.kind, scheme, items)
    return json({ date, summary, ...body }, 200, { 'cache-control': 'private, no-store' })
  } catch (e) {
    return json({ error: `dups failed: ${(e as Error).message}` }, 503)
  }
}
