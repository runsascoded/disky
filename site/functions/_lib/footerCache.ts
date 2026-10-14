/**
 * The footer LRU (specs/done/footer-lru.md): `index_row_groups` as a cache of
 * the path store's cold footers (`<tier>.groups.parquet`), one entry per
 * (scan, variant, generation), tracked in `footer_cache`.
 *
 * - **Miss** (`openIndex` finds no rows and no entry for the pointer's
 *   generation): the read serves from the cold footer as before, and queues a
 *   **fill** — claim the entry (`filling`), insert the footer's rows with
 *   `INSERT OR IGNORE` one footer group per D1 batch (each guarded on the claim
 *   still standing), then mark it `hot` in one statement that also checks the
 *   row count. A reader trusts D1 only for a `hot` entry (or an untracked one:
 *   a sync's complete set), so it never reads a partial fill; two isolates
 *   filling at once insert the same rows and only one claim wins.
 * - **Access**: a `hot` entry's `last_access_day` is written at most once a
 *   day per isolate, and only when the day changed.
 * - **Evict** (`evictFooters`): demote the least recently read `hot` entries
 *   past the budget to `evicting` (readers go cold at once), and delete the
 *   rows of entries demoted at least `graceS` ago (longer than a reader's
 *   handle lives), then their entry.
 *
 * Writes never run on a request's path: `openIndex` queues them, and the
 * middleware starts them after the response (`footerCacheDrain` under
 * `waitUntil`). Without the table (the migration not applied yet) the reader
 * runs exactly the pre-LRU query and queues nothing.
 */
import type { Env } from './auth.js'
import { isPrimary, storeKey } from './stores.js'

/** A fill claim older than this is abandoned (its isolate died): the next miss takes it over. */
export const FILL_STALE_S = 900
/** How long an `evicting` entry keeps its rows: past a reader handle's lifetime (`HANDLE_TTL`, 60 s). */
export const EVICT_GRACE_S = 120
/** The per-row overhead `bytes` counts beside the text columns (the integer columns and keys). */
export const ROW_FIXED_BYTES = 64
/** `FOOTER_CACHE_MAX_ROWS`' default: ~0.5 GB of D1 with the indexes. */
export const DEFAULT_MAX_ROWS = 1_000_000
/** A footer with more rows than this is never filled (served cold): a fill must fit a `waitUntil`. */
export const FILL_MAX_ROWS = 200_000
/** SQL bytes per fill INSERT (D1 caps a statement at 100 KB). */
const STMT_BYTES = 90_000

export type State = 'filling' | 'hot' | 'evicting'
/** One (scan, variant, generation), `variant` as D1 names it (`d1Variant`). */
export interface Key { date: string; variant: string; gen: string }
/** One `index_row_groups` row as the cold footer holds it (`FOOTER_COLS`). */
export interface FooterRow {
  rg: number | bigint
  d_min: number | bigint
  d_max: number | bigint
  p_min: string
  p_max: string
  b_max: number | bigint
  u_min: string | null
  u_max: string | null
  row_start: number | bigint
  row_end: number | bigint
  rg_json: string
}

const day = (ms: number): number => Math.floor(ms / 86_400_000)
const sec = (ms: number): number => Math.floor(ms / 1000)
const keyStr = (k: Key): string => `${k.date}\u0000${k.variant}\u0000${k.gen}`

// --- the probe ---------------------------------------------------------------------------------

/** What the reader learns about the pointer's generation in one query. */
export interface Probe {
  /** Some row of the generation is in `index_row_groups`. */
  any: boolean
  state: State | null
  startedTs: number | null
  lastAccessDay: number | null
}

/** DBs found without `footer_cache` (→ when): asked again after a minute, so a migration is picked up. */
const absent = new WeakMap<object, number>()
const ABSENT_TTL = 60_000
const isAbsent = (e: unknown): boolean => /no such table: footer_cache/.test((e as Error)?.message ?? '')

