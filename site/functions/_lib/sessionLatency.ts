/** Request latency from the session log (specs/session-log.md): every `api` event the client logged (route, status,
 *  duration, the response's `x-cache`), summarized per route and cache tier, per user, and the slowest requests —
 *  what `/admin/latency` shows. Read from D1's `session_log_batches` when asked; nothing is stored apart. */
import type { D1Database } from '@cloudflare/workers-types'
import type { LatencySummary, Req, Stat } from './sessionLatencyShape.js'
import type { SlogEvent } from './sessionLogShape.js'

export type { LatencySummary, Req, Stat }

/** Batches one summary reads at most (newest first): a few MB of JSON. */
export const MAX_BATCHES = 20_000
const SLOWEST = 25

/** A logged URL's route: its path, scan ids as `:scan`, a session id as `:sid`; the `/data/<scan>/<file>` reads by file. */
export function routeOf(u: string): string {
  const path = u.split(/[?#]/)[0]
  return path
    .replace(/\/\d{4}-\d{2}-\d{2}(T\d{4})?(?=\/|$)/g, '/:scan')
    .replace(/\/sessions\/[A-Za-z0-9_-]{8,}$/, '/sessions/:sid')
}

/** The nearest-rank `q` quantile of ascending `xs` (non-empty). */
export const quantile = (xs: number[], q: number): number => xs[Math.min(xs.length - 1, Math.max(0, Math.ceil(q * xs.length) - 1))]

function stat(rs: Req[]): Stat {
  const ms = rs.map(r => r.ms).sort((a, b) => a - b)
  return { n: ms.length, p50: quantile(ms, 0.5), p90: quantile(ms, 0.9), p99: quantile(ms, 0.99), max: ms[ms.length - 1], errors: rs.filter(r => r.s === 0 || r.s >= 400).length }
}

/** The requests of a batch's events (`api` events with a duration). */
export function requestsOf(sid: string, email: string | null, events: SlogEvent[]): Req[] {
  const out: Req[] = []
  for (const e of events) {
    if (e.k !== 'api' || typeof e.ms !== 'number' || typeof e.u !== 'string') continue
    out.push({ sid, email, t: e.t, route: routeOf(e.u), u: e.u, s: typeof e.s === 'number' ? e.s : 0, ms: e.ms, c: typeof e.c === 'string' ? e.c.toLowerCase() : '-' })
  }
  return out
}

/** Per route and cache tier (most requests first), per user (slowest p50 first), and the slowest requests. */
export function summarize(reqs: Req[], o: { since: number; batches: number; capped: boolean }): LatencySummary {
  const group = <K extends string>(key: (r: Req) => K) => {
    const m = new Map<K, Req[]>()
    for (const r of reqs) {
      const k = key(r)
      const l = m.get(k)
      if (l) l.push(r)
      else m.set(k, [r])
    }
    return m
  }
  const routes = [...group(r => `${r.route}\0${r.c}`)].map(([k, rs]) => {
    const [route, cache] = k.split('\0')
    return { route, cache, ...stat(rs) }
  }).sort((a, b) => b.n - a.n || (a.route < b.route ? -1 : a.route > b.route ? 1 : a.cache < b.cache ? -1 : 1))
  const users = [...group(r => r.email ?? '(signed out)')].map(([email, rs]) => ({ email, ...stat(rs) }))
    .sort((a, b) => b.p50 - a.p50 || (a.email < b.email ? -1 : 1))
  const slowest = [...reqs].sort((a, b) => b.ms - a.ms || a.t - b.t).slice(0, SLOWEST)
  return { since: o.since, requests: reqs.length, batches: o.batches, capped: o.capped, routes, users, slowest }
}

/** Batches one statement reads (D1 caps a result's size; a week's `events` blobs in one `.all()` can pass it). */
export const PAGE_BATCHES = 2_000

/** The summary of the batches received since `since` (epoch ms), optionally one user's or one session's: newest first,
 *  `PAGE_BATCHES` a statement (keyset on `(last_ms, sid, pid, seq)`), at most `MAX_BATCHES`. */
export async function latency(db: D1Database, f: { since: number; email?: string; sid?: string }, page = PAGE_BATCHES): Promise<LatencySummary> {
  const where = ['b.last_ms >= ?']
  const args: unknown[] = [f.since]
  if (f.email) { where.push('l.email = ?'); args.push(f.email) }
  if (f.sid) { where.push('b.sid = ?'); args.push(f.sid) }
  type Row = { sid: string; pid: string; seq: number; last_ms: number; email: string | null; events: string }
  const rows: Row[] = []
  let after: Row | null = null
  let capped = false
  for (;;) {
    const w: string[] = after ? [...where, '(b.last_ms, b.sid, b.pid, b.seq) < (?, ?, ?, ?)'] : where
    const a: unknown[] = after ? [...args, after.last_ms, after.sid, after.pid, after.seq] : args
    const r: { results: Row[] } = await db.prepare(`SELECT b.sid, b.pid, b.seq, b.last_ms, l.email, b.events FROM session_log_batches b JOIN session_log l ON l.sid = b.sid WHERE ${w.join(' AND ')} ORDER BY b.last_ms DESC, b.sid DESC, b.pid DESC, b.seq DESC LIMIT ${page}`)
      .bind(...a).all<Row>()
    rows.push(...r.results)
    if (rows.length > MAX_BATCHES) { capped = true; rows.length = MAX_BATCHES; break }
    if (r.results.length < page) break
    after = r.results[r.results.length - 1]
  }
  const reqs = rows.flatMap(x => requestsOf(x.sid, x.email, JSON.parse(x.events) as SlogEvent[]))
  return summarize(reqs, { since: f.since, batches: rows.length, capped })
}
