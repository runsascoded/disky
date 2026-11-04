/** A progressive diff's level request (specs/diff-progressive.md §1, step 2): each asked directory's changed children
 *  between two scans, sized by their range churn, read from the interval store's versions that change in the range.
 *
 *  Only the `path` sort's groups that can hold a version opening or closing in `(D1, D2]` are read (`planChanged`), so
 *  an unchanged child is never read: its bytes are the directory's less its changed children's (`unchanged`). A
 *  child's range churn is `c2 − c1`, its cumulative churn (`churn_cum` / `churn_end`) at both ends, from those versions
 *  alone:
 *  - `c1`: its version live at D1's `churn_cum`; absent at D1, its first version in the range's `churn_cum` less its
 *    size (it appeared there, every byte of it new: what that scan's churn added);
 *  - `c2`: its version live at D2's `churn_cum`; absent at D2, its last version's `churn_end`.
 *  So the request needs D1 at or after the generation's churn base (`ivChurnFrom`): before it, nothing counted the
 *  appearance.
 *
 *  Each directory's answer depends on (generation, D1, D2, the directory, K, the measure) only, never the canvas: the
 *  endpoint keeps one edge-cache entry per directory (`api/diff/level.ts`). A batch of directories (a level) is read
 *  together, each group decoded once. A directory whose only changed child is a directory is followed into it in the
 *  same request (`next`), a round of reads per link, so a deep single change isn't a request per level.
 *
 *  Where the generation and every run holding a scan of the range carry the level index (`interval_level.py`,
 *  `openIvLevel`), a round reads it instead (`levelRound`): each asked directory's transitions in `(D1, D2]`, one key
 *  range per file, every directory and file planned together and read in two waves (`readLevel`). Same answers. */
import type { Env } from './auth.js'
import { asOfScans, type ChangedPlan, DiffUnavailable, FooterBudget, type IndexHandle, intervalsOn, ivChurnFrom, ivRefuseHeld, LEVEL_BUDGET, type LevelHandle, type LevelRec, type LevelSpend, openInterval, openIvLevel, planChanged, readChanged, readFits, readLevel, type Rect, type Row, syncClock, type Trace, withChurn, withTrace } from './index.js'
import { readRootAgg } from './view.js'

/** The answer's version, in its edge-cache key: bumped with every change to what a level answers. */
export const LEVEL_V = 1

/** K, the changed children drawn per directory (the rest fold into `other`), is snapped to a power of two in this
 *  range: a display choice (the cells that fit the parent's rectangle), snapped so nearby canvases share entries. */
export const LEVEL_K_MIN = 4
export const LEVEL_K_MAX = 512
export const levelK = (k: number): number => {
  const c = Math.min(LEVEL_K_MAX, Math.max(LEVEL_K_MIN, Number.isFinite(k) ? k : LEVEL_K_MIN))
  return 2 ** Math.ceil(Math.log2(c))
}

/** What ranks a directory's changed children (and sizes their cells): range churn (bytes added + removed, the default)
 *  or net change |b − a|. */
export type Measure = 'churn' | 'net'

/** A directory's own change, or a child's. `a`/`b` bytes and `oa`/`ob` objects at D1 / D2, `churn` over the range. */
export interface Sides { a: number; b: number; oa: number; ob: number; churn: number }

export interface LevelRow extends Sides {
  /** The child's name under the directory. */
  p: string
  k: 'file' | 'dir'
  /** `changed` also for a child that came and went inside the range (absent at both ends). */
  s: 'added' | 'removed' | 'changed'
}

export interface Level extends Sides {
  path: string
  /** The `k` largest changed children by the measure. */
  rows: LevelRow[]
  /** The other changed children, summed (`n` of them); null when every changed child is drawn. */
  other: (Sides & { n: number }) | null
  /** The unchanged children, summed: the directory less its changed children. */
  unchanged: { a: number; b: number; oa: number; ob: number }
  /** The only changed child, a directory: its own answer rides along (followed in the same request). */
  next?: string
}

