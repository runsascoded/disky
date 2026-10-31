/** A Δ-mode diff from the interval store's transitions (specs/diff-from-changes.md): what changed under a path between
 *  two scans, read from the `changes` sort, so its cost is the changes under the path over the floor, not the path's
 *  size. Unscoped diffs only (no lens, owner pool, class scope or filter: they read as-of views, `buildDiff`).
 *
 *  1. Candidates: the transitions in `(from, to]` under P whose larger side is at least the floor (`readChanges`). A
 *     path with none can't be a cell: its size was under the floor at both scans, so its Δ is too.
 *  2. Endpoints: a candidate's `from` side is its first transition's old side, its `to` side its last one's new side.
 *     The earliest transition read is the first in the range when its old side is at least the floor (an earlier one
 *     would have that as its new side, and been read); the latest is the last when its new side is. Otherwise that
 *     side is read as of its scan (`readAsks` on `path`).
 *  3. The walk: as `buildDiff`'s, over the candidates only. An unchanged child has Δ 0 (no cell); a parent's `(other)`
 *     is its Δ less its drawn children's, i.e. the movement under the floor.
 *
 *  The floor is the Δ map's: `G · minArea / (w·h)`, `G` the root's drawn area (Σ of its cells' |Δ|, `diffRoot`). `G`
 *  is at least the Σ|Δ| of P's changed children, so reading at today's byte floor and lowering it to that bound (a
 *  second read) never misses a cell. */
import type { Env } from './auth.js'
import { asOfScans, openIndex, readAsks, readChanges, type Trace, type Transition, TooWide } from './index.js'
import { type Diff, type DiffRow, NotFound, readRootAgg } from './view.js'

const parentOf = (p: string): string => { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i) }

export interface ChangesOpts {
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
}

/** A side's total. */
type Side = { b: number; o: number } | null
interface Cand { kind: 'file' | 'dir'; a: Side; b: Side; d: number }

/** Floors tried at most: the byte floor, then the bound from P's changed children (and, while that bound is 0, a
 *  sixteenth at a time). */
const FLOOR_STEPS = 6
/** Asks per `readAsks` call of the endpoint lookups. */
const ASK_CHUNK = 64

/** Each path's total as of `date` (absent: null), by point lookups on its `path` sort. */
async function asOf(env: Env, date: string, paths: { path: string; depth: number }[]): Promise<Map<string, Side>> {
  const out = new Map<string, Side>()
  if (!paths.length) return out
  const h = await openIndex(env, date, 'path')
  for (let i = 0; i < paths.length; i += ASK_CHUNK) {
    const chunk = paths.slice(i, i + ASK_CHUNK)
    const want = new Set(chunk.map(p => p.path))
    const { rows } = await readAsks(h, chunk, r => want.has(r.path), { maxGroups: 120 })
    for (const p of chunk) out.set(p.path, null)
    for (const r of rows) {
      const s = out.get(r.path)
      out.set(r.path, { b: (s?.b ?? 0) + r.size, o: (s?.o ?? 0) + r.n_files })
    }
  }
  return out
}

/** The candidates over the floor at their depth (`thrAt`) with both sides (step 2), and the lookups that took. */
async function candidates(env: Env, o: ChangesOpts, floor: number): Promise<{ cands: Map<string, Cand>; lookups: number; groups: number } | null> {
  const dP = o.path === '' ? 0 : o.path.split('/').length
  const thrAt = (depth: number) => floor * o.atten ** Math.max(0, depth - dP - 1)
  const got = await readChanges(env, o.from, o.to, o.path, floor, { trace: o.trace })
  if (!got) return null
  const byPath = new Map<string, Transition[]>()
  for (const t of got.rows) {
    if (Math.max(t.size ?? 0, t.sizePrev ?? 0) < thrAt(t.depth)) continue
    const l = byPath.get(t.path)
    if (l) l.push(t)
    else byPath.set(t.path, [t])
  }
  const cands = new Map<string, Cand>()
  const askA: { path: string; depth: number }[] = []
  const askB: { path: string; depth: number }[] = []
  for (const [p, ts] of byPath) {
    ts.sort((x, y) => x.b - y.b)
    const first = ts[0]
    const last = ts[ts.length - 1]
    const a: Side = first.sizePrev == null ? null : { b: first.sizePrev, o: first.nFilesPrev ?? 0 }
    const b: Side = last.size == null ? null : { b: last.size, o: last.nFiles ?? 0 }
    cands.set(p, { kind: last.size != null ? last.kind : first.kind, a, b, d: first.depth })
    const f = thrAt(first.depth)
    if (!(first.sizePrev != null && first.sizePrev >= f)) askA.push({ path: p, depth: first.depth })
    if (!(last.size != null && last.size >= f)) askB.push({ path: p, depth: last.depth })
  }
  const [la, lb] = await Promise.all([asOf(env, o.from, askA), asOf(env, o.to, askB)])
  for (const [p, s] of la) cands.get(p)!.a = s
  for (const [p, s] of lb) cands.get(p)!.b = s
  // A transition the lookups undo (the path is the same at both scans: it changed and changed back) is no change.
  for (const [p, c] of cands) if (c.a && c.b && c.a.b === c.b.b && c.a.o === c.b.o) cands.delete(p)
  return { cands, lookups: askA.length + askB.length, groups: got.groups }
}

const absDelta = (c: { a: Side; b: Side }): number => Math.abs((c.b?.b ?? 0) - (c.a?.b ?? 0))

