import { describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { fixture } from './testStore'
import { sqliteD1 } from './testD1'
import { buildView, type ViewNode } from './view'

// Rows come back in reverse of the store's order: what a generation laid out differently reads (gcs `g3.c001` vs
// `g3`, 2026-11-11), so the view's order can't lean on it.
vi.mock('./index', async importOriginal => {
  const actual = await importOriginal<typeof import('./index')>()
  return {
    ...actual,
    readRects: async (...args: Parameters<typeof actual.readRects>) => (await actual.readRects(...args)).reverse(),
    readSizeRects: async (...args: Parameters<typeof actual.readSizeRects>) => (await actual.readSizeRects(...args)).reverse(),
  }
})

async function storeEnv(): Promise<Env> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { existsSync(path: string): boolean; readFileSync(path: string): Uint8Array }
  const bucket = {
    get: async (key: string, o?: { range?: { offset: number; length: number } }) => {
      const file = fixture(`iv/${key}`)
      if (!fs.existsSync(file)) return null
      const buf = fs.readFileSync(file)
      const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
      const part = buf.slice(offset, offset + length)
      return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
    },
  } as unknown as R2Bucket
  const { db } = await sqliteD1()
  return { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: bucket } as Env
}

/** Each node's named children, as (bytes, name) pairs in the order the view gives them. */
const orders = (n: ViewNode, out: [number, string][][] = []): [number, string][][] => {
  const kids = (n.children ?? []).filter(c => c.name !== '(other)')
  if (kids.length > 1) out.push(kids.map(c => [c.bytes, c.name]))
  for (const c of n.children ?? []) orders(c, out)
  return out
}
const canonical = (xs: [number, string][]) => [...xs].sort((x, y) => y[0] - x[0] || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0))

describe("a view's children", () => {
  it('come by bytes, then name: equal-sized siblings never in the order the store read them', async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    const view = await buildView(await storeEnv(), { date: '2026-08-03', path: '', w: 64, h: 48, minArea: 1, atten: 2, ivPlanRows: 0 })
    const got = orders(view.tree)
    // the fixture's root holds three 10-byte siblings
    expect(got[0]).toEqual([[30, 'f'], [20, 'e'], [10, 'b1'], [10, 'd.json'], [10, 'e']])
    expect(got).toEqual(got.map(canonical))
  })
})