/** A path's change over `(d1, d2]` from its versions there (`readChanged`, sorted by `vf`), or null when one version
 *  spans the range (unchanged) or it has none touching it. */
export function pathChange(pieces: Row[], d1: number, d2: number): (Sides & { k: 'file' | 'dir'; s: LevelRow['s'] }) | null {
  const live = (D: number) => pieces.find(r => r.vf! <= D && D < r.vt!)
  const A = live(d1), B = live(d2)
  if (A && B && A.vf === B.vf) return null
  const cum = (r: Row, f: 'churn_cum' | 'churn_end'): number => {
    const v = r[f]
    if (v == null) throw new DiffUnavailable('churn', `${r.path}: no ${f} on its version [${r.vf}, ${r.vt})`)
    return v
  }
  let c1: number
  if (A) c1 = cum(A, 'churn_cum')
  else {
    const first = pieces.find(r => r.vf! > d1 && r.vf! <= d2)
    if (!first) return null
    c1 = cum(first, 'churn_cum') - first.size
  }
  let c2: number
  if (B) c2 = cum(B, 'churn_cum')
  else {
    const ended = pieces.filter(r => r.vt! > d1 && r.vt! <= d2)
    if (!ended.length) return null
    c2 = cum(ended[ended.length - 1], 'churn_end')
  }
  const k = (B ?? A ?? pieces[0]).kind
  return { k, s: !A ? (B ? 'added' : 'changed') : !B ? 'removed' : 'changed', a: A?.size ?? 0, b: B?.size ?? 0, oa: A?.n_files ?? 0, ob: B?.n_files ?? 0, churn: c2 - c1 }
}

const name = (dir: string, p: string): string => (dir === '' ? p : p.slice(dir.length + 1))
const parentOf = (p: string): string => { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i) }
const depthOf = (p: string): number => (p === '' ? 0 : p.split('/').length)

/** `dir`'s answer from its own change and its changed children's (`children`: full paths). */
export function levelOf(dir: string, own: Sides, children: (Sides & { p: string; k: 'file' | 'dir'; s: LevelRow['s'] })[], k: number, m: Measure): Level {
  const changed = children.filter(c => c.churn !== 0 || c.a !== c.b || c.oa !== c.ob)
  const by = (c: Sides) => (m === 'churn' ? c.churn : Math.abs(c.b - c.a))
  changed.sort((x, y) => by(y) - by(x) || (x.p < y.p ? -1 : x.p > y.p ? 1 : 0))
  const rows = changed.slice(0, k).map(({ p, k: kind, s, a, b, oa, ob, churn }): LevelRow => ({ p: name(dir, p), k: kind, s, a, b, oa, ob, churn }))
  const rest = changed.slice(k)
  const sum = (xs: Sides[]) => xs.reduce((t, c) => ({ a: t.a + c.a, b: t.b + c.b, oa: t.oa + c.oa, ob: t.ob + c.ob, churn: t.churn + c.churn }), { a: 0, b: 0, oa: 0, ob: 0, churn: 0 })
  const all = sum(changed)
  const only = changed.length === 1 && changed[0].k === 'dir' ? changed[0].p : undefined
  return {
    path: dir,
    a: own.a, b: own.b, oa: own.oa, ob: own.ob, churn: own.churn,
    rows,
    other: rest.length ? { n: rest.length, ...sum(rest) } : null,
    unchanged: { a: own.a - all.a, b: own.b - all.b, oa: own.oa - all.oa, ob: own.ob - all.ob },
    ...(only ? { next: only } : {}),
  }
}

/** One directory's answer from several stores' answers for it (`parts`, each cut at `k` or more under the same
 *  measure): a union view's root (`api/diff/level.ts`), whose children are its members' disjoint roots. Totals,
 *  `unchanged` and `other` are summed; the rows are the parts' rows ranked as `levelOf` ranks them and cut at `k`
 *  after the merge (the rest, and the parts' own `other`s, are `other`). The same answer as `levelOf` over every
 *  part's children at once: a child in the merged top `k` is in its own part's top `k`. */
