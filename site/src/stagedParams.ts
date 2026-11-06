// `/staged`'s view state as URL params (use-prms codecs; `urlParams.ts` describes each key): which batches are open,
// each table's page, the completed batches' fold, the runs table's page and size. A default view writes nothing, so a
// link is as long as what its sender changed, and a reload or Back lands on the same view.
//
// The URL holds the view, never what a deletion acts on. The checked rows, the armed confirm and the bucket cut are
// component state (`StagedPage`): a link must not open with rows selected, a delete armed or a run narrowed, so
// whoever dispatches has checked every row by hand, on the page they are looking at (`stagedParams.test.ts`). One
// param does reach a run's inputs, the scan the page is sized at (`?d=`: which rows can be checked, the confirm's total),
// so a page sized at any scan but the one a run reads dispatches nothing (`stagedScan.ts` `sizedAtOldScan`).
//
// Values use only what a query string leaves bare (`URLSearchParams`: letters, digits, `*-._`): the scroll-spy
// re-serializes the whole query each time it names a section.
import type { Param } from 'use-prms'
import { SECTION_KEY } from './homeSections'

/** A group's key (`StagedPage`'s `gkey`): a session's lead batch id; the items staged before batches existed; the one
 *  table of the flat view. */
export const EARLIER_KEY = 'none'
export const FLAT_KEY = '-1'

/** Which batches are open. An unlisted batch does what `base` says: follows its completion (`auto`: a live batch
 *  open, a settled one folded), or the "expand all" / "collapse all" last clicked. `open` / `closed` list the batches
 *  set against it (sorted, disjoint). */
export interface Folds {
  base: 'auto' | 'open' | 'closed'
  open: string[]
  closed: string[]
}
export const NO_FOLDS: Folds = { base: 'auto', open: [], closed: [] }

/** A run of ids a hand-typed `1-999999999` may expand to. */
const MAX_RUN = 4096
const order = (k: string) => (k === EARLIER_KEY ? Infinity : Number(k))
const canon = (keys: readonly string[]): string[] => [...new Set(keys)].sort((a, b) => order(a) - order(b))

/** Keys as the URL lists them: ascending, consecutive ids as `a-b`, the earlier group (`e`) last. */
function encodeKeys(keys: readonly string[]): string {
  const ks = canon(keys)
  const ids = ks.filter(k => k !== EARLIER_KEY).map(Number)
  const out: string[] = []
  for (let i = 0; i < ids.length;) {
    let j = i
    while (j + 1 < ids.length && ids[j + 1] === ids[j] + 1) j++
    out.push(j > i ? `${ids[i]}-${ids[j]}` : `${ids[i]}`)
    i = j + 1
  }
  if (ks.includes(EARLIER_KEY)) out.push('e')
  return out.join('.')
}

function decodeKeys(s: string): string[] {
  const out: string[] = []
  for (const t of s.split('.')) {
    if (t === 'e') { out.push(EARLIER_KEY); continue }
    const m = /^(\d{1,9})(?:-(\d{1,9}))?$/.exec(t)
    if (!m) continue
    const a = Number(m[1]), b = m[2] === undefined ? a : Number(m[2])
    if (b < a || b - a >= MAX_RUN) continue
    for (let i = a; i <= b; i++) out.push(String(i))
  }
  return canon(out)
}

/** `?x=<open>[_<closed>]`, each side a list of batch ids (`12.15-18.e`) or `*` for every batch: `12` (batch 12
 *  opened), `_7` (7 folded), `*` (expand all), `*_7` (all but 7), `_*` (collapse all), `12_*` (all folded but 12). */
export const foldsParam: Param<Folds> = {
  encode: f => {
    const open = f.base === 'open' ? '*' : encodeKeys(f.open)
    const closed = f.base === 'closed' ? '*' : encodeKeys(f.base === 'auto' ? f.closed.filter(k => !f.open.includes(k)) : f.closed)
    return (closed ? `${open}_${closed}` : open) || undefined
  },
  decode: s => {
    const parts = (s ?? '').split('_')
    const [o, c = ''] = parts
    if (parts.length > 2 || (o === '*' && c === '*')) return NO_FOLDS
    if (o === '*') return { base: 'open', open: [], closed: decodeKeys(c) }
    if (c === '*') return { base: 'closed', open: decodeKeys(o), closed: [] }
    const open = decodeKeys(o)
    return { base: 'auto', open, closed: decodeKeys(c).filter(k => !open.includes(k)) }
  },
}

/** What the URL says of one batch: folded (true), open (false), or nothing (its default applies). */
export const foldOverride = (f: Folds, key: string): boolean | undefined =>
  f.base === 'open' ? f.closed.includes(key)
    : f.base === 'closed' ? !f.open.includes(key)
      : f.open.includes(key) ? false : f.closed.includes(key) ? true : undefined

/** `f` with batch `key` folded or opened. Against `auto`, a batch set to its own default (`dflt`: folded) leaves the
 *  URL; against "all", it is an exception or not. */
