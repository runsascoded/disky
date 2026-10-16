import { describe, expect, it, vi } from 'vitest'
import { excludedFromFunctions, FUNCTIONS_EXCLUDED } from './_lib/routes'
import { BUILD, buildId } from './_lib/build'
import { cacheKeyFor, cacheMatch, cacheStore } from './_lib/edgeCache'

// The stamp itself (wasm-backed) is out of scope: any call to it fails the test.
const stampPage = vi.fn(async () => { throw new Error('stamped a non-HTML response') })
const anonymousPage = vi.fn(async (): Promise<Response | null> => null)
vi.mock('./_lib/og/serve.js', () => ({ stampPage, anonymousPage }))
const { onRequest } = await import('./_middleware')
const run = (url: string, upstream: Response, method = 'GET') =>
  onRequest({ request: new Request(url, { method }), env: {} as never, next: async () => upstream })
const html = () => new Response('<!doctype html>\n', { headers: { 'content-type': 'text/html; charset=utf-8' } })

// `/llms.txt` (the deployment's `API.md`, emitted by the `llms-txt` build
// plugin) is public: it is excluded from the Functions, so no gate runs for
// it, and the one site-wide middleware (the OG stamp) never gates either.

const load = async <T>(mod: string): Promise<T> => (await import(/* @vite-ignore */ mod)) as T
const readPublic = async (name: string): Promise<string> => {
  const { readFileSync } = await load<{ readFileSync(file: URL, enc: 'utf8'): string }>('node:fs')
  return readFileSync(new URL(`../public/${name}`, (import.meta as ImportMeta & { url: string }).url), 'utf8')
}

describe('static assets that never reach a Function', () => {
  it('`_routes.json` excludes the fonts, the union marks’ icons, the cards and `/llms.txt` (not the bundle: see `assetMiss`); `FUNCTIONS_EXCLUDED` mirrors it', async () => {
    const routes = JSON.parse(await readPublic('_routes.json')) as { exclude: string[] }
    expect(routes).toEqual({
      version: 1,
      include: ['/*'],
      exclude: ['/_fonts/*', '/icons/*', '/og.jpg', '/gcs.png', '/favicon.svg', '/llms.txt'],
    })
    expect(FUNCTIONS_EXCLUDED).toEqual(routes.exclude)
  })
  it('an icon path under `/icons/` is a static asset; one elsewhere would reach the catch-all (the SPA’s `index.html`)', () => {
    expect(['/icons/meta.svg', '/icons/a/b.png', '/favicon.svg', '/meta.svg', '/iconsx/meta.svg', '/api/icon'].map(p => excludedFromFunctions(p))).toEqual([
      true, true, true, false, false, false,
    ])
  })
  it('`_redirects` is the SPA fallback alone (Pages drops a `404` rule, and a redirect would shadow real assets)', async () => {
    const rules = (await readPublic('_redirects')).split('\n').filter(l => l.trim() && !l.startsWith('#'))
    expect(rules).toEqual(['/*  /index.html  200'])
  })
  it('`/llms.txt` is served as UTF-8 text', async () => {
    expect((await readPublic('_headers')).split('\n')).toEqual(['/llms.txt', '  Content-Type: text/plain; charset=utf-8', ''])
  })
  it('the middleware passes a non-HTML asset through untouched, signed out', async () => {
    const asset = new Response('# guide\n', { headers: { 'content-type': 'text/plain; charset=utf-8' } })
    const res = await onRequest({ request: new Request('https://gcs.example.test/llms.txt'), env: {} as never, next: async () => asset })
    expect([res === asset, stampPage.mock.calls.length]).toEqual([true, 0])
  })
})

describe('an `/assets/` miss is a no-store 404, never the SPA shell', () => {
  it('the SPA fallback\'s HTML for a bundle URL becomes a plain-text 404 nothing caches', async () => {
    for (const method of ['GET', 'HEAD']) {
      const res = await run('https://gcs.example.test/assets/index-NOPE.js', html(), method)
      expect([res.status, [...res.headers], await res.text()]).toEqual([
        404,
        [['cache-control', 'no-store'], ['content-type', 'text/plain; charset=utf-8'], ['x-disky-build', buildId()]],
        'asset not found\n',
      ])
    }
    expect(stampPage.mock.calls.length).toBe(0)
  })
  it('a real bundle file passes through as Pages served it (status, long cache, body)', async () => {
    const js = new Response('export {}\n', { headers: { 'content-type': 'application/javascript', 'cache-control': 'public, max-age=14400, must-revalidate' } })
    const res = await run('https://gcs.example.test/assets/index-DvNEkll9.js', js)
    expect(res).toBe(js)
    const notModified = new Response(null, { status: 304, headers: { etag: 'W/"x"' } })
    expect(await run('https://gcs.example.test/assets/index-DvNEkll9.js', notModified)).toBe(notModified)
  })
  it('HTML outside `/assets/` (a deep SPA route) still goes to the stamp', async () => {
    stampPage.mockClear()
    const page = html()
    const res = await run('https://gcs.example.test/marin-us-central1/x', page)
    expect([res, stampPage.mock.calls.length]).toEqual([page, 1])
  })
})