/** The generation's rows-present bit and its entry, or null when the table is missing (the caller then
 *  runs its pre-LRU query; nothing is queued). */
export async function probe(env: Env, k: Key): Promise<Probe | null> {
  const db = env.DB!
  const at = absent.get(db)
  if (at != null && Date.now() - at < ABSENT_TTL) return null
  try {
    const r = await db.prepare(
      'SELECT (SELECT 1 FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ? LIMIT 1) AS x, c.state, c.started_ts, c.last_access_day ' +
      'FROM (SELECT 1) LEFT JOIN footer_cache c ON c.date = ? AND c.variant = ? AND c.gen = ?',
    ).bind(k.date, k.variant, k.gen, k.date, k.variant, k.gen).first<{ x: number | null; state: State | null; started_ts: number | null; last_access_day: number | null }>()
    absent.delete(db)
    return { any: !!r?.x, state: r?.state ?? null, startedTs: r?.started_ts ?? null, lastAccessDay: r?.last_access_day ?? null }
  } catch (e) {
    if (!isAbsent(e)) throw e
    absent.set(db, Date.now())
    return null
  }
}

/** Whether the reader may plan from D1's rows: a `hot` entry's, or an untracked generation's (a sync's
 *  complete set — fills always claim before their first row, and eviction deletes rows before its entry). */
export const trusted = (p: Probe): boolean => p.state === 'hot' || (p.state == null && p.any)

/** Whether a cold read should queue a fill: no rows and no entry, or a fill claim gone stale. */
export const wantsFill = (p: Probe, nowMs = Date.now()): boolean =>
  (p.state == null && !p.any) || (p.state === 'filling' && (p.startedTs ?? 0) < sec(nowMs) - FILL_STALE_S)

// --- the background queue ----------------------------------------------------------------------

const queued = new Map<string, () => Promise<unknown>>()
const running = new Set<string>()

function enqueue(id: string, task: () => Promise<unknown>): void {
  if (running.has(id) || queued.has(id)) return
  queued.set(id, task)
}

/** Start every queued footer-cache write (fills, access stamps) and resolve once they settle. The
 *  middleware hands this to `waitUntil` after the response; a failure is logged, never thrown. */
export async function footerCacheDrain(): Promise<void> {
  const tasks = [...queued.entries()]
  queued.clear()
  await Promise.all(tasks.map(async ([id, task]) => {
    running.add(id)
    try {
      await task()
    } catch (e) {
      console.log(`footer cache: ${id.replace(/\u0000/g, ' ')}: ${(e as Error).message}`)
    } finally {
      running.delete(id)
    }
  }))
}

/** The access days this isolate wrote (or read), per DB and entry. */
const accessDays = new WeakMap<object, Map<string, number>>()

/** A `hot` entry was opened: queue its `last_access_day` stamp when the day moved past both the stored
 *  value and the one this isolate last wrote. */
export function noteAccess(env: Env, k: Key, stored: number | null, nowMs = Date.now()): void {
  const db = env.DB!
  let m = accessDays.get(db)
  if (!m) accessDays.set(db, (m = new Map()))
  const ks = keyStr(k)
  const today = day(nowMs)
  if (Math.max(stored ?? 0, m.get(ks) ?? 0) >= today) return
  m.set(ks, today)
  enqueue(`access\u0000${ks}`, () => db.prepare('UPDATE footer_cache SET last_access_day = ? WHERE date = ? AND variant = ? AND gen = ? AND last_access_day < ?')
    .bind(today, k.date, k.variant, k.gen, today).run())
}

/** Queue a fill of `k` from `rows` (the cold footer, a batch per footer group). */
export function queueFill(env: Env, k: Key, rows: () => AsyncIterable<FooterRow[]>): void {
  enqueue(`fill\u0000${keyStr(k)}`, () => fill(env, k, rows))
}

// --- the fill ----------------------------------------------------------------------------------

