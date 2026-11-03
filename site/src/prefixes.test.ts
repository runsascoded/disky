import { describe, expect, it } from 'vitest'
import { CHUNK, fetchPrefixStats, relAgo, sortPrefixRows } from './prefixes'

describe('relAgo', () => {
  it('each unit once its count is unambiguous', () => {
    const now = 1_000_000_000
    const at = (s: number) => relAgo(now - s, now)
    const D = 86400
    expect([at(5), at(90), at(3 * 3600), at(3 * D), at(13 * D), at(14 * D), at(60 * D), at(61 * D), at(400 * D), at(729 * D), at(730 * D), at(-10)]).toEqual([
      '5s ago', '1m ago', '3h ago', '3d ago', '13d ago', '2w ago', '8w ago', '2mo ago', '13mo ago', '23mo ago', '1y ago', '0s ago',
    ])
  })
})

describe('sortPrefixRows', () => {
  const rows = [
    { name: 'gs://b/a/', staged: 3, stat: { b: 10, o: 1 } },
    { name: 'gs://b/b/', staged: 1 },
    { name: 'gs://b/c/', staged: 2, stat: { b: 30, o: 1, d: 5 } },
    { name: 'gs://b/d/', staged: 2, stat: { b: 10, o: 2 } },
  ]
  const order = (k: string, asc: boolean) => sortPrefixRows(rows, k, asc, r => r.staged).map(r => r.name.slice(7, 8))
  it('by bytes, created, an extra column; rows without the value last in both directions; ties by name', () => {
    expect([order('b', false), order('b', true), order('d', false), order('staged', true), order('name', false)]).toEqual([
      ['c', 'a', 'd', 'b'],
      ['a', 'd', 'c', 'b'],
      ['c', 'a', 'b', 'd'],
      ['b', 'c', 'd', 'a'],
      ['d', 'c', 'b', 'a'],
    ])
  })
})

describe('fetchPrefixStats', () => {
  const keys = Array.from({ length: 2 * CHUNK + 5 }, (_, i) => `gs://b/${String(i).padStart(5, '0')}/`)
  /** A fake `/api/prefixes`: each prefix's stat is its index; `fail(n, body)` decides the n-th call's fate. */
  const server = (fail: (n: number, prefixes: string[]) => 'drop' | 'http' | null = () => null) => {
    const calls: { n: number; first: string; size: number }[] = []
    let open = 0, peak = 0
    const f = (async (_url: string, init: RequestInit) => {
      const { prefixes } = JSON.parse(init.body as string) as { prefixes: string[] }
      const n = calls.length
      calls.push({ n, first: prefixes[0], size: prefixes.length })
      peak = Math.max(peak, ++open)
      await new Promise(r => setTimeout(r, 5))
      open--
      const fate = fail(n, prefixes)
      if (fate === 'drop') throw new TypeError('Load failed')
      if (fate === 'http') return new Response(JSON.stringify({ error: 'boom' }), { status: 503 })
      return new Response(JSON.stringify({ stats: Object.fromEntries(prefixes.map(p => [p, { b: keys.indexOf(p), o: 1 }])) }))
    }) as unknown as typeof fetch
    return { f, calls, peak: () => peak }
  }
  const want = Object.fromEntries(keys.map((p, i) => [p, { b: i, o: 1 }]))

  it('sends every chunk at once and merges them', async () => {
    const s = server()
    expect(await fetchPrefixStats('d', keys, s.f)).toEqual(want)
    expect([s.calls.map(c => [c.first, c.size]), s.peak()]).toEqual([[[keys[0], CHUNK], [keys[CHUNK], CHUNK], [keys[2 * CHUNK], 5]], 3])
  })
  it('a dropped connection resends that chunk alone, once', async () => {
    const s = server(n => (n === 1 ? 'drop' : null))
    expect(await fetchPrefixStats('d', keys, s.f)).toEqual(want)
    expect(s.calls.map(c => [c.n, c.first])).toEqual([[0, keys[0]], [1, keys[CHUNK]], [2, keys[2 * CHUNK]], [3, keys[CHUNK]]])
  })
  it('a chunk dropping twice fails the set; an HTTP error is not resent', async () => {
    const twice = server((_n, p) => (p[0] === keys[CHUNK] ? 'drop' : null))
    await expect(fetchPrefixStats('d', keys, twice.f)).rejects.toEqual(new TypeError('Load failed'))
    expect(twice.calls.filter(c => c.first === keys[CHUNK]).length).toBe(2)
    const http = server(n => (n === 0 ? 'http' : null))
    await expect(fetchPrefixStats('d', keys, http.f)).rejects.toEqual(new Error('boom'))
    expect(http.calls.length).toBe(3)
  })
})
