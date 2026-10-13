// /health (specs/health-page.md): the state of the deployment's append-only stores — each store's stack (a base
// generation plus binary-counter runs), what every scan is covered by, and how fresh it all is. DOM-free: the Function
// (`functions/_lib/health.ts`) reads the stores and assembles the document; the page lays the stacks out as shards on
// a timeline (`stackTiers`, rendered by `pyrmts-react`'s `CoverTimeline`).
import type { PyramidCoverSegment, PyramidTierCoverStatus } from 'pyrmts'
import { scanTime } from './scanSlug'

/** The binary counter folds runs into a new base generation (a compaction) at this level (`append_runner.COMPACT_LEVEL`):
 *  a manifest's `compact_level` overrides it. */
export const COMPACT_LEVEL = 5

/** A run a store's manifest lists (`append_runner.RUN_FIELDS`). */
export interface StackRun { key: string; first: string; last: string; level: number; scans: string[]; rows?: number; bytes?: number }

/** A static-index tier's presence on R2: `light` (`catalog/meta.json`: the suffix shards and catalog), `drill`
 *  (`drill/meta.json`), `anchors` (`anchors/meta.json`, and `anchors/start/meta.json` when the base carries one). */
export interface StaticTiers { light: boolean; drill: boolean; anchors: boolean }

/** A run as /health reports it: its manifest entry, whether its served files are whole on R2 (`r2`), and (static) its
 *  tiers. */
export interface HealthRun extends StackRun { r2: boolean; tiers?: StaticTiers }

export type StoreKind = 'interval' | 'static'

/** One store's stack as read from R2: its generation, the base's scans, the newest manifest's runs (oldest first). */
export interface StoreRead {
  kind: StoreKind
  gen: string
  /** The newest manifest's file name (`manifests.ts`), null when the generation is its base alone. */
  manifest: string | null
  /** Its revision (`<id>.mNNN.json`: NNN; 0 for a scan's own) and what it revises. */
  rev: number
  revises: string | null
  /** When the newest manifest was uploaded to R2 (epoch s): the store's newest append (or merge). */
  manifest_ts: number | null
  compact_level: number
  base: string[]
  /** The base's tiers (static), each true when its marker is on R2. */
  base_tiers?: StaticTiers
  runs: HealthRun[]
  /** The read failed (no `scans.json`, an unreadable manifest): the stack is unknown. */
  error?: string
  /** The store's merge status record (`<root>/merging.json`), when a merge holds (or held) the lease. */
  merging?: MergeRecord | null
}

// ── Merges in progress ─────────────────────────────────────────────────────

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`

/** A carry a merge job has planned (`append_runner.StatusMirror`'s `merges`; the first is the one building). */
export interface MergePlan { inputs: string[]; output: string; level: number; first: string; last: string; scans: number }

/** A merge's status record, mirrored to R2 beside the store by the job holding the merge lease
 *  (`append_runner.StatusMirror`): written as each carry starts, refreshed every `heartbeat_s`, removed when the job ends
 *  cleanly, `state: failed` when it raised. A job killed outright leaves it `merging` with a heartbeat that stops. Times
 *  in epoch s. */
export interface MergeRecord {
  holder: string
  owner?: string
  state: 'merging' | 'failed'
  started_ts: number
  updated_ts: number
  heartbeat_s?: number | null
  lease_s?: number
  manifest?: string | null
  merges: MergePlan[]
  done?: { output: string; manifest: string | null }[]
  error?: string
}

/** A heartbeat older than this and a merge is stuck: ~6 of `append_runner.HEARTBEAT_S` (5 min) missed. */
export const STUCK_S = 1800
/** The merge lease's lifetime (`append_runner.LEASE_S`), when the record doesn't carry its own. */
export const LEASE_S = 4 * 3600

/** A merge as /health reports it: its record, `status` (`stuck`: no heartbeat for `STUCK_S`, or held past its lease),
 *  its age and its heartbeat's (s), and why it's stuck. */
export interface MergeState extends MergeRecord { status: 'merging' | 'stuck' | 'failed'; age: number; beat_age: number; why: string | null }

/** `s` seconds as `12m` or `4h05m`. */
export const dur = (s: number): string => (s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`)

