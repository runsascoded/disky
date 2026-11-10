/**
 * The activity feed's D1 side and sinks (specs/done/activity-feed.md): the per-kind config (`activity_kinds`), bursts
 * (`activity_bursts`), server events (`activity_events`; a burst's client errors are read from its session-log batches,
 * `session_log_batches.burst_id`), and each burst's Slack thread / Discord message
 * (`activity_posts`, which also claims a post so two isolates never open two threads for one burst). Everything here is
 * best-effort: callers run it in `waitUntil`, and a failure is logged, never raised into the request that caused it.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { BATCH_GAP_S } from './batchGroups.js'
import { slackApi, type SlackEnv, slackScopes, slackPeople } from './slack.js'
import { dataUriImage, identiconPng, identiconSeed, publishAvatar } from './publicAvatar.js'
import type { SlogBatch, SlogEvent } from './sessionLogShape.js'
import {
  activityEventLine, addPages, addTrail, type Burst, DISCORD, emptyTrail, type Fmt, fmtGap, highlights, joins, KIND_DEFS, type KindConf,
  type KindRow, type KindsConf, actorName, mergeKinds, renderDetails, renderDiscord, renderHeadline, renderLines, renderSummary, reported, senderName,
  type ServerEvent, sessionUrl,
  SLACK, tallyClient,
} from './activityShape.js'

export interface ActivityEnv extends SlackEnv {
  DB?: D1Database
  /** A Discord channel webhook URL (a secret). Unset = no Discord. */
  DISCORD_ADMIN_WEBHOOK?: string
  /** Seeds the generated faces (`identiconSeed`); unset = posts without a face of their own. */
  SESSION_SECRET?: string
}

export type Wait = (p: Promise<unknown>) => void

const missingTable = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

// ---- config ----

/** How long one isolate trusts its read of `activity_kinds`. */
export const KINDS_TTL_MS = 60_000
export interface KindsCache { at: number | null; db: D1Database | null; conf: KindsConf }
const kindsCache: KindsCache = { at: null, db: null, conf: mergeKinds([]) }

/** The config: defaults, overridden by `activity_kinds` rows; cached per isolate (and D1 binding) for `KINDS_TTL_MS`.
 *  A missing table (the migration not applied) reads as the defaults. */
export async function readKinds(db: D1Database, now: number = Date.now(), cache: KindsCache = kindsCache, fresh = false): Promise<KindsConf> {
  if (!fresh && cache.db === db && cache.at !== null && now - cache.at < KINDS_TTL_MS) return cache.conf
  let rows: KindRow[] = []
  try {
    rows = (await db.prepare('SELECT kind, log, notify FROM activity_kinds').all<KindRow>()).results
  } catch (e) {
    if (!missingTable(e)) throw e
  }
  cache.conf = mergeKinds(rows)
  cache.at = now
  cache.db = db
  return cache.conf
}

/** Set one kind's config and append the change to `admin_edits` (`tbl = 'activity_kinds'`). Refreshes this isolate's
 *  cache. */
export async function writeKind(db: D1Database, kind: string, conf: KindConf, who: string, nowS: number): Promise<KindsConf> {
  const prior = await db.prepare('SELECT kind, log, notify, who, ts FROM activity_kinds WHERE kind = ?').bind(kind).first()
  const row = { kind, log: conf.log ? 1 : 0, notify: conf.notify, who, ts: nowS }
  await db.batch([
    db.prepare(`INSERT INTO activity_kinds (kind, log, notify, who, ts) VALUES (?1, ?2, ?3, ?4, ?5)
                ON CONFLICT (kind) DO UPDATE SET log = excluded.log, notify = excluded.notify, who = excluded.who, ts = excluded.ts`)
      .bind(kind, row.log, row.notify, who, nowS),
    db.prepare("INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES ('activity_kinds', ?, ?, ?, ?, ?, ?)")
      .bind(kind, prior ? 'update' : 'insert', who, nowS, prior ? JSON.stringify(prior) : null, JSON.stringify(row)),
  ])
  return readKinds(db, Date.now(), kindsCache, true)
}

