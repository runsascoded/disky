import { describe, expect, it } from 'vitest'
import { forwardPs, pagePs, withPs } from './pathStoreParam'

const O = 'https://disk.example'

describe('withPs', () => {
  it('adds the page’s `ps` to a same-origin `/api/` request only', () => {
    expect([
      withPs('/api/subtree?date=d&w=8', 'iv', O),
      withPs(`${O}/api/series`, 'scan', O),
      withPs('/api/subtree?ps=scan', 'iv', O),
      withPs('/data/laptop/x.json', 'iv', O),
      withPs('https://other.example/api/subtree', 'iv', O),
      withPs('/api/subtree?date=d', null, O),
    ]).toEqual([
      '/api/subtree?date=d&w=8&ps=iv',
      `${O}/api/series?ps=scan`,
      '/api/subtree?ps=scan',
      '/data/laptop/x.json',
      'https://other.example/api/subtree',
      '/api/subtree?date=d',
    ])
  })
})

describe('pagePs', () => {
  it('reads `ps`, empty as none', () => {
    expect([pagePs('?ps=iv&x=1'), pagePs('?ps='), pagePs('')]).toEqual(['iv', null, null])
  })
})

describe('forwardPs', () => {
  const win = (search: string) => {
    const seen: string[] = []
    const w = { location: { search, origin: O }, fetch: async (i: RequestInfo | URL) => { seen.push(typeof i === 'string' ? i : i instanceof URL ? i.toString() : i.url); return new Response() } }
    return { w: w as unknown as Pick<Window, 'fetch' | 'location'>, seen }
  }
  it('forwards through strings, URLs and Requests', async () => {
    const { w, seen } = win('?ps=iv')
    forwardPs(w)
    await w.fetch('/api/subtree?date=d')
    await w.fetch(new URL(`${O}/api/diff`))
    await w.fetch(new Request(`${O}/api/series?x=1`))
    await w.fetch('/assets/app.js')
    expect(seen).toEqual(['/api/subtree?date=d&ps=iv', `${O}/api/diff?ps=iv`, `${O}/api/series?x=1&ps=iv`, '/assets/app.js'])
  })
  it('leaves fetch alone without one', () => {
    const { w } = win('?x=1')
    const f = w.fetch
    forwardPs(w)
    expect(w.fetch).toBe(f)
  })
})
