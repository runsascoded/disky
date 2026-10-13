/**
 * The session log's D1 side (specs/session-log.md): ingest a validated batch,
 * purge past retention, and the admin reads. Tables: `session_log` (one row
 * per session) and `session_log_batches` (one row per client flush, events as
 * a JSON array), plus the on/off switch `session_log_switch` (one row) —
 * `migrations/cw/0018_session_log.sql`.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { isErrorEvent, LIMITS, type SessionRow, type SlogBatch, type SlogEvent, type StoredEvent } from './sessionLogShape.js'

export interface Who {
  email: string | null
  via: string
}

export type IngestResult = 'stored' | 'duplicate' | 'capped'

const firstUrl = (events: SlogEvent[]): string | null => {
  const e = events.find(x => x.k === 'start' || x.k === 'nav')
  return typeof e?.u === 'string' ? e.u : null
}

/** Store one batch: the batch row (a repeat of `(sid, pid, seq)` — a retried beacon — is ignored and counts
 *  nothing), then the session row's counters. A session past `LIMITS.sessionEvents` stores nothing more. */
export async function ingest(db: D1Database, who: Who, ua: string | null, b: SlogBatch, nowS: number): Promise<IngestResult> {
  const prior = await db.prepare('SELECT n_events FROM session_log WHERE sid = ?').bind(b.sid).first<{ n_events: number }>()
  if (prior && prior.n_events + b.events.length > LIMITS.sessionEvents) return 'capped'
  const ts = b.events.map(e => e.t)
  const first = Math.min(...ts), last = Math.max(...ts)
  const ins = await db.prepare(
    'INSERT OR IGNORE INTO session_log_batches (sid, pid, seq, received_ts, first_ms, last_ms, n, events) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
  ).bind(b.sid, b.pid, b.seq, nowS, first, last, b.events.length, JSON.stringify(b.events)).run()
  if (!ins.meta.changes) return 'duplicate'
  const errors = b.events.filter(isErrorEvent).length
  await db.prepare(
    `INSERT INTO session_log (sid, email, via, build, ua, started_ms, last_ms, n_events, n_errors, n_batches, first_url, received_ts)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 1, ?10, ?11)
     ON CONFLICT (sid) DO UPDATE SET
       email = COALESCE(excluded.email, email), via = excluded.via, build = excluded.build, ua = COALESCE(excluded.ua, ua),
       started_ms = MIN(started_ms, excluded.started_ms), last_ms = MAX(last_ms, excluded.last_ms),
       n_events = n_events + excluded.n_events, n_errors = n_errors + excluded.n_errors, n_batches = n_batches + 1,
       first_url = COALESCE(first_url, excluded.first_url), received_ts = excluded.received_ts`,
  ).bind(b.sid, who.email, who.via, b.build || null, ua ? ua.slice(0, 300) : null, first, last, b.events.length, errors, firstUrl(b.events), nowS).run()
  return 'stored'
}

/** Delete everything received before `cutoffS`; a missing table (the migration not applied) is a nop. */
export async function purge(db: D1Database, cutoffS: number): Promise<{ batches: number; sessions: number } | null> {
  try {
    const b = await db.prepare('DELETE FROM session_log_batches WHERE received_ts < ?').bind(cutoffS).run()
    const s = await db.prepare('DELETE FROM session_log WHERE received_ts < ?').bind(cutoffS).run()
    return { batches: b.meta.changes ?? 0, sessions: s.meta.changes ?? 0 }
  } catch (e) {
    if (missingTable(e)) return null
    throw e
  }
}

export const retainDays = (raw: string | undefined): number => {
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? n : 30
}

export const PURGE_EVERY_MS = 3_600_000

/** Run `purge` at most once per `PURGE_EVERY_MS` per `state` (one per isolate in production). */
export async function maybePurge(db: D1Database, retainRaw: string | undefined, now: number, state: { last: number }): Promise<boolean> {
  if (now - state.last < PURGE_EVERY_MS) return false
  state.last = now
  await purge(db, Math.floor(now / 1000) - retainDays(retainRaw) * 86_400)
  return true
}

/** The switch's row: `until_ms` null = off. */
export interface SwitchRow {
  until_ms: number | null
  who: string
  ts: number
}

const missingTable = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