export async function buildDiffChanges(env0: Env, o: ChangesOpts): Promise<Diff> {
  const env = asOfScans(env0, [o.from, o.to])
  const { path } = o
  const dP = path === '' ? 0 : path.split('/').length
  const [ra0, rb0] = await Promise.all([readRootAgg(env, { date: o.from, path }), readRootAgg(env, { date: o.to, path })])
  if (!ra0 && !rb0) throw new NotFound(path)
  // A side with no bytes under P is none, as `buildDiff`'s views read it (its objects, zero-byte ones, aren't drawn).
  const [ra, rb] = [ra0?.b ? ra0 : null, rb0?.b ? rb0 : null]
  const k = o.minArea / (o.w * o.h)
  let floor = Math.max(ra?.b ?? 0, rb?.b ?? 0) * k
  let got: Awaited<ReturnType<typeof candidates>> | undefined
  let truncated = false
  for (let step = 0; step < FLOOR_STEPS; step++) {
    let next: Awaited<ReturnType<typeof candidates>>
    try {
      next = await candidates(env, o, floor)
    } catch (e) {
      // A lower floor too wide to read: the last floor's answer stands, cut (`truncated`).
      if (e instanceof TooWide && got) { truncated = true; break }
      throw e
    }
    if (!next) throw new NotFound(path)
    got = next
    const bound = [...got.cands].reduce((s, [p, c]) => s + (parentOf(p) === path ? absDelta(c) : 0), 0) * k
    if (bound >= floor || floor < 1) break
    floor = bound > 0 ? bound : floor / 16
  }
  const { cands, lookups } = got!
  // A changed path under an unchanged parent (Δ 0: a move between its children) is not reached: its parent is one
  // unchanged cell, as the as-of walk draws it.
  const kids = new Map<string, string[]>()
  for (const p of [...cands.keys()].sort()) {
    const q = parentOf(p)
    if (q !== path && !cands.has(q)) continue
    const l = kids.get(q)
    if (l) l.push(p)
    else kids.set(q, [p])
  }
  const rows: DiffRow[] = []
  let expansions = 0
  const rel = (p: string) => (path === '' ? p : p.slice(path.length + 1))
  const status = (a: Side, b: Side): DiffRow['s'] =>
    !a ? 'added' : !b ? 'removed' : Math.round(a.b) !== Math.round(b.b) || Math.round(a.o) !== Math.round(b.o) ? 'changed' : 'unchanged'
  const emit = (p: string, d: number, k1: 'file' | 'dir', a: Side, b: Side, x: boolean) => rows.push({
    p, d, k: k1, s: status(a, b), a: Math.round(a?.b ?? 0), b: Math.round(b?.b ?? 0), oa: Math.round(a?.o ?? 0), ob: Math.round(b?.o ?? 0), ...(x ? { x: true } : {}),
  })
  let level: { p: string; d: number; k: 'file' | 'dir'; a: Side; b: Side }[] = [{ p: path, d: dP, k: 'dir', a: ra, b: rb }]
  while (level.length) {
    const next: typeof level = []
    for (const it of level) {
      const names = kids.get(it.p) ?? []
      const same = !!(it.a && it.b && Math.round(it.a.b) === Math.round(it.b.b) && Math.round(it.a.o) === Math.round(it.b.o))
      const expand = !!((it.a || it.b) && !same && names.length) && (o.depth == null || it.d - dP < o.depth)
      if (it.p !== path) emit(rel(it.p), it.d - dP, it.k, it.a, it.b, expand)
      if (!expand) continue
      expansions++
      const sum = { a: { b: 0, o: 0 }, b: { b: 0, o: 0 } }
      for (const cp of names) {
        const c = cands.get(cp)!
        if (c.a && it.a) { sum.a.b += c.a.b; sum.a.o += c.a.o }
        if (c.b && it.b) { sum.b.b += c.b.b; sum.b.o += c.b.o }
        // A side the parent is absent on holds none of its children (the as-of walk's reading; in a scan's rows a
        // path's descendants exist only with it).
        next.push({ p: cp, d: it.d + 1, k: c.kind, a: it.a ? c.a : null, b: it.b ? c.b : null })
      }
      const restA = { b: Math.max(0, (it.a?.b ?? 0) - sum.a.b), o: Math.max(0, (it.a?.o ?? 0) - sum.a.o) }
      const restB = { b: Math.max(0, (it.b?.b ?? 0) - sum.b.b), o: Math.max(0, (it.b?.o ?? 0) - sum.b.o) }
      if (restA.b > 0 || restB.b > 0) emit(it.p === path ? '(other)' : `${rel(it.p)}/(other)`, it.d - dP + 1, 'dir', restA.b > 0 ? restA : null, restB.b > 0 ? restB : null, false)
    }
    level = next
  }
  const skeleton = rows.filter(r => r.x)
  const frontier = rows.filter(r => !r.x && r.s !== 'unchanged').sort((r1, r2) => Math.abs(r2.b - r2.a) - Math.abs(r1.b - r1.a))
  return {
    rows: [...skeleton, ...frontier.slice(0, o.top)],
    total_a: Math.round(ra?.b ?? 0),
    total_b: Math.round(rb?.b ?? 0),
    objects_a: Math.round(ra?.o ?? 0),
    objects_b: Math.round(rb?.o ?? 0),
    threshold: floor,
    tier: 'changes',
    expansions,
    truncated: truncated || frontier.length > o.top,
    lookups,
    lookups_capped: false,
  }
}