export function mergeLevels(dir: string, parts: readonly Level[], k: number, m: Measure): Level {
  const by = (c: Sides) => (m === 'churn' ? c.churn : Math.abs(c.b - c.a))
  const rows = parts.flatMap(l => l.rows).sort((x, y) => by(y) - by(x) || (x.p < y.p ? -1 : x.p > y.p ? 1 : 0))
  type Sum = { a: number; b: number; oa: number; ob: number; churn: number }
  const zero = (): Sum => ({ a: 0, b: 0, oa: 0, ob: 0, churn: 0 })
  const add = (t: Sum, c: Sides): Sum => ({ a: t.a + c.a, b: t.b + c.b, oa: t.oa + c.oa, ob: t.ob + c.ob, churn: t.churn + c.churn })
  const rest = rows.slice(k)
  const others = parts.flatMap(l => (l.other ? [l.other] : []))
  const n = rest.length + others.reduce((t, o) => t + o.n, 0)
  const own = parts.reduce(add, zero())
  const unchanged = parts.reduce((t, l) => ({ a: t.a + l.unchanged.a, b: t.b + l.unchanged.b, oa: t.oa + l.unchanged.oa, ob: t.ob + l.unchanged.ob }), { a: 0, b: 0, oa: 0, ob: 0 })
  const only = n === 0 && rows.length === 1 && rows[0].k === 'dir' ? (dir === '' ? rows[0].p : `${dir}/${rows[0].p}`) : undefined
  return {
    path: dir,
    ...own,
    rows: rows.slice(0, k),
    other: n ? { n, ...[...rest, ...others].reduce(add, zero()) } : null,
    unchanged,
    ...(only ? { next: only } : {}),
  }
}

export interface LevelOpts {
  from: string
  to: string
  dirs: string[]
  k: number
  m: Measure
  /** Follow single-changed-directory chains (`next`) in the same request (default on). */
  chain?: boolean
  /** Filled by the read: directories it left for a later request (`LEVEL_BUDGET` spent), and ones too wide even alone. */
  more?: string[]
  wide?: string[]
  trace?: Trace
  /** Called as the first data read (`readChanged`) starts: the end of the request's fixed phase (`Server-Timing` `fixed`). */
  onRead?: () => void
  /** The request's phases (`serverTiming`'s `lap`), through a level-index read: `churn`, then each round's `plan`, `index`,
   *  `pages`, `data`, `decode`, `rollup`, `own`. */
  lap?: (name: string) => void
}

/** What every round of a level request reads through: both scans' interval-store stamps and the `to` scan's `path`
 *  sort with its churn columns. */
export interface LevelPrep { env: Env; h: IndexHandle; d1: number; d2: number }

/** `buildLevels`' handles and checks, before any directory's read: what a request can start beside its gate (a
 *  `DiffUnavailable` it throws is the request's only once it reads). */
export async function prepareLevels(env0: Env, o: { from: string; to: string; trace?: Trace }): Promise<LevelPrep> {
  const env = asOfScans(env0, [o.from, o.to])
  // The interval store's handles only: a scan it doesn't hold is refused before any per-scan pointer or footer is read
  // (what `openIndex` would open, only to be refused here: this runs beside the gate, `AHEAD_COST`).
  const open = async (date: string): Promise<IndexHandle & { asOf: number }> => {
    const h = intervalsOn(env) ? await openInterval(env, date, 'path') : null
    if (h?.asOf != null) return h as IndexHandle & { asOf: number }
    // A date the store holds but can't open now (a broken run's scan) is its refusal, as `openIndex`'s.
    if (!h && intervalsOn(env)) await ivRefuseHeld(env, date, 'path')
    throw new DiffUnavailable('a per-scan store', 'a level diff reads the interval store; one scan is per-scan')
  }
  const [hA, hB] = await Promise.all([open(o.from), open(o.to)])
  const [d1, d2] = [hA.asOf, hB.asOf]
  const base = await ivChurnFrom(env)
  if (base == null) throw new DiffUnavailable('churn', 'the interval store generation carries no churn')
  if (d1 < base) throw new DiffUnavailable('churn', `churn is counted from scan stamp ${base}; ${o.from} is before it`)
  const h0 = withChurn(hB)
  if (!h0) throw new DiffUnavailable('churn', 'the path sort has no churn columns')
  return { env, h: withTrace(h0, o.trace), d1, d2 }
}

