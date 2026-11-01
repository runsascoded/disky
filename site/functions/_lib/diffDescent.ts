/** A Δ-mode diff by lazy descent over the interval store (specs/diff-from-changes.md, revision 3): P's children at both
 *  scans, then the children of the children whose net Δ can hold a cell, level by level. Reads only the `path` sort,
 *  so it needs no index beyond the store's; its cost follows the cells drawn (each expanded directory's children),
 *  not P's size nor the churn elsewhere. Unscoped diffs only, as `buildDiffChanges`.
 *
 *  A level reads, for every directory it expands, its children at both scans as `path` ranges (`readRects`), groups
 *  pruned by the floor at that depth. A child read on one side only is either absent on the other or under the floor
 *  there: its version says which when it is live at both scans (`vf ≤ D1`, `vt > D2`: the same row), else that
 *  parent's children are read again on that side with no floor (a contiguous range, never a point lookup).
 *
 *  A directory expands when it changed and its net |Δ| is at least the floor of its children's depth: a smaller Δ
 *  can't hold a drawn cell (its area is under one), and moves inside an unchanged directory aren't opened (the
 *  as-of walk's contract). `(other)` rows come from the shared tail (`capDiffRows`: each expanded row less its drawn
 *  children). The floor is the Δ map's, `G · minArea / (w·h)`: read P's children at `|Δ P| · k` (a lower
 *  bound: `G ≥ |Δ P|`), then use `Σ |Δ child| · k` below (`G ≥` that too). */
import type { Env } from './auth.js'
import { asOfScans, ChangesUnavailable, type IndexHandle, openIndex, readRects, type Rect, type Row } from './index.js'
import { capDiffRows, type Diff, type DiffRow, NotFound, readRootAgg } from './view.js'
import type { ChangesOpts } from './diffChanges.js'

type Side = { b: number; o: number; kind: 'file' | 'dir'; vf?: number; vt?: number } | null

const childRect = (p: string, depth: number): Rect =>
  ({ dLo: depth, dHi: depth, pLo: p ? `${p}/` : '', pHi: p ? `${p}0` : '￿' })
const parentOf = (p: string): string => { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i) }

/** Each path's row (one per path in the folded `path` sort; slices summed if a sort has several). */
function bySide(rows: Row[]): Map<string, Side> {
  const m = new Map<string, Side>()
  for (const r of rows) {
    const s = m.get(r.path)
    m.set(r.path, s ? { ...s, b: s.b + r.size, o: s.o + r.n_files } : { b: r.size, o: r.n_files, kind: r.kind, vf: r.vf, vt: r.vt })
  }
  return m
}