// ---- bursts ----

interface BurstRow {
  id: number; actor: string; via: string; origin: string; first_ts: number; last_ts: number
  counts: string; pages: string; trail: string; sids: string; rendered_ms: number | null; dirty: number
}
export type StoredBurst = Burst & { rendered_ms: number | null; dirty: boolean }

const parse = (r: BurstRow): StoredBurst => ({
  id: r.id, actor: r.actor, via: r.via, origin: r.origin, first_ts: r.first_ts, last_ts: r.last_ts,
  counts: JSON.parse(r.counts), pages: JSON.parse(r.pages), trail: JSON.parse(r.trail), sids: JSON.parse(r.sids),
  rendered_ms: r.rendered_ms, dirty: !!r.dirty,
})

export async function loadBurst(db: D1Database, id: number): Promise<StoredBurst | null> {
  const r = await db.prepare('SELECT * FROM activity_bursts WHERE id = ?').bind(id).first<BurstRow>()
  return r ? parse(r) : null
}

/** How far back a tab's session resumes its own earlier burst (`burstFor`'s `sid`). */
export const RESUME_S = 7 * 86400

/** The actor's burst for an event at `ts`: their latest, if within `BATCH_GAP_S`; else, given the event's session `sid`,
 *  the newest burst (within `RESUME_S`) that session was already in, so one tab keeps one thread (`resumed` is the gap,
 *  in seconds); else a new one. */
export async function burstFor(
  db: D1Database, actor: string, via: string, origin: string, ts: number, gapS: number = BATCH_GAP_S, sid?: string,
): Promise<{ id: number; resumed?: number }> {
  const latest = await db.prepare('SELECT id, last_ts FROM activity_bursts WHERE actor = ? ORDER BY last_ts DESC LIMIT 1')
    .bind(actor).first<{ id: number; last_ts: number }>()
  const extend = (id: number) => db.prepare('UPDATE activity_bursts SET first_ts = MIN(first_ts, ?1), last_ts = MAX(last_ts, ?1), via = ?2 WHERE id = ?3')
    .bind(ts, via, id).run()
  if (latest && joins(latest, ts, gapS)) {
    await extend(latest.id)
    return { id: latest.id }
  }
  if (sid) {
    const prior = await db.prepare(
      `SELECT id, last_ts FROM activity_bursts WHERE actor = ? AND last_ts >= ? AND last_ts <= ?
       AND EXISTS (SELECT 1 FROM json_each(sids) WHERE value = ?) ORDER BY last_ts DESC LIMIT 1`,
    ).bind(actor, ts - RESUME_S, ts, sid).first<{ id: number; last_ts: number }>()
    if (prior) {
      await extend(prior.id)
      const gap = ts - prior.last_ts
      return { id: prior.id, ...(gap > gapS ? { resumed: gap } : {}) }
    }
  }
  const r = await db.prepare(
    `INSERT INTO activity_bursts (actor, via, origin, first_ts, last_ts, counts, pages, trail, sids, dirty)
     VALUES (?, ?, ?, ?, ?, '{}', '{}', ?, '[]', 0) RETURNING id`,
  ).bind(actor, via, origin, ts, ts, JSON.stringify(emptyTrail())).first<{ id: number }>()
  return { id: r!.id }
}