describe('`index.html`\'s boot watchdog', () => {
  it('is a classic inline script ahead of the entry module, so its capture listener sees the entry\'s load error', async () => {
    const { readFileSync } = await load<{ readFileSync(file: URL, enc: 'utf8'): string }>('node:fs')
    const page = readFileSync(new URL('../index.html', (import.meta as ImportMeta & { url: string }).url), 'utf8')
    expect(page.match(/<script[^>]*>/g)).toEqual(['<script>', '<script type="module" src="/src/main.tsx">'])
  })
})

describe('the middleware keeps the isolate alive for the colo puts', () => {
  it('hands `waitUntil` exactly one promise per request, which settles', async () => {
    const kept: Promise<unknown>[] = []
    const res = await onRequest({ request: new Request('https://gcs.example.test/api/x'), env: {} as never, next: async () => new Response('{}', { headers: { 'content-type': 'application/json' } }), waitUntil: p => { kept.push(p) } })
    expect([res.status, kept.length, await Promise.all(kept)]).toEqual([200, 1, [undefined]])
  })
})

it('private anonymous unfurls bypass route handlers that read protected metadata', async () => {
  const generic = html()
  anonymousPage.mockResolvedValueOnce(generic)
  const next = vi.fn(async () => new Response('protected staging counts'))
  const result = await onRequest({ request: new Request('https://cw.example/staged'), env: {} as never, next })
  expect([result === generic, next.mock.calls]).toEqual([true, []])
})

describe('every response names the build serving it (`x-disky-build: <sha> <time>`)', () => {
  it('the header is `<sha> <iso time>`, from the generated build module', () => {
    expect([buildId(), /^[0-9a-f]{10}(-dirty)?$|^unknown$/.test(BUILD.sha), /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(BUILD.time)]).toEqual([`${BUILD.sha} ${BUILD.time}`, true, true])
  })
  it('a cached endpoint\'s miss and its colo hit — the stored copy written by an older build — both carry the current build', async () => {
    const held = new Map<string, Response>()
    const g = globalThis as unknown as { caches?: unknown }
    const was = g.caches
    g.caches = { default: { match: async (k: Request) => held.get(k.url)?.clone(), put: async (k: Request, r: Response) => { held.set(k.url, r) } } }
    try {
      const key = cacheKeyFor('subtree', '', 'k')
      const req = () => new Request('https://gcs.example.test/api/subtree?date=x')
      const miss = await onRequest({ request: req(), env: {} as never, next: () => cacheStore({}, key, '{"a":1}') })
      const stale = new Response('{"a":1}', { headers: { 'content-type': 'application/json', 'cache-control': 'public, s-maxage=60', 'x-disky-build': 'old 2026-01-01T00:00:00Z' } })
      held.set(key.url, stale)
      const hit = await onRequest({ request: req(), env: {} as never, next: async () => (await cacheMatch({}, key))! })
      expect([miss, hit].map(r => [r.headers.get('x-cache'), r.headers.get('x-disky-build')])).toEqual([['miss', buildId()], ['hit', buildId()]])
    } finally {
      g.caches = was
    }
  })
  it('on any response: a refusal, an asset miss, one whose headers are immutable (copied)', async () => {
    const got = await Promise.all([
      run('https://gcs.example.test/api/x', new Response('{}', { status: 400 })),
      run('https://gcs.example.test/assets/x.js', html()),
      run('https://gcs.example.test/api/y', Response.redirect('https://gcs.example.test/z', 302)),
    ])
    expect(got.map(r => [r.status, r.headers.get('x-disky-build')])).toEqual([[400, buildId()], [404, buildId()], [302, buildId()]])
  })
})
