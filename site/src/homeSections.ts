// The home page's sections (DOM-free: the Pages Functions share it). The scroll-spy (`useSectionSpy`, param mode)
// names the one in view as `?v=<name>` (`encodeSection`), where a link-preview unfurler can see it (fragments never
// reach the server); deep links scroll to it.

/** The URL param holding the section in view. Not `s`, use-prms' default: the home page's `?s=` is the shade axis
 * (`?s=c`), the staged page's its sort. */
export const SECTION_KEY = 'v'
/** Its spelling before 2026-11-11 (`?section=over-time`): still read (`sectionParam`), and rewritten on load
 * (`migrateSection`), so older links and Slack unfurls keep landing on their section. */
export const LEGACY_SECTION_KEY = 'section'

/** Section id → its `?v=` name, where a shorter one reads as well (`over-time` → `time`); a staged batch
 * (`batch-12`) and any other id are written as themselves. */
const NAMES: Record<string, string> = { 'tree-map': 'map', tbl: 'table', 'over-time': 'time' }
/** `?v=` name → section id: `NAMES` inverted, plus the one-letter codes minted 2026-11-11 (`?v=o`), still read. */
const IDS: Record<string, string> = {
  ...Object.fromEntries(Object.entries(NAMES).map(([id, n]) => [n, id])),
  m: 'tree-map', t: 'tbl', o: 'over-time', d: 'diff', c: 'mtime', r: 'runs',
}

/** A section id as `?v=` spells it (`over-time` → `time`, `diff` → `diff`). */
export const encodeSection = (id: string): string => NAMES[id] ?? id

/** A `?v=` (or legacy `?section=`) value → its section id: a name, an id spelled out (`diff`, a retired anchor
 * such as `changes`, `batch-12`), or a one-letter code (`o`, `b12`). */
export function decodeSection(v: string): string {
  if (Object.hasOwn(IDS, v)) return IDS[v]
  const b = /^b(\d+)$/.exec(v)
  return b ? `batch-${b[1]}` : LEGACY_ANCHORS[v] ?? v
}

/** The section id a query names: `?v=`, else the legacy `?section=`; null when neither does. */
export function sectionParam(sp: URLSearchParams): string | null {
  const v = sp.get(SECTION_KEY) ?? sp.get(LEGACY_SECTION_KEY)
  return v == null ? null : decodeSection(v)
}

/** `search` with a legacy `section=<id>` pair rewritten in place as `v=<name>` (dropped when `v=` is there already);
 * the other pairs as written. Null when there is none. */
export function migrateSection(search: string): string | null {
  const pairs = search.replace(/^\?/, '').split('&').filter(Boolean)
  const i = pairs.findIndex(kv => kv.split('=')[0] === LEGACY_SECTION_KEY)
  if (i < 0) return null
  const has = pairs.some(kv => kv.split('=')[0] === SECTION_KEY)
  const v = decodeURIComponent(pairs[i].slice(LEGACY_SECTION_KEY.length + 1))
  const out = has || !v ? pairs.filter((_, j) => j !== i) : pairs.map((kv, j) => (j === i ? `${SECTION_KEY}=${encodeSection(decodeSection(v))}` : kv))
  return out.length ? `?${out.join('&')}` : ''
}

/** Section element ids, top to bottom. */
export const SECTION_IDS = ['tree-map', 'tbl', 'over-time', 'diff', 'mtime'] as const

/** Retired anchor ids → the ids that replaced them; old links keep working. */
export const LEGACY_ANCHORS: Record<string, string> = {
  'size-over-time': 'over-time', 'created-date': 'mtime', changes: 'diff',
}

/** The home section a URL fragment names (`#diff`, a retired `#changes`), canonical; null for any other fragment. */
export function sectionOfHash(hash: string): string | null {
  const id = hash.replace(/^#/, '')
  const canon = LEGACY_ANCHORS[id] ?? id
  return (SECTION_IDS as readonly string[]).includes(canon) ? canon : null
}