/** Add to a burst's counts (atomically, per kind), its pages (by count, and `trail`: in order) and its sessions. */
export async function bump(
  db: D1Database, id: number, counts: Record<string, number>, pages: Record<string, number> = {}, sid?: string, trail: readonly string[] = [],
): Promise<void> {
  const kinds = Object.entries(counts).filter(([, n]) => n > 0)
  if (kinds.length) {
    const set = kinds.map((_, i) => `'$.${kinds[i][0]}', CAST(COALESCE(json_extract(counts, '$.${kinds[i][0]}'), 0) + ?${i + 2} AS INTEGER)`).join(', ')
    await db.prepare(`UPDATE activity_bursts SET counts = json_set(counts, ${set}) WHERE id = ?1`).bind(id, ...kinds.map(([, n]) => n)).run()
  }
  if (Object.keys(pages).length || trail.length || sid) {
    const r = await db.prepare('SELECT pages, trail, sids FROM activity_bursts WHERE id = ?').bind(id).first<{ pages: string; trail: string; sids: string }>()
    if (!r) return
    const sids: string[] = JSON.parse(r.sids)
    if (sid && !sids.includes(sid)) sids.push(sid)
    await db.prepare('UPDATE activity_bursts SET pages = ?, trail = ?, sids = ? WHERE id = ?')
      .bind(JSON.stringify(addPages(JSON.parse(r.pages), pages)), JSON.stringify(addTrail(JSON.parse(r.trail), trail)), JSON.stringify(sids.slice(-50)), id).run()
  }
}

/** A burst's server events, oldest first. */
/** A burst's events: its server events (`activity_events`) and its client errors (`batchErrors`), in time order. */
export async function burstEvents(db: D1Database, b: Pick<StoredBurst, 'id' | 'actor' | 'via'>, conf: KindsConf): Promise<ServerEvent[]> {
  const r = await db.prepare('SELECT ts, kind, actor, via, method, route, line, detail FROM activity_events WHERE burst_id = ? ORDER BY ts, id')
    .bind(b.id).all<Omit<ServerEvent, 'detail'> & { detail: string | null }>()
  const events = r.results.map(e => ({ ...e, detail: e.detail ? JSON.parse(e.detail) : undefined }))
  const errors = await batchErrors(db, b, conf)
  return errors.length ? [...events, ...errors].sort((x, y) => x.ts - y.ts) : events
}

/** Client errors a burst shows as lines (its thread's); past this they are only counted. */
export const MAX_ERROR_EVENTS = 100

/** A burst's client errors, read from the session-log batches fed into it (`burst_id`): each on the page it happened
 *  on, `detail.sid` its session; the first `MAX_ERROR_EVENTS`. */
export async function batchErrors(db: D1Database, b: Pick<StoredBurst, 'id' | 'actor' | 'via'>, conf: KindsConf): Promise<ServerEvent[]> {
  const r = await db.prepare('SELECT sid, events, received_ts FROM session_log_batches WHERE burst_id = ? ORDER BY first_ms, sid, pid, seq')
    .bind(b.id).all<{ sid: string; events: string; received_ts: number }>()
  return r.results
    .flatMap(({ sid, events, received_ts }) => tallyClient(JSON.parse(events) as SlogEvent[], conf, received_ts * 1000).errors.map((e): ServerEvent => ({
      ts: e.ts, kind: 'error', actor: b.actor, via: b.via, method: '', route: e.page ?? '', line: e.line, detail: { sid },
    })))
    .sort((x, y) => x.ts - y.ts)
    .slice(0, MAX_ERROR_EVENTS)
}

export interface Actor { actor: string; via: string; origin: string }

/** Record a request's server events (those whose kind is recorded), then report the burst. */
export async function recordServer(env: ActivityEnv, who: Actor, events: ServerEvent[], wait: Wait, now: number = Date.now()): Promise<number | null> {
  const db = env.DB
  if (!db || !events.length) return null
  try {
    const conf = await readKinds(db, now)
    const kept = events.filter(e => conf[e.kind]?.log)
    if (!kept.length) return null
    const { id } = await burstFor(db, who.actor, who.via, who.origin, kept[0].ts)
    await db.batch(kept.map(e => db.prepare(
      'INSERT INTO activity_events (burst_id, ts, kind, actor, via, method, route, line, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
    ).bind(id, e.ts, e.kind, e.actor, e.via, e.method, e.route, e.line, e.detail ? JSON.stringify(e.detail) : null)))
    const counts: Record<string, number> = {}
    for (const e of kept) counts[e.kind] = (counts[e.kind] ?? 0) + 1
    await bump(db, id, counts)
    await report(env, id, kept.some(e => conf[e.kind].notify === 'line'), wait, now)
    return id
  } catch (e) {
    if (!missingTable(e)) console.log(`activity: record failed: ${(e as Error).message}`)
    return null
  }
}

