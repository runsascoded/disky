import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { asOfScans, type IndexHandle, openIndex, planRects, readRects, readSlicesByFolded, type Row, slices } from './index'
import { fixture } from './testStore'
import { buildView, type ViewNode } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// What a thresholded path-first read returns is what its row groups say by construction: a version it keeps has its
// close record in a planned group, and a path it keeps has all of its bytes.
//
// `fixtures/iv/interval-store/g9` (`fixtures/iv/gen.py g9`): a base scan S1 and a run S2 in 4-row groups. S2 rewrites
// `bk/grow/a/f0` (102,400 → 4,194,304) and removes `bk/grow/b/f0`. In the base's `path` sort their old rows share a group
// with `bk/big/*/f0`'s 8 MiB rows; in the run's, their close records are a group of their own (`b_max` 102,400). A read of
// S2 at a 1 MiB floor plans the first and not the second. In `slices`, `bk/big`'s total (16 MiB) is two 8 MiB slices, so
// a floor over 8 MiB finds no group of its slices though the path clears it.

const [S1, S2] = ['2026-09-30T0009', '2026-09-30T0010']
const MiB = 1 << 20

/** `INDEX_R2` over `fixtures/iv/`. */
function r2(): R2Bucket {
  const node = async () => {
    const mod = 'node:fs'
    return (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
  }
  const get = async (key: string, o?: { range?: { offset: number; length: number } | { suffix: number } }) => {
    const fs = await node()
    const file = fixture(`iv/${key}`)
    if (!fs.existsSync(file)) return null
    const buf = fs.readFileSync(file)
    const r = o?.range
    const { offset = 0, length = buf.byteLength - offset } = r && 'suffix' in r ? { offset: Math.max(0, buf.byteLength - r.suffix) } : r ?? {}
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  const list = async ({ prefix, cursor }: { prefix: string; cursor?: string }) => {
    const fs = await node()
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${dir}${f}`).filter(k => k.startsWith(prefix) && fs.statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false, cursor }
  }
  const head = async (key: string) => ((await node()).existsSync(fixture(`iv/${key}`)) ? { key } : null)
  return { get, list, head } as unknown as R2Bucket
}

const store = { PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g9', INDEX_R2: r2(), ROOT_LABEL: 'root' } as Env
const ALL = [{ dLo: 1, dHi: 1e9, pLo: '', pHi: '￿' }]
const row = (r: Row) => `${r.path} ${r.size}`
const slice = (r: Row) => `${r.path} ${r.usr ?? '-'} ${r.size}`
const folded = (date: string): Promise<IndexHandle> => openIndex(asOfScans(store, [date]), date, 'path')
const sliced = (date: string): Promise<IndexHandle> => openIndex(slices(asOfScans(store, [date])), date, 'path')
/** A view's tiles, `path bytes objects`. */
const tiles = (n: ViewNode, at = ''): string[] => [`${at}${n.name} ${n.bytes} ${n.objects}`, ...(n.children ?? []).flatMap(c => tiles(c, `${at}${n.name}/`))]

beforeAll(() => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
})

describe("a thresholded read of the folded `path` sort", () => {
  it('leaves out a rewritten and a removed object whose close records the floor left unplanned', async () => {
    const h = await folded(S2)
    const plan = await planRects(h, ALL, () => MiB)
    expect((await readRects(h, ALL, () => MiB, undefined, plan)).map(row).sort()).toEqual([
      'bk 21022720',
      'bk/big 16777216',
      'bk/big/a 8388608',
      'bk/big/a/f0 8388608',
      'bk/big/b 8388608',
      'bk/big/b/f0 8388608',
      'bk/grow 4245504',
      'bk/grow/a 4194304',
      // …not its 102,400 of S1 beside it, nor the removed `bk/grow/b/f0`.
      'bk/grow/a/f0 4194304',
    ])
  })

  it('is the unpruned read cut at the floor, at every floor, on both scans', async () => {
    for (const date of [S1, S2]) {
      const h = await folded(date)
      const every = await readRects(h, ALL)
      const floors = [...new Set(every.flatMap(r => [r.size, r.size + 1]))].sort((a, b) => a - b)
      const got: Record<number, string[]> = {}
      const want: Record<number, string[]> = {}
      for (const floor of floors) {
        got[floor] = (await readRects(h, ALL, () => floor)).map(row).sort()
        want[floor] = every.filter(r => r.size >= floor).map(row).sort()
      }
      expect({ date, got }).toEqual({ date, got: want })
    }
  })

  it('at a floor that rises with depth, and one that falls with it', async () => {
    for (const atten of [2, 0.5]) {
      const h = await folded(S2)
      const every = await readRects(h, ALL)
      const thrAt = (d: number) => MiB * atten ** (d - 1)
      expect((await readRects(h, ALL, thrAt)).map(row).sort()).toEqual(every.filter(r => r.size >= thrAt(r.depth)).map(row).sort())
    }
  })
})

describe('a plain view read from `path`', () => {
  it('draws a rewritten object at its current size, and no removed one', async () => {
    // 22×11 px at 12 px² a tile: a floor of 1,042,449 bytes, every level.
    const v = await buildView(store, { date: S2, path: 'bk', w: 22, h: 11, minArea: 12, atten: 1, maxDepth: 3, ivPlanRows: 0 })
    expect({ tier: v.tier, threshold: Math.round(v.threshold) }).toEqual({ tier: 'path', threshold: 1042449 })
    expect(tiles(v.tree).sort()).toEqual([
      'bk 21022720 4',
      'bk/big 16777216 2',
      'bk/big/a 8388608 1',
      'bk/big/a/f0 8388608 1',
      'bk/big/b 8388608 1',
      'bk/big/b/f0 8388608 1',
      'bk/grow 4245504 2',
      'bk/grow/a 4194304 1',
      'bk/grow/a/f0 4194304 1',
    ])
  })
})

describe('a read of the path-first slice sort', () => {
  it("leaves no ended slice beside a path's current ones, and holds every slice of a path clearing the floor", async () => {
    for (const date of [S1, S2]) {
      const h = await sliced(date)
      const every = await readRects(h, ALL)
      const total = new Map<string, number>()
      for (const r of every) total.set(r.path, (total.get(r.path) ?? 0) + r.size)
      const floors = [...new Set([...total.values(), ...every.map(r => r.size)].flatMap(t => [t, t + 1]))].sort((a, b) => a - b)
      const got: Record<number, string[]> = {}
      const want: Record<number, string[]> = {}
      for (const floor of floors) {
        const clears = (r: Row) => total.get(r.path)! >= floor
        got[floor] = (await readRects(h, ALL, () => floor)).filter(clears).map(slice).sort()
        want[floor] = every.filter(clears).map(slice).sort()
      }
      expect({ date, got }).toEqual({ date, got: want })
    }
  })

  it('by way of the folded sort: every slice of the paths clearing the floor, and no other', async () => {
    for (const date of [S1, S2]) {
      const [h, fh] = [await sliced(date), await folded(date)]
      const every = await readRects(h, ALL)
      const total = new Map<string, number>()
      for (const r of every) total.set(r.path, (total.get(r.path) ?? 0) + r.size)
      const floors = [...new Set([...total.values()].flatMap(t => [t, t + 1]))].sort((a, b) => a - b)
      const got: Record<number, string[]> = {}
      const want: Record<number, string[]> = {}
      for (const floor of floors) {
        const spans = await planRects(fh, ALL, () => floor)
        got[floor] = (await readSlicesByFolded(h, fh, ALL, () => floor, spans)).map(slice).sort()
        want[floor] = every.filter(r => total.get(r.path)! >= floor).map(slice).sort()
      }
      expect({ date, got }).toEqual({ date, got: want })
    }
  })

  it("a lens's: one owner's slices over the floor, none ended", async () => {
    const h = await sliced(S2)
    const every = await readRects(h, ALL)
    const lens = { key: 'alice' } as Parameters<typeof readRects>[3]
    const alice = every.filter(r => r.usr === 'alice')
    for (const floor of [1, 102400, 102401, MiB, 4194304, 4194305, 8388609]) {
      expect({ floor, got: (await readRects(h, ALL, () => floor, lens)).map(slice).sort() }).toEqual({ floor, got: alice.filter(r => r.size >= floor).map(slice).sort() })
    }
  })
})

describe('a class-scoped view', () => {
  it('keeps a path whose total clears the floor though none of its slices does', async () => {
    // Every byte is Standard, so the scope's bytes are the totals'. 5×5 px at 12 px² a tile: a floor of 10,090,905 bytes,
    // over `bk/big`'s 8 MiB slices, under its 16 MiB.
    const v = await buildView(store, { date: S2, path: 'bk', w: 5, h: 5, minArea: 12, atten: 1, classes: new Set(['1']) })
    expect({ threshold: Math.round(v.threshold), tiles: tiles(v.tree).sort() }).toEqual({
      threshold: 10090906,
      tiles: ['bk 21022720 4', 'bk/big 16777216 2'],
    })
  })
})