const lit = (v: string | null): string => (v == null ? 'NULL' : `'${v.replace(/'/g, "''")}'`)
const int = (v: number | bigint): string => {
  if (typeof v === 'bigint') return String(v)
  if (!Number.isSafeInteger(v)) throw new Error(`not an integer: ${v}`)
  return String(v)
}
/** A row's `bytes` (as `dt_cloud.index_footer.row_bytes` counts it). */
export const rowBytes = (r: FooterRow): number =>
  r.p_min.length + r.p_max.length + (r.u_min?.length ?? 0) + (r.u_max?.length ?? 0) + r.rg_json.length + ROW_FIXED_BYTES

export type FillResult = 'filled' | 'busy' | 'lost'

/** Fill `k` (exported for tests; readers queue it with `queueFill`): `busy` when another claim holds the
 *  entry, `lost` when the entry changed under the fill (evicted, retired) or its rows don't add up. */
export async function fill(env: Env, k: Key, rows: () => AsyncIterable<FooterRow[]>, nowMs = Date.now()): Promise<FillResult> {
  const db = env.DB!
  const now = sec(nowMs)
  const claim = await db.prepare(
    "INSERT INTO footer_cache (date, variant, gen, state, started_ts) VALUES (?, ?, ?, 'filling', ?) " +
    "ON CONFLICT (date, variant, gen) DO UPDATE SET started_ts = excluded.started_ts WHERE footer_cache.state = 'filling' AND footer_cache.started_ts < ?",
  ).bind(k.date, k.variant, k.gen, now, now - FILL_STALE_S).run()
  if (!claim.meta.changes) return 'busy'
  const sec2 = !isPrimary(env)
  const cols = `${sec2 ? 'store, ' : ''}date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json`
  const pre = `${sec2 ? `${lit(storeKey(env))}, ` : ''}${lit(k.date)}, ${lit(k.variant)}, ${lit(k.gen)}`
  const head = `INSERT OR IGNORE INTO index_row_groups (${cols}) SELECT * FROM (VALUES `
  const tail = ") WHERE EXISTS (SELECT 1 FROM footer_cache WHERE date = ? AND variant = ? AND gen = ? AND state = 'filling')"
  const stmt = (tuples: string[]) => db.prepare(head + tuples.join(',') + tail).bind(k.date, k.variant, k.gen)
  let n = 0
  let bytes = 0
  for await (const group of rows()) {
    const stmts = []
    let cur: string[] = []
    let size = head.length + tail.length
    for (const r of group) {
      const t = `(${pre}, ${int(r.rg)}, ${int(r.d_min)}, ${int(r.d_max)}, ${lit(r.p_min)}, ${lit(r.p_max)}, ${int(r.b_max)}, ${lit(r.u_min)}, ${lit(r.u_max)}, ${int(r.row_start)}, ${int(r.row_end)}, ${lit(r.rg_json)})`
      if (cur.length && size + t.length + 1 > STMT_BYTES) {
        stmts.push(stmt(cur))
        cur = []
        size = head.length + tail.length
      }
      cur.push(t)
      size += t.length + 1
      n += 1
      bytes += rowBytes(r)
    }
    if (cur.length) stmts.push(stmt(cur))
    if (stmts.length) await db.batch(stmts)
  }
  // Hot only when every row landed: the count check and the flip are one statement.
  const done = await db.prepare(
    "UPDATE footer_cache SET state = 'hot', rows = ?, bytes = ?, filled_ts = ?, last_access_day = ? " +
    "WHERE date = ? AND variant = ? AND gen = ? AND state = 'filling' AND started_ts = ? " +
    'AND (SELECT COUNT(*) FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ?) = ?',
  ).bind(n, bytes, sec(Date.now()), day(nowMs), k.date, k.variant, k.gen, now, k.date, k.variant, k.gen, n).run()
  return done.meta.changes ? 'filled' : 'lost'
}

// --- eviction ----------------------------------------------------------------------------------