/** The rects of `dirs`' read: each one's children, and (not the root) its own row. */
const levelRects = (dirs: string[]): Rect[] => dirs.flatMap((p): Rect[] => {
  const d = depthOf(p)
  const kids = { dLo: d + 1, dHi: d + 1, pLo: p ? `${p}/` : '', pHi: p ? `${p}0` : '\uffff' }
  return p ? [kids, { dLo: d, dHi: d, pLo: p, pHi: p }] : [kids]
})

/** The distinct footer groups a plan made beside the gate (`planLevel`) reads at most, across its tiers: a wider plan is
 *  left to the read, after the gate (`AHEAD_COST`). A knob object: tests shrink it. */
export const PLAN_AHEAD_FOOTERS = { max: 16 }

/** The groups a first round over `dirs` reads (`planChanged`): planned beside the response-cache lookups, it is
 *  `buildLevels`' plan when the lookups leave every directory to read. It reads at most `PLAN_AHEAD_FOOTERS` footer
 *  groups (else `TooWide`: no plan, and the read plans again). */
export const planLevel = (p: LevelPrep, dirs: string[]): Promise<ChangedPlan> => {
  const footerBudget = new FooterBudget(PLAN_AHEAD_FOOTERS.max)
  const cap = (t: IndexHandle): IndexHandle => ({ ...t, footerBudget })
  const h = { ...cap(p.h), ...(p.h.runs ? { runs: p.h.runs.map(cap) } : {}) }
  return planChanged(h, levelRects([...new Set(dirs)]), p.d1, p.d2)
}

/** The level sort's handles for the range (`openIvLevel`: its tiers' footers), or null when some tier the range touches
 *  lacks it: what a request opens beside its gate, before the `path` sort's prep (only wanted on null). */
export const openLevelIndex = (env0: Env, o: { from: string; to: string; trace?: Trace }): Promise<LevelHandle | null> =>
  openIvLevel(asOfScans(env0, [o.from, o.to]), o.from, o.to, o.trace)

/** Links followed at most: well past any store's depth. */
const CHAIN_MAX = 64

/** The level answers of `o.dirs` (and of the chains they lead into), by path: from the generation's `level` sort when
 *  every tier the range touches has it (`given.level`, opened beside the request's gate: null, it hasn't; absent, opened
 *  here), else from the `path` sort through `given`'s prep and first-round plan (made beside the gate) or fresh ones. */
export async function buildLevels(env0: Env, o: LevelOpts, given?: { level?: LevelHandle | null; prep?: LevelPrep; plan?: ChangedPlan }): Promise<Map<string, Level>> {
  const env = asOfScans(env0, [o.from, o.to])
  const lh = given?.level !== undefined ? given.level : await openIvLevel(env, o.from, o.to)
  if (lh) {
    const base = await ivChurnFrom(env)
    if (base == null) throw new DiffUnavailable('churn', 'the interval store generation carries no churn')
    if (lh.d1 < base) throw new DiffUnavailable('churn', `churn is counted from scan stamp ${base}; ${o.from} is before it`)
    o.lap?.('churn')
    const spend = { left: LEVEL_BUDGET.bytes }
    const got = await chained(o, dirs => levelRound(env, lh, dirs, o, spend))
    if (got) {
      o.trace?.('level-index', 1)
      return got
    }
  }
  const prep = given?.prep ?? await prepareLevels(env0, o)
  // A plan made beside the cache lookups answers the first round when it planned exactly that round's directories.
  let plan = given?.plan
  return (await chained(o, async dirs => {
    const got = await round(prep.env, prep.h, dirs, prep.d1, prep.d2, o, plan)
    plan = undefined
    return got
  }))!
}