/** Count a stored session-log batch's events into its actor's burst and link the batch to it (`burst_id`: its errors
 *  are the burst's error lines), then report it: at once when an error is a `line` kind, else throttled. */
export async function feedClient(
  env: ActivityEnv, who: Actor, batch: Pick<SlogBatch, 'sid' | 'pid' | 'seq' | 'events'>, wait: Wait, now: number = Date.now(),
): Promise<number | null> {
  const db = env.DB
  const { sid, events } = batch
  if (!db || !events.length) return null
  try {
    const conf = await readKinds(db, now)
    const { counts, pages, trail, errors } = tallyClient(events, conf, now)
    if (!Object.keys(counts).length) return null
    const ts = Math.floor(Math.max(...events.map(e => e.t)) / 1000)
    const at = Math.min(ts, Math.floor(now / 1000))
    const { id, resumed } = await burstFor(db, who.actor, who.via, who.origin, at, BATCH_GAP_S, sid)
    if (resumed !== undefined) {
      // The resumption is the thread's line (where the tab came back to, after how long); counted in the summary.
      await db.prepare("INSERT INTO activity_events (burst_id, ts, kind, actor, via, method, route, line, detail) VALUES (?, ?, 'resume', ?, ?, '', ?, ?, ?)")
        .bind(id, at, who.actor, who.via, trail.at(-1) ?? '', `after ${fmtGap(resumed)}`, JSON.stringify({ sid })).run()
      counts.resume = 1
    }
    await db.prepare('UPDATE session_log_batches SET burst_id = ? WHERE sid = ? AND pid = ? AND seq = ?').bind(id, sid, batch.pid, batch.seq).run()
    await bump(db, id, counts, pages, sid, trail)
    await report(env, id, (errors.length > 0 && conf.error?.notify === 'line') || (resumed !== undefined && conf.resume?.notify === 'line'), wait, now)
    return id
  } catch (e) {
    if (!missingTable(e)) console.log(`activity: client feed failed: ${(e as Error).message}`)
    return null
  }
}

// ---- sinks ----

/** Count-only changes re-render a burst at most this often. */
export const THROTTLE_MS = 20_000
/** A claim on a post older than this is abandoned (its poster failed) and may be retaken. */
export const CLAIM_STALE_MS = 60_000
const PENDING = 'pending'

export const sinks = (env: ActivityEnv): { slack: boolean; discord: boolean } => ({
  slack: !!(env.SLACK_BOT_TOKEN && env.SLACK_ADMIN_CHANNEL),
  discord: !!env.DISCORD_ADMIN_WEBHOOK,
})

const sleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms))

/** Re-render a burst in its sinks: at once when `urgent` (a `line` event) or the last render is `THROTTLE_MS` old;
 *  otherwise mark it dirty and render once the window ends (in `wait`). */
export async function report(env: ActivityEnv, id: number, urgent: boolean, wait: Wait, now: number = Date.now()): Promise<void> {
  const db = env.DB!
  const s = sinks(env)
  if (!s.slack && !s.discord) return
  const b = await loadBurst(db, id)
  if (!b) return
  const conf = await readKinds(db, now)
  if (!reported(b.counts, conf)) return
  const since = b.rendered_ms === null ? Infinity : now - b.rendered_ms
  if (!urgent && since < THROTTLE_MS) {
    await db.prepare('UPDATE activity_bursts SET dirty = 1 WHERE id = ?').bind(id).run()
    wait(sleep(THROTTLE_MS - since + 250).then(() => trailing(env, id)).catch(e => console.log(`activity: trailing render failed: ${(e as Error).message}`)))
    return
  }
  await render(env, b, conf, now)
}

