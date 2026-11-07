// /api/activity — the activity feed's admin surface (specs/done/activity-feed.md), admin only:
//
//   GET /api/activity/kinds              { kinds: KindState[], sinks }   each kind's config (defaults + `/admin` edits)
//   PUT /api/activity/kinds/:kind        { log, notify } → the same       one kind's config; lands in `admin_edits`
//   GET /api/activity/bursts             { bursts }                       ?actor= &kind= &since= (YYYY-MM-DD) &limit=
//   GET /api/activity/bursts/:id         { burst, events, slack }         one burst, its events, its thread
//   POST /api/activity/render            { rendered }                     re-render bursts in their sinks: ?burst=ID…
//                                                                         or ?since= (YYYY-MM-DD: every burst active since)
import { type Ctx, type Env as AuthEnv, json, requireAdmin } from '../../_lib/auth.js'
import { type ActivityEnv, burstEvents, listBursts, loadBurst, readKinds, rerender, sinks, slackParent, writeKind } from '../../_lib/activity.js'
import { KIND_BY_NAME, KIND_DEFS, NOTIFY, type Notify } from '../../_lib/activityShape.js'
import { noteActivity } from '../../_lib/activityHook.js'

type C = Ctx & { env: AuthEnv & ActivityEnv; params?: { path?: string | string[] } }

const NO_STORE = { 'cache-control': 'no-store' }
const NOT_SET_UP = 'activity feed is not set up on this deployment (migration `activity-feed`)'
const notSetUp = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

async function kinds(ctx: C): Promise<Response> {
  const db = ctx.env.DB!
  const conf = await readKinds(db, Date.now(), undefined, true)
  return json({
    kinds: KIND_DEFS.map(d => ({ kind: d.kind, source: d.source, covers: d.covers, default: { log: d.log, notify: d.notify }, ...conf[d.kind] })),
    sinks: sinks(ctx.env),
  }, 200, NO_STORE)
}

/** A Slack message's permalink, from its channel and ts. */
const permalink = (channel: string, ts: string): string => `https://slack.com/archives/${channel}/p${ts.replace('.', '')}`

export const onRequest = async (ctx: C): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const db = ctx.env.DB
  if (!db) return json({ error: 'no D1 bound' }, 503)
  const p = ctx.params?.path
  const segs = (Array.isArray(p) ? p : (p ?? '').split('/')).filter(Boolean).map(decodeURIComponent)
  const m = ctx.request.method
  const q = new URL(ctx.request.url).searchParams
  try {
    if (segs[0] === 'kinds' && segs.length === 1 && m === 'GET') return kinds(ctx)
    if (segs[0] === 'kinds' && segs.length === 2 && m === 'PUT') {
      const kind = segs[1]
      if (!KIND_BY_NAME[kind]) return json({ error: `unknown kind ${kind}` }, 400)
      let body: { log?: unknown; notify?: unknown }
      try { body = await ctx.request.json() } catch { return json({ error: 'body is not JSON' }, 400) }
      if (typeof body?.log !== 'boolean') return json({ error: 'log must be true or false' }, 400)
      if (!(NOTIFY as readonly unknown[]).includes(body.notify)) return json({ error: `notify must be one of ${NOTIFY.join(', ')}` }, 400)
      await writeKind(db, kind, { log: body.log, notify: body.notify as Notify }, id.email ?? id.name ?? 'unknown', Math.floor(Date.now() / 1000))
      noteActivity(ctx, { line: `set activity kind \`${kind}\`: record ${body.log ? 'on' : 'off'}, notify ${body.notify}` })
      return kinds(ctx)
    }
    if (segs[0] === 'bursts' && segs.length === 1 && m === 'GET') {
      const since = q.get('since')
      const sinceMs = since ? Date.parse(since) : NaN
      const limit = Number(q.get('limit'))
      const bursts = await listBursts(db, {
        actor: q.get('actor') || undefined,
        kind: q.get('kind') || undefined,
        since: Number.isFinite(sinceMs) ? Math.floor(sinceMs / 1000) : undefined,
        limit: Number.isFinite(limit) && limit > 0 ? limit : undefined,
      })
      return json({ bursts }, 200, NO_STORE)
    }
    if (segs[0] === 'bursts' && segs.length === 2 && /^\d+$/.test(segs[1]) && m === 'GET') {
      const bid = Number(segs[1])
      const burst = await loadBurst(db, bid)
      if (!burst) return json({ error: `no burst ${bid}` }, 404)
      const ts = await slackParent(db, bid)
      const channel = ctx.env.SLACK_ADMIN_CHANNEL
      return json({ burst, events: await burstEvents(db, burst, await readKinds(db, Date.now())), slack: ts && channel ? permalink(channel, ts) : null }, 200, NO_STORE)
    }
    if (segs[0] === 'render' && segs.length === 1 && m === 'POST') {
      const since = q.get('since')
      const sinceMs = since ? Date.parse(since) : NaN
      if (since && Number.isNaN(sinceMs)) return json({ error: `bad since ${since}` }, 400)
      const ids = since
        ? (await db.prepare('SELECT id FROM activity_bursts WHERE last_ts >= ? ORDER BY id').bind(Math.floor(sinceMs / 1000)).all<{ id: number }>()).results.map(r => r.id)
        : q.getAll('burst').filter(b => /^\d+$/.test(b)).map(Number)
      if (!ids.length) return json({ error: 'name bursts (?burst=ID) or ?since=YYYY-MM-DD' }, 400)
      const rendered = await rerender(ctx.env, ids)
      noteActivity(ctx, { line: `re-rendered ${rendered.length} activity burst${rendered.length === 1 ? '' : 's'}` })
      return json({ rendered }, 200, NO_STORE)
    }
  } catch (e) {
    if (notSetUp(e)) return json({ error: NOT_SET_UP }, 503)
    throw e
  }
  return json({ error: 'not found' }, 404)
}