/** The switch's row; null = never set (off); `'missing'` = the migration isn't applied (off). */
export async function readSwitch(db: D1Database): Promise<SwitchRow | null | 'missing'> {
  try {
    return await db.prepare('SELECT until_ms, who, ts FROM session_log_switch WHERE id = 1').first<SwitchRow>()
  } catch (e) {
    if (missingTable(e)) return 'missing'
    throw e
  }
}

/** Set the switch (`untilMs` null = off) and append the change to `admin_edits` (`tbl = 'session_log_switch'`,
 *  pk `1`), in one transaction. */
export async function writeSwitch(db: D1Database, untilMs: number | null, who: string, nowS: number): Promise<SwitchRow> {
  const prior = await db.prepare('SELECT until_ms, who, ts FROM session_log_switch WHERE id = 1').first<SwitchRow>()
  const row: SwitchRow = { until_ms: untilMs, who, ts: nowS }
  await db.batch([
    db.prepare('INSERT INTO session_log_switch (id, until_ms, who, ts) VALUES (1, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET until_ms = excluded.until_ms, who = excluded.who, ts = excluded.ts')
      .bind(untilMs, who, nowS),
    db.prepare("INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES ('session_log_switch', '1', ?, ?, ?, ?, ?)")
      .bind(prior ? 'update' : 'insert', who, nowS, prior ? JSON.stringify(prior) : null, JSON.stringify(row)),
  ])
  return row
}

/** How long one isolate trusts its read of the switch: a change elsewhere reaches every tab within this. */
export const SWITCH_TTL_MS = 60_000

/** One isolate's last read of the switch (`at` null = none yet). */
export interface SwitchCache {
  at: number | null
  row: SwitchRow | null | 'missing'
}

/** `readSwitch`, cached in `cache` (one per isolate in production) for `SWITCH_TTL_MS`. Expiry is still exact: the
 *  cache holds the end instant, and callers compare it with their own clock. */
export async function cachedSwitch(db: D1Database, now: number, cache: SwitchCache): Promise<SwitchRow | null | 'missing'> {
  if (cache.at !== null && now - cache.at < SWITCH_TTL_MS) return cache.row
  cache.row = await readSwitch(db)
  cache.at = now
  return cache.row
}

export interface SessionFilter {
  email?: string
  /** epoch ms: sessions active since then. */
  since?: number
  errors?: boolean
  limit?: number
}

/** Sessions, most recently active first. `email` matches a substring, case-insensitively. */
export async function listSessions(db: D1Database, f: SessionFilter): Promise<SessionRow[]> {
  const where: string[] = []
  const args: unknown[] = []
  if (f.email) { where.push("LOWER(COALESCE(email, '')) LIKE ?"); args.push(`%${f.email.toLowerCase()}%`) }
  if (f.since !== undefined) { where.push('last_ms >= ?'); args.push(f.since) }
  if (f.errors) where.push('n_errors > 0')
  const limit = Math.min(Math.max(f.limit ?? 200, 1), 500)
  const sql = `SELECT * FROM session_log${where.length ? ` WHERE ${where.join(' AND ')}` : ''} ORDER BY last_ms DESC, sid LIMIT ${limit}`
  const r = await db.prepare(sql).bind(...args).all<SessionRow>()
  return r.results
}

/** A session's row and its events, flattened across batches: by `t`, ties by page load, batch and position. */
export async function sessionDetail(db: D1Database, sid: string): Promise<{ session: SessionRow; events: StoredEvent[] } | null> {
  const session = await db.prepare('SELECT * FROM session_log WHERE sid = ?').bind(sid).first<SessionRow>()
  if (!session) return null
  const rows = await db.prepare('SELECT pid, seq, events FROM session_log_batches WHERE sid = ? ORDER BY first_ms, pid, seq')
    .bind(sid).all<{ pid: string; seq: number; events: string }>()
  const tagged = rows.results.flatMap(r => (JSON.parse(r.events) as SlogEvent[]).map((e, i) => ({ e: { ...e, pid: r.pid, seq: r.seq } as StoredEvent, i })))
  tagged.sort((a, b) => a.e.t - b.e.t || (a.e.pid < b.e.pid ? -1 : a.e.pid > b.e.pid ? 1 : 0) || a.e.seq - b.e.seq || a.i - b.i)
  return { session, events: tagged.map(x => x.e) }
}