export function mergeState(rec: MergeRecord | null | undefined, now: number): MergeState | null {
  if (!rec) return null
  const age = Math.max(0, now - rec.started_ts), beat_age = Math.max(0, now - rec.updated_ts), lease = rec.lease_s ?? LEASE_S
  if (rec.state === 'failed') return { ...rec, status: 'failed', age, beat_age, why: rec.error ?? null }
  const why = beat_age > STUCK_S ? `no heartbeat for ${dur(beat_age)}` : age > lease ? `held ${dur(age)}, past its ${dur(lease)} lease` : null
  return { ...rec, status: why ? 'stuck' : 'merging', age, beat_age, why }
}

/** `ts` (epoch s) as `HH:MM UTC`, with its date when it isn't `now`'s day. */
export function clock(ts: number, now: number): string {
  const t = new Date(ts * 1000).toISOString(), n = new Date(now * 1000).toISOString()
  return `${t.slice(0, 10) === n.slice(0, 10) ? t.slice(11, 16) : `${t.slice(0, 10)} ${t.slice(11, 16)}`} UTC`
}

/** A merge's one-line summary: `merging since 06:00 UTC: 2 runs → deltas/a_b (L1)` (a failed one: `merge failed at …`). */
export function mergeLine(m: MergeState, now: number): string {
  const plan = m.merges.map(x => `${plural(x.inputs.length, 'run')} → ${x.output} (L${x.level})`).join('; ')
  return m.status === 'failed' ? `merge failed at ${clock(m.updated_ts, now)}: ${plan}` : `merging since ${clock(m.started_ts, now)}: ${plan}`
}

/** A carry the binary counter has due (`append_runner.plan_carries`): the runs it merges and the run it makes. */
export interface Carry { inputs: string[]; output: string; level: number; first: string; last: string; scans: number }

/** `append_runner.plan_carries`, ported: the binary counter replayed over `runs` (oldest first). Each is pushed in turn,
 *  and while the two newest share a level (and both carry a drill or neither) they carry into one a level up; carries
 *  that chain fold into one merge of every run they consumed. A carry never reaches `top` (a compaction's job). */
export function planCarries(runs: readonly StackRun[], drilled: ReadonlySet<string> = new Set(), top = COMPACT_LEVEL): { after: StackRun[]; carries: Carry[] } {
  const dr = new Set(drilled)
  const stack: { r: StackRun; ins: StackRun[] }[] = []
  for (const r of runs) {
    stack.push({ r, ins: [r] })
    while (stack.length >= 2) {
      const a = stack[stack.length - 2], b = stack[stack.length - 1]
      if (a.r.level !== b.r.level || a.r.level + 1 >= top || dr.has(a.r.key) !== dr.has(b.r.key)) break
      const m: StackRun = { key: runKey(a.r.first, b.r.last), first: a.r.first, last: b.r.last, level: a.r.level + 1, scans: [...a.r.scans, ...b.r.scans] }
      if (dr.has(a.r.key)) dr.add(m.key)
      stack.splice(-2, 2, { r: m, ins: [...a.ins, ...b.ins] })
    }
  }
  return {
    after: stack.map(x => x.r),
    carries: stack.filter(x => x.ins.length > 1).map(({ r, ins }) => ({ inputs: ins.map(i => i.key), output: r.key, level: r.level, first: r.first, last: r.last, scans: r.scans.length })),
  }
}

/** A run's directory under its generation (`append_runner.run_key`). */
export const runKey = (first: string, last: string): string => (first === last ? `deltas/${first}` : `deltas/${first}_${last}`)

/** How close a stack is to its compaction: the scans its runs hold of the `2^level` a compaction folds, and whether one
 *  is due (two adjacent runs at `level - 1` after the due carries, which never merge further). */
export interface Compaction { level: number; run_scans: number; capacity: number; due: boolean; max_level: number }

