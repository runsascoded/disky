import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { type Level, mergeLevels } from '../_lib/diffLevel'
import { sqliteD1 } from '../_lib/testD1'
import { fixtureR2 } from '../_lib/testR2'
import { readJson } from '../_lib/testStore'
import { onRequestGet as level } from './diff/level'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `/api/diff/level` on a union view: each member answers at its own scans, the root is their answers merged. The
// deployment: the primary is `iv-churn/interval-store-b1/` (the fixture's files under `b1`), a secondary `b2` is
// `interval-store-b2/` (under `b2`), its union `all` the main view. The oracle: the whole store (`interval-store/`),
// which holds both roots.

type Body = { from: string; to: string; k: number; m: string; levels: Level[]; union?: { view: string; sources: { store: string; from: string | null; to: string | null; change: string }[] } }

const IV = { PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g' }
const B2 = { vars: { ...IV, ROOT_LABEL: 'b2', INTERVAL_STORE_PREFIX: 'interval-store-b2' } }

let union: Env
let whole: Env
let pairs: [string, string][]

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const r2 = fixtureR2('iv-churn', [])
  union = { DB: (await sqliteD1()).db, ROOT_LABEL: 'all', ...IV, INTERVAL_STORE_PREFIX: 'interval-store-b1', INDEX_R2: r2, STORES_JSON: JSON.stringify({ b2: B2, all: { union: ['primary', 'b2'], main: true } }) } as Env
  whole = { DB: (await sqliteD1()).db, ROOT_LABEL: 'all', ...IV, INDEX_R2: r2 } as Env
  const cases = await readJson<{ from: string; to: string; path: string }[]>('iv-churn/expected.json')
  pairs = [...new Set(cases.filter(c => c.path === '').map(c => `${c.from}|${c.to}`))].map(p => p.split('|') as [string, string])
})

const get = async (env: Env, qs: string): Promise<Body> => {
  const r = await level({ request: new Request(`http://localhost/api/diff/level?${qs}`), env })
  const text = await r.text()
  expect([r.status, r.status === 200 ? '' : text]).toEqual([200, ''])
  return JSON.parse(text) as Body
}
const byPath = (b: Body) => Object.fromEntries(b.levels.map(l => [l.path, l]))

describe('union level diffs', () => {
  it('the union\'s root and its directories answer as one store holding every member\'s roots', async () => {
    expect(pairs.length).toBe(10)
    for (const [from, to] of pairs) {
      for (const k of [4, 32]) {
        const qs = `from=${from}&to=${to}&k=${k}&p=&p=b1&p=b1/x&p=b2`
        const [u, w] = await Promise.all([get(union, qs), get(whole, qs)])
        expect(byPath(u)).toEqual(byPath(w))
        expect(u.union).toEqual({ view: 'all', sources: [{ store: 'primary', from, to, change: 'changed' }, { store: 'b2', from, to, change: 'changed' }] })
      }
    }
  })

  it('the union\'s root is its members\' own roots (`store=primary`, `store=b2`) merged, and follows a single change', async () => {
    let followed = 0
    for (const [from, to] of pairs) {
      const qs = `from=${from}&to=${to}&k=32&p=`
      const [u, p, b] = await Promise.all([get(union, qs), get(union, `${qs}&store=primary`), get(union, `${qs}&store=b2`)])
      const root = byPath(u)['']
      expect(root).toEqual(mergeLevels('', [byPath(p)[''], byPath(b)['']], 32, 'churn'))
      expect([root.a, root.b, root.oa, root.ob, root.churn]).toEqual(['a', 'b', 'oa', 'ob', 'churn'].map(f => byPath(p)[''][f as 'a'] + byPath(b)[''][f as 'a']))
      if (root.next) {
        followed++
        expect(byPath(u)[root.next]).toEqual(byPath(await get(whole, qs))[root.next])
      }
    }
    expect(followed).toBeGreaterThan(0)
  })

  it('refuses what a single store refuses', async () => {
    const r = await level({ request: new Request(`http://localhost/api/diff/level?from=${pairs[0][1]}&to=${pairs[0][0]}&p=`), env: union })
    expect([r.status, await r.text()]).toEqual([400, 'from must precede to'])
    const nope = await level({ request: new Request(`http://localhost/api/diff/level?from=${pairs[0][0]}&to=${pairs[0][1]}&p=nope`), env: union })
    expect([nope.status, await nope.text()]).toEqual([404, 'path not found in either scan'])
  })
})
