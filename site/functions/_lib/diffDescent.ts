/** A Δ-mode diff by lazy descent over the interval store (specs/diff-from-changes.md, revision 3): P's children at both
 *  scans, then the children of the children whose net Δ can hold a cell, level by level. Reads only the `path` sort,
 *  so it needs no index beyond the store's; its cost follows the cells drawn (each expanded directory's children),
 *  not P's size nor the churn elsewhere. Unscoped diffs only (no lens, owner pool, class scope or filter: `buildDiff`).
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
import { asOfScans, DiffUnavailable, type IndexHandle, openIndex, planRects, readRects, type Rect, type Row, type Span, type Trace } from './index.js'
import { capDiffRows, type Diff, type DiffRow, NotFound, readRootAgg } from './view.js'

type Side = { b: number; o: number; kind: 'file' | 'dir'; vf?: number; vt?: number } | null

/** The descent's answer version, in its edge-cache key (`&dm=d<v>`, `api/diff.ts`): bumped with every change to what
 *  it answers, or to which reader answers (a handoff's walk body differs from a descent's), so a deploy never serves an
 *  earlier descent's cached body (a global `CACHE_V` bump would make every view and diff cold everywhere). 2: best
 *  first, stopped at `top`, `expansions_skipped`. 3: the planner's handoff. 4: the time and work budgets' handoffs
 *  (r2's first rerun served pre-budget descent bodies from KV at its earlier canvases). */
export const DESCENT_V = 4

/** The planner (specs/diff-from-changes.md revision 3): the descent continues past P's children only when at most this
 *  many of them need expanding (changed, |Δ| over their children's floor) — the change is concentrated, and the descent
 *  reads a few ranges where the as-of walk reads P's whole subtree. More, and it hands off to the walk (`HandOff`),
 *  which reuses nothing of it. Set from gcs dev (18 cases: at ≤ 1 the descent took 0.2–0.8 s against the walk's
 *  1.4–3.0 s; at ≥ 4, 0.3–19 s against 1.8–3.5 s), not per deployment. */
export const DESCENT_MAX_EXPANDABLE = 2

/** The running-cost bail-out: past this many ms since it started (checked before each round, and a round in flight is
 *  raced against it), the descent hands off to the walk, so its worst case is about this plus the walk. It sees I/O
 *  only (a Worker's clock doesn't advance during CPU: `DESCENT_BUDGET_ROWS` covers that), so it catches many cheap
 *  rounds: cw's `marin-us-east-08a`, 13 rounds of 9–287 ms, ~960 ms on this clock at the end (2.5 s wall against the
 *  walk's 1.3 s), never reached 1,000. Every kept case faster than its walk ends well under 500: cw ≤ 322 ms server
 *  total, gcs ≤ 324, hf ≤ 524 wall. Near it, which reader answers depends on timing: both answers are exact, their
 *  drawn rows may differ, and an edge-cached body is whichever came first. */
export const DESCENT_BUDGET_MS = 500

/** The work bail-out: rows the descent's reads have returned (both sides, unfloored re-reads included), checked after
 *  each read; and before each read, those plus the rows its planned groups hold (from the footers, nothing fetched:
 *  Server-Timing `descent-planned`), so a read that would pass it isn't issued. A Worker's clock advances only across
 *  I/O, so `DESCENT_BUDGET_MS` never sees the descent's CPU — decoding and folding these rows (r2's `marin-na`: 1.44 s
 *  of Server-Timing against 3.7 s wall). Calibration (`descent-rows`): hf's kept cases read at most 4,682 (10-15→10-31
 *  `/buckets`, growing about a scan a day; ~1.7× headroom); r2's `marin-na` ≥ 9,261 (its L4 alone, in children); gcs's
 *  `checkpoints` over a week 35,708 (2.8 s against its walk's 0.5 s). Both of those trip; 10,000 would put r2 at the edge. */
export const DESCENT_BUDGET_ROWS = 8000

