import { describe, expect, it } from 'vitest'
import { buildTree } from './diffModel'
import type { DiffData, DiffRow } from './diffModel'
import { deltaPct, diffTableRows, fmtPct, sortDiffRows } from './diffRows'
import type { DiffTableRow } from './diffRows'

const row = (p: string, s: DiffRow['s'], a: number, b: number, oa: number, ob: number, x?: true): DiffRow =>
  ({ p, d: p.split('/').length, k: 'dir', s, a, b, oa, ob, ...(x ? { x } : {}) })

const data = (rows: DiffRow[]): DiffData => ({
  prev: '2026-09-01', curr: '2026-09-02',
  total_a: rows.filter(r => r.d === 1).reduce((s, r) => s + r.a, 0),
  total_b: rows.filter(r => r.d === 1).reduce((s, r) => s + r.b, 0),
  objects_a: 0, objects_b: 0, expansions: 0, truncated: false, threshold: 0, lookups: 0, lookups_capped: false,
  rows,
})

// One drilled node's children as `/api/diff` emits them: a grown dir (expanded,
// its Δ carried by two children), a shrunk one, one added, one removed, one
// unchanged, and the `(other)` fold.
const ROWS: DiffRow[] = [
  row('ckpt', 'changed', 1000, 1500, 10, 12, true),
  row('ckpt/run-1', 'changed', 400, 900, 4, 6),
  row('ckpt/run-2', 'unchanged', 600, 600, 6, 6),
  row('logs', 'changed', 800, 200, 80, 20),
  row('new', 'added', 0, 300, 0, 3),
  row('gone', 'removed', 250, 0, 5, 0),
  row('static', 'unchanged', 700, 700, 7, 7),
  row('(other)', 'changed', 90, 100, 900, 950),
]

const strip = (rs: DiffTableRow[]) => rs.map(({ key, status, synthetic, segs, a, b, delta, pct, oa, ob, odelta }) =>
  ({ key, status, synthetic, segs, a, b, delta, pct, oa, ob, odelta }))

describe('diffTableRows', () => {
  it('lists the map’s cells (Δ mode: what moved) with before/after/Δ per side; the fold never drills', () => {
    const { cells } = buildTree(data(ROWS), 'delta', false)
    // Δ mode orders cells by |Δ| (their area); `static` (Δ 0) is not a cell.
    expect(strip(diffTableRows(cells))).toEqual([
      { key: 'logs', status: 'changed', synthetic: false, segs: ['logs'], a: 800, b: 200, delta: -600, pct: -0.75, oa: 80, ob: 20, odelta: -60 },
      { key: 'ckpt', status: 'changed', synthetic: false, segs: ['ckpt'], a: 1000, b: 1500, delta: 500, pct: 0.5, oa: 10, ob: 12, odelta: 2 },
      { key: 'new', status: 'added', synthetic: false, segs: ['new'], a: 0, b: 300, delta: 300, pct: null, oa: 0, ob: 3, odelta: 3 },
      { key: 'gone', status: 'removed', synthetic: false, segs: ['gone'], a: 250, b: 0, delta: -250, pct: -1, oa: 5, ob: 0, odelta: -5 },
      { key: '(other)', status: 'changed', synthetic: true, segs: [], a: 90, b: 100, delta: 10, pct: 10 / 90, oa: 900, ob: 950, odelta: 50 },
    ])
  })
  it('max mode also lists the unchanged directory, and a first-scanned root reads `first`', () => {
    const { cells } = buildTree(data(ROWS), 'max', true)
    expect(diffTableRows(cells).map(r => [r.key, r.status])).toEqual([
      ['ckpt', 'changed'],
      ['new', 'first'],
      ['(other)', 'changed'],
      ['static', 'unchanged'],
      ['gone', 'removed'],
      ['logs', 'changed'],
    ])
  })
  it('a nested child’s segments are its path below the diffed node', () => {
    const { cells } = buildTree(data(ROWS), 'delta', false)
    const kids = cells.find(c => c.key === 'ckpt')!.children!
    expect(diffTableRows(kids).map(r => [r.key, r.segs, r.status])).toEqual([
      ['ckpt/run-1', ['ckpt', 'run-1'], 'changed'],
      ['ckpt/run-2', ['ckpt', 'run-2'], 'unchanged'],
    ])
  })
})

describe('sortDiffRows', () => {
  const rows = diffTableRows(buildTree(data(ROWS), 'max', false).cells)
  const order = (k: Parameters<typeof sortDiffRows>[1], asc: boolean) => sortDiffRows(rows, k, asc).map(r => r.key)
  it('|Δ| descending is the default, then ascending; the sort is stable', () => {
    expect(order('delta', false)).toEqual(['logs', 'ckpt', 'new', 'gone', '(other)', 'static'])
    expect(order('delta', true)).toEqual(['static', '(other)', 'gone', 'new', 'ckpt', 'logs'])
  })
  it('before / after bytes sort by value', () => {
    expect(order('a', false)).toEqual(['ckpt', 'logs', 'static', 'gone', '(other)', 'new'])
    expect(order('b', true)).toEqual(['gone', '(other)', 'logs', 'new', 'static', 'ckpt'])
  })
  it('Δ% sorts by magnitude with no-before rows last either way', () => {
    expect(order('pct', false)).toEqual(['gone', 'logs', 'ckpt', '(other)', 'static', 'new'])
    expect(order('pct', true)).toEqual(['static', '(other)', 'ckpt', 'logs', 'gone', 'new'])
  })
  it('objects Δ sorts by magnitude; names A→Z; statuses in movement order', () => {
    expect(order('odelta', false)).toEqual(['logs', '(other)', 'gone', 'new', 'ckpt', 'static'])
    expect(order('name', true)).toEqual(['(other)', 'ckpt', 'gone', 'logs', 'new', 'static'])
    expect(order('status', true)).toEqual(['new', 'gone', 'ckpt', '(other)', 'logs', 'static'])
  })
  it('does not mutate its input', () => {
    const before = rows.map(r => r.key)
    sortDiffRows(rows, 'name', true)
    expect(rows.map(r => r.key)).toEqual(before)
  })
})

describe('deltaPct / fmtPct', () => {
  it('Δ over before; none when nothing was there', () => {
    expect(deltaPct(200, 250)).toBe(0.25)
    expect(deltaPct(200, 0)).toBe(-1)
    expect(deltaPct(0, 500)).toBeNull()
    expect(deltaPct(0, 0)).toBeNull()
  })
  it('signed, at a precision that suits the size', () => {
    expect(fmtPct(0.25)).toBe('+25.0%')
    expect(fmtPct(-1)).toBe('−100%')
    expect(fmtPct(0.0123)).toBe('+1.23%')
    expect(fmtPct(12.345)).toBe('+1.2k%')
    expect(fmtPct(0)).toBe('+0.00%')
  })
})