export function compaction(after: readonly StackRun[], level: number): Compaction {
  return {
    level,
    run_scans: after.reduce((n, r) => n + r.scans.length, 0),
    capacity: 2 ** level,
    due: after.some((r, i) => i > 0 && r.level === level - 1 && after[i - 1].level === level - 1),
    max_level: after.reduce((m, r) => Math.max(m, r.level), -1),
  }
}

/** The runs a reader serves a tier from: the base and the runs up to (not including) the first one without it (each
 *  reader's stack is cut there: `Tiers`, the drill stack, `AnchoredSource`, `loadIvState`). */
export function servedPrefix<T>(runs: readonly T[], has: (r: T) => boolean): T[] {
  const i = runs.findIndex(r => !has(r))
  return i < 0 ? [...runs] : runs.slice(0, i)
}

/** One store's health: its read, the due carries, the compaction gauge, and the scans each tier serves. */
export interface StoreHealth extends StoreRead {
  carries: Carry[]
  compaction: Compaction
  /** The stack's newest scan (the base's newest when there are no runs). */
  newest: string | null
  /** The scans each served tier answers for (`light` the interval store's path sorts, or the static index's suffix
   *  shards and catalog). A tier the base lacks serves none (`drill` / `anchors` unconfigured). */
  served: { light: string[]; drill: string[] | null; anchors: string[] | null }
  /** The run each served stack is cut at (its first run without the tier), when one is. */
  cut: { light: string | null; drill: string | null; anchors: string | null }
  /** The merge holding (or that last held) the lease, from its status record: null when none is recorded. */
  merge: MergeState | null
}

/** `now`: epoch s (a merge's age). */
export function storeHealth(s: StoreRead, now: number = Math.floor(Date.now() / 1000)): StoreHealth {
  const drilled = new Set(s.runs.filter(r => r.tiers?.drill).map(r => r.key))
  const { after, carries } = planCarries(s.runs, drilled, s.compact_level)
  const all = [...s.base, ...s.runs.flatMap(r => r.scans)]
  const tier = (has: (r: HealthRun) => boolean, base: boolean): { scans: string[] | null; cut: string | null } => {
    if (!base) return { scans: null, cut: null }
    const ok = servedPrefix(s.runs, has)
    return { scans: [...s.base, ...ok.flatMap(r => r.scans)], cut: s.runs[ok.length]?.key ?? null }
  }
  const light = tier(r => r.r2 && (r.tiers?.light ?? true), !s.error)
  const drill = s.kind === 'static' ? tier(r => r.r2 && !!r.tiers?.drill, !!s.base_tiers?.drill) : { scans: null, cut: null }
  const anchors = s.kind === 'static' ? tier(r => r.r2 && !!r.tiers?.anchors, !!s.base_tiers?.anchors) : { scans: null, cut: null }
  return {
    ...s, carries, compaction: compaction(after, s.compact_level), newest: all.length ? all[all.length - 1] : null,
    served: { light: light.scans ?? [], drill: drill.scans, anchors: anchors.scans },
    cut: { light: light.cut, drill: drill.cut, anchors: anchors.cut },
    merge: mergeState(s.merging, now),
  }
}

// ── Coverage ───────────────────────────────────────────────────────────────

/** A scan's cell in one column: `present`; `missing` (a gap: the store should hold it and doesn't); `pending` (newer
 *  than the store's newest while the scan's job is still running); `na` (not expected: before the store's generation,
 *  or the tier isn't configured). */
export type Cell = 'present' | 'missing' | 'pending' | 'na'
export const COLUMNS = ['path', 'interval', 'light', 'drill', 'anchors', 'r2'] as const
export type Column = typeof COLUMNS[number]
export interface ScanCoverage { scan: string; cells: Record<Column, Cell>; gaps: Column[] }

/** What a scan is covered by. `perScan`: the scans with a per-scan path store (D1 `index_schema` `path` rows);
 *  `running`: scans whose job is running (a store not holding them yet is `pending`, once it's newer than the store's
 *  newest); `anyRunning`: some job is running, so a scan newer than every store's newest is `pending` too even when no
 *  running job names it; `stores`: the configured stores' health (an absent store is `na` in its columns).
 *
 *  - `path`: the scan's path sorts are served — per-scan, or from the interval store.
 *  - `interval`: the interval store serves it (expected for every scan from its base's first on).
 *  - `light` / `drill` / `anchors`: the static name index serves it in that tier (expected from its base's first scan
 *    on, `drill` / `anchors` when the base carries the tier).
 *  - `r2`: every store listing it has its files whole on R2 (a listed run missing files there is `missing`: the
 *    readers cut their stack at it). */
