import { describe, expect, it } from 'vitest'
import { canDeepen, deepestView, depthPlan, dominant, legendOf, NoScanMatch, ownerColors, resolveScan, scanOfSel, tilesOf, UNOWNED_COLOR } from './data'
import type { TreeNode } from '../../../src/types'
import { dateColor, slotColor } from '../../../src/colors'
import { sqliteD1 } from '../testD1'

describe('D1', () => {
  describe('scanOfSel: the after scan of a `?d=` selection', () => {
    it('compact and ISO ids; look-back / from suffixes dropped', () => {
      expect(['261002', '261002-1200', '261002-260930', '2026-10-02', '2026-10-02T1200', '261002-7d', '-7d', 'junk', undefined].map(scanOfSel))
        .toEqual(['2026-10-02', '2026-10-02T1200', '2026-10-02', '2026-10-02', '2026-10-02T1200', '2026-10-02', undefined, undefined, undefined])
    })
  })

  describe('tiles and legend', () => {
    const users = [{ u: 'ann', b: 9 }, { u: 'bo', b: 5 }]
    const color = ownerColors(users)
    const tree: TreeNode = {
      n: 'root', b: 100, o: 10, us: [['ann', 50], ['bo', 20]],
      c: [
        { n: 'a', b: 60, o: 6, us: [['ann', 50]], c: [{ n: 'a1', b: 50, o: 5, us: [['ann', 50]] }, { n: 'a2', b: 10, o: 1 }] },
        { n: 'b', b: 30, o: 3, us: [['bo', 10]] },
        { n: '(other)', b: 10, o: 1, f: 4 },
      ],
    }
    it('dominant: the top owner unless the unowned remainder outweighs it', () => {
      expect(tree.c!.map(dominant)).toEqual(['ann', null, null])
    })
    it('tiles: owner colours by scan rank, unowned and folds grey', () => {
      expect(tilesOf(tree, color)).toEqual([
        { name: 'a', b: 60, color: '#4269d0', kids: [{ name: 'a1', b: 50, color: '#4269d0' }, { name: 'a2', b: 10, color: UNOWNED_COLOR }] },
        { name: 'b', b: 30, color: UNOWNED_COLOR },
        { name: '(other)', b: 10, color: '#2b2e35' },
      ])
    })
    it('tiles nest to the view\'s depth: tree mode keys every cell by its depth-1 slot and depth-2 rank', () => {
      const deep: TreeNode = {
        n: 'root', b: 100, o: 10,
        c: [
          { n: 'a', b: 70, o: 7, c: [
            { n: 'a1', b: 50, o: 5, c: [{ n: 'a1x', b: 30, o: 3, c: [{ n: 'a1x0', b: 30, o: 3 }] }, { n: '(other)', b: 20, o: 2, f: 3 }] },
            { n: 'a2', b: 20, o: 2 },
          ] },
          { n: 'b', b: 30, o: 3 },
        ],
      }
      expect(tilesOf(deep, color, 'tree')).toEqual([
        { name: 'a', b: 70, color: slotColor(0), kids: [
          { name: 'a1', b: 50, color: slotColor(0, 0, 2), kids: [
            { name: 'a1x', b: 30, color: slotColor(0, 0, 2), kids: [{ name: 'a1x0', b: 30, color: slotColor(0, 0, 2) }] },
            { name: '(other)', b: 20, color: '#2b2e35' },
          ] },
          { name: 'a2', b: 20, color: slotColor(0, 1, 2) },
        ] },
        { name: 'b', b: 30, color: slotColor(1) },
      ])
    })
    it('tree mode under one top-level dir: its children take the slots, the lone ancestor a neutral frame', () => {
      // hf: the org's one bucket over models / buckets / datasets — keyed at the root, the whole card is one hue.
      const lone: TreeNode = {
        n: 'root', b: 100, o: 10,
        c: [{ n: 'org', b: 100, o: 10, c: [
          { n: 'models', b: 70, o: 7, c: [{ n: 'm1', b: 50, o: 5 }, { n: 'm2', b: 20, o: 2 }] },
          { n: 'datasets', b: 30, o: 3 },
        ] }],
      }
      expect(tilesOf(lone, color, 'tree')).toEqual([
        { name: 'org', b: 100, color: '#232322', kids: [
          { name: 'models', b: 70, color: slotColor(0), kids: [
            { name: 'm1', b: 50, color: slotColor(0, 0, 2) },
            { name: 'm2', b: 20, color: slotColor(0, 1, 2) },
          ] },
          { name: 'datasets', b: 30, color: slotColor(1) },
        ] },
      ])
      // owner mode keeps the view's own children as they are
      expect(tilesOf(lone, color)[0].color).toBe(UNOWNED_COLOR)
    })
    it('date mode: containers neutral at every depth; the scale spans the drawn leaves', () => {
      const deep: TreeNode = {
        n: 'root', b: 100, o: 10, d: 5,
        c: [
          { n: 'a', b: 70, o: 7, d: 30, c: [{ n: 'a1', b: 50, o: 5, d: 30, c: [{ n: 'a1x', b: 50, o: 5, d: 20 }] }, { n: 'a2', b: 20, o: 2, d: 30 }] },
          { n: 'b', b: 30, o: 3, d: 10 },
        ],
      }
      expect(tilesOf(deep, color, 'date')).toEqual([
        { name: 'a', b: 70, color: '#232322', kids: [
          { name: 'a1', b: 50, color: '#232322', kids: [{ name: 'a1x', b: 50, color: dateColor(0.5) }] },
          { name: 'a2', b: 20, color: dateColor(1) },
        ] },
        { name: 'b', b: 30, color: dateColor(0) },
      ])
    })
    it('legend: owners and the unowned pool by bytes, named', () => {
      expect(legendOf(tree, color, u => u.toUpperCase())).toEqual([
        { label: 'ANN', color: '#4269d0', b: 50 },
        { label: 'unowned', color: UNOWNED_COLOR, b: 30 },
        { label: 'BO', color: '#efb118', b: 20 },
      ])
    })
  })

  describe('depth by budget', () => {
    it('depthPlan: the floor, then depth 3 and the cap, under the budget; bad values fall back', () => {
      expect([
        depthPlan({}),
        depthPlan({ OG_DEPTH_BUDGET_MS: '0', OG_MAX_DEPTH: '3' }),
        depthPlan({ OG_DEPTH_BUDGET_MS: 'soon', OG_MAX_DEPTH: '1' }),
        depthPlan({ OG_DEPTH_BUDGET_MS: '', OG_MAX_DEPTH: '8' }),
      ]).toEqual([
        { floor: 2, deeper: [3, 5], budgetMs: 1500 },
        { floor: 2, deeper: [3], budgetMs: 0 },
        { floor: 2, deeper: [], budgetMs: 1500 },
        { floor: 2, deeper: [3, 8], budgetMs: 1500 },
      ])
    })

    /** A read per depth that lands after `ms[depth]` (or fails, `'x'`), logging what was asked. */
    const reads = (ms: Record<number, number | 'x'>) => {
      const asked: number[] = []
      const read = (d: number) => {
        asked.push(d)
        const t = ms[d]
        return new Promise<string>((res, rej) => setTimeout(() => t === 'x' ? rej(new Error(`depth ${d}`)) : res(`view ${d}`), t === 'x' ? 1 : t))
      }
      return { asked, read }
    }
    const plan = { floor: 2, deeper: [3, 5], budgetMs: 200 }
    it.each([
      ['every deeper read lands: the deepest', { 2: 1, 3: 5, 5: 10 }, plan, { value: 'view 5', depth: 5 }, [2, 3, 5]],
      ['the cap runs past the budget: the deepest that landed', { 2: 1, 3: 5, 5: 1000 }, plan, { value: 'view 3', depth: 3 }, [2, 3, 5]],
      ['none lands in time: the floor', { 2: 1, 3: 1000, 5: 1000 }, plan, { value: 'view 2', depth: 2 }, [2, 3, 5]],
      ['a failed deeper read is dropped', { 2: 1, 3: 5, 5: 'x' }, plan, { value: 'view 3', depth: 3 }, [2, 3, 5]],
      ['a zero budget reads the floor alone', { 2: 1 }, { ...plan, budgetMs: 0 }, { value: 'view 2', depth: 2 }, [2]],
    ] as const)('%s', async (_, ms, p, want, asked) => {
      const r = reads(ms)
      expect([await deepestView({ ...p, deeper: [...p.deeper] }, r.read, () => true), r.asked]).toEqual([want, asked])
    })
    it('a floor that can\'t deepen, or that spent the budget, is drawn alone', async () => {
      const flat = reads({ 2: 1 })
      let t = 0
      const slow = reads({ 2: 1 })
      expect([
        await deepestView(plan, flat.read, () => false), flat.asked,
        await deepestView(plan, d => { t += 300; return slow.read(d) }, () => true, () => t), slow.asked,
      ]).toEqual([{ value: 'view 2', depth: 2 }, [2], { value: 'view 2', depth: 2 }, [2]])
    })
    it('canDeepen: some depth-2 cell is a real directory', () => {
      const tree = (c2: TreeNode[]): TreeNode => ({ n: 'r', b: 1, o: 1, c: [{ n: 'a', b: 1, o: 1, c: c2 }, { n: 'f', b: 1, o: 1, k: 'file' }] })
      expect([
        canDeepen(tree([{ n: 'a1', b: 1, o: 1, k: 'dir' }])),
        canDeepen(tree([{ n: 'a1', b: 1, o: 1, k: 'file' }, { n: '(other)', b: 1, o: 1, f: 2 }])),
        canDeepen(tree([])),
      ]).toEqual([true, false, false])
    })
  })

  describe('resolveScan: a card draws the scan its ?d= names, or none (a 404)', () => {
    const env = async () => {
      const { db } = await sqliteD1()
      for (const date of ['2026-10-08', '2026-10-09T0601', '2026-10-09T1236']) await db.prepare("INSERT INTO index_schema (date, variant, version, schema_json) VALUES (?, 'path', 2, '[]')").bind(date).run()
      return { DB: db } as unknown as Parameters<typeof resolveScan>[0]
    }
    it.each([
      [undefined, '2026-10-09T1236'],
      ['-7d', '2026-10-09T1236'],
      ['261009', '2026-10-09T1236'],
      ['26100906', '2026-10-09T0601'],
      ['261009-261008', '2026-10-09T1236'],
      ['2026-10-08', '2026-10-08'],
    ])('d=%s → %s', async (d, scan) => {
      expect(await resolveScan(await env(), d)).toBe(scan)
    })
    it.each(['26100903', '261010', 'junk'])('d=%s names no scan: NoScanMatch, never the latest', async d => {
      await expect(resolveScan(await env(), d)).rejects.toEqual(new NoScanMatch(d))
    })
  })
})
