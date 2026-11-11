import { describe, expect, it } from 'vitest'
import { decodeSection, encodeSection, migrateSection, sectionParam } from './homeSections'

describe('the section param', () => {
  it('names each section id, and reads a name, a spelled-out id, a one-letter code or a retired anchor back', () => {
    const ids = ['tree-map', 'tbl', 'over-time', 'diff', 'mtime', 'runs', 'batch-12']
    expect(ids.map(encodeSection)).toEqual(['map', 'table', 'time', 'diff', 'mtime', 'runs', 'batch-12'])
    expect(ids.map(id => decodeSection(encodeSection(id)))).toEqual(ids)
    expect(['diff', 'changes', 'batch-3', 'size-over-time', 'o', 'm', 'b12'].map(decodeSection)).toEqual(['diff', 'diff', 'batch-3', 'over-time', 'over-time', 'tree-map', 'batch-12'])
  })
  it('`?v=` first, else the legacy `?section=`; null when neither', () => {
    expect(['v=o', 'section=diff', 'v=d&section=over-time', 'd=261002'].map(q => sectionParam(new URLSearchParams(q)))).toEqual(['over-time', 'diff', 'diff', null])
  })
  it('rewrites a legacy `section=` pair in place, the other pairs as written; null when there is none', () => {
    expect([
      '?d=26111100-1d&section=over-time',
      '?section=batch-12&x=*_7',
      '?q=a%7Cb&section=changes&v=o',
      '?section=',
      '?d=261002&v=d',
    ].map(migrateSection)).toEqual(['?d=26111100-1d&v=time', '?v=batch-12&x=*_7', '?q=a%7Cb&v=o', '', null])
  })
})