export function coverage(scans: readonly string[], perScan: ReadonlySet<string>, running: ReadonlySet<string>, stores: readonly StoreHealth[], anyRunning = false): ScanCoverage[] {
  const iv = stores.find(s => s.kind === 'interval'), st = stores.find(s => s.kind === 'static')
  const sets = (s: StoreHealth | undefined) => s && {
    first: s.base[0] ?? s.runs[0]?.first ?? null,
    newest: s.newest,
    light: new Set(s.served.light),
    drill: s.served.drill && new Set(s.served.drill),
    anchors: s.served.anchors && new Set(s.served.anchors),
  }
  const I = sets(iv), S = sets(st)
  const newests = stores.map(s => s.newest).filter((n): n is string => n !== null).sort()
  const newestAll = newests.length ? newests[newests.length - 1] : null
  /** The scan's job is (or may be) running: one names it, or one runs and the scan is past every store's newest. */
  const inFlight = (scan: string): boolean => running.has(scan) || (anyRunning && newestAll !== null && scan > newestAll)
  type Sets = NonNullable<typeof I>
  const cell = (x: Sets | undefined, set: Set<string> | null | undefined, scan: string): Cell => {
    if (!x || !set || x.first === null || scan < x.first) return 'na'
    if (set.has(scan)) return 'present'
    return x.newest !== null && scan > x.newest && inFlight(scan) ? 'pending' : 'missing'
  }
  return [...scans].sort().map(scan => {
    const interval = cell(I, I?.light, scan)
    const path: Cell = perScan.has(scan) || interval === 'present' ? 'present' : inFlight(scan) ? 'pending' : 'missing'
    const light = cell(S, S?.light, scan)
    // R2: a store lists the scan (its base, or a run of the newest manifest) but the run's files aren't whole there.
    const holders = [iv, st].flatMap(x => (x ? [x] : [])).filter(x => x.base.includes(scan) || x.runs.some(r => r.scans.includes(scan)))
    const r2: Cell = !holders.length ? 'na' : holders.some(x => (x.base.includes(scan) ? !!x.error : x.runs.some(r => r.scans.includes(scan) && !r.r2))) ? 'missing' : 'present'
    const cells: Record<Column, Cell> = { path, interval, light, drill: cell(S, S?.drill, scan), anchors: cell(S, S?.anchors, scan), r2 }
    return { scan, cells, gaps: COLUMNS.filter(c => cells[c] === 'missing') }
  })
}

// ── Freshness ──────────────────────────────────────────────────────────────

export interface Freshness {
  /** The newest scan any source knows (D1 `index_schema`, the stores, `scan_runs`), and its age (s). */
  newest_scan: string | null
  newest_scan_age: number | null
  /** Per store: its newest scan and that scan's age, and its newest manifest's upload and that upload's age. */
  stores: { kind: StoreKind; newest: string | null; newest_age: number | null; manifest_ts: number | null; manifest_age: number | null }[]
}

/** `freshness`' ages: a scan's age is from its job's start (`started`, epoch s) when one is known, else from its id's
 *  own time (a date-only id is 00:00 UTC, which over-ages a scan whose job ran later that day). */
export function freshness(scans: readonly string[], stores: readonly StoreHealth[], now: number, started: ReadonlyMap<string, number> = new Map()): Freshness {
  const ageOf = (scan: string | null): number | null => {
    if (scan === null) return null
    const s = started.get(scan)
    const t = s ?? scanTime(scan) / 1000
    return Number.isNaN(t) ? null : Math.max(0, now - Math.floor(t))
  }
  const newest = scans.length ? [...scans].sort()[scans.length - 1] : null
  return {
    newest_scan: newest,
    newest_scan_age: ageOf(newest),
    stores: stores.map(s => ({
      kind: s.kind, newest: s.newest, newest_age: ageOf(s.newest),
      manifest_ts: s.manifest_ts, manifest_age: s.manifest_ts === null ? null : Math.max(0, now - s.manifest_ts),
    })),
  }
}

