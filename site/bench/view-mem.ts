// Memory of `/api/subtree` renders in Node, against a deployment's live stores: what an isolate holds while
// it answers (and while its `waitUntil` work, e.g. `upgradePartial`'s background full run, goes on), and what it
// keeps after. One process is one isolate: URLs run in turn, so caches carry across them as on the edge.
//
//   esbuild bench/view-mem.ts --bundle --platform=node --format=esm --outfile=OUT.mjs
//   node --expose-gc OUT.mjs --vars vars.json [--var K=V]… [--snap MB] [--out FILE] URL…
//
// URL: a full `https://<host>/api/subtree?…` (the host only keys caches), or `/api/diff?…`: the diff route, gated — on
// `http://localhost/…` the dev identity passes it (`auth.ts` `identify`); elsewhere `$SITE_TOKEN`, if set, as a Bearer. `--vars`: the deployment's `[vars]` as
// JSON; `--var K=V` overrides one (`K=` drops it). Bindings from the environment, read-only:
//   INDEX_R2  ← R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (the S3 API, SigV4)
//   DB        ← CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, D1_DB_ID (the D1 HTTP API; a write is logged, not sent)
//   CACHE_KV, USAGE, `caches.default`: empty (every read misses, every write is dropped), so each render is cold
//   of the colo and KV, and nothing in-process stands in for them.
// Any other environment variable named by `--secret NAME` is passed through (a store's HMAC pair, say).
// `--snap MB`: a heap snapshot (`<out>.heapsnapshot`) the first time the heap passes MB; `--snap-end`: one after the
// last URL and its `waitUntil` work, collected (`<out>.end.heapsnapshot`): what the isolate keeps. `--bodies`: each
// response body as `<out>.<n>.body`.
import { createHash, createHmac } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { writeHeapSnapshot } from 'node:v8'
import { warmSubtree } from '../functions/api/subtree.js'
import { onRequestGet as diffGet } from '../functions/api/diff.js'

const err = (...a: unknown[]) => console.error(...a)

interface Opts { vars: string; set: [string, string][]; secrets: string[]; snapMb?: number; snapEnd?: boolean; bodies?: boolean; out?: string; urls: string[] }

function parseArgs(argv: string[]): Opts {
  const o: Opts = { vars: '', set: [], secrets: [], urls: [] }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const val = () => argv[++i]
    if (a === '--vars') o.vars = val()
    else if (a === '--var') { const kv = val(); const j = kv.indexOf('='); o.set.push([kv.slice(0, j), kv.slice(j + 1)]) }
    else if (a === '--secret') o.secrets.push(val())
    else if (a === '--snap') o.snapMb = Number(val())
    else if (a === '--snap-end') o.snapEnd = true
    else if (a === '--bodies') o.bodies = true
    else if (a === '--out') o.out = val()
    else if (a.startsWith('--')) throw new Error(`unknown option ${a}`)
    else o.urls.push(a)
  }
  if (!o.vars || !o.urls.length) throw new Error('usage: view-mem --vars vars.json [--var K=V]… [--secret NAME]… [--snap MB] [--out FILE] URL…')
  return o
}

const need = (k: string): string => {
  const v = process.env[k]
  if (!v) throw new Error(`view-mem: $${k} is not set`)
  return v
}

// --- R2 over its S3 API ---------------------------------------------------------------------------------------

const sha256 = (s: string) => createHash('sha256').update(s).digest('hex')
const hmac = (k: string | Buffer, s: string) => createHmac('sha256', k).update(s).digest()
const enc = (s: string) => encodeURIComponent(s).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)

