import type { D1Database } from '@cloudflare/workers-types'
import { d1SessionLogStore } from '@open-athena/auth/session-log/d1'
import type { SlackEnv } from './slack.js'

export const MONITOR_SCOPE = 'gcs:session-monitor:read'
export const MONITOR_END_MS = Date.UTC(2026, 9, 21)
export const POLICY = { window: 600, cooldown: 3600, quiet: 1800, dailyCap: 3, batches: 200, lease: 90 } as const

export interface Counts { batches: number; events: number; exceptions: number; failures: number; slow: number; affected: number; latest: number }
export interface State {
  version: number; lease_until: number; checked_s: number; signal_s: number; alert_s: number
  day: string; attempts: number; thread_ts: string | null; delivery: string
}
export interface Decision { alert: boolean; reason: string; next: State }

/** No client timestamps, identities, paths, query text, messages or stacks leave the aggregate query. */
export async function counts(db: D1Database, now: number): Promise<Counts> {
  const row = await db.prepare(`
    WITH batches AS MATERIALIZED (
      SELECT sid, received_ts, events FROM session_log_batches WHERE received_ts > ? AND received_ts <= ? LIMIT ?
    ), events AS (
      SELECT sid, received_ts,
        json_extract(e.value, '$.k') AS k,
        CASE WHEN json_type(e.value, '$.s') IN ('integer', 'real') THEN json_extract(e.value, '$.s') END AS s,
        CASE WHEN json_type(e.value, '$.ms') IN ('integer', 'real') THEN json_extract(e.value, '$.ms') END AS ms,
        json_extract(e.value, '$.err') AS err
      FROM batches, json_each(batches.events) e
      WHERE (SELECT COUNT(*) FROM batches) <= ?
    ), classified AS (
      SELECT *, k IN ('error', 'reject') AS exception,
        (k = 'console' OR (k = 'api' AND ((s = 0 AND COALESCE(err, '') != 'abort') OR s >= 500))) AS failure,
        (k = 'api' AND s > 0 AND s < 400 AND ms >= 10000) AS slow
      FROM events
    ) SELECT (SELECT COUNT(*) FROM batches) AS batches, COUNT(*) AS events,
      COALESCE(SUM(exception), 0) AS exceptions, COALESCE(SUM(failure), 0) AS failures,
      COALESCE(SUM(slow), 0) AS slow,
      COUNT(DISTINCT CASE WHEN exception OR failure OR slow THEN sid END) AS affected,
      COALESCE(MAX(CASE WHEN exception OR failure OR slow THEN received_ts END), 0) AS latest
    FROM classified
  `).bind(now - POLICY.window, now, POLICY.batches + 1, POLICY.batches).first<Counts>()
  if (!row) throw new Error('session monitor aggregate missing')
  if (row.batches > POLICY.batches) throw new Error('session monitor batch budget exceeded')
  return row
}

/** Global circuit breaker: quiet recovery is silent; new evidence is needed for a follow-up. */
export function decide(prior: State, c: Counts, now: number): Decision {
  const day = new Date(now * 1000).toISOString().slice(0, 10)
  const signal = c.exceptions > 0 || c.failures >= 3 || c.slow >= 3
  const next: State = {
    ...prior, version: prior.version + 1, lease_until: 0, checked_s: now,
    day, attempts: prior.day === day ? prior.attempts : 0,
    signal_s: signal ? Math.max(prior.signal_s, c.latest) : prior.signal_s,
    thread_ts: now - prior.signal_s >= POLICY.quiet ? null : prior.thread_ts,
  }
  const reason = !signal ? 'quiet' : c.latest <= prior.alert_s ? 'already-seen'
    : now - prior.alert_s < POLICY.cooldown ? 'cooldown' : next.attempts >= POLICY.dailyCap ? 'daily-cap' : 'alert'
  if (reason === 'alert') {
    next.attempts++
    next.alert_s = now
    next.lease_until = now + POLICY.lease
    next.delivery = 'attempting'
  }
  return { alert: reason === 'alert', reason, next }
}