export interface Entry extends Key { rows: number; bytes: number; lastAccessDay: number }
export interface Budget { maxRows: number; maxBytes: number | null }
export interface EvictResult {
  budget: Budget
  /** The hot entries' totals before this pass. */
  hot: { entries: number; rows: number; bytes: number }
  /** Hot entries demoted to `evicting` by this pass (least recently read first). */
  demoted: Entry[]
  /** `evicting` entries whose rows (and entry) this pass deleted. */
  deleted: Entry[]
}

/** The deployment's budget: `FOOTER_CACHE_MAX_ROWS` (default `DEFAULT_MAX_ROWS`) and
 *  `FOOTER_CACHE_MAX_BYTES` (default none). */
export function footerBudget(env: Env): Budget {
  const pos = (v: string | undefined, name: string): number | null => {
    if (v == null || v === '') return null
    const n = Number(v)
    if (!Number.isFinite(n) || n < 0) throw new Error(`${name}: not a non-negative number: ${v}`)
    return n
  }
  return { maxRows: pos(env.FOOTER_CACHE_MAX_ROWS, 'FOOTER_CACHE_MAX_ROWS') ?? DEFAULT_MAX_ROWS, maxBytes: pos(env.FOOTER_CACHE_MAX_BYTES, 'FOOTER_CACHE_MAX_BYTES') }
}

type EntryRow = { date: string; variant: string; gen: string; rows: number; bytes: number; last_access_day: number }
const entry = (r: EntryRow): Entry => ({ date: r.date, variant: r.variant, gen: r.gen, rows: r.rows, bytes: r.bytes, lastAccessDay: r.last_access_day })

/** One eviction pass (the admin route and `dt-cloud footer-evict` run the same SQL): demote the least
 *  recently read hot entries (ties: the older scan first) until the rest fit `budget`, then delete the
 *  rows and entry of every entry demoted at least `graceS` ago. `dryRun`: report, write nothing. */
export async function evictFooters(db: D1Database, budget: Budget, { graceS = EVICT_GRACE_S, nowMs = Date.now(), dryRun = false }: { graceS?: number; nowMs?: number; dryRun?: boolean } = {}): Promise<EvictResult> {
  const now = sec(nowMs)
  const cols = 'date, variant, gen, rows, bytes, last_access_day'
  const { results: hot } = await db.prepare(`SELECT ${cols} FROM footer_cache WHERE state = 'hot' ORDER BY last_access_day, date, variant, gen`).all<EntryRow>()
  let rows = hot.reduce((s, r) => s + r.rows, 0)
  let bytes = hot.reduce((s, r) => s + r.bytes, 0)
  const totals = { entries: hot.length, rows, bytes }
  const over = () => rows > budget.maxRows || (budget.maxBytes != null && bytes > budget.maxBytes)
  const demoted: Entry[] = []
  for (const r of hot) {
    if (!over()) break
    demoted.push(entry(r))
    rows -= r.rows
    bytes -= r.bytes
  }
  if (!dryRun && demoted.length) {
    await db.batch(demoted.map(e => db.prepare("UPDATE footer_cache SET state = 'evicting', started_ts = ? WHERE date = ? AND variant = ? AND gen = ? AND state = 'hot'").bind(now, e.date, e.variant, e.gen)))
  }
  const { results: due } = await db.prepare(`SELECT ${cols} FROM footer_cache WHERE state = 'evicting' AND started_ts <= ? ORDER BY last_access_day, date, variant, gen`).bind(now - graceS).all<EntryRow>()
  const deleted = due.map(entry)
  if (!dryRun) {
    // Rows, then the entry, in one transaction per entry.
    for (const e of deleted) {
      await db.batch([
        db.prepare('DELETE FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ?').bind(e.date, e.variant, e.gen),
        db.prepare("DELETE FROM footer_cache WHERE date = ? AND variant = ? AND gen = ? AND state = 'evicting'").bind(e.date, e.variant, e.gen),
      ])
    }
  }
  return { budget, hot: totals, demoted, deleted }
}