/** The descent declines: `breadth`, the planner (`DESCENT_MAX_EXPANDABLE`, `detail` = P's children to expand); `budget`,
 *  its elapsed time (`DESCENT_BUDGET_MS`, `detail` = ms); `work`, the rows it read (`DESCENT_BUDGET_ROWS`, `detail` =
 *  rows). `buildDiff` answers with the as-of walk. */
export class HandOff extends Error {
  constructor(readonly why: 'breadth' | 'budget' | 'work', readonly detail: number) {
    super(why === 'breadth' ? `descent hands off: ${detail} of P's children to expand`
      : why === 'work' ? `descent hands off: ${detail} rows read` : `descent hands off: over its budget after ${Math.round(detail)} ms`)
    this.name = 'HandOff'
  }
}

const BUDGET = Symbol('budget')

/** Directories one round expands at most (largest |Δ| first). */
const ROUND_PARENTS = 64

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

export interface DescentOpts {
  from: string
  to: string
  path: string
  w: number
  h: number
  minArea: number
  /** The floor's growth per level below P's children (`thrAt`, as the as-of reads attenuate it). */
  atten: number
  top: number
  depth?: number
  trace?: Trace
  /** Never hand off to the walk (tests of the descent itself). */
  noHandOff?: boolean
  /** A fixed floor for P's children instead of `|Δ P| · minArea / (w·h)`: a union root's common threshold. */
  threshold?: number
  /** The running-cost budget (`DESCENT_BUDGET_MS` by default; tests of the planner alone pass `Infinity`). */
  budgetMs?: number
  /** The work budget (`DESCENT_BUDGET_ROWS` by default; tests of the planner alone pass `Infinity`). */
  budgetRows?: number
}

