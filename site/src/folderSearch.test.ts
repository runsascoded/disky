import { describe, expect, it } from 'vitest'
import { fetchMatches, matchEntries, matchLabel, searchScan, type NameMatches } from './folderSearch'

describe('matchLabel', () => {
  it('the name, then where it is', () => {
    expect(matchLabel('grug')).toBe('grug')
    expect(matchLabel('bk/users/grug')).toBe('grug — bk/users/')
  })
  it('a long parent elided from the middle, the name kept whole', () => {
    const got = matchLabel('marin-us-central2/checkpoints/experiments/scaling/run-0042/grug', 48)
    expect(got).toBe('grug — marin-us-central2/checkpoin…ing/run-0042/')
    expect(got.length).toBe(48)
  })
})

describe('matchEntries', () => {
  const size = (b: number) => `${b} B`
  const body: NameMatches = {
    date: 'D', path: '', q: 'grug', total: 3, complete: true,
    items: [
      { path: 'bk/a/grug', kind: 'dir', b: 900, o: 12 },
      { path: 'bk/b/grug.txt', kind: 'file', b: 50, o: 1 },
      { path: 'bk/c/grug', kind: null, b: 7, o: 1 },
    ],
  }
  it('one row per match: name and place, kind and size (a folder\'s objects), its path searchable', () => {
    const picked: string[] = []
    const got = matchEntries(body, { go: m => picked.push(m.path), filter: () => picked.push('filter'), size })
    expect(got.map(e => ({ id: e.id, label: e.label, description: e.description, keywords: e.keywords }))).toEqual([
      { id: 'match:bk/a/grug', label: 'grug — bk/a/', description: 'folder · 900 B · 12 objects', keywords: ['bk/a/grug'] },
      { id: 'match:bk/b/grug.txt', label: 'grug.txt — bk/b/', description: 'file · 50 B', keywords: ['bk/b/grug.txt'] },
      { id: 'match:bk/c/grug', label: 'grug — bk/c/', description: 'folder · 7 B', keywords: ['bk/c/grug'] },
    ])
    for (const e of got) e.handler!()
    expect(picked).toEqual(['bk/a/grug', 'bk/b/grug.txt', 'bk/c/grug'])
  })
  it('an incomplete answer ends with its reason, which filters the map', () => {
    const picked: string[] = []
    const got = matchEntries({ ...body, items: [], total: 90_000, complete: false, reason: 'Too many matches to list.' },
      { go: () => picked.push('go'), filter: () => picked.push('filter'), size })
    expect(got.map(e => [e.id, e.label, e.description])).toEqual([['match:incomplete', 'Too many matches to list.', 'Pick to filter the map instead']])
    got[0].handler!()
    expect(picked).toEqual(['filter'])
  })
})

describe('searchScan', () => {
  const indexed = ['2026-10-19', '2026-10-20', '2026-10-21']
  it('the map\'s scan when the index covers it, or when there is no static index (no list)', () => {
    expect([searchScan('2026-10-21', indexed), searchScan('2026-10-22', null)]).toEqual(['2026-10-21', '2026-10-22'])
  })
  it('a just-published scan: the newest covered one before it', () => {
    expect(searchScan('2026-10-22', indexed)).toBe('2026-10-21')
    expect(searchScan('2026-10-20T1800', indexed)).toBe('2026-10-20')
  })
  it('none while the list loads, before every covered scan, or without a scan', () => {
    expect([searchScan('2026-10-22', 'loading'), searchScan('2026-10-01', indexed), searchScan(null, indexed)]).toEqual([null, null, null])
  })
})

describe('matchEntries scan note', () => {
  it('a result from an older scan than the map\'s says which', () => {
    const body: NameMatches = { date: '2026-10-21', path: '', q: 'crash', total: 1, complete: true, items: [{ path: 'crashes', kind: 'dir', b: 86, o: 8499 }] }
    expect(matchEntries(body, { go: () => {}, filter: () => {}, size: b => `${b} B`, scanNote: 'as of scan 2026-10-21' }).map(e => [e.label, e.description]))
      .toEqual([['crashes', 'folder · 86 B · 8,499 objects · as of scan 2026-10-21']])
  })
})

describe('fetchMatches', () => {
  const body: NameMatches = { date: '2026-10-21', path: '', q: 'crash', total: 1, complete: true, items: [{ path: 'crashes', kind: 'dir', b: 86, o: 8499 }] }
  it('calls the fetcher bare (the global fetch throws "Illegal invocation" as a method) with the scan, query and syntax', async () => {
    const urls: string[] = []
    // As the browser's fetch: any `this` but the global (or none) throws.
    function fetchLike(this: unknown, url: string): Promise<Response> {
      if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation')
      urls.push(url)
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
    }
    expect(await fetchMatches(fetchLike, { date: '2026-10-21', q: 'crash', qs: 'plain', limit: 10 })).toEqual(body)
    expect(urls).toEqual(['/api/name-matches?date=2026-10-21&q=crash&qs=plain&limit=10'])
  })
  it('a union view asks by instant; a refused search is null', async () => {
    const urls: string[] = []
    const refuse = (url: string) => { urls.push(url); return Promise.resolve(new Response('{"error":"Search isn’t available for this scan yet."}', { status: 400 })) }
    expect(await fetchMatches(refuse, { at: '2026-10-22T00:00:00Z', q: 'crash', qs: 'plain', limit: 5 })).toBe(null)
    expect(urls).toEqual(['/api/name-matches?at=2026-10-22T00%3A00%3A00Z&q=crash&qs=plain&limit=5'])
  })
})
