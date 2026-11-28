import { beforeEach, describe, expect, it } from 'vitest'
import { answeredBy, backendStore, carriedSearch, cookieValue, forwardBe, headline, pagePin, repinHref, withBe } from './backend'

const O = 'https://disk.example'

describe('the page\'s pin', () => {
  it('is the URL\'s `be`, else the cookie the server set', () => {
    expect([
      pagePin('?be=vm', ''),
      pagePin('?be=idx', 'disky_be=vm'),
      pagePin('', 'a=1; disky_be=vm'),
      pagePin('?be=x', 'disky_be=y'),
      pagePin('', ''),
    ]).toEqual(['vm', 'idx', 'vm', null, null])
    expect([cookieValue('a=1; disky_be=vm', 'disky_be'), cookieValue('xdisky_be=vm', 'disky_be'), cookieValue('', 'disky_be')]).toEqual(['vm', null, null])
  })

  it('a `vm` pin rides in the page\'s URL; `idx` (the default) never', () => {
    expect([
      carriedSearch('', 'vm'),
      carriedSearch('?d=x&f=a%2Cb', 'vm'),
      carriedSearch('?be=idx', 'vm'),
      carriedSearch('?d=x', 'idx'),
      carriedSearch('?d=x', null),
    ]).toEqual(['?be=vm', '?d=x&f=a%2Cb&be=vm', null, null, null])
  })

  it('a `vm` pin rides on same-origin `/api/` requests only', () => {
    expect([
      withBe('/api/subtree?date=d&w=8', 'vm', O),
      withBe(`${O}/api/series`, 'vm', O),
      withBe('/api/subtree?be=idx', 'vm', O),
      withBe('/data/x.json', 'vm', O),
      withBe('https://other.example/api/subtree', 'vm', O),
      withBe('/api/subtree?date=d', 'idx', O),
      withBe('/api/subtree?date=d', null, O),
    ]).toEqual([
      '/api/subtree?date=d&w=8&be=vm',
      `${O}/api/series?be=vm`,
      '/api/subtree?be=idx',
      '/data/x.json',
      'https://other.example/api/subtree',
      '/api/subtree?date=d',
      '/api/subtree?date=d',
    ])
  })

  it('re-pinning is the same page with `be` set', () => {
    expect([repinHref(`${O}/bk?d=x&be=vm`, 'idx'), repinHref(`${O}/`, 'vm')]).toEqual([`${O}/bk?d=x&be=idx`, `${O}/?be=vm`])
  })
})

describe('what answered', () => {
  beforeEach(() => backendStore.reset())

  it('reads `Server-Timing`\'s `be` and the note', () => {
    expect([
      answeredBy('mw;dur=1, be;desc="vm", total;dur=9', null),
      answeredBy('be;desc="idx-fallback"', 'vmd unreachable'),
      answeredBy('be;desc="other"', null),
      answeredBy(null, 'VM mode is off'),
    ]).toEqual([{ be: 'vm' }, { be: 'idx-fallback', note: 'vmd unreachable' }, {}, { note: 'VM mode is off' }])
  })

  it('the badge shows the newest map answer, else the newest of any', () => {
    const vm = { be: 'vm' as const, at: 1 }
    const series = { be: 'idx-fallback' as const, note: 'n', at: 3 }
    const diff = { be: 'idx' as const, at: 2 }
    expect([
      headline({}),
      headline({ '/api/series': series }),
      headline({ '/api/series': series, '/api/subtree': vm }),
      headline({ '/api/series': series, '/api/subtree': vm, '/api/diff': diff }),
    ]).toEqual([null, series, vm, diff])
  })

  it('`forwardBe`: carries a `vm` pin into the URL and onto `/api/` requests, and records each map answer\'s backend', async () => {
    const seen: string[] = []
    const replaced: string[] = []
    const answers: Record<string, Record<string, string>> = {
      '/api/subtree': { 'server-timing': 'be;desc="vm"', 'x-disky-be-note': 'nodes carry no mtime' },
      '/api/series': { 'server-timing': 'mw;dur=2, be;desc="idx-fallback"', 'x-disky-be-note': 'size over time: vmd has no series yet' },
      '/api/largest': { 'server-timing': 'be;desc="idx"' },
    }
    const win = {
      location: { search: '?d=x', origin: O, pathname: '/bk', hash: '#s' },
      document: { cookie: 'disky_be=vm' },
      history: { state: null, replaceState: (_s: unknown, _t: string, url: string) => { replaced.push(url) } },
      fetch: async (i: RequestInfo | URL) => {
        const url = typeof i === 'string' ? i : i instanceof URL ? i.toString() : i.url
        seen.push(url)
        return new Response('{}', { headers: answers[new URL(url, O).pathname] ?? {} })
      },
    }
    forwardBe(win as unknown as Parameters<typeof forwardBe>[0])
    await win.fetch('/api/subtree?date=d')
    await win.fetch(new Request(`${O}/api/series?x=1`))
    await win.fetch('/api/largest')
    await win.fetch('/assets/app.js')
    expect(replaced).toEqual(['/bk?d=x&be=vm#s'])
    expect(seen).toEqual(['/api/subtree?date=d&be=vm', `${O}/api/series?x=1&be=vm`, '/api/largest?be=vm', '/assets/app.js'])
    const { pin, seen: by } = backendStore.get()
    expect({ pin, by: Object.fromEntries(Object.entries(by).map(([k, { be, note }]) => [k, { be, note }])) }).toEqual({
      pin: 'vm',
      by: {
        '/api/subtree': { be: 'vm', note: 'nodes carry no mtime' },
        '/api/series': { be: 'idx-fallback', note: 'size over time: vmd has no series yet' },
      },
    })
  })

  it('`forwardBe` with no pin: nothing carried, nothing added, nothing recorded without a `be`', async () => {
    const seen: string[] = []
    const replaced: string[] = []
    const win = {
      location: { search: '', origin: O, pathname: '/', hash: '' },
      document: { cookie: '' },
      history: { state: null, replaceState: (_s: unknown, _t: string, url: string) => { replaced.push(url) } },
      fetch: async (i: RequestInfo | URL) => { seen.push(String(i)); return new Response('{}') },
    }
    forwardBe(win as unknown as Parameters<typeof forwardBe>[0])
    await win.fetch('/api/subtree?date=d')
    expect([replaced, seen, backendStore.get()]).toEqual([[], ['/api/subtree?date=d'], { pin: null, seen: {} }])
  })
})