// ── The document ───────────────────────────────────────────────────────────

export interface HealthDoc {
  /** Epoch s the document was computed at. */
  now: number
  /** Every scan any source knows, oldest first. */
  scans: string[]
  stores: StoreHealth[]
  coverage: ScanCoverage[]
  /** The scans with a gap in any column, and how many per column. */
  gaps: Record<Column, number>
  /** Scans a finished job ran for that nothing serves (no per-scan store, in no store): not coverage rows — a
   *  scan never published has no gaps to show — but worth a look (e.g. a backfilled run of a scan purged since). */
  unserved: string[]
  freshness: Freshness
}

/** A scan job as /health reads it (D1 `scan_runs`: every run, downstream jobs and attempts included). */
export interface HealthJob { scan: string; status: string; started_ts?: number | null }

/** The /health document from the sources: the stores' reads, the per-scan path store's scans, and the scan jobs (which
 *  are running, matched by scan id; when each scan's job started, for its age). */
export function healthDoc(reads: readonly StoreRead[], perScan: readonly string[], jobs: readonly HealthJob[], now: number): HealthDoc {
  const stores = reads.map(r => storeHealth(r, now))
  const live = jobs.filter(j => j.status === 'running')
  const running = new Set(live.map(j => j.scan))
  const started = new Map<string, number>()
  for (const j of jobs) if (j.started_ts != null) started.set(j.scan, Math.min(started.get(j.scan) ?? Infinity, j.started_ts))
  // A scan is a coverage row when something serves it, or its job is still running; a finished job's scan that
  // nothing serves is `unserved` instead.
  const scans = [...new Set([...perScan, ...running, ...stores.flatMap(s => [...s.base, ...s.runs.flatMap(r => r.scans)])])].filter(s => !Number.isNaN(scanTime(s))).sort()
  const known = new Set(scans)
  const unserved = [...new Set(jobs.filter(j => j.status !== 'running' && !known.has(j.scan)).map(j => j.scan))].filter(s => !Number.isNaN(scanTime(s))).sort()
  const cov = coverage(scans, new Set(perScan), running, stores, live.length > 0)
  const gaps = Object.fromEntries(COLUMNS.map(c => [c, cov.filter(x => x.cells[c] === 'missing').length])) as Record<Column, number>
  return { now, scans, stores, coverage: cov, gaps, unserved, freshness: freshness(scans, stores, now, started) }
}

// ── Shards on a timeline (`CoverTimeline`'s rows) ──────────────────────────

/** A scan span's instants on the timeline: from its first scan to the next scan after its last in `axis` (all scans,
 *  sorted), or `now` for the newest. */
export function spanOf(first: string, last: string, axis: readonly string[], now: number): { start: number; end: number } {
  const next = axis.find(s => s > last)
  return { start: scanTime(first), end: next ? scanTime(next) : now }
}

const iso = (ms: number): string => new Date(ms).toISOString()

/** One timeline row: its segments as `pyramidCover` would report them, with the counts the type carries. */
export function row(tier: string, segments: PyramidCoverSegment[], now: number): PyramidTierCoverStatus {
  const missing = segments.filter(s => s.status === 'missing')
  return {
    tier, bin: '', maxRung: '', rungs: [], segments,
    totalExpected: segments.length,
    totalPresent: segments.filter(s => s.status === 'present').length,
    totalPending: segments.filter(s => s.status === 'pending').length,
    complete: missing.length === 0,
    firstMissingPeriod: missing[0]?.start ?? null,
    lastMaxBoundary: iso(now), dustAgeSec: 0, staleShardCount: 0,
  }
}

