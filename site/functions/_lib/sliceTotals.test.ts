import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { asOfScans, groupDoc, type IndexHandle, IvGroup, IvTierBroken, IvUnavailable, ivRefusal, openIndex, planSizeRects, readRects, readSizeRects, rowColumns, type Row, slices, withTrace } from './index'
import { fixture } from './testStore'
import { buildView } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// A scoped read (an owner pool, a class scope) of the interval store's total-keyed slice sort (`slices-bytotal`) tests
// each row's own `tot` against the threshold, on the raw columns: a row under it is never shaped, and a version that
// has ended is never summed with the path's current slices.
//
// `fixtures/iv/interval-store/g8` (`fixtures/iv/gen.py g8`): a base scan S1 and a run S2 in 4-row groups. `bk/grow`'s
// total goes 256,000 → 4,450,304 between them. In the base, its unowned and alice's old slices share a group with
// `bk/big/b`'s 8 MiB rows; in the run, alice's and bob's close records are a group of their own (`b_max` 256,000). A
// read of S2 at a 1 MiB floor plans the first and not the second.

const [S1, S2] = ['2026-09-30T0007', '2026-09-30T0008']
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
  // `list` / `head` over the fixture dir: the store's run is found by its manifest.
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

const store = { PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g8', INDEX_R2: r2(), ROOT_LABEL: 'root' } as Env
const ALL = [{ dLo: 1, dHi: 1e9, pLo: '', pHi: '￿' }]
/** A slice row as the tests name it: `depth path owner size objects`. */
const slice = (r: Row) => `${r.depth} ${r.path} ${r.usr ?? '-'} ${r.size} ${r.n_files}`
/** The slice sorts of `date`: path-first (every slice, the reference) and total-keyed. */
const sorts = async (date: string): Promise<{ byPath: IndexHandle; byTotal: IndexHandle }> => {
  const env = slices(asOfScans(store, [date]))
  return { byPath: await openIndex(env, date, 'path'), byTotal: await openIndex(env, date, 'bysize') }
}

beforeAll(() => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
})

describe('IvGroup.pick', () => {
  it("hands the read's test each row's `tot`, and shapes only the rows it passes", () => {
    const raw = [
      { depth: 2, path: 'bk/grow', usr: 'alice', vf: 1, vt: 2, kind: 'dir', size: 100, n_files: 1, tot: 250 },
      { depth: 2, path: 'bk/grow', usr: 'bob', vf: 1, vt: 2, kind: 'dir', size: 150, n_files: 1, tot: 250 },
      { depth: 2, path: 'bk/big', usr: 'alice', vf: 1, vt: 2, kind: 'dir', size: 900, n_files: 3, tot: 1000 },
    ]
    const seen: [string, number, number | undefined][] = []
    const g = new IvGroup({ version: 3 }, groupDoc(raw, Object.keys(raw[0])))
    const rows = g.pick((_d, p, size, tot) => { seen.push([p, size, tot]); return tot! >= 500 })
    expect(seen).toEqual([['bk/grow', 100, 250], ['bk/grow', 150, 250], ['bk/big', 900, 1000]])
    expect(rows.map(r => [r.path, r.usr, r.size, r.tot])).toEqual([['bk/big', 'alice', 900, 1000]])
    // …and of a group already held shaped (`all`).
    expect(g.all().map(r => r.tot)).toEqual([250, 250, 1000])
    expect(g.pick((_d, _p, _s, tot) => tot! < 500).map(r => [r.path, r.usr, r.tot])).toEqual([['bk/grow', 'alice', 250], ['bk/grow', 'bob', 250]])
  })
  it('hands no `tot` on a sort without the column', () => {
    const raw = [{ depth: 1, path: 'bk', usr: null, vf: 1, vt: 2, kind: 'dir', size: 7, n_files: 1 }]
    const seen: (number | undefined)[] = []
    new IvGroup({ version: 3 }, groupDoc(raw, Object.keys(raw[0]))).pick((_d, _p, _s, tot) => { seen.push(tot); return true })
    expect(seen).toEqual([undefined])
  })
})

