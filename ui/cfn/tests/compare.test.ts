/** On-the-fly `/api/compare` (spec `specs/public-diff-demo.md` Phase 3): reads
 *  the depth-≤2 slice of two scans and diffs them at the edge — no persisted
 *  index (response carries no `index` field, so the client treats it as final).
 *  Fixtures: two scans of `/cmp` (`fixtures-compare/gen.py`) — `grew` grows,
 *  `shrank` shrinks, `gone` removed, `new` added, `same` unchanged. */
import { beforeEach, describe, expect, it } from 'vitest'
import { join } from 'node:path'
import { dirBucket } from './fakeR2'
import { resetScanCache } from '../manifests'
import type { Env } from '../env'
import { onRequestGet as compare } from '../../functions/api/compare'
import { onRequestGet as history } from '../../functions/api/scans/history'

const PREFIX = 'scans/'
const env: Env = { SCANS: dirBucket(join(__dirname, 'fixtures-compare'), PREFIX), SCANS_PREFIX: PREFIX }
const T1 = '2026-01-02T03:04:05'
const T2 = '2026-01-03T03:04:05'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const call = async (fn: (ctx: any) => Promise<Response>, path: string) => {
  const res = await fn({ request: new Request(`http://x${path}`), env, params: {} })
  return { status: res.status, body: await res.json() }
}

beforeEach(resetScanCache)

describe('/api/compare', () => {
  it('orders the two scans of a path as ids 1 (older) and 2 (newer)', async () => {
    const { body } = await call(history, '/api/scans/history?uri=/cmp')
    expect(body.map((s: { id: number; time: string }) => [s.id, s.time]).sort((a: [number, string], b: [number, string]) => a[0] - b[0]))
      .toEqual([[1, T1], [2, T2]])
  })

  it('flat: one-level child diff for the table (unchanged dropped, best-first)', async () => {
    const { status, body } = await call(compare, '/api/compare?uri=/cmp&scan1=1&scan2=2')
    expect(status).toBe(200)
    expect(body.uri).toBe('/cmp')
    expect(body.scan1).toEqual({ id: 1, time: T1, size: 730, n_desc: 8, scan_path: '/cmp' })
    expect(body.scan2).toEqual({ id: 2, time: T2, size: 1180, n_desc: 8, scan_path: '/cmp' })
    expect(body.summary).toEqual({ added: 1, removed: 1, changed: 2, unchanged: 1, total_delta: 450 })
    expect(body.rows.map((r: Record<string, unknown>) => [r.path, r.status, r.size, r.size_old ?? null, r.size_delta, r.n_desc_delta ?? null])).toEqual([
      ['new', 'added', 600, null, 600, 1],
      ['grew', 'changed', 400, 100, 300, 0],
      ['gone', 'removed', null, 250, -250, -1],
      ['shrank', 'changed', 100, 300, -200, 0],
    ])
    // No persisted index → no `index` field → the client does not poll.
    expect('index' in body).toBe(false)
  })

  it('recursive: depth-≤2 frontier for the treemap, changed dirs expanded one level', async () => {
    const { status, body } = await call(compare, '/api/compare?recursive=1&uri=/cmp&scan1=1&scan2=2')
    expect(status).toBe(200)
    expect(body.recursive).toBe(true)
    expect(body.rows.map((r: Record<string, unknown>) => [r.path, r.depth, r.status, r.size_delta, r.expanded, r.pruned])).toEqual([
      ['new', 1, 'added', 600, false, false],
      ['grew', 1, 'changed', 300, true, false],
      ['grew/x', 2, 'changed', 300, false, false],
      ['gone', 1, 'removed', -250, false, false],
      ['shrank', 1, 'changed', -200, true, false],
      ['shrank/y', 2, 'changed', -200, false, false],
    ])
    // `same` (unchanged) is labeled grey context under the compared uri, and the
    // added/removed subtrees (`new`, `gone`) stay single rows (not descended).
    expect(body.unchanged.top.map((r: { path: string }) => r.path)).toEqual(['same'])
    expect(body.unchanged.rest).toEqual({})
    expect(body.summary).toEqual({ added: 1, removed: 1, changed: 2, unchanged: 1, total_delta: 450, expansions: 2, truncated: false })
  })

  it('drills into an added subtree (uri present in only the newer scan)', async () => {
    const { status, body } = await call(compare, '/api/compare?uri=/cmp/new&scan1=1&scan2=2')
    expect(status).toBe(200)
    expect(body.scan1.size).toBe(null) // /cmp/new absent from scan 1
    expect(body.scan2.size).toBe(600)
    expect(body.rows.map((r: Record<string, unknown>) => [r.path, r.status, r.size_delta])).toEqual([['w', 'added', 600]])
  })

  it('errors like Flask on a bad request / missing scan', async () => {
    expect(await call(compare, '/api/compare?uri=/cmp&scan1=1')).toEqual({ status: 400, body: { error: 'scan1 and scan2 are required' } })
    expect(await call(compare, '/api/compare?uri=/cmp&scan1=1&scan2=9')).toEqual({ status: 404, body: { error: 'scan not found', scan1: 1, scan2: 9 } })
  })
})