/** A store's stack as timeline rows, top to bottom: `base` (one shard over the base's scans), one row per level from the
 *  compaction's `level - 1` down to 0 (each run a shard over its scans' span on its level's row; a due carry's output a
 *  `pending` shard on its level, as is each output a merge in progress (or stuck) has planned and not yet listed), and
 *  for the static index a `drill` and an `anch` row (the base and each run present or missing on R2). A run whose files
 *  aren't whole on R2 is `missing`. `axis`: every scan (sorted); `now` in ms. */
export function stackTiers(s: StoreHealth, axis: readonly string[], now: number): PyramidTierCoverStatus[] {
  const seg = (first: string, last: string, status: PyramidCoverSegment['status'], label: string, key: string): PyramidCoverSegment => {
    const { start, end } = spanOf(first, last, axis, now)
    return { start: iso(start), end: iso(end), shardDur: label, status, key }
  }
  const rows: PyramidTierCoverStatus[] = []
  const baseSeg = s.base.length ? [seg(s.base[0], s.base[s.base.length - 1], s.error ? 'missing' : 'present', `${plural(s.base.length, 'scan')}`, `${s.gen} (base)`)] : []
  rows.push(row('base', baseSeg, now))
  const top = Math.max(s.compaction.level - 1, s.compaction.max_level)
  const active = s.merge && s.merge.status !== 'failed' ? s.merge : null
  const listed = new Set(s.runs.map(r => r.key))
  const planned = (active?.merges ?? []).filter(x => !listed.has(x.output))
  const merging = new Set(planned.map(x => x.output))
  const verb = active?.status === 'stuck' ? 'stuck merge' : 'merging'
  for (let lv = top; lv >= 0; lv--) {
    const segs = [
      ...s.runs.filter(r => r.level === lv).map(r => seg(r.first, r.last, r.r2 ? 'present' : 'missing', plural(r.scans.length, 'scan'), r.key)),
      ...s.carries.filter(c => c.level === lv && !merging.has(c.output)).map(c => seg(c.first, c.last, 'pending', `due carry of ${plural(c.inputs.length, 'run')} · ${plural(c.scans, 'scan')}`, c.output)),
      ...planned.filter(x => x.level === lv).map(x => seg(x.first, x.last, 'pending', `${verb} of ${plural(x.inputs.length, 'run')} · ${plural(x.scans, 'scan')}`, x.output)),
    ].sort((a, b) => a.start.localeCompare(b.start) || a.status.localeCompare(b.status))
    rows.push(row(`L${lv}`, segs, now))
  }
  if (s.kind === 'static' && s.base.length) {
    for (const [tier, label] of [['drill', 'drill'], ['anchors', 'anch']] as const) {
      if (!s.base_tiers?.[tier] && !s.runs.some(r => r.tiers?.[tier])) continue
      rows.push(row(label, [
        seg(s.base[0], s.base[s.base.length - 1], s.base_tiers?.[tier] ? 'present' : 'missing', `${tier} · base`, `${s.gen} (base)`),
        ...s.runs.map(r => seg(r.first, r.last, r.tiers?.[tier] ? 'present' : 'missing', `${tier} · ${plural(r.scans.length, 'scan')}`, r.key)),
      ], now))
    }
  }
  return rows
}

/** The coverage grid as timeline rows: one per column, one shard per scan (`na` cells left out). */
export const COLUMN_LABELS: Record<Column, string> = { path: 'path', interval: 'iv', light: 'light', drill: 'drill', anchors: 'anch', r2: 'r2' }

export function coverageTiers(cov: readonly ScanCoverage[], columns: readonly Column[], now: number): PyramidTierCoverStatus[] {
  const axis = cov.map(c => c.scan)
  return columns.map(col => row(COLUMN_LABELS[col], cov.flatMap(c => {
    const cell = c.cells[col]
    if (cell === 'na') return []
    const { start, end } = spanOf(c.scan, c.scan, axis, now)
    return [{ start: iso(start), end: iso(end), shardDur: c.scan, status: cell }]
  }), now))
}

/** The columns a document has anything in (a store that isn't configured contributes none). */
export const liveColumns = (cov: readonly ScanCoverage[]): Column[] => COLUMNS.filter(c => cov.some(x => x.cells[c] !== 'na'))
