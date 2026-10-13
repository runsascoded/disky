import { describe, expect, it } from 'vitest'
import { dominant, legendOf, NoScanMatch, ownerColors, resolveScan, scanOfSel, tilesOf, UNOWNED_COLOR } from './data'
import type { TreeNode } from '../../../src/types'

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
  it('legend: owners and the unowned pool by bytes, named', () => {
    expect(legendOf(tree, color, u => u.toUpperCase())).toEqual([
      { label: 'ANN', color: '#4269d0', b: 50 },
      { label: 'unowned', color: UNOWNED_COLOR, b: 30 },
      { label: 'BO', color: '#efb118', b: 20 },
    ])
  })
})

describe('resolveScan: a card draws the scan its ?d= names, or none (a 404)', () => {
  const env = async () => {
    const { sqliteD1 } = await import('../testD1')
    const { db } = await sqliteD1('cw')
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
