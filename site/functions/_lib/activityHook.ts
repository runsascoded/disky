/**
 * Server events from successful writes (specs/done/activity-feed.md § Server events): `_middleware.ts` calls `captureWrite`
 * after a non-GET request answered 2xx; a route says what the request did with `noteActivity`, on Pages' per-request
 * `ctx.data` (the middleware and the route each get their own copy of the `Request`, but share `data`), else the event
 * gets a generic line.
 */
import { type Ctx, identify } from './auth.js'
import { type ActivityEnv, recordServer, type Wait } from './activity.js'
import { type Detail, genericLine, type ServerEvent } from './activityShape.js'
import { bodySummary } from './sessionLogShape.js'

/** What a request did, one detail per thing (an `/api/actions` POST of 12 actions notes 12). Outside Pages (no
 *  `ctx.data`) a nop. */
export function noteActivity(ctx: Pick<Ctx, 'data'>, ...details: Detail[]): void {
  if (!ctx.data) return
  const prior = ctx.data.activity as Detail[] | undefined
  ctx.data.activity = prior ? [...prior, ...details] : details
}

/** A write's body, for its summary: JSON only (an avatar upload is its type), read from a clone before the route
 *  consumes the original. */
export function readBody(req: Request): Promise<string | null> {
  const type = req.headers.get('content-type') ?? ''
  if (!type.includes('json')) return Promise.resolve(null)
  return req.clone().text().catch(() => null)
}

/** Who made a request, as a burst's actor: signed-in email, else the grant's name; null for signed-out viewers. */
export async function actorOf(ctx: Ctx): Promise<{ actor: string; via: string } | null> {
  const id = await identify(ctx)
  if (!id || id.via === 'public') return null
  return { actor: id.email ?? (id.name ? `grant:${id.name}` : 'grant'), via: id.via }
}

/** Adds what a route couldn't cheaply (an assignment's size), before the events are stored. */
export type Enrich = (ctx: Ctx, events: ServerEvent[]) => Promise<void>

/** Record a successful write as server events (one per noted detail, else one generic). */
export async function captureWrite(
  ctx: Ctx, kind: string, body: Promise<string | null> | null, wait: Wait, now: number = Date.now(), enrich?: Enrich,
): Promise<void> {
  const env = ctx.env as ActivityEnv
  if (!env.DB) return
  try {
    const who = await actorOf(ctx)
    if (!who) return
    const url = new URL(ctx.request.url)
    const method = ctx.request.method
    const ts = Math.floor(now / 1000)
    const base = { ts, actor: who.actor, via: who.via, method, route: url.pathname }
    const details = ctx.data?.activity as Detail[] | undefined
    let events: ServerEvent[]
    if (details?.length) {
      events = details.map(({ line, kind: k, ...rest }) => ({ ...base, kind: k ?? kind, line, ...(Object.keys(rest).length ? { detail: rest } : {}) }))
    } else {
      const summary = body ? bodySummary(await body) : null
      events = [{ ...base, kind, line: genericLine(method, url.pathname), ...(summary ? { detail: { body: summary } } : {}) }]
    }
    if (enrich) await enrich(ctx, events)
    await recordServer(env, { ...who, origin: url.origin }, events, wait, now)
  } catch (e) {
    console.log(`activity: capture failed: ${(e as Error).message}`)
  }
}