function signed(method: string, url: URL, headers: Record<string, string>, id: string, secret: string): Record<string, string> {
  const amz = new Date().toISOString().replace(/[:-]|\.\d{3}/g, '')
  const day = amz.slice(0, 8)
  const h: Record<string, string> = { ...headers, host: url.host, 'x-amz-date': amz, 'x-amz-content-sha256': 'UNSIGNED-PAYLOAD' }
  const names = Object.keys(h).map(k => k.toLowerCase()).sort()
  const lower = Object.fromEntries(Object.entries(h).map(([k, v]) => [k.toLowerCase(), v]))
  const query = [...url.searchParams].sort(([a, x], [b, y]) => (a < b ? -1 : a > b ? 1 : x < y ? -1 : 1)).map(([k, v]) => `${enc(k)}=${enc(v)}`).join('&')
  const canon = [method, url.pathname, query, names.map(k => `${k}:${lower[k].trim()}\n`).join(''), names.join(';'), 'UNSIGNED-PAYLOAD'].join('\n')
  const scope = `${day}/auto/s3/aws4_request`
  const key = hmac(hmac(hmac(hmac(`AWS4${secret}`, day), 'auto'), 's3'), 'aws4_request')
  const sig = createHmac('sha256', key).update(['AWS4-HMAC-SHA256', amz, scope, sha256(canon)].join('\n')).digest('hex')
  return { ...h, authorization: `AWS4-HMAC-SHA256 Credential=${id}/${scope}, SignedHeaders=${names.join(';')}, Signature=${sig}` }
}

type Range = { offset?: number; length?: number; suffix?: number }

