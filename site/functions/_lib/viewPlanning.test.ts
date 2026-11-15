import { beforeEach, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import type { Span } from './index'
import { fixture, ivClassBytes } from './testStore'
import { sqliteD1 } from './testD1'
import { buildView } from './view'

const planning = vi.hoisted(() => ({ groups: 1, rows: 700_001 }))

vi.mock('./index', async importOriginal => {
  const actual = await importOriginal<typeof import('./index')>()
  return {
    ...actual,
    // Model an interval size sort with broad candidate groups: its real decoder refuses these
    // bounds before fetching data, while the fixture's path sort can answer the same view.
    planSizeRects: async (...args: Parameters<typeof actual.planSizeRects>): Promise<Span[]> => {
      const spans = await actual.planSizeRects(...args)
      return Array.from({ length: planning.groups }, (_, rg) => ({ ...spans[0], rg, rowStart: 0, rowEnd: planning.rows }))
    },
  }
})
vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

beforeEach(() => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
})

it.each([{ groups: 1, rows: 700_001 }, { groups: 251, rows: 1 }])('uses the path plan when the interval size plan exceeds its read budget: %j', async bounds => {
  Object.assign(planning, bounds)
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as {
    existsSync(path: string): boolean
    readFileSync(path: string): Uint8Array
  }
  const gets: string[] = []
  const bucket = {
    get: async (key: string, o?: { range?: { offset: number; length: number } }) => {
      const file = fixture(`iv/${key}`)
      if (!fs.existsSync(file)) return null
      gets.push(key)
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
  const env = { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: bucket } as Env
  const view = await buildView(env, { date: '2026-08-03', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, ivPlanRows: 0 })
  const cb = await ivClassBytes('2026-08-03', 'b2/e', 8)
  expect({ tier: view.tier, tree: view.tree }).toEqual({
    tier: 'path',
    tree: {
      name: 'e', kind: 'dir', bytes: 20, objects: 3, last_read: 6, mtime: 7, class_bytes: cb[''], owners: [['alice', 10]],
      children: [
        { name: 'd.json', kind: 'file', bytes: 20, objects: 1, last_read: 6, mtime: 7, class_bytes: cb['/d.json'] },
        { name: 'e', kind: 'dir', bytes: 10, objects: 1, last_read: 5, mtime: 7, class_bytes: cb['/e'] },
      ],
    },
  })
  expect(gets.filter(key => key.endsWith('/bysize.parquet'))).toEqual([])
})