export async function buildDiffDescent(env0: Env, o: ChangesOpts): Promise<Diff> {
  const env = asOfScans(env0, [o.from, o.to])
  const { path } = o
  const dP = path === '' ? 0 : path.split('/').length
  const [ra0, rb0] = await Promise.all([readRootAgg(env, { date: o.from, path }), readRootAgg(env, { date: o.to, path })])
  if (!ra0 && !rb0) throw new NotFound(path)
  const ra: Side = ra0?.b ? { ...ra0, kind: 'dir' } : null
  const rb: Side = rb0?.b ? { ...rb0, kind: 'dir' } : null
  const [hA, hB] = await Promise.all([openIndex(env, o.from, 'path'), openIndex(env, o.to, 'path')])
  const [D1, D2] = [hA.asOf, hB.asOf]
  if (D1 == null || D2 == null) throw new ChangesUnavailable('a per-scan store', 'a descent diff reads the interval store; one scan is per-scan')
  const k = o.minArea / (o.w * o.h)
  const dlt = (a: Side, b: Side) => Math.abs((b?.b ?? 0) - (a?.b ?? 0))
  let floor = dlt(ra, rb) * k
  const thrAt = (depth: number) => floor * o.atten ** Math.max(0, depth - dP - 1)
  const same = (a: Side, b: Side) => !!(a && b && Math.round(a.b) === Math.round(b.b) && Math.round(a.o) === Math.round(b.o))

  /** One level: the children of `parents` at both scans (floored), then unfloored where a side is unknown. */
  const level = async (parents: string[], depth: number) => {
    const rects = parents.map(p => childRect(p, depth))
    const thr = thrAt(depth)
    const over = (r: Row) => r.size >= thr
    const [rowsA, rowsB] = await Promise.all([readRects(hA, rects, thrAt), readRects(hB, rects, thrAt)])
    const A = bySide(rowsA.filter(over))
    const B = bySide(rowsB.filter(over))
    // A side a child wasn't read on: the same row when its version spans both scans, else read that parent unfloored.
    const redoA = new Set<string>(), redoB = new Set<string>()
    for (const [p, s] of B) if (!A.has(p)) { if (s!.vf! <= D1) A.set(p, s); else redoA.add(parentOf(p)) }
    for (const [p, s] of A) if (!B.has(p)) { if (s!.vt! > D2) B.set(p, s); else redoB.add(parentOf(p)) }
    const [moreA, moreB] = await Promise.all([
      redoA.size ? readRects(hA, [...redoA].map(q => childRect(q, depth))) : Promise.resolve([] as Row[]),
      redoB.size ? readRects(hB, [...redoB].map(q => childRect(q, depth))) : Promise.resolve([] as Row[]),
    ])
    const fullA = bySide(moreA), fullB = bySide(moreB)
    const names = new Set([...A.keys(), ...B.keys()])
    const out = new Map<string, { a: Side; b: Side }>()
    for (const p of names) {
      const a = A.get(p) ?? (redoA.has(parentOf(p)) ? fullA.get(p) ?? null : null)
      const b = B.get(p) ?? (redoB.has(parentOf(p)) ? fullB.get(p) ?? null : null)
      out.set(p, { a, b })
    }
    return { out, reads: 2 + (redoA.size ? 1 : 0) + (redoB.size ? 1 : 0) }
  }

  const rows: DiffRow[] = []
  let expansions = 0
  let levels = 0
  const rel = (p: string) => (path === '' ? p : p.slice(path.length + 1))
  const status = (a: Side, b: Side): DiffRow['s'] =>
    !a ? 'added' : !b ? 'removed' : Math.round(a.b) !== Math.round(b.b) || Math.round(a.o) !== Math.round(b.o) ? 'changed' : 'unchanged'
  const emit = (p: string, d: number, k1: 'file' | 'dir', a: Side, b: Side, x: boolean) => rows.push({
    p, d, k: k1, s: status(a, b), a: Math.round(a?.b ?? 0), b: Math.round(b?.b ?? 0), oa: Math.round(a?.o ?? 0), ob: Math.round(b?.o ?? 0), ...(x ? { x: true } : {}),
  })
  type Item = { p: string; d: number; k: 'file' | 'dir'; a: Side; b: Side }
  let frontier: Item[] = (ra || rb) && !same(ra, rb) ? [{ p: path, d: dP, k: 'dir', a: ra, b: rb }] : []
  while (frontier.length && (o.depth == null || frontier[0].d - dP < o.depth)) {
    const depth = frontier[0].d + 1
    const t0 = performance.now()
    const { out, reads } = await level(frontier.map(it => it.p), depth)
    levels++
    o.trace?.(`L${depth - dP}`, performance.now() - t0, `${frontier.length} parents, ${out.size} children, ${reads} reads`)
    if (depth === dP + 1) {
      // The floor from P's children: `G ≥ Σ |Δ child|`.
      floor = Math.max(floor, [...out.values()].reduce((s, c) => s + dlt(c.a, c.b), 0) * k)
    }
    const kids = new Map<string, string[]>()
    for (const p of [...out.keys()].sort()) {
      const q = parentOf(p)
      const l = kids.get(q)
      if (l) l.push(p)
      else kids.set(q, [p])
    }
    const next: Item[] = []
    for (const it of frontier) {
      const names = kids.get(it.p) ?? []
      if (it.p !== path) emit(rel(it.p), it.d - dP, it.k, it.a, it.b, !!names.length)
      if (!names.length) continue
      expansions++
      for (const cp of names) {
        const c = out.get(cp)!
        // A side the parent is absent on holds none of its children.
        const a = it.a ? c.a : null, b = it.b ? c.b : null
        const kind = b?.kind ?? a?.kind ?? 'dir'
        const deeper = kind === 'dir' && !same(a, b) && dlt(a, b) >= thrAt(depth + 1) && (o.depth == null || depth - dP < o.depth)
        if (deeper) next.push({ p: cp, d: depth, k: kind, a, b })
        else emit(rel(cp), depth - dP, kind, a, b, false)
      }
    }
    frontier = next
  }
  // Directories at the depth cap: drawn as leaves.
  for (const it of frontier) if (it.p !== path) emit(rel(it.p), it.d - dP, it.k, it.a, it.b, false)
  const cut = capDiffRows(rows, o.top, { a: Math.round(ra?.b ?? 0), b: Math.round(rb?.b ?? 0), oa: Math.round(ra?.o ?? 0), ob: Math.round(rb?.o ?? 0) })
  return {
    rows: cut.rows,
    total_a: Math.round(ra?.b ?? 0),
    total_b: Math.round(rb?.b ?? 0),
    objects_a: Math.round(ra?.o ?? 0),
    objects_b: Math.round(rb?.o ?? 0),
    threshold: floor,
    tier: 'descent',
    expansions,
    truncated: cut.cut,
    lookups: 0,
    lookups_capped: false,
  }
}
