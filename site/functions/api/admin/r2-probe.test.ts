import { describe, expect, it } from 'vitest'
import type { Env } from '../../_lib/auth'
import { onRequestGet, probeable, probeR2, type ProbeSample } from './r2-probe'

// The admin R2 latency probe over a mocked `INDEX_R2`: which keys it reads, the ranged gets it makes (offsets, lengths,
// one at a time then all at once), and the route's gate and refusals.

const SIZE = 1000
const KEY = 'interval-store/g/served/level.parquet'

/** A bucket holding `KEY` (`SIZE` bytes, byte i = i % 256), logging each call (a get with the gets then in flight). */
function bucket() {
  const bytes = Uint8Array.from({ length: SIZE }, (_, i) => i % 256)
  const log: string[] = []
  let inFlight = 0
  const tick = () => new Promise(r => setTimeout(r, 1))
  const b = {
    head: async (key: string) => { log.push(`head ${key}`); return key === KEY ? { key, size: SIZE } : null },
    get: async (key: string, o: { range: { offset: number; length: number } }) => {
      inFlight++
      log.push(`get ${key} ${o.range.offset}+${o.range.length} (${inFlight})`)
      await tick()
      inFlight--
      if (key !== KEY) return null
      const part = bytes.slice(o.range.offset, o.range.offset + o.range.length)
      return { arrayBuffer: async () => { await tick(); return part.buffer } }
    },
  }
  return { b: b as unknown as R2Bucket, log }
}

/** A source of offsets cycling through `xs`. */
const cycle = (xs: number[]) => { let i = 0; return () => xs[i++ % xs.length] }
/** A sample with its timings replaced (they are the host's), checked first to be ordered and non-negative. */
const shape = (s: ProbeSample) => {
  expect(s.ttfb_ms >= 0 && s.total_ms >= s.ttfb_ms).toBe(true)
  return { offset: s.offset, len: s.len }
}

describe('probeR2', () => {
  it('heads the object, then times n ranged gets one at a time, then n at once at fresh offsets', async () => {
    const { b, log } = bucket()
    const got = (await probeR2(b, KEY, { n: 3, len: 300, random: cycle([0, 0.5, 0.9999, 0.25, 0.75, 0.1]) }))!
    // Offsets: ⌊r · (1000 − 300 + 1)⌋.
    expect([got.key, got.size, got.n, got.len, got.samples.map(shape), got.parallel.samples.map(shape)]).toEqual([
      KEY, SIZE, 3, 300,
      [{ offset: 0, len: 300 }, { offset: 350, len: 300 }, { offset: 700, len: 300 }],
      [{ offset: 175, len: 300 }, { offset: 525, len: 300 }, { offset: 70, len: 300 }],
    ])
    // One at a time (each alone in flight), then all three at once.
    expect(log).toEqual([
      `head ${KEY}`,
      `get ${KEY} 0+300 (1)`, `get ${KEY} 350+300 (1)`, `get ${KEY} 700+300 (1)`,
      `get ${KEY} 175+300 (1)`, `get ${KEY} 525+300 (2)`, `get ${KEY} 70+300 (3)`,
    ])
  })

  it('times from the clock it is given: ttfb to the get resolving, total to its body read', async () => {
    const { b } = bucket()
    let t = 0
    // Every clock read is 10 ms after the last.
    const got = (await probeR2(b, KEY, { n: 1, len: 50, random: () => 0, now: () => (t += 10) }))!
    expect([got.head_ms, got.samples, got.sequential, got.parallel]).toEqual([
      10,
      [{ offset: 0, len: 50, ttfb_ms: 10, total_ms: 20 }],
      { wall_ms: 40, stats: { ttfb: { min: 10, p50: 10, max: 10 }, total: { min: 20, p50: 20, max: 20 } } },
      { wall_ms: 40, samples: [{ offset: 0, len: 50, ttfb_ms: 10, total_ms: 20 }], stats: { ttfb: { min: 10, p50: 10, max: 10 }, total: { min: 20, p50: 20, max: 20 } } },
    ])
  })

  it('a len past the object reads it whole; a missing key is null', async () => {
    const { b, log } = bucket()
    const got = (await probeR2(b, KEY, { n: 1, len: 5000, random: () => 0.7 }))!
    expect([got.len, got.samples.map(shape)]).toEqual([SIZE, [{ offset: 0, len: SIZE }]])
    expect(await probeR2(b, 'interval-store/g/nope.parquet', { n: 1, len: 1 })).toBe(null)
    expect(log.at(-1)).toBe('head interval-store/g/nope.parquet')
  })
})

describe('/api/admin/r2-probe', () => {
  const env = (b: R2Bucket, extra: Partial<Env> = {}) => ({ INDEX_R2: b, ...extra } as unknown as Env)
  const at = (host: string, qs: string, e: Env) => onRequestGet({ request: new Request(`http://${host}/api/admin/r2-probe?${qs}`), env: e } as never)
  const body = async (r: Response) => [r.status, r.headers.get('cache-control'), await r.json()]

  it('reads only interval-store and static-name-index keys', () => {
    const e = env(bucket().b)
    expect(['interval-store/g/served/level.parquet', 'static-names/g1/names/a.parquet', 'snapshots/x/tree.json', 'interval-store/../secret', '', 'interval-storex/a'].map(k => probeable(e, k)))
      .toEqual([true, true, false, false, false, false])
    expect(probeable(env(bucket().b, { INTERVAL_STORE_PREFIX: 'meta/interval-store' }), 'meta/interval-store/g/a.parquet')).toBe(true)
  })

  it('is admin only: an anonymous request is a 401 and reads nothing', async () => {
    const { b, log } = bucket()
    const r = await at('example.test', `object=${KEY}`, env(b))
    expect([r.status, log]).toEqual([401, []])
  })

  it('refuses a key outside the prefixes, a bad n or len, and a missing object', async () => {
    const { b, log } = bucket()
    expect(await Promise.all([
      at('localhost', 'object=snapshots/x/tree.json', env(b)).then(body),
      at('localhost', `object=${KEY}&n=0`, env(b)).then(body),
      at('localhost', `object=${KEY}&len=1.5`, env(b)).then(body),
      at('localhost', 'object=interval-store/g/nope.parquet', env(b)).then(body),
    ])).toEqual([
      [400, 'no-store', { error: 'key must be under interval-store/ or static-names/' }],
      [400, 'no-store', { error: 'n: an integer in [1, 64]' }],
      [400, 'no-store', { error: 'len: an integer in [1, 16777216]' }],
      [404, 'no-store', { error: 'interval-store/g/nope.parquet: not found' }],
    ])
    expect(log).toEqual(['head interval-store/g/nope.parquet'])
  })

  it('answers the probe, uncached, with the colo', async () => {
    const { b, log } = bucket()
    const req = new Request(`http://localhost/api/admin/r2-probe?object=${KEY}&n=2&len=100`)
    Object.defineProperty(req, 'cf', { value: { colo: 'EWR' } })
    const r = await onRequestGet({ request: req, env: env(b) } as never)
    const [status, cc, j] = await body(r) as [number, string, { key: string; size: number; n: number; len: number; colo: string; samples: ProbeSample[]; parallel: { samples: ProbeSample[] } }]
    expect([status, cc, j.key, j.size, j.n, j.len, j.colo, j.samples.map(s => s.len), j.parallel.samples.map(s => s.len), log.length])
      .toEqual([200, 'no-store', KEY, SIZE, 2, 100, 'EWR', [100, 100], [100, 100], 5])
  })
})