async function trailing(env: ActivityEnv, id: number, now: number = Date.now()): Promise<void> {
  const b = await loadBurst(env.DB!, id)
  if (!b?.dirty || (b.rendered_ms !== null && now - b.rendered_ms < THROTTLE_MS)) return
  await render(env, b, await readKinds(env.DB!, now), now)
}

/** Post or edit one part of a burst in one sink: the first caller claims it (`activity_posts`) and posts, later ones
 *  edit the posted message; a part still being posted elsewhere is skipped (that render carries the latest). */
export async function ensurePost(
  db: D1Database, burst: number, sink: string, part: string, now: number,
  post: () => Promise<string | null>, edit: (msg: string) => Promise<unknown>,
): Promise<string | null> {
  const row = await db.prepare('SELECT msg_id, claimed_ms FROM activity_posts WHERE burst_id = ? AND sink = ? AND part = ?')
    .bind(burst, sink, part).first<{ msg_id: string; claimed_ms: number }>()
  if (row && row.msg_id !== PENDING) { await edit(row.msg_id); return row.msg_id }
  const claim = row
    ? await db.prepare('UPDATE activity_posts SET claimed_ms = ? WHERE burst_id = ? AND sink = ? AND part = ? AND msg_id = ? AND claimed_ms < ?')
      .bind(now, burst, sink, part, PENDING, now - CLAIM_STALE_MS).run()
    : await db.prepare('INSERT OR IGNORE INTO activity_posts (burst_id, sink, part, msg_id, claimed_ms) VALUES (?, ?, ?, ?, ?)')
      .bind(burst, sink, part, PENDING, now).run()
  if (!claim.meta.changes) return null
  const msg = await post()
  if (msg) await db.prepare('UPDATE activity_posts SET msg_id = ? WHERE burst_id = ? AND sink = ? AND part = ?').bind(msg, burst, sink, part).run()
  else await db.prepare('DELETE FROM activity_posts WHERE burst_id = ? AND sink = ? AND part = ? AND msg_id = ?').bind(burst, sink, part, PENDING).run()
  return msg
}

/** Re-render bursts in their sinks now, from their events (a format change reaching existing threads, or errors
 *  linked late); returns the ids rendered. */
export async function rerender(env: ActivityEnv, ids: readonly number[], now: number = Date.now()): Promise<number[]> {
  const conf = await readKinds(env.DB!, now)
  const done: number[] = []
  for (const id of ids) {
    const b = await loadBurst(env.DB!, id)
    if (!b || !reported(b.counts, conf)) continue
    await render(env, b, conf, now)
    done.push(id)
  }
  return done
}

export interface Sender { name: string; icon: string | null }

/** Who a burst's posts are sent as: a signed-in actor's profile name and face, a grant's holder name (else its label,
 *  else `grant <id prefix>`) and face; without a face, a generated identicon (seeded by `SESSION_SECRET`), never the
 *  bot's own icon. Faces go through `public_avatars` so the sinks can fetch them; a missing table or secret just
 *  leaves the post without one. */
export async function senderOf(env: ActivityEnv, b: Pick<Burst, 'actor' | 'via' | 'origin'>, now: number): Promise<Sender> {
  const db = env.DB!
  let name = actorName(b.actor)
  let face: string | null = null
  if (b.via === 'grant' && b.actor.startsWith('grant:')) {
    const key = b.actor.slice('grant:'.length)
    const g = await db.prepare(
      'SELECT id, name, subject_json FROM grants WHERE id = ?1 OR name = ?1 ORDER BY (id = ?1) DESC, (revoked_at IS NULL) DESC, created_at DESC LIMIT 1',
    ).bind(key).first<{ id: string; name: string | null; subject_json: string | null }>()
    if (g) {
      let subject: { name?: string; avatar?: string } = {}
      try { subject = g.subject_json ? JSON.parse(g.subject_json) : {} } catch { /* a malformed subject reads as none */ }
      name = subject.name?.trim() || g.name?.trim() || `grant ${g.id.slice(0, 4)}`
      face = subject.avatar ?? null
    }
  } else if (b.via === 'session') {
    const p = await db.prepare('SELECT name, avatar FROM profiles WHERE email = ?').bind(b.actor).first<{ name: string | null; avatar: string | null }>()
    name = senderName(b.actor, p?.name)
    face = p?.avatar ?? null
  }
  const nowS = Math.floor(now / 1000)
  try {
    const img = dataUriImage(face)
    if (img) return { name, icon: await publishAvatar(db, b.origin, img, nowS) }
    if (!env.SESSION_SECRET) return { name, icon: null }
    const gen = { type: 'image/png', bytes: identiconPng(await identiconSeed(env.SESSION_SECRET, b.actor)) }
    return { name, icon: await publishAvatar(db, b.origin, gen, nowS) }
  } catch (e) {
    if (!missingTable(e)) throw e
    return { name, icon: null }
  }
}