export function setFold(f: Folds, key: string, collapsed: boolean, dflt: boolean): Folds {
  const without = (ks: string[]) => ks.filter(k => k !== key)
  if (f.base === 'open') return { ...f, closed: collapsed ? canon([...f.closed, key]) : without(f.closed) }
  if (f.base === 'closed') return { ...f, open: collapsed ? without(f.open) : canon([...f.open, key]) }
  const open = without(f.open), closed = without(f.closed)
  if (collapsed === dflt) return { base: 'auto', open, closed }
  return collapsed ? { base: 'auto', open, closed: canon([...closed, key]) } : { base: 'auto', open: canon([...open, key]), closed }
}

/** "Expand all" / "collapse all". */
export const foldAll = (collapsed: boolean): Folds => ({ base: collapsed ? 'closed' : 'open', open: [], closed: [] })

/** `f`, or nothing when it leaves every batch (`[key, folded by default]`) as its default has it: the default view. */
export const settleFolds = (f: Folds, groups: readonly (readonly [string, boolean])[]): Folds =>
  groups.every(([k, dflt]) => (foldOverride(f, k) ?? dflt) === dflt) ? NO_FOLDS : f

/** Each staged table's page (0-based; absent: its first), by group key. */
export type Pages = Record<string, number>

/** `?pg=`: the tables off their first page, 1-based: `12-3` (batch 12, page 3), `e-2` (the earlier group), a bare
 *  `3` (the flat table), `.`-joined. */
export const pagesParam: Param<Pages> = {
  encode: p => {
    const tokens = canon(Object.keys(p).filter(k => k !== FLAT_KEY && p[k] >= 1)).map(k => `${k === EARLIER_KEY ? 'e' : k}-${p[k] + 1}`)
    return [...(p[FLAT_KEY] >= 1 ? [`${p[FLAT_KEY] + 1}`] : []), ...tokens].join('.') || undefined
  },
  decode: s => {
    const out: Pages = {}
    for (const t of (s ?? '').split('.')) {
      const m = /^(?:(\d{1,9}|e)-)?(\d{1,6})$/.exec(t)
      if (!m || Number(m[2]) < 2) continue
      out[m[1] === undefined ? FLAT_KEY : m[1] === 'e' ? EARLIER_KEY : String(Number(m[1]))] = Number(m[2]) - 1
    }
    return out
  },
}

/** A page number in the URL (1-based) → its 0-based index; null when it isn't one. */
const pageIndex = (s: string | undefined): number | null => (s !== undefined && /^\d{1,6}$/.test(s) && Number(s) >= 1 ? Number(s) - 1 : null)

/** `?done`: the completed batches' fold. Absent: folded. A bare `?done`: open on its first page; `?done=3`: on its
 *  third. In the page: null, or the 0-based page. */
export const settledParam: Param<number | null> = {
  encode: p => (p == null ? undefined : p < 1 ? '' : `${p + 1}`),
  decode: s => (s === undefined ? null : pageIndex(s) ?? 0),
}

/** `?rp=`: the runs table's page (1-based). Absent (null): the table picks it, the open run's page, else the first
 *  (`runTable.ts` `runsPage`). */
export const runsPageParam: Param<number | null> = {
  encode: p => (p == null ? undefined : `${p + 1}`),
  decode: pageIndex,
}

export const RUNS_PAGE_SIZES = [20, 50, Infinity]
/** `?rn=`: runs per page, `50` or `all`; 20 when absent. */
export const runsSizeParam: Param<number> = {
  encode: n => (n === RUNS_PAGE_SIZES[0] ? undefined : n === Infinity ? 'all' : `${n}`),
  decode: s => (s === 'all' ? Infinity : RUNS_PAGE_SIZES.includes(Number(s)) ? Number(s) : RUNS_PAGE_SIZES[0]),
}

/** A section a `#` link names: a batch (`#batch-12`) or the runs table. */
export interface LinkedSection { section: string; batch: number | null }

/** The section a URL fragment names, for the links that say it there: a batch's permalink, a Slack thread's, the
 *  staged card's (`/staged#batch-12`); null for any other fragment. */
export function sectionOfHash(hash: string): LinkedSection | null {
  const m = /^#(?:batch-(\d{1,9})|runs)$/.exec(hash)
  return m ? { section: hash.slice(1), batch: m[1] === undefined ? null : Number(m[1]) } : null
}

/** `search`'s `key=value` pairs but `keys`', each as written (a re-serialization would escape them). */
const without = (search: string, keys: string[]): string[] =>
  search.replace(/^\?/, '').split('&').filter(kv => kv && !keys.includes(kv.split('=')[0]))

/** The query a `#` link settles on: `to` named in `?section=`, where the scroll-spy keeps the section in view. A link
 *  to a batch shows batches, so the filter and the flat table go (they show one table, no batch to land on). The
 *  other params keep their spelling: only whole `key=value` pairs are dropped. */
export function adoptSection(search: string, to: LinkedSection): string {
  const rest = without(search, [SECTION_KEY, ...(to.batch == null ? [] : ['q', 'flat'])])
  return `?${[...rest, `${SECTION_KEY}=${to.section}`].join('&')}`
}

/** `search` less its section: what the URL holds once the scroll-spy is turned off. */
export function dropSection(search: string): string {
  const rest = without(search, [SECTION_KEY])
  return rest.length ? `?${rest.join('&')}` : ''
}
