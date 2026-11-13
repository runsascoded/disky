import { describe, expect, it } from 'vitest'
import { API_SHAPE, API_SHAPE_HEADER, reloadKey, shapeSkewed, watchApiShape } from './apiShape'

describe('API shape skew', () => {
  it('`shapeSkewed`: only a present header naming another shape', () => {
    expect([null, '', String(API_SHAPE), ` ${API_SHAPE} `, '1', '3'].map(h => shapeSkewed(h))).toEqual([false, false, false, false, true, true])
  })

  const run = async (headers: (string | null)[], stored: Record<string, string> = {}) => {
    const reloads: string[] = []
    const store = { getItem: (k: string) => stored[k] ?? null, setItem: (k: string, v: string) => { stored[k] = v } }
    let i = 0
    const win = {
      fetch: (async () => {
        const h = headers[i++]
        return new Response('{}', { headers: h == null ? {} : { [API_SHAPE_HEADER]: h } })
      }) as typeof fetch,
      location: { reload: () => { reloads.push(`reload@${i}`) } },
    }
    watchApiShape(win, store)
    for (const _ of headers) await win.fetch('/api/x')
    return { reloads, stored }
  }

  it('reloads once when the server names another shape, never for its own or none', async () => {
    expect(await run([String(API_SHAPE), null, '9', '9', String(API_SHAPE)])).toEqual({
      reloads: ['reload@3'],
      stored: { [reloadKey('9')]: '1' },
    })
  })

  it('a reload that meets the same other shape again does not loop', async () => {
    expect((await run(['9'], { [reloadKey('9')]: '1' })).reloads).toEqual([])
  })

  it('no storage: no reload', async () => {
    const reloads: string[] = []
    const win = { fetch: (async () => new Response('{}', { headers: { [API_SHAPE_HEADER]: '9' } })) as typeof fetch, location: { reload: () => { reloads.push('x') } } }
    watchApiShape(win, null)
    await win.fetch('/api/x')
    expect(reloads).toEqual([])
  })
})