/** Slack's own workspace photo takes precedence for an authenticated person's Slack posts. Grants retain their
 *  explicit subject face: a grant label is not evidence that its holder owns a Slack account. */
export async function slackSenderOf(env: ActivityEnv, b: Pick<Burst, 'actor' | 'via'>, fallback: Sender): Promise<Sender> {
  if (b.via !== 'session') return fallback
  const person = (await slackPeople(env, [b.actor]))[b.actor.toLowerCase()]
  const image = person?.image
  if (!image) return fallback
  try {
    if (new URL(image).protocol === 'https:') return { name: fallback.name, icon: image }
  } catch { /* an unusable Slack photo falls back to the site's face */ }
  return fallback
}

async function render(env: ActivityEnv, b: StoredBurst, conf: KindsConf, now: number): Promise<void> {
  const db = env.DB!
  await db.prepare('UPDATE activity_bursts SET rendered_ms = ?, dirty = 0 WHERE id = ?').bind(now, b.id).run()
  const events = await burstEvents(db, b, conf)
  const h = highlights(events)
  const lineKinds = KIND_DEFS.filter(d => conf[d.kind]?.notify === 'line').map(d => [d.kind, events.filter(e => e.kind === d.kind)] as const).filter(([, es]) => es.length)
  /** A client line (an error, a resumption) is its own text (escaped for the sink), on its page, its time linked to its
   *  session (an error to the session's errors). */
  const lineEvents = (es: readonly ServerEvent[], fmt: Fmt) => es.map(e => {
    const sid = (e.kind === 'error' || e.kind === 'resume') && typeof e.detail?.sid === 'string' ? e.detail.sid : null
    if (!sid) return { ...e, line: activityEventLine(e, fmt, b.origin) }
    return { ...e, line: `${fmt.esc(e.line)}${e.route ? ` on ${fmt.code(e.route)}` : ''}`, href: sessionUrl(b.origin, sid, e.kind === 'error' ? 'errors' : undefined) }
  })
  const s = sinks(env)
  const who = s.slack || s.discord ? await senderOf(env, b, now) : { name: '', icon: null }
  if (s.slack) {
    const channel = env.SLACK_ADMIN_CHANNEL!
    const asUser = (await slackScopes(env)).has('chat:write.customize')
    const slackWho = asUser ? await slackSenderOf(env, b, who) : who
    const sender = asUser ? { username: slackWho.name, ...(slackWho.icon ? { icon_url: slackWho.icon } : {}) } : {}
    const text = renderHeadline(b, { fmt: SLACK, conf, name: who.name }, asUser)
    const parent = await ensurePost(db, b.id, 'slack', 'parent', now,
      async () => (await slackApi(env, 'chat.postMessage', { channel, text, ...sender, unfurl_links: false, unfurl_media: false })).ts ?? null,
      ts => slackApi(env, 'chat.update', { channel, ts, text }))
    if (parent) {
      const details = renderDetails(b, { fmt: SLACK, conf, highlights: h })
      if (details) {
        await ensurePost(db, b.id, 'slack', 'details', now,
          async () => (await slackApi(env, 'chat.postMessage', { channel, thread_ts: parent, text: details, ...sender, unfurl_links: false, unfurl_media: false })).ts ?? null,
          ts => slackApi(env, 'chat.update', { channel, ts, text: details }))
      }
      for (const [kind, es] of lineKinds) {
        const reply = renderLines(kind, lineEvents(es, SLACK), SLACK)
        await ensurePost(db, b.id, 'slack', kind, now,
          async () => (await slackApi(env, 'chat.postMessage', { channel, thread_ts: parent, text: reply, ...sender, unfurl_links: false, unfurl_media: false })).ts ?? null,
          ts => slackApi(env, 'chat.update', { channel, ts, text: reply }))
      }
    }
  }
  if (s.discord) {
    const url = env.DISCORD_ADMIN_WEBHOOK!
    const content = renderDiscord(
      renderSummary(b, { fmt: DISCORD, conf, highlights: h, name: who.name }),
      lineKinds.map(([kind, es]) => renderLines(kind, lineEvents(es, DISCORD), DISCORD)),
      `${b.origin}/admin/activity?burst=${b.id}`,
    )
    const body = JSON.stringify({ content, allowed_mentions: { parse: [] } })
    const created = JSON.stringify({ content, allowed_mentions: { parse: [] }, username: who.name.slice(0, 80), ...(who.icon ? { avatar_url: who.icon } : {}) })
    const headers = { 'content-type': 'application/json' }
    await ensurePost(db, b.id, 'discord', 'parent', now,
      async () => {
        const r = await fetch(`${url}?wait=true`, { method: 'POST', headers, body: created })
        if (!r.ok) { console.log(`activity: discord post ${r.status}`); return null }
        return ((await r.json()) as { id?: string }).id ?? null
      },
      async msg => {
        const r = await fetch(`${url}/messages/${msg}`, { method: 'PATCH', headers, body })
        if (!r.ok) console.log(`activity: discord edit ${r.status}`)
      })
  }
}