/** `o.dirs`' answers and their chains' (`next`), a round of `read` per link; null as soon as a round is (the level
 *  index can't answer it: the caller reads the `path` sort). */
async function chained(o: LevelOpts, read: (dirs: string[]) => Promise<Level[] | null>): Promise<Map<string, Level> | null> {
  const out = new Map<string, Level>()
  let todo = [...new Set(o.dirs)]
  for (let link = 0; todo.length && link <= CHAIN_MAX; link++) {
    const got = await read(todo)
    if (!got) return null
    for (const l of got) out.set(l.path, l)
    todo = o.chain === false ? [] : got.flatMap(l => (l.next && !out.has(l.next) ? [l.next] : []))
  }
  return out
}

/** A path's change over the range from its transitions there (`LevelRec`s, by `ts`): its values before the first and
 *  after the last, its churn the last's `churnCum` less the first's `churnPrev`; null without any. */
export function transitionsChange(ts: LevelRec[]): (Sides & { k: 'file' | 'dir'; s: LevelRow['s'] }) | null {
  if (!ts.length) return null
  const first = ts[0], last = ts[ts.length - 1]
  const A = first.op !== 0, B = last.op !== 2
  const kind = B ? last.kind : A ? first.kindPrev! : first.kind
  return {
    k: kind === 'file' ? 'file' : 'dir', s: !A ? (B ? 'added' : 'changed') : !B ? 'removed' : 'changed',
    a: A ? first.sizePrev : 0, b: B ? last.size : 0, oa: A ? first.nFilesPrev : 0, ob: B ? last.nFiles : 0, churn: last.churnCum - first.churnPrev,
  }
}

/** One round from the level index: `dirs`' transitions (`readLevel`), split while too wide as `round` splits. Null when a
 *  directory's own transitions show it a file on some side in the range (its own rows are written for directories and
 *  closings only, so a file's changes have none): that round is read from the `path` sort instead. */
async function levelRound(env: Env, lh: LevelHandle, dirs: string[], o: LevelOpts, spend: LevelSpend): Promise<Level[] | null> {
  let recs: LevelRec[]
  try {
    recs = await readLevel(lh, dirs, spend, o.trace, o.lap)
  } catch (e) {
    if (!String((e as Error).message).startsWith('query too wide')) throw e
    if (dirs.length === 1) {
      // Asked alone it is the request's answer (a 413); beside others, they still get theirs.
      if (o.dirs.length === 1 && spend.left === LEVEL_BUDGET.bytes) throw e
      ;(spend.left === LEVEL_BUDGET.bytes ? (o.wide ??= []) : (o.more ??= [])).push(dirs[0])
      return []
    }
    const m = dirs.length >> 1
    const x = await levelRound(env, lh, dirs.slice(0, m), o, spend)
    if (!x) return null
    if (spend.left < LEVEL_BUDGET.bytes) {
      (o.more ??= []).push(...dirs.slice(m))
      return x
    }
    const y = await levelRound(env, lh, dirs.slice(m), o, spend)
    return y && [...x, ...y]
  }
  const by = new Map<string, LevelRec[]>()
  for (const r of recs) {
    const k = `${r.path}\0${r.name}`
    const l = by.get(k)
    if (l) l.push(r)
    else by.set(k, [r])
  }
  const kids = new Map<string, (Sides & { p: string; k: 'file' | 'dir'; s: LevelRow['s'] })[]>(dirs.map(p => [p, []]))
  const own = new Map<string, Sides>()
  for (const [k, ts] of by) {
    ts.sort((x, y) => x.ts - y.ts)
    const [dir, nm] = k.split('\0')
    if (nm === '') {
      if (ts.some(r => r.kind !== 'dir' || (r.kindPrev != null && r.kindPrev !== 'dir'))) return null
      const c = transitionsChange(ts)
      if (c && dir !== '') own.set(dir, c)
      continue
    }
    const c = transitionsChange(ts)
    if (c) kids.get(dir)?.push({ p: dir ? `${dir}/${nm}` : nm, ...c })
  }
  if (o.lap) {
    await syncClock()
    o.lap('rollup')
  }
  o.trace?.('lv-own-dirs', dirs.filter(p => !own.has(p)).length)
  const levels = await Promise.all(dirs.map(async p => {
    const children = kids.get(p)!
    let mine = own.get(p)
    if (!mine) {
      // The root (no transitions of its own), or a directory with none in the range: its totals as of each scan.
      const [ra, rb] = await Promise.all([readRootAgg(env, { date: o.from, path: p }), readRootAgg(env, { date: o.to, path: p })])
      mine = { a: ra?.b ?? 0, b: rb?.b ?? 0, oa: ra?.o ?? 0, ob: rb?.o ?? 0, churn: children.reduce((n, c) => n + c.churn, 0) }
    }
    return levelOf(p, mine, children, o.k, o.m)
  }))
  o.lap?.('own')
  return levels
}