describe("a total-keyed read's rows", () => {
  it("leave out a path's ended slices whose close records the floor left unplanned", async () => {
    const { byTotal } = await sorts(S2)
    let held = 0
    const rows = await readSizeRects(withTrace(byTotal, (name, n) => { if (name === 'held') held += n }), ALL, () => MiB)
    expect(rows.map(slice).sort()).toEqual([
      '1 bk - 51200 1',
      '1 bk alice 12685312 3',
      '1 bk bob 8491008 2',
      '2 bk/big - 0 0',
      '2 bk/big alice 8388608 1',
      '2 bk/big bob 8388608 1',
      // …not alice's 102,400 of S1 beside her 4,296,704 of S2.
      '2 bk/grow - 51200 1',
      '2 bk/grow alice 4296704 2',
      '2 bk/grow bob 102400 1',
      '3 bk/big/a alice 8388608 1',
      '3 bk/big/b bob 8388608 1',
      '3 bk/grow/a alice 4296704 2',
      '4 bk/big/a/f0 alice 8388608 1',
      '4 bk/big/b/f0 bob 8388608 1',
      '4 bk/grow/a/f1 alice 4194304 1',
    ])
    // The versions it held to say so: the 15 above, the 3 of `bk` that S2 ended and the 3 close records that end them.
    // Its plan's other 3 rows are `bk/grow`'s of S1 (total 256,000): two slices, and the one close record planned.
    const plan = await planSizeRects(byTotal, ALL, () => MiB)
    expect({ planned: plan.reduce((n, g) => n + (g.rowEnd - g.rowStart), 0), held }).toEqual({ planned: 24, held: 21 })
  })

  it('are the path-first sort\'s, at every threshold, on both scans', async () => {
    for (const date of [S1, S2]) {
      const { byPath, byTotal } = await sorts(date)
      const every = await readRects(byPath, ALL)
      const at = (r: Row) => `${r.depth}\0${r.path}`
      const total = new Map<string, number>()
      for (const r of every) total.set(at(r), (total.get(at(r)) ?? 0) + r.size)
      // Each path's total, and one byte past it.
      const floors = [...new Set([...total.values()].flatMap(t => [t, t + 1]))].sort((a, b) => a - b)
      const got: Record<number, string[]> = {}
      const want: Record<number, string[]> = {}
      for (const floor of floors) {
        got[floor] = (await readSizeRects(byTotal, ALL, () => floor)).map(slice).sort()
        want[floor] = every.filter(r => total.get(at(r))! >= floor).map(slice).sort()
      }
      expect({ date, floors: floors.length, got }).toEqual({ date, floors: date === S1 ? 12 : 16, got: want })
    }
  })
})

describe('a class-scoped view over the total-keyed sort', () => {
  it("counts a path's current slices only", async () => {
    // Every byte is Standard, so the scope's bytes are the totals'. 22×11 px at 12 px² a tile: a floor of 1,052,604
    // bytes, between `bk/grow`'s two totals. `ivPlanRows: 0`: the subtree is read from the size sort, as a bucket's is.
    const v = await buildView(store, { date: S2, path: 'bk', w: 22, h: 11, minArea: 12, atten: 1, classes: new Set(['1']), ivPlanRows: 0 })
    const kids = (v.tree.c ?? []).map(c => [c.n, c.b, c.o])
    expect({ tier: v.tier, threshold: Math.round(v.threshold), root: [v.tree.b, v.tree.o], kids }).toEqual({
      tier: 'bysize',
      threshold: 1052604,
      root: [21227520, 6],
      kids: [['big', 16777216, 2], ['grow', 4450304, 4]],
    })
  })
})

// Last in the file: it cuts the run out of this isolate's store (`IV_BROKEN_TTL`).
describe("a run's total-keyed sort without `tot`, under a base that has it", () => {
  it('is its run broken, by name, not a read short of its rows', async () => {
    const { byTotal } = await sorts(S2)
    const run = byTotal.runs![0]
    expect(run.columns!.slice(-1)).toEqual(['tot'])
    // The run as its footer would open without the column: `openIvRun`'s schema and `rowColumns` of it.
    const schema = run.schema.filter(l => l.name !== 'tot')
    const bare = { ...byTotal, runs: [{ ...run, schema, columns: rowColumns(run.version, schema) }] } as IndexHandle
    const err = await readSizeRects(bare, ALL, () => MiB).then(() => null, (e: unknown) => e)
    expect(err).toBeInstanceOf(IvTierBroken)
    expect((err as Error).message).toBe(`interval store run ${run.run} is broken: generation g8, tier ${run.run}: sort \`slices-bytotal\` has no \`tot\` column under a base whose sort has it, so its rows can't be tested against a threshold`)
    const res = ivRefusal(err)!
    expect([res.status, res.headers.get('retry-after'), res.headers.get('cache-control')]).toEqual([503, '30', 'no-store'])
    // The retry (`ivRetry`) finds the run cut out: its scan is refused, and the scan before it answers.
    await expect(sorts(S2)).rejects.toBeInstanceOf(IvUnavailable)
    const before = await sorts(S1)
    expect((await readSizeRects(before.byTotal, ALL, () => (1 << 24) + 1)).map(slice).sort()).toEqual(['1 bk - 51200 1', '1 bk alice 8491008 2', '1 bk bob 8491008 2'])
  })
})
