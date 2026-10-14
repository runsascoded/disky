import { describe, expect, it } from 'vitest'
import { composeFederatedTrees, federatedSeries, mountFederatedAgeRows } from './federatedViews'
import { parseSourceCatalog, resolveFederation } from './federatedScans'

const t = (hour: number) => `2026-10-13T${String(hour).padStart(2, '0')}:00:00.000Z`
const source = (name: string, hours: number[], base: number) => parseSourceCatalog({
  version: 1, source: name, label: name, mount: name, scope: { store: `r2:test/${name}`, prefix: '' },
  scans: hours.map((hour, i) => ({
    id: String(hour), revision: '1', digest: String(i).repeat(64), complete: true,
    observed_from: t(hour), observed_through: t(hour), published_at: t(hour + 1),
    total_bytes: base + i, total_objects: i, artifacts: { format: 'path-store-v2', path: `${hour}/path.parquet` },
  })),
})
const catalogs = [source('awair', [8, 10], 100), source('ctbk', [9, 11], 1000)]
const view = resolveFederation(catalogs, t(10))
const reads = view.sources.map(s => ({ source: s.source, digest: s.scan!.digest,
  tree: { n: '.', b: s.scan!.total_bytes, o: s.scan!.total_objects, c: [{ n: 'pyramid', b: s.scan!.total_bytes, o: s.scan!.total_objects }] },
}))
describe('federated source reader adapters', () => {
  it('mounts pinned source roots as ordinary tree nodes, preserving children', () => {
    expect(composeFederatedTrees(view, reads)).toEqual({
      tree: { n: 'All projects', k: 'dir', b: 1101, o: 1, c: [
        { n: 'ctbk', b: 1000, o: 0, c: [{ n: 'pyramid', b: 1000, o: 0 }] },
        { n: 'awair', b: 101, o: 1, c: [{ n: 'pyramid', b: 101, o: 1 }] },
      ] }, missing: [],
    })
    expect(reads.map(r => r.tree.n)).toEqual(['.', '.'])
  })
  it('missing source response produces partial totals, not a zero source node', () => {
    expect(composeFederatedTrees(view, [reads[0]])).toEqual({ tree: {
      n: 'All projects', k: 'dir', b: 101, o: 1,
      c: [{ n: 'awair', b: 101, o: 1, c: [{ n: 'pyramid', b: 101, o: 1 }] }],
    }, missing: ['ctbk'] })
    expect(composeFederatedTrees(resolveFederation(catalogs, t(7)), [])).toEqual({ tree: { n: 'All projects', k: 'dir', b: 0, o: 0, c: [] }, missing: ['awair', 'ctbk'] })
  })
  it('rejects wrong content or totals even when a source response is available', () => {
    expect(() => composeFederatedTrees(view, [{ ...reads[0], digest: 'f'.repeat(64) }])).toThrow('source tree does not match the pinned scan')
    expect(() => composeFederatedTrees(view, [{ ...reads[0], tree: { ...reads[0].tree, b: 99 } }])).toThrow('source tree does not match the pinned scan')
    expect(() => composeFederatedTrees(view, [{ ...reads[0], tree: { ...reads[0].tree, o: 99 } }])).toThrow('source tree does not match the pinned scan')
    expect(() => composeFederatedTrees(view, [reads[0], reads[0]])).toThrow('duplicate source tree response')
  })
  it('age rows retain absolute object dates, owner/access facets, and bin totals', () => {
    const rows = [{ d: 19000, d1: 'pyramid', b: 12, o: 2, u: 'ryan', a: 19500 }, { d: 19001, b: 5, o: 1 }]
    expect(mountFederatedAgeRows('awair', rows)).toEqual([
      { d: 19000, d1: 'awair/pyramid', b: 12, o: 2, u: 'ryan', a: 19500 },
      { d: 19001, d1: 'awair', b: 5, o: 1 },
    ])
    expect(rows).toEqual([{ d: 19000, d1: 'pyramid', b: 12, o: 2, u: 'ryan', a: 19500 }, { d: 19001, b: 5, o: 1 }])
  })
  it('source step values change only on their own observations, not another source event', () => {
    expect(federatedSeries(catalogs)).toEqual([
      { source: 'awair', points: [
        { at: t(8), bytes: 100, objects: 0 }, { at: t(9), bytes: 100, objects: 0 },
        { at: t(10), bytes: 101, objects: 1 }, { at: t(11), bytes: 101, objects: 1 },
      ] },
      { source: 'ctbk', points: [
        { at: t(8), bytes: null, objects: null }, { at: t(9), bytes: 1000, objects: 0 },
        { at: t(10), bytes: 1000, objects: 0 }, { at: t(11), bytes: 1001, objects: 1 },
      ] },
    ])
  })
  it('same-observation corrections use the same tie break as the cutoff resolver', () => {
    const original = catalogs[0].scans[0]
    const correction = { ...original, revision: '2', published_at: t(12), total_bytes: 500 }
    const c = { ...catalogs[0], scans: [correction, original] }
    expect(federatedSeries([c])).toEqual([{ source: 'awair', points: [{ at: t(8), bytes: 500, objects: 0 }] }])
    expect(resolveFederation([c], t(8)).sources[0].scan).toEqual(correction)
    expect(federatedSeries([])).toEqual([])
  })
})