/** One read of `dirs`' changed children (and their own versions), split while it doesn't fit the decoder. */
async function round(env: Env, h: IndexHandle, dirs: string[], d1: number, d2: number, o: LevelOpts, given?: ChangedPlan): Promise<Level[]> {
  const rects = levelRects(dirs)
  const plan = given ?? await planChanged(h, rects, d1, d2)
  o.trace?.('level-plan', plan.spans.length, `${dirs.length} dirs, ${plan.spans.length} groups, ${plan.rows} rows`)
  if (!readFits(plan.spans)) {
    if (dirs.length === 1) throw new Error(`query too wide: ${dirs[0] || '(root)'}'s changed children span ${plan.spans.length} row groups (${plan.rows} rows)`)
    const m = dirs.length >> 1
    return [...await round(env, h, dirs.slice(0, m), d1, d2, o), ...await round(env, h, dirs.slice(m), d1, d2, o)]
  }
  o.onRead?.()
  const rows = await readChanged(h, rects, d1, d2, { plan })
  const byPath = new Map<string, Row[]>()
  for (const r of rows) {
    const l = byPath.get(r.path)
    if (l) l.push(r)
    else byPath.set(r.path, [r])
  }
  const kids = new Map<string, (Sides & { p: string; k: 'file' | 'dir'; s: LevelRow['s'] })[]>(dirs.map(p => [p, []]))
  const own = new Map<string, Sides>()
  for (const [p, pieces] of byPath) {
    pieces.sort((x, y) => x.vf! - y.vf!)
    const c = pathChange(pieces, d1, d2)
    if (!c) continue
    if (kids.has(p) && p !== '') own.set(p, c)
    const l = kids.get(parentOf(p))
    if (l) l.push({ p, ...c })
  }
  return Promise.all(dirs.map(async p => {
    const children = kids.get(p)!
    let mine = own.get(p)
    if (!mine) {
      // The root (no row of its own: its totals are its depth-1 rows'), or a directory one version spans (unchanged:
      // no child changed either): its totals read as of each scan.
      const [ra, rb] = await Promise.all([readRootAgg(env, { date: o.from, path: p }), readRootAgg(env, { date: o.to, path: p })])
      mine = { a: ra?.b ?? 0, b: rb?.b ?? 0, oa: ra?.o ?? 0, ob: rb?.o ?? 0, churn: children.reduce((n, c) => n + c.churn, 0) }
    }
    return levelOf(p, mine, children, o.k, o.m)
  }))
}
