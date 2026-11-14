import { describe, expect, it } from 'vitest'
import { attenLog2, checkResponse, diffBody, pinsMatch, shareLog2, signRequest, subtreeBody, type VmAnswer, vmOwners, vmRequest } from './vmAdapter.js'

const pins = { gen: '2026-10-09b.g3.c001', manifest: '2026-11-13T1734.m001.json' }

describe('vmRequest', () => {
  it('derives the cell rule from the viewport and maps the scopes', () => {
    expect([shareLog2(390, 844, 8), shareLog2(2560, 1440, 8), shareLog2(1280, 768, 12)]).toEqual([16, 19, 17])
    expect([1, 2, 4].map(attenLog2)).toEqual([0, 1, 2])
    expect(() => attenLog2(3)).toThrow('atten 3: VM mode takes a power of 2')
    expect([vmOwners({ owner: 'owned' }), vmOwners({ owner: 'unowned' }), vmOwners({ owner: { not: ['a', 'b'] } }), vmOwners({ lens: 'u1' }), vmOwners({})]).toEqual([
      { not_in: [''] }, { in: [''] }, { not_in: ['', 'a', 'b'] }, { in: ['u1'] }, undefined,
    ])
    const q = { alts: [[{ kind: 'sub' as const, text: 'ckpt' }]], neg: [] }
    expect(vmRequest({ pins, date: '2026-11-13T1734', path: 'b1/', query: q, owner: 'owned', classes: new Set(['3', '2'] as const), w: 1280, h: 768, minArea: 12, atten: 2, deadlineMs: 5000 })).toEqual({
      pins, scan: '2026-11-13T1734', path: 'b1', q, owners: { not_in: [''] }, classes: [2, 3], cell: { share_log2: 17, atten_log2: 1 }, deadline_ms: 5000,
    })
    expect(vmRequest({ pins, from: 'a', to: 'b', path: '', w: 256, h: 256, minArea: 16, atten: 1, maxDepth: 3 })).toEqual({
      pins, from: 'a', to: 'b', path: '', cell: { share_log2: 12, atten_log2: 0, depth: 3 },
    })
    // No `atten`: the Worker's own halving (`ATTEN_DEFAULT` = 2), named in the request.
    expect(vmRequest({ pins, date: 'a', path: '', w: 390, h: 844, minArea: 8 })).toEqual({
      pins, scan: 'a', path: '', cell: { share_log2: 16, atten_log2: 1 },
    })
  })
})

const answer = (diff: boolean): VmAnswer => ({
  v: 1, tenant: 'gcs', pins, path: '', cell: { share_log2: 4, atten_log2: 1 },
  ...(diff ? { from: 'a', to: 'b' } : { scan: 'a' }),
  total: { bytes: 160, objects: 6, ...(diff ? { bytes_to: 100, objects_to: 4 } : {}) },
  nodes: 2,
  tree: {
    name: '', kind: 'dir', bytes: 160, objects: 6, ...(diff ? { bytes_to: 100, objects_to: 4 } : {}),
    children: [
      { name: 'b1', kind: 'dir', bytes: 150, objects: 4, ...(diff ? { bytes_to: 90, objects_to: 3 } : {}), matched: true,
        children: [{ name: 'x', kind: 'file', bytes: 150, objects: 4, ...(diff ? { bytes_to: 0, objects_to: 0 } : {}) }, { name: '(other)', kind: 'dir', bytes: 0, objects: 0, ...(diff ? { bytes_to: 90, objects_to: 3 } : {}), folded: 2 }] },
      { name: '(other)', kind: 'dir', bytes: 10, objects: 2, ...(diff ? { bytes_to: 10, objects_to: 1 } : {}), folded: 1 },
    ],
  },
  timing: { pass_ms: 1, merge_ms: 0, total_ms: 1, rows: 6, dirs: 1, shards: 1 },
})

describe('answers as the API bodies', () => {
  it('subtree: the root label, the threshold, the tree', () => {
    expect(subtreeBody(answer(false), { date: 'a', w: 1280, h: 800, minArea: 12, atten: 2, rootLabel: 'all buckets' })).toEqual({
      date: 'a', path: '', w: 1280, h: 800, minArea: 12, atten: 2, tier: 'vm', index: `vm:${pins.gen}/${pins.manifest}`,
      threshold: 10, nodes: 2, truncated: false,
      tree: {
        name: 'all buckets', kind: 'dir', bytes: 160, objects: 6,
        children: [
          { name: 'b1', kind: 'dir', bytes: 150, objects: 4, matched: true, children: [{ name: 'x', kind: 'file', bytes: 150, objects: 4 }, { name: '(other)', kind: 'dir', bytes: 0, objects: 0, folded: 2 }] },
          { name: '(other)', kind: 'dir', bytes: 10, objects: 2, folded: 1 },
        ],
      },
    })
  })

  it('diff: rows in pre-order with both sides and a status', () => {
    const row = (path: string, depth: number, kind: 'file' | 'dir', status: string, bb: number, ba: number, ob: number, oa: number, expanded = false) =>
      ({ path, depth, kind, status, bytes_before: bb, bytes_after: ba, objects_before: ob, objects_after: oa, ...(expanded ? { expanded: true } : {}) })
    expect(diffBody(answer(true))).toEqual({
      rows: [
        row('b1', 1, 'dir', 'changed', 150, 90, 4, 3, true),
        row('b1/x', 2, 'file', 'removed', 150, 0, 4, 0),
        row('b1/(other)', 2, 'dir', 'added', 0, 90, 0, 3),
        row('(other)', 1, 'dir', 'changed', 10, 10, 2, 1),
      ],
      bytes_before: 160, bytes_after: 100, objects_before: 6, objects_after: 4,
    })
  })
})

describe('signatures', () => {
  // The vectors `vmd` (`serve.rs`) and `vmd_client.py` compute for the same strings.
  it('signs a request and checks a reply as vmd does', async () => {
    expect(await signRequest('k', 'gcs', 'POST', '/v1/view', '{}', 1700000000)).toEqual({
      'x-disky-tenant': 'gcs', 'x-disky-ts': '1700000000', 'x-disky-sig': '9db88e722608723dc50a0d0aaf32bca0123cd6738402f555777db155a9032988',
    })
    const sig = 'b9e05c824a1a6e5bc3df4b28f9d36f5667304f1d8cf0b125492e74a56ccca4fe'
    expect(await Promise.all([
      checkResponse('k', 'gcs', '1700000000', '{"v":1}', sig),
      checkResponse('k', 'gcs', '1700000001', '{"v":1}', sig),
      checkResponse('k2', 'gcs', '1700000000', '{"v":1}', sig),
      checkResponse('k', 'gcs', '1700000000', '{"v":1}', null),
    ])).toEqual([true, false, false, false])
    expect([pinsMatch(pins, { ...pins }), pinsMatch(pins, { ...pins, manifest: 'x' })]).toEqual([true, false])
  })
})
