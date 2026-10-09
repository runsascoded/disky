import { describe, expect, it, vi } from 'vitest'

// The stamp itself (wasm-backed) is out of scope: any call to it fails the test.
const stampPage = vi.fn(async () => { throw new Error('stamped a non-HTML response') })
vi.mock('./_lib/og/serve.js', () => ({ stampPage }))
const { onRequest } = await import('./_middleware')

// `/llms.txt` (the deployment's `API.md`, emitted by the `llms-txt` build
// plugin) is public: it is excluded from the Functions, so no gate runs for
// it, and the one site-wide middleware (the OG stamp) never gates either.

const load = async <T>(mod: string): Promise<T> => (await import(/* @vite-ignore */ mod)) as T
const readPublic = async (name: string): Promise<string> => {
  const { readFileSync } = await load<{ readFileSync(file: URL, enc: 'utf8'): string }>('node:fs')
  return readFileSync(new URL(`../public/${name}`, (import.meta as ImportMeta & { url: string }).url), 'utf8')
}

describe('static assets that never reach a Function', () => {
  it('`_routes.json` excludes the bundle, the fonts, the cards and `/llms.txt`', async () => {
    expect(JSON.parse(await readPublic('_routes.json'))).toEqual({
      version: 1,
      include: ['/*'],
      exclude: ['/assets/*', '/_fonts/*', '/og.jpg', '/gcs.png', '/favicon.svg', '/llms.txt'],
    })
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