/** Slack contents are entirely constructed from counts and a fixed authenticated admin link. */
export function alertBody(c: Counts, thread: string | null, channel: string): Record<string, unknown> {
  return {
    channel, ...(thread ? { thread_ts: thread, reply_broadcast: false } : {}),
    text: `GCS session-log alert (last 10 minutes): ${c.exceptions} uncaught errors/rejections, ${c.failures} request/console failures, ${c.slow} successful requests ≥10s; ${c.affected} affected sessions. <https://gcs.oa.dev/admin/sessions|Admin sessions>. Notifications limited to 3/day, at least 1h apart.`,
    unfurl_links: false, unfurl_media: false, parse: 'none',
  }
}

/** A single bounded attempt. A timeout/429/ambiguous delivery consumes the same budget as success. */
export async function sendAlert(env: SlackEnv, body: Record<string, unknown>): Promise<{ ok: boolean; ts?: string }> {
  try {
    const res = await fetch('https://slack.com/api/chat.postMessage', {
      method: 'POST', signal: AbortSignal.timeout(8000),
      headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}`, 'content-type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
    })
    if (!res.ok) return { ok: false }
    const result = await res.json() as { ok?: boolean; ts?: string }
    return result.ok && typeof result.ts === 'string' ? { ok: true, ts: result.ts } : { ok: false }
  } catch {
    // Transport failures (including unknown delivery) are expected states, never immediate retries.
    return { ok: false }
  }
}

export async function check(
  db: D1Database,
  env: SlackEnv,
  now: number,
  dry: boolean,
  send: typeof sendAlert = sendAlert,
): Promise<Record<string, unknown>> {
  const sw = await d1SessionLogStore(db).readSwitch()
  if (!sw || sw === 'missing' || sw.until_ms === null || sw.until_ms <= now * 1000 || now * 1000 >= MONITOR_END_MS) return { status: 'disabled' }
  // Pin all reads/writes to primary: a stale replica must not replenish the budget or erase a thread.
  const prior = await db.prepare('SELECT version, lease_until, checked_s, signal_s, alert_s, day, attempts, thread_ts, delivery FROM session_monitor WHERE id = 1').first<State>()
  if (!prior) throw new Error('session monitor state missing')
  if (!dry && (prior.lease_until > now || now - prior.checked_s < 240)) return { status: 'already-checked' }
  const c = await counts(db, now)
  const decision = decide(prior, c, now)
  if (dry) return { status: 'dry-run', decision: decision.reason, counts: c }
  if (!env.SLACK_BOT_TOKEN || !env.SLACK_ADMIN_CHANNEL) throw new Error('session monitor Slack not configured')
  const n = decision.next
  const claim = await db.prepare(`UPDATE session_monitor SET version = ?, lease_until = ?, checked_s = ?, signal_s = ?, alert_s = ?, day = ?, attempts = ?, thread_ts = ?, delivery = ? WHERE id = 1 AND version = ? AND lease_until <= ?`)
    .bind(n.version, n.lease_until, n.checked_s, n.signal_s, n.alert_s, n.day, n.attempts, n.thread_ts, n.delivery, prior.version, now).run()
  if (!claim.meta.changes) return { status: 'already-checked' }
  if (!decision.alert) return { status: decision.reason, counts: c }
  const result = await send(env, alertBody(c, n.thread_ts, env.SLACK_ADMIN_CHANNEL))
  await db.prepare('UPDATE session_monitor SET thread_ts = COALESCE(?, thread_ts), delivery = ?, lease_until = 0 WHERE id = 1 AND version = ?')
    .bind(result.ok ? (n.thread_ts ?? result.ts ?? null) : null, result.ok ? 'sent' : 'failed-or-unknown', n.version).run()
  return { status: result.ok ? 'alerted' : 'delivery-failed-or-unknown', counts: c }
}