function r2(endpoint: string, bucket: string, id: string, secret: string) {
  const urlOf = (key: string, q: Record<string, string> = {}) => {
    const u = new URL(`${endpoint.replace(/\/+$/, '')}/${bucket}/${key.split('/').map(enc).join('/')}`)
    for (const [k, v] of Object.entries(q)) u.searchParams.set(k, v)
    return u
  }
  const call = async (method: string, u: URL, headers: Record<string, string> = {}) => {
    const res = await fetch(u, { method, headers: signed(method, u, headers, id, secret) })
    if (res.status === 404) return null
    if (!res.ok && res.status !== 206) throw new Error(`r2 ${method} ${u.pathname}: ${res.status} ${(await res.text()).slice(0, 200)}`)
    return res
  }
  const object = (key: string, res: Response, range?: Range) => {
    const cr = res.headers.get('content-range')
    const size = cr ? Number(cr.split('/')[1]) : Number(res.headers.get('content-length'))
    const etag = (res.headers.get('etag') ?? '').replace(/"/g, '')
    return {
      key, size, etag, httpEtag: `"${etag}"`, version: etag, uploaded: new Date(res.headers.get('last-modified') ?? 0), range,
      httpMetadata: { contentType: res.headers.get('content-type') ?? undefined }, customMetadata: {},
      body: res.body, bodyUsed: false,
      arrayBuffer: () => res.arrayBuffer(), text: () => res.text(), json: <T>() => res.json() as Promise<T>,
      blob: () => res.blob(),
    }
  }
  return {
    async get(key: string, o?: { range?: Range }) {
      const r = o?.range
      const headers: Record<string, string> = r ? { range: r.suffix != null ? `bytes=-${r.suffix}` : `bytes=${r.offset ?? 0}-${r.length != null ? (r.offset ?? 0) + r.length - 1 : ''}` } : {}
      const res = await call('GET', urlOf(key), headers)
      return res && object(key, res, r)
    },
    async head(key: string) {
      const res = await call('HEAD', urlOf(key))
      if (!res) return null
      const o = object(key, res)
      return { ...o, body: undefined }
    },
    async list(o: { prefix?: string; cursor?: string; delimiter?: string; limit?: number } = {}) {
      const q: Record<string, string> = { 'list-type': '2' }
      if (o.prefix) q.prefix = o.prefix
      if (o.cursor) q['continuation-token'] = o.cursor
      if (o.delimiter) q.delimiter = o.delimiter
      if (o.limit) q['max-keys'] = String(o.limit)
      const u = new URL(`${endpoint.replace(/\/+$/, '')}/${bucket}`)
      for (const [k, v] of Object.entries(q)) u.searchParams.set(k, v)
      const xml = await (await call('GET', u))!.text()
      const tag = (s: string, t: string) => s.match(new RegExp(`<${t}>([^<]*)</${t}>`))?.[1]
      const objects = [...xml.matchAll(/<Contents>([\s\S]*?)<\/Contents>/g)].map(m => ({
        key: tag(m[1], 'Key')!.replace(/&amp;/g, '&'), size: Number(tag(m[1], 'Size')), etag: (tag(m[1], 'ETag') ?? '').replace(/&quot;|"/g, ''),
        uploaded: new Date(tag(m[1], 'LastModified') ?? 0),
      }))
      const delimitedPrefixes = [...xml.matchAll(/<CommonPrefixes><Prefix>([^<]*)<\/Prefix><\/CommonPrefixes>/g)].map(m => m[1])
      const truncated = tag(xml, 'IsTruncated') === 'true'
      return { objects, delimitedPrefixes, truncated, ...(truncated ? { cursor: tag(xml, 'NextContinuationToken') } : {}) }
    },
    async put() { throw new Error('view-mem: R2 is read-only here') },
    async delete() { throw new Error('view-mem: R2 is read-only here') },
  }
}

// --- D1 over its HTTP API, reads only -------------------------------------------------------------------------

const writes: string[] = []

function d1(account: string, token: string, db: string) {
  const run = async (sql: string, params: unknown[]) => {
    if (!/^\s*(select|with|pragma)\b/i.test(sql)) {
      writes.push(sql.replace(/\s+/g, ' ').slice(0, 120))
      return { results: [], success: true, meta: { changes: 0, rows_read: 0, rows_written: 0 } }
    }
    const res = await fetch(`https://api.cloudflare.com/client/v4/accounts/${account}/d1/database/${db}/query`, {
      method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify({ sql, params }),
    })
    const j = await res.json() as { success: boolean; errors?: unknown[]; result?: { results: unknown[]; meta: unknown }[] }
    if (!j.success) throw new Error(`d1: ${JSON.stringify(j.errors).slice(0, 300)} (${sql.slice(0, 80)})`)
    return { results: j.result![0].results, success: true, meta: j.result![0].meta }
  }
  const stmt = (sql: string, params: unknown[] = []) => ({
    bind: (...ps: unknown[]) => stmt(sql, ps),
    all: () => run(sql, params),
    run: () => run(sql, params),
    first: async (col?: string) => {
      const r = (await run(sql, params)).results[0] as Record<string, unknown> | undefined
      return r == null ? null : col ? r[col] : r
    },
    raw: async () => (await run(sql, params)).results.map(r => Object.values(r as Record<string, unknown>)),
    sql, params,
  })
  return {
    prepare: (sql: string) => stmt(sql),
    batch: (ss: ReturnType<typeof stmt>[]) => Promise.all(ss.map(s => run(s.sql, s.params))),
    exec: async (sql: string) => run(sql, []),
  }
}

// --- the empty colo, KV and analytics -------------------------------------------------------------------------

const emptyCache = { match: async () => undefined, put: async () => {}, delete: async () => false }
;(globalThis as unknown as { caches: unknown }).caches = { default: emptyCache, open: async () => emptyCache }
const emptyKv = { get: async () => null, getWithMetadata: async () => ({ value: null, metadata: null }), put: async () => {}, delete: async () => {}, list: async () => ({ keys: [], list_complete: true }) }
const emptyUsage = { writeDataPoint: () => {} }

// --- the sampler ----------------------------------------------------------------------------------------------

const MB = 1 << 20
const mb = (n: number) => Math.round(n / MB * 10) / 10
interface Peak { rss: number; heap: number; external: number; arrayBuffers: number }
const zero = (): Peak => ({ rss: 0, heap: 0, external: 0, arrayBuffers: 0 })
let peak = zero()
let snapped = false

function sample(o: Opts): void {
  const m = process.memoryUsage()
  peak = { rss: Math.max(peak.rss, m.rss), heap: Math.max(peak.heap, m.heapUsed), external: Math.max(peak.external, m.external), arrayBuffers: Math.max(peak.arrayBuffers, m.arrayBuffers) }
  if (o.snapMb != null && !snapped && m.heapUsed > o.snapMb * MB) {
    snapped = true
    const f = writeHeapSnapshot(`${o.out ?? 'view-mem'}.heapsnapshot`)
    err(`view-mem: heap ${mb(m.heapUsed)} MB > ${o.snapMb}: snapshot ${f}`)
  }
}

const gc = (globalThis as unknown as { gc?: () => void }).gc
const settledHeap = () => { gc?.(); gc?.(); return process.memoryUsage().heapUsed }

async function main(): Promise<void> {
  const o = parseArgs(process.argv.slice(2))
  const vars: Record<string, string> = JSON.parse(readFileSync(o.vars, 'utf8'))
  for (const [k, v] of o.set) { if (v === '') delete vars[k]; else vars[k] = v }
  const env = {
    ...vars,
    ...Object.fromEntries(o.secrets.map(k => [k, need(k)])),
    INDEX_R2: r2(need('R2_ENDPOINT'), need('R2_BUCKET'), need('R2_ACCESS_KEY_ID'), need('R2_SECRET_ACCESS_KEY')),
    DB: d1(need('CLOUDFLARE_ACCOUNT_ID'), need('CLOUDFLARE_API_TOKEN'), need('D1_DB_ID')),
    CACHE_KV: emptyKv,
    USAGE: emptyUsage,
  }
  if (!gc) err('view-mem: no gc() (run node with --expose-gc): retained sizes are upper bounds')
  const base = settledHeap()
  const timer = setInterval(() => sample(o), 5)
  const results = []
  for (const url of o.urls) {
    const pending: Promise<unknown>[] = []
    peak = zero()
    const t0 = performance.now()
    let status = 0, bytes = 0, timing = '', md5 = ''
    let error: string | undefined
    try {
      const waitUntil = (p: Promise<unknown>) => { pending.push(p) }
      const res = new URL(url).pathname === '/api/diff'
        ? await diffGet({ request: new Request(url, { headers: { 'user-agent': 'dt-cloud-probe/1', ...(process.env.SITE_TOKEN ? { authorization: `Bearer ${process.env.SITE_TOKEN}` } : {}) } }), env, waitUntil } as never)
        : await warmSubtree(env as never, url, waitUntil)
      status = res.status
      timing = res.headers.get('server-timing') ?? ''
      const body = new Uint8Array(await res.arrayBuffer())
      bytes = body.byteLength
      md5 = createHash('md5').update(body).digest('hex')
      if (o.bodies) writeFileSync(`${o.out ?? 'view-mem'}.${results.length}.body`, body)
    } catch (e) {
      error = String((e as Error).stack ?? e).slice(0, 400)
    }
    const ms = Math.round(performance.now() - t0)
    sample(o)
    const response = peak
    peak = zero()
    const t1 = performance.now()
    await Promise.allSettled(pending)
    sample(o)
    const after = { ms: Math.round(performance.now() - t1), tasks: pending.length, peak: pending.length ? peak : null }
    const retained = settledHeap() - base
    const row = {
      url, status, bytes, md5, ms, error, timing,
      response: Object.fromEntries(Object.entries(response).map(([k, v]) => [k, mb(v)])),
      waitUntil: { ...after, peak: after.peak && Object.fromEntries(Object.entries(after.peak).map(([k, v]) => [k, mb(v)])) },
      retainedHeap: mb(retained),
    }
    results.push(row)
    console.log(JSON.stringify(row))
  }
  clearInterval(timer)
  if (o.snapEnd) { settledHeap(); err(`view-mem: end snapshot ${writeHeapSnapshot(`${o.out ?? 'view-mem'}.end.heapsnapshot`)}`) }
  if (writes.length) err(`view-mem: ${writes.length} D1 writes dropped, e.g. ${writes[0]}`)
  if (o.out) writeFileSync(`${o.out}.json`, JSON.stringify(results, null, 1))
}

main().then(() => process.exit(0), e => { err(e); process.exit(1) })