// ---- retention and admin reads ----

/** Delete bursts, events and posts received before `cutoffS`; a missing table is a nop. */
export async function purgeActivity(db: D1Database, cutoffS: number): Promise<void> {
  try {
    await db.batch([
      db.prepare('DELETE FROM activity_events WHERE ts < ?').bind(cutoffS),
      db.prepare('DELETE FROM activity_posts WHERE burst_id IN (SELECT id FROM activity_bursts WHERE last_ts < ?)').bind(cutoffS),
      db.prepare('DELETE FROM activity_bursts WHERE last_ts < ?').bind(cutoffS),
    ])
  } catch (e) {
    if (!missingTable(e)) throw e
  }
}

export interface BurstFilter { actor?: string; kind?: string; since?: number; limit?: number }

/** Bursts, most recent first. `actor` matches a substring, case-insensitively; `kind` keeps bursts that counted it. */
export async function listBursts(db: D1Database, f: BurstFilter): Promise<StoredBurst[]> {
  const where: string[] = []
  const args: unknown[] = []
  if (f.actor) { where.push('LOWER(actor) LIKE ?'); args.push(`%${f.actor.toLowerCase()}%`) }
  if (f.kind) { where.push("COALESCE(json_extract(counts, '$.' || ?), 0) > 0"); args.push(f.kind) }
  if (f.since !== undefined) { where.push('last_ts >= ?'); args.push(f.since) }
  const limit = Math.min(Math.max(f.limit ?? 200, 1), 500)
  const r = await db.prepare(`SELECT * FROM activity_bursts${where.length ? ` WHERE ${where.join(' AND ')}` : ''} ORDER BY last_ts DESC, id DESC LIMIT ${limit}`)
    .bind(...args).all<BurstRow>()
  return r.results.map(parse)
}

/** A burst's Slack thread permalink parts (channel, parent ts), if posted. */
export async function slackParent(db: D1Database, id: number): Promise<string | null> {
  const r = await db.prepare("SELECT msg_id FROM activity_posts WHERE burst_id = ? AND sink = 'slack' AND part = 'parent'").bind(id).first<{ msg_id: string }>()
  return r && r.msg_id !== PENDING ? r.msg_id : null
}