export async function buildDiffDescent(env0: Env, o: DescentOpts): Promise<Diff> {
  const tStart = performance.now()
  // Rows the reads returned (`DESCENT_BUDGET_ROWS`), reported as Server-Timing `descent-rows` (its dur is the count).
  let rowsRead = 0
  const budgetRows = o.budgetRows ?? DESCENT_BUDGET_ROWS
  const spend = (n: number) => {
    rowsRead += n
    if (o.noHandOff || rowsRead <= budgetRows) return
    o.trace?.('descent-rows', rowsRead, `${rowsRead} rows read`)
    o.trace?.('handoff', performance.now() - tStart, `work: ${rowsRead} rows read (max ${budgetRows})`)
    throw new HandOff('work', rowsRead)
  }
  // Before a read: the rows its planned groups hold (from the footers, nothing fetched yet), which bound what it decodes.
  // Over the budget with them, the descent hands off before paying for the read (Server-Timing `descent-planned`).
  let rowsPlanned = 0
  const afford = (spans: Span[]) => {
    const n = spans.reduce((x, s) => x + s.rowEnd - s.rowStart, 0)
    rowsPlanned += n
    if (o.noHandOff || rowsRead + n <= budgetRows) return
    o.trace?.('descent-rows', rowsRead, `${rowsRead} rows read`)
    o.trace?.('descent-planned', rowsPlanned, `${rowsPlanned} rows planned`)
    o.trace?.('handoff', performance.now() - tStart, `work: ${rowsRead} rows read + ${n} planned (max ${budgetRows})`)
    throw new HandOff('work', rowsRead + n)
  }
  const env = asOfScans(env0, [o.from, o.to])
  const { path } = o
  const dP = path === '' ? 0 : path.split('/').length
  const [ra0, rb0] = await Promise.all([readRootAgg(env, { date: o.from, path }), readRootAgg(env, { date: o.to, path })])
  if (!ra0 && !rb0) throw new NotFound(path)
  const ra: Side = ra0?.b ? { ...ra0, kind: 'dir' } : null
  const rb: Side = rb0?.b ? { ...rb0, kind: 'dir' } : null
  const [hA, hB] = await Promise.all([openIndex(env, o.from, 'path'), openIndex(env, o.to, 'path')])
  const [D1, D2] = [hA.asOf, hB.asOf]
  if (D1 == null || D2 == null) throw new DiffUnavailable('a per-scan store', 'a descent diff reads the interval store; one scan is per-scan')
  const k = o.minArea / (o.w * o.h)
  const dlt = (a: Side, b: Side) => Math.abs((b?.b ?? 0) - (a?.b ?? 0))
  let floor = o.threshold ?? dlt(ra, rb) * k
  const thrAt = (depth: number) => floor * o.atten ** Math.max(0, depth - dP - 1)
  const same = (a: Side, b: Side) => !!(a && b && Math.round(a.b) === Math.round(b.b) && Math.round(a.o) === Math.round(b.o))

  /** One round: the children of `parents` (each at its own depth) at both scans, each depth at its floor, then
   *  unfloored where a side is unknown. */
  const level = async (parents: { p: string; d: number }[]) => {
    const depthOf = new Map(parents.map(x => [x.p, x.d + 1]))
    const rects = parents.map(x => childRect(x.p, x.d + 1))
    const over = (r: Row) => r.size >= thrAt(r.depth)
    const [planA, planB] = await Promise.all([planRects(hA, rects, thrAt), planRects(hB, rects, thrAt)])
    afford([...planA, ...planB])
    const [rowsA, rowsB] = await Promise.all([readRects(hA, rects, thrAt, undefined, planA), readRects(hB, rects, thrAt, undefined, planB)])
    spend(rowsA.length + rowsB.length)
    const A = bySide(rowsA.filter(over))
    const B = bySide(rowsB.filter(over))
    // A side a child wasn't read on: the same row when its version spans both scans, else read that parent unfloored.
    const redoA = new Set<string>(), redoB = new Set<string>()
    for (const [p, s] of B) if (!A.has(p)) { if (s!.vf! <= D1) A.set(p, s); else redoA.add(parentOf(p)) }
    for (const [p, s] of A) if (!B.has(p)) { if (s!.vt! > D2) B.set(p, s); else redoB.add(parentOf(p)) }
    const redoRects = (s: Set<string>) => [...s].map(q => childRect(q, depthOf.get(q)!))
    const [moreRA, moreRB] = [redoRects(redoA), redoRects(redoB)]
    const [morePA, morePB] = await Promise.all([moreRA.length ? planRects(hA, moreRA) : [], moreRB.length ? planRects(hB, moreRB) : []])
    afford([...morePA, ...morePB])
    const [moreA, moreB] = await Promise.all([
      moreRA.length ? readRects(hA, moreRA, undefined, undefined, morePA) : Promise.resolve([] as Row[]),
      moreRB.length ? readRects(hB, moreRB, undefined, undefined, morePB) : Promise.resolve([] as Row[]),
    ])
    spend(moreA.length + moreB.length)
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
  // Best first: each round expands the frontier's largest |Δ| first (at most `ROUND_PARENTS`), until the changed rows
  // drawn would fill `top` (the client draws no more) or nothing over its children's floor is left. What is left on
  // the frontier is drawn as a leaf (its bytes counted; the tail's `(other)` keeps every parent exact).
  let frontier: Item[] = (ra || rb) && !same(ra, rb) ? [{ p: path, d: dP, k: 'dir', a: ra, b: rb }] : []
  let drawnChanged = 0
  const expandable = (it: Item) => o.depth == null || it.d - dP < o.depth
  while (frontier.length && drawnChanged < o.top) {
    frontier.sort((x, y) => dlt(y.a, y.b) - dlt(x.a, x.b) || (x.p < y.p ? -1 : 1))
    const take = frontier.filter(expandable).slice(0, ROUND_PARENTS)
    if (!take.length) break
    const t0 = performance.now()
    const budget = o.budgetMs ?? DESCENT_BUDGET_MS
    if (!o.noHandOff && t0 - tStart >= budget) {
      o.trace?.('handoff', t0 - tStart, `budget: before L${levels + 1}, ${budget} ms`)
      throw new HandOff('budget', t0 - tStart)
    }
    const round = level(take)
    let timer: ReturnType<typeof setTimeout> | undefined
    const res = o.noHandOff || budget === Infinity ? await round : await Promise.race([
      round,
      new Promise<typeof BUDGET>(r => { timer = setTimeout(() => r(BUDGET), Math.max(0, budget - (t0 - tStart))) }),
    ]).finally(() => { if (timer !== undefined) clearTimeout(timer) })
    if (res === BUDGET) {
      // The round goes on unawaited; its reads are dropped (a rejection too).
      round.catch(() => {})
      const spent = performance.now() - tStart
      o.trace?.('handoff', spent, `budget: L${levels + 1} over ${budget} ms`)
      throw new HandOff('budget', spent)
    }
    const { out, reads } = res
    levels++
    o.trace?.(`L${levels}`, performance.now() - t0, `${take.length} parents, ${out.size} children, ${reads} reads`)
    if (levels === 1) {
      // The floor from P's children: `G ≥ Σ |Δ child|`.
      if (o.threshold === undefined) floor = Math.max(floor, [...out.values()].reduce((s, c) => s + dlt(c.a, c.b), 0) * k)
      // The planner: how many of P's children would expand (as below), against `DESCENT_MAX_EXPANDABLE`.
      const expandable = [...out.values()].filter(c => {
        const kind = c.b?.kind ?? c.a?.kind ?? 'dir'
        return kind === 'dir' && !same(c.a, c.b) && dlt(c.a, c.b) >= thrAt(dP + 2)
      }).length
      // Server-Timing `plan`: the descent's time up to the decision (what a handoff spent), the count in its desc.
      o.trace?.('plan', performance.now() - tStart, `${expandable} to expand (max ${DESCENT_MAX_EXPANDABLE})`)
      if (expandable > DESCENT_MAX_EXPANDABLE && !o.noHandOff) {
        o.trace?.('handoff', performance.now() - tStart, `breadth: ${expandable} to expand`)
        throw new HandOff('breadth', expandable)
      }
    }
    const kids = new Map<string, string[]>()
    for (const p of [...out.keys()].sort()) {
      const q = parentOf(p)
      const l = kids.get(q)
      if (l) l.push(p)
      else kids.set(q, [p])
    }
    const taken = new Set(take.map(it => it.p))
    const next: Item[] = frontier.filter(it => !taken.has(it.p))
    for (const it of take) {
      const names = kids.get(it.p) ?? []
      if (it.p !== path) emit(rel(it.p), it.d - dP, it.k, it.a, it.b, !!names.length)
      if (!names.length) continue
      expansions++
      for (const cp of names) {
        const c = out.get(cp)!
        // A side the parent is absent on holds none of its children.
        const a = it.a ? c.a : null, b = it.b ? c.b : null
        const kind = b?.kind ?? a?.kind ?? 'dir'
        const depth = it.d + 1
        if (kind === 'dir' && !same(a, b) && dlt(a, b) >= thrAt(depth + 1)) next.push({ p: cp, d: depth, k: kind, a, b })
        else {
          emit(rel(cp), depth - dP, kind, a, b, false)
          if (!same(a, b)) drawnChanged++
        }
      }
    }
    frontier = next
  }
  // The frontier left (the budget, the depth cap): drawn as leaves. Those the budget stopped (not the cap) are counted:
  // the answer is cut, not complete.
  const skipped = frontier.filter(expandable).length
  for (const it of frontier) if (it.p !== path) emit(rel(it.p), it.d - dP, it.k, it.a, it.b, false)
  o.trace?.('descent-rows', rowsRead, `${rowsRead} rows read`)
  o.trace?.('descent-planned', rowsPlanned, `${rowsPlanned} rows planned`)
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
    truncated: cut.cut || skipped > 0,
    ...(skipped ? { expansions_skipped: skipped } : {}),
    lookups: 0,
    lookups_capped: false,
  }
}
