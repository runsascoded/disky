import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { clearIsolate } from './shared'
import { fixture } from './testStore'
import { sqliteD1 } from './testD1'
import { buildView } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

async function storeEnv(): Promise<Env> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as {
    existsSync(path: string): boolean
    readFileSync(path: string): Uint8Array
  }
  const bucket = {
    get: async (key: string, o?: { range?: { offset: number; length: number } }) => {
      const file = fixture(`iv/${key}`)
      if (!fs.existsSync(file)) return null
      const buf = fs.readFileSync(file)
      const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
      const part = buf.slice(offset, offset + length)
      return {
        size: buf.byteLength,
        arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
        json: async () => JSON.parse(new TextDecoder().decode(part)),
      }
    },
  } as unknown as R2Bucket
  const { db } = await sqliteD1()
  return { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: bucket } as Env
}

const VIEW = { date: '2026-08-03', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, ivPlanRows: 0 }

describe('a read of row groups the colo holds', () => {
  beforeEach(() => {
    clearIsolate()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {} }
  })

  it('takes each colo copy as it arrives: a copy doesn\'t wait in memory for the slowest', async () => {
    const env = await storeEnv()
    // First pass: the decoded copies, as the colo would hold them.
    const copies = new Map<string, ArrayBuffer>()
    const c = (globalThis as unknown as { caches: { default: unknown } }).caches
    c.default = {
      match: async () => undefined,
      put: async (k: Request, r: Response) => { if (k.url.includes('rg=')) copies.set(k.url, await r.arrayBuffer()) },
    }
    await buildView(env, { ...VIEW, path: '' })
    await sleep(50)
    // A file with several copies: one read's. The same file's copies come through one `mapLimit`.
    const files = new Map<string, string[]>()
    for (const k of copies.keys()) files.set(k.split('?')[0], [...(files.get(k.split('?')[0]) ?? []), k])
    const keys = [...files.values()].sort((a, b) => b.length - a.length)[0]
    expect(keys.length).toBeGreaterThan(1)
    clearIsolate()
    // Second pass: one copy never comes until the test says so; every other has been handed over by then.
    let release!: () => void
    const held1 = new Promise<void>(r => { release = r })
    let returned = 0
    let taken = 0
    c.default = {
      match: async (k: Request) => {
        const buf = copies.get(k.url)
        if (!buf) return undefined
        if (k.url === keys[keys.length - 1]) await held1
        returned++
        return new Response(buf)
      },
      put: async () => {},
    }
    const trace = (name: string) => { if (name === 'gcolo') taken++ }
    const read = buildView(env, { ...VIEW, path: '', trace })
    await sleep(100)
    // Taken as they came, with the slow one still out.
    expect([returned > 0, taken]).toEqual([true, returned])
    release()
    const view = await read
    expect(view.tree.bytes).toBeGreaterThan(0)
    expect(taken).toBe(returned)
  })
})
