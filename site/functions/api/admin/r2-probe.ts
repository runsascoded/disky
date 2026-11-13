/** R2 read latency, as this colo's Worker sees it through the `INDEX_R2` binding. Admin only; never cached.
 *
 *   GET /api/admin/r2-probe?object=<R2 key>[&n=<count>][&len=<bytes>]
 *
 *  `object` (not `key`, which the gate reads as a share link): an object under the interval store (`ivPrefix`) or the static name index (`static-names/`); anything else
 *  is a 400. The object is `head`ed for its size, then `n` ranged gets of `len` bytes at random offsets within it are
 *  timed, first one after another (each a cold-ish round), then `n` more at once (fresh offsets). Per get: `ttfb_ms`,
 *  until the get resolves (its headers), and `total_ms`, until its body is read (`arrayBuffer`). Both marks are taken
 *  around I/O, so the Worker's clock (which advances only across I/O) measures them. Answers
 *  `{ key, size, n, len, colo, head_ms, samples, sequential: { wall_ms, stats }, parallel: { wall_ms, samples, stats } }`. */
import { type Ctx, json, requireAdmin } from '../../_lib/auth.js'
import { ivPrefix } from '../../_lib/index.js'

const NO_STORE = { 'cache-control': 'no-store' }

/** Gets per phase at most, and the bytes one reads at most. */
export const PROBE_MAX_N = 64
export const PROBE_MAX_LEN = 16 << 20
const DEFAULT_N = 10
const DEFAULT_LEN = 64 << 10

/** One timed ranged get. */
export interface ProbeSample { offset: number; len: number; ttfb_ms: number; total_ms: number }
export interface ProbeStats { ttfb: { min: number; p50: number; max: number }; total: { min: number; p50: number; max: number } }
export interface ProbeResult {
  key: string
  size: number
  n: number
  len: number
  head_ms: number
  samples: ProbeSample[]
  sequential: { wall_ms: number; stats: ProbeStats }
  parallel: { wall_ms: number; samples: ProbeSample[]; stats: ProbeStats }
}

/** Whether `key` is one the probe may read: an interval-store or static-name-index object. */
export const probeable = (env: Ctx['env'], key: string): boolean =>
  !!key && !key.includes('..') && (key.startsWith(`${ivPrefix(env)}/`) || key.startsWith('static-names/'))

const ms = (x: number): number => Math.round(x * 10) / 10

function stats(ss: ProbeSample[]): ProbeStats {
  const of = (f: (s: ProbeSample) => number) => {
    const xs = ss.map(f).sort((a, b) => a - b)
    return { min: xs[0], p50: xs[(xs.length - 1) >> 1], max: xs[xs.length - 1] }
  }
  return { ttfb: of(s => s.ttfb_ms), total: of(s => s.total_ms) }
}

/** The probe itself, over any bucket, clock and source of offsets (the tests'); null when `key` isn't there. */
export async function probeR2(bucket: R2Bucket, key: string, o: { n: number; len: number; random?: () => number; now?: () => number }): Promise<ProbeResult | null> {
  const random = o.random ?? Math.random
  const now = o.now ?? (() => performance.now())
  const t0 = now()
  const head = await bucket.head(key)
  const head_ms = ms(now() - t0)
  if (!head) return null
  const size = head.size
  const len = Math.min(o.len, size)
  const offset = () => Math.floor(random() * (size - len + 1))
  const sample = async (off: number): Promise<ProbeSample> => {
    const t = now()
    const got = await bucket.get(key, { range: { offset: off, length: len } })
    const ttfb = now() - t
    if (!got) throw new Error(`${key}: gone mid-probe`)
    const body = await got.arrayBuffer()
    return { offset: off, len: body.byteLength, ttfb_ms: ms(ttfb), total_ms: ms(now() - t) }
  }
  const samples: ProbeSample[] = []
  const ts = now()
  for (let i = 0; i < o.n; i++) samples.push(await sample(offset()))
  const seqWall = ms(now() - ts)
  const offs = Array.from({ length: o.n }, offset)
  const tp = now()
  const par = await Promise.all(offs.map(sample))
  const parWall = ms(now() - tp)
  return {
    key, size, n: o.n, len, head_ms, samples,
    sequential: { wall_ms: seqWall, stats: stats(samples) },
    parallel: { wall_ms: parWall, samples: par, stats: stats(par) },
  }
}

/** `sp`'s integer `name` in `[1, max]`, its default when absent; null when malformed. */
function count(sp: URLSearchParams, name: string, dflt: number, max: number): number | null {
  const v = sp.get(name)
  if (v == null || v === '') return dflt
  const x = Number(v)
  return Number.isInteger(x) && x >= 1 && x <= max ? x : null
}

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const bucket = ctx.env.INDEX_R2
  if (!bucket) return json({ error: 'no INDEX_R2 binding' }, 503, NO_STORE)
  const sp = new URL(ctx.request.url).searchParams
  const key = sp.get('object') ?? ''
  if (!probeable(ctx.env, key)) return json({ error: `key must be under ${ivPrefix(ctx.env)}/ or static-names/` }, 400, NO_STORE)
  const n = count(sp, 'n', DEFAULT_N, PROBE_MAX_N)
  if (n == null) return json({ error: `n: an integer in [1, ${PROBE_MAX_N}]` }, 400, NO_STORE)
  const len = count(sp, 'len', DEFAULT_LEN, PROBE_MAX_LEN)
  if (len == null) return json({ error: `len: an integer in [1, ${PROBE_MAX_LEN}]` }, 400, NO_STORE)
  const got = await probeR2(bucket, key, { n, len })
  if (!got) return json({ error: `${key}: not found` }, 404, NO_STORE)
  const colo = (ctx.request as Request & { cf?: { colo?: string } }).cf?.colo ?? null
  return json({ ...got, colo }, 200, NO_STORE)
}
